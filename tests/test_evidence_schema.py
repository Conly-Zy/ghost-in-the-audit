"""Property tests for the citation gate — the core invariant of Ghost in the Audit:

    For every finding the gate allows, the citation is complete:
    repo-relative path + ordered line range + at least one log evidence
    entry with a timezone-aware ISO-8601 timestamp.

Hypothesis generates the malformations (missing fields, schema drift, broken
timestamps, inverted ranges, path traversal); the gate must catch all of them.
A handful of subprocess tests verify the real hook entrypoint
(`python3 src/evidence/validator.py`) end to end, including fail-closed
behavior on malformed events.

Run:  python -m pytest tests/ -q
Demo: python tests/test_evidence_schema.py --demo-rejection
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.evidence import validator as gate  # noqa: E402
from src.evidence.schema import Finding as FindingModel  # noqa: E402

settings.register_profile("gate", deadline=None, max_examples=100)
settings.load_profile("gate")

# ---------------------------------------------------------------- strategies

SAFE_ALPHABET = st.characters(min_codepoint=32, max_codepoint=0x2FFF, blacklist_categories=("Cs",))

claims = st.text(alphabet=SAFE_ALPHABET, min_size=8, max_size=120)
signatures = st.from_regex(r"[A-Za-z][A-Za-z0-9_-]{2,30}", fullmatch=True)
paths = st.builds(
    lambda d, f: f"{d}/{f}",
    st.sampled_from(["src", "lib", "api", "payments", "core", "utils"]),
    st.sampled_from(["retry.ts", "charge.ts", "cache.ts", "server.ts", "main.py", "handler.go"]),
)
timezones = st.sampled_from(["Z", "+00:00", "+08:00", "-05:00"])


@st.composite
def iso_timestamps(draw):
    day = draw(st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31)))
    moment = draw(st.times())
    micro = f".{moment.microsecond:06d}" if moment.microsecond else ""
    return (
        f"{day.isoformat()}T{moment.hour:02d}:{moment.minute:02d}:"
        f"{moment.second:02d}{micro}{draw(timezones)}"
    )


evidence_entries = st.builds(
    lambda ts, sig, line, msg: {
        "timestamp": ts,
        "signature": sig,
        "log_path": "logs/app.log",
        "line": line,
        "message": msg,
    },
    iso_timestamps(),
    signatures,
    st.one_of(st.none(), st.integers(min_value=1, max_value=500)),
    st.one_of(st.none(), st.text(alphabet=SAFE_ALPHABET, max_size=80)),
)


@st.composite
def valid_findings(draw):
    start = draw(st.integers(min_value=1, max_value=10_000))
    end = draw(st.integers(min_value=start, max_value=start + 500))
    agent = draw(st.one_of(st.none(), st.just("log-whisperer"), st.just("data-flow-tracker")))
    finding = {
        "claim": draw(claims),
        "path": draw(paths),
        "line_start": start,
        "line_end": end,
        "log_evidence": draw(st.lists(evidence_entries, min_size=1, max_size=3)),
        "confidence": draw(st.sampled_from(["high", "medium", "low"])),
    }
    if agent is not None:
        finding["agent"] = agent
    return finding


def write_event(content: str, path: str = "reports/findings.json") -> dict:
    """A PreToolUse fs_write hook event, shaped like the Kiro event document."""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "fs_write",
        "cwd": str(ROOT),
        "session_id": "property-test",
        "tool_input": {"path": path, "content": content},
    }


def schema_accepts(finding: dict) -> bool:
    try:
        FindingModel.model_validate(finding)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- properties

@given(valid_findings())
def test_complete_citation_is_always_allowed(finding):
    """A fully cited finding passes the gate, alone or inside a findings list."""
    for content in (json.dumps(finding), json.dumps({"findings": [finding]})):
        code, reason = gate.evaluate_event(write_event(content))
        assert code == gate.ALLOW, reason


@given(valid_findings(), st.sampled_from(["claim", "path", "line_start", "line_end", "log_evidence", "confidence"]))
def test_missing_required_field_is_always_denied(finding, field):
    """Remove any one required field → the finding must not pass."""
    broken = dict(finding)
    del broken[field]
    code, reason = gate.evaluate_event(write_event(json.dumps({"findings": [broken]})))
    assert code == gate.DENY, f"gate allowed a finding missing {field!r}: {reason}"


@given(valid_findings(), st.sampled_from(["lineStart", "evidence", "proof", "file", "lines"]))
def test_unknown_field_is_always_denied(finding, extra):
    """Schema drift (unknown/misspelled fields) is a rejection, not a warning."""
    broken = dict(finding)
    broken[extra] = "drift"
    code, reason = gate.evaluate_event(write_event(json.dumps(broken)))
    assert code == gate.DENY, f"gate allowed a finding with unknown field {extra!r}: {reason}"


BAD_TIMESTAMPS = [
    "2026-10-31T02:14:07",      # naive — no timezone, ambiguous
    "2026-10-31 02:14:07",      # space-separated and naive
    "not-a-date",
    "",
    "31/10/2026 02:14:07",
]


@given(valid_findings(), st.sampled_from(BAD_TIMESTAMPS))
def test_naive_or_broken_timestamps_are_denied(finding, ts):
    broken = copy.deepcopy(finding)
    broken["log_evidence"][0]["timestamp"] = ts
    code, reason = gate.evaluate_event(write_event(json.dumps({"findings": [broken]})))
    assert code == gate.DENY, f"gate allowed an unanchored timestamp {ts!r}: {reason}"


@given(valid_findings(), st.text(alphabet=SAFE_ALPHABET, max_size=40))
def test_gate_verdict_matches_schema_on_arbitrary_timestamps(finding, ts):
    """Meta-consistency: the gate denies exactly when the schema rejects."""
    broken = copy.deepcopy(finding)
    broken["log_evidence"][0]["timestamp"] = ts
    code, reason = gate.evaluate_event(write_event(json.dumps({"findings": [broken]})))
    accepted = schema_accepts(broken)
    if accepted:
        assert code == gate.ALLOW, reason
    else:
        assert code == gate.DENY, f"gate allowed a finding the schema rejects ({ts!r}): {reason}"


@given(valid_findings(), st.integers(min_value=-5, max_value=9_999))
def test_inverted_line_range_is_denied(finding, line_end):
    assume(line_end < finding["line_start"])
    broken = dict(finding)
    broken["line_end"] = line_end
    code, reason = gate.evaluate_event(write_event(json.dumps(broken)))
    assert code == gate.DENY, f"gate allowed an inverted line range: {reason}"


@given(valid_findings(), st.integers(max_value=0))
def test_nonpositive_line_start_is_denied(finding, line_start):
    broken = dict(finding)
    broken["line_start"] = line_start
    broken["line_end"] = max(finding["line_end"], 1)
    code, reason = gate.evaluate_event(write_event(json.dumps(broken)))
    assert code == gate.DENY, f"gate allowed line_start={line_start}: {reason}"


BAD_PATHS = [
    "../outside.ts",
    "/etc/passwd",
    "",
    "src/../../../etc/passwd",
    "C:\\Windows\\evil.ts",
    "src//double//slash.ts",
]


@given(valid_findings(), st.sampled_from(BAD_PATHS))
def test_path_violations_are_denied(finding, path):
    broken = dict(finding)
    broken["path"] = path
    code, reason = gate.evaluate_event(write_event(json.dumps(broken)))
    assert code == gate.DENY, f"gate allowed a bad path {path!r}: {reason}"


@given(valid_findings())
def test_empty_log_evidence_is_denied(finding):
    broken = dict(finding)
    broken["log_evidence"] = []
    code, reason = gate.evaluate_event(write_event(json.dumps(broken)))
    assert code == gate.DENY, f"gate allowed a claim without log evidence: {reason}"


@given(valid_findings(), st.sampled_from(["src/cli.py", "docs/notes.md", "README.md", "tests/test_x.py"]))
def test_non_report_writes_pass_untouched(finding, path):
    """The gate only polices writes that publish findings — source and docs pass."""
    code, reason = gate.evaluate_event(write_event(json.dumps(finding), path=path))
    assert code == gate.ALLOW, reason


@given(st.text(alphabet=SAFE_ALPHABET, max_size=400))
def test_non_finding_content_is_never_blocked(content):
    assume(not any(marker in content for marker in ('"claim"', '"findings"', '"log_evidence"')))
    code, reason = gate.evaluate_event(write_event(content))
    assert code == gate.ALLOW, reason


# ------------------------------------------------- end-to-end (subprocess)

GOOD_FINDING = {
    "claim": "The retry handler double-counts timeout errors.",
    "path": "src/payments/retry.ts",
    "line_start": 24,
    "line_end": 31,
    "log_evidence": [
        {
            "timestamp": "2026-10-31T02:12:47Z",
            "signature": "DOUBLE_COUNT_TIMEOUT",
            "log_path": "logs/app.log",
            "line": 14,
        }
    ],
    "confidence": "high",
}


def run_gate(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "src" / "evidence" / "validator.py")],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
    )


def test_gate_allows_complete_finding_end_to_end():
    result = run_gate(json.dumps(write_event(json.dumps({"findings": [GOOD_FINDING]}))))
    assert result.returncode == 0, result.stderr
    assert "allowed" in result.stdout


def test_gate_denies_uncited_finding_end_to_end():
    uncited = copy.deepcopy(GOOD_FINDING)
    del uncited["log_evidence"]
    result = run_gate(json.dumps(write_event(json.dumps({"findings": [uncited]}))))
    assert result.returncode == 2, result.stdout
    assert "log_evidence" in result.stderr  # the missing field is named on STDERR
    assert "finding[0]" in result.stderr


def test_gate_fails_closed_on_malformed_event():
    result = run_gate("this is not json {")
    assert result.returncode == 1
    assert "fail-closed" in result.stderr


def test_gate_fails_closed_on_non_object_event():
    result = run_gate("42")
    assert result.returncode == 1


def test_cli_validate_preflight(tmp_path):
    good = tmp_path / "findings.json"
    good.write_text(json.dumps({"findings": [GOOD_FINDING]}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", "validate", str(good)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "all fully cited" in result.stdout

    bad = tmp_path / "uncited.json"
    uncited = copy.deepcopy(GOOD_FINDING)
    del uncited["log_evidence"]
    uncited["path"] = "../escape.ts"
    bad.write_text(json.dumps({"findings": [uncited]}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", "validate", str(bad)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert result.returncode == 2
    assert "log_evidence" in result.stderr


# ---------------------------------------------------------------- demo mode

def demo_rejection() -> int:
    """Reproduce the interception scenario on demand: an uncited finding hits the gate."""
    uncited = copy.deepcopy(GOOD_FINDING)
    del uncited["log_evidence"]
    event = write_event(json.dumps({"findings": [uncited]}))
    result = run_gate(json.dumps(event))

    print("=== citation-gate demo: an uncited finding hits the gate ===")
    print(f"hook event: fs_write -> reports/findings.json (claim present, no log_evidence)")
    print(f"exit code : {result.returncode}   (2 = deny)")
    print("--- STDERR returned to the agent ---")
    print(result.stderr.rstrip())
    if result.returncode == 2 and "log_evidence" in result.stderr:
        print("--- demo OK: the gate blocked the uncited finding and named the missing field ---")
        return 0
    print("--- demo FAILED: expected deny (exit 2) naming the missing field ---", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if "--demo-rejection" in sys.argv:
        sys.exit(demo_rejection())
    print("usage: python tests/test_evidence_schema.py --demo-rejection", file=sys.stderr)
    sys.exit(2)
