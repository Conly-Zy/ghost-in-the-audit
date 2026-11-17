# Kiro Platform Notes (T1–T8)

Section 11 of the [design spec](../.kiro/specs/ghost-in-the-audit/design.md) defines the calibration checklist. Documented behavior comes from the official Kiro documentation (`kiro.dev/docs`, October and November 2026 updates) and the awslabs reference page with observed results. Live verification requires probe Hooks in Kiro IDE and Kiro CLI. For items marked `PENDING`, the toolkit relies on the documented contract and its implemented fallback behavior.

## Implementation updates to the design

1. **Hook files.** The toolkit uses `.kiro/hooks/ghost-pre-tool-use.json` and `.kiro/hooks/ghost-post-tool-use.json` with the v1 JSON schema. Kiro silently ignores the deprecated `.hook` format in the original design.
2. **Evidence_Recorder locking.** Design §2.3 describes `L_R` as shared. Strictly increasing timestamps (R4.5) and exactly-once, non-interleaved handling of overlaps (R4.8) require the read, compute, and write steps to be mutually exclusive. The critical section uses an exclusive lock. The order stays `L_G` then `L_R` to avoid deadlock. See `tests/test_components.py::TestRecorder::test_concurrent_recordings`.
3. **Run_Controller arguments.** Design §5.1 lists `run_controller.py <request_file>`. The implementation takes `<request_file> <payload_json>` because the body about to be written is in the Hook payload's `tool_input`.
4. **Append behavior.** `append_once` uses `O_CREAT` so records such as Gate_Live_Record and Rejection_Log can be created on demand (R2.3). The initial run directory does not include `gate-live.jsonl`. R1.8 requires Evidence_Log, Findings_Ledger, and Rejection_Log to start as empty files.
5. **Multiple active runs trigger `STATE_CORRUPT`.** If `scan_runs` finds corrupt state or more than one active run, it returns the affected paths. The caller fails closed under R8.10 and R15.6.
6. **`invoke_sub_agent` is classified as read.** Dispatch itself writes no files. Each subagent tool call triggers its own Hooks as verified in the IDE in T5. Without this catalog entry, the guard would block dispatch during an active run (R3). See T4.

## T1 Hook file format — documented, live verification pending

The toolkit implements the differences from the original design shown below.

| Item | Original design | Implemented format |
|---|---|---|
| Location and extension | `.kiro/hooks/*.hook` | `.kiro/hooks/*.json`. Any `.json` filename is accepted. Kiro silently ignores the deprecated `.kiro.hook` format. |
| File structure | `{"enabled","name","events":[{type,match,command,timeout_ms}]}` | `{"version":"v1","hooks":[{name,description?,trigger,matcher?,action:{type,command},timeout,enabled?,confirm?}]}` |
| Event names | `pre_tool_use` / `post_tool_use` (snake case) | `PreToolUse` / `PostToolUse` (PascalCase) |
| Matcher | `"*"` | `"*"` is a wildcard. Tool IDs, category tags (`read`, `write`, `shell`), `@mcp`, and regular expressions are also supported. |
| Timeout | `timeout_ms` in milliseconds | `timeout` in seconds, with a default of 60. `0` disables the timeout. The pre Hook uses 90 seconds and the post Hook uses 15. |
| Command action | Shell command | `action.type: "command"`. The command runs at the workspace root (T3). |

The documented timeout unit and configurable limit satisfy A16's requirement for a timeout of at least 60 seconds. Confirmation in the IDE and CLI is pending.

## T2 Hook payload — documented, live verification pending

