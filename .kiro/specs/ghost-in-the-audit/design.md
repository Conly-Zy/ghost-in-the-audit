# Design — Ghost in the Audit

## Orchestration DAG

Kiro Crew fixes the task graph upfront — before the first subagent starts, the
topology cannot be rewired mid-flight. Dynamic behavior is expressed as
configurable loop-backs (`trigger` + `max_iterations`), not graph mutation.

```text
          spawn (parallel, identical leads package)
 Exorcist ──┬──▶ log-whisperer ──┐
            │    (logs → anomaly clusters, verbatim timestamps)
            │                    │ findings
            ├──▶ data-flow-tracker (per lead, parallel)
            │    (call chains → path:line citations)
            │                    │ findings
            ▼                    ▼
        citation-auditor  ◀──────┘
            │  verdict
            ├── forwarded ──▶ report assembly (reports/*)
            └── rejected ───▶ loop back to sender
                              (trigger: auditor rejection,
                               max_iterations: 2)
                              └── still uncited ──▶ report "uncited" appendix
```

## Parallel vs serial

- **Parallel:** `log-whisperer` and `data-flow-tracker` run simultaneously on
  the same anomaly space with isolated context. Each subagent gets its own
  session plus full project-memory injection (preferences, steering), but no
  visibility into the main conversation or into each other — independent
  conclusions that agree are signal, not cross-contamination.
- **Serial:** `citation-auditor` strictly after both investigators; report
  assembly strictly after the auditor's verdicts.
- **Isolation consequences:** every spawn prompt must inline the inputs —
  target paths, the evidence contract, the expected JSON — because subagents
  cannot "see what the orchestrator sees".
- **Concurrency & failure model:** crew concurrency auto-sizes (≈3–32, excess
  queued); each subagent has a 30-minute hard timeout and a ~2-minute stall
  warning. The design assumes not every subagent returns on time: partial
  output from stalled/stopped subagents is handled as `uncited` input and
  routed through the auditor like any other finding.

## citation-gate hook (fail-closed)

- **Event:** `PreToolUse`, matcher `fs_write` (`.kiro/hooks/citation-gate.json`).
- The validator (`src/evidence/validator.py`) reads the hook event JSON on
  STDIN (`hook_event_name`, `tool_name`, `tool_input`, `cwd`, `session_id`, …).
- **Scope:** only writes that publish findings are gated — paths matching
  `reports/`, `*findings.json`, or `*audit-report*`. Ordinary writes (source
  code, docs) pass untouched; this keeps the gate precise instead of
  heuristic-sniffing every write.
- **Exit semantics:**
  - `0` — allow; one status line on STDOUT (injected as context).
  - `2` — deny; missing/invalid fields printed to STDERR, returned to the LLM
    so the agent can repair evidence and retry.
  - anything else (`1`, timeout, crash, missing interpreter) — **fail-closed**:
    PreToolUse resolves a missing verdict to *blocked*. Breaking the hook can
    never silently disable the policy it enforces.
- **Verdict log:** every decision is appended (best-effort) to
  `logs/citation-gate.log` (override with `CITATION_GATE_LOG`) — the
  interception trail shown in the demo.

## Evidence schema

Defined in `src/evidence/schema.py` (Pydantic v2, `extra="forbid"`) and
documented in `.kiro/skills/evidence-format/SKILL.md`. Agents emit findings in
exactly that shape; unknown fields are rejections, not warnings. The same
module is the single source of truth used by the hook, the auditor's checks,
and the property tests.

## Property testing

Hypothesis generates finding-shaped inputs — complete citations, every
required field individually removed, malformed timestamps, inverted line
ranges, path traversal — and asserts the gate's invariant:

> For every finding the gate allows, the citation is complete.

This tests the *shape* of valid input rather than a handful of hand-written
examples, which is what makes "the interception is real" demonstrable.

## Known limitations (documented, accepted for scope)

- The gate inspects `fs_write` content only; a shell heredoc could
  theoretically bypass it. Steering forbids report writes via shell, and the
  Citation Auditor remains the second line of defense.
- The gate does not verify that cited lines exist on disk (findings may cite
  the repo under audit, which is not necessarily present at report time);
  existence spot-checks are the auditor's job.
- The gate does not block a findings report with **zero** findings — an empty
  audit is a legitimate result, and the `uncited` appendix carries the rest of
  the story.
- The `uncited` appendix is written as prose bullets, never as finding-shaped
  JSON, so uncited claims cannot slip past a gate that only accepts fully
  cited findings (see requirements §6).
