# Design Document: ghost-in-the-audit

This design addresses all of `requirements.md`, including details that document delegates to the design. Terms follow the Requirements Document Glossary unless stated otherwise. A reference such as `7#20` means requirement 7, acceptance criterion 20.

---

## 1. Overview

ghost-in-the-audit (Toolkit) is native Kiro configuration with five custom agents, two Hooks, one steering source file, and six validation scripts started through Hooks. Toolkit contains no LLM orchestration code. The Hook pipeline enforces fail-closed validation, evidence checking, and write protection.

1. **Exit code 2 is the only blocking channel.** Each rejection exits 2 and puts the Reason_Code on stderr. Hook_Launcher converts other failure paths to this form (see platform constraints in the Introduction).
2. **Fail closed explicitly.** Exceptions, timeouts, and indeterminate checks produce Blocking_Signal. Allow a validated operation only after all checks pass and the write succeeds (requirement 8).
3. **Single writer plus locks.** Each Run_Artifact has exactly one writer component. Components coordinate using `flock` on directory or file descriptors with a fixed lock order to prevent deadlocks.
4. **Minimal platform dependencies.** Use documented and observed Kiro features for Hook stdin payloads, exit code 2, custom agent tool limits, and steering loading. Each assumption has a fallback in section 11.

Use Python 3.9+ and its standard library only (`json`, `hashlib`, `fcntl`, `os`, `re`, `datetime`, `subprocess`, `time`). No network access, target code execution, or third-party dependencies.

---

## 2. Architecture

### 2.1 Component diagram

```text
User ──audit request──▶ Orchestrator ──subagent──▶ Cartographer / Security_Auditor /
                         │                        Dependency_Auditor / Quality_Auditor
                         │                                         │
                  writes control/*.json                 write runs/<id>/inbox/*.json
                         └──────────────────┬──────────────────────┘
                                            ▼
                     PreToolUse Hook (every tool call)
                     Hook_Launcher (hook_launcher.py pre)
                       1. Scan runs/ for an active Audit_Run
                       2. Route to at most one script
                          ├─▶ Run_Controller (run_controller.py)
                          ├─▶ Citation_Gate (citation_gate.py)
                          └─▶ Write_Guard (write_guard.py)
                     Rejection_Message returns on stderr with exit code 2

                     PostToolUse Hook (read tool results)
                     Hook_Launcher (post) ──▶ Evidence_Recorder (evidence_recorder.py)

On successful Finalize, Run_Controller invokes Report_Renderer to generate report.md / report.json.
```

### 2.2 Audit lifecycle

```text
User request ─▶ Orchestrator writes control/start.json
             ─▶ Run_Controller validates and creates runs/<run_id>/ with empty Ledger/Log/Inbox and state.json
             ─▶ Orchestrator reads state.json to confirm creation (1#12)
             ─▶ Orchestrator writes inbox/gate-canary.probe (Canary_Submission)
             ─▶ Citation_Gate rejects with CITATION_MISSING and writes gate-live.jsonl to confirm liveness
             ─▶ Dispatch Cartographer, then verify Evidence_Log is nonempty (2#6)
             ─▶ Dispatch Security / Dependency / Quality
             │    PostToolUse records each read in evidence.jsonl.
             │    Citation_Gate checks each Submission, appending accepted findings to findings.jsonl
             │    or returning a rejection to the submitter.
             ─▶ Orchestrator collects Completion_Summary results and writes control/finalize.json
             ─▶ Run_Controller revalidates, Report_Renderer generates reports, state.json becomes completed
             ─▶ Orchestrator tells the User the report.md / report.json paths and statistics
If any stage stalls, User requests abort ─▶ control/abort.json ─▶ state.json becomes aborted.
```

### 2.3 Concurrency and locking