- Kiro IDE 1.x writes JSON to stdin and then closes it. The snake-case field names are `session_id`, `hook_event_name`, `cwd`, `tool_name`, `tool_input`, and `tool_response`. Kiro IDE 0.12 used camelCase fields in the now-retired `USER_PROMPT` environment channel. The toolkit does not support that channel.
- **A11 (PreToolUse provides the tool name and arguments).** The fields are documented. The awslabs reference reports empty `tool_input` objects in some builds, including `{}` captured for write tools in `PostToolUse` on 1.0.165 and 1.0.242. Issue #763 reports that the field is populated starting in 1.0.309. Live verification is **PENDING**. An invalid or incomplete payload during an active run fails closed with `PAYLOAD_INVALID`. With no active run, the tool call is allowed (R15.5).
- **A12 (PostToolUse returns paths and line text for reads).** The documentation does not specify the read response format. The awslabs captures show free-form `tool_response` text for write and shell tools. Live verification is **PENDING** beyond the IDE observation in T5. An unrecognized response yields no evidence for that file (R4.3). If none of the reads produce evidence, all citations are rejected with `EVIDENCE_NOT_FOUND`, as expected under A12.
- The Hook_Launcher explicitly converts downstream failures it receives to exit code 2, which blocks a `PreToolUse` call. How each client behaves when the command fails to start or Kiro terminates it still needs live verification. [Hook trigger documentation](https://kiro.dev/docs/hooks/types/#pre-tool-use)
- The documentation says stdout from a successful Hook command enters the agent's context. The launcher and scripts keep stdout empty, so an allowed call adds nothing to that context.
- When the Hook payload supplies a `session_id`, the Evidence_Entry records it as `session` (R4.6). Otherwise the entry omits that field.

## T3 Hook working directory — documented, live verification pending

The documentation says command actions run at the workspace root. The launcher retains the payload's `cwd` as a fallback input for resolving the root. The current implementation uses `os.getcwd()` and returns `GATE_FAILURE` if `runs/`, `control/`, or `.kiro/scripts/ghostlib.py` is missing.

## T4 Tool_Catalog — documented, live verification pending

The table shows how the toolkit classifies built-in tools described in Kiro's tool documentation.

| Tool | Class | In catalog | Rationale |
|---|---|---|---|
| `read_file` / `read_files` | read | Yes | `read_files` is described as reading line ranges across multiple files. The official tool list and search summary differ, so the toolkit conservatively classifies it as read. |
| `list_directory` / `file_search` / `grep_search` | read | Yes | Directory listing, glob matching, and content search |
| `invoke_sub_agent` | read | Yes | Dispatch writes no files. Subagent tool calls trigger their own Hooks. Omitting dispatch from the catalog would block R3. |
| `introspect` / `disclose_context` | read | Yes | Document lookup and steering activation have no file side effects. |
| `code` (CLI) / `read_code` (IDE) | — | No | The descriptions include AST rewriting, so the toolkit cannot confirm read-only behavior. Calls during an active run are blocked with `UNDETERMINABLE`. |
| `fs_write` | write, op=create_or_modify | Yes | Takes `$.path` and content from `$.text` or `$.content`. The gate checks the current Inbox to distinguish creation from modification. |
| `fs_append` | write, op=modify | Yes | Takes `$.path` and shares content extraction with `fs_write`. The live argument format is pending confirmation. |
| `str_replace` | write, op=modify | Yes | Takes `$.path` and provides no full content. If the gate cannot obtain the content for a new file, it returns `UNDETERMINABLE` or `PAYLOAD_INVALID`. |
| `delete_file` | write, op=delete | Yes | Takes `$.path`. |
| `execute_bash` / `execute_pwsh` and IDE process tools | shell | No, by design | Blocked with `UNDETERMINABLE` during an active run (R11.3). |
| `remote_web_search` / `web_fetch` | web | No | Blocked with `UNDETERMINABLE` during an active run. |
| `createHook` / `kiro_powers` / semantic rename | — | No | Blocked with `UNDETERMINABLE` during an active run. Keeping rename tools out of the catalog is part of the protection. |

The IDE formats for `fs_write` and `read_file` are documented from observation in T5. Broader verification of live arguments and response formats is **PENDING**. The toolkit rejects writes it cannot classify and records evidence only when it can determine both the line numbers and returned content. The documentation distinguishes the CLI 2.x aliases `fs_read`, `write`, `glob`, `grep`, and `shell` from Hook and runtime IDs. The aliases are omitted from the catalog.

## T5 Agent restrictions (A13/A14) — Task 8.3 IDE probe

The probe ran on 2026-11-16 in a Kiro IDE session using the ACP client on macOS. A temporary probe marked `TEMP-A13-PROBE` appended records to `runs/.a13-probe/trace.jsonl` after `route_pre` and `route_post` in `hook_launcher.py` read stdin. A spec subagent made tool calls and used `invoke_sub_agent` to dispatch `cartographer` and `.kiro/agents/a14-probe.md`. The temporary agent used the Audit_Agent permission rules. The observations below come from the trace and tool-call results. The probe code, trace directory, and temporary agent were removed. There was no active audit during the probe, and it left no run directory.

| Test | Status | Evidence |
|---|---|---|
| **A13.** Subagent tool calls trigger the workspace `PreToolUse` and `PostToolUse` Hooks. | **VERIFIED** | Calls to `read_file`, `fs_write`, `list_directory`, and `grep_search` at the two subagent levels each produced a `pre` and `post` trace record. The dispatch paths were the spec subagent to `cartographer` and `a14-probe` to `subagent_cartographer`. The real toolkit Hook also blocked subagent `fs_write` calls with `RUN_NOT_ACTIVE` for `runs/<id>/inbox/` and `REQUEST_INVALID` for `control/probe.txt`, confirming that exit code 2 blocks subagent calls. |
| Frontmatter fields `name`, `description`, `tools`, and `permissions.rules` with `capability`, `match`, `exclude`, and `effect` | **VERIFIED** | The probe agent loaded with these fields. Errors attributed the rule to `Source: subagent-profile`. |
| The `tools: ["read","write"]` allowlist excludes shell and web. | **VERIFIED** with extra tools | `cartographer` reported no shell or web tools. Kiro also supplies `read_code`, `create_artifact`, `load_resource`, `report_progress`, and `user_input`. Write_Guard fails closed with `UNDETERMINABLE` for these uncataloged tools during an active run. The platform's `user_input` tool can ask the user questions, which deviates from R11.4. See the open items below. |
| The **original** agent definition with `tools` alone restricts file paths. | **FAILED** | `cartographer` read the root `README.md` and wrote `runs/.a13-probe/a14-write.txt` outside the Inbox. Those path restrictions existed only in the prompt, so Kiro did not enforce them. |
| Writable roots (R11.5/R11.6) with `fs_write` deny `**` and exclusions | **VERIFIED** | Kiro blocked `a14-probe` from writing to `runs/.a13-probe/…`, `README-probe.md`, and `.kiro/steering/probe.md` without prompting. It returned `Blocked by a permission rule (deny fs_write matching "**"); the user did not reject it. Source: subagent-profile.` Writes to `runs/<id>/inbox/probe.json` and `control/probe.txt` passed the permission layer and reached the toolkit Hook. |
| Readable roots (R11.7) with `fs_read` deny `**` and exclusions for `targets/**` and `runs/**` | **VERIFIED** with a search limitation | `read_file README.md` and `list_directory .kiro` were blocked without prompting (`deny fs_read matching "**"`). `read_file runs/…`, `read_file targets/.gitkeep`, and `list_directory targets` succeeded. Any `fs_read` deny rule also blocks `grep_search` and `file_search`, even when scoped to `targets/**`. This matches the aws-samples `permissions.md` observation. The audit agents therefore use `read_file` and `list_directory` to inspect code. |
| **A14.** Out-of-scope calls fail without prompting. | **VERIFIED** with deny rules | All denied calls above returned an error without a prompt. The documentation describes `deny` as an unconditional block and says it overrides conflicting policy inherited from the parent session. |
| Subagent allowlist (R11.8) with `subagent/<name>` in `tools` | **VERIFIED** | With `tools: [..., "subagent/cartographer"]`, the probe received only the `subagent_cartographer` dispatch tool. It had no tools for `security-auditor` or for built-in or general-purpose subagents. Calling `cartographer` succeeded without prompting. The `capability: subagent` allow rule skips only the launch prompt. The documentation says `@builtin` or `*` in `tools` disables this restriction. All five definitions omit both entries. |
| Can the Hook payload identify the subagent? | Observation affecting A13 | Subagent calls shared the parent's `session_id` and had no agent-name field. The requirements treat the `agent` field as self-reported. The toolkit does not use `session_id` to distinguish agents. |

### Configuration changes recorded in Task 8.3

- All five `.kiro/agents/*.md` definitions gained `permissions.rules`. Audit_Agent write rules deny `**` with an exclusion for `runs/*/inbox/**`. Orchestrator also excludes `control/**`. All five read rules deny `**` with exclusions for `targets/**` and `runs/**`.
- To satisfy R11.8, Orchestrator moved from the unrestricted `invoke_sub_agent` to `subagent/cartographer`, `subagent/security-auditor`, `subagent/dependency-auditor`, and `subagent/quality-auditor`. A `capability: subagent` allow rule matching those four names skips the launch prompt.
- `tool_catalog.json` gained the four observed `subagent_<name>` dispatch IDs in the read class. Without those entries, the guard returns `UNDETERMINABLE` for Orchestrator dispatch during an active run.
- `dev/sync_evidence_rules.py --check` confirmed that the five existing inline Evidence_Rules copies still match. The full suite recorded 136 passing tests during this task.

### Open items

1. **The Orchestrator profile itself is pending live verification.** `a14-probe` used the same `subagent/<name>` syntax. The probe session could not switch top-level agents, so it did not test the Orchestrator profile itself. In a fresh session, select `orchestrator` and ask it to write `README-probe.md`, read `README.md`, and invoke a subagent outside its four-agent allowlist. Each call should return `Blocked by a permission rule … Source: …` or have no corresponding tool. A request to write `control/start.json` should reach Run_Controller, which can return a code such as `TARGET_INVALID`.
2. **Kiro CLI verification is pending.** The results above are from an IDE session using ACP. Repeat the checks in CLI V3 under task 9.2.
3. **The `fs_read` deny rule also blocks `grep_search` and `file_search`.** Restoring search would require removing that rule, leaving R11.7 enforced by the prompt alone. The current configuration enforces R11.7 at the platform level. The trade-off remains open for the user to decide.
4. **Kiro supplies tools beyond the allowlist.** `tools` cannot remove `user_input`, `create_artifact`, `load_resource`, `report_progress`, or `read_code`. Whether `excludedTools` removes them is untested. Write_Guard returns `UNDETERMINABLE` for these tools during an active run. Blocking `report_progress` does not affect the audit. Whether `user_input` can still open a prompt when blocked by the Hook is unverified.

### T2 and T4 payload observations

- IDE `fs_write` supplies the content in `text`. Control requests and Citation_Gate also accept `content` and reject conflicting string values. `fs_append` shares the extraction logic, and its live arguments remain unverified.
- `read_file` returns `tool_response` as a string in the form `<file name="…" language="…" >\n<content>\n…\n</content>\n</file>`, sometimes followed by `Note: To continue, use offset N.` The returned text has no line numbers. Evidence_Recorder extracts the file text inside the tags and uses `tool_input.offset + 1` as the first line number. It records no evidence for an unknown format or invalid offset.
- `list_directory` and `execute_bash` also return plain-text `tool_response` strings. The IDE probe verified the T2 field names `session_id`, `hook_event_name`, `cwd`, `tool_name`, `tool_input`, and `tool_response`.

## T6 Steering loading (A15) — documented, live verification pending

- Steering supports `inclusion: manual` in frontmatter.
- The documentation describes automatic loading of `AGENTS.md` files in subdirectories as steering. R1.5's `AUTO_LOADED_CONTEXT_PRESENT` check covers this behavior.
- The documentation says custom agents need `resources: ["file://.kiro/steering/**/*.md"]` to load steering automatically. Design §6.2 instead embeds the full Evidence_Rules in all five definitions and uses the sync script under `dev/` to keep them aligned.
- `disclose_context` can load steering manually, but there is no guarantee it will run before the first tool call. The toolkit keeps the rules inline.

## T7 Performance budgets — met in integration tests

Measurements were taken locally on macOS with Python 3.14 on 2026-11-16.

| Scenario | Budget | Measured | Test |
|---|---|---|---|
| Pre and post Hooks for an unrelated call with 1,000 run directories | ≤ 1 s (R15.4/A23) | About 0.5–0.6 s | `tests/test_hooks.py::TestPerformanceBudget` |
| Full Citation_Gate validation with 10,000 Evidence_Log entries | ≤ 15 s (design §9) | About 0.37 s | `tests/test_perf.py` |
| Start scans a target tree with roughly 10,000 files, checking for auto-loaded context and path escapes | ≤ 20 s (design §9) | About 0.07 s | `tests/test_perf.py` |

All three measurements were within budget, with the most headroom in citation validation and the Start scan. The measurements did not call for early-exit optimization.

## Live verification checklist for task 9.2

1. Capture the probe Hook's stdin payload and compare the T2 fields and each tool's `tool_input` and `tool_response` formats.
2. Confirm `timeout: 90` takes effect (A16) and verify when exit code 2 blocks a tool call.
3. Verify that subagent tool calls reach both Hooks (A13). **VERIFIED** in the IDE under task 8.3. CLI confirmation is pending.
4. Verify that out-of-scope writes fail without a prompt (A14). **VERIFIED** with IDE deny rules under task 8.3. The Orchestrator profile and CLI confirmation are pending. See T5.
5. Check the live argument formats of `fs_append` and other tools, as well as CLI read responses.

## T8 / 9.2 Live End-to-End Verification — PENDING

The sample at `dev/e2e-sample-target/` is copied to `targets/pinger-ide` and `targets/pinger-cli` for separate live runs. The report verifier is `python3 dev/verify_e2e_report.py "runs/<run_id>" --require-all-dispatched`. `tests/test_verify_e2e.py` exercises the verifier with reports generated by a dry run. The original passes and the tampered report fails. [End-to-end runbook](e2e-runbook.md)

Before the live runs, run `python3 -m unittest tests.test_observed_payload.TestObservedPayloadEndToEnd -v`. The tested replay simulates tool calls in the observed IDE payload format and covers Start, Canary rejection, evidence from reads, citation acceptance, and Finalize. Enter the live client results below.

| Item | Kiro IDE | Kiro CLI |
|---|---|---|
| Status | PENDING | PENDING |
| Date | | |
| Kiro version | | |
| Operating system / Python version | | |
| Target name | `pinger-ide` | `pinger-cli` |
| `run_id` | | |
| Final state (`completed` or `aborted`) | | |
| Stages 1–3 — Start, confirmation, and Canary (`CITATION_MISSING` and `gate-live.jsonl`) | | |
| Stage 4 — `evidence.jsonl` entry count after Cartographer | | |
| Stage 5 — Completion_Status for each of the three auditors | | |
| `findings.jsonl` entry count / rejected submissions / invalidated findings | | |
| Stage 7 — Whether the paths and counts reported by Orchestrator match `report.json` | | |
| Verifier `RESULT` line | | |
| Verifier `FAIL` items, if any | | |
| Unexpected Reason_Code values or deviations | | |
| Outcome (`PASS` or `FAIL`) | PENDING | PENDING |

Paste the complete verifier output for each run.

```text
IDE output from python3 dev/verify_e2e_report.py runs/<run_id> --require-all-dispatched
```

```text
CLI output from python3 dev/verify_e2e_report.py runs/<run_id> --require-all-dispatched
```
