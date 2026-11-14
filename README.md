# Ghost in the Audit

*A Kiro Crew audit that refuses to guess.*

Ghost in the Audit orchestrates a team of specialized subagents to investigate unfamiliar codebases. Every finding it produces carries a verifiable citation—a file, a line range, and a log timestamp. If an agent cannot back a claim with evidence, the finding does not leave its session.

Inspired by the haunted-house feeling of opening a repository you did not write. The exorcism is an audit; every ghost gets a name and a citation.

## Why this exists

A single AI agent investigating an unfamiliar codebase forces you to be the switchboard. You ask it to read logs, then to trace data flow, then to write the report—each handoff waits for you. Kiro Crew subagents remove that bottleneck: they run in parallel with isolated context, and their results are injected back into the main session as they complete.

But parallelism without accountability is just faster guessing. Ghost in the Audit adds one non-negotiable rule: **no evidence, no forwarding.**

## How it works

```text
┌─────────────────────────────────────────────────────────────┐
│                         Exorcist                            │
│  (orchestrator · spawns subagents · assembles final report) │
└────────────┬────────────────────────────┬───────────────────┘
             │                            │
             ▼                            ▼
   ┌─────────────────┐          ┌───────────────────┐
   │  Log Whisperer  │          │ Data-flow Tracker │
   │  clusters log   │          │  traces call      │
   │  anomalies      │          │  chains around    │
   │                 │          │  each anomaly     │
   └────────┬────────┘          └─────────┬─────────┘
            │                             │
            └──────────────┬──────────────┘
                           ▼
                 ┌──────────────────┐
                 │ Citation Auditor │
                 │  validates every │
                 │  finding against │
                 │  evidence schema │
                 └────────┬─────────┘
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
     citation valid            citation missing
     → forward to report       → reject + return
                                 to sender, never
                                 reaches the report
```

The orchestration is declared as a Kiro Crew workflow. Subagents run with isolated context and full project memory injection—they see your preferences and project conventions, but they do not see each other's in-progress work, which prevents cross-contamination between investigation threads.

A Kiro Hook (`citation-gate`) fires before every write that could publish findings. For PreToolUse, the hook is fail-closed: if it cannot deliver a verdict (timeout, crash, missing script), the tool call is blocked and resolves to deny. Breaking the hook cannot silently disable the policy it enforces.

## What a finding looks like

Every forwarded finding carries:

```json
{
  "claim": "The retry handler double-counts timeout errors.",
  "path": "src/payments/retry.ts",
  "line_start": 142,
  "line_end": 178,
  "log_evidence": [
    {
      "timestamp": "2026-10-31T02:14:07Z",
      "signature": "DOUBLE_COUNT_TIMEOUT"
    }
  ],
  "confidence": "high"
}
```

A finding without `path`, `line_start`/`line_end`, or at least one `log_evidence` entry is rejected before assembly. It does not become a row in the final report.

## Quick start

### Prerequisites

- Kiro installed and authenticated, with Crew (subagents) enabled
- Python 3.11+ (for the evidence validator)
- `pip install -r requirements.txt` — pydantic, hypothesis, pytest
- A repository you want to audit (or use the included `demo/sample-repo/`)

### Run the audit

```bash
git clone https://github.com/Conly-Zy/ghost-in-the-audit.git
cd ghost-in-the-audit

# The .kiro/ directory is the project configuration—agents, hooks, specs, and steering.
# No global setup is needed; Kiro reads project-scoped config from .kiro/.

kirocrew spawn run "Audit demo/sample-repo for anomalies. Every finding must carry file:line and log evidence."
```

For a fire-and-forget run:

```bash
kirocrew spawn run --async "Audit demo/sample-repo ..."
kirocrew spawn list
```

### Inspect what the Hook blocked

The citation-gate Hook writes rejection reasons to STDERR, and appends every verdict to `logs/citation-gate.log`. In the Activity panel, a blocked write shows the hook verdict next to the tool call. To reproduce the rejection scenario deliberately:

```bash
python tests/test_evidence_schema.py --demo-rejection
```

You can also pre-flight a findings file before the audit does:

```bash
python -m src.cli validate reports/findings.json
```

## Repository structure

```text
.kiro/
├── agents/           # Four custom agents, each with a JSON config + prompt
│   └── prompts/      # file://-referenced prompt files
├── hooks/            # citation-gate: fail-closed evidence validation
├── skills/           # evidence-format: shared citation schema skill
├── steering/         # Always-injected product context
├── specs/            # requirements.md · design.md · tasks.md
└── settings/         # mcp.json
src/
├── evidence/         # Pydantic schema + validator called by the Hook
└── cli.py            # Optional CLI wrapper
tests/
└── test_evidence_schema.py   # Property-based tests: uncited findings must be rejected
demo/
└── sample-repo/              # A haunted, unfamiliar codebase for the demo
```

## Built with

Kiro Specs · Kiro Hooks · Kiro Agents · Kiro Crew · Kiro MCP · Python · Pydantic · Hypothesis

## Demo

A 2–3 minute recording covers the full loop: Exorcist spawns the crew → a subagent returns an uncited finding → the citation-gate hook blocks the write and prints the missing fields on STDERR → the agent repairs the evidence → the Citation Auditor forwards it → the final report shows `file:line` + log-timestamp provenance for every row.

<!-- Demo video link (public or unlisted, no password): _to be attached before submission_ -->

## Design rule

When the costume and the tool conflict, the tool wins.

The haunted-room metaphor lives in the activity feed and the demo narrative. The audit report itself stays deliberately plain: every claim, its citation, and nothing else. A ghost you cannot point to on a specific line of a real file does not get to exist.

## License

MIT — see [LICENSE](LICENSE).
