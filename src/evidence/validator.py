#!/usr/bin/env python3
"""citation-gate — fail-closed evidence validator for the Kiro PreToolUse hook.

STDIN  : a Kiro hook event JSON document
         (hook_event_name, tool_name, tool_input, cwd, session_id, ...)
STDOUT : on allow, one status line (injected as context)
STDERR : on deny, the rejection reasons (returned to the LLM for repair)

Exit codes
    0  allow        — the write does not publish findings, or every finding
                      carries a complete citation
    2  deny         — finding-shaped content with missing/invalid evidence;
                      reasons on STDERR
    1  fail-closed  — no verdict possible (malformed event, unreadable STDIN,
                      crash, uninspectable content). For PreToolUse gating a
                      missing verdict resolves to *blocked*: breaking this hook
                      can never silently disable the no-evidence-no-forwarding
                      policy.

Scope: only writes that publish findings are gated — paths matching
`reports/`, `*findings.json`, or `*audit-report*`. Ordinary writes (source
code, docs, prose) pass untouched, which keeps the gate precise instead of
heuristically sniffing every write.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence.schema import FindingsError, parse_findings  # noqa: E402

ALLOW = 0
DENY = 2
FAIL_CLOSED = 1

CONTENT_KEYS = ("content", "file_text", "new_string", "text")
FINDING_MARKERS = ('"claim"', '"findings"', '"log_evidence"')
REPORT_PATH_RE = re.compile(r"(^|/)reports/|findings\.json$|audit-report", re.IGNORECASE)


def _report_write(tool_input: dict) -> tuple[bool, str]:
    """Is this write publishing audit findings? Returns (is_report_write, path)."""
    path = tool_input.get("path") or tool_input.get("file_path") or ""
    path = str(path)
    return bool(path and REPORT_PATH_RE.search(path)), path


def _extract_content(tool_input: dict) -> str | None:
    """The text the tool call would write, or None if it cannot be inspected."""
    for key in CONTENT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
    return None


def _candidate_json_objects(content: str) -> Iterator[dict]:
    """Yield every JSON object embedded in the content.

    Whole-document JSON first, then fenced ```json blocks, then an inline
    scan (raw_decode from each `{`) for report prose that embeds findings
    without fencing. Malformed fragments are simply skipped — the caller
    decides what "no parseable candidate" means.
    """
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            yield obj
            return
    except (json.JSONDecodeError, ValueError):
        pass

    for block in re.findall(r"```(?:json)?\s*(.*?)```", content, flags=re.DOTALL):
        try:
            obj = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            yield obj

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", content):
        try:
            obj, _end = decoder.raw_decode(content[match.start():])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            yield obj


def evaluate_event(event: dict) -> tuple[int, str]:
    """Pure decision function: hook event in, (exit code, reason) out."""
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    is_report, path = _report_write(tool_input)
    if not is_report:
        return ALLOW, f"write to {path!r} does not publish findings; gate not applied"

    content = _extract_content(tool_input)
    if content is None:
        return FAIL_CLOSED, (
            f"citation-gate: report write to {path!r} carries no inspectable "
            "content; no verdict possible — blocking (fail-closed)"
        )

    if not any(marker in content for marker in FINDING_MARKERS):
        return ALLOW, f"report {path!r} contains no finding-shaped content; allowed"

    # Deduplicate candidates found by different extraction passes.
    candidates: list[dict] = []
    seen: set[str] = set()
    for obj in _candidate_json_objects(content):
        if "claim" not in obj and "findings" not in obj:
            continue
        key = json.dumps(obj, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            candidates.append(obj)

    if not candidates:
        return DENY, (
            "citation-gate: report carries finding-shaped content but no valid "
            "JSON finding could be parsed — no evidence, no forwarding"
        )

    reasons: list[str] = []
    count = 0
    for i, obj in enumerate(candidates):
        try:
            count += len(parse_findings(obj))
        except FindingsError as exc:
            for reason in exc.rejections:
                reasons.append(f"candidate[{i}] {reason}")

    if reasons:
        header = (
            "citation-gate: REJECTED — findings below lack verifiable citations "
            "(path + line range + at least one timezone-aware log timestamp). "
            "No evidence, no forwarding. Repair the evidence and retry:"
        )
        return DENY, header + "\n" + "\n".join(f"  - {r}" for r in reasons)

    return ALLOW, f"citation-gate: allowed {path!r} — {count} finding(s), all fully cited"


def _log_verdict(event: dict, code: int, reason: str) -> None:
    """Best-effort interception trail for the demo. Never breaks the gate."""
    try:
        dest = os.environ.get("CITATION_GATE_LOG", "logs/citation-gate.log")
        log_path = Path(dest)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        tool_input = event.get("tool_input") or {}
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": event.get("session_id"),
            "tool_name": event.get("tool_name"),
            "path": (tool_input or {}).get("path") or (tool_input or {}).get("file_path"),
            "verdict": {ALLOW: "allow", DENY: "deny", FAIL_CLOSED: "fail-closed-blocked"}.get(
                code, str(code)
            ),
            "reason": reason[:500],
        }
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — logging must never take the gate down
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("hook event must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        reason = f"citation-gate: unreadable hook event ({exc}); blocking (fail-closed)"
        print(reason, file=sys.stderr)
        _log_verdict({}, FAIL_CLOSED, reason)
        return FAIL_CLOSED

    try:
        code, reason = evaluate_event(event)
    except Exception as exc:  # noqa: BLE001 — any validator crash must fail closed
        code, reason = FAIL_CLOSED, (
            f"citation-gate: validator crashed ({exc.__class__.__name__}: {exc}); "
            "blocking (fail-closed)"
        )

    _log_verdict(event, code, reason)
    if code == DENY:
        print(reason, file=sys.stderr)
    else:
        print(reason)
    return code


if __name__ == "__main__":
    sys.exit(main())
