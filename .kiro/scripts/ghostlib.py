#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ghostlib — ghost-in-the-audit shared library.

Python 3.9+ standard library only (D1). All parsing functions are pure:
any defect returns None / a failure list, never a partially built object
(fail-closed).

Basis: .kiro/specs/ghost-in-the-audit/design.md section 3, requirements.md R4–R10.
"""

import fcntl
import hashlib
import json
import os
import re
import stat as stat_module
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

# ============================================================
# Identifiers and reserved names (design 3.2)
# ============================================================

RUN_ID_RE = re.compile(r"^run-[0-9]{13}-[0-9a-f]{6}\Z")
CANARY_FILE_NAME = "gate-canary.probe"
SUBMISSION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\.json\Z")
CONTROL_FILE_NAMES = frozenset(("start.json", "finalize.json", "abort.json"))
FINDING_ID_RE = re.compile(r"^F-[0-9]{6}\Z")
SUMMARY_FINDING_ID_RE = re.compile(r"F-[0-9]{6}")

CATEGORIES = ("architecture", "security", "dependency", "quality", "embedded-instruction")
SEVERITIES = ("critical", "high", "medium", "low", "info")
AUDIT_AGENTS = ("Cartographer", "Security_Auditor", "Dependency_Auditor", "Quality_Auditor")

FINDING_KEYS = ("run_id", "agent", "title", "category", "severity", "description", "citations")
CITATION_KEYS = ("location", "evidence_timestamp", "excerpt")

# Length caps are counted in Unicode code points (R5.11 / A18)
TITLE_MAX = 200
DESCRIPTION_MAX = 4000
LOCATION_MAX = 1024
EXCERPT_MAX = 4000
MAX_CITATIONS = 10
MAX_SPAN_LINES = 50
LINE_NUMBER_MAX = 999999999          # Location line-number cap (R6.5)
ENTRY_LINE_NUMBER_MAX = 2147483647   # Evidence_Entry line-number cap (R4.11)
MAX_LISTED_FAILURES = 50             # Rejection_Message per-item line cap (R9.2)
MAX_SUMMARY_STATEMENTS = 20          # Summary_Statement count cap (R13.11)
MAX_SUMMARY_STATEMENT_LEN = 500      # Per-statement Summary_Statement code-point cap (R13.11)
MAX_FINDING_SERIAL = 999999          # Finding serial-number cap (R10.7 boundary)

EVIDENCE_KEYS = ("ts", "run_id", "path", "start", "end", "sha256")


# ============================================================
# Timestamps (design 3.4; R4.11, R4.12)
# ============================================================

_TS_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{3})Z\Z"
)
_MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_leap_year(year):
    """Gregorian leap year (same rule before 1582; Glossary Evidence_Timestamp)."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _days_in_month(year, month):
    if month == 2 and _is_leap_year(year):
        return 29
    return _MONTH_DAYS[month - 1]


def parse_ts(s):
    """Parse an Evidence_Timestamp → aware datetime(UTC); any defect returns None."""
    if not isinstance(s, str):
        return None
    m = _TS_RE.match(s)
    if m is None:
        return None
    year, month, day, hour, minute, sec, ms = (int(g) for g in m.groups())
    if not 1 <= year <= 9999:
        return None
    if not 1 <= month <= 12:
        return None
    if not 1 <= day <= _days_in_month(year, month):
        return None
    if hour > 23 or minute > 59 or sec > 59:
        return None
    try:
        return datetime(year, month, day, hour, minute, sec, ms * 1000, tzinfo=timezone.utc)
    except ValueError:
        return None


def format_ts(dt):
    """datetime(UTC) → `YYYY-MM-DDTHH:MM:SS.sssZ` (truncated below milliseconds)."""
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (
        dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
        dt.microsecond // 1000,
    )


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_str():
    return format_ts(utc_now())


def ts_plus_ms(dt, ms):
    return dt + timedelta(milliseconds=ms)


def next_strict_ts(prev_ts, now_dt):
    """Strictly increasing timestamp: max(now, prev + 1ms); prev None → now (R4.5)."""
    now_str = format_ts(now_dt)
    if prev_ts is None:
        return now_str
    candidate = ts_plus_ms(parse_ts(prev_ts), 1)
    return now_str if now_dt >= candidate else format_ts(candidate)


# ============================================================
# Text and hashes (design 3.4)
# ============================================================

def split_lines(data):
    """Byte string → list of lines (Glossary Line_Number definition).

    Strict UTF-8 decode (None on failure); empty text = 0 lines; split on LF,
    a trailing LF produces no extra line; each segment sheds at most one
    trailing CR.
    """
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, bytes):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text == "":
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def read_file_lines(path):
    """Read a file and split lines; any failure returns None (FILE_UNREADABLE semantics)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return split_lines(data)


def normalized_text(text):
    """Normalized_Text: collapse whitespace per line, then join with LF (Glossary)."""
    return "\n".join(" ".join(line.split()) for line in text.split("\n"))


def evidence_hash(lines, start, end):
    """SHA-256 (lowercase hex) of the recorded line range joined with LF (design 3.4; same method as R7.14)."""
    joined = "\n".join(lines[start - 1:end])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def bytes_hash(data):
    """SHA-256 hex digest (lowercase) of the exact bytes."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ============================================================
