#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Citation_Gate — fail-closed validation of Submissions (design 5.3).

Launched by Hook_Launcher: citation_gate.py <payload_json absolute path>

Decision pipeline (order = implementation order; failures accumulate unless
short-circuiting is noted):
  0 target Inbox run determination (UNDETERMINABLE / RUN_NOT_ACTIVE no log
    write / INBOX_IMMUTABLE)
  1 read state / evidence / findings whole under the L_R shared lock → STATE_CORRUPT
  2 file name check (before Schema, R10.9) → FILE_NAME_INVALID
  3 modifying an existing file / a new file colliding with a Ledger name → INBOX_IMMUTABLE
  4 extract the full content → PAYLOAD_INVALID / UNDETERMINABLE
  5 Submission_Parser: CITATION_MISSING → CITATION_INCOMPLETE → SCHEMA_INVALID (short-circuit)
  6 RUN_MISMATCH
  7 per-Citation fixed-order sub-checks (each stops at first failure, the rest continue, R7.20)
  8 UNCITED_LOCATION (R7.16)
  9 DUPLICATE_SUBMISSION (with the existing Finding_ID)
 10 accept protocol (L_R upgraded to exclusive → revalidate → assign number →
    single write() append → silent exit 0)

Internal deadline 15s (checked at phase boundaries and between individual
Citations); once the accept-append phase is entered, the deadline is no
longer checked (R10.2). Canary special-casing (R2.2/R2.3/R2.7) is in reject().
"""

import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ghostlib as g  # noqa: E402

INTERNAL_DEADLINE = 15.0


class Gate_Exit(Exception):
    def __init__(self, lines, code=2):
        super().__init__(code)
        self.lines = lines
        self.code = code


def emit(lines, code=2):
    raise Gate_Exit(lines, code)


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        emit([g.rejection_line("TIMEOUT")])


def locate_inbox_target(cwd, payload):
    """payload → (owner_run_id, run_dir, inbox_path, file_name, abs_target).

    Cannot locate → None (UNDETERMINABLE).
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    p = g.json_pointer(tool_input, "$.path")
    if not isinstance(p, str) or p == "":
        return None
    raw = p if os.path.isabs(p) else os.path.join(cwd, p)
    resolved = g.resolve_real(raw)
    if resolved is None:
        return None
    runs_root = g.resolve_real(os.path.join(cwd, "runs"))
    if runs_root is None or not g.path_within(resolved, runs_root):
        return None
    rel = os.path.relpath(resolved, runs_root)
    parts = rel.split(os.sep)
    if len(parts) != 3 or parts[1] != "inbox":
        return None
    run_id, _inbox, file_name = parts
    run_dir = os.path.join(runs_root, run_id)
    return run_id, run_dir, os.path.join(run_dir, "inbox"), file_name, resolved


def read_run_artifacts(run_dir, run_id):
    """Read the three artifacts whole under the L_R shared lock; (state, entries, ledger, None) or (None, …).
    Missing / unreadable / unparseable / non-strictly-increasing Evidence timestamps → STATE_CORRUPT (R8.7).
    """
    state_text = g.read_text(os.path.join(run_dir, "state.json"))
    if state_text is None:
        raise _Corrupt(os.path.join(run_dir, "state.json"))
    state = g.parse_state(state_text)
    if state is None:
        raise _Corrupt(os.path.join(run_dir, "state.json"))

    ev_text = g.read_text(os.path.join(run_dir, "evidence.jsonl"))
    if ev_text is None:
        raise _Corrupt(os.path.join(run_dir, "evidence.jsonl"))
    entries, corrupt = g.parse_evidence_log(ev_text)
    if entries is None:
        raise _Corrupt(os.path.join(run_dir, "evidence.jsonl"))

    ld_text = g.read_text(os.path.join(run_dir, "findings.jsonl"))
    if ld_text is None:
        raise _Corrupt(os.path.join(run_dir, "findings.jsonl"))
    ledger = []
    for line in ld_text.split("\n"):
        if line == "":
            continue
        try:
            rec = g.StrictJSONParser(line).parse()
        except g._JSONError:
            raise _Corrupt(os.path.join(run_dir, "findings.jsonl"))
        required = ("finding_id", "accepted_at", "submission_file", "canonical")
        if not isinstance(rec, dict) or any(k not in rec for k in required) \
                or not isinstance(rec.get("canonical"), dict) \
                or not isinstance(rec.get("finding_id"), str) \
                or not g.FINDING_ID_RE.match(rec["finding_id"]) \
                or g.parse_ts(rec.get("accepted_at")) is None \
                or not isinstance(rec.get("submission_file"), str) \
                or ("session" in rec and not isinstance(rec.get("session"), str)):
            raise _Corrupt(os.path.join(run_dir, "findings.jsonl"))
        ledger.append(rec)
    return state, entries, ledger


