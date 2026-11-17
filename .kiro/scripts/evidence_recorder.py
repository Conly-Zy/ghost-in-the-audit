#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evidence_Recorder — records lines returned by Read_Operations as evidence (design 5.2).

Launched by Hook_Launcher (post): evidence_recorder.py <payload_json absolute path>

Flow: active-run determination → extract {line_no → line_text} per Tool_Catalog
response rules (shape undeterminable → 0 entries, R4.3) → line filtering and
merging (R4.1/R4.2) → path normalization (R4.13) → L_R exclusive critical
section (read whole log, strictly increasing timestamps, single write()
append, R4.5–R4.9).

Observed read_file response shape (docs/platform-notes.md T5 / runbook B2):
string `<file …>\n<content>\n…\n</content>\n</file>` (optional Note); the
starting line is determined by tool_input.offset (0-based);
parse_read_file_text / start_line_of_read parse accordingly, returning
0 entries on mismatch (fail-closed). The old dict shape (content/start_line
keys) stays compatible.

Internal deadline 10s (design 9); lock-wait timeout / missing log → 0 entries,
exit 0 (acceptable degradation); log exists but some line is unparseable →
exit code 2 STATE_CORRUPT (design 5.2, task 4.4).

Note: design 2.3 marks the Recorder's L_R as "shared", but R4.5 (strictly
increasing) and R4.8 (overlapping handling exactly once, no interleaving)
require read-compute-write mutual exclusion, so the critical section uses an
exclusive lock — the lock order is still L_G → L_R, no deadlock. Recorded in
docs/platform-notes.md.
"""

import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ghostlib as g  # noqa: E402

INTERNAL_DEADLINE = 10.0

# Common key spellings for response extraction (revised per tool_catalog.json after T2 observation; PENDING)
_CONTENT_KEYS = ("content", "text", "lines_content", "file_content")
_LINES_KEYS = ("lines", "lines_text", "content_lines")
_PATH_KEYS = ("path", "file", "file_path", "filepath")
_MATCH_LIST_KEYS = ("matches", "results", "items", "lines")
_MATCH_FILE_KEYS = ("file", "path", "file_path", "filepath")
_MATCH_LINE_KEYS = ("line_number", "line", "lineNumber", "start_line")
_MATCH_TEXT_KEYS = ("text", "content", "line_text", "preview")


def exit_silent():
    sys.exit(0)


_FILE_OPEN_RE = re.compile(r"<file(?:\s[^>]*)?>\Z")
_FILE_CLOSE_RE = re.compile(r"\n</file>(?:\n+Note: To continue, use offset [0-9]+\.)?\n*\Z")


def parse_read_file_text(text):
    """Parse the observed read_file response string shape (docs/platform-notes.md T5, runbook B2):

        <file name="…" language="…" >\n<content>\n…file lines…\n</content>\n</file>
        [Note: To continue, use offset N.]

    Returns the file text inside the `<content>` wrapper (without the outer
    tag lines); None on mismatch (shape undeterminable → caller records
    0 entries, R4.3 fail-closed). Line numbers exclude the 2-line offset of
    the tag lines; the starting line is determined by tool_input.offset
    (0-based, see start_line_of_read). After the closing tag only the observed
    pagination hint and trailing newlines are accepted. If a standalone
    `</content>` line appears in the body, the response is not used as evidence.
    """
    if not isinstance(text, str):
        return None
    nl = text.find("\n")
    if nl < 0 or not text.startswith("<file"):
        return None
    if _FILE_OPEN_RE.match(text[:nl]) is None:
        return None
    body = text[nl + 1:]
    if not body.startswith("<content>\n"):
        return None
    body = body[len("<content>\n"):]
    end = body.find("\n</content>")
    if end < 0:
        return None
    if _FILE_CLOSE_RE.match(body, end + len("\n</content>")) is None:
        return None
    return body[:end]


def start_line_of_read(tool_input):
    """read_file's starting line (observed semantics: tool_input.offset is a 0-based starting line number, T5/B2).

    Missing offset → 1 (read from the start); offset a non-negative integer
    → offset + 1; anything else (negative / non-integer / bool) → None: the
    starting line is undeterminable, the caller records 0 entries
    (fail-closed, never renumber from 1).
    """
    if not isinstance(tool_input, dict) or "offset" not in tool_input:
        return 1
    v = tool_input["offset"]
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return v + 1
    return None


def fail_state_corrupt(path):
    sys.stderr.write(g.rejection_line("STATE_CORRUPT", path) + "\n")
    sys.stderr.flush()
    sys.exit(2)


def extract_returned_lines(tool_input, tool_response, entry):
    """Extract {line_no → line_text} per file per the catalog response rules (R4.3).

    Returns [(path_or_None, {line_no: text})]; path None means the file cannot
    be determined (the caller treats it as 0 entries). Completely unrecognized
    shape → [].
    """
    kind = (entry.get("response") or {}).get("kind", "none") \
        if isinstance(entry.get("response"), dict) else "none"
    if kind in ("none", "directory_listing", "path_list"):
        return []  # read shapes that return no line text: no evidence to record
    if not isinstance(tool_response, (dict, list, str)):
        return []

    out = []

    def file_map(path, start_line, content_text, lines_list):
        if isinstance(content_text, str) and content_text != "":
            base = start_line if isinstance(start_line, int) \
                and not isinstance(start_line, bool) and start_line >= 1 else 1
            lm = {}
            for i, line in enumerate(g.split_lines(content_text.encode("utf-8")) or [], base):
                lm[i] = line
            out.append((path, lm))
        elif isinstance(lines_list, list) and lines_list:
            lm = {}
            for i, item in enumerate(lines_list, 1):
                if isinstance(item, dict):
                    no = item.get("line_number", item.get("line", item.get("start_line")))
                    txt = item.get("text", item.get("content"))
                    if isinstance(no, int) and not isinstance(no, bool) and no >= 1 \
                            and isinstance(txt, str):
                        lm[no] = txt
                elif isinstance(item, str):
                    lm[i] = item
            if lm:
                out.append((path, lm))

    if kind == "file_text":
        path = None
        if isinstance(tool_input, dict):
            for spec in ((entry.get("response") or {}).get("path"), "$.path"):
                v = g.json_pointer(tool_input, spec) if spec else None
                if isinstance(v, str) and v:
                    path = v
                    break
        if isinstance(tool_response, str):
            # Observed shape (T5/B2): <file …>\n<content>\n…\n</content>\n</file>[Note: …];
            # the starting line is determined by tool_input.offset (0-based),
            # not by counting the outer tag lines
            content = parse_read_file_text(tool_response)
            if content is None:
                return []  # unknown string shape → 0 entries (R4.3 fail-closed)
            start = start_line_of_read(tool_input)
            if start is None:
                return []  # invalid offset → starting line undeterminable → 0 entries
            file_map(path, start, content, None)
            return out
        if isinstance(tool_response, dict):
            content = None
            for k in _CONTENT_KEYS:
                if isinstance(tool_response.get(k), str):
                    content = tool_response[k]
                    break
            lines = None
            for k in _LINES_KEYS:
                if isinstance(tool_response.get(k), list):
                    lines = tool_response[k]
                    break
            if content is None and lines is None:
                return []  # shape undeterminable → 0 entries (R4.3)
            start_line = tool_response.get("start_line", tool_response.get("startLine"))
            if not (isinstance(start_line, int) and not isinstance(start_line, bool)
                    and start_line >= 1):
                # Legacy dict shape compatibility: without an explicit start_line, use the observed tool_input.offset semantics
                start_line = start_line_of_read(tool_input)
                if start_line is None:
                    return []  # invalid offset → starting line undeterminable → 0 entries
            file_map(path, start_line, content, lines)
            return out
        if isinstance(tool_response, list):
            # List form: each item {path, line, text}, or plain text lines
            lm = {}
            has_struct = False
            for item in tool_response:
                if isinstance(item, dict):
                    has_struct = True
                    no = item.get("line_number", item.get("line"))
                    txt = item.get("text", item.get("content"))
                    if isinstance(no, int) and not isinstance(no, bool) \
                            and isinstance(txt, str):
                        lm[no] = txt
            if has_struct and lm:
                out.append((path, lm))
                return out
            # Plain text list: line numbers are positions (starting line undeterminable → start at 1)
            if all(isinstance(x, str) for x in tool_response) and tool_response:
                lm = {i: t for i, t in enumerate(tool_response, 1)}
                out.append((path, lm))
                return out
        return []

    if kind == "match_list":
        if not isinstance(tool_response, dict):
            return []
        matches = None
        for k in _MATCH_LIST_KEYS:
            if isinstance(tool_response.get(k), list):
                matches = tool_response[k]
                break
        if matches is None:
            return []
        by_file = {}
        for m in matches:
            if not isinstance(m, dict):
                return []  # an unknown shape mixed in → 0 entries overall
            fpath = None
            for k in _MATCH_FILE_KEYS:
                if isinstance(m.get(k), str) and m[k]:
                    fpath = m[k]
                    break
            no = None
            for k in _MATCH_LINE_KEYS:
                v = m.get(k)
                if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
                    no = v
                    break
            txt = None
            for k in _MATCH_TEXT_KEYS:
                if isinstance(m.get(k), str):
                    txt = m[k]
                    break
            if fpath is None or no is None or txt is None:
                return []  # one incomplete item → shape undeterminable → 0 entries
            by_file.setdefault(fpath, {})[no] = txt
        for fpath, lm in by_file.items():
            out.append((fpath, lm))
        return out

    return []  # unknown kind → 0 entries


def merge_ranges(numbers):
    """Sort, deduplicate, and merge adjacent numbers into maximal closed ranges (R4.1)."""
    nums = sorted(set(numbers))
    ranges = []
    for n in nums:
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    return ranges


def build_entries(cwd, active_dir, extracted):
    """Line filtering (R4.2) + path normalization (R4.13) → (entries to append, base instant)."""
    state = None
    st_text = g.read_text(os.path.join(active_dir, "state.json"))
    if st_text is not None:
        state = g.parse_state(st_text)
    if state is None:
        return [], None  # Target undeterminable → 0 entries fail-closed
    target_dir = os.path.join(cwd, "targets", state["target"])
    target_real = g.resolve_real(target_dir)
    if target_real is None:
        return [], None
    ci = g._symlink_case_insensitive()

    merged = {}  # resolved path → line map
    for path_str, line_map in extracted:
        if path_str is None or not line_map:
            continue
        if os.path.isabs(path_str):
            raw = path_str
        else:
            raw = os.path.join(cwd, path_str)
        resolved = g.resolve_real(raw)
        if resolved is None or not g.path_within(resolved, target_real, casefold=ci):
            continue  # R4.4: outside the Target → not recorded
        slot = merged.setdefault(resolved, {})
        for no, txt in line_map.items():
            slot[no] = txt

    entries = []
    now_dt = g.utc_now()
    for resolved, line_map in merged.items():
        rel = g.rel_location_path(resolved, target_real)
        if rel is None:
            continue
        lines_now = g.read_file_lines(resolved)
        if lines_now is None:
            continue
        kept = [n for n, txt in line_map.items()
                if isinstance(n, int) and 1 <= n <= len(lines_now)
                and g.normalized_text(txt) == g.normalized_text(lines_now[n - 1])]
        for start, end in merge_ranges(kept):
            entries.append({
                "ts": None,  # assigned inside the lock
                "run_id": state["run_id"],
                "path": rel,
                "start": start,
                "end": end,
                "sha256": g.evidence_hash(lines_now, start, end),
            })
    return entries, now_dt


def main():
    if len(sys.argv) < 2:
        exit_silent()
    cwd = os.getcwd()
    payload_text = g.read_text(sys.argv[1])
    payload = g.strict_json_loads(payload_text) if payload_text else None
    if not isinstance(payload, dict):
        exit_silent()

    # Active-run determination (design 5.2 step 1)
    scan = g.scan_runs(os.path.join(cwd, "runs"))
    if scan["active_run_id"] is None:
        exit_silent()  # R15.2
    active_dir = scan["active_dir"]

    # Non-read tools → exit 0
    catalog = None
    text = g.read_text(os.path.join(SCRIPT_DIR, "tool_catalog.json"))
    data = g.strict_json_loads(text) if text else None
    if isinstance(data, list):
        catalog = {e.get("name"): e for e in data
                   if isinstance(e, dict) and isinstance(e.get("name"), str)}
    entry = catalog.get(payload.get("tool_name")) if catalog else None
    if entry is None or entry.get("class") != "read":
        exit_silent()

    extracted = extract_returned_lines(payload.get("tool_input"),
                                       payload.get("tool_response"), entry)
    if not extracted:
        exit_silent()
    entries, now_dt = build_entries(cwd, active_dir, extracted)
    if not entries:
        exit_silent()
    if now_dt is None:
        now_dt = g.utc_now()

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or session_id == "":
        session_id = None

    # L_R exclusive critical section (see the lock-semantics note in the module docstring)
    log_path = os.path.join(active_dir, "evidence.jsonl")
    deadline = g.monotonic_deadline(INTERNAL_DEADLINE)
    lock = g.Dir_Lock(active_dir, exclusive=True, deadline=deadline)
    if not lock.acquire():
        exit_silent()  # lock-wait timeout → 0 entries, exit 0 (missing a record is acceptable degradation)
    try:
        prev_text = g.read_text(log_path)
        if prev_text is None:
            if os.path.exists(log_path):
                fail_state_corrupt(log_path)  # exists but unreadable
            exit_silent()  # absent → do not create (R4.14)
        entries_old, corrupt = g.parse_evidence_log(prev_text)
        if entries_old is None:
            # The last line being written by this component can only be a complete prefix or a complete line; unparseable means corrupt
            fail_state_corrupt(log_path)
        prev_ts = entries_old[-1]["ts"] if entries_old else None
        lines = []
        ts_prev = prev_ts
        for e in entries:
            e["ts"] = g.next_strict_ts(ts_prev, now_dt)
            ts_prev = e["ts"]
            if session_id is not None:
                e["session"] = session_id
            lines.append(g.evidence_entry_dump(e))
        try:
            g.append_once_many(log_path, lines)  # single write() (R4.7/R4.8)
        except OSError:
            exit_silent()  # append failed: leave nothing behind (single-write semantics)
    finally:
        lock.release()
    exit_silent()


if __name__ == "__main__":
    main()
