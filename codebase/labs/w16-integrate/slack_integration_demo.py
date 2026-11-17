#!/usr/bin/env python3
"""W16 demo — a REAL external integration through the governed door: Slack.

When the agent hands a hard case to a human, the escalation is a Block Kit
card in the on-call channel — posted through the SAME governed MCP door as
every other tool, and its centerpiece is a button that lands the on-call
analyst back in the Meridian Bank portal (the Ops Console), where the work
actually lives.

Two lessons in one file:

  1. Integration goes through the SAME governed MCP door as everything else —
     `notify_slack` is a typed tool, scope-checked in a deterministic layer, and
     every call writes an audit row. Slack is just another backend; the door
     doesn't care.

  2. Real integrations FAIL, and an FDE designs for that. We inject the failures
     you actually hit in production — rate limits, expired credentials, timeouts,
     duplicate sends, schema drift — and show the handling pattern for each:
     backoff, graceful fallback, circuit breaker, idempotency, defensive parsing.

Self-contained, stdlib only. Runs with zero setup:

    python3 slack_integration_demo.py                # mock: RBAC + all 5 failure sims
    python3 slack_integration_demo.py --simulate rate_limit
    python3 slack_integration_demo.py --real         # live post (see below)

Two live rungs (full setup: SLACK_LIVE_SETUP.md — the app manifest names the
bot "Meridian Escalations"):

  rung 1 — incoming webhook (2 min):
    export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/…"
    python3 slack_integration_demo.py --real

  rung 2 — bot token, the production door (chat.postMessage as the app;
  real Slack Web API semantics: ok:false errors, Retry-After on 429):
    export SLACK_BOT_TOKEN="xoxb-…"
    export SLACK_CHANNEL_ID="C…"
    python3 slack_integration_demo.py --real

When both are set, --real uses the bot token (the higher rung).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Which portal the card links back to: your self-hosted instance or the hosted class one.
#   export MERIDIAN_CONSOLE_URL="http://localhost:3100"   # self-hosted
OPS_CONSOLE = os.environ.get(
    "MERIDIAN_CONSOLE_URL", "https://ik-fde-casestudy-bank.vercel.app").rstrip("/")


# ───────────────────────── the governed door (mirrors mcp_demo.py) ─────────────────────────
class AccessDenied(Exception):
    pass


@dataclass
class Identity:
    """Who the agent is acting as (result of an OBO token exchange)."""
    email: str
    scope: str  # "type_a" | "type_b" | "all"


@dataclass
class AuditLog:
    rows: list[dict] = field(default_factory=list)

    def write(self, identity: str, scope: str, action: str, resource: str, decision: str, **extra):
        row = {"acting_identity": identity, "scope": scope, "action": action,
               "resource": resource, "decision": decision}
        row.update(extra)
        self.rows.append(row)
        return row


# ───────────────────────── the Slack transport (with injectable faults) ─────────────────────────
class SlackError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"slack {code}: {detail}")
        self.code = code
        self.detail = detail


class SlackTransport:
    """Posts a Block Kit message to an incoming webhook.

    `fault` injects a realistic failure so the resilience logic can be shown
    without a live Slack. In --real mode fault is None and it POSTs for real.
    """

    def __init__(self, webhook_url: str | None, fault: str | None = None):
        self.webhook_url = webhook_url
        self.fault = fault
        self._calls = 0

    def post(self, payload: dict, timeout: float = 5.0) -> dict:
        self._calls += 1

        # --- real POST (only when a webhook URL is configured and no fault injected) ---
        if self.webhook_url and not self.fault:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                self.webhook_url, data=data,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = r.read().decode()
                return {"http": r.status, "body": body}  # Slack returns "ok" text
            except urllib.error.HTTPError as e:
                raise SlackError(e.code, e.read().decode()[:200])
            except TimeoutError:
                raise SlackError("timeout", "no response")

        # --- simulated transports (deterministic, offline) ---
        if self.fault == "rate_limit":
            # 429 on the first attempt, then success — the classic backoff case
            if self._calls == 1:
                raise SlackError(429, json.dumps({"error": "rate_limited", "retry_after": 1}))
            return {"http": 200, "body": "ok"}
        if self.fault == "expired_webhook":
            raise SlackError(404, json.dumps({"error": "invalid_token"}))  # revoked/rotated
        if self.fault == "timeout":
            raise SlackError("timeout", "no response")  # every call hangs
        if self.fault == "schema_drift":
            # 200 OK but the response shape changed (no plain "ok" body)
            return {"http": 200, "body": json.dumps({"status": "accepted", "ts": "1720000000.1"})}
        # no fault → clean success
        return {"http": 200, "body": "ok"}


class SlackBotTransport:
    """The production rung: chat.postMessage with a bot token — the message is
    posted AS the app ("Meridian Escalations"), not through an anonymous webhook.

    Same .post(payload) contract as SlackTransport, but real Slack Web API
    semantics: HTTP 200 with {"ok": false, "error": …} on failure, and 429s
    carry a Retry-After header. Both are mapped onto SlackError so the door's
    backoff/breaker logic works unchanged — that's the point: the hardening
    lives in the door, not in the transport."""

    API_URL = "https://slack.com/api/chat.postMessage"

    def __init__(self, token: str, channel_id: str):
        self.token = token
        self.channel_id = channel_id

    def post(self, payload: dict, timeout: float = 5.0) -> dict:
        body = {"channel": self.channel_id,
                "text": payload.get("text", ""),
                "blocks": payload.get("blocks", [])}
        req = urllib.request.Request(
            self.API_URL, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:   # Web API rate limit — honor Retry-After
                retry_after = int(e.headers.get("Retry-After", "1"))
                raise SlackError(429, json.dumps({"error": "ratelimited",
                                                  "retry_after": retry_after}))
            raise SlackError(e.code, e.read().decode()[:200])
        except TimeoutError:
            raise SlackError("timeout", "no response")
        if not resp.get("ok"):
            # invalid_auth / channel_not_found / not_in_channel / …
            raise SlackError(resp.get("error", "unknown"), json.dumps(resp)[:200])
        return {"http": 200, "body": "ok", "ts": resp.get("ts"),
                "channel": resp.get("channel")}


# ───────────────────────── the ticket fallback (graceful degradation) ─────────────────────────
def create_meridian_ticket(dispute_id: str, summary: str, audit: AuditLog, ident: Identity) -> dict:
    """When Slack can't be reached, the human STILL gets the work — as a queue
    ticket in the Ops Console. Escalation must never be lost just because one
    channel is down."""
    ticket_id = f"TCK-{dispute_id}-fallback"
    audit.write(ident.email, ident.scope, "create_ticket_fallback",
                f"dispute/{dispute_id}", "allow", ticket_id=ticket_id)
    return {"ticket_id": ticket_id, "channel": "ops_console_queue", "fallback": True}


# ───────────────────────── the notify_slack tool (resilient, scoped, audited) ─────────────────────────
class SlackNotifier:
    """One typed tool behind the governed door. All the production hardening —
    scope gate, idempotency, retry/backoff, circuit breaker, defensive parse,
    fallback — lives HERE, in deterministic code, not in the model."""

    ESCALATE_SCOPES = {"type_a", "all"}   # who may page a human
    MAX_RETRIES = 3
    CB_THRESHOLD = 2                       # consecutive hard failures → open the breaker

    def __init__(self, transport: SlackTransport, audit: AuditLog):
        self.transport = transport
        self.audit = audit
        self._seen_idem: dict[str, dict] = {}   # idempotency: key → prior result
        self._consecutive_failures = 0
        self._circuit_open = False

    def _authorize(self, ident: Identity, dispute_type: str, resource: str):
        allowed = ident.scope in self.ESCALATE_SCOPES and (
            ident.scope == "all" or ident.scope == f"type_{dispute_type.lower()}")
        self.audit.write(ident.email, ident.scope, "notify_slack", resource,
                         "allow" if allowed else "deny")
        if not allowed:
            raise AccessDenied(
                f"{ident.email} (scope={ident.scope}) may not escalate Type-{dispute_type} to Slack")

    @staticmethod
    def _block_kit(channel, dispute_id, dispute_type, reg_e_status, recommendation) -> dict:
        return {
            "channel": channel,
            "text": f"Dispute {dispute_id} escalated for human review — open it in the Ops Console",  # notification fallback text
            "blocks": [
                {"type": "header",
                 "text": {"type": "plain_text", "text": f"🚨 Dispute {dispute_id} — human review needed"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Type:*\nType-{dispute_type}"},
                    {"type": "mrkdwn", "text": f"*Reg E:*\n{reg_e_status}"},
                ]},
                {"type": "section",
                 "text": {"type": "mrkdwn", "text": f"*Agent recommendation:*\n{recommendation}"}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Open in Ops Console"},
                     "url": f"{OPS_CONSOLE}/disputes/{dispute_id}"}]},
                {"type": "context", "elements": [
                    {"type": "mrkdwn",
                     "text": "through the governed door · scoped + audited · "
                             f"the work lives in the <{OPS_CONSOLE}/disputes/{dispute_id}|Meridian portal>"}]},
            ],
        }

    def notify(self, ident: Identity, channel: str, dispute_id: str, dispute_type: str,
               reg_e_status: str, recommendation: str, idem_key: str,
               log=print) -> dict:
        """The tool the agent calls. Returns a structured result the agent can reason about."""
        resource = f"slack/{channel}#dispute/{dispute_id}"

        # 1) scope gate (deterministic — the LLM cannot talk past it)
        self._authorize(ident, dispute_type, resource)

        # 2) idempotency — the same escalation twice must NOT double-page
        if idem_key in self._seen_idem:
            prior = self._seen_idem[idem_key]
            log(f"    idempotency: key '{idem_key}' already sent → returning prior result, not re-posting")
            return {**prior, "deduped": True}

        payload = self._block_kit(channel, dispute_id, dispute_type, reg_e_status, recommendation)

        # 3) circuit breaker — if Slack has been failing, don't even try; go straight to fallback
        if self._circuit_open:
            log("    circuit-breaker OPEN → skipping Slack, routing to the ticket fallback")
            result = create_meridian_ticket(dispute_id, recommendation, self.audit, ident)
            self._seen_idem[idem_key] = result
            return {**result, "delivered_via": "fallback", "reason": "circuit_open"}

        # 4) retry with exponential backoff (respect Retry-After on 429)
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                resp = self.transport.post(payload)
                delivered = self._parse(resp, log)     # 5) defensive parse
                self._consecutive_failures = 0
                result = {"delivered_via": "slack", "channel": channel, "ok": delivered}
                self._seen_idem[idem_key] = result
                return result
            except SlackError as e:
                if e.code == 429:
                    wait = self._retry_after(e.detail)
                    log(f"    attempt {attempt}: 429 rate-limited → backoff {wait}s, retrying")
                    self._sleep(wait * attempt)         # exponential-ish
                    continue
                # non-retryable transport failure (404 expired, timeout, 5xx)
                log(f"    attempt {attempt}: hard failure ({e}) → not retryable")
                self._trip(log)
                break

        # 6) exhausted / hard-failed → graceful fallback so the human still gets it
        log("    Slack undeliverable → routing to the ticket fallback (escalation is never lost)")
        result = create_meridian_ticket(dispute_id, recommendation, self.audit, ident)
        self._seen_idem[idem_key] = result
        return {**result, "delivered_via": "fallback"}

    # --- resilience helpers ---
    def _parse(self, resp: dict, log) -> bool:
        body = resp.get("body", "")
        if body == "ok":
            return True
        # schema drift: 200 but not the plain "ok" we expected — don't crash, log + accept
        log(f"    defensive-parse: unexpected Slack response body {body!r}; HTTP was "
            f"{resp.get('http')} → treating as delivered, flagging for review")
        return True

    def _trip(self, log):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.CB_THRESHOLD and not self._circuit_open:
            self._circuit_open = True
            log(f"    circuit-breaker TRIPPED after {self._consecutive_failures} consecutive "
                f"failures — further calls short-circuit to the fallback")

    @staticmethod
    def _retry_after(detail: str) -> int:
        try:
            return int(json.loads(detail).get("retry_after", 1))
        except Exception:
            return 1

    @staticmethod
    def _sleep(_seconds):
        pass  # mock: don't actually block the demo; real backoff would time.sleep here


# ───────────────────────── the demo scenarios ─────────────────────────
def _fresh(fault=None, webhook=None, bot=None):
    audit = AuditLog()
    transport = (SlackBotTransport(*bot) if bot
                 else SlackTransport(webhook, fault=fault))
    notifier = SlackNotifier(transport, audit)
    return notifier, audit


ANALYST_A = Identity("oncall_a@meridian.test", "type_a")   # may escalate Type-A
ANALYST_B = Identity("analyst_b@meridian.test", "type_b")  # may NOT escalate Type-A


def _escalate(notifier, ident, idem="esc-4417-1", log=print):
    return notifier.notify(
        ident, channel="#disputes-oncall", dispute_id="4417", dispute_type="A",
        reg_e_status="RED — 2 days to deadline",
        recommendation="Fraud-class, evidence thin. Issue provisional credit [chunk 1] and have a human confirm.",
        idem_key=idem, log=log)


def scenario_happy(webhook=None) -> tuple[str, str]:
    notifier, audit = _fresh(webhook=webhook)
    _escalate(notifier, ANALYST_A)
    return ("happy path (on-call, Type-A)",
            "posted to Slack, audit row written")


def scenario_rbac() -> tuple[str, str]:
    notifier, audit = _fresh()
    try:
        _escalate(notifier, ANALYST_B)
        return ("RBAC", "ERROR — deny did not fire")
    except AccessDenied as e:
        deny = [r for r in audit.rows if r["decision"] == "deny"]
        print(f"    403 DENIED — {e}")
        print(f"    audit deny row: {json.dumps(deny[-1])}")
        return ("RBAC (Type-B tries to escalate Type-A)",
                "403 DENIED at the door + deny row audited — Slack never called")


def scenario_rate_limit() -> tuple[str, str]:
    notifier, audit = _fresh(fault="rate_limit")
    r = _escalate(notifier, ANALYST_A)
    return ("rate_limit (Slack 429)",
            f"backoff + retry → delivered_via={r['delivered_via']}")


def scenario_expired() -> tuple[str, str]:
    notifier, audit = _fresh(fault="expired_webhook")
    r = _escalate(notifier, ANALYST_A)
    fb = [row for row in audit.rows if row["action"] == "create_ticket_fallback"]
    return ("expired_webhook (Slack 404 invalid_token)",
            f"graceful fallback → ticket {fb[-1]['ticket_id']} (escalation not lost)")


def scenario_timeout() -> tuple[str, str]:
    notifier, audit = _fresh(fault="timeout")
    # three escalations of DIFFERENT disputes: calls 1-2 fail and trip the breaker;
    # call 3 short-circuits straight to the fallback WITHOUT calling Slack at all.
    notifier.notify(ANALYST_A, "#disputes-oncall", "4417", "A", "RED", "…", idem_key="t-4417")
    notifier.notify(ANALYST_A, "#disputes-oncall", "9001", "A", "AMBER", "…", idem_key="t-9001")
    r3 = notifier.notify(ANALYST_A, "#disputes-oncall", "9002", "A", "AMBER", "…", idem_key="t-9002")
    return ("timeout (Slack hangs, ×3)",
            f"breaker opened after 2 fails; 3rd call short-circuited → reason={r3.get('reason','fallback')}")


def scenario_duplicate() -> tuple[str, str]:
    notifier, audit = _fresh()
    _escalate(notifier, ANALYST_A, idem="dup-1")
    r2 = _escalate(notifier, ANALYST_A, idem="dup-1")   # same idempotency key
    return ("duplicate (same idempotency key twice)",
            f"second send deduped={r2.get('deduped')} — on-call paged once, not twice")


def scenario_schema_drift() -> tuple[str, str]:
    notifier, audit = _fresh(fault="schema_drift")
    r = _escalate(notifier, ANALYST_A)
    return ("schema_drift (200 but response shape changed)",
            f"defensive parse → still delivered_via={r['delivered_via']}, flagged not crashed")


SCENARIOS = {
    "happy": scenario_happy, "rbac": scenario_rbac, "rate_limit": scenario_rate_limit,
    "expired_webhook": scenario_expired, "timeout": scenario_timeout,
    "duplicate": scenario_duplicate, "schema_drift": scenario_schema_drift,
}


def run_all(webhook=None) -> bool:
    print("\n" + "=" * 74)
    print("  W16 — Slack integration through the governed MCP door")
    print("  the agent pings on-call when it escalates — scoped, audited, and")
    print("  hardened against the failures real integrations actually hit")
    print("=" * 74)

    order = ["happy", "rbac", "rate_limit", "expired_webhook", "timeout",
             "duplicate", "schema_drift"]
    results = []
    for key in order:
        print(f"\n▶ {key}")
        name, outcome = SCENARIOS[key]() if key != "happy" else scenario_happy(webhook)
        tag = "SETUP" if key == "happy" else ("RBAC" if key == "rbac" else "PROBLEM")
        if key in ("happy", "rbac"):
            print(f"  {tag}: {name}  →  HANDLED: {outcome}")
        else:
            print(f"  PROBLEM: {name}  →  HANDLED: {outcome}")
        results.append((key, name, outcome))

    print("\n" + "-" * 74)
    print("  SUMMARY — every failure mode handled deterministically, in the door")
    print("-" * 74)
    print(f"  {'scenario':<18}{'what happened':<56}")
    for key, name, outcome in results:
        print(f"  {key:<18}{outcome[:54]:<56}")
    print("\n  Takeaway: the model never sees a raw 500, a lost page, or a double-page.")
    print("  Backoff, fallback, circuit breaker, idempotency, defensive parse — all")
    print("  live in the deterministic integration layer. That is the FDE's job.\n")
    return True


if __name__ == "__main__":
    args = sys.argv[1:]
    real = "--real" in args
    webhook = os.environ.get("SLACK_WEBHOOK_URL") if real else None
    token = os.environ.get("SLACK_BOT_TOKEN") if real else None
    channel_id = os.environ.get("SLACK_CHANNEL_ID") if real else None
    bot = (token, channel_id) if (token and channel_id) else None
    if real and not (bot or webhook):
        print("--real needs credentials — either rung works (setup: SLACK_LIVE_SETUP.md):\n"
              "  rung 2 (bot, posts as the app):  export SLACK_BOT_TOKEN=xoxb-… "
              "SLACK_CHANNEL_ID=C…\n"
              "  rung 1 (webhook):                       export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/…")
        raise SystemExit(2)

    if "--simulate" in args:
        which = args[args.index("--simulate") + 1]
        if which not in SCENARIOS:
            print(f"unknown scenario '{which}'. choices: {', '.join(SCENARIOS)}")
            raise SystemExit(2)
        print(f"\n▶ {which}")
        name, outcome = (scenario_happy(webhook) if which == "happy" else SCENARIOS[which]())
        print(f"  → {name}: {outcome}\n")
        raise SystemExit(0)

    if real:
        via = "bot token (chat.postMessage)" if bot else "incoming webhook"
        print(f"Posting a REAL escalation via {via}…")
        notifier, audit = _fresh(webhook=webhook, bot=bot)
        res = _escalate(notifier, ANALYST_A)
        print(f"  result: {json.dumps(res)}")
        print(f"  audit:  {json.dumps(audit.rows[-1])}")
        print("  → check the channel: the escalation card (with its Ops Console button) should be there.\n")
        raise SystemExit(0)

    ok = run_all(webhook)
    raise SystemExit(0 if ok else 1)