class _Corrupt(Exception):
    def __init__(self, path):
        super().__init__(path)
        self.path = path


def load_catalog_entry(tool_name):
    text = g.read_text(os.path.join(SCRIPT_DIR, "tool_catalog.json"))
    data = g.strict_json_loads(text) if text else None
    if isinstance(data, list):
        for e in data:
            if isinstance(e, dict) and e.get("name") == tool_name:
                return e
    return None


def agent_of(content):
    obj = g.strict_json_loads(content)
    if isinstance(obj, dict) and isinstance(obj.get("agent"), str) \
            and obj["agent"] in g.AUDIT_AGENTS:
        return obj["agent"]
    return "unknown"


def main():
    cwd = os.getcwd()
    deadline = g.monotonic_deadline(INTERNAL_DEADLINE)
    try:
        run(cwd, deadline)
    except Gate_Exit as exc:
        sys.stderr.write("\n".join(exc.lines) + "\n")
        sys.stderr.flush()
        sys.exit(exc.code)
    sys.exit(0)


def run(cwd, deadline):
    if len(sys.argv) < 2:
        emit([g.rejection_line("UNDETERMINABLE")])
    payload_text = g.read_text(sys.argv[1])
    payload = g.strict_json_loads(payload_text) if payload_text else None
    if not isinstance(payload, dict):
        emit([g.rejection_line("PAYLOAD_INVALID")])  # 8#2

    check_deadline(deadline)

    loc = locate_inbox_target(cwd, payload)
    if loc is None:
        emit([g.rejection_line("UNDETERMINABLE")])  # 8#3
    run_id, run_dir, _inbox_dir, file_name, target_abs = loc

    scan = g.scan_runs(os.path.join(cwd, "runs"))
    if scan["corrupt_paths"]:
        emit([g.rejection_line("STATE_CORRUPT", " ".join(scan["corrupt_paths"]))])
    active_run = scan["active_run_id"]
    is_canary = file_name == g.CANARY_FILE_NAME
    valid_name = bool(g.SUBMISSION_NAME_RE.match(file_name))
    exists = os.path.exists(target_abs)

    # —— order 0: target Inbox run determination ——
    if active_run != run_id:
        # target run not active (including no active run)
        if exists:
            emit([g.rejection_line("INBOX_IMMUTABLE")])  # 10#4 (no log write, 9#10)
        emit([g.rejection_line("RUN_NOT_ACTIVE")])       # 15#3 / 7#5 (no log write)

    lock_shared = g.Dir_Lock(run_dir, exclusive=False, deadline=deadline)
    if not lock_shared.acquire():
        emit([g.rejection_line("TIMEOUT")])
    try:
        try:
            state, entries, ledger = read_run_artifacts(run_dir, run_id)
        except _Corrupt as exc:
            emit([g.rejection_line("STATE_CORRUPT", exc.path)])
    finally:
        lock_shared.release()

    check_deadline(deadline)

    # target run already ended (race between the active determination and the read; theoretically impossible, fail-closed)
    if state["status"] != "active":
        emit([g.rejection_line("RUN_NOT_ACTIVE")])

    filename_failures = []
    # —— order 2: file name check (before Schema, R10.9) ——
    if not valid_name and not is_canary:
        filename_failures.append(("FILE_NAME_INVALID", None))
    elif is_canary:
        # Canary_File_Name with non-empty citations → FILE_NAME_INVALID (10#9)
        probe_content, _probe_status = extract_content(
            payload, load_catalog_entry(payload.get("tool_name")))
        probe = g.strict_json_loads(probe_content) \
            if isinstance(probe_content, str) else None
        if probe is not None and isinstance(probe, dict):
            cits = probe.get("citations")
            if isinstance(cits, list) and len(cits) > 0:
                filename_failures.append(("FILE_NAME_INVALID", None))

    # —— order 3: Inbox immutability ——
    if exists:
        # the tool call is about to modify/overwrite/append to/delete an existing file (10#4)
        filename_failures.append(("INBOX_IMMUTABLE", None))
    else:
        for rec in ledger:
            if rec["submission_file"] == file_name:
                filename_failures.append(("INBOX_IMMUTABLE", None))
                break

    # —— order 4: extract the full content about to be written ——
    entry = load_catalog_entry(payload.get("tool_name"))
    content, content_status = extract_content(payload, entry)
    if content is None:
        if content_status == "missing":
            emit([g.rejection_line("PAYLOAD_INVALID")])       # 8#2 missing field
        emit([g.rejection_line("UNDETERMINABLE")])            # 8#3 unknown shape / conflicting candidates
    content_sha = g.bytes_hash(content.encode("utf-8"))

    check_deadline(deadline)

    # —— order 5: Submission_Parser (short-circuit priority, R5.12/R5.13/R7.19) ——
    finding, parse_failures = g.parse_submission(content)
    schema_codes = {"SCHEMA_INVALID", "CITATION_MISSING", "CITATION_INCOMPLETE"}
    failures = []
    if parse_failures:
        for f in parse_failures:
            detail = f["path"] if f["path"] else None
            failures.append((f["reason_code"], detail))
    else:
        # —— order 6: RUN_MISMATCH (R7.4, character-for-character comparison) ——
        if finding["run_id"] != run_id:
            failures.append(("RUN_MISMATCH", None))

        # —— order 7: per-Citation fixed-order sub-checks ——
        lines_cache = {}
        for i, cit in enumerate(finding["citations"], 1):
            check_deadline(deadline)
            code = g.check_citation(cit["location"], cit["evidence_timestamp"],
                                    cit["excerpt"], os.path.join(cwd, "targets",
                                                                 state["target"]),
                                    entries, lines_cache)
            if code is not None:
                failures.append((code, "citations[%d]" % i))

        # —— order 8: UNCITED_LOCATION (R7.16) ——
        for ref in g.uncited_location_refs(finding):
            failures.append(("UNCITED_LOCATION", g.format_location(ref)))

        # —— order 9: DUPLICATE_SUBMISSION (R10.3, byte-for-byte comparison) ——
        canonical = g.canonical_form(finding)
        for rec in ledger:
            if g.reserialize_canonical_obj(rec["canonical"]) == canonical:
                failures.append(("DUPLICATE_SUBMISSION", rec["finding_id"]))
                break

    # R7.19: on Schema-class failures, output only Schema-class + file-name-class Reason_Codes
    if any(code in schema_codes for code, _ in failures):
        failures = [f for f in failures
                    if f[0] in schema_codes or f[0] in ("FILE_NAME_INVALID", "INBOX_IMMUTABLE")]
    failures = filename_failures + failures

    if not failures:
        accept(run_dir, run_id, ledger, finding, file_name,
               payload.get("session_id"))
        return  # exit 0, no further signal of any kind (R10.2)

    reject(run_dir, run_id, state, failures, content_sha, is_canary, content)


