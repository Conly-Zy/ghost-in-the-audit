#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write_Guard — blocks the agent from modifying Protected_Paths (design 5.4).

Launched by Hook_Launcher: write_guard.py <payload_json absolute path>

Check order: invalid payload → PAYLOAD_INVALID (only while active); tools
outside the catalog / unresolvable paths / undeterminable Protected_Path →
UNDETERMINABLE (only while active); otherwise classify by path:
  active: inside the active run's Target directory ∪ Toolkit_Configuration
          (.kiro/{agents,hooks,steering,scripts}/**) ∪ runs/ paths outside
          each Inbox → PROTECTED_PATH
  no active: only Run_Artifacts and reserved paths are blocked
Internal deadline 4.5s (raises TIMEOUT itself on expiry; Launcher deadline 5s).
Resolved_Paths are always realpath'd; case-insensitive filesystems compare
with casefold.
"""

import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ghostlib as g  # noqa: E402

INTERNAL_DEADLINE = 4.5
MAX_LISTED_PATHS = 50

RESERVED_ARTIFACT_NAMES = {"state.json", "evidence.jsonl", "findings.jsonl",
                           "rejections.jsonl", "gate-live.jsonl",
                           "report.md", "report.json"}
TOOLKIT_CONFIG_SUBDIRS = ("agents", "hooks", "steering", "scripts")


class Guard_Exit(Exception):
    def __init__(self, lines, code=2):
        super().__init__(code)
        self.lines = lines
        self.code = code


def emit(lines, code=2):
    raise Guard_Exit(lines, code)


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        emit([g.rejection_line("TIMEOUT")])


def resolve_one(cwd, raw):
    if not isinstance(raw, str) or raw == "":
        return None
    path = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
    return g.resolve_real(path)


def protected_in_active(cwd, resolved, state, runs_root):
    """Protected_Path determination in active mode (R11.1)."""
    ci = g._symlink_case_insensitive()
    # 1. inside the active run's Target directory
    target_real = g.resolve_real(os.path.join(cwd, "targets", state["target"]))
    if target_real and g.path_within(resolved, target_real, casefold=ci):
        return True
    # 2. Toolkit_Configuration
    for sub in TOOLKIT_CONFIG_SUBDIRS:
        root = g.resolve_real(os.path.join(cwd, ".kiro", sub))
        if root and g.path_within(resolved, root, casefold=ci):
            return True
    # 3. runs/ paths outside each Inbox
    if g.path_within(resolved, runs_root, casefold=ci):
        rel = os.path.relpath(resolved, runs_root)
        parts = rel.split(os.sep)
        if not (len(parts) >= 2 and parts[1] == "inbox"):
            return True  # path not inside an Inbox (includes Run_Artifacts and reserved paths)
        if len(parts) == 2:
            return True  # the Inbox directory itself (rename/delete an Inbox)
    return False


def protected_without_active(cwd, resolved, runs_root):
    """No-active mode: block only Run_Artifacts and reserved paths (R11.2 / D5.4)."""
    ci = g._symlink_case_insensitive()
    if not g.path_within(resolved, runs_root, casefold=ci):
        return False
    rel = os.path.relpath(resolved, runs_root)
    parts = rel.split(os.sep)
    if len(parts) >= 2 and parts[-1] in RESERVED_ARTIFACT_NAMES:
        return True
    return False


def dir_contains_protected(cwd, dir_path, state, runs_root, deadline, active):
    """Rename/delete a directory: does it contain any protected path? (R11.1/R11.2)"""
    count = 0
    for root, dirs, files in os.walk(dir_path):
        check_deadline(deadline)
        for name in dirs + files:
            full = os.path.join(root, name)
            hit = protected_in_active(cwd, full, state, runs_root) if active \
                else protected_without_active(cwd, full, runs_root)
            if hit:
                return True
            count += 1
            if count > 200000:
                emit([g.rejection_line("TIMEOUT")])  # huge directory tree: cannot decide within the deadline
    return False


def main():
    cwd = os.getcwd()
    deadline = g.monotonic_deadline(INTERNAL_DEADLINE)
    try:
        run(cwd, deadline)
    except Guard_Exit as exc:
        sys.stderr.write("\n".join(exc.lines) + "\n")
        sys.stderr.flush()
        sys.exit(exc.code)
    sys.exit(0)


def run(cwd, deadline):
    payload_text = g.read_text(sys.argv[1]) if len(sys.argv) > 1 else None
    payload = g.strict_json_loads(payload_text) if payload_text else None
    valid = isinstance(payload, dict) and isinstance(payload.get("tool_name"), str) \
        and isinstance(payload.get("tool_input"), dict)

    scan = g.scan_runs(os.path.join(cwd, "runs"))
    if scan["corrupt_paths"]:
        emit([g.rejection_line("STATE_CORRUPT", " ".join(scan["corrupt_paths"]))])
    active = scan["active_run_id"] is not None
    state = None
    if active:
        st_text = g.read_text(os.path.join(scan["active_dir"], "state.json"))
        state = g.parse_state(st_text) if st_text else None
        if state is None:
            emit([g.rejection_line("STATE_CORRUPT",
                                   os.path.join(scan["active_dir"], "state.json"))])

    if not valid:
        if active:
            emit([g.rejection_line("PAYLOAD_INVALID")])  # 11#12
        sys.exit(0)  # no active run → allow (15#5)

    check_deadline(deadline)

    catalog_text = g.read_text(os.path.join(SCRIPT_DIR, "tool_catalog.json"))
    data = g.strict_json_loads(catalog_text) if catalog_text else None
    entry = None
    if isinstance(data, list):
        for e in data:
            if isinstance(e, dict) and e.get("name") == payload["tool_name"]:
                entry = e
                break
    if entry is None or entry.get("class") != "write":
        if active:
            emit([g.rejection_line("UNDETERMINABLE")])  # 11#3 (tool outside the catalog / shell)
        sys.exit(0)

    specs = entry.get("paths")
    if not isinstance(specs, list) or not specs:
        if active:
            emit([g.rejection_line("UNDETERMINABLE")])
        sys.exit(0)
    resolved_paths = []
    for spec in specs:
        raw = g.json_pointer(payload["tool_input"], spec)
        rp = resolve_one(cwd, raw)
        if rp is None:
            if active:
                emit([g.rejection_line("UNDETERMINABLE")])  # path undeterminable
            sys.exit(0)
        resolved_paths.append(rp)

    check_deadline(deadline)

    runs_root = g.resolve_real(os.path.join(cwd, "runs")) or os.path.join(cwd, "runs")
    blocked = []
    op = entry.get("op")
    for rp in resolved_paths:
        check_deadline(deadline)
        if op in ("delete",) and os.path.isdir(rp):
            # delete/rename a directory: block if it contains a protected path (R11.1/R11.2)
            try:
                hit = dir_contains_protected(cwd, rp, state, runs_root, deadline, active)
            except Guard_Exit:
                raise
            if hit:
                blocked.append(rp)
            continue
        hit = protected_in_active(cwd, rp, state, runs_root) if active \
            else protected_without_active(cwd, rp, runs_root)
        if hit:
            blocked.append(rp)

    if not blocked:
        sys.exit(0)  # R11.9 allow

    lines = [g.rejection_line("PROTECTED_PATH", p) for p in blocked[:MAX_LISTED_PATHS]]
    if len(blocked) > MAX_LISTED_PATHS:
        lines.append(g.TRUNCATION_LINE_TEMPLATE.format(
            n=len(blocked) - MAX_LISTED_PATHS))
    emit(lines)


if __name__ == "__main__":
    main()
