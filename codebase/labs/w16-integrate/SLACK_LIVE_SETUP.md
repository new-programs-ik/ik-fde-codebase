# Slack, live and through MCP — setup + integration map

**Goal.** Slack is not a side demo: the escalation leg completes the circle
(agent → guardrail trips → on-call human paged in Slack → one click back into
the **Meridian Bank portal** where the work lives). This page is (a) the exact
5-minute setup for the workspace owner, and (b) the three integration rungs,
of which the governed one is the course's answer.

---

## A. One-time setup (workspace owner, ~5 minutes)

1. **Create the workspace** — slack.com → *Create a new workspace* (free plan
   is fine; a throwaway account works).
2. **Create the app from the manifest** — https://api.slack.com/apps →
   *Create New App* → *From a manifest* → pick the workspace → paste
   `slack_app_manifest.yaml` (this folder) → *Create*. The app is
   **Meridian Escalations** with exactly two scopes: `chat:write`,
   `chat:write.public`.
3. **Install it** — app page → *Install App* → *Allow*.
4. **Collect two values:**
   - **Bot token** — *OAuth & Permissions* → *Bot User OAuth Token* (`xoxb-…`)
   - **Channel ID** — create `#meridian-oncall` in Slack, open it → click the
     channel name → *About* → *Channel ID* (`C…`)
   - *(optional, rung 1)* *Incoming Webhooks* → Activate → *Add New Webhook* →
     `#meridian-oncall` → copy the `https://hooks.slack.com/services/…` URL
5. **Hand them to the machine** — put them in `codebase/.env`
   (git-ignored; never commit):

   ```
   SLACK_BOT_TOKEN=xoxb-…
   SLACK_CHANNEL_ID=C…
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/…   # optional
   ```

## B. The three integration rungs

| Rung | What | Governance | When |
|---|---|---|---|
| 1 · Off-the-shelf MCP | `npx -y @modelcontextprotocol/server-slack` (env: `SLACK_BOT_TOKEN`, `SLACK_TEAM_ID`) — any MCP client gets `slack_post_message` etc. | **None of ours** — raw token power, no Meridian scope check, no audit row, no idempotency | 5-min wire-up; the *contrast* beat in class |
| 2 · **Our Slack MCP server (the course's answer)** | `meridian_slack_mcp.py` (this folder) — spec-compliant MCP over stdio, stdlib-only, one typed tool `notify_slack` that rides the governed door: scope → idempotency → backoff/breaker → audit → portal-linked card | **Full** — identity is server-side (the model can't pass someone else's badge), every call audited | The engagement answer; M3 stretch |
| 3 · Managed (documented) | Put a `/notify` endpoint behind `/api/v1` and let **AgentCore Gateway** MCP-ify it like the other four tools (one `openapi.json` entry) | Platform RBAC + audit, Gateway auth | Noted for the engagement; not built in the lab |

**Wire rung 2 into a client:**

```bash
# Claude Code
claude mcp add meridian-slack -- python3 <repo>/codebase/labs/w16-integrate/meridian_slack_mcp.py

# smoke-test the whole protocol with no client at all
python3 meridian_slack_mcp.py --selftest
```

**Fully automated flow (what "done" looks like):** the agent works a dispute →
the citation tripwire routes it to HUMAN_REVIEW → the agent calls `notify_slack`
over MCP → the card lands in `#meridian-oncall` → the on-call analyst clicks
**Open in Ops Console** → they are in the portal, on the dispute, with the
audit row already written.

## B2. The five demo steps, on real screens (captured live 2026-07-30)

Workspace `ik-test`, channel `#meridian-oncall` (`C0BLPUH1ZQV`), bot
`meridian-escalations` (`B0BLWT46U9J`). Captures in `course/assets/slack/`:

| # | Screenshot | The beat |
|---|---|---|
| 1 | `slack_1_card_in_channel_2026-07-30.jpg` | The card, posted by **meridian-escalations APP** — header, Type/Reg-E fields, the agent's recommendation with `[chunk 1]`, and the **Open in Ops Console** button |
| 2 | `slack_2_two_cards_portal_unfurl_2026-07-30.jpg` | Two escalations (dispute 4417 via the demo, dispute 29 via MCP) — each with the governed-door context line and Slack's unfurl of the **Meridian Ops Console** link |
| 3 | `slack_3_button_lands_on_portal_login_2026-07-30.jpg` | Clicking the button → the portal **login** gate. The deep link is auth-gated: Slack tells you *what* happened, the portal decides *who may see it* |
| 4 | `slack_4_portal_dispute29_2026-07-30.jpg` | Signed in as Jason Soto (Type A) → dispute #29: Reg E countdown, evidence, Actions |
| 5 | `slack_5_portal_linked_tickets_2026-07-30.jpg` | **Linked tickets (23)** — the agent's writes, newest `#9000000024`. Slack was the *page*; the portal is the *record* |

Teach step 3 deliberately: an escalation channel that could bypass the portal's
RBAC would be a hole in the door. It can't — the notification carries a link,
never the data.

## C. Live verification run (owner of this repo, once credentials exist)

```bash
cd "$(git rev-parse --show-toplevel)"/codebase
set -a; source .env; set +a
cd labs/w16-integrate
python3 slack_integration_demo.py --real          # bot rung: card posts as the app
python3 meridian_slack_mcp.py --selftest          # same, through the MCP protocol
python3 slack_integration_demo.py                 # the 7-scenario mock suite stays green
```

Evidence to capture (dated, committed): the terminal output, a screenshot of
the card in `#meridian-oncall`, and a screenshot of the portal page the card's
button opens. Wire all three into the W16 deck (Slack block) + this lab README.

**Status — LIVE and verified 2026-07-30.** Verbatim capture:
`logs/slack_live_2026-07-30.txt`.
- `auth.test` → `ok:true`, team `ik-test`, bot `meridianescalations`
- `slack_integration_demo.py --real` → `delivered_via: slack`, allow row audited
- `meridian_slack_mcp.py --selftest` (transport mode **bot**) → initialize ✓,
  tools/list ✓, tools/call delivered ✓, **same idem key deduped ✓**, and a
  **Type-B → Type-A escalation denied with `decision: deny` audited** ✓
- five screenshots captured (§B2), the full circle closed in the portal

**Scope note found live:** the installed app has `chat:write` +
`channels:history` (not `chat:write.public`), so the bot must be **in** the
channel — `/invite @meridian-escalations` — and `conversations.list` returns
`missing_scope` without `channels:read`. Both are correct least-privilege
outcomes worth teaching: the app can post where it was invited and cannot
enumerate the workspace.
