# Week 17 Lab — The Eval Suite, Observability & the Incident Drill

**Milestone produced:** **M4 — Capstone complete** (spans W13–17).
**You build on:** the `eval` gate from W15 (`platform/agent`), now run as the full **golden +
adversarial** deploy gate, plus observability and an incident drill.

> Story: Meridian (a mid-size US bank) was failing the 10-business-day legal clock (Regulation E)
> on 12.8% of disputed card charges; the agent now drafts recommendations into the bank's own
> screen, human-approved. Compliance said *"prove this can't happen, or we don't go live."*
> Today you turn "trust us" into evidence — and hand the client a system their own team can operate.

**In-class activities (watch + decide — you answer in chat, nobody codes):**
- ✳ **"Set the gate"** (0:40, ~8 min) — you pick the release bar, the zero-tolerance case, and the
  failure path for three very different clients
- ✳ **"The 3 a.m. page"** (3:05, ~10 min, right after the live drill) — you fill the four blanked
  fields of the incident write-up from the real trace evidence

---

## 0. Run the deploy gate (2 min)

```bash
cd "$(git rev-parse --show-toplevel)"/codebase/agent
PYTHONPATH=src python3 -m meridian_agent eval --suite golden --min-pass 0.95 --mock
echo "exit code: $?"     # 0 = deploy allowed, 1 = blocked
```
Real output (this exact run, mock brain, $0):
```
golden suite v1.0 — 150 cases · gate >= 95% AND 0 adversarial failures
  adversarial   13/13
  edge          19/19
  standard     118/118
  (no failing cases)

pass rate: 100% (150/150)  ·  GATE PASSED
```
The suite is the **150-case golden set, embedded and real** (`codebase/agent/src/meridian_agent/data/golden_set.json`) —
a golden set being a fixed exam: cases with known right answers:
118 cases fetched from the live platform (seed 4417) + 32 authored edge/adversarial cases
(adversarial = designed to trick the system), labeled
from Reg E first principles with a per-case rationale and a frozen clock. Per-case grading is
execution-based on scorecard fields: **classification**, **urgency**, **recommendation**,
**groundedness** (citation present), **poisoned-chunk quarantine** (a malicious instruction
hidden inside a retrieved document — prompt injection, a.k.a. XPIA),
**guardrail enforcement** (no uncited provisional credit), **Reg E deadline**, and the
**handoff decision** — i.e. the W16 incident, weaponized into permanent tests.
**Gate semantics:** pass = (rate ≥ `--min-pass`) **AND** (zero adversarial failures) — one live
injection hole blocks the release regardless of the average. Useful flags: `--cases N`,
`--category standard|edge|adversarial`, `--verbose`.

**$0 path:** the gate runs in `--mock` with no key. For the second grader use **Promptfoo**
(OSS): `promptfoo-golden.yaml` re-runs all 150 cases (verified to agree 150/150 with the gate),
and `promptfoo.yaml` adds LLM-rubric asserts with **ollama** as the default provider; **local
OTel + Grafana** for dashboards instead of CloudWatch.

**Run it at home:** fork + clone this repo — https://github.com/aeropj/ik-fde —
then `make verify` and `make golden` (the full 150-case gate).

---

## Activity — "Set the gate" (0:40–0:48, before Lab I)

Three clients — a bank moving money (this engagement), an internal IT-helpdesk bot, a
marketing-copy assistant. For each you pick, in chat: the pass bar, what gets zero tolerance
(one failure blocks the release), and what happens on failure. The model answers are revealed
in class. The point: the bar is a *consequence* score, not a quality
score — and not every system deserves a deploy gate.

---

## Lab I — the eval suite (0:50–1:55, instructor-driven demo)

**Not a code-along:** in class the instructor runs every command on the shared screen with
predict-then-verify checkpoints; you run everything yourself afterwards for M4 (your fork, above).

1. **Golden set:** the full 150-case gate (`--suite golden --min-pass 0.95 --mock`) with its
   per-category breakdown. Acceptance criteria = the M0/M1 thresholds.
2. **Adversarial zero tolerance:** `--category adversarial` runs the 13 adversarial cases (the
   poisoned chunk, the exfiltration attempt, the confidently-wrong 45-vs-10-day Reg E deadline).
   One `ADV` failure blocks deploy even at 99% aggregate — the sabotage test in
   `codebase/agent/tests/test_golden.py` proves the clause.
3. **Cross-check graders:** the execution-graded golden gate **and** Promptfoo must agree.
   Run both **from this lab folder** (the provider resolves the agent from here). Prereqs:
   Node 18+ (`npx` ships with it) + Python; no API key needed.
   ```bash
   # agreement runner — all 150 golden cases, must match the gate case-by-case:
   npx promptfoo@latest eval -c promptfoo-golden.yaml --no-cache   # expect: 150 passing
   # rubric grader — needs a local model ($0): install ollama, `ollama pull llama3.1`, then:
   npx promptfoo@latest eval -c promptfoo.yaml --no-cache          # expect: 3 scenarios pass
   npx promptfoo@latest view                                       # per-case results in browser
   ```
   (No ollama? Swap in the commented `bedrock:` provider in `promptfoo.yaml` if you have AWS
   creds.) Judge failure modes + calibration: notebook §5.
