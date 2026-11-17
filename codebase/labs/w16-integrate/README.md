# Week 16 Lab — The Meridian MCP Server + RBAC + the Two-Analyst Test

**Plain-words key (terms used throughout this folder):** *MCP* = a standard plug shape —
build the tool once, any agent can use it. *RBAC* = access rules by role: who may see
what. *Idempotent / idempotency key* = safe to retry — the same request twice creates
ONE ticket, not two. *OBO (on-behalf-of)* = the agent borrows the signed-in analyst's
permissions, acting on their behalf. *IdP* = the company's login system (Okta, Keycloak,
AWS's IAM Identity Center). *Webhook* = the other system calls YOU when something
happens. *Circuit breaker* = stop calling a failing system for a while instead of
hammering it. *Enumeration* = probing ids one by one to learn which exist.

**Milestone produced:** **M3 — Integrated & access-controlled.**
**Marquee demo:** `mcp_demo.py` (in this folder) — a self-contained, runnable MCP server with
per-user scope enforcement and an append-only audit log, proven by the two-analyst test.

> Story: last week's pilot user said *"articulate and useless — it can't see our tickets, it
> can't see the core system, and it has no idea who's asking."* Today you give the agent
> senses (the MCP door) and a badge (identity + RBAC).

**How this lab works:** in class you *watch* the instructor build the door up on screen and
answer predict-then-verify drills — there is **no code-along**. You build it yourself at
home, in this repo: https://github.com/aeropj/ik-fde
— fork + clone → `make verify` → run `codebase/labs/w16-integrate/mcp_demo.py` → extend it into your M3 there.

**In-class activities (instructor-run, all-cohort, no code):**
- **Design the scope matrix** (~8 min, Block 3): an
  insurance client, 5 roles × 6 resources; learners fill who-may-see-what in chat;
  model answer + the two traps.
- **Break the door** (~8 min, Block 3): six attack
  attempts; learners predict stopped-or-not and which layer; the demos in this folder
  verify every prediction on screen (answer key quotes the exact output lines).

---

## 0. See the demo first (2 min)

```bash
cd "$(git rev-parse --show-toplevel)"/codebase/labs/w16-integrate
python3 mcp_demo.py            # four proofs, end to end
echo "exit code: $?"           # 0 = everything holds
```
Four proofs in one run: the **two-analyst test** (same question, two identities →
Analyst A (type_a) allowed, Analyst B (type_b) denied, both audited), the **idempotent
write** (`create_ticket` twice with one key → the original comes back, marked
`idempotent_replay`), **retry/backoff** against the flaky legacy core (503 on the first
call), and the **enumeration check** (a nonexistent id denies exactly like an
out-of-scope id). That is the M3 acceptance criterion in miniature. Read `mcp_demo.py` —
it is the spec you will re-implement against the live platform.

**LIVE mode (what the instructor runs at ~2:55):** the *same* governed door against the
deployed platform, with two real scoped analyst keys:

```bash
export MERIDIAN_KEY_A="mrdn_…"   # a type_a-scoped analyst key
export MERIDIAN_KEY_B="mrdn_…"   # a type_b-scoped analyst key
python3 mcp_demo.py --live       # defaults to dispute 29
```
200 vs 403 from the real `/api/v1`, a **real** ticket written on dispute 29 (subject
prefix "Governed-door recommendation"), the `Idempotency-Key` replay proven end to end,
and the platform's own audit rows via `GET /audit`. A dated verbatim capture of a real
`--live` run (ticket 9000000019) is in `logs/governed_door_live.txt` —
that is the output shape you should see. Note: the live seed's ids differ from
the lab narrative — 29 is the seeded Type-A dispute you saw in W15; 4417 lives in the
offline mock.

**$0 path:** the whole week runs locally — the MCP server + mock backends need no cloud.
Real-IdP OBO wiring (the borrow against your own real login system / OIDC provider) is
stretch, not floor; the Meridian platform's scoped per-analyst API keys simulate the
borrow, and the demo above proves the *logic* with zero setup. The managed path
(AgentCore Gateway/Identity) is in `agentcore_gateway/` and `../../docs/agentcore-deep-dive.md`.

---

## Lab I — the MCP server (in class 0:45–1:55, watched; built by you at home)

Build **one** MCP server exposing four typed tools over three backends
(core-banking REST, ServiceNow-style tickets, policy doc store):

| Tool | Notes |
|---|---|
| `lookup_dispute(id)` | scope-checked; a nonexistent id denies **exactly like** an out-of-scope id (anti-enumeration), and the denial is audited |
| `fetch_transactions(id)` | scope-checked; rides through the provided `FlakyLegacyCore` (503 first call) with retry/backoff |
| `create_ticket(...)` | **idempotency key** — the replay returns the *original* ticket, marked `idempotent_replay` |
| `search_policy(q)` | read-only, unscoped, still audited |

Requirements: typed args, **retry/backoff** on the flaky legacy backend, **structured
errors the agent can reason about** (`{"error":"rate_limited","retry_after":2}`, never a
raw 500), and **an audit row on every access — allowed, denied, or not-found**.
All four tools are already implemented in `mcp_demo.py` — in class you predict each
output before the instructor runs it; at home you re-implement them in your fork.

Wire the real backends by swapping the in-memory dicts in `mcp_demo.py` for calls to the live
platform (`codebase/agent/src/meridian_agent/db.py` / the `/api/v1` REST API) — the file's
`--live` mode is the worked reference for exactly that swap.

**Principle (W14 made real):** the agent calls only this one governed door. RBAC is enforced
in the **deterministic** MCP layer (and DB row-level security) — *never* in the LLM/prompt.

---

## Lab II — identity & RBAC (in class 2:10–3:05, watched)