def extract_content(payload, entry):
    """Extract the full content about to be written; (content, None) or (None, status).

    The Tool_Catalog content rule is a candidate pointer list (B1: the observed
    fs_write content parameter name is `$.text`; `$.content` is the
    compatibility fallback). status values:
      "missing"   the catalog declares a content rule but no string value can be extracted → PAYLOAD_INVALID
      "ambiguous" the candidate pointers yielded ≥2 distinct values; which one
                  would actually be written is undeterminable
                  (fail-closed) → UNDETERMINABLE
      "no_spec"   the catalog declares no content rule (e.g. str_replace) → UNDETERMINABLE
    """
    if not isinstance(entry, dict):
        return None, "no_spec"
    specs = entry.get("content")
    if isinstance(specs, str):
        specs = [specs]
    if not isinstance(specs, list):
        return None, "no_spec"
    values = g.extract_string_candidates(payload.get("tool_input"), specs)
    if not values:
        return None, "missing"
    if len(values) > 1:
        return None, "ambiguous"
    return values[0], None


def reject(run_dir, run_id, state, failures, content_sha, is_canary, content_text):
    """Rejection output (R9); Canary special-casing (R2.2/R2.3/R2.7)."""
    codes = [code for code, _ in failures]
    codes_dedup = list(dict.fromkeys(codes))
    agent = "unknown"
    obj = g.strict_json_loads(content_text or "")
    if isinstance(obj, dict) and isinstance(obj.get("agent"), str) \
            and obj["agent"] in g.AUDIT_AGENTS:
        agent = obj["agent"]

    # —— Canary special case: exactly CITATION_MISSING, no System_Reason_Code, run active ——
    if is_canary and codes_dedup == ["CITATION_MISSING"] \
            and state["status"] == "active":
        lock = g.Dir_Lock(run_dir, exclusive=True, deadline=g.monotonic_deadline(3.0))
        if lock.acquire():
            try:
                record = {"run_id": run_id, "at": g.utc_now_str(),
                          "sha256": content_sha}
                try:
                    g.append_once(os.path.join(run_dir, "gate-live.jsonl"),
                                  json.dumps(record, ensure_ascii=False,
                                             separators=(",", ":")))
                except OSError:
                    pass  # recording failed, still reject (R2.8: the Orchestrator sees no record → abort)
            finally:
                lock.release()

    message = g.build_rejection_message(failures)
    lines = message.split("\n")

    # —— Rejection_Record (R9.4; only while the target is active, 9#10) ——
    if state["status"] == "active":
        record = {
            "rejected_at": g.utc_now_str(),
            "reason_codes": codes_dedup,
            "agent": agent,
            "sha256": content_sha,
        }
        lock = g.Dir_Lock(run_dir, exclusive=True, deadline=g.monotonic_deadline(3.0))
        appended = False
        if lock.acquire():
            try:
                try:
                    g.append_once(os.path.join(run_dir, "rejections.jsonl"),
                                  json.dumps(record, ensure_ascii=False,
                                             separators=(",", ":")))
                    appended = True
                except OSError:
                    appended = False
            finally:
                lock.release()
        if not appended:
            lines.append(g.rejection_line("INTERNAL_ERROR"))  # R9.9
    emit(lines)


