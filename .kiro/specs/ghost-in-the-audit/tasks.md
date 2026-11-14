# Tasks — Ghost in the Audit

Implementation checklist for the spec. Items are marked done only after the
corresponding verification has actually run.

## Foundation

- [x] Satisfy Kiroween hard requirements: public-repo-ready layout, MIT `LICENSE` at root, `.kiro/` committed (never gitignored), `.env.example` placeholders only
- [x] `.gitignore`: secrets and runtime artifacts out; explicit note that `.kiro/` must never be excluded

## Specs first (no code before this)

- [x] `requirements.md` — EARS contracts for all four agents + hook + report
- [x] `design.md` — DAG, parallel vs serial stages, loop-back (`max_iterations: 2`), fail-closed hook semantics, isolation consequences
- [x] `tasks.md` — this file

## Agents & context

- [x] Four agent configs under `.kiro/agents/` (exorcist, log-whisperer, data-flow-tracker, citation-auditor), each with a `file://` prompt
- [x] Exorcist declares `subagent` in `tools` with `availableAgents` allowlist + `trustedAgents`
- [x] Prompts make isolation explicit: inputs inlined per spawn, uncited as first-class output, no fabrication
- [x] `.kiro/steering/product.md` — always-injected product context with the hard rules
- [x] `.kiro/skills/evidence-format/SKILL.md` — shared citation schema skill

## Evidence engine

- [x] `src/evidence/schema.py` — Pydantic v2 models, `extra="forbid"`, tz-aware timestamp check, repo-relative path check, ordered line range
- [x] `src/evidence/validator.py` — STDIN hook-event → verdict; exit 0 / 2 / fail-closed 1; reasons to STDERR; verdict log to `logs/citation-gate.log`
- [x] `.kiro/hooks/citation-gate.json` — PreToolUse · `fs_write` · `python3 src/evidence/validator.py`
- [x] `src/cli.py` — `validate` pre-flight + `audit` spawn-command wrapper

## Tests

- [x] Hypothesis property tests: complete citations always pass; every required field removed always denied; malformed timestamps / inverted ranges / traversal paths / unknown fields always denied
- [x] Fail-closed test: malformed hook event → exit 1
- [x] End-to-end subprocess tests of `python3 src/evidence/validator.py`

## Demo

- [x] `demo/sample-repo/` — haunted codebase with planted anomalies (double-counted timeouts, swallowed capture errors, stale cache hits, unhandled rejections) + `logs/app.log` carrying matching signatures
- [x] `python tests/test_evidence_schema.py --demo-rejection` reproduces the interception scenario on demand

## Submission (pending, outside the repo)

- [ ] Record 2–3 min demo video: Exorcist spawn → uncited finding → hook block with STDERR reasons → evidence repair → forwarded report with file:line + log provenance
- [ ] Push to public GitHub repo (`Conly-Zy/ghost-in-the-audit`), MIT visible in About, attach YouTube demo link (public/unlisted, no password)