1. **The borrow — OAuth2 on-behalf-of (OBO):** the analyst signs in at the company's
   login system (any OIDC IdP — IAM Identity Center, Okta, Keycloak); the agent swaps
   that login token for one scoped to *that* analyst — acting on their behalf, no
   standing admin access (the token-exchange standard, RFC 8693).
2. **Agent identity:** "who is the agent acting as?" — managed form: AgentCore Identity
   (OAuth token vault); Meridian simulates it with scoped per-analyst API keys.
3. **Scope enforcement in the MCP layer:** `type_a` → only Type-A records (see `_authorize`).
4. **Audit:** every call writes `{acting_identity, scope, action, resource, decision}`.

### The two-analyst test (the centerpiece — instructor-run, you predict)
The identical question under two differently-scoped accounts → different,
correctly-filtered answers **and** matching audit entries:
```bash
python3 mcp_demo.py 4417       # mock: Type-A dispute — A allowed, B denied
python3 mcp_demo.py --live     # the same door against production (dispute 29)
```
- ✅ **Drill 5 (predict → verify):** call analyst B's answer in chat before the run; the
  `RESULT: PASS ✓ scope holds — different answers + matching audit` line settles it. At
  home: your M3 must produce this line from your own build.
- **Poll:** *Where should the filter live — agent, MCP, or system of record?* (Answer: all
  three; defense in depth. The prompt is advisory, the MCP scope check and DB RLS are enforced.)

---

## Lab III — a real integration through the door: Slack (in class 3:05–3:12, watched)

The pilot user wanted the agent to *do* something when it escalates, not just
write a row. So wire ONE high-impact, free integration: when the agent hands a
hard dispute to a human, it **pings the on-call analyst in Slack** — through the
same governed MCP door, scope-checked and audited like every other tool.

```bash
python3 slack_integration_demo.py            # mock: RBAC + all 5 failure simulations
python3 slack_integration_demo.py --simulate rate_limit
```

The whole point is the second lesson: **real integrations fail, and you design
for that.** The demo injects the failures you actually hit in production and
shows the handling pattern for each:

| Problem (injected) | What you'd see in prod | How the door handles it |
|---|---|---|
| `rate_limit` | Slack returns HTTP 429 | exponential **backoff + retry** |
| `expired_webhook` | 404 invalid_token (rotated cred) | **graceful fallback** → Ops Console ticket (escalation never lost) |
| `timeout` | the request hangs | per-call timeout → **circuit breaker** opens → short-circuit to fallback |
| `duplicate` | the agent retries the whole step | **idempotency key** → on-call paged once, not twice |
| `schema_drift` | Slack changes a response field | **defensive parse** → flag, don't crash |

Plus RBAC: a `type_b` analyst trying to escalate a Type-A dispute is **denied at
the door** with an audit row — Slack is never even called. All of this hardening
lives in the **deterministic integration layer**, never in the model.

### The same tool over real MCP — `meridian_slack_mcp.py`

Slack isn't only reachable from our script: `notify_slack` is exposed by a
**spec-compliant MCP server** (JSON-RPC over stdio, ~150 lines of stdlib), so any
MCP client — Claude Code, Claude Desktop, an IDE — can escalate through the
governed door. Identity is *server-side* config, so the model cannot pass someone
else's badge.

```bash
python3 meridian_slack_mcp.py --selftest     # drives initialize → tools/list → tools/call → dedupe → deny
claude mcp add meridian-slack -- python3 $PWD/meridian_slack_mcp.py
```

Contrast beat for class: the off-the-shelf `@modelcontextprotocol/server-slack`
wires Slack to an agent in five minutes with **no** Meridian scope check, audit
row, or idempotency — fine on a laptop, indefensible in the bank's review.

**Run it for real (~5 min):** full owner setup, the app manifest, the three
integration rungs, and the five dated demo screenshots are in
**[`SLACK_LIVE_SETUP.md`](SLACK_LIVE_SETUP.md)**.

```bash
set -a; source .env; set +a                  # SLACK_BOT_TOKEN + SLACK_CHANNEL_ID (git-ignored)
python3 slack_integration_demo.py --real     # a real escalation card lands in #meridian-oncall
python3 meridian_slack_mcp.py --selftest     # same, through the MCP protocol
```

**Verified live 2026-07-30** (workspace `ik-test`, channel `#meridian-oncall`,
bot `meridian-escalations`): card delivered, replay deduped, Type-B→Type-A
escalation denied with an audit row, and the card's button walked
Slack → portal login → dispute 29 → **Linked tickets (23)**. Verbatim log:
`logs/slack_live_2026-07-30.txt`; screenshots: `course/assets/slack/`.

- ✅ **At-home stretch evidence:** the `PROBLEM: … → HANDLED: …` summary from your own
  run, or a screenshot of the escalation card in your own free Slack workspace.

---

## Definition of done (M3 floor)
- [ ] Agent answers dispute queries **through the MCP server** bridging ≥2 systems.
- [ ] Per-user RBAC enforced + audit logging.
- [ ] **The two-analyst test passes in your own deployment** (different answers + matching audit).

**Stretch:** a third backend; webhook-driven ticket updates; deny-by-default with explicit grants.
Build it in your fork of this repo: https://github.com/aeropj/ik-fde.

> Cliffhanger → W17: "During user-acceptance testing on staging, a crafted document planted
> in the policy store injected instructions into the agent. Two failures, one evening:
> (1) the agent pulled another customer's data into a draft customer email — the output
> tripwire caught the draft before send; the retrieval layer never flagged it; (2) the agent
> asserted a confidently wrong Reg E deadline — 45 business days instead of 10 — and no gate
> flagged it at all. Compliance's memo, forwarded by the CISO: 'Prove this can't happen in
> production, or we don't go live.'"
