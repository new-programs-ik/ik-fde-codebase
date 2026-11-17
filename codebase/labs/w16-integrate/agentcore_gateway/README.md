# AgentCore Gateway lab — the managed version of the door you built by hand

In `../mcp_demo.py` you built the W16 lesson yourself: one governed door
(typed tools, a deterministic scope check the LLM cannot talk past, an audit
row per access) with a scoped `mrdn_` key per analyst. This lab stands up the
**same door as managed AWS resources**, so you can say on an engagement what
the managed service is charging for — and what it is *not* doing for you.

| Hand-rolled (mcp_demo.py) | Managed (this lab) |
|---|---|
| scoped `mrdn_` key in an env var | **AgentCore Identity** — API-key credential provider: the key lives in a vault and is injected per call; agent code never sees it |
| `MeridianMCPServer` class | **Gateway** — an MCP endpoint AWS hosts |
| each `@tool` wrapper you typed | **Gateway target** — `openapi.json`; every `operationId` becomes an MCP tool (`get_dispute`, `get_dispute_evidence`, `search_policies`, `create_dispute_ticket`) |
| "who may call the door?" | **Gateway authorizer** — `AWS_IAM`: callers sign with SigV4 |

What does **not** move: RBAC scope and the audit trail are still enforced
inside `/api/v1` on every request — deterministic layer, never the LLM. The
gateway replaces tool-wiring and key-handling, not enforcement. (That spec is
a 4-route subset of the platform's own `/api/v1/openapi.json`; Redoc at
`/api/docs`.)

Because the door speaks MCP, the *same* endpoint serves your agent, Claude
Code, Cursor, or any other MCP client — that is the point of Gateway: wire
the API once, every agent surface gets the tools.

## The ladder

```bash
# Rung 0 — $0 floor, runs anywhere, no credentials:
python3 gateway_setup.py plan          # prints every AWS call apply would make

# Rungs 1–3 — your own AWS account (bedrock-agentcore + iam perms) +
# your scoped analyst key:
export MERIDIAN_API_KEY="mrdn_..."
python3 gateway_setup.py apply         # vault → role → gateway → target
python3 gateway_setup.py invoke        # SigV4-signed MCP tools/list + get_dispute 29
python3 gateway_setup.py destroy       # leave nothing running you don't own
```

`plan` output captured: `logs/gateway_plan.txt`. `apply` writes
`gateway_state.json` (the created ids/ARNs) so `destroy` can clean up even
after a partial run.

## IK shared account status

The shared `intksrtuser09` account is governed by the lab provider's **Lab
Control Panel**: while the AWS Lab is **stopped**, the key carries an explicit
IAM deny on `bedrock-agentcore:*` and `apply` fails closed at step 1 —
captured verbatim in `logs/gateway_denied_2026-07-22.txt`. Pressing **Start**
lifts the deny: the full ladder (`apply` → `invoke` → `destroy`) was verified
live on this account on 2026-07-22 (`logs/gateway_apply_live_2026-07-22.txt`).
On your own account you need `BedrockAgentCoreFullAccess` plus `iam:PassRole`
on the gateway role. An FDE reads a denial like that log and names the exact
missing action — that is itself the W16 skill; keep both logs side by side.