def accept(run_dir, run_id, _ledger, finding, file_name, session_id):
    """Accept protocol (R10.1/R10.2/R10.5–R10.8; R14.7)."""
    # Accept-append phase entered: the deadline is no longer checked (R10.2)
    lock = g.Dir_Lock(run_dir, exclusive=True, deadline=None)
    if not lock.acquire():
        emit([g.rejection_line("INTERNAL_ERROR")])  # theoretically unreachable (no deadline)
    try:
        # Revalidate inside the lock that state is still active (R14.7) and reread the ledger
        try:
            state, _entries, ledger2 = read_run_artifacts(run_dir, run_id)
        except _Corrupt as exc:
            emit([g.rejection_line("STATE_CORRUPT", exc.path)])
        if state["status"] != "active":
            emit([g.rejection_line("RUN_NOT_ACTIVE")])  # R14.7 (no log write)

        # Recheck duplicates / file-name collisions (R10.8: as if this validation had only started after the accepted records)
        canonical = g.canonical_form(finding)
        for rec in ledger2:
            if g.reserialize_canonical_obj(rec["canonical"]) == canonical:
                emit([g.rejection_line("DUPLICATE_SUBMISSION", rec["finding_id"])])
            if rec["submission_file"] == file_name:
                emit([g.rejection_line("INBOX_IMMUTABLE")])

        serial = len(ledger2) + 1
        if serial > g.MAX_FINDING_SERIAL:
            emit([g.rejection_line("INTERNAL_ERROR")])  # 10#7 boundary
        record = {
            "finding_id": g.finding_id(serial),
            "accepted_at": g.utc_now_str(),
            "submission_file": file_name,
        }
        if isinstance(session_id, str) and session_id != "":
            record["session"] = session_id  # design 3.3: session before canonical
        record["canonical"] = g.canonical_obj(finding)
        try:
            g.append_once(os.path.join(run_dir, "findings.jsonl"),
                          json.dumps(record, ensure_ascii=False,
                                     separators=(",", ":")))
        except OSError:
            emit([g.rejection_line("INTERNAL_ERROR")])  # R8.11: leave nothing partial
    finally:
        lock.release()
    # exit 0, no further Blocking_Signal of any kind (R10.2)


if __name__ == "__main__":
    main()
