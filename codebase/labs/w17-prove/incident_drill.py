"""W17 LIVE INCIDENT DRILL — the instructor-triggered staging regression (DoD A4.2).

What this is
------------
A staging traffic simulator that pushes real dispute cases through the REAL
reference pipeline (`meridian_agent`) and renders a live terminal dashboard
(rolling pass rate vs SLO, guardrail-block rate, p95 latency) while writing
every request as an OTel-style JSON trace line to ``incident_traces.jsonl``.

The incident is a ROUTING REGRESSION, injected live: a "config push" points the
policy step's retrieval at a thin index that no longer contains the
provisional-credit chunks. Money-moving recommendations lose their grounding,
the citation tripwire starts BLOCKING them, and the eval pass rate falls
through the SLO. Nothing is faked: the same pipeline, gated by the same
scorecard, actually degrades — the dashboard shows a real system failing.

How the drill runs (run-of-show 2:35)
-------------------------------------
Terminal 1 (on screen):   python3 incident_drill.py
Terminal 2 (instructor):  touch INCIDENT_ON      # the "config push"
                          rm INCIDENT_ON         # the rollback decision
Students watch the dashboard, name the first offending trace_id in chat
(incident commander), decide the rollback, then draft the RCA. On exit the
drill prints the RCA skeleton with the decision fields BLANK (so the "3 a.m.
page" activity isn't spoiled) and writes the evidence-complete reference RCA
to ``incident_rca_reference.txt`` — open that at the debrief, or re-run with
``--reveal`` to print it.

Deterministic fallback (no live toggling, CI-safe):
    python3 incident_drill.py --inject-at 30 --rollback-at 75 --requests 100 --fast
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# works in both layouts: the legacy layout (platform/agent/src) and the learner
# repo (agent/src); MERIDIAN_AGENT_SRC overrides.
_HERE = Path(__file__).resolve()
for _cand in (_HERE.parents[3] / "platform" / "agent" / "src",
              _HERE.parents[2] / "agent" / "src",
              Path(os.environ.get("MERIDIAN_AGENT_SRC", "/nonexistent"))):
    if (_cand / "meridian_agent").is_dir():
        sys.path.insert(0, str(_cand))
        break

from meridian_agent.golden import GoldenRepo, load_golden  # noqa: E402
from meridian_agent.pipeline import run_pipeline  # noqa: E402
from meridian_agent.trace import Tracer  # noqa: E402

FLAG = Path(__file__).parent / "INCIDENT_ON"
TRACES = Path(__file__).parent / "incident_traces.jsonl"
RCA_REF = Path(__file__).parent / "incident_rca_reference.txt"
SLO = 0.95           # eval pass-rate SLO on the rolling window
WINDOW = 25          # requests per rolling window
GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m")


class ThinIndexRepo(GoldenRepo):
    """The regression: same store, wrong index — the provisional-credit chunks
    are missing from retrieval. This is what a bad config push looks like."""

    backend = "golden(thin-index)"

    def get_policy_chunks(self):
        return [c for c in super().get_policy_chunks()
                if "provisional" not in c["content"].lower()]


def _traffic(golden):
    """Deterministic rotation over golden standard cases (embedded bundles)."""
    cases = [c for c in golden["cases"] if c["category"] == "standard"]
    i = 0
    while True:
        yield cases[i % len(cases)]
        i += 1


def _quiet_run(repo, case):
    import contextlib
    import io
    t0 = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        sc = run_pipeline(repo, case["bundle"]["dispute"]["dispute_id"],
                          tracer=Tracer(),
                          now=datetime.fromisoformat(case["now"]))
    ms = (time.perf_counter() - t0) * 1000
    return sc, ms


def _dashboard(n, window, incident_live, first_bad):
    ok = sum(1 for w in window if w["eval_pass"])
    rate = ok / len(window) if window else 1.0
    blocks = sum(1 for w in window if w["guardrail_blocked"])
    lat = sorted(w["latency_ms"] for w in window) or [0.0]
    p95 = lat[max(0, int(0.95 * len(lat)) - 1)]
    color = GREEN if rate >= SLO else RED
    slo_tag = "OK " if rate >= SLO else "BREACH"
    inc = f"  {RED}◉ config drift live{RESET}" if incident_live else ""
    line = (f"req {n:>4} │ window pass {color}{rate:>4.0%}{RESET} "
            f"(SLO {SLO:.0%} {color}{slo_tag}{RESET}) │ "
            f"blocks {blocks:>2}/{len(window)} │ p95 {p95:5.1f}ms{inc}")
    if first_bad:
        line += f"  {DIM}first offending trace: {first_bad}{RESET}"
    print(line)


def _rca_reference(events, first_bad_evt, inject_n, rollback_n, total):
    """The evidence-complete RCA — the ANSWER KEY for the 3 a.m. activity."""
    bad = [e for e in events if not e["eval_pass"]]
    lines = ["── RCA REFERENCE (generated from incident_traces.jsonl) ──",
             "INCIDENT: staging eval pass rate breached the 95% SLO"]
    if first_bad_evt:
        lines.append(f"DETECTION: first failing trace {first_bad_evt['trace_id']} "
                     f"(request #{first_bad_evt['n']}, dispute {first_bad_evt['dispute_id']})")
    if inject_n is not None:
        lines.append(f"TIMELINE:  config push at request #{inject_n}"
                     + (f" → rollback at #{rollback_n}" if rollback_n is not None else " → NO ROLLBACK"))
    lines.append(f"BLAST RADIUS: {len(bad)}/{total} requests failed the gate; "
                 f"{sum(1 for e in bad if e['guardrail_blocked'])} money-moving "
                 f"recommendations were BLOCKED by the citation tripwire (fail-closed — "
                 f"no uncited credit reached a human or the ledger)")
    lines.append("ROOT CAUSE: retrieval config pointed the policy step at an index "
                 "missing the provisional-credit chunks; recommendations lost their "
                 "grounding and the output guardrail (correctly) refused them")
    lines.append("WHAT HELD:  the tripwire + human queue — the failure was loud and "
                 "safe, not silent and wrong")
    lines.append("ACTION ITEMS: [ ] gate config pushes behind the golden suite  "
                 "[ ] alert on block-rate spike, not just pass rate  "
                 "[ ] add index-completeness check to the deploy runbook")
    lines.append(f"evidence: {TRACES} ({total} trace lines)")
    return lines


def _rca(events, first_bad_evt, inject_n, rollback_n, total, reveal=False):
    bad = [e for e in events if not e["eval_pass"]]
    ref = _rca_reference(events, first_bad_evt, inject_n, rollback_n, total)
    RCA_REF.write_text("\n".join(ref) + "\n")

    if reveal:
        print(f"\n{BOLD}{ref[0]}{RESET}")
        for line in ref[1:]:
            print(line)
        return

    # Spoiler-free by default: the class fills these blanks in the "3 a.m.
    # page" activity BEFORE the reference is revealed.
    push = f"config push at request #{inject_n}" if inject_n is not None else "config push at request #____"
    print(f"\n{BOLD}── RCA SKELETON (generated from incident_traces.jsonl) — fill the blanks ──{RESET}")
    print("INCIDENT: staging eval pass rate breached the 95% SLO")
    print(f"DETECTION: ____________________________________   {DIM}<- the evidence: which trace?{RESET}")
    print(f"TIMELINE:  {push} → _________   {DIM}<- rollback? when?{RESET}")
    print(f"BLAST RADIUS: {len(bad)}/{total} requests failed the gate in the incident window")
    print(f"ROOT CAUSE: ____________________________________   {DIM}<- from the traces: what changed at the flip?{RESET}")
    print(f"WHAT HELD:  ____________________________________   {DIM}<- loud-and-safe, or silent-and-wrong?{RESET}")
    print(f"ACTION ITEMS: [ ] ______________________________   {DIM}<- name ONE{RESET}")
    print(f"{DIM}evidence: {TRACES} ({total} trace lines){RESET}")
    print(f"{DIM}instructor — reference RCA (for the debrief, AFTER the class answers): "
          f"{RCA_REF.name}  ·  or re-run with --reveal{RESET}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--requests", type=int, default=0,
                    help="stop after N requests (0 = run until Ctrl-C)")
    ap.add_argument("--inject-at", type=int, default=None,
                    help="deterministic mode: inject the regression at request N")
    ap.add_argument("--rollback-at", type=int, default=None,
                    help="deterministic mode: roll back at request N")
    ap.add_argument("--fast", action="store_true", help="no sleeps (CI / rehearsal)")
    ap.add_argument("--reveal", action="store_true",
                    help="print the filled reference RCA on exit (debrief mode; "
                         "default prints the blanked skeleton so the activity "
                         "isn't spoiled)")
    args = ap.parse_args()

    golden = load_golden()
    FLAG.unlink(missing_ok=True)
    TRACES.unlink(missing_ok=True)
    events, window = [], []
    first_bad = None
    inject_n = rollback_n = None

    print(f"{BOLD}MERIDIAN STAGING — dispute traffic vs the eval SLO{RESET}")
    print(f"{DIM}instructor: `touch INCIDENT_ON` = the config push · "
          f"`rm INCIDENT_ON` = the rollback · Ctrl-C ends the drill{RESET}\n")

    n = 0
    try:
        with TRACES.open("a") as sink:
            for case in _traffic(golden):
                n += 1
                live_flag = FLAG.exists()
                if args.inject_at is not None:
                    live_flag = (n >= args.inject_at and
                                 (args.rollback_at is None or n < args.rollback_at))
                if live_flag and inject_n is None:
                    inject_n = n
                if not live_flag and inject_n is not None and rollback_n is None and n > inject_n:
                    rollback_n = n

                repo_cls = ThinIndexRepo if live_flag else GoldenRepo
                repo = repo_cls(case, golden["base_policy_chunks"])
                sc, ms = _quiet_run(repo, case)
                eval_pass = all(ok for _, ok, _ in sc.checks)

                evt = {
                    "n": n,
                    "trace_id": f"trc-{n:05d}-{sc.dispute_id}",
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "dispute_id": sc.dispute_id,
                    "route": repo.backend,
                    "recommendation": sc.recommendation,
                    "guardrail_blocked": sc.guardrail_blocked,
                    "eval_pass": eval_pass,
                    "latency_ms": round(ms, 1),
                }
                events.append(evt)
                sink.write(json.dumps(evt) + "\n")
                if not eval_pass and first_bad is None:
                    first_bad = evt["trace_id"]

                window.append(evt)
                if len(window) > WINDOW:
                    window.pop(0)
                if n % 5 == 0:
                    _dashboard(n, window, live_flag, first_bad)
                if args.requests and n >= args.requests:
                    break
                if not args.fast:
                    time.sleep(0.4)
    except KeyboardInterrupt:
        print(f"\n{DIM}drill ended by instructor{RESET}")

    first_bad_evt = next((e for e in events if not e["eval_pass"]), None)
    _rca(events, first_bad_evt, inject_n, rollback_n, len(events), reveal=args.reveal)
    FLAG.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