# Strict JSON parsing (with object paths; design 3.3 "strict parsing")
# ============================================================

_JSON_WS = " \t\r\n"
_JSON_DIGITS = "0123456789"


class _JSONError(Exception):
    """path is the object path to the error site ("" = document root); dup is True only for duplicate keys."""

    def __init__(self, path, message, dup=False):
        super().__init__(message)
        self.path = path
        self.dup = dup


class StrictJSONParser:
    """Strict JSON parser: rejects duplicate keys, NaN/Infinity, trailing
    content, and lone surrogates.

    dicts preserve the key order from the file. Failures raise _JSONError.
    """

    def __init__(self, text):
        if not isinstance(text, str):
            raise _JSONError("", "not a string")
        self.s = text
        self.i = 0
        self.n = len(text)

    def parse(self):
        self._skip_ws()
        value = self._value("", 0)
        self._skip_ws()
        if self.i != self.n:
            raise _JSONError("", "trailing content after JSON value")
        return value

    # ---- internal ----

    def _skip_ws(self):
        while self.i < self.n and self.s[self.i] in _JSON_WS:
            self.i += 1

    def _value(self, path, depth):
        if depth > 100:
            raise _JSONError(path, "nesting too deep")
        if self.i >= self.n:
            raise _JSONError(path, "unexpected end of input")
        c = self.s[self.i]
        if c == "{":
            return self._object(path, depth)
        if c == "[":
            return self._array(path, depth)
        if c == '"':
            return self._string(path)
        if self.s.startswith("true", self.i):
            self.i += 4
            return True
        if self.s.startswith("false", self.i):
            self.i += 5
            return False
        if self.s.startswith("null", self.i):
            self.i += 4
            return None
        if c == "-" or c in _JSON_DIGITS:
            return self._number(path)
        raise _JSONError(path, "unexpected character")

    def _object(self, path, depth):
        self.i += 1
        obj = {}
        self._skip_ws()
        if self.i < self.n and self.s[self.i] == "}":
            self.i += 1
            return obj
        while True:
            self._skip_ws()
            if self.i >= self.n or self.s[self.i] != '"':
                raise _JSONError(path, "expected string object key")
            key = self._string(path)
            child_path = path + "." + key if path else key
            if key in obj:
                # Duplicate field name: reported by its field path (R5.7, R9.3)
                raise _JSONError(child_path, "duplicate key", dup=True)
            self._skip_ws()
            if self.i >= self.n or self.s[self.i] != ":":
                raise _JSONError(path, "expected ':'")
            self.i += 1
            self._skip_ws()
            obj[key] = self._value(child_path, depth + 1)
            self._skip_ws()
            if self.i >= self.n:
                raise _JSONError(path, "unterminated object")
            c = self.s[self.i]
            self.i += 1
            if c == "}":
                return obj
            if c != ",":
                raise _JSONError(path, "expected ',' or '}'")

    def _array(self, path, depth):
        self.i += 1
        arr = []
        self._skip_ws()
        if self.i < self.n and self.s[self.i] == "]":
            self.i += 1
            return arr
        idx = 0
        while True:
            self._skip_ws()
            arr.append(self._value("%s[%d]" % (path, idx + 1), depth + 1))
            idx += 1
            self._skip_ws()
            if self.i >= self.n:
                raise _JSONError(path, "unterminated array")
            c = self.s[self.i]
            self.i += 1
            if c == "]":
                return arr
            if c != ",":
                raise _JSONError(path, "expected ',' or ']'")

    def _string(self, path):
        self.i += 1  # opening quote
        out = []
        s, n = self.s, self.n
        while True:
            if self.i >= n:
                raise _JSONError(path, "unterminated string")
            c = s[self.i]
            self.i += 1
            if c == '"':
                return "".join(out)
            if c == "\\":
                if self.i >= n:
                    raise _JSONError(path, "unterminated escape")
                e = s[self.i]
                self.i += 1
                if e == '"':
                    out.append('"')
                elif e == "\\":
                    out.append("\\")
                elif e == "/":
                    out.append("/")
                elif e == "b":
                    out.append("\b")
                elif e == "f":
                    out.append("\f")
                elif e == "n":
                    out.append("\n")
                elif e == "r":
                    out.append("\r")
                elif e == "t":
                    out.append("\t")
                elif e == "u":
                    out.append(self._unicode_escape(path))
                else:
                    raise _JSONError(path, "invalid escape")
            else:
                if ord(c) < 0x20:
                    raise _JSONError(path, "raw control character in string")
                out.append(c)

    def _unicode_escape(self, path):
        cp = self._hex4(path)
        if 0xD800 <= cp <= 0xDBFF:
            if self.s[self.i:self.i + 2] != "\\u":
                raise _JSONError(path, "lone high surrogate")
            save = self.i
            self.i += 2
            low = self._hex4(path)
            if not 0xDC00 <= low <= 0xDFFF:
                self.i = save  # rewind so the rest is parsed as regular characters (which then fail syntactically or splice literally)
                raise _JSONError(path, "invalid low surrogate")
            cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)
        elif 0xDC00 <= cp <= 0xDFFF:
            raise _JSONError(path, "lone low surrogate")
        return chr(cp)

    def _hex4(self, path):
        chunk = self.s[self.i:self.i + 4]
        if len(chunk) != 4 or any(ch not in "0123456789abcdefABCDEF" for ch in chunk):
            raise _JSONError(path, "invalid \\u escape")
        self.i += 4
        return int(chunk, 16)

    def _number(self, path):
        s, n = self.s, self.n
        start = self.i
        if s[self.i] == "-":
            self.i += 1
            if self.i >= n or s[self.i] not in _JSON_DIGITS:
                raise _JSONError(path, "invalid number")
        if s[self.i] == "0":
            self.i += 1
        elif s[self.i] in "123456789":
            while self.i < n and s[self.i] in _JSON_DIGITS:
                self.i += 1
        else:
            raise _JSONError(path, "invalid number")
        if self.i < n and s[self.i] == ".":
            self.i += 1
            if self.i >= n or s[self.i] not in _JSON_DIGITS:
                raise _JSONError(path, "invalid number")
            while self.i < n and s[self.i] in _JSON_DIGITS:
                self.i += 1
        if self.i < n and s[self.i] in "eE":
            self.i += 1
            if self.i < n and s[self.i] in "+-":
                self.i += 1
            if self.i >= n or s[self.i] not in _JSON_DIGITS:
                raise _JSONError(path, "invalid number")
            while self.i < n and s[self.i] in _JSON_DIGITS:
                self.i += 1
        return json.loads(s[start:self.i])


