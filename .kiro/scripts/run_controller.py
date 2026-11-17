#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run_Controller — processes Control_Request, maintains the Run_State_Record (design 5.1).

Launched by Hook_Launcher:
    run_controller.py <request_file absolute path> <payload_json absolute path>

REQUEST_INVALID precheck (R11.13) → Start / Abort / Finalize flows.
All state writes: L_R exclusive → temp file + os.replace (design 2.3).
Deadlines (design 9): Start/Abort internal 18s; Finalize total 48s (render 40s).
"""

import json
import os
import shutil
import sys
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ghostlib as g  # noqa: E402

START_DEADLINE = 18.0
ABORT_DEADLINE = 18.0
FINALIZE_DEADLINE = 48.0
RENDER_DEADLINE = 40.0

RUN_ARTIFACT_NAMES = ("state.json", "evidence.jsonl", "findings.jsonl",
                      "rejections.jsonl", "gate-live.jsonl")
MAX_AUTO_LOADED_LISTED = 100
MAX_SYMLINK_LISTED = 100


def out_lines(lines):
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()
    sys.exit(2)


def fail(code, detail=None):
    out_lines([g.rejection_line(code, detail)])


def internal_error(detail=None):
    fail("INTERNAL_ERROR", detail)


# ============================================================
# REQUEST_INVALID precheck (R11.13; design 5.1)
# ============================================================

def precheck_request(request_file, cwd):
    control_root = g.resolve_real(os.path.join(cwd, "control")) or \
        os.path.join(cwd, "control")
    if not os.path.isabs(request_file):
        request_file = os.path.join(cwd, request_file)
    resolved = g.resolve_real(request_file)
    if resolved is None:
        resolved = request_file
    if not g.path_within(resolved, control_root):
        fail("REQUEST_INVALID")
    rel = os.path.relpath(resolved, control_root)
    if os.sep in rel or rel in (".", ".."):
        fail("REQUEST_INVALID")  # not a direct child file of control
    if rel not in g.CONTROL_FILE_NAMES:
        fail("REQUEST_INVALID")
    return resolved, rel


def load_op(payload):
    """Tool op determination; op=delete etc. beyond R11.13 semantics → INTERNAL_ERROR (design 5.1)."""
    tool_name = payload.get("tool_name")
    text = g.read_text(os.path.join(SCRIPT_DIR, "tool_catalog.json"))
    data = g.strict_json_loads(text) if text is not None else None
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("name") == tool_name:
                return entry.get("op")
    return None


# Control_Request is written via fs_write; as observed (docs T5/B1) its content
# parameter is named `text`, and `content` is the compatibility fallback this
# repo's tests have always driven. If both are present and differ, the copy
# that would actually be written is undeterminable → fail-closed.
REQUEST_BODY_CONTENT_SPECS = ("$.text", "$.content")


def extract_request_body(payload):
    """Extract the full content about to be written from the payload; None if unavailable or candidates conflict (fail-closed)."""
    values = [v for v in g.extract_string_candidates(
        payload.get("tool_input"), list(REQUEST_BODY_CONTENT_SPECS)) if v != ""]
    if len(values) != 1:
        return None
    return values[0]


# ============================================================
# Start (R1; design 5.1 checklist)
# ============================================================

def scan_target_tree(target_dir):
    """Single scandir recursion: regular file count + Auto_Loaded_Context + symlink escapes.

    Returns (regular_files>0, auto_loaded_sorted, escapes_sorted).
    """
    target_real = g.resolve_real(target_dir)
    ci = g._symlink_case_insensitive()
    has_file = False
    auto_loaded = []
    escapes = []
    skip_paths = set()

    def rel_posix(p):
        return os.path.relpath(p, target_dir).replace(os.sep, "/")

    def check_symlink(full):
        # A symlink's own name counts toward Auto_Loaded (design 5.1 order 5)
        name = os.path.basename(full)
        if name.casefold() == "agents.md" or name == ".kiro":
            auto_loaded.append(rel_posix(full))
        # Escape determination (R1.6): regardless of whether the target exists
        try:
            resolved = os.path.realpath(full, strict=True)
        except OSError as e:
            import errno
            if getattr(e, "errno", None) == errno.ELOOP:
                escapes.append(rel_posix(full))
                return
            resolved = os.path.realpath(full)
        if not g.path_within(resolved, target_real, casefold=ci):
            escapes.append(rel_posix(full))

    def walk(dir_path):
        nonlocal has_file
        try:
            with os.scandir(dir_path) as it:
                entries = list(it)
        except OSError:
            return
        for e in entries:
            full = e.path
            if full in skip_paths:
                continue
            if e.is_symlink():
                check_symlink(full)
                continue  # do not descend into directory symlinks
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name == ".kiro":  # directory named .kiro (Glossary)
                        auto_loaded.append(rel_posix(full))
                    walk(full)
                elif e.is_file(follow_symlinks=False):
                    has_file = True
                    name = e.name
                    if name.casefold() == "agents.md":
                        auto_loaded.append(rel_posix(full))
            except OSError:
                continue

    walk(target_dir)
    auto_loaded.sort()
    escapes.sort()
    return has_file, auto_loaded, escapes


def start_flow(request_file, payload, cwd, deadline):
    body = g.strict_json_loads(extract_request_body(payload) or "")
    if not isinstance(body, dict):
        body = {}
    target = body.get("target")
    focus = body.get("focus")

    # O1 structural defect → INTERNAL_ERROR fail-closed
    allowed = {"target", "focus"}
    if isinstance(body, dict) and not set(body.keys()) <= allowed:
        internal_error()
    if "focus" in body and not isinstance(focus, str):
        internal_error()

    target_valid = True
    problems = []  # [(code, detail)]

    if not isinstance(target, str) or target == "" or "/" in target or "\\" in target \
            or target in (".", "..") or any(ord(c) <= 0x1F or 0x7F <= ord(c) <= 0x9F
                                            for c in target):
        target_valid = False
        problems.append(("TARGET_INVALID", target if isinstance(target, str) else None))
    else:
        targets_root = os.path.join(cwd, "targets")
        names = []
        try:
            names = os.listdir(targets_root)
        except OSError:
            pass
        entry = os.path.join(targets_root, target)
        if target not in names or not os.path.isdir(entry) or os.path.islink(entry):
            # byte-for-byte same name + not a symlink (A22)
            target_valid = False
            problems.append(("TARGET_INVALID", target))

    # 2 STATE_CORRUPT scan (includes unknown keys) — always executed
    runs_dir = os.path.join(cwd, "runs")
    scan = g.scan_runs(runs_dir)
    if scan["corrupt_paths"]:
        problems.append(("STATE_CORRUPT", " ".join(scan["corrupt_paths"])))
    # 3 RUN_ALREADY_ACTIVE
    if scan["active_run_id"] is not None:
        problems.append(("RUN_ALREADY_ACTIVE", scan["active_run_id"]))

    target_dir = os.path.join(cwd, "targets", target) if isinstance(target, str) and target \
        else None
    if target_valid and target_dir is not None:
        has_file, auto_loaded, escapes = scan_target_tree(target_dir)
        # 4 TARGET_EMPTY
        if not has_file:
            problems.append(("TARGET_EMPTY", None))
        # 5 AUTO_LOADED_CONTEXT_PRESENT
        if auto_loaded:
            listed = " ".join(auto_loaded[:MAX_AUTO_LOADED_LISTED])
            problems.append(("AUTO_LOADED_CONTEXT_PRESENT",
                             "count=%d %s" % (len(auto_loaded), listed)))
        # 6 SYMLINK_ESCAPE
        if escapes:
            listed = " ".join(escapes[:MAX_SYMLINK_LISTED])
            problems.append(("SYMLINK_ESCAPE",
                             "count=%d %s" % (len(escapes), listed)))

    if problems:
        out_lines([g.rejection_line(code, detail) for code, detail in problems])

    # Create protocol (design 5.1): L_G exclusive → rescan → generate ID → staging tree → rename
    lock = g.Dir_Lock(runs_dir, exclusive=True, deadline=deadline)
    if not lock.acquire():
        fail("TIMEOUT")
    try:
        scan = g.scan_runs(runs_dir)
        if scan["corrupt_paths"]:
            out_lines([g.rejection_line("STATE_CORRUPT", " ".join(scan["corrupt_paths"]))])
        if scan["active_run_id"] is not None:
            fail("RUN_ALREADY_ACTIVE", scan["active_run_id"])
        run_id = g.new_run_id()
        for _ in range(10):
            if not os.path.exists(os.path.join(runs_dir, run_id)):
                break
            run_id = g.new_run_id()
        else:
            internal_error()
        tmp_dir = os.path.join(runs_dir, ".tmp-" + str(uuid.uuid4()))
        try:
            os.makedirs(os.path.join(tmp_dir, "inbox"))
            for name in ("evidence.jsonl", "findings.jsonl", "rejections.jsonl"):
                with open(os.path.join(tmp_dir, name), "wb"):
                    pass
            state = {"run_id": run_id, "target": target}
            if isinstance(focus, str) and focus != "":
                state["focus"] = focus
            state["status"] = "active"
            state["started_at"] = g.utc_now_str()
            g.atomic_write(os.path.join(tmp_dir, "state.json"),
                           json.dumps(state, ensure_ascii=False, separators=(",", ":")))
            os.rename(tmp_dir, os.path.join(runs_dir, run_id))
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
    finally:
        lock.release()
    sys.exit(0)


# ============================================================
# Abort (R14; design 5.1)
# ============================================================

def load_run_state(run_dir):
    """Read and strictly parse state.json; (state, None) or (None, error code)."""
    text = g.read_text(os.path.join(run_dir, "state.json"))
    if text is None:
        return None, "STATE_CORRUPT"
    state = g.parse_state(text)
    if state is None:
        return None, "STATE_CORRUPT"
    return state, None


def abort_flow(request_file, payload, cwd, deadline):
    body = g.strict_json_loads(extract_request_body(payload) or "")
    run_id = body.get("run_id") if isinstance(body, dict) else None
    if not isinstance(run_id, str) or not g.RUN_ID_RE.match(run_id):
        fail("RUN_NOT_ACTIVE")
    run_dir = os.path.join(cwd, "runs", run_id)
    if not os.path.isdir(run_dir):
        fail("RUN_NOT_ACTIVE")

    lock = g.Dir_Lock(run_dir, exclusive=True, deadline=deadline)
    if not lock.acquire():
        fail("TIMEOUT")
    try:
        state, err = load_run_state(run_dir)
        if err is not None:
            fail(err)
        if state["status"] != "active":
            fail("RUN_NOT_ACTIVE")
        new_state = dict(state)
        new_state["status"] = "aborted"
        new_state["finished_at"] = g.utc_now_str()
        try:
            g.atomic_write(os.path.join(run_dir, "state.json"),
                           json.dumps(new_state, ensure_ascii=False, separators=(",", ":")))
        except OSError:
            # R14.5: keep the pre-update content
            fail("INTERNAL_ERROR")
    finally:
        lock.release()
    sys.exit(0)


# ============================================================
# Finalize (R2.5/2.9, R13, R14.3; design 5.1)
# ============================================================

def validate_finalize_body(body):
    """Finalize_Request structural validation (O1: defects without a dedicated code → INTERNAL_ERROR)."""
    if not isinstance(body, dict):
        return False
    if not set(body.keys()) <= {"run_id", "completion", "summary_statements"}:
        return False
    comp = body.get("completion")
    if not isinstance(comp, dict):
        return False
    for k, v in comp.items():
        if k not in g.AUDIT_AGENTS or v not in ("completed", "failed"):
            return False
    ss = body.get("summary_statements", [])
    if not isinstance(ss, list) or not all(isinstance(s, str) for s in ss):
        return False
    return True


def finalize_flow(request_file, payload, cwd, deadline):
    import report_renderer

    body = g.strict_json_loads(extract_request_body(payload) or "")
    run_id = body.get("run_id") if isinstance(body, dict) else None
    if not isinstance(run_id, str) or not g.RUN_ID_RE.match(run_id):
        fail("RUN_NOT_ACTIVE")
    run_dir = os.path.join(cwd, "runs", run_id)
    if not os.path.isdir(run_dir):
        fail("RUN_NOT_ACTIVE")

    lock = g.Dir_Lock(run_dir, exclusive=True, deadline=deadline)
    if not lock.acquire():
        fail("TIMEOUT")
    try:
        def cleanup_and_emit(lines):
            # R13.17: status unchanged, no report.* (including leftover tmp)
            for name in ("report.md", "report.json",
                         "report.md.tmp", "report.json.tmp"):
                try:
                    os.unlink(os.path.join(run_dir, name))
                except OSError:
                    pass
            out_lines(lines)

        state, err = load_run_state(run_dir)
        if err is not None:
            cleanup_and_emit([g.rejection_line(err)])
        if state["status"] != "active":
            cleanup_and_emit([g.rejection_line("RUN_NOT_ACTIVE")])
        if not validate_finalize_body(body):
            cleanup_and_emit([g.rejection_line("INTERNAL_ERROR")])

        # GATE_NOT_VERIFIED (R2.5)
        gate_verified = False
        gt = g.read_text(os.path.join(run_dir, "gate-live.jsonl"))
        if gt is not None:
            try:
                for line in gt.split("\n"):
                    if line == "":
                        continue
                    rec = g.StrictJSONParser(line).parse()
                    if (isinstance(rec, dict) and isinstance(rec.get("run_id"), str)
                            and rec["run_id"] == run_id):
                        gate_verified = True
                        break
            except g._JSONError:
                gate_verified = False
        if not gate_verified:
            cleanup_and_emit([g.rejection_line("GATE_NOT_VERIFIED")])

        # RECORDER_NOT_VERIFIED (R2.9)
        et = g.read_text(os.path.join(run_dir, "evidence.jsonl"))
        entries, corrupt = g.parse_evidence_log(et)
        if entries is None or len(entries) == 0:
            cleanup_and_emit([g.rejection_line("RECORDER_NOT_VERIFIED")])

        finished_at = g.utc_now_str()
        try:
            report_renderer.render_and_write(
                run_dir=run_dir, cwd=cwd, state=state,
                finalize_body=body, finished_at=finished_at,
                deadline=g.monotonic_deadline(RENDER_DEADLINE))
        except report_renderer.Renderer_Error as exc:
            cleanup_and_emit(exc.lines)
        except Exception:
            cleanup_and_emit([g.rejection_line("REPORT_FAILED")])

        # Report written under final names → atomically swap state.json to completed (R13.15)
        new_state = dict(state)
        new_state["status"] = "completed"
        new_state["finished_at"] = finished_at
        try:
            g.atomic_write(os.path.join(run_dir, "state.json"),
                           json.dumps(new_state, ensure_ascii=False, separators=(",", ":")))
        except OSError:
            cleanup_and_emit([g.rejection_line("REPORT_FAILED")])
    finally:
        lock.release()
    sys.exit(0)


# ============================================================
# Entry point
# ============================================================

def main():
    if len(sys.argv) < 3:
        internal_error()
    request_file = sys.argv[1]
    payload_path = sys.argv[2]
    cwd = os.getcwd()

    try:
        payload_text = g.read_text(payload_path)
    except Exception:
        payload_text = None
    payload = g.strict_json_loads(payload_text) if payload_text else None
    if not isinstance(payload, dict):
        internal_error()

    request_file, request_name = precheck_request(request_file, cwd)

    op = load_op(payload)
    if op in ("delete", "move"):
        # Deleting/renaming control files is beyond R11.13 semantics → fail-closed (design 5.1)
        internal_error()
    body_text = extract_request_body(payload)
    if body_text is None:
        internal_error()

    if request_name == "start.json":
        deadline = g.monotonic_deadline(START_DEADLINE)
        start_flow(request_file, payload, cwd, deadline)
    elif request_name == "abort.json":
        deadline = g.monotonic_deadline(ABORT_DEADLINE)
        abort_flow(request_file, payload, cwd, deadline)
    else:
        deadline = g.monotonic_deadline(FINALIZE_DEADLINE)
        finalize_flow(request_file, payload, cwd, deadline)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # Any unexpected exception → fail-closed (spirit of R8.9; report INTERNAL_ERROR first)
        try:
            internal_error()
        except BaseException:
            sys.exit(2)
