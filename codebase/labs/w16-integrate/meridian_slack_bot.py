#!/usr/bin/env python3
"""Two-way Slack: the on-call analyst ASKS, the agent ANSWERS — through the door.

The escalation path (`slack_integration_demo.py`) is one-way: the agent pages a
human. That is half a conversation. This is the other half — a Socket Mode bot
that listens in `#meridian-oncall`, answers questions about disputes and policy,
and does it through **the same governed door**: every answer is scope-checked
against the asking analyst's mapped seat and written to the audit trail.

Why Socket Mode: it opens an outbound WebSocket, so it runs from a laptop behind
NAT with **no public URL, no ngrok, no inbound firewall rule** — the only sane
choice for a live class.

    export SLACK_BOT_TOKEN="xoxb-…"     # posts replies
    export SLACK_APP_TOKEN="xapp-…"     # opens the Socket Mode connection
    python3 meridian_slack_bot.py                 # run the bot (Ctrl-C to stop)
    python3 meridian_slack_bot.py --selftest      # route + governance tests, no Slack needed

Ask it, in the channel (mention it, or DM it):

    @meridian-escalations status
    @meridian-escalations dispute 29
    @meridian-escalations policy provisional credit
    @meridian-escalations tickets 29
    @meridian-escalations help

Governance, unchanged from the escalation path:
  * The Slack user is mapped to an analyst SEAT (`SLACK_USER_MAP`, or the default
    seat). Identity is server-side — nobody can widen their scope by typing.
  * Reads go through the platform's `/api/v1` with that seat's scoped key, so a
    Type-B seat asking about a Type-A dispute gets the same 403 the UI gives.
  * Every question writes an audit row: who asked, what they asked, allow/deny.
  * The bot answers with FACTS and links back to the portal. It never decides
    anything that moves money — that stays a human click in the Ops Console.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

BASE_URL = os.environ.get(
    "MERIDIAN_BASE_URL", "https://ik-fde-casestudy-bank.vercel.app/api/v1").rstrip("/")
CONSOLE = os.environ.get(
    "MERIDIAN_CONSOLE_URL", "https://ik-fde-casestudy-bank.vercel.app").rstrip("/")

# Which analyst seat a Slack user speaks as. In production this is the OAuth
# on-behalf-of exchange (W16); here it is an explicit, auditable mapping.
#   SLACK_USER_MAP='{"U123":"type_a","U456":"type_b"}'
SLACK_USER_MAP: dict[str, str] = json.loads(os.environ.get("SLACK_USER_MAP", "{}"))
DEFAULT_SCOPE = os.environ.get("MERIDIAN_ACTING_SCOPE", "type_a")
KEY_FOR_SCOPE = {
    "type_a": os.environ.get("MERIDIAN_KEY_A") or os.environ.get("MERIDIAN_API_KEY", ""),
    "type_b": os.environ.get("MERIDIAN_KEY_B", ""),
}


# ───────────────────────────── audit (same shape as the door) ─────────────────
@dataclass
class AuditLog:
    rows: list[dict] = field(default_factory=list)

    def write(self, who: str, scope: str, action: str, resource: str, decision: str, **extra):
        row = {"acting_identity": who, "scope": scope, "action": action,
               "resource": resource, "decision": decision, **extra}
        self.rows.append(row)
        print(f"    audit → {json.dumps(row)}", flush=True)
        return row


AUDIT = AuditLog()


# ───────────────────────────── the portal, read-only ─────────────────────────
class PortalError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"portal {code}: {detail}")
        self.code, self.detail = code, detail


def portal_get(path: str, key: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(BASE_URL + path,
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise PortalError(e.code, e.read().decode()[:200])
    except Exception as e:                                    # network, timeout…
        raise PortalError("unreachable", str(e)[:200])


# ───────────────────────────── the answers ───────────────────────────────────
HELP = (
    "*What I can answer* (I read the bank through the governed door — scoped and audited):\n"
    "• `dispute <id>` — type, status, amount, Reg E clock, provisional credit\n"
    "• `tickets <id>` — the agent recommendations already on that dispute\n"
    "• `policy <words>` — the policy chunks the agent would cite\n"
    "• `status` — my connection, the portal I'm pointed at, and the seat I act as\n"
    "• `help` — this message\n"
    "_I answer with facts and links. Anything that moves money stays a human click in the Ops Console._"
)


def fmt_dispute(d: dict) -> str:
    clock = "⚠️ BREACHED" if d.get("sla_breached") else "on time"
    pc = "issued" if d.get("provisional_credit_issued") else "not issued"
    return (f"*Dispute {d['dispute_id']}* — Type-{d.get('dispute_type')} · reason {d.get('reason_code')}\n"
            f"• amount: ${int(d.get('amount_cents') or 0)/100:,.2f}"
            f"  · merchant: {d.get('merchant_name', '—')}\n"
            f"• status: `{d.get('status')}` · assigned: {d.get('assigned_analyst', '—')}\n"
            f"• Reg E due: {str(d.get('reg_e_due_at'))[:10]} ({clock})"
            f"  · provisional credit: {pc}\n"
            f"<{CONSOLE}/disputes/{d['dispute_id']}|Open in Ops Console>")


def answer(text: str, who: str, scope: str) -> str:
    """Route a question. Returns Slack-formatted text. Never raises."""
    q = re.sub(r"<@[^>]+>", "", text or "")          # real Slack mentions
    q = re.sub(r"^\s*@\S+\s*", "", q).strip()        # a typed "@name " prefix
    low = q.lower()
    key = KEY_FOR_SCOPE.get(scope, "")

    if not low or low in ("help", "hi", "hello", "?"):
        AUDIT.write(who, scope, "help", "-", "allow")
        return HELP

    if low.startswith("status"):
        AUDIT.write(who, scope, "status", "-", "allow")
        reach = "unknown"
        try:
            portal_get("/disputes/29", key)
            reach = "reachable ✅"
        except PortalError as e:
            reach = f"{e.code}"
        return (f"*Meridian bot — status*\n"
                f"• portal: `{BASE_URL}` — {reach}\n"
                f"• console: {CONSOLE}\n"
                f"• I am acting as seat scope `{scope}` (server-side; you cannot change it by asking)\n"
                f"• audit rows this session: {len(AUDIT.rows)}")

    m = re.match(r"(?:dispute|d)\s*#?(\d+)", low)
    if m:
        did = m.group(1)
        if not key:
            AUDIT.write(who, scope, "lookup_dispute", f"dispute/{did}", "deny",
                        reason="no key configured for this seat")
            return f"I have no API key configured for seat scope `{scope}` — ask your instructor to set it."
        try:
            d = portal_get(f"/disputes/{did}", key)["dispute"]
            AUDIT.write(who, scope, "lookup_dispute", f"dispute/{did}", "allow")
            return fmt_dispute(d)
        except PortalError as e:
            AUDIT.write(who, scope, "lookup_dispute", f"dispute/{did}", "deny", http=str(e.code))
            if str(e.code) == "403":
                return (f"🚫 *403 — out of scope.* Seat `{scope}` may not read dispute {did}.\n"
                        f"_That is the governed door working: the deny is audited, and I cannot talk past it._")
            if str(e.code) == "404":
                return f"No dispute {did} on this portal. (Denials and not-founds look alike on purpose.)"
            return f"Portal said `{e.code}`. I did not get an answer — the audit row records the attempt."

    m = re.match(r"tickets?\s*#?(\d+)", low)
    if m:
        did = m.group(1)
        try:
            data = portal_get(f"/disputes/{did}/tickets", key)
            ts = data.get("tickets", [])
            AUDIT.write(who, scope, "list_tickets", f"dispute/{did}", "allow", count=len(ts))
            if not ts:
                return f"No tickets on dispute {did} yet."
            head = f"*{len(ts)} ticket(s) on dispute {did}* — newest first:\n"
            body = "\n".join(
                f"• `#{t['ticket_id']}` {t.get('subject','')} — {str(t.get('created_at'))[:10]}"
                for t in ts[:5])
            return head + body + f"\n<{CONSOLE}/disputes/{did}|Open in Ops Console>"
        except PortalError as e:
            AUDIT.write(who, scope, "list_tickets", f"dispute/{did}", "deny", http=str(e.code))
            return f"🚫 Could not read tickets for {did} (`{e.code}`). The attempt is audited."

    m = re.match(r"(?:policy|policies)\s+(.*)", low)
    if m and m.group(1).strip():
        query = m.group(1).strip()
        try:
            chunks = portal_get(
                "/policies?q=" + urllib.parse.quote_plus(query), key).get("chunks", [])
            AUDIT.write(who, scope, "search_policy", f"query={query}", "allow", hits=len(chunks))
            if not chunks:
                return f"No policy chunks matched “{query}”."
            lines = [f"*Policy hits for “{query}”* (these are the `[chunk NN]` ids the agent cites):"]
            for c in chunks[:4]:
                sec = c.get("section") or c.get("doc_title") or "section"
                txt = (c.get("content") or "")[:160].replace("\n", " ")
                lines.append(f"• `[chunk {c.get('chunk_id', '?')}]` *{sec}* — {txt}…")
            return "\n".join(lines)
        except PortalError as e:
            AUDIT.write(who, scope, "search_policy", f"query={query}", "deny", http=str(e.code))
            return f"Policy search failed (`{e.code}`). The attempt is audited."

    AUDIT.write(who, scope, "unrecognized", q[:60], "allow")
    return ("I didn't recognise that. I answer `dispute <id>`, `tickets <id>`, "
            "`policy <words>`, `status`, `help`.")


def scope_for(slack_user: str) -> str:
    return SLACK_USER_MAP.get(slack_user, DEFAULT_SCOPE)


# ───────────────────────────── Socket Mode wiring ────────────────────────────
def serve() -> int:
    bot, app = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_APP_TOKEN")
    if not (bot and app):
        print("need SLACK_BOT_TOKEN (xoxb-…) and SLACK_APP_TOKEN (xapp-…).\n"
              "Enable Socket Mode + event subscriptions first — see SLACK_LIVE_SETUP.md §D.")
        return 2
    try:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ImportError:
        print("pip install slack_sdk   (Socket Mode needs it)")
        return 2

    web = WebClient(token=bot)
    me = web.auth_test()
    bot_user_id = me["user_id"]
    print(f"connected as {me['user']} ({bot_user_id}) in {me['team']}")
    print(f"portal: {BASE_URL}\nlistening… (Ctrl-C to stop)\n", flush=True)

    sm = SocketModeClient(app_token=app, web_client=web)

    def handle(client: SocketModeClient, req: SocketModeRequest):
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        ev = req.payload.get("event", {})
        if ev.get("bot_id") or ev.get("user") == bot_user_id:
            return                                    # never answer ourselves
        if ev.get("type") not in ("app_mention", "message"):
            return
        if ev.get("type") == "message" and ev.get("channel_type") not in ("im", "channel", "group"):
            return
        text, user, channel = ev.get("text", ""), ev.get("user", "?"), ev.get("channel")
        # in a channel, only respond when mentioned; in a DM, always
        if ev.get("type") == "message" and ev.get("channel_type") != "im" \
           and f"<@{bot_user_id}>" not in text:
            return
        scope = scope_for(user)
        print(f"  ← <@{user}> (seat {scope}): {text!r}", flush=True)
        reply = answer(text, who=f"slack:{user}", scope=scope)
        web.chat_postMessage(channel=channel, text=reply,
                             thread_ts=ev.get("thread_ts") or ev.get("ts"))
        print(f"  → replied in {channel}\n", flush=True)

    sm.socket_mode_request_listeners.append(handle)
    sm.connect()
    try:
        import threading
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping.")
    return 0


# ───────────────────────────── selftest (no Slack needed) ────────────────────
def selftest() -> int:
    print("routing + governance selftest (no Slack connection needed)\n")
    cases = [
        ("help", "type_a", "What I can answer"),
        ("", "type_a", "What I can answer"),
        ("status", "type_a", "Meridian bot — status"),
        ("dispute 29", "type_a", "Dispute 29"),
        ("@bot dispute 29", "type_b", "403"),
        ("tickets 29", "type_a", "ticket(s) on dispute 29"),
        ("policy provisional credit", "type_a", "Policy hits"),
        ("what is the weather", "type_a", "didn't recognise"),
    ]
    ok = True
    for text, scope, want in cases:
        got = answer(text, who="selftest", scope=scope)
        good = want in got
        ok &= good
        print(f"  [{'PASS' if good else 'FAIL'}] scope={scope:7s} {text!r:34s} → expected {want!r}")
        if not good:
            print(f"          got: {got[:200]!r}")
    denies = [r for r in AUDIT.rows if r["decision"] == "deny"]
    print(f"\n  audit rows: {len(AUDIT.rows)} (incl. {len(denies)} deny) — "
          f"every question logged, allow AND deny")
    ok &= len(denies) >= 1
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selftest() if "--selftest" in sys.argv[1:] else serve())