def strict_json_loads(text):
    """Strict JSON parsing; returns None on failure (error type not distinguished)."""
    try:
        return StrictJSONParser(text).parse()
    except _JSONError:
        return None


# ============================================================
# Evidence_Codec (R4.10–R4.15; design 3.3)
# ============================================================

_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")


def _entry_in_range(v, lo, hi):
    # bool is a subclass of int and must be excluded explicitly (bool posing as int → parse failure)
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def evidence_entry_dump(entry):
    """Evidence_Entry → one JSON line (fixed key order, no trailing newline)."""
    obj = {
        "ts": entry["ts"],
        "run_id": entry["run_id"],
        "path": entry["path"],
        "start": entry["start"],
        "end": entry["end"],
        "sha256": entry["sha256"],
    }
    if entry.get("session") is not None:
        obj["session"] = entry["session"]
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def evidence_entry_load(line):
    """One JSON line → Evidence_Entry (fixed key order); any defect returns None (R4.15)."""
    try:
        obj = StrictJSONParser(line).parse()
    except _JSONError:
        return None
    if not isinstance(obj, dict):
        return None
    keys = list(obj.keys())
    if keys and keys[-1] == "session":
        keys = keys[:-1]
    if tuple(keys) != EVIDENCE_KEYS:
        return None  # missing, undefined, or misordered keys
    if parse_ts(obj["ts"]) is None:
        return None
    if not isinstance(obj["run_id"], str) or not obj["run_id"]:
        return None
    if not isinstance(obj["path"], str) or not obj["path"]:
        return None
    if not _entry_in_range(obj["start"], 1, ENTRY_LINE_NUMBER_MAX):
        return None
    if not _entry_in_range(obj["end"], 1, ENTRY_LINE_NUMBER_MAX):
        return None
    if obj["start"] > obj["end"]:
        return None
    if not isinstance(obj["sha256"], str) or _SHA256_RE.match(obj["sha256"]) is None:
        return None
    if "session" in obj and not isinstance(obj["session"], str):
        return None
    entry = {k: obj[k] for k in EVIDENCE_KEYS}
    if "session" in obj:
        entry["session"] = obj["session"]
    return entry


def parse_evidence_log(text):
    """Evidence_Log full text → (entries, corrupt).

    If any line is unparseable, returns (None, line_number); non-strictly
    increasing timestamps count as corrupt (R8.7).
    """
    if text is None:
        return None, None
    entries = []
    prev_dt = None
    for i, line in enumerate(text.split("\n"), 1):
        if line == "":
            continue  # a trailing LF produces no new line; empty lines can only come from the tail
        entry = evidence_entry_load(line)
        if entry is None:
            return None, i
        dt = parse_ts(entry["ts"])
        if prev_dt is not None and dt <= prev_dt:
            return None, i
        prev_dt = dt
        entries.append(entry)
    return entries, None


# ============================================================
# Location_Parser / Location_Formatter (R6; design 3.6)
# ============================================================

def _location_path_problem(path):
    """Invalid-path conditions from R6.4; returns True when invalid."""
    if not isinstance(path, str) or path == "":
        return True
    if path.startswith("/"):
        return True
    if "\\" in path:
        return True
    for ch in path:
        o = ord(ch)
        if o <= 0x1F or 0x7F <= o <= 0x9F:
            return True
    try:
        raw = path.encode("utf-8")
    except UnicodeEncodeError:
        return True
    if len(raw) > 4096:
        return True
    for seg in path.split("/"):
        if seg == "" or seg == "." or seg == "..":
            return True
    return False