| Lock | Descriptor | Holders | Purpose |
|---|---|---|---|
| `L_G`, global run registry lock | Directory fd for `runs/` (open with `O_RDONLY`, then flock) | Hook_Launcher shared during scans, Run_Controller exclusive during Start creation | Detect active runs and enforce one active run (1#7) |
| `L_R`, per-run lock | Directory fd for `runs/<run_id>/` | Evidence_Recorder shared during Evidence_Log read-modify-write, Citation_Gate shared for log reads and exclusive for ledger appends, Run_Controller exclusive throughout Finalize/Abort | Record atomicity, unique Finding_ID, and serialized Finalize (14#8) |

- Always acquire `L_G` before `L_R` when both are needed. Never reverse the order.
- Write Run_Artifacts under `flock(LOCK_EX)` using a **single `write()` to append full lines** for JSONL or **a temporary file and atomic `os.replace`** for `state.json` and `report.*`. A crash leaves only a complete prefix or a complete previous version (4#7, 10#6, 8#11).
- Readers of Run_Artifacts (Citation_Gate and Run_Controller) read each whole file under a shared lock to avoid torn reads. An unparseable last line yields STATE_CORRUPT (8#7).
- Lock waits count against the script's internal deadline. Fail closed on timeout as specified in section 9.

---

## 3. Workspace layout and data formats

### 3.1 Repository and workspace layout

Install Toolkit at the Kiro workspace root.

```text
<workspace>/                           (Kiro workspace root and Toolkit location)
├── .kiro/
│   ├── agents/
│   │   ├── orchestrator.md
│   │   ├── cartographer.md
│   │   ├── security-auditor.md
│   │   ├── dependency-auditor.md
│   │   └── quality-auditor.md
│   ├── hooks/
│   │   ├── ghost-pre-tool-use.hook     (PreToolUse → Hook_Launcher pre, 90s timeout)
│   │   └── ghost-post-tool-use.hook    (PostToolUse → Hook_Launcher post, 15s timeout)
│   ├── steering/
│   │   └── evidence-rules.md           (normative source for Evidence_Rules, inclusion: manual)
│   └── scripts/
│       ├── hook_launcher.py            (Hook_Launcher, entry points `pre` | `post`)
│       ├── run_controller.py           (Run_Controller)
│       ├── citation_gate.py            (Citation_Gate)
│       ├── write_guard.py              (Write_Guard)
│       ├── evidence_recorder.py        (Evidence_Recorder)
│       ├── report_renderer.py          (Report_Renderer, imported by run_controller)
│       ├── ghostlib.py                 (shared parsing, hashing, locks, timestamps, and message templates)
│       └── tool_catalog.json           (Tool_Catalog)
├── targets/                            (Targets_Directory, where User places codebases)
│   └── .gitkeep
├── runs/                               (Runs_Directory)
│   ├── .gitkeep
│   ├── run-<epoch_ms>-<6hex>/           (Run_Directory named for the Audit_Run identifier)
│   │   ├── state.json                  (Run_State_Record)
│   │   ├── evidence.jsonl              (Evidence_Log)
│   │   ├── findings.jsonl              (Findings_Ledger)
│   │   ├── rejections.jsonl            (Rejection_Log)
│   │   ├── gate-live.jsonl             (Gate_Live_Record)
│   │   ├── inbox/                      (Submission_Inbox)
│   │   ├── report.md                   (Audit_Report, completed runs only)
│   │   └── report.json
│   └── .tmp-<uuid>/                    (Start staging directory; scans ignore all names starting with `.`)
└── control/                            (Control_Directory)
    ├── .gitkeep
    ├── start.json / finalize.json / abort.json   (three reserved filenames)
```

- Runs_Directory scans by Hook_Launcher, Run_Controller, and Citation_Gate treat only direct subdirectories without a leading `.` as Run_Directory. `.tmp-*` and `.gitkeep` never count. Leftover staging directories cannot trigger STATE_CORRUPT, and a failed Start leaves no Run_Directory without a Run_State_Record. See the creation protocol in 5.1 and “Scope and limitations” in the Introduction.
- Consider adding `runs/`, `targets/`, and `control/` to `.gitignore`.
- Consider excluding `targets/` in global `.gitignore` and in Claude Code / Kiro context settings to keep target content out of agent context.

### 3.2 Identifiers and reserved names

| Name | Specification | Basis |
|---|---|---|
| Audit_Run identifier (Run ID) | `run-<13-digit epoch milliseconds>-<6 random lowercase hex digits>`, e.g. `run-1731690000123-a1b2c3`. Regex `^run-[0-9]{13}-[0-9a-f]{6}$`. Use it as the directory name. Check collisions under `L_G` and regenerate as needed, including against historical runs (1#7). | Globally unique, sortable, and path-safe |
| Reserved Control_Request filenames | `start.json`, `finalize.json`, `abort.json` | Requirements 1, 13, 14 and A24 |
| Canary_File_Name | `gate-canary.probe`, outside Submission_File_Name because its extension is different from `.json` | Requirement 2, 10#9, Glossary |
| Submission_File_Name | `^[a-z0-9][a-z0-9-]{0,63}\.json$` | 10#9 |
| Finding_ID | `F-` followed by a zero-padded six-digit decimal sequence number, e.g. `F-000001`. The sequence is the Accepted_Finding's append position n (10#7). Fail closed with INTERNAL_ERROR if n > 999999. | Glossary |
| Citation JSON keys | `location`, `evidence_timestamp`, `excerpt`. Glossary uses conceptual names. Evidence_Rules and Submission_Schema share these snake_case keys. | Requirement 5 |
| Finding_ID reference in Summary_Statement | Literal `F-######` token (regex `F-[0-9]{6}`) | 13#11 |

### 3.3 Data schemas

Parse strictly. Unknown keys cause parsing failure (STATE_CORRUPT / INTERNAL_ERROR).

**Run_State_Record `state.json`** is written exclusively by Run_Controller using a temporary file and `os.replace`.

```json
{"run_id":"…","target":"…","focus":"…?","status":"active|completed|aborted",
 "started_at":"…Z","finished_at":"…Z?"}
```

Fixed key order is `run_id, target, focus?, status, started_at, finished_at?`. Include `focus` only when Start has Audit_Focus and `finished_at` only when completed or aborted.

**Evidence_Entry** is one JSON object per line in `evidence.jsonl`, appended exclusively by Evidence_Recorder.

```json
{"ts":"…Z","run_id":"…","path":"src/a.py","start":10,"end":20,"sha256":"…64hex","session":"…?"}
```

Key order is fixed. Include `session` only when provided by Hook input (4#6). `start` and `end` must be integers from 1 to 2147483647, excluding booleans, with start ≤ end (4#11).

**Findings_Ledger records** are appended exclusively by Citation_Gate, one per line in `findings.jsonl`.

```json
{"finding_id":"F-000001","accepted_at":"…Z","submission_file":"auth-bypass.json",
 "session":"…?","canonical":{…Canonical_Form object…}}
```

`canonical` embeds the Submission_Parser result as a JSON object using section 3.5 serialization rules. For duplicates, run Canonical_Serializer again and compare byte for byte, relying on idempotence (5#10).

**Rejection_Record** is appended exclusively by Citation_Gate, one per line in `rejections.jsonl`.

```json
{"rejected_at":"…Z","reason_codes":["EVIDENCE_MISMATCH"],"agent":"Security_Auditor|unknown","sha256":"…64hex"}
```

`reason_codes` lists every failed check's Reason_Code once (9#5).

**Gate_Live_Record** is appended exclusively by Citation_Gate, one per line in `gate-live.jsonl`.

```json
{"run_id":"…","at":"…Z","sha256":"…64hex"}
```

Append one line per Canary_Submission satisfying 2#3.

**Control_Request bodies**

```json
start.json    : {"target":"<direct child directory name under Targets_Directory>","focus":"…?"}
finalize.json : {"run_id":"…","completion":{"Cartographer":"completed|failed", …dispatched agents only…},
                 "summary_statements":["…"?]}
abort.json    : {"run_id":"…"}
```

Schema validation follows O1. Check defects with dedicated Reason_Codes: missing or unreadable `target`/`run_id` (TARGET_INVALID / RUN_NOT_ACTIVE) and filename and location (REQUEST_INVALID, 11#13). Keep other defects, such as unknown fields or invalid completion values, outside REQUEST_INVALID. Run_Controller fails closed with exit 2 and `INTERNAL_ERROR` for those defects. This gives a direct failure instead of O1's abnormal termination followed by GATE_FAILURE, with no new semantics.

**Predefined Canary_Submission** values for 2#1

```json
{"run_id":"<Audit_Run identifier>","agent":"Cartographer","title":"ghost canary gate probe",
 "category":"architecture","severity":"info","description":"liveness probe; citations intentionally empty",
 "citations":[]}
```

### 3.4 Shared hashing, timestamps, and text rules in ghostlib

- **`split_lines`** implements Line_Number. Decode UTF-8 strictly (failure returns None → FILE_UNREADABLE). Empty text has zero lines. Split on `\n`, discard a final empty segment if present, and remove at most one trailing `\r` from each line.
- **Evidence content hash** is shared by Evidence_Recorder, Citation_Gate, and Report_Renderer (7#14). Compute `sha256( "\n".join(lines[start-1:end]).encode("utf-8") ).hexdigest()` for lowercase hex SHA-256 over the original range joined by LF and encoded as UTF-8.
- **Submission content hash** is shared by Rejection_Record and Gate_Live_Record (2#3). Use lowercase hex SHA-256 over exactly the full bytes the tool call would write. Encode JSON string arguments as UTF-8.
- **Evidence_Timestamp** uses `utc_now()` to produce `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC, milliseconds). Parse using a fixed-width regex, Gregorian calendar rules including before 1582, and seconds ≤ 59. Formatting exactly reverses parsing (4#12). Evidence_Recorder uses `max(current milliseconds, previous timestamp + 1ms)` (4#5).
- **Normalized_Text** is `" ".join(s.split())`, which splits on Unicode whitespace, joins with single spaces, and trims surrounding whitespace. Apply per line, then join lines with LF.
- **String length** always counts Unicode code points (A18).

### 3.5 Canonical_Form (5#8–5#10)

Serialize with `json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False)` and construct objects with fixed key order.

- Finding: `run_id, agent, title, category, severity, description, citations`
- Citation: `location, evidence_timestamp, excerpt`

Equivalent Findings have identical field values without Unicode normalization and yield identical bytes. Parsing and reserializing Canonical_Form yields identical bytes (idempotence).

### 3.6 Location and Location_Reference character sets

Location_Parser / Location_Formatter implement requirement 6. Split at the last ASCII `:`. Forbid empty, `.`, and `..` segments, `\`, control characters (U+0000–001F, U+007F–009F), and a leading `/`. Paths are at most 4096 UTF-8 bytes. Decimal line numbers are 1–999999999 without leading zeros. `<start>-<end>` requires start < end (A19, use `a.py:10` for a single line). Unit tests enforce round-trip properties (6#7, 6#9).

**Location_Reference stripping sets** (7#16)

- Tokenize on Unicode whitespace (`str.split()`).
- **Leading set** (strip a contiguous prefix) is `(` `)` `[` `]` `{` `}` `<` `>` `"` `'` `` ` `` and Unicode characters `（` `）` `［` `］` `｛` `｝` `〈` `〉` `《` `》` `「` `」` `『` `』` `【` `】` `“` `”` `‘` `’`.
- **Trailing set** is the leading set plus `，` `、` `。` `．` `；` `：` `？` `！` `,` `.` `;` `:` `?` `!` `…`.
- A stripped token is a Location_Reference if Location_Parser parses it successfully.

---

## 4. Hook pipeline

### 4.1 Hook configuration and payload contract

Both Hooks match all tools (`match: "*"`). Unknown tools must be blocked with UNDETERMINABLE while a run is active (11#3), so a tool-name allowlist cannot serve as the Hook match. Use the field names documented for the selected Kiro version (T1 in section 11).

```json
// .kiro/hooks/ghost-pre-tool-use.hook
{"enabled": true, "name": "ghost-pre-tool-use",
 "events": [{"type": "pre_tool_use", "match": "*",
             "command": "python3 .kiro/scripts/hook_launcher.py pre",
             "timeout_ms": 90000}]}
// .kiro/hooks/ghost-post-tool-use.hook uses the same structure, command … post, and timeout_ms 15000
```

**Payload contract** uses one line of JSON on stdin, following the Kiro docs and awslabs observations (T2 in section 11).

- PreToolUse: `{"session_id": "…?", "tool_name": "…", "tool_input": {…}}` (A11).
- PostToolUse adds `"tool_response"`. Read tools must provide paths and returned line text (A12). Record the verified shape in Tool_Catalog after T2, such as `content` with `start_line` or a list of `file/line/text` search results.

**Working directory.** Hook commands start at the workspace root (T3). Hook_Launcher checks that `runs/`, `control/`, and `.kiro/scripts/ghostlib.py` exist at startup. If anything is missing, fail closed with exit 2 and `GATE_FAILURE`.

### 4.2 Hook_Launcher routing

`hook_launcher.py <pre|post>` runs these steps.

1. **Scan for an active run.** Hold shared `L_G`, enumerate non-dot child directories of `runs/`, and read and strictly parse each `state.json`. Return `active(run_id)` or `none` or `corrupt(paths)`. For ≤1000 Run_Directories the full process, including Python startup, takes ≤1 second (15#4, A23, T6). On `corrupt`, exit 2 immediately with `STATE_CORRUPT`, each path, and the predefined explanation on stderr. Skip routing in step 3 (15#6, 8#13).
2. **Parse payload.** On empty or invalid JSON or missing tool_name or tool_input, send active-run calls to Write_Guard for PAYLOAD_INVALID (11#12). Launcher messages cannot contain this code under 8#13. Without an active run, allow by exiting 0 (15#5).
3. **Route `pre` by Tool_Catalog.** Launch at most one script per call, satisfying the sum rule in 8#15.

   | Class | No active run | Active run |
   |---|---|---|
   | Read-only tool | Exit 0 | Exit 0 (post records evidence) |
   | Write with all paths direct child files of the **same** `runs/<id>/inbox/` | Citation_Gate (create → RUN_NOT_ACTIVE, change existing → INBOX_IMMUTABLE, 15#3, 10#4, 9#10) | Citation_Gate (target is the run owning that Inbox) |
   | Write with any path under `control/` | Run_Controller (REQUEST_INVALID and related checks, 11#13) | Same |
   | Write with indeterminate paths, unknown tool, or shell | Exit 0 | Write_Guard (UNDETERMINABLE, 11#3) |
   | Other writes, including mixed scopes across Inboxes or Inbox plus other paths | Write_Guard (block only Run_Artifact and reserved paths under 11#2, allow others under 15#1 and 15#5) | Write_Guard (PROTECTED_PATH / UNDETERMINABLE) |

   Mixed scopes route to Write_Guard to fail closed. Renaming a Target file into an Inbox, for example, removes the source, which is outside Citation_Gate's Submission checks. Multiple paths can exceed any single check's scope. Use UNDETERMINABLE under the intent of 8#3 and 11#3 (X1 in section 10).
4. **Route `post`.** Exit 0 without an active run (15#2). Otherwise invoke Evidence_Recorder.
5. **Subprocess handling.** Use `subprocess.run(argv)` with list arguments and no shell. Use section 9 deadlines as `timeout=` and kill on timeout. On exit 2, pass stderr through unchanged if it includes a known Reason_Code, otherwise emit GATE_FAILURE. On exit 0, exit quietly with 0. On crash, other exit code, or failure to start, emit GATE_FAILURE (8#8, 8#9). Launcher messages contain only Reason_Code (GATE_FAILURE/TIMEOUT/STATE_CORRUPT), script name, paths, and predefined descriptions (8#13, 9#11). Stdout is never used because the platform does not relay it.

### 4.3 Tool_Catalog (`tool_catalog.json`)

```json
[{"name": "<built-in Kiro tool name>", "class": "read"},
 {"name": "<file-write tool>", "class": "write", "op": "create|modify|delete|move",
  "paths": ["$.file_path"], "content": "$.content"}]
```

- Populate initial entries from built-ins in the selected Kiro version. Reads include file read, directory listing, and file search. Writes include file creation and editing. In T4, verify tool names, op detection, and extraction rules against real payloads. `create` means the resolved target does not exist and full content can be extracted. `modify/delete/move` follow argument shape. Path and content extraction use `$.`-prefixed JSON Pointer-style references.
- Register `tool_response` extraction for directory listings and searches for Evidence_Recorder (5.2).
- Deliberately omit shell tools. All tool names outside Tool_Catalog yield UNDETERMINABLE during an active run (11#3). Kiro_Crew has no shell tools (11#4).

---

## 5. Component design

### 5.1 Run_Controller (`run_controller.py`, invokes Report_Renderer)

Hook_Launcher runs `run_controller.py <absolute request_file path>`. See section 9 for overall deadlines. For all state writes, hold exclusive `L_R`, write a temporary file, and use `os.replace`.

**Run REQUEST_INVALID checks first (11#13).** Require a direct child file of `control/` with one of the three reserved names, or exit 2 with REQUEST_INVALID. Other actions on control files (deletion or rename) fall outside clause 13 and fail closed with exit 2 and INTERNAL_ERROR.

**Start (20s deadline).** Accumulate failures unless a short circuit applies. List all Reason_Codes in one Blocking_Signal (1#9).

| Step | Check | Reason_Code | Short circuit |
|---|---|---|---|
| 1 | Read start.json. Require a readable nonempty target without `/`, `\`, or control characters, other than `.` or `..`, and a byte-identically named non-symlink directory under `targets/` outside `runs/` (A22). | TARGET_INVALID | Skip the tree checks under 1#2 (TARGET_EMPTY, AUTO_LOADED_CONTEXT_PRESENT, and SYMLINK_ESCAPE). Still run steps 2 and 3. |
| 2 | Scan all Run_State_Records in the workspace for missing, unreadable, or unparseable state, including unknown keys. | STATE_CORRUPT (each path) | No |
| 3 | Detect an active Audit_Run from the same scan. | RUN_ALREADY_ACTIVE (identifier) | No |
| 4 | Scan Target for regular files, excluding symlinks. | TARGET_EMPTY | No |
| 5 | Scan for case-insensitive `AGENTS.md` filenames and `.kiro` directories at any depth, including matching symlink names. | AUTO_LOADED_CONTEXT_PRESENT (total plus first 100 paths in lexicographic order) | No |
| 6 | Find symlinks resolving outside realpath(target), including nonexistent destinations, or resolution loops (strict realpath ELOOP). | SYMLINK_ESCAPE (total plus first 100 paths) | No |

Use one `os.walk` for the two tree scans in steps 5 and 6 and the regular file check. Use `scandir`, avoid descending through directory symlinks, and classify with `follow_symlinks=False`. Standard-sized repositories fit in the 20s deadline (T7).

**Creation protocol.** Once all checks pass, hold exclusive `L_G`, rescan for an active run (serializing concurrent Starts under 1#7), generate Run ID, create `runs/.tmp-<uuid>/`, and write empty `evidence.jsonl`, `findings.jsonl`, `rejections.jsonl`, and `inbox/`, plus `state.json` containing active and started_at. Use `os.rename` to publish as `runs/<run_id>`, then release the lock. On any exception, delete the staging tree and exit 2 with INTERNAL_ERROR, leaving no visible part of the attempted creation (1#14). `L_G` guarantees a single active run. Launcher termination at 20s leaves at most a `.tmp-*` directory that scans ignore.

**Abort (20s deadline).** Read state.json under exclusive `L_R`. A missing or nonactive run yields RUN_NOT_ACTIVE (14#3). Otherwise atomically replace the record with `status=aborted` and `finished_at=now`. On write failure issue INTERNAL_ERROR and preserve the previous contents (14#5). `L_R` serializes concurrent Finalize and Abort requests so the second sees the first's result (14#8).

**Finalize (50s deadline including 40s rendering, 8#12 and 13#14).** Hold exclusive `L_R` throughout.

1. Missing or unknown run_id or a nonactive run yields RUN_NOT_ACTIVE (14#3).
2. Missing or indeterminate `gate-live.jsonl` yields GATE_NOT_VERIFIED (2#5).
3. Empty, unreadable, or indeterminate `evidence.jsonl` yields RECORDER_NOT_VERIFIED (2#9).
4. On rejection above, delete any staged reports, preserve state, and leave no report.* (13#17). Orchestrator submits Abort under 2#10.
5. Invoke Report_Renderer (5.5) with its 40s internal deadline. On SUMMARY_UNSUPPORTED or REPORT_FAILED, exit 2, remove staged files, and preserve state.
6. On success, publish `report.md` and `report.json` at their final names before atomically replacing state.json with completed and matching finished_at (13#15). If terminated after report publication but before the state update, the run remains active. A repeated Finalize rerenders and overwrites both reports (see “Scope and limitations” in the Introduction).

### 5.2 Evidence_Recorder (`evidence_recorder.py`) and Evidence_Codec

After PostToolUse launches the recorder:

1. Check for an active run as in 4.2. Without one, exit 0 (15#2).
2. Use Tool_Catalog response extraction to obtain `{line number → line text}` per file for the lines returned to the agent. If the shape is indeterminate, record zero entries for the file (4#3). Exit 0 for non-read tools.
3. Read each current Target file with `split_lines`. Keep only line numbers whose returned text and current text have equal Normalized_Text (4#2). Sort, deduplicate, and merge adjacent lines into maximal contiguous ranges (4#1).
4. Record `realpath(read path)` relative to `realpath(target root)` (4#13). Resolution failure or paths outside Target yield zero entries (4#4). If the path cannot identify a unique active Target, fail closed with zero entries (this should be impossible under 1#7).
5. Under shared `L_R`, read all of `evidence.jsonl`. If the last line fails to parse, exit 2 with STATE_CORRUPT without writing (4#14, 8#7). While locked, compute strictly increasing timestamps per 3.4 and range hashes. Append all entries with one `write()` before releasing the lock (4#8 and 4#9). A crash leaves no partial line (4#7). Count lock waits in the 10s internal deadline in section 9. On timeout record zero entries and exit 0. Missing evidence can only cause later EVIDENCE_NOT_FOUND, so this fallback does not admit unsupported findings.
6. Include the Hook input's session identifier, if present, in every entry (4#6).

Evidence_Codec in ghostlib implements 4#10–4#15. Serialize in fixed key order and parse strictly. Unknown keys, invalid types or ranges, and invalid timestamps cause failure without a partial result. Unit tests enforce round-trip properties.

### 5.3 Citation_Gate (`citation_gate.py`)

Hook_Launcher runs `citation_gate.py <payload_json_path>` using a temporary payload file to avoid argv length limits. Enforce a 15s internal deadline (8#14), checking between stages and between Citations. On timeout exit 2 with TIMEOUT. **Stop checking deadlines once acceptance append starts** (10#2).

Process checks in this order and accumulate failures unless a short circuit applies. Report failures in one Blocking_Signal. Requirement 7#17 determinism follows from basing those validation checks only on Submission content, current Target, Evidence_Log, and state.json.

| Step | Check | Reason_Code | Details |
|---|---|---|---|
| 0 | Identify the Inbox's run | As shown | Indeterminate → UNDETERMINABLE (8#3). No active run with create → RUN_NOT_ACTIVE (15#3) and no Rejection_Log write (9#10). No active run with change or deletion of existing file → INBOX_IMMUTABLE (10#4). |
| 1 | Read all of state.json / evidence.jsonl / findings.jsonl under shared `L_R` | STATE_CORRUPT | Missing, unreadable, unparseable, or non-strictly-increasing Evidence timestamps (8#7). |
| 2 | Check filename and operation | FILE_NAME_INVALID | Filename outside `Submission_File_Name`, or `gate-canary.probe` with nonempty citations, **before Schema** (10#9). |
| 3 | Modify, overwrite, rename, or delete existing Inbox files, or create a filename already recorded in the Ledger | INBOX_IMMUTABLE | 10#4 |
| 4 | Extract the full contents about to be written | PAYLOAD_INVALID for missing fields or UNDETERMINABLE for unknown shape | 8#2, 8#3 |
| 5 | Submission_Parser | CITATION_MISSING → CITATION_INCOMPLETE → SCHEMA_INVALID | 5#12, 5#13, 5#7. On SCHEMA_INVALID, emit only Schema and filename Reason_Codes (7#19). Paths use field names and 1-based array indices, e.g. `citations[3].excerpt`. |
| 6 | run_id differs from run identifier | RUN_MISMATCH | 7#4 |
| 7 | Run fixed-order subchecks per Citation in array order. Stop at first failure for that Citation and continue with other Citations (7#20). | See below | Subchecks are pure functions over Target and Evidence_Log snapshots. |
| 8 | Coverage of Location_Reference in title and description | UNCITED_LOCATION | One failure per uncovered reference (7#16). |
| 9 | Compare Canonical_Form with ledger bytes | DUPLICATE_SUBMISSION (existing Finding_ID) | 10#3 |
| 10 | Accept | — | See below. |

Step 7 order within each Citation is `LOCATION_MALFORMED → SPAN_TOO_LARGE → TIMESTAMP_MALFORMED → PATH_OUTSIDE_TARGET → FILE_NOT_FOUND → FILE_UNREADABLE → LINE_OUT_OF_RANGE → EVIDENCE_NOT_FOUND → EVIDENCE_MISMATCH → CONTENT_CHANGED → EXCERPT_MISMATCH`.

- **FILE_NOT_FOUND / PATH_OUTSIDE_TARGET.** Resolve Location with `realpath`. Check missing paths, nonregular files, and paths escaping realpath(target) under 7#6 and 7#7. Strict resolution failure due to a loop yields PATH_OUTSIDE_TARGET.
- **EVIDENCE_NOT_FOUND / EVIDENCE_MISMATCH.** Match Evidence_Timestamp character for character to the unique log entry. Strictly increasing timestamps prevent duplicates. Require exact path equality and entry.start ≤ cite.start ≤ cite.end ≤ entry.end (7#12, 7#13).
- **CONTENT_CHANGED.** Reject if the current file has fewer than entry.end lines or `evidence_hash(current file, entry.start..entry.end)` differs from entry.sha256 (7#14, using 3.4).
- **EXCERPT_MISMATCH.** Let B be LF-joined Normalized_Text of the covered lines, and e the Normalized_Text of Excerpt. Find every occurrence of e in B. Pass if an occurrence fully contains the span in B of a covered nonempty line (7#15).
- **UNCITED_LOCATION.** Extract each Location_Reference from title and description under 3.6. Require a Citation with an exact path match and a containing line range (7#16).
- **Canary (2#2, 2#3, 2#7).** For an active target run and a result containing CITATION_MISSING and no System_Reason_Code, append `{run_id, at, sha256}` to `gate-live.jsonl` under exclusive `L_R` before Blocking_Signal. Leave Gate_Live_Record untouched otherwise.
- **Rejection (requirement 9).** Exit 2 with one stderr line per failure in format `<REASON_CODE>[ <detail>]: <predefined description>`. Allowed detail values are `citations[<n>]`, a field path, a parseable Location path, or `finding F-######`. For more than 50 items, emit the first 50 and one fixed-format remainder count (9#2, 9#3). Append exactly one complete Rejection_Record under exclusive `L_R` only if the target run is active (9#4, 9#10). If append fails, still reject, add a separate INTERNAL_ERROR line, and leave no partial log record (9#9). Include only content allowed by 9#3. Location syntax prevents control characters in stderr paths.
- **Acceptance (requirement 10).** Upgrade to exclusive `L_R`. Recheck active state under lock, using RUN_NOT_ACTIVE if the run ended while waiting (14#7), and recheck ledger duplicates and filename collisions (10#8). Set finding_id to the record count plus one and accepted_at to now. Append to the ledger with one `write()`. Release the lock and exit 0 without further signals (10#2).
- **Residual 20s Launcher window after the 15s internal deadline.** Termination after ledger append leaves a Finding without an Inbox file. As described in the Introduction, resubmitting the same content returns DUPLICATE_SUBMISSION and the existing Finding_ID (10#3).

### 5.4 Write_Guard (`write_guard.py`)

Launcher starts Write_Guard with the payload. Invalid payload yields PAYLOAD_INVALID while active (11#12), and without an active run Launcher already allows it under 15#5. Tool names outside Tool_Catalog, shell tools, unresolvable paths, or indeterminate Protected_Path yield UNDETERMINABLE while active (11#3). Otherwise classify paths as follows.

- While active, block Resolved_Path inside the active Target, inside Toolkit_Configuration (`.kiro/agents/**`, `.kiro/hooks/**`, `.kiro/steering/**`, `.kiro/scripts/**`), or within `runs/` outside Inboxes. Include every Run_Artifact and reserved path such as not-yet-generated `gate-live.jsonl` and `report.*`. Return PROTECTED_PATH with one line per blocked path, up to 50 lines plus a remainder count (11#1).
- Without an active run, block only Run_Artifact and reserved paths based on known filenames within `runs/<run>/` (11#2). Allow other paths (15#1).
- Resolve each Resolved_Path using `realpath`. Compare using casefold on case-insensitive filesystems (Glossary).

### 5.5 Report_Renderer (`report_renderer.py`, invoked within Run_Controller's Finalize transaction)

The internal deadline is 40s (13#14). Inputs are the five run-directory records, current Target, and Finalize_Request.

1. Parse the four Run_Artifacts. An unreadable or unparseable input or a changed ledger on reread yields REPORT_FAILED.
2. **Revalidate (13#2, 13#3).** Parse canonical for each Accepted_Finding (failure → REPORT_FAILED). Rerun 7#6–7#16 against the current Target and Evidence_Log in 7#20 order with its short circuits, including SPAN_TOO_LARGE and TIMESTAMP_MALFORMED. List failed items as invalidated with Reason_Code and Citation index or field path.
3. **Findings (13#4).** List findings that pass revalidation. Sort critical → high → medium → low → info, then by acceptance order within each severity.
4. **Summary validation (13#11).** More than 20 statements, an item over 500 code points, no Finding_ID from the findings list, an ID outside the list, or a Location_Reference without corresponding Finding Citation coverage yields SUMMARY_UNSUPPORTED (exit 2 with the 1-based index of every failing statement). Extract IDs with `F-[0-9]{6}` and reuse 3.6 for Location_Reference.
5. **Statistics (13#7).** Include all five severity and category values even when zero, the invalidated count, and rejection statistics over all Rejection_Log entries except Canary records whose sha256 matches Gate_Live_Record. Count multi-code entries once per code. Group names outside the four Audit_Agents under `unknown`.
6. **Coverage (13#8, 13#21).** Use one `os.walk` to count regular Target files and to count and list files whose relative path cannot form a valid Location (illegal characters or segments, >1024 code points, or >4096 UTF-8 bytes). Count distinct Evidence_Log paths and list all of them in JSON.
7. **Write output.** Write `report.md.tmp` and `report.json.tmp` in full before publishing under final filenames via `os.replace`. On failure, delete temporary files and issue REPORT_FAILED (13#17).

**Markdown escaping (13#10).** Run `md_escape` on all Submission, Finalize, and Target text, including paths, Target name, and summary statements, then place it in backtick code spans. Replace `\` with `\\`, `` ` `` with `` \` ``, and `|` with `\|`. Replace LF, CR, and Tab with single spaces. Apply the same escaping to Excerpt and put it in a fenced block with fixed info string `text`. Escaping removes bare backticks and prevents fence boundaries, headings, list items, table boundaries, links, images, and HTML elements. Use `json.dumps` to encode JSON safely.

**`report.md` section order**

1. **Metadata** includes run_id, Target, start and end times, and the four-agent status table (`completed`, `failed`, or `not_dispatched`).
2. **Summary** lists each statement with its referenced IDs.
3. **Statistics** shows the counts described above.
4. **Findings** lists Finding_ID, title, severity, category, self-reported agent, description, and every Citation's location @ts and fenced Excerpt.
5. **Invalidated** lists findings that failed revalidation.
6. **Coverage** shows the coverage data described above.
7. **Notes** includes the disclaimer for an empty findings list (13#9) and the evidence limits statement (13#20). A Citation establishes that the code was read and still matched at report generation. It does not establish semantic correctness.

**`report.json` top-level structure**

```json
{"schema_version": 1,
 "run": {"run_id","target","started_at","finished_at","agents":{4 keys: "completed|failed|not_dispatched"}},
 "summary": [{"index":1,"statement":"…","finding_ids":["F-…"]}],
 "findings": [{"finding_id","agent","category","severity","title","description",
               "citations":[{"location","evidence_timestamp","excerpt","path","start","end"}]}],
 "invalidated": [{"finding_id","failures":[{"reason_code","citation_index"?,"field_path"?}]}],
 "statistics": {"by_severity":{5 levels},"by_category":{5 levels},"invalidated_count":N,
                "rejected":{"total":N,"by_reason_code":{},"by_agent":{}}},
 "coverage": {"evidence_file_count":N,"evidence_paths":[…],"target_regular_file_count":N,
              "unlocatable_file_count":N,"unlocatable_paths":[…]},
 "notes": {"empty_findings_disclaimer": true?,"evidence_caveat":"…"}}
```

Each finding parsed by Submission_Parser yields the same Canonical_Form as its ledger record (13#13, enforced by tests).

---

## 6. Agents and Evidence_Rules

### 6.1 Five agent definitions (`.kiro/agents/*.md`)

Use the frontmatter fields verified for the selected Kiro version (T5).

| Agent | Built-in tools | Additional restrictions |
|---|---|---|
| Orchestrator | Read, list, search, write, and subagent invocation | Write only to `control/**` and `runs/*/inbox/**`. Invoke only the four Audit_Agents. Read only `targets/**` and `runs/**` (11#4–11#8). |
| Four Audit_Agents | Read, list, search, and write | Write only to `runs/*/inbox/**`. Read only the same roots as Orchestrator (11#5, 11#7). |

Out-of-scope calls fail immediately without prompting the User (A14, T5).

Include the complete Evidence_Rules in every definition (6.2). Enumerate the Glossary review areas. Specify the allowed category for each agent (Cartographer=architecture, Security_Auditor=security, Dependency_Auditor=dependency, Quality_Auditor=quality, with embedded-instruction available to all four under 3#1 and 12#5). The workflow is to read code, look up timestamps in `evidence.jsonl`, write the Submission, and handle rejections. Permit at most two corrections for one finding and abandon and count it after a third rejection (9#6, 9#7). Stop submitting on DUPLICATE_SUBMISSION without counting the finding as abandoned (9#12). Immediately end the Assignment on System_Reason_Code and report it in Completion_Summary (9#8). Use the format in 6.3.

### 6.2 Evidence_Rules and inlining strategy (12#1, 12#2, A15)

`.kiro/steering/evidence-rules.md` is the **normative source**. Explicit `inclusion: manual` frontmatter prevents automatic loading in any session (12#2). Use A15's alternative and **inline the full Evidence_Rules in all five agent definitions**. Requirement 12#1 needs the full text in context before the first tool call, which the platform cannot guarantee in manual/fileMatch modes. Kiro's definition loader includes the inline text deterministically, including for subagents. During development, `dev/sync_evidence_rules.py` inserts the normative block into all five and verifies byte-for-byte equality to prevent drift. The script is not called at runtime.

Evidence_Rules covers Submission_Schema (JSON keys in 3.2, length limits, and category/severity/agent values), Location syntax (including A19's single-line form), Evidence_Timestamp lookup, untrusted content (section 7), and Completion_Summary format (6.3). All values match requirements 5, 6, and 7. To look up Evidence_Timestamp, read `runs/<run_id>/evidence.jsonl` (JSONL). Match path character for character and require the entry range to contain the citation range. Copy its `ts` into `evidence_timestamp`. If no entry matches, make one read call to produce fresh evidence before citing, or abandon the finding.

### 6.3 Completion_Summary format (3#5, 3#6)

Return exactly one text block with five fixed labels. If any line is missing, treat it as no Completion_Summary (3#8).

```
ACCEPTED: <filename, filename, … | none>
REJECTED_COUNT: <decimal integer>
ABANDONED_COUNT: <decimal integer>
REVIEWED_PATHS: <paths within Target, … | all>
SYSTEM_REASON_CODES: <CODE, … | none>
```

### 6.4 Required Orchestrator workflow (in its definition)

If the request names no directory, withhold Start and ask the User to specify one (1#16). If Start is blocked, list each Reason_Code and accompanying detail and do not retry automatically (1#10, 1#11). On a successful write, confirm creation via `runs/*/state.json` by matching target and active status, then report run_id and Run_Directory path (1#12). If there is no matching record (the Hook may not have loaded), tell the User to retry in a new session and withhold dispatch and Canary_Submission (1#15). Perform the liveness checks in 2#1–2#10. Submit the Canary, consult the error and gate-live.jsonl, and submit Abort and notify the User on failure. After Cartographer returns, verify a nonempty Evidence_Log (2#6). Process Completion_Summary under 3#7–3#13. Use Findings_Ledger as the sole source of findings and neither relay nor act on extra content. Send the completion map in Finalize. Summary_Statements must cite Finding_IDs and cannot be free-form narrative (A9). Check state.json afterward and report under 13#16 or 13#19. On a User request to stop, submit Abort and report under 14#6.

---

## 7. Untrusted content and injection defenses (requirement 12)

- **Instruction source allowlist (12#7).** Orchestrator follows its definition, Evidence_Rules, and User messages in the session. Audit_Agent follows its definition, Evidence_Rules, and Assignment. Evidence_Rules prevail in a conflict.
- **Three Embedded_Instruction criteria (12#8).** Include all three, covering full-width and multilingual text, and continue analyzing text classified as data as part of the audit.
- **Audit_Agent handling.** Before finishing, submit category=embedded-instruction with a Citation whose Excerpt completely includes the line containing the text (12#5). Continue Assignment under the instruction allowlist (12#6). Orchestrator likewise acts only on triggers in the requirements (12#9).
- **Structural defenses complement the instructions.** Toolkit_Configuration sits outside Target and Write_Guard protects it. Agent access is restricted to `targets/**` and `runs/**` for reads, and Target content cannot enter scripts or configuration (11#7). Allowlist or escape all echoed Submission, Location, and Completion_Summary fields before sending them to stderr (9#3) or writing a report (5.5). Evidence_Rules treats Target paths in an Assignment as data.
- **Scope.** Citation_Gate verifies that evidence exists and matches. It does not determine semantic correctness. The agent field is self-reported.

---

## 8. Rejection_Message format and predefined Reason_Code descriptions

Line format is `<REASON_CODE>[ <detail>]: <description>`. The ghostlib constants provide one description per code (9#2).

| Reason_Code | Predefined description |
|---|---|
| TARGET_INVALID | The Target_Codebase name in the Start_Request is missing, invalid, or does not exist. |
| TARGET_EMPTY | The Target_Codebase contains no regular files. |
| RUN_ALREADY_ACTIVE | An Audit_Run with active status already exists in the workspace. |
| AUTO_LOADED_CONTEXT_PRESENT | The Target_Codebase contains files or directories that Kiro would auto-load (e.g. AGENTS.md, .kiro). |
| SYMLINK_ESCAPE | The Target_Codebase contains symbolic links pointing outside it or that cannot be resolved. |
| STATE_CORRUPT | The audit state file is missing, unreadable, or unparseable. |
| INTERNAL_ERROR | Internal Toolkit error; blocked fail-closed. |
| GATE_NOT_VERIFIED | No Gate_Live_Record; cannot confirm the Citation_Gate took effect. |
| RECORDER_NOT_VERIFIED | The Evidence_Log is empty or unreadable; cannot confirm the Evidence_Recorder took effect. |
| CITATION_MISSING | The Submission lacks a citations field, or its value is null or an empty array. |
| CITATION_INCOMPLETE | A Citation lacks location, evidence_timestamp, or excerpt, or one of them is null or an empty string. |
| SCHEMA_INVALID | The Submission does not conform to the Submission_Schema. |
| RUN_MISMATCH | The Submission's run_id does not match the target Audit_Run. |
| RUN_NOT_ACTIVE | The target Audit_Run does not exist or has already ended. |
| LOCATION_MALFORMED | The Location is syntactically invalid. |
| FILE_NOT_FOUND | The cited path does not exist in the Target_Codebase or is not a regular file. |
| PATH_OUTSIDE_TARGET | The cited path resolves outside the Target_Codebase. |
| FILE_UNREADABLE | The cited file is unreadable or not valid UTF-8. |
| LINE_OUT_OF_RANGE | The cited line numbers exceed the file's current line count. |
| SPAN_TOO_LARGE | A single Citation covers more than 50 lines. |
| TIMESTAMP_MALFORMED | The Evidence_Timestamp is not a valid UTC millisecond-precision timestamp. |
| EVIDENCE_NOT_FOUND | No Evidence_Entry matching that timestamp exists in the Evidence_Log. |
| EVIDENCE_MISMATCH | The matched Evidence_Entry's path or line range differs from the Citation. |
| CONTENT_CHANGED | The cited code differs from what was recorded as evidence (it has changed since). |
| EXCERPT_MISMATCH | The Excerpt does not match the cited line content. |
| UNCITED_LOCATION | A file:line reference in title or description is not covered by any Citation. |
| PAYLOAD_INVALID | The hook input is empty, is not valid JSON, or lacks the tool name/arguments. |
| UNDETERMINABLE | The effect of this tool call cannot be determined; blocked fail-closed. |
| GATE_FAILURE | The validation script did not run correctly; blocked fail-closed. |
| TIMEOUT | Validation or processing timed out; blocked fail-closed. |
| FILE_NAME_INVALID | The Submission file name is invalid. |
| INBOX_IMMUTABLE | Existing files in the Submission_Inbox must not be modified, or the file name is already used by an accepted Finding. |
| DUPLICATE_SUBMISSION | Content duplicates an already accepted Finding. |
| PROTECTED_PATH | This path is write-protected during the audit. |
| REQUEST_INVALID | The Control_Request file name or location is invalid. |
| REPORT_FAILED | Generating the Audit_Report failed; the Audit_Run remains active. |
| SUMMARY_UNSUPPORTED | The Finalize_Request contains Summary_Statements that failed validation. |

Hook_Launcher templates (8#13) are `GATE_FAILURE [<script>]: <description>`, `TIMEOUT [<script>]: <description>`, and `STATE_CORRUPT: <paths…> <description>`. Omit the script name when the Launcher scan itself fails.

---

## 9. Deadlines and timeouts (A17, 8#5, 8#6, 8#12, 8#14, 8#15, 11#10, 11#11, 13#18)

| Component | Internal deadline | Launcher deadline (kill) | Hook timeout |
|---|---|---|---|
| Citation_Gate | 15s (8#14, no checks after acceptance append begins) | 20s (8#5) | pre 90s |
| Write_Guard | 4.5s (emits TIMEOUT itself) | 5s (11#10) | pre 90s |
| Run_Controller | Start/Abort 18s, Finalize 48s total (40s rendering, 13#14) | 20s / 50s (8#12) | pre 90s |
| Evidence_Recorder | 10s | — (post Hook timeout of 15s is the fallback) | post 15s |

Each pre event launches at most one script, so the Hook meets 8#15's sum rule. Its 90s timeout exceeds the 60s Run_Controller minimum (13#18), 30s Citation_Gate minimum (8#6), and 10s Write_Guard minimum (11#11). Lock waits count against internal deadlines. Without an active run and with ≤1000 Run_Directories, total pre+post runtime for every unrelated tool call must be ≤1s (15#4). Budget approximately 80ms for two Python startups and 200ms for two scans (verify in T6).

---

## 10. Error handling and fail-closed behavior

| Failure | Behavior | Basis |
|---|---|---|
| Hook payload empty, invalid, or missing fields | Active → PAYLOAD_INVALID. Otherwise allow. | 8#2, 11#12, 15#5 |
| Unknown tool, shell, or indeterminate path | Active → UNDETERMINABLE. Otherwise allow. | 11#3, 15#5 |
| Script crash, exit outside {0,2}, or exit 2 without Reason_Code | GATE_FAILURE with script name and predefined description | 8#8, 8#9, 8#13 |
| Script exceeds deadline | Launcher kills script and emits TIMEOUT | 8#5, 8#12, 11#10 |
| Run status indeterminate (scan failure) | Treat as active and return STATE_CORRUPT plus path list | 8#10, 15#6 |
| Unparseable Evidence_Log, Ledger, or state | Reject with STATE_CORRUPT / REPORT_FAILED and write no artifacts | 8#7, 13#14, 4#14 |
| Termination during append | Single-write atomicity leaves only a complete prefix or complete lines | 4#7, 8#11, 10#6 |
| Ledger grows during Finalize (theoretically impossible under exclusive L_R) | Inconsistent reread → REPORT_FAILED | 13#14 |
| Finding sequence number >999999 | INTERNAL_ERROR | 10#7 boundary |
| X1 Mixed write across Inboxes or Inbox plus other paths | Active → UNDETERMINABLE as the conservative default. Otherwise apply only 11#2. | Intent of 8#3 and 11#3, see 4.2 |

---

## 11. Platform assumptions and calibration checklist

Verify each item during implementation and record results in `docs/platform-notes.md`. Deviations require design review.

| # | Assumption | Verify | Fallback if false |
|---|---|---|---|
| T1 | Hook file format | `.kiro/hooks/*.hook` fields and event types, and support for `timeout_ms` ≥60s (A16) | If the maximum is lower, reduce the Finalize rendering budget and update the requirement assumptions. |
| T2 | Hook payload | stdin JSON keys `session_id/tool_name/tool_input/tool_response` (A11/A12), and paths and line text in PostToolUse read results | If A11 fails, reject all Submissions as designed. If A12 fails, reject all Citations with EVIDENCE_NOT_FOUND and the Audit_Run cannot finalize (expected under 12#1). |
| T3 | Hook cwd | Command starts at the workspace root | Resolve the root from payload or environment. On failure emit GATE_FAILURE. |
| T4 | Built-in tool names and arguments | Full Tool_Catalog covering read/write/list/search names, arguments, and responses | Block unknown tools with UNDETERMINABLE while active. |
| T5 | Agent restrictions (A14) | Tool allowlist, writable and readable roots, and subagent allowlist fail without prompting on violations. Under A13, subagent tool calls also trigger Hooks. | If A13 fails, Write_Guard and Citation_Gate do not cover Audit_Agents. Block release, use session-level compensation, and escalate as a requirements change. |
| T6 | Steering loading (A15) | `inclusion: manual` prevents automatic loading. The sync script guarantees identical inline text in all five definitions. | Inline the only copy into each definition (already the primary strategy). |
| T7 | Performance benchmarks | pre+post ≤1s with ≤1000 Run_Directories (15#4, A23). Start tree scans and Gate validation finish within their deadlines on typical workloads. | Optimize with `os.scandir` and early short circuits or lower the budget. |

---

## 12. Test strategy

- **Unit and component property tests.** Directly assert round trips and idempotence. Cover Evidence_Codec (4#11 and 4#12, including leap years, 1582, and leap-second boundaries), Location_Parser/Formatter (6#7, 6#9), Canonical_Serializer equivalence and idempotence (5#8–5#10), full Submission_Parser defect matrix (5#7, 5#12, 5#13), all-occurrence Excerpt matching (7#15), Location_Reference stripping with full-width and mixed characters, `md_escape` preventing structural Markdown for arbitrary inputs, strictly increasing timestamps, and `split_lines` for CRLF, BOM, empty files, and no trailing LF.
- **Integration in a simulated Hook harness.** Drive all six scripts with synthetic payloads independently of Kiro. Workspace fixtures cover positive and negative cases for every acceptance criterion in requirements 1–15. Focus on Canary liveness (2#1–2#10), rejection line format and 50-line truncation (9#2, 9#3), unique Finding_IDs under concurrent Submissions and at most one acceptance for duplicates or filename collisions (10#5, 10#8), ten simultaneous Starts yielding one active run (1#7), Finalize/Abort serialization (14#8), and RUN_NOT_ACTIVE for submissions after completion (7#5, 14#7).
- **Fault injection.** Terminate processes at write windows and verify prefix invariants (4#7, 10#6, 8#11). Corrupt state/evidence and check exact STATE_CORRUPT paths (15#6). Change Target during the run and check CONTENT_CHANGED and report invalidations. Exercise payload variants for A11 and A12 fallbacks.
- **Performance (T7).** Check the pre/post budget with 1000 Run_Directories, the Gate's 15s budget with 10⁴ Evidence_Log entries, and 20s Start scanning for a Target with files numbering in the tens of thousands.
- **Platform acceptance (T1–T7).** Run the checklist and an end-to-end audit in real Kiro IDE and Kiro CLI, including a Target fixture with AGENTS.md, prompt-injection comments, outward symlinks, and `.kiro` directories.

---

## 13. Requirements traceability

| Requirement | Design sections |
|---|---|
| 1 Start an audit | 5.1 Start and creation protocol, 3.2 Run ID, 4.2 routing |
| 2 Verify gate liveness | 5.3 Canary and gate-live.jsonl, 6.4 Orchestrator workflow |
| 3 Responsibilities and dispatch | 6.1 agent definitions, 6.3 Completion_Summary, 6.4 |
| 4 Read evidence | 5.2 Evidence_Recorder/Codec, 3.4 line splitting and hashes, 4.1 post payload |
| 5 Submission format | 3.3/3.5 schema and Canonical_Form, 5.3 step 5 |
| 6 Location syntax | 3.6, ghostlib Location module |
| 7 Citation checks | 5.3 pipeline and subcheck order |
| 8 Fail closed | 4.2 Launcher handling, section 9 deadlines, section 10 behavior |
| 9 Return rejections | 5.3 rejection output, section 8 description table |
| 10 Acceptance and ledger | 5.3 acceptance protocol, 3.3 ledger schema |
| 11 Read-only and write protection | 5.4 Write_Guard, 4.3 Tool_Catalog, 6.1 restrictions |
| 12 Untrusted content | 6.2 Evidence_Rules, section 7 injection defenses |
| 13 Audit_Report | 5.5 Renderer, 5.1 Finalize workflow |
| 14 Abort and closed runs | 5.1 Abort, 5.3 step 0 and acceptance recheck, 2.3 L_R serialization |
| 15 Avoid disrupting other work | 4.2 fast path, section 9 budgets, 5.4 without an active run |

---

## 14. Open issues and next steps

- **O1: Full Control_Request schema validation.** Open. Use dedicated Reason_Codes where defined and fail closed with INTERNAL_ERROR for other schema defects. REQUEST_INVALID keeps its current meaning.
- **O2: Protect all of Runs_Directory without an active run.** Open. Forged records cause later operations to fail closed with STATE_CORRUPT or RUN_ALREADY_ACTIVE. Unverified findings cannot pass. The User may decide whether to include `runs/**` in Protected_Path at all times.
- After design review, generate `tasks.md` using the Kiro spec workflow. Suggested order is ghostlib and unit tests, Hook_Launcher/Tool_Catalog and T1–T4 calibration, Run_Controller, Evidence_Recorder, Citation_Gate, Write_Guard, Report_Renderer, five agent definitions and Evidence_Rules sync, then end-to-end and platform acceptance.
