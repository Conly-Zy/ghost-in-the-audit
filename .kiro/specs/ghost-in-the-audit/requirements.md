# Requirements — Ghost in the Audit

Kiroween submission. The product promise: auditing an unfamiliar repository
must produce **only evidence-backed findings**. Everything below is written so
that "no evidence, no forwarding" is enforced by the system, not by agent
goodwill.

## 1. Orchestration (Exorcist)

- WHEN a run starts, the Exorcist SHALL locate the target repository and its log files, and spawn `log-whisperer` and `data-flow-tracker` in parallel with an identical leads package (log paths, code directories, suspected signatures).
- The Exorcist SHALL route every finding through `citation-auditor` before it enters the final report.
- IF `citation-auditor` rejects a finding, the Exorcist SHALL return it to the sender with the rejection reasons inlined, for at most 2 re-investigation iterations.
- IF a finding is still uncited after the final iteration, the Exorcist SHALL publish it in the report's `uncited` appendix rather than dropping it.
- IF a subagent returns partial output (stalled ~2 min, or stopped by the user), the Exorcist SHALL treat that output as uncited input and route it through `citation-auditor` like any other finding.
- The Exorcist SHALL NOT read logs or trace code itself; its only outputs are spawn instructions, loop-backs, verdict routing, and the final report.

## 2. Log Whisperer

- WHEN the Log Whisperer receives a log stream, the system SHALL return anomalies clustered by anomaly signature.
- Each anomaly SHALL carry at least one raw log timestamp, copied verbatim, with the log path and line number.
- IF an anomaly cannot be anchored to a concrete log line, the system SHALL mark it `uncited` instead of silently dropping it.
- The Log Whisperer SHALL NOT read source code files.

## 3. Data-flow Tracker

- WHEN the Data-flow Tracker receives an anomaly lead, the system SHALL return findings whose `path`, `line_start`, and `line_end` are read from the actual files at audit time.
- Each finding SHALL include at least one log evidence entry with a verbatim, timezone-aware ISO-8601 timestamp.
- IF the code at fault cannot be located, the system SHALL return the finding as `uncited` with the reason, instead of weakening the citation.

## 4. Citation Auditor

- WHEN the Citation Auditor receives a finding from an upstream agent, the system SHALL verify that it contains `path`, `line_start`, `line_end`, and at least one log evidence entry.
- IF a finding misses any required field, the system SHALL refuse to forward it and return the rejection reason to the sender.
- WHEN timestamps are checked, the system SHALL require ISO-8601 with an explicit timezone; naive timestamps SHALL be rejected.
- WHEN a path is checked, the system SHALL require a relative in-repo POSIX path with no `..` traversal; absolute paths SHALL be rejected.
- WHEN the line range is checked, the system SHALL require `line_start >= 1` and `line_end >= line_start`.
- IF a finding contains unknown or misspelled fields, the system SHALL reject it (schema drift is a rejection, not a warning).
- IF the auditor cannot determine validity, it SHALL reject with reasons (fail-closed) — never guess, never repair the finding itself.

## 5. citation-gate hook

- WHEN any write would publish finding-shaped content (writes under `reports/`, or any `*findings.json` / `*audit-report*` path), the hook SHALL validate every finding against the evidence schema.
- IF validation fails, the hook SHALL deny the write (exit 2) and surface the missing or invalid fields on STDERR.
- IF the hook cannot reach a verdict (malformed event, crash, missing interpreter), the write SHALL be blocked (fail-closed): a hook that cannot deliver a verdict must not be silently bypassable.
- Writes that do not publish findings (source code, docs, prose) SHALL pass the gate untouched.
- Every verdict SHALL be appended (best-effort) to `logs/citation-gate.log` so the demo can show the interception trail.

## 6. Final report

- WHEN the audit completes, the Exorcist SHALL write `reports/audit-report.md` and `reports/findings.json`.
- The report SHALL contain only findings that passed the citation gate, plus the `uncited` appendix.
- Each forwarded row SHALL display its citation: `path:line_start–line_end` and the log timestamp(s).
- The `uncited` appendix SHALL present entries as prose bullets (claim, sender, missing evidence) and SHALL NOT embed finding-shaped JSON: the gate accepts only fully cited findings, so an uncited claim cannot masquerade as a report row.

## Acceptance criteria

- [ ] A deliberately uncited finding is blocked by the hook, and the rejection reason (missing fields) appears on STDERR.
- [ ] A fully cited finding passes the hook unchanged.
- [ ] A malformed hook event blocks the write (fail-closed), never silently allows it.
- [ ] `pytest tests/` passes, including property tests asserting that every malformed finding shape is rejected.
