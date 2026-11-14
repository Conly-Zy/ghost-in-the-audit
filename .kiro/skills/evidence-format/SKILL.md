---
name: evidence-format
description: The citation schema every Ghost in the Audit finding must conform to before it can be forwarded or written into a report. Use this whenever composing, reviewing, or repairing findings.
---

# Evidence format — the citation contract

A finding is a JSON object with exactly these fields (unknown fields are
rejected — typos must not pass silently):

| Field | Type | Rule |
| --- | --- | --- |
| `claim` | string | ≥ 8 chars, one falsifiable statement |
| `path` | string | repo-relative POSIX path, no `..`, not absolute |
| `line_start` | integer | ≥ 1 |
| `line_end` | integer | ≥ `line_start` |
| `log_evidence` | array | ≥ 1 entry |
| `log_evidence[].timestamp` | string | ISO-8601 **with explicit timezone**, copied verbatim from a log line |
| `log_evidence[].signature` | string | anomaly cluster signature, 3–64 chars of `[A-Za-z0-9_-]` |
| `log_evidence[].log_path` | string? | repo-relative path of the log file |
| `log_evidence[].line` | integer? | line number in the log file |
| `log_evidence[].message` | string? | the matching log line, verbatim |
| `confidence` | enum | `high` \| `medium` \| `low` |
| `agent` | string? | emitting agent name |
| `status` | enum | `forwarded` \| `uncited` \| `rejected` |

## Valid example (passes the gate)

```json
{
  "claim": "The retry handler double-counts timeout errors.",
  "path": "src/payments/retry.ts",
  "line_start": 24,
  "line_end": 31,
  "log_evidence": [
    {
      "timestamp": "2026-10-31T02:12:47Z",
      "signature": "DOUBLE_COUNT_TIMEOUT",
      "log_path": "logs/app.log",
      "line": 8,
      "message": "[DOUBLE_COUNT_TIMEOUT] aggregate tally inflated for ch_8841"
    }
  ],
  "confidence": "high",
  "agent": "data-flow-tracker"
}
```

## Invalid examples (all rejected by the citation gate)

- missing `log_evidence` — a claim with no log timestamp is a guess
- `line_end < line_start` — the citation points at a non-existent range
- `path: "../src/leak.ts"` — traversal outside the repo
- `path: "/etc/passwd"` — absolute paths are not citations
- timestamp without timezone `"2026-10-31T02:14:07"` — ambiguous, therefore unanchored
- unknown field `lineStart` — schema drift; the misspelling must not slip through

## Enforcement

`python3 src/evidence/validator.py` is wired as the `citation-gate` PreToolUse
hook (`.kiro/hooks/citation-gate.json`). It reads the hook event JSON on STDIN
and gates writes that publish findings (anything under `reports/`, plus any
`*findings.json` / `*audit-report*` path):

- **exit 0** — allow (no finding-shaped content, or every finding fully cited)
- **exit 2** — deny; the missing/invalid fields are printed to STDERR so the
  agent can repair the evidence and retry
- **anything else** — fail-closed: the write is blocked. A hook that cannot
  deliver a verdict must never be silently bypassable.

Pre-flight a findings file without the hook:

```bash
python -m src.cli validate reports/findings.json
```
