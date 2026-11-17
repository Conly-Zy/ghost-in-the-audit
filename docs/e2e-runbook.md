# End-to-End Runbook (Task 9.2, R13, Design D2.2)

Run one full audit in Kiro IDE and one in Kiro CLI. Each run starts with placing a target under `targets/` and ends with checking `report.md` and `report.json` against the audit records using `dev/verify_e2e_report.py`. Both live runs are pending. Record the results in [T8 / 9.2 Live End-to-End Verification](platform-notes.md#t8--92-live-end-to-end-verification--pending).

Run the shell commands below in your own terminal from the workspace root (the repository root). During an audit, Write_Guard intercepts agent tool calls only. Your terminal stays available (A10).

## 0. Replay the observed IDE payloads

Run the regression that replays the payload formats observed by the IDE probe before starting a live session.

```bash
python3 -m unittest tests.test_observed_payload.TestObservedPayloadEndToEnd -v
```

| # | Observed IDE behavior | How the toolkit handles it |
|---|---|---|
| B1 | `fs_write` uses `text` for the content. | Control requests and the Inbox gate accept `text` and also support `content`. They reject conflicting values. `fs_append` uses the same extraction logic, and its live payload format is still pending confirmation. |
| B2 | `read_file` returns a string wrapped in `<file>` and `<content>` tags. The offset is zero-based. | The recorder removes the wrapper, uses `offset + 1` as the first line number, and checks the returned text against the file on disk line by line. It records no evidence if it cannot parse the response. |

The replay has tested the Hook scripts and report verification using simulated tool calls. This runbook tracks live IDE and CLI verification separately.

## 1. Prepare the workspace once for both clients

```bash
python3 --version                                   # Requires Python 3.9 or newer
python3 -m unittest discover -s tests -t .          # All tests should pass
python3 dev/sync_evidence_rules.py --check          # All five Evidence_Rules copies should match
python3 dev/check_workspace.py                     # Expect a healthy workspace message and exit code 0
```

If `check_workspace.py` reports leftover `.tmp-*` directories, run `python3 dev/check_workspace.py --purge-tmp`. If it reports a missing `Run_State_Record` or multiple active runs, resolve the issue manually before continuing. Otherwise, Start is rejected with `STATE_CORRUPT`.

Confirm that there is no active audit run.

```bash
grep -l '"status":"active"' runs/*/state.json 2>/dev/null || echo "no active run"
```

Make a separate copy of the sample target for each client so you can tell the two runs apart.

```bash
cp -R dev/e2e-sample-target targets/pinger-ide
cp -R dev/e2e-sample-target targets/pinger-cli
```

The sample passes the Start checks. It has seven regular UTF-8 files, no `AGENTS.md` regardless of capitalization, no `.kiro` directory, and no symlinks. The dry run in `tests/test_verify_e2e.py` successfully starts an audit with this sample. The table lists the deliberate issues for manual comparison. Agents do not need to find them all.

| Area | Location | What to look for |
|---|---|---|
| Architecture | `pinger/main.py:15-16` and `pinger/main.py:7-12` | CLI entry point and main flow, respectively |
| Security | Lines 1–2 of `pinger/config.py` | Hard-coded password and token, both sample values |
| Security | `pinger/ops.py:12` | The host is concatenated into an `os.system` command, creating a command injection risk. |
| Dependency | `requirements.txt:1` | `requests==2.19.0` pins an old version. |
| Dependency | `requirements.txt:2-3` | `pyyaml>=3.10` sets only a minimum version, and `flask` is unpinned. |
| Quality | `pinger/ops.py:13-14` | `except Exception: pass` swallows exceptions. |
| Quality | `pinger/ops.py:5-7` | The code opens a file and leaves it unclosed. |
| Quality | `tests/ops_check.py` | `ping_host` has no test, and the test runner does not discover this filename. |

**Keep `targets/pinger-*` unchanged from Finalize through report verification.** The verifier uses the current target files to recheck findings and recalculate coverage, so changes can produce discrepancies.

## 2. Run in Kiro IDE

1. Open the repository root directly as the Kiro IDE workspace.
2. Record the IDE version from the About menu or `kiro --version`.
3. Start a fresh session. Kiro loads Hooks at session startup, so sessions opened before `.kiro/` was in place or updated will miss those changes.
4. Select `orchestrator` in the agent picker.
5. Send this request exactly as written.

   ```text
   Audit the pinger-ide directory under targets/.
   ```

6. Follow Section 4 until Orchestrator reports the paths to `report.md` and `report.json`.
7. Record the run ID from Orchestrator's first reply. You can also check `ls -t runs | head -1`.
8. Verify the report as described in Section 5.

## 3. Run in Kiro CLI

Wait until the IDE run is `completed` or `aborted`. The workspace supports one active audit run at a time.

1. Start a CLI session from the workspace root and select the `orchestrator` agent. Check `--help` for your CLI version. A typical invocation is shown below.

   ```bash
   cd /path/to/ghost-in-the-audit
   kiro-cli --version
   kiro-cli chat --agent orchestrator
   ```

2. Send the equivalent request for the CLI target.

   ```text
   Audit the pinger-cli directory under targets/.
   ```

3. Follow the same stages as in the IDE, record the run ID, and verify the report as described in Section 5.
4. If Orchestrator reports an empty Evidence_Log after Cartographer and aborts, compare `wc -l runs/<run_id>/evidence.jsonl` with the tool-call history. Check whether subagent reads reached the Hook (A13) and whether the CLI response matched the supported read format (B2). Record what you confirmed.

## 4. Expected behavior at each stage (Design D2.2)

Replace `<id>` and `<run_id>` with the actual run ID. For the terminal checks that use `$R`, set `R='runs/<run_id>'` with that same ID. The first two rows show `pinger-ide`. Expect `pinger-cli` for the CLI run.

| # | Stage | What Orchestrator should do | Terminal check |
|---|---|---|---|
| 1 | Start | Successfully write `{"target":"pinger-ide"}` to `control/start.json`. | `cat control/start.json` |
| 2 | Confirm run creation (R1.12) | Read `runs/*/state.json` and report the run ID and run directory. | `cat $R/state.json` shows `"status":"active"` and `"target":"pinger-ide"`. `ls $R` shows `evidence.jsonl`, `findings.jsonl`, `rejections.jsonl`, `inbox/`, and `state.json`. |
| 3 | Check that the gate is active (R2.1–R2.3) | Attempt to write `$R/inbox/gate-canary.probe`. The write is blocked with `CITATION_MISSING` and no System_Reason_Code. Orchestrator then reads the gate-live record. | `cat $R/gate-live.jsonl` shows one line with `run_id`, `at`, and `sha256`. `cat $R/rejections.jsonl` shows one line with `"reason_codes":["CITATION_MISSING"]` and the same `sha256`. `ls $R/inbox` shows an empty directory. |
| 4 | Cartographer (R3.2, R2.6) | Dispatch `cartographer`, receive a five-line Completion_Summary, then read `evidence.jsonl` and confirm it contains evidence. | `wc -l $R/evidence.jsonl` shows a count above zero. `ls $R/inbox` shows accepted `*.json` files. |
| 5 | Three auditors (R3.3) | Dispatch `security-auditor`, `dependency-auditor`, and `quality-auditor` in sequence or in parallel. Each returns a five-line Completion_Summary. | `cat $R/findings.jsonl` shows one `F-00000n` finding per line. `wc -l $R/rejections.jsonl` counts rejected writes. Agents can correct and resubmit rejected submissions. |
| 6 | Finalize (R3.10, R13.15) | Successfully write `control/finalize.json`. `completion` lists all four agents as `completed` or `failed`, and `summary_statements` cites only `F-######` finding IDs. | `cat control/finalize.json` shows the request. `cat $R/state.json` shows `"status":"completed"` and `finished_at`. |
| 7 | Report (R13.16) | Report the paths to `report.md` and `report.json` under `runs/<id>/`, the counts by severity, and the number of invalidated findings. | `ls $R/report.md $R/report.json` lists both files. The counts match the statistics section of the report. |

Record any reply that includes an auditor's free-form narrative, the title or description of a rejected submission, or a conclusion outside the findings ledger (R3.7, R3.13).

## 5. Verify each completed run

```bash
python3 dev/verify_e2e_report.py "runs/<run_id>" --require-all-dispatched
```

The verifier recomputes the expected report from the run artifacts and prints `PASS` or `FAIL` for each check. It ends with `RESULT: PASS` and exit code 0 if every check passes, or `RESULT: FAIL` and exit code 1 if any check fails. It checks the following.

- `state.json` is `completed`, and its `run_id`, `target`, `started_at`, and `finished_at` match the report.
- `gate-live.jsonl` contains the run's Canary record, and the corresponding rejection lists only `CITATION_MISSING`.
- The findings match the ledger records that pass revalidation, in the same order. The invalidated list matches the records that fail revalidation, including the Reason_Code and citation index or field path. Each finding's Canonical_Form matches the ledger byte for byte.
- Severity order is correct. The five buckets each for `by_severity` and `by_category`, the invalidated count, and the rejection statistics match the recomputed values, with the Canary excluded from the rejection statistics by its `sha256`. The statistics lines in `report.md` match as well.
- Coverage matches the recomputed data.
- All four agent statuses are present and match `control/finalize.json`. The accepted statuses are `completed`, `failed`, and `not_dispatched`. `--require-all-dispatched` disallows the last of these.
- Each citation has a valid Location and Evidence_Timestamp. The entry at that timestamp in `evidence.jsonl` covers the Location.
- Finding titles, citation lines, and the invalidated list in `report.md` match `report.json`. The evidence scope statement and the caveat for an empty findings list meet R13.20 and R13.9.

Save the full output and paste it into the results record.

```bash
python3 dev/verify_e2e_report.py "runs/<run_id>" --require-all-dispatched | tee "/tmp/verify-<run_id>.txt"
```

Confirm these two items manually.

1. The severity and invalidated counts in Orchestrator's stage 7 reply match `statistics.by_severity` and `statistics.invalidated_count` in `report.json`.
2. Spot-check two findings in `report.md`. A citation line looks like `` `pinger/ops.py:12` @ 2026-…Z ``. The fenced excerpt should match the cited line in the target file.

## 6. Record the results

Record the client, Kiro version, date, run ID, target name, final state, verifier `RESULT` line, any `FAIL` items, and any deviations by stage in the [live verification table](platform-notes.md#t8--92-live-end-to-end-verification--pending). Task 9.2 passes only after both runs return `RESULT: PASS` and both pass the manual checks in Section 5. Then mark task 9.2 complete in [tasks.md](../.kiro/specs/ghost-in-the-audit/tasks.md).

## 7. Troubleshooting by Reason_Code

| Symptom or Reason_Code | Stage | Meaning and response |
|---|---|---|
| `start.json` writes successfully, but Orchestrator cannot confirm run creation | 2 | The Hook did not load in this session (R1.15). Start a fresh session. Check that `.kiro/hooks/ghost-pre-tool-use.json` exists and that `python3` is on Kiro's `PATH`. |
| `TARGET_INVALID` | 1 | The directory name is wrong or `targets/<name>` is a symlink. Check `ls -la targets/`. Names are case-sensitive. |
| `TARGET_EMPTY` | 1 | The target has no regular files. Copy the sample again with `cp -R`. |
| `AUTO_LOADED_CONTEXT_PRESENT` | 1 | The target has an `AGENTS.md` (any capitalization) or a `.kiro` directory. The sample should have neither. Rename or remove the entry, then start a fresh session. |
| `SYMLINK_ESCAPE` | 1 | A symlink points outside the target or forms a loop. Check with `find targets/<name> -type l`. |
| `RUN_ALREADY_ACTIVE` | 1 | A run is already active. The message includes its run ID. Say `Abort the audit.` in the session or confirm the previous run has ended. |
| `STATE_CORRUPT` | Any | A `runs/*/state.json` file is missing or corrupt, or more than one run is active. The message includes the affected paths. Run `python3 dev/check_workspace.py`. Back up any corrupt run directory before moving it out of `runs/` manually. |
| `REQUEST_INVALID` | 1, 6 | Orchestrator wrote a control file with a name that is not reserved. Record the deviation. |
| The Canary write succeeds | 3 | Citation_Gate is inactive and Orchestrator will abort (R2.4). Start a fresh session and check that the Hook loaded. |
| `PAYLOAD_INVALID` or `UNDETERMINABLE` on a Canary or submission | 3, 4, 5 | The Hook input is missing arguments, the tool is absent from Tool_Catalog, or the write content cannot be extracted. Record the tool name and actual argument format and compare with B1. |
| `GATE_FAILURE` | Any | A validation script crashed or returned an unexpected exit code. The message names the script. If the same payload does not reproduce the failure in your terminal, record the exact message. |
| `TIMEOUT` | Any | A script exceeded its deadline. Record the stage and target size. The sample is small enough that a timeout is unexpected. |
| No entries in Evidence_Log after Cartographer | 4 | Check the read response format (B2) and whether subagent read calls reach the Hook (A13). Orchestrator will abort. |
| Many `EVIDENCE_NOT_FOUND` or `EVIDENCE_MISMATCH` errors | 5 | The agent may have used the wrong timestamp or path. It can normally correct a finding up to twice. If every citation fails, check whether the paths and line ranges in `evidence.jsonl` match the actual reads (B2). |
| `CONTENT_CHANGED` or a nonempty invalidated list | 5, 7 | The target changed after the submission was accepted. Keep the target unchanged during the live run. |
| `PROTECTED_PATH` | Any | An agent tried to write to a protected path in the target, `.kiro/`, or the run artifacts. Record the agent and path. |
| `GATE_NOT_VERIFIED` | 6 | `gate-live.jsonl` is missing. Stage 3 did not pass and Orchestrator will abort. |
| `RECORDER_NOT_VERIFIED` | 6 | `evidence.jsonl` is empty. See B2 and A13. |
| `SUMMARY_UNSUPPORTED` | 6 | A summary statement omits a `F-######` citation, cites an unknown ID, or names a `file:line` outside the cited evidence. This code also covers exceeding the limits of 20 statements or 500 code points. The run stays active, and Orchestrator can correct and resubmit. |
| `REPORT_FAILED` | 6 | Report_Renderer could not read or write a file, or took more than 40 seconds. The run stays active. Record the message and have Orchestrator retry Finalize. |
| `RUN_NOT_ACTIVE` | 5, 6 | The run has ended or the run ID is wrong. If this occurs during an audit, record it as a deviation. |
| The verifier returns `FAIL` | Report verification | Use the `FAIL` lines to locate the discrepancy. A mismatch may indicate a renderer defect or a change to the target or artifacts after Finalize. Keep `runs/<id>/` intact. |

To stop a run, say `Abort the audit.` in the Orchestrator session. Orchestrator writes `control/abort.json` and the state changes to `aborted`. Aborted runs produce no report and require no report verification.
