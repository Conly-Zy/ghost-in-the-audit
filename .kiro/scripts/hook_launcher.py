#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hook_Launcher — Kiro Hook command entry point (design 4.2).

Usage (cwd must be the workspace root, T3):
    python3 .kiro/scripts/hook_launcher.py pre
    python3 .kiro/scripts/hook_launcher.py post

Reads the payload from stdin (snake_case: session_id / hook_event_name / cwd /
tool_name / tool_input / tool_response, calibrated per T2) and routes by
Tool_Catalog classification:

    pre  → Run_Controller / Citation_Gate / Write_Guard (at most one script)
    post → Evidence_Recorder (only while active)

Exit codes: 2 = Blocking_Signal (stderr carries the Reason_Code); 0 = allow.
Subprocess discipline (design 4.2 step 5): list argv, kill on timeout per the
D9 deadline table, on exit code 2 verify stderr contains a known Reason_Code
or rewrite it as GATE_FAILURE, exit code 0 passes silently, anything else →
GATE_FAILURE; stdout is never used.
"""

import json
import os
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ghostlib as g  # noqa: E402

# Subprocess deadlines (design 9 deadline table: Launcher kill deadlines)
GUARD_LAUNCH_LIMIT = 5.0      # Write_Guard (R11.10)
GATE_LAUNCH_LIMIT = 20.0      # Citation_Gate (R8.5)
CTRL_LIMIT_DEFAULT = 20.0     # Run_Controller: Start/Abort (R8.12)
CTRL_LIMIT_FINALIZE = 50.0    # Run_Controller: Finalize (R8.12)
RECORDER_LAUNCH_LIMIT = 14.0  # post hook 15s catch-all, leaving 1s of headroom
SCAN_LOCK_WAIT = 5.0          # L_G shared lock wait (counts toward the Launcher itself)

LAUNCH_SCRIPTS = {
    "Citation_Gate": "citation_gate.py",
    "Write_Guard": "write_guard.py",
    "Run_Controller": "run_controller.py",
    "Evidence_Recorder": "evidence_recorder.py",
}


def fail(code, detail=None):
    """Emit a Launcher-own message with exit code 2 (only D8 template whitelist content, R8.13)."""
    sys.stderr.write(g.rejection_line(code, detail) + "\n")
    sys.stderr.flush()
    sys.exit(2)


def fail_gate_failure(script_label, message=None):
    detail = "[%s]" % script_label if script_label else None
    line = g.rejection_line("GATE_FAILURE", detail)
    if message:
        line += " " + message
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    sys.exit(2)


def workspace_self_check():
    """Missing runs/, control/, or ghostlib → GATE_FAILURE (design 4.1)."""
    root = os.getcwd()
    for rel in ("runs", "control", os.path.join(".kiro", "scripts", "ghostlib.py")):
        if not os.path.exists(os.path.join(root, rel)):
            fail("GATE_FAILURE", "[%s]" % "hook_launcher")


def scan_active():
    """Scan under the L_G shared lock; corrupt → STATE_CORRUPT exit 2 (R15.6)."""
    deadline = g.monotonic_deadline(SCAN_LOCK_WAIT)
    runs_dir = os.path.join(os.getcwd(), "runs")
    lock = g.Dir_Lock(runs_dir, exclusive=False, deadline=deadline)
    if not lock.acquire():
        fail("GATE_FAILURE", "[%s]" % "hook_launcher")
    try:
        scan = g.scan_runs(runs_dir)
    finally:
        lock.release()
    if scan["corrupt_paths"]:
        # STATE_CORRUPT: <paths…> <description> (design 8 template)
        sys.stderr.write("STATE_CORRUPT: %s %s\n" % (
            " ".join(scan["corrupt_paths"]), g.REASON_DESCRIPTIONS["STATE_CORRUPT"]))
        sys.exit(2)
    return scan


def load_catalog():
    """Load the Tool_Catalog (corrupt while active → GATE_FAILURE; no effect without an active run)."""
    text = g.read_text(os.path.join(SCRIPT_DIR, "tool_catalog.json"))
    if text is None:
        return None
    data = g.strict_json_loads(text)
    if not isinstance(data, list):
        return None
    catalog = {}
    for entry in data:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        klass = entry.get("class")
        if not isinstance(name, str) or klass not in ("read", "write"):
            return None
        catalog[name] = entry
    return catalog


def parse_payload(raw):
    """Strict parse of the stdin payload → dict or None (design 4.2 step 2)."""
    payload = g.strict_json_loads(raw)
    if payload is None or not isinstance(payload, dict):
        return None
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    if "tool_input" not in payload:
        return None
    return payload


def extract_paths(tool_input, entry):
    """Extract the list of path strings per the catalog paths rules; any missing/non-string → (None, False).

    Returns (paths, determinable).
    """
    specs = entry.get("paths")
    if not isinstance(specs, list) or not specs:
        return None, False
    paths = []
    for spec in specs:
        v = g.json_pointer(tool_input, spec)
        if not isinstance(v, str) or v == "":
            return None, False
        paths.append(v)
    return paths, True


def classify_write_paths(cwd, paths):
    """Classify determinable write paths (design 4.2 routing table).

    Returns "inbox" | "control" | "mixed" | "other"; for inboxes also returns
    {inbox_dir: [direct child file names]} (only when all land in one Inbox).
    """
    resolved = []
    for p in paths:
        rp = g.resolve_real(os.path.join(cwd, p) if not os.path.isabs(p) else p)
        if rp is None:
            return "mixed", None
        resolved.append(rp)

    control_root = os.path.join(cwd, "control")
    runs_root = os.path.join(cwd, "runs")

    hits_control = [p for p in resolved if g.path_within(p, control_root)]
    if hits_control:
        if len(hits_control) == len(resolved):
            return "control", None
        return "mixed", None

    inbox_map = {}
    for p in resolved:
        rel = None
        if g.path_within(p, runs_root):
            after = os.path.relpath(p, runs_root)
            parts = after.split(os.sep)
            if len(parts) >= 3 and parts[1] == "inbox":
                run_dir = os.path.join(runs_root, parts[0])
                inbox_dir = os.path.join(run_dir, "inbox")
                inbox_map.setdefault(inbox_dir, []).append(p)
                continue
        return "mixed", None  # any path not inside an Inbox → mixed scope

    if len(inbox_map) == 1:
        inbox_dir = next(iter(inbox_map))
        for p in inbox_map[inbox_dir]:
            if os.path.relpath(p, inbox_dir).count(os.sep) != 0:
                return "mixed", None  # not an Inbox direct child file
            if not g.SUBMISSION_NAME_RE.match(os.path.basename(p)) \
                    and os.path.basename(p) != g.CANARY_FILE_NAME:
                return "mixed", None  # outside the reserved names (e.g. AGENTS.md), not treated as a Submission
        return "inbox", inbox_map
    return "mixed", None


def write_payload_file(payload_text):
    fd, tmp = tempfile.mkstemp(prefix="ghost-payload-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload_text.encode("utf-8"))
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def run_script(script_key, argv, timeout, cwd):
    """Subprocess discipline (design 4.2 step 5). Returns None to allow (caller exits 0 directly).

    In the Blocking_Signal case this process ends immediately with exit code 2.
    """
    script_path = os.path.join(SCRIPT_DIR, LAUNCH_SCRIPTS[script_key])
    argv = [sys.executable, script_path] + argv
    try:
        proc = subprocess.run(
            argv, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        fail("TIMEOUT", "[%s]" % script_key)
    except OSError:
        fail_gate_failure(script_key)
    stderr_text = ""
    try:
        stderr_text = proc.stderr.decode("utf-8", errors="replace")
    except Exception:
        pass
    if proc.returncode == 0:
        return None
    if proc.returncode == 2:
        if _has_known_reason_code(stderr_text):
            sys.stderr.write(stderr_text)
            sys.stderr.flush()
            sys.exit(2)
        fail_gate_failure(script_key)
    fail_gate_failure(script_key)


def _has_known_reason_code(stderr_text):
    for line in stderr_text.splitlines():
        line = line.strip()
        for sep in (" ", ":"):
            idx = line.find(sep)
            if idx > 0:
                code = line[:idx].rstrip(":")
                if code in g.KNOWN_REASON_CODES:
                    return True
    return False


def route_pre():
    workspace_self_check()
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    scan = scan_active()
    active = scan["active_run_id"] is not None
    cwd = os.getcwd()

    payload = parse_payload(raw)
    if payload is None:
        if active:
            # invalid payload with an active run → hand to Write_Guard (which raises PAYLOAD_INVALID, R11.12)
            tmp = write_payload_file(raw if raw.strip() else "")
            try:
                run_script("Write_Guard", [tmp], GUARD_LAUNCH_LIMIT, cwd)
            finally:
                os.unlink(tmp)
        return  # no active run → allow (R15.5)

    catalog = load_catalog()
    entry = catalog.get(payload["tool_name"]) if catalog else None

    if entry is None:
        # unknown tool (including shell/web classes): active → Write_Guard; no active → allow
        if active:
            tmp = write_payload_file(raw)
            try:
                run_script("Write_Guard", [tmp], GUARD_LAUNCH_LIMIT, cwd)
            finally:
                os.unlink(tmp)
        return
    if entry["class"] == "read":
        return  # read-only tool: evidence recording happens in post

    # write tool: extract paths
    paths, determinable = extract_paths(payload.get("tool_input"), entry)
    if not determinable:
        if active:
            tmp = write_payload_file(raw)
            try:
                run_script("Write_Guard", [tmp], GUARD_LAUNCH_LIMIT, cwd)
            finally:
                os.unlink(tmp)
        return  # no active run → allow (R15.5)

    kind, inbox_map = classify_write_paths(cwd, paths)
    if kind == "control":
        request_file = g.resolve_real(
            os.path.join(cwd, paths[0]) if not os.path.isabs(paths[0]) else paths[0])
        limit = CTRL_LIMIT_FINALIZE if os.path.basename(request_file) == "finalize.json" \
            else CTRL_LIMIT_DEFAULT
        tmp = write_payload_file(raw)
        try:
            run_script("Run_Controller", [request_file, tmp], limit, cwd)
        finally:
            os.unlink(tmp)
        return
    if kind == "inbox":
        tmp = write_payload_file(raw)
        try:
            run_script("Citation_Gate", [tmp], GATE_LAUNCH_LIMIT, cwd)
        finally:
            os.unlink(tmp)
        return
    # mixed / other → Write_Guard (mixed scope fails closed, design 4.2 X1)
    if active or _touches_run_artifact(cwd, paths):
        tmp = write_payload_file(raw)
        try:
            run_script("Write_Guard", [tmp], GUARD_LAUNCH_LIMIT, cwd)
        finally:
            os.unlink(tmp)
    return


def _touches_run_artifact(cwd, paths):
    """Without an active run, hand to Write_Guard only when Run_Artifacts / reserved paths are involved (R15.1, D5.4).

    Determined against the set of known file names inside runs/<run>/; other
    runs/ paths pass (forged records get rejected later by STATE_CORRUPT /
    RUN_ALREADY_ACTIVE, design 14 O2).
    """
    runs_root = os.path.join(cwd, "runs")
    reserved = {"state.json", "evidence.jsonl", "findings.jsonl",
                "rejections.jsonl", "gate-live.jsonl", "report.md", "report.json"}
    for p in paths:
        rp = g.resolve_real(os.path.join(cwd, p) if not os.path.isabs(p) else p)
        if rp is None:
            continue
        if g.path_within(rp, runs_root):
            rel = os.path.relpath(rp, runs_root)
            parts = rel.split(os.sep)
            if len(parts) >= 2 and parts[-1] in reserved:
                return True
    return False


def route_post():
    workspace_self_check()
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    scan = scan_active()
    if scan["active_run_id"] is None:
        return  # R15.2: no active run, no writes of any kind
    tmp = write_payload_file(raw)
    cwd = os.getcwd()
    script_path = os.path.join(SCRIPT_DIR, LAUNCH_SCRIPTS["Evidence_Recorder"])
    try:
        proc = subprocess.run(
            [sys.executable, script_path, tmp], cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=RECORDER_LAUNCH_LIMIT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return  # post cannot block; missing a record is acceptable degradation (missing evidence → later EVIDENCE_NOT_FOUND)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if proc.returncode == 2:
        stderr_text = proc.stderr.decode("utf-8", errors="replace")
        if _has_known_reason_code(stderr_text):
            sys.stderr.write(stderr_text)
            sys.stderr.flush()
            sys.exit(2)  # STATE_CORRUPT etc. visible externally (a post exit code does not block the tool)
    return


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "pre":
        route_pre()
    elif mode == "post":
        route_post()
    else:
        fail("GATE_FAILURE", None)
    sys.exit(0)


if __name__ == "__main__":
    main()
