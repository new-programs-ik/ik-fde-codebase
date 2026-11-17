#!/usr/bin/env python3
"""The Meridian Slack MCP server — Slack behind OUR governed door, as real MCP.

This is a spec-compliant MCP server over stdio (JSON-RPC 2.0, one message per
line) written in ~150 lines of stdlib — the W16 move, applied to the protocol
itself: no framework, every part visible. Any MCP client can connect:

    # Claude Code
    claude mcp add meridian-slack -- python3 course/week-16/lab/meridian_slack_mcp.py

    # Claude Desktop (claude_desktop_config.json)
    {"mcpServers": {"meridian-slack": {"command": "python3",
        "args": ["<abs path>/course/week-16/lab/meridian_slack_mcp.py"]}}}

It exposes ONE typed tool, `notify_slack`, and the call rides the exact same
governed logic as `slack_integration_demo.py`: deterministic scope check →
idempotency → retry/backoff + circuit breaker → append-only audit row → the
Block Kit card whose centerpiece is the button back into the Meridian Bank
portal. Slack is "completely accessible through MCP" — with governance intact.

Contrast rung (taught, not recommended for the engagement): the off-the-shelf
Slack MCP server (`npx @modelcontextprotocol/server-slack`) wires Slack to any
agent in five minutes — but it hangs the raw bot token on the tool with NO
Meridian scope check, NO audit row, NO idempotency. Fine on your laptop;
indefensible in the bank's security review. That contrast IS the W16 lesson.

Identity is SERVER-side configuration, not a tool argument — the model cannot
pass someone else's badge (that's the OBO discipline):

    MERIDIAN_ACTING_EMAIL  (default oncall_a@meridian.test)
    MERIDIAN_ACTING_SCOPE  (default type_a)

Transport resolution, in order:
    SLACK_BOT_TOKEN + SLACK_CHANNEL_ID  → chat.postMessage as the app (live)
    SLACK_WEBHOOK_URL                   → incoming webhook (live)
    neither                             → offline mock (deterministic success)

Smoke-test without any MCP client (drives the full protocol over a pipe):

    python3 meridian_slack_mcp.py --selftest
"""
from __future__ import annotations

import json
import os
import sys

from slack_integration_demo import (
    AccessDenied, AuditLog, Identity, SlackBotTransport, SlackNotifier,
    SlackTransport,
)

PROTOCOL_FALLBACK = "2025-06-18"

TOOL = {
    "name": "notify_slack",
    "description": (
        "Escalate a Meridian dispute to the on-call analyst in Slack, through "
        "the governed door: scope-checked against the acting analyst's badge, "
        "idempotent, retried with backoff, audited. The card links back to the "
        "dispute in the Meridian Bank portal. Returns the delivery result."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "dispute_id": {"type": "string", "description": "Meridian dispute id, e.g. '29'"},
            "dispute_type": {"type": "string", "enum": ["A", "B"],
                             "description": "Dispute type — checked against the acting analyst's scope"},
            "reg_e_status": {"type": "string", "description": "Reg E clock status, e.g. 'RED — 2 days to deadline'"},
            "recommendation": {"type": "string", "description": "The agent's recommendation text (with citations)"},
            "channel": {"type": "string", "description": "Slack channel (display only for webhook/mock)",
                        "default": "#meridian-oncall"},
            "idem_key": {"type": "string", "description": "Idempotency key; same key never double-pages"},
        },
        "required": ["dispute_id", "dispute_type", "reg_e_status", "recommendation"],
    },
}


def _build_notifier() -> tuple[SlackNotifier, AuditLog, str]:
    audit = AuditLog()
    token, channel = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_CHANNEL_ID")
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if token and channel:
        transport, mode = SlackBotTransport(token, channel), "bot"
    elif webhook:
        transport, mode = SlackTransport(webhook), "webhook"
    else:
        transport, mode = SlackTransport(None), "mock"
    return SlackNotifier(transport, audit), audit, mode


NOTIFIER, AUDIT, MODE = _build_notifier()
IDENTITY = Identity(os.environ.get("MERIDIAN_ACTING_EMAIL", "oncall_a@meridian.test"),
                    os.environ.get("MERIDIAN_ACTING_SCOPE", "type_a"))


def call_notify_slack(args: dict) -> tuple[str, bool]:
    """Returns (json_text, is_error). Denials are tool errors WITH the audit row."""
    quiet = lambda *_a, **_k: None  # tool logs stay out of the stdio channel
    try:
        result = NOTIFIER.notify(
            IDENTITY,
            channel=args.get("channel", "#meridian-oncall"),
            dispute_id=str(args["dispute_id"]),
            dispute_type=args["dispute_type"],
            reg_e_status=args["reg_e_status"],
            recommendation=args["recommendation"],
            idem_key=args.get("idem_key", f"esc-{args['dispute_id']}"),
            log=quiet)
        return json.dumps({"mode": MODE, "acting_identity": IDENTITY.email,
                           "result": result, "audit_row": AUDIT.rows[-1]}), False
    except AccessDenied as e:
        return json.dumps({"mode": MODE, "error": "access_denied", "detail": str(e),
                           "audit_row": AUDIT.rows[-1]}), True


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                       # notification (e.g. notifications/initialized)
        return None
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion", PROTOCOL_FALLBACK)
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": proto, "capabilities": {"tools": {}},
            "serverInfo": {"name": "meridian-slack", "version": "1.0.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "notify_slack":
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')!r}"}}
        text, is_err = call_notify_slack(params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_err}}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method {method!r} not found"}}


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


def selftest() -> None:
    """Drive the full protocol in-process — initialize → list → call → dedupe → deny."""
    import copy
    seq = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_FALLBACK, "capabilities": {},
                    "clientInfo": {"name": "selftest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "notify_slack", "arguments": {
                "dispute_id": "29", "dispute_type": "A",
                "reg_e_status": "RED — 2 days to deadline",
                "recommendation": "Reg E provisional credit [chunk 24]; human confirm."}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {  # same idem key → dedupe
            "name": "notify_slack", "arguments": {
                "dispute_id": "29", "dispute_type": "A",
                "reg_e_status": "RED — 2 days to deadline",
                "recommendation": "Reg E provisional credit [chunk 24]; human confirm."}}},
    ]
    ok = True
    for msg in seq:
        resp = handle(copy.deepcopy(msg))
        if msg.get("id") is None:
            continue
        assert resp and ("result" in resp or "error" in resp), resp
        tag = msg["method"]
        if tag == "tools/call":
            payload = json.loads(resp["result"]["content"][0]["text"])
            detail = payload.get("result", payload)
            print(f"  {tag} #{msg['id']}: isError={resp['result']['isError']} "
                  f"delivered_via={detail.get('delivered_via')} deduped={detail.get('deduped', False)}")
            ok &= not resp["result"]["isError"]
        else:
            print(f"  {tag}: ok")
    # denial path: a Type-B badge may not escalate Type-A
    global IDENTITY
    IDENTITY = Identity("analyst_b@meridian.test", "type_b")
    resp = handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "notify_slack", "arguments": {
            "dispute_id": "4417", "dispute_type": "A", "reg_e_status": "RED",
            "recommendation": "…", "idem_key": "deny-1"}}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    print(f"  tools/call #5 (Type-B → Type-A): isError={resp['result']['isError']} "
          f"error={payload.get('error')} audit_decision={payload['audit_row']['decision']}")
    ok &= resp["result"]["isError"] and payload["audit_row"]["decision"] == "deny"
    print("SELFTEST", "PASS" if ok else "FAIL", f"(transport mode: {MODE})")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        selftest()
    serve()
