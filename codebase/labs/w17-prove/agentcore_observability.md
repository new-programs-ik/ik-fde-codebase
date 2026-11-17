# AgentCore Observability — the auditor's view of the W15 deploy

**W17 supplementary · ~5 min.** In W15, `demo2` printed spans by hand
(`trace.py`-style console lines) and the AgentCore deploy wired the managed
version: OTel traces of every model call, tool call, and handoff, delivered to
CloudWatch. This page is where the W17 auditor goes looking for them. The
concepts live in `codebase/docs/agentcore-deep-dive.md` §3c; this is the
command card.

## What the verified deploy vended (2026-07-12 capture)

From `codebase/labs/w15-build/demo/logs/deploy_agentcore.txt`, the deploy created and
wired, without being asked:

- log groups per resource —
  `/aws/vendedlogs/bedrock-agentcore/memory/APPLICATION_LOGS/meridian_triage_mem-tgHNJ3Ag5Q`
  and `/aws/bedrock-agentcore/runtimes/meridian_triage-od4fT79qSQ-DEFAULT`
- X-Ray trace segment destination → CloudWatch Logs ("Transaction Search
  configured"), with a documented ~10–15 min PENDING window on first launch
- the **GenAI Observability dashboard**:
  `https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#gen-ai-observability/agent-core`

## The three commands an auditor runs

```bash
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --log-stream-name-prefix "<yyyy/mm/dd>/[runtime-logs" --follow

aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --log-stream-names "otel-rt-logs" --since 1h

aws logs start-query --log-group-name aws/spans \
  --start-time <epoch-3h> --end-time <epoch-now> \
  --query-string 'fields @timestamp, name, attributes.session.id
                  | sort @timestamp desc | limit 20'
```

The first is the agent's own stdout/stderr (your spans, your guardrail
verdicts); the second is the OTel export stream — the machine-readable version
of the same story; the third queries **`aws/spans`**, the log group where every
span actually lands (`AgentCore.Runtime.Invoke`, memory `CreateEvent` /
`ListEvents`, model calls) and which the GenAI Observability views render from.
`<runtime-id>` is printed by `agentcore deploy`
(`meridian_triage-Vigq5WHGKb` in the 2026-07-30 capture).

**Console honesty note (verified live 2026-07-30):** on a locked-down shared
account the GenAI Observability *sessions/traces list views* can spin
indefinitely while the spans sit perfectly queryable in `aws/spans`. Never
build a handover runbook on a dashboard you don't control — list the queries.
Real console captures (dashboard front door, the spans query with results, the
runtime record): `course/assets/aws-console/*_2026-07-30.jpg`.

## Why W17 cares

This is the W15 lesson, productized: when the client asks *"prove the agent
didn't breach the SLA at 3 a.m."*, the answer is a trace query, not a shrug.
The eval gate (`meridian-agent eval`) says the agent *should* behave; the
trace stream shows what it *did*. A handover runbook that lists these two
commands plus the dashboard URL passes the auditor test; one that says "check
the logs" does not.

## Account status

Live tailing requires the runtime to exist and your identity to have
CloudWatch + AgentCore access. On the IK shared account the Lab Control
Panel's **Start/Stop is the IAM switch**: stopped ⇒ explicit deny on
`bedrock-agentcore:*` and everything fails closed (captured denial:
`codebase/labs/w16-integrate/agentcore_gateway/logs/`); started ⇒ all three commands
work as written — most recently verified end-to-end on **2026-07-30**
(`codebase/labs/w15-build/demo/logs/agentcore_session_2026-07-30.txt`: invoke → ticket
9000000024 → memory round-trip → logs tail → spans query). On your own
account, deploy the W15 runtime and these commands work as written.
