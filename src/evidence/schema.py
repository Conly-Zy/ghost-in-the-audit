"""Evidence schema for Ghost in the Audit.

A finding is only a finding if it carries a verifiable citation: a
repo-relative path, an ordered line range, and at least one log evidence entry
with a timezone-aware ISO-8601 timestamp. Anything else is `uncited` — and the
citation gate rejects it.

The schema is `extra="forbid"` on purpose: unknown or misspelled fields
(`lineStart`, `evidence`, …) are rejections, not warnings. Schema drift is how
ghosts get in.

Single source of truth for: the citation-gate hook (`validator.py`), the
Citation Auditor's checks, and the property tests.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingStatus(str, Enum):
    FORWARDED = "forwarded"
    UNCITED = "uncited"
    REJECTED = "rejected"


class FindingsError(Exception):
    """Aggregated rejection reasons, ready to be printed to the hook's STDERR."""

    def __init__(self, rejections: list[str]):
        self.rejections = rejections
        super().__init__("; ".join(rejections))


def validate_repo_path(path: str) -> str:
    """Require a repo-relative POSIX path; absolute paths and `..` traversal are rejected."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty repo-relative path")
    p = path.strip().replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise ValueError(f"path must be relative, got {path!r}")
    parts = p.split("/")
    if any(part == ".." for part in parts):
        raise ValueError(f"path must not traverse outside the repo: {path!r}")
    if any(part == "" for part in parts):
        raise ValueError(f"path must not contain empty segments: {path!r}")
    return "/".join(parts)


class LogEvidence(BaseModel):
    """One anchor into a raw log: a verbatim timestamp plus the anomaly signature."""

    model_config = ConfigDict(extra="forbid")

    timestamp: str
    signature: str
    log_path: Optional[str] = None
    line: Optional[int] = Field(default=None, ge=1)
    message: Optional[str] = None

    @field_validator("timestamp")
    @classmethod
    def _timestamp_must_be_tz_aware_iso8601(cls, v: str) -> str:
        text = v.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                "timestamp must be ISO-8601, e.g. 2026-10-31T02:14:07Z"
            ) from None
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise ValueError(
                "timestamp must carry an explicit timezone (Z or ±HH:MM); "
                "a naive timestamp is ambiguous and therefore unanchored"
            )
        return v.strip()

    @field_validator("signature")
    @classmethod
    def _signature_shape(cls, v: str) -> str:
        if not isinstance(v, str) or not SIGNATURE_RE.fullmatch(v.strip()):
            raise ValueError("signature must match [A-Za-z0-9_-]{3,64}")
        return v.strip()

    @field_validator("log_path")
    @classmethod
    def _log_path_relative(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return validate_repo_path(v)


class Finding(BaseModel):
    """A falsifiable claim that must point at its evidence."""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=8)
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    log_evidence: list[LogEvidence] = Field(min_length=1)
    confidence: Confidence
    agent: Optional[str] = None
    status: FindingStatus = FindingStatus.FORWARDED

    @field_validator("path")
    @classmethod
    def _path_must_be_repo_relative(cls, v: str) -> str:
        return validate_repo_path(v)

    @model_validator(mode="after")
    def _line_range_ordered(self) -> "Finding":
        if self.line_end < self.line_start:
            raise ValueError(
                f"line_end ({self.line_end}) must be >= line_start ({self.line_start})"
            )
        return self


def _format_pydantic_error(index: int, exc: Exception) -> str:
    parts = []
    for err in getattr(exc, "errors", lambda: [])():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "<root>"
        msg = err.get("msg", "invalid")
        expected = err.get("type", "")
        if expected == "missing":
            msg = f"missing required field '{loc}'"
        parts.append(f"{loc}: {msg}" if expected != "missing" else msg)
    if not parts:  # not a pydantic error — fall back to the message
        parts.append(str(exc))
    return f"finding[{index}]: " + "; ".join(parts)


def parse_findings(obj: object) -> list[Finding]:
    """Validate a finding object or a `{"findings": [...]}` document.

    Raises `FindingsError` carrying one human-readable reason per rejected
    finding; returns the validated findings otherwise (possibly an empty list
    for an empty `findings` document — an empty audit is a legitimate result).
    """
    if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
        items = obj["findings"]
    elif isinstance(obj, dict) and "claim" in obj:
        items = [obj]
    else:
        raise FindingsError(
            ["no parseable finding: expected a finding object or {'findings': [...]}"]
        )

    rejections: list[str] = []
    accepted: list[Finding] = []
    for i, item in enumerate(items):
        try:
            accepted.append(Finding.model_validate(item))
        except Exception as exc:  # noqa: BLE001 — every failure becomes a reason
            rejections.append(_format_pydantic_error(i, exc))
    if rejections:
        raise FindingsError(rejections)
    return accepted