_NUM_RE = re.compile(r"^[1-9][0-9]{0,8}\Z")                    # 1..999999999, no leading zeros
_RANGE_RE = re.compile(r"^([1-9][0-9]{0,8})-([1-9][0-9]{0,8})\Z")


def parse_location(s):
    """Location string → (value, None) or (None, "LOCATION_MALFORMED").

    Splits at the last ASCII colon (R6.3); line forms <line> / <start>-<end> (R6.5).
    """
    if not isinstance(s, str):
        return None, "LOCATION_MALFORMED"
    idx = s.rfind(":")
    if idx < 0:
        return None, "LOCATION_MALFORMED"
    path, line_part = s[:idx], s[idx + 1:]
    if _location_path_problem(path):
        return None, "LOCATION_MALFORMED"
    if "-" in line_part:
        m = _RANGE_RE.match(line_part)
        if m is None:
            return None, "LOCATION_MALFORMED"
        start, end = int(m.group(1)), int(m.group(2))
        if start >= end:  # A19: the range form requires start < end; `a.py:10-10` is invalid
            return None, "LOCATION_MALFORMED"
    else:
        if _NUM_RE.match(line_part) is None:
            return None, "LOCATION_MALFORMED"
        start = end = int(line_part)
    return {"path": path, "start": start, "end": end}, None


def format_location(loc):
    """Location value → string (R6.6): single-line form when start == end."""
    if loc["start"] == loc["end"]:
        return "%s:%d" % (loc["path"], loc["start"])
    return "%s:%d-%d" % (loc["path"], loc["start"], loc["end"])


# ---- Location_Reference extraction (R7.16; design 3.6 full table) ----

# Note: the full-width/CJK brackets and punctuation below are load-bearing
# strip-character sets from the spec, not prose; do not translate them.
_LEAD_STRIP_CHARS = (
    "()[]{}<>\"'`"
    "（）［］｛｝〈〉《》「」『』【】"
    "“”‘’"
)
_TRAIL_STRIP_CHARS = _LEAD_STRIP_CHARS + "，、。．；：？！,.;:?!…"


def extract_location_references(text):
    """Free text → list of Location values (split on Unicode whitespace + lead/trail stripping)."""
    refs = []
    if not isinstance(text, str):
        return refs
    for frag in text.split():
        core = frag.lstrip(_LEAD_STRIP_CHARS).rstrip(_TRAIL_STRIP_CHARS)
        loc, err = parse_location(core)
        if err is None:
            refs.append(loc)
    return refs


# ============================================================
# Submission_Parser / Canonical_Serializer (R5; design 3.5)
# ============================================================

def _schema_invalid(path):
    return {"reason_code": "SCHEMA_INVALID", "path": path,
            "citation_index": None, "field": None}


