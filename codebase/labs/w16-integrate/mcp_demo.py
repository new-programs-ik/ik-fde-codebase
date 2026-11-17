#!/usr/bin/env python3
"""W16 demo — the Meridian MCP server: one governed door, per-user RBAC, audit.

Self-contained (stdlib only). It teaches the load-bearing W16 lesson concretely:
the agent never touches a system of record directly — it calls ONE governed door
(the MCP server), which enforces scope in a DETERMINISTIC layer the LLM cannot
talk past, and writes an append-only audit row for EVERY access: allowed,
denied, or not-found.

Run everything (mock, zero setup):

    python3 mcp_demo.py

You will see four proofs, in order:

  1. THE TWO-ANALYST TEST — the same question, two differently-scoped analysts,
     two correctly-filtered answers + matching audit rows (the M3 criterion);
  2. IDEMPOTENT WRITE — create_ticket called twice with the same
     Idempotency-Key: the replay returns the ORIGINAL ticket, marked;
  3. RETRY/BACKOFF — fetch_transactions rides through the FlakyLegacyCore
     (503 on the first call), and the tool absorbs it with exponential backoff;
  4. ENUMERATION CHECK — a nonexistent id and an out-of-scope id return the
     SAME denial shape, so a Type-B analyst cannot enumerate Type-A ids.

LIVE mode — the SAME governed door, against a real Meridian platform. Export two
differently-scoped analyst keys and pass --live:

    export MERIDIAN_KEY_A="mrdn_..."     # a type_a-scoped analyst key
    export MERIDIAN_KEY_B="mrdn_..."     # a type_b-scoped analyst key
    python3 mcp_demo.py --live           # defaults to dispute 29

Which platform it hits is one env var — the hosted class instance by default, or
YOUR self-hosted portal (local Docker, or your own EC2 box):

    export MERIDIAN_BASE_URL="http://localhost:3100/api/v1"     # self-hosted
    # unset  → https://ik-fde-casestudy-bank.vercel.app/api/v1  (hosted default)

In live mode the two-analyst test runs against real scoped keys (200 vs 403
enforced by the platform's API layer + Postgres), create_ticket writes a REAL
ticket on the dispute (subject prefix "Governed-door recommendation") and the
replay proves Idempotency-Key end to end, then the platform's own audit trail
is fetched via GET /audit. Note: the live seed's ids differ from the lab
narrative — 29 is the seeded Type-A dispute you saw in W15; 4417 lives in the
offline mock.

In the real lab students replace the in-memory backends with calls to the live
Meridian platform (platform/agent/src/meridian_agent/db.py / the /api/v1 REST
API) and wire OAuth2 on-behalf-of token exchange; the SHAPE of the server —
typed tools, scope check, audit — is identical. Build yours at home in your
fork of this repo: https://github.com/new-programs-ik/ik-fde-codebase
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

BASE_URL = os.environ.get(
    "MERIDIAN_BASE_URL", "https://ik-fde-casestudy-bank.vercel.app/api/v1").rstrip("/")

# --- tiny stand-in for the three client backends (real lab: Meridian platform) ---
_DISPUTES = {
    "4417": {"dispute_id": "4417", "dispute_type": "A", "merchant": "NORTHGATE DIGITAL",
             "amount_usd": 312.00, "status": "intake", "reason_code": "10.4"},
    "8120": {"dispute_id": "8120", "dispute_type": "B", "merchant": "CRESTLINE MOBILE",
             "amount_usd": 21.17, "status": "investigating", "reason_code": "13.1"},
}
_TRANSACTIONS = {
    "4417": [{"txn_id": "T-90021", "amount_usd": 312.00, "channel": "online", "mcc": "5815"}],
    "8120": [{"txn_id": "T-44310", "amount_usd": 21.17, "channel": "recurring", "mcc": "4899"}],
}
_POLICY = [
    {"chunk_id": 1, "reg": "Reg E", "text": "Provisional credit within 10 business days while investigating."},
    {"chunk_id": 7, "reg": "Reg E", "text": "Type-A unauthorized claims require fraud-evidence gather."},
]


class FlakyLegacyCore:
    """The 1990s core-banking middleware: 503s the first call, answers after.

    fetch_transactions rides through this, so the retry/backoff pattern is
    exercised for real — the M3 'provided flaky backend' is this class."""

    def __init__(self):
        self.calls = 0

    def get_transactions(self, dispute_id: str) -> dict:
        self.calls += 1
        if self.calls == 1:  # first call always fails — a structured error, never a stack trace
            return {"error": "unavailable", "http": 503, "retry_after": 0.05}
        return {"transactions": _TRANSACTIONS.get(dispute_id, [])}


class AccessDenied(Exception):
    pass


@dataclass
class AuditLog:
    rows: list[dict] = field(default_factory=list)

    def write(self, identity: str, scope: str, action: str, resource: str, decision: str):
        self.rows.append({"acting_identity": identity, "scope": scope,
                          "action": action, "resource": resource, "decision": decision})


@dataclass
class Identity:
    """The result of an OAuth2 on-behalf-of exchange: who the agent is acting as."""
    email: str
    scope: str          # "type_a" | "type_b" | "all"
    api_key: str = ""   # live mode only: the scoped mrdn_ key IS the badge


class MeridianMCPServer:
    """The one governed door. Every tool: typed args, scope check, audit row.

    Same class, two transports: the in-memory mock (default) or the deployed
    platform (live=True). The SHAPE of the door — scope before data, audit on
    every access — does not change."""

    def __init__(self, audit: AuditLog, live: bool = False):
        self.audit = audit
        self.live = live
        self._legacy = FlakyLegacyCore()
        self._tickets_by_idem: dict[str, dict] = {}   # idempotency: key -> original ticket
        self._ticket_seq = 0

    # ---- the deterministic scope gate (the LLM CANNOT bypass this) ----
    def _authorize(self, ident: Identity, dispute_type: str | None, action: str, resource: str) -> bool:
        """dispute_type=None means the record does not exist. A nonexistent id
        gets the SAME denial as an out-of-scope id — otherwise a Type-B analyst
        could enumerate Type-A dispute ids by comparing the two errors. Every
        access — allow, deny, or not-found — writes an audit row (the CISO's
        condition), and the deny row is written BEFORE the raise."""
        allowed = dispute_type is not None and (
            ident.scope == "all" or ident.scope == f"type_{dispute_type.lower()}")
        self.audit.write(ident.email, ident.scope, action, resource,
                         "allow" if allowed else "deny")
        if not allowed:
            raise AccessDenied(
                f"{ident.email} (scope={ident.scope}) may not access {resource}")
        return True

    # ---- typed tools (the agent's only interface to client systems) ----
    def lookup_dispute(self, ident: Identity, dispute_id: str) -> dict:
        if self.live:
            return self._live_lookup(ident, dispute_id)
        d = _DISPUTES.get(dispute_id)
        self._authorize(ident, d["dispute_type"] if d else None,
                        "lookup_dispute", f"dispute/{dispute_id}")
        return d

    def fetch_transactions(self, ident: Identity, dispute_id: str) -> list:
        d = _DISPUTES.get(dispute_id)
        self._authorize(ident, d["dispute_type"] if d else None,
                        "fetch_transactions", f"dispute/{dispute_id}/txns")
        # the legacy core is flaky — absorb it with exponential backoff so the
        # agent never sees the 503 (structured errors let CODE decide, not the model)
        delay = 0.05
        for attempt in range(1, 4):
            out = self._legacy.get_transactions(dispute_id)
            if out.get("error"):
                print(f"  attempt {attempt}: 503 legacy core unavailable → backoff {delay:.2f}s, retrying")
                time.sleep(min(out.get("retry_after", delay), 0.05))
                delay *= 2
                continue
            return out["transactions"]
        raise RuntimeError("legacy core still down after retries")  # becomes a structured error upstream

    def create_ticket(self, ident: Identity, dispute_id: str, subject: str,
                      body: str, idem_key: str) -> dict:
        if self.live:
            return self._live_create_ticket(ident, dispute_id, subject, body, idem_key)
        d = _DISPUTES.get(dispute_id)
        self._authorize(ident, d["dispute_type"] if d else None,
                        "create_ticket", f"dispute/{dispute_id}/tickets")
        if idem_key in self._tickets_by_idem:  # replay → the ORIGINAL ticket, marked
            return {**self._tickets_by_idem[idem_key], "idempotent_replay": True}
        self._ticket_seq += 1
        t = {"ticket_id": f"TCK-{dispute_id}-{9000 + self._ticket_seq}",
             "dispute_id": dispute_id, "subject": subject, "status": "open"}
        self._tickets_by_idem[idem_key] = t
        return dict(t)

    def search_policy(self, ident: Identity, query: str) -> list:
        # policy KB is read-only and unscoped; still audited
        self.audit.write(ident.email, ident.scope, "search_policy", f"policy?q={query}", "allow")
        return [c for c in _POLICY if query.lower() in c["text"].lower()] or _POLICY[:1]

    # ---- live transport (--live): the deployed /api/v1 layer + Postgres do the enforcing ----
    def _live_req(self, method: str, path: str, key: str,
                  payload: dict | None = None, idem_key: str | None = None):
        headers = {"Authorization": f"Bearer {key}"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode() or "{}")
            except Exception:
                body = {}
            return e.code, body

    def _live_lookup(self, ident: Identity, dispute_id: str) -> dict:
        status, body = self._live_req("GET", f"/disputes/{dispute_id}", ident.api_key)
        # the platform wrote the authoritative audit row (see GET /audit);
        # mirror it locally so the printed log reads the same in both modes
        self.audit.write(ident.email, ident.scope, "lookup_dispute",
                         f"dispute/{dispute_id}", "allow" if status == 200 else "deny")
        if status != 200:
            err = body.get("error") or {}
            raise AccessDenied(f"HTTP {status} {err.get('code', 'denied')} — "
                               f"{err.get('message', 'access denied')}")
        return body["dispute"]

    def _live_create_ticket(self, ident: Identity, dispute_id: str, subject: str,
                            body_text: str, idem_key: str) -> dict:
        status, body = self._live_req(
            "POST", f"/disputes/{dispute_id}/tickets", ident.api_key,
            payload={"channel": "chat", "subject": subject, "body": body_text},
            idem_key=idem_key)
        self.audit.write(ident.email, ident.scope, "create_ticket",
                         f"dispute/{dispute_id}/tickets",
                         "allow" if status in (200, 201) else "deny")
        if status not in (200, 201):
            err = body.get("error") or {}
            raise AccessDenied(f"HTTP {status} {err.get('code', 'denied')} — "
                               f"{err.get('message', 'access denied')}")
        t = body.get("ticket") or {}
        if body.get("idempotent_replay"):
            t = {**t, "idempotent_replay": True}
        return t


def _fmt_dispute(d: dict) -> str:
    merchant = d.get("merchant") or d.get("merchant_name") or "?"
    if "amount_usd" in d:
        amount = float(d["amount_usd"])
    else:
        amount = float(d.get("amount_cents") or 0) / 100.0
    return f"Type-{d['dispute_type']} · {merchant} · ${amount:.2f} · {d['status']}"


def _ask(server: MeridianMCPServer, ident: Identity, dispute_id: str) -> str:
    """Same 'question' (lookup a dispute) issued by whichever identity is passed."""
    try:
        return _fmt_dispute(server.lookup_dispute(ident, dispute_id))
    except AccessDenied as e:
        return f"403 DENIED — {e}"


def two_analyst_test(dispute_id: str = "4417", server: MeridianMCPServer | None = None,
                     analyst_a: Identity | None = None, analyst_b: Identity | None = None) -> bool:
    server = server or MeridianMCPServer(AuditLog())
    audit = server.audit
    analyst_a = analyst_a or Identity("analyst_a@meridian.test", "type_a")
    analyst_b = analyst_b or Identity("analyst_b@meridian.test", "type_b")

    print(f"\n=== TWO-ANALYST TEST — both ask: lookup_dispute({dispute_id}) ===\n")
    ra = _ask(server, analyst_a, dispute_id)
    rb = _ask(server, analyst_b, dispute_id)
    print(f"  Analyst A ({analyst_a.scope}):  {ra}")
    print(f"  Analyst B ({analyst_b.scope}):  {rb}")

    print("\n--- append-only audit log ---")
    for row in audit.rows:
        print("  " + json.dumps(row))

    # The proof: same question, two identities, correctly-filtered answers.
    a_allowed = "DENIED" not in ra
    b_denied = "DENIED" in rb  # the dispute is Type-A, so the Type-B analyst must be denied
    decisions = [r["decision"] for r in audit.rows]
    ok = a_allowed and b_denied and "allow" in decisions and "deny" in decisions
    print(f"\nRESULT: {'PASS ✓ scope holds — different answers + matching audit' if ok else 'FAIL ✗'}")
    return ok


def idempotent_write_demo(server: MeridianMCPServer, ident: Identity,
                          dispute_id: str = "4417") -> bool:
    print(f"\n=== IDEMPOTENT WRITE — create_ticket, same Idempotency-Key twice ===\n")
    key = "w16-demo-idem-1"
    subject = f"Governed-door recommendation — dispute {dispute_id}"
    first = server.create_ticket(ident, dispute_id, subject, "Recommend provisional credit; human to confirm.", key)
    replay = server.create_ticket(ident, dispute_id, subject, "Recommend provisional credit; human to confirm.", key)
    print("  first : " + json.dumps(first, ensure_ascii=False))
    print("  replay: " + json.dumps(replay, ensure_ascii=False))
    ok = replay["ticket_id"] == first["ticket_id"] and replay.get("idempotent_replay") is True
    if ok:
        print("  ✓ same ticket_id, replay marked — one ticket exists; the retry was SAFE")
    return ok


def flaky_backend_demo(server: MeridianMCPServer, ident: Identity,
                       dispute_id: str = "4417") -> bool:
    print(f"\n=== RETRY/BACKOFF — fetch_transactions through the flaky legacy core ===\n")
    txns = server.fetch_transactions(ident, dispute_id)
    print("  result: " + json.dumps(txns))
    print("  ✓ the agent never saw the 503 — the TOOL absorbed it with backoff")
    return bool(txns)


def enumeration_check(server: MeridianMCPServer, analyst_b: Identity) -> bool:
    print(f"\n=== ENUMERATION CHECK — not-found must look exactly like out-of-scope ===\n")
    out_of_scope = _ask(server, analyst_b, "4417")   # exists, but Type-A
    nonexistent = _ask(server, analyst_b, "9999")    # does not exist
    print(f"  Analyst B asks for 4417 (exists, Type-A):  {out_of_scope}")
    print(f"  Analyst B asks for 9999 (does not exist):  {nonexistent}")
    same_shape = out_of_scope.startswith("403 DENIED") and nonexistent.startswith("403 DENIED")
    audited = [r for r in server.audit.rows if r["resource"] == "dispute/9999"
               and r["decision"] == "deny"]
    if same_shape and audited:
        print("  ✓ identical denial shape + both denials audited — Type-B cannot enumerate Type-A ids")
    return same_shape and bool(audited)


def mock_run(dispute_id: str = "4417") -> bool:
    server = MeridianMCPServer(AuditLog())
    analyst_a = Identity("analyst_a@meridian.test", "type_a")
    analyst_b = Identity("analyst_b@meridian.test", "type_b")

    ok = two_analyst_test(dispute_id, server=server, analyst_a=analyst_a, analyst_b=analyst_b)
    ok = idempotent_write_demo(server, analyst_a, dispute_id) and ok
    ok = flaky_backend_demo(server, analyst_a, dispute_id) and ok
    ok = enumeration_check(server, analyst_b) and ok

    print(f"\nDEMO: {'PASS ✓ all four proofs hold — scope, idempotent replay, retry/backoff, enumeration-safe denials' if ok else 'FAIL ✗'}")
    return ok


def live_run(dispute_id: str = "29") -> bool:
    """The same governed door, against the deployed platform with two REAL
    scoped analyst keys. Run in class at ~2:55 — then refresh the Ops Console
    on dispute 29 to show the new ticket, and GET /audit shows the rows."""
    key_a = os.environ.get("MERIDIAN_KEY_A")
    key_b = os.environ.get("MERIDIAN_KEY_B")
    if not (key_a and key_b):
        print("--live needs TWO differently-scoped analyst keys exported:\n"
              "  export MERIDIAN_KEY_A=mrdn_...   # type_a scope\n"
              "  export MERIDIAN_KEY_B=mrdn_...   # type_b scope")
        raise SystemExit(2)

    audit = AuditLog()
    server = MeridianMCPServer(audit, live=True)

    print("\n" + "=" * 74)
    print("  LIVE — the same governed door, against the deployed Meridian platform")
    print(f"  {BASE_URL}")
    print("=" * 74)

    # resolve who each badge belongs to (GET /me)
    idents = {}
    for label, key in (("A", key_a), ("B", key_b)):
        status, me = server._live_req("GET", "/me", key)
        if status != 200:
            print(f"  key {label}: /me returned HTTP {status} — check the key")
            return False
        me = me.get("me", me)
        idents[label] = Identity(me.get("email", f"analyst_{label.lower()}"),
                                 me.get("scope", "?"), key)
        print(f"  key {label} resolves to: {idents[label].email}  (scope={idents[label].scope})")

    ok = two_analyst_test(dispute_id, server=server,
                          analyst_a=idents["A"], analyst_b=idents["B"])

    # the write, through the same door — a REAL ticket lands on the dispute
    print(f"\n=== LIVE WRITE — create_ticket on dispute {dispute_id} (Idempotency-Key honored) ===\n")
    idem = f"w16-live-{uuid.uuid4().hex[:8]}"
    subject = f"Governed-door recommendation — dispute {dispute_id} (W16 two-analyst proof)"
    body = ("Recommendation issued through the W16 governed MCP door: fraud-class, "
            "issue provisional credit per Reg E; human analyst to confirm.")
    try:
        first = server.create_ticket(idents["A"], dispute_id, subject, body, idem)
        replay = server.create_ticket(idents["A"], dispute_id, subject, body, idem)
        print("  first : " + json.dumps({k: first.get(k) for k in ("ticket_id", "subject", "status")},
                                        ensure_ascii=False))
        print("  replay: " + json.dumps({k: replay.get(k) for k in ("ticket_id", "idempotent_replay")}))
        write_ok = (replay.get("ticket_id") == first.get("ticket_id")
                    and replay.get("idempotent_replay") is True)
        if write_ok:
            print(f"  ✓ real ticket {first.get('ticket_id')} exists ONCE — refresh the Ops "
                  f"Console on dispute {dispute_id} to see it")
    except AccessDenied as e:
        print(f"  write failed: {e}")
        write_ok = False
    ok = write_ok and ok

    # the platform's own audit trail — the authoritative record
    print("\n=== LIVE AUDIT — GET /audit (the platform's own append-only trail) ===\n")
    status, body = server._live_req("GET", "/audit?limit=8", key_a)
    rows = body.get("audit", []) if status == 200 else []
    for row in rows[:8]:
        print("  " + json.dumps({k: row.get(k) for k in
                                 ("action", "resource_type", "resource_id", "scope", "decision")}))
    status_b, body_b = server._live_req("GET", "/audit?limit=8", key_b)
    rows_b = body_b.get("audit", []) if status_b == 200 else []
    deny_rows = [r for r in rows_b if r.get("decision") == "deny"]
    if deny_rows:
        print("  (key B's trail) " + json.dumps({k: deny_rows[0].get(k) for k in
                                                 ("action", "resource_id", "scope", "decision")}))
    ok = ok and bool(rows)

    print(f"\nLIVE RESULT: {'PASS ✓ — same action as W15, now scoped and audited; the circle closes through the governed door' if ok else 'FAIL ✗ — check keys/scopes'}")
    return ok


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if a != "--live"]
    dispute = args[0] if args else None
    if "--live" in sys.argv[1:]:
        ok = live_run(dispute or "29")
    else:
        ok = mock_run(dispute or "4417")
    raise SystemExit(0 if ok else 1)