4. **Red-team:** run your prompt-injection/adversarial set against your own deployment before
   the client's red team does (start from the W16 incident library; Bedrock Guardrails covers
   the generic classes — your domain tripwires stay yours).

- ✅ **Checkpoints 1–4** (paste from the shared screen after predicting): the smoke `GATE PASSED`
  line + exit code · the golden `pass rate … GATE PASSED` line · the adversarial `13/13` line ·
  a Promptfoo pass-count line.
- **Poll:** *96% overall but 1 adversarial Reg E failure — ship?* (The gate answers for you:
  24/25 = 96% clears `--min-pass 0.95`, but one adversarial failure still blocks. A Reg E
  mis-citation is the regulator-facing failure the engagement exists to prevent.)

---

## Lab II — observability & the incident drill (2:10–3:20)

- **Dashboards (3):** eval-pass rate (quality) · latency (experience) · cost per resolution
  (economics) — from OpenTelemetry traces (OTel — the open standard for traces) → CloudWatch /
  AgentCore Observability (or local OTel + Grafana).
- **Reliability artifacts:** SLO/SLA (the promise of how well the system runs — here: ≥95% of
  checks pass — and its contractual form; set back in M0), **runbook**, one-command **rollback**,
  **RCA** (the written story of what broke, how we found it, and what changes) template.

### The drill (live, 2:30–3:05) — `incident_drill.py`
Terminal 1 streams real golden cases through the **real pipeline** with a live dashboard
(rolling pass rate vs the 95% SLO, block rate, p95 — the time the slowest 1-in-20 requests
takes) and writes every request to
`incident_traces.jsonl`. Terminal 2 (instructor): `touch INCIDENT_ON` = the config push that
points retrieval at a thin index missing the provisional-credit chunks; `rm INCIDENT_ON` = the
rollback. On exit the drill prints the RCA skeleton with the decision fields **blank** (the
"3 a.m. page" answers stay unspoiled) and writes the filled reference to
`incident_rca_reference.txt` — opened only at the debrief (`--reveal` prints it). You:
1. **Detect** it on the dashboard — the window pass rate falls through the SLO, block rate spikes.
2. **Race:** first to paste the offending **trace_id** (`trc-…`) from the dashboard is
   "incident commander" for the debrief (✅ **Checkpoint 5** — the trace_id line is the evidence).
3. **Decide** rollback vs hotfix (then watch the window recover after `rm INCIDENT_ON`).
4. **Fill the RCA** — the drill prints the blanked skeleton; the class fills it in the
   "3 a.m. page" activity, then the reference from `incident_rca_reference.txt` is revealed.
   The prevention line MUST include a new eval case.

**Re-run it at home (deterministic, CI-safe):**
```bash
python3 incident_drill.py --inject-at 30 --rollback-at 75 --requests 100 --fast
```

### Activity — "The 3 a.m. page" (3:05–3:15, right after the drill)

You are Meridian's on-call at 3 a.m.; the vendor rolled off months ago. The drill's RCA
skeleton goes up with four fields blanked — detection evidence, rollback decision + when,
what held, one action item — next to the real trace excerpts. Fill them in chat
(`DETECT: … | ROLLBACK: … | HELD: … | ACTION: …`), then the drill's auto-generated answer is
revealed. The reference RCA is `incident_rca_reference.txt`, which `incident_drill.py` writes
from the deterministic run (or re-run it with `--reveal`).

---

## The steering-committee readout (3:20–3:38)
Present value to "the CIO" in **business terms**, not architecture:
*disputes auto-resolved · agent-caused Reg E breaches · cost per resolution · handle time on the hard 20%.*

---

## Definition of done (M4 floor)
- [ ] Deploy gate green **including adversarial cases** (`eval --suite golden --min-pass 0.95 --mock`, all 150).
- [ ] Live dashboard (latency / cost / eval-pass).
- [ ] RCA + runbook + handover pack (escalation paths, "what we'd watch monthly").
- [ ] Steering-committee deck (value in business terms).

**Tiers:** Operator (the floor) · Builder (floor + ≥2: extend the golden set, continuous-eval
sketch, a second RCA, judge calibration executed) · Architect (Builder + ≥2: managed guardrails
layered, the drill packaged for the client, multi-release gate evidence, top-quartile leaderboard).

> Cliffhanger → W18: renewal signed; the risk team asks *"could your agent research counterparties —
> news, filings, sanctions — before merchants go bad?"* Interview season opens.