def _dedup_failures(failures):
    seen = set()
    out = []
    for f in failures:
        key = (f["reason_code"], f["path"], f["citation_index"], f["field"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def parse_submission(text):
    """Submission text → (finding, None) or (None, failures).

    Priority (R5.12, R5.13, design 5.3 order 5):
    CITATION_MISSING → CITATION_INCOMPLETE → SCHEMA_INVALID; first hit short-circuits.
    failure element: {reason_code, path, citation_index(1-based|None), field}
    """
    try:
        value = StrictJSONParser(text).parse()
    except _JSONError as exc:
        if exc.dup:
            # Duplicate field name: reported by the path of the object containing it (R9.3)
            return None, [_schema_invalid(exc.path or "")]
        return None, [_schema_invalid("")]  # not JSON / syntax error → document root

    if not isinstance(value, dict):
        return None, [_schema_invalid("")]

    # —— CITATION_MISSING (R5.12) ——
    if "citations" not in value:
        return None, [{"reason_code": "CITATION_MISSING", "path": "citations",
                       "citation_index": None, "field": None}]
    cits = value["citations"]
    if cits is None or (isinstance(cits, list) and len(cits) == 0):
        return None, [{"reason_code": "CITATION_MISSING", "path": "citations",
                       "citation_index": None, "field": None}]

    # —— CITATION_INCOMPLETE (R5.13; applies to object elements only) ——
    incomplete = []
    if isinstance(cits, list):
        for i, c in enumerate(cits, 1):
            if not isinstance(c, dict):
                continue
            for f in CITATION_KEYS:
                if f not in c or c[f] is None or c[f] == "":
                    incomplete.append({
                        "reason_code": "CITATION_INCOMPLETE",
                        "path": "citations[%d].%s" % (i, f),
                        "citation_index": i, "field": f,
                    })
    if incomplete:
        return None, incomplete

    # —— SCHEMA_INVALID collection (R5.7) ——
    fails = []

    for k in value:
        if k not in FINDING_KEYS:
            fails.append(_schema_invalid(""))  # undefined top-level field → path of its containing object (root)

    for k in ("run_id", "agent", "title", "category", "severity", "description"):
        if k not in value or not isinstance(value[k], str):
            fails.append(_schema_invalid(k))

    if not isinstance(cits, list):
        fails.append(_schema_invalid("citations"))
    else:
        if len(cits) > MAX_CITATIONS:
            fails.append(_schema_invalid("citations"))
        for i, c in enumerate(cits, 1):
            cpath = "citations[%d]" % i
            if not isinstance(c, dict):
                fails.append(_schema_invalid(cpath))
                continue
            for k in c:
                if k not in CITATION_KEYS:
                    fails.append(_schema_invalid(cpath))  # undefined field → path of its containing Citation object
            for f in CITATION_KEYS:
                if f in c and not isinstance(c[f], str):
                    fails.append(_schema_invalid(cpath + "." + f))
            if isinstance(c.get("location"), str) and len(c["location"]) > LOCATION_MAX:
                fails.append(_schema_invalid(cpath + ".location"))
            if isinstance(c.get("excerpt"), str) and len(c["excerpt"]) > EXCERPT_MAX:
                fails.append(_schema_invalid(cpath + ".excerpt"))

    title = value.get("title")
    if isinstance(title, str) and not 1 <= len(title) <= TITLE_MAX:
        fails.append(_schema_invalid("title"))
    desc = value.get("description")
    if isinstance(desc, str) and not 1 <= len(desc) <= DESCRIPTION_MAX:
        fails.append(_schema_invalid("description"))
    if isinstance(value.get("category"), str) and value["category"] not in CATEGORIES:
        fails.append(_schema_invalid("category"))
    if isinstance(value.get("severity"), str) and value["severity"] not in SEVERITIES:
        fails.append(_schema_invalid("severity"))
    if isinstance(value.get("agent"), str) and value["agent"] not in AUDIT_AGENTS:
        fails.append(_schema_invalid("agent"))

    fails = _dedup_failures(fails)
    if fails:
        return None, fails

    finding = {
        "run_id": value["run_id"],
        "agent": value["agent"],
        "title": value["title"],
        "category": value["category"],
        "severity": value["severity"],
        "description": value["description"],
        "citations": [
            {k: c[k] for k in CITATION_KEYS} for c in cits
        ],
    }
    return finding, None


def canonical_obj(finding):
    """Finding → fixed-key-order object (design 3.5)."""
    return {
        "run_id": finding["run_id"],
        "agent": finding["agent"],
        "title": finding["title"],
        "category": finding["category"],
        "severity": finding["severity"],
        "description": finding["description"],
        "citations": [
            {"location": c["location"],
             "evidence_timestamp": c["evidence_timestamp"],
             "excerpt": c["excerpt"]}
            for c in finding["citations"]
        ],
    }


def canonical_form(finding):
    """Finding → Canonical_Form string (equivalent objects are byte-identical and idempotent, R5.8–R5.10)."""
    return json.dumps(canonical_obj(finding), ensure_ascii=False, separators=(",", ":"))


def reserialize_canonical_obj(obj):
    """Re-serialize a canonical object embedded in the Ledger (key order taken from the object itself)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ============================================================
# Locks and atomic writes (design 2.3, D3.1)
# ============================================================

def monotonic_deadline(seconds):
    """Absolute monotonic-clock instant; seconds None → None (no deadline)."""
    return None if seconds is None else time.monotonic() + seconds


class LockTimeout(Exception):
    pass


class Dir_Lock:
    """flock on a directory fd (L_G / L_R, design 2.3).

    deadline is an absolute time.monotonic() instant; polls LOCK_NB until it
    expires. acquire() returning False means the lock wait timed out (the
    caller fails closed on timeout).
    """

    def __init__(self, path, exclusive, deadline=None):
        self.path = path
        self.exclusive = exclusive
        self.deadline = deadline
        self._fd = None

    def acquire(self):
        fd = os.open(self.path, os.O_RDONLY)
        mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
                self._fd = fd
                return True
            except OSError:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    os.close(fd)
                    return False
                time.sleep(0.004)

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        if not self.acquire():
            raise LockTimeout(self.path)
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def append_once(path, text):
    """Append one line in a single write() under the lock (adds LF; creates the file if absent, R2.3).

    A short write raises OSError (R4.7/R8.11/R10.6).
    """
    if not text.endswith("\n"):
        text += "\n"
    data = text.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        written = os.write(fd, data)
    finally:
        os.close(fd)
    if written != len(data):
        raise OSError("short write (%d/%d bytes)" % (written, len(data)))


def append_once_many(path, lines):
    """Join multiple lines into one write() append (R4.8: a single write does not interleave)."""
    data = "".join(l if l.endswith("\n") else l + "\n" for l in lines).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        written = os.write(fd, data)
    finally:
        os.close(fd)
    if written != len(data):
        raise OSError("short write (%d/%d bytes)" % (written, len(data)))


def read_text(path):
    """Read a file as a UTF-8 string; any failure returns None."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def atomic_write(path, data):
    """Temp file + os.replace atomic swap (state.json / report.*).

    data is str (UTF-8) or bytes. On failure the temp file is cleaned up and
    the error re-raised.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".atomic-", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ============================================================
# Reason_Code constants and description table (design 8 full table, 37 codes)
# ============================================================

REASON_DESCRIPTIONS = {
    "TARGET_INVALID": "The Target_Codebase name in the Start_Request is missing, invalid, or does not exist.",
    "TARGET_EMPTY": "The Target_Codebase contains no regular files.",
    "RUN_ALREADY_ACTIVE": "An Audit_Run with active status already exists in the workspace.",
    "AUTO_LOADED_CONTEXT_PRESENT": "The Target_Codebase contains files or directories that Kiro would auto-load (e.g. AGENTS.md, .kiro).",
    "SYMLINK_ESCAPE": "The Target_Codebase contains symbolic links pointing outside it or that cannot be resolved.",
    "STATE_CORRUPT": "The audit state file is missing, unreadable, or unparseable.",
    "INTERNAL_ERROR": "Internal Toolkit error; blocked fail-closed.",
    "GATE_NOT_VERIFIED": "No Gate_Live_Record; cannot confirm the Citation_Gate took effect.",
    "RECORDER_NOT_VERIFIED": "The Evidence_Log is empty or unreadable; cannot confirm the Evidence_Recorder took effect.",
    "CITATION_MISSING": "The Submission lacks a citations field, or its value is null or an empty array.",
    "CITATION_INCOMPLETE": "A Citation lacks location, evidence_timestamp, or excerpt, or one of them is null or an empty string.",
    "SCHEMA_INVALID": "The Submission does not conform to the Submission_Schema.",
    "RUN_MISMATCH": "The Submission's run_id does not match the target Audit_Run.",
    "RUN_NOT_ACTIVE": "The target Audit_Run does not exist or has already ended.",
    "LOCATION_MALFORMED": "The Location is syntactically invalid.",
    "FILE_NOT_FOUND": "The cited path does not exist in the Target_Codebase or is not a regular file.",
    "PATH_OUTSIDE_TARGET": "The cited path resolves outside the Target_Codebase.",
    "FILE_UNREADABLE": "The cited file is unreadable or not valid UTF-8.",
    "LINE_OUT_OF_RANGE": "The cited line numbers exceed the file's current line count.",
    "SPAN_TOO_LARGE": "A single Citation covers more than 50 lines.",
    "TIMESTAMP_MALFORMED": "The Evidence_Timestamp is not a valid UTC millisecond-precision timestamp.",
    "EVIDENCE_NOT_FOUND": "No Evidence_Entry matching that timestamp exists in the Evidence_Log.",
    "EVIDENCE_MISMATCH": "The matched Evidence_Entry's path or line range differs from the Citation.",
    "CONTENT_CHANGED": "The cited code differs from what was recorded as evidence (it has changed since).",
    "EXCERPT_MISMATCH": "The Excerpt does not match the cited line content.",
    "UNCITED_LOCATION": "A file:line reference in title or description is not covered by any Citation.",
    "PAYLOAD_INVALID": "The hook input is empty, is not valid JSON, or lacks the tool name/arguments.",
    "UNDETERMINABLE": "The effect of this tool call cannot be determined; blocked fail-closed.",
    "GATE_FAILURE": "The validation script did not run correctly; blocked fail-closed.",
    "TIMEOUT": "Validation or processing timed out; blocked fail-closed.",
    "FILE_NAME_INVALID": "The Submission file name is invalid.",
    "INBOX_IMMUTABLE": "Existing files in the Submission_Inbox must not be modified, or the file name is already used by an accepted Finding.",
    "DUPLICATE_SUBMISSION": "Content duplicates an already accepted Finding.",
    "PROTECTED_PATH": "This path is write-protected during the audit.",
    "REQUEST_INVALID": "The Control_Request file name or location is invalid.",
    "REPORT_FAILED": "Generating the Audit_Report failed; the Audit_Run remains active.",
    "SUMMARY_UNSUPPORTED": "The Finalize_Request contains Summary_Statements that failed validation.",
}

KNOWN_REASON_CODES = frozenset(REASON_DESCRIPTIONS)

# System_Reason_Code (Glossary): indicates Toolkit / Audit_Run state problems
SYSTEM_REASON_CODES = frozenset((
    "PAYLOAD_INVALID", "UNDETERMINABLE", "INTERNAL_ERROR",
    "GATE_FAILURE", "TIMEOUT", "STATE_CORRUPT", "RUN_NOT_ACTIVE",
))

SCHEMA_CLASS_CODES = frozenset(("SCHEMA_INVALID", "CITATION_MISSING", "CITATION_INCOMPLETE"))

TRUNCATION_LINE_TEMPLATE = "… and {n} more not listed"


def rejection_line(code, detail=None):
    """`<REASON_CODE>[ <detail>]: <description>` (R9.2; design 8)."""
    desc = REASON_DESCRIPTIONS[code]
    if detail:
        return "%s %s: %s" % (code, detail, desc)
    return "%s: %s" % (code, desc)


def build_rejection_message(failures):
    """failures: [(code, detail|None), …] → multi-line message (truncated past 50 items + remaining-count line)."""
    lines = [rejection_line(code, detail) for code, detail in failures]
    if len(lines) > MAX_LISTED_FAILURES:
        rest = len(lines) - MAX_LISTED_FAILURES
        lines = lines[:MAX_LISTED_FAILURES]
        lines.append(TRUNCATION_LINE_TEMPLATE.format(n=rest))
    return "\n".join(lines)


# ============================================================
# Runs scanner (design 4.2, 5.5; R15.6)
# ============================================================

STATE_STATUS_VALUES = ("active", "completed", "aborted")


def parse_state(text):
    """Run_State_Record strict parse (fixed key order, any unknown key = corrupt); returns None on defect."""
    try:
        obj = StrictJSONParser(text).parse()
    except _JSONError:
        return None
    if not isinstance(obj, dict):
        return None
    status = obj.get("status")
    expected = ["run_id", "target"]
    if "focus" in obj:
        expected.append("focus")
    expected.append("status")
    expected.append("started_at")
    if status in ("completed", "aborted"):
        expected.append("finished_at")
    if list(obj.keys()) != expected:
        return None
    for k in ("run_id", "target", "started_at"):
        if not isinstance(obj[k], str) or not obj[k]:
            return None
    if status not in STATE_STATUS_VALUES:
        return None
    if "focus" in obj and not isinstance(obj["focus"], str):
        return None
    if parse_ts(obj["started_at"]) is None:
        return None
    if "finished_at" in obj and parse_ts(obj["finished_at"]) is None:
        return None
    return obj


def scan_runs(runs_dir):
    """Scan the Runs_Directory → dict(active_run_id, active_dir, corrupt_paths, active_count).

    Only direct subdirectories not starting with `.` count as Run_Directory
    (design 3.1). More than one active means the state is undeterminable
    (returned with corrupt_paths containing each active directory); the
    caller fails closed per R8.10 / R15.6.
    """
    result = {"active_run_id": None, "active_dir": None,
              "corrupt_paths": [], "active_count": 0}
    try:
        with os.scandir(runs_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        result["corrupt_paths"].append(str(runs_dir))
        return result
    actives = []
    for e in entries:
        if e.name.startswith("."):
            continue
        if not e.is_dir():
            continue
        state_path = os.path.join(e.path, "state.json")
        text = read_text(state_path)
        if text is None:
            result["corrupt_paths"].append(state_path)
            continue
        state = parse_state(text)
        if state is None:
            result["corrupt_paths"].append(state_path)
            continue
        if state["status"] == "active":
            actives.append((state["run_id"], e.path))
    if result["corrupt_paths"]:
        return result
    result["active_count"] = len(actives)
    if len(actives) == 1:
        result["active_run_id"], result["active_dir"] = actives[0]
    elif len(actives) > 1:
        result["corrupt_paths"] = [p for _, p in actives]
        result["active_count"] = 0
    return result


# ============================================================
# Markdown escaping (R13.10; design 5.5)
# ============================================================

def md_escape(s):
    """First replaces `\\` → `\\\\`, `` ` `` → `` \\\\` `` and `|` → `\\|`, then LF/CR/Tab → single space."""
    s = s.replace("\\", "\\\\").replace("`", "\\`").replace("|", "\\|")
    return s.replace("\n", " ").replace("\r", " ").replace("\t", " ")


# ============================================================
# Other shared helpers
# ============================================================

def new_run_id():
    """`run-<13-digit epoch ms>-<6-digit lowercase hex>` (design 3.2)."""
    return "run-%013d-%s" % (int(time.time() * 1000), os.urandom(3).hex())


def finding_id(serial):
    """Serial n (1-based) → `F-%06d` (design 3.2)."""
    return "F-%06d" % serial


def canary_submission_text(run_id):
    """Canary_Submission predefined content (design 3.3; R2.1)."""
    return json.dumps({
        "run_id": run_id,
        "agent": "Cartographer",
        "title": "ghost canary gate probe",
        "category": "architecture",
        "severity": "info",
        "description": "liveness probe; citations intentionally empty",
        "citations": [],
    }, ensure_ascii=False, separators=(",", ":"))


def path_within(path, root, casefold=False):
    """Whether the resolved path lies inside the resolved root (root itself included).

    Case-insensitive filesystems compare with casefold (design 5.4).
    """
    if casefold:
        path, root = path.casefold(), root.casefold()
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def rel_location_path(resolved, target_root):
    """resolved → `/`-separated path relative to the Target root; None when outside the Target (R4.13)."""
    if not path_within(resolved, target_root):
        return None
    rel = os.path.relpath(resolved, target_root)
    if rel == ".":
        return None
    return rel.replace(os.sep, "/")


def resolve_real(path):
    """realpath (strictness supplemented by the caller as needed); None on OSError such as resolution loops."""
    try:
        return os.path.realpath(path)
    except OSError:
        return None


def json_pointer(obj, spec):
    """Value getter for `$.a.b` style specs (Tool_Catalog extraction rules); None if any level is missing.

    Invalid spec (not str, or not starting with `$.`) returns None.
    """
    if not isinstance(spec, str) or not spec.startswith("$."):
        return None
    cur = obj
    if spec == "$.":
        return cur
    for part in spec[2:].split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def extract_string_candidates(tool_input, specs):
    """Extract string values per a candidate pointer list (catalog order preserved, values deduplicated).

    specs is a single `$.x` string or a list of them; a pointer yielding a
    non-string (including missing) value is skipped.
    Caller semantics (B1: the observed fs_write content parameter name is
    `text`; `content` is a compatibility fallback only):
      exactly 1 distinct value → adopt it; 0 → missing parameter
      (PAYLOAD_INVALID semantics); ≥2 distinct values → cannot determine
      which one would actually be written (fail-closed, UNDETERMINABLE semantics).
    """
    if not isinstance(tool_input, dict):
        return []
    if isinstance(specs, str):
        specs = [specs]
    if not isinstance(specs, list):
        return []
    out = []
    for spec in specs:
        v = json_pointer(tool_input, spec) if isinstance(spec, str) else None
        if isinstance(v, str) and v not in out:
            out.append(v)
    return out


# ============================================================
# Citation sub-checks (R7.6–R7.16; shared by Citation_Gate and Report_Renderer)
# ============================================================

def _symlink_case_insensitive():
    return sys.platform in ("darwin", "win32")


def check_citation(location, evidence_timestamp, excerpt, target_real,
                   evidence_entries, lines_cache=None):
    """Single Citation sub-checks in fixed order (R7.20), stopping at the first failure.

    target_real: realpath(Target root); evidence_entries: list of Evidence_Entry.
    Returns None (pass) or a REASON_CODE. The caller appends the `citations[n]` detail on failure.
    """
    import errno as _errno

    # 1 LOCATION_MALFORMED
    loc, err = parse_location(location)
    if err is not None:
        return "LOCATION_MALFORMED"
    # 2 SPAN_TOO_LARGE
    if loc["end"] - loc["start"] + 1 > MAX_SPAN_LINES:
        return "SPAN_TOO_LARGE"
    # 3 TIMESTAMP_MALFORMED
    if parse_ts(evidence_timestamp) is None:
        return "TIMESTAMP_MALFORMED"
    # 4 PATH_OUTSIDE_TARGET (includes resolution loops, design 5.3)
    target_root = resolve_real(target_real) or target_real
    ci = _symlink_case_insensitive()
    joined = os.path.join(target_root, loc["path"].replace("/", os.sep))
    try:
        resolved = os.path.realpath(joined, strict=True)
    except OSError as e:
        if getattr(e, "errno", None) == _errno.ELOOP:
            return "PATH_OUTSIDE_TARGET"
        resolved = os.path.realpath(joined)  # dangling link: still run the containment check
    if not path_within(resolved, target_root, casefold=ci):
        return "PATH_OUTSIDE_TARGET"
    # 5 FILE_NOT_FOUND
    try:
        st = os.stat(resolved)
    except OSError:
        return "FILE_NOT_FOUND"
    if not stat_module.S_ISREG(st.st_mode):
        return "FILE_NOT_FOUND"
    # 6 FILE_UNREADABLE
    if lines_cache is not None and resolved in lines_cache:
        lines = lines_cache[resolved]
    else:
        lines = read_file_lines(resolved)
        if lines_cache is not None:
            lines_cache[resolved] = lines
    if lines is None:
        return "FILE_UNREADABLE"
    # 7 LINE_OUT_OF_RANGE
    if loc["end"] > len(lines):
        return "LINE_OUT_OF_RANGE"
    # 8 EVIDENCE_NOT_FOUND (character-for-character timestamp match; strict increase ⇒ at most one)
    entry = None
    for e in evidence_entries or []:
        if e["ts"] == evidence_timestamp:
            entry = e
            break
    if entry is None:
        return "EVIDENCE_NOT_FOUND"
    # 9 EVIDENCE_MISMATCH
    if (entry["path"] != loc["path"] or entry["start"] > loc["start"]
            or entry["end"] < loc["end"]):
        return "EVIDENCE_MISMATCH"
    # 10 CONTENT_CHANGED
    if len(lines) < entry["end"]:
        return "CONTENT_CHANGED"
    if evidence_hash(lines, entry["start"], entry["end"]) != entry["sha256"]:
        return "CONTENT_CHANGED"
    # 11 EXCERPT_MISMATCH (R7.15: all-occurrence coverage semantics)
    block = normalized_text("\n".join(lines[loc["start"] - 1:loc["end"]]))
    e_norm = normalized_text(excerpt)
    if not _excerpt_covers_nonempty_line(block, e_norm):
        return "EXCERPT_MISMATCH"
    return None


def _excerpt_covers_nonempty_line(block, e_norm):
    """Among all occurrences of e in B, is there one that fully contains some nonempty covered line span?"""
    if e_norm == "":
        return False
    spans = []
    off = 0
    for line in block.split("\n"):
        spans.append((off, off + len(line)))
        off += len(line) + 1
    start = 0
    while True:
        i = block.find(e_norm, start)
        if i < 0:
            return False
        j = i + len(e_norm)
        for s, t in spans:
            if s < t and i <= s and j >= t:
                return True
        start = i + 1


def uncited_location_refs(finding):
    """Location_References in title/description not covered by any Citation (R7.16).

    Returns the list of uncovered Location values (one per uncovered reference).
    """
    refs = extract_location_references(finding.get("title") or "") \
        + extract_location_references(finding.get("description") or "")
    cits = []
    for c in finding.get("citations") or []:
        loc, err = parse_location(c.get("location"))
        if err is None:
            cits.append(loc)
    uncovered = []
    for ref in refs:
        covered = any(
            c["path"] == ref["path"]
            and c["start"] <= ref["start"] and ref["end"] <= c["end"]
            for c in cits)
        if not covered:
            uncovered.append(ref)
    return uncovered
