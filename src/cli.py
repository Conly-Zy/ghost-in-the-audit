#!/usr/bin/env python3
"""Thin CLI wrapper for Ghost in the Audit.

    python -m src.cli validate reports/findings.json   # pre-flight a findings file
    python -m src.cli audit demo/sample-repo           # print the kirocrew spawn command
    python -m src.cli audit demo/sample-repo --run     # ...and actually run it

`validate` runs the exact same schema the citation-gate hook enforces, so a
report that passes here will pass the gate.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # .../src, so `evidence` imports standalone

from evidence.schema import FindingsError, parse_findings  # noqa: E402


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"citation-gate: no such file: {path}", file=sys.stderr)
        return 2
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"citation-gate: unreadable findings file ({exc})", file=sys.stderr)
        return 2
    try:
        findings = parse_findings(doc)
    except FindingsError as exc:
        print("citation-gate: REJECTED — missing/invalid evidence:", file=sys.stderr)
        for reason in exc.rejections:
            print(f"  - {reason}", file=sys.stderr)
        return 2
    print(f"citation-gate: OK — {len(findings)} finding(s), all fully cited")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    prompt = (
        f"Audit {args.target} for anomalies. Every finding must carry file:line "
        "and verbatim log evidence. Findings without citations will be blocked "
        "by the citation-gate hook."
    )
    command = ["kirocrew", "spawn", "run", prompt]
    if args.run:
        if shutil.which("kirocrew") is None:
            print(
                "kirocrew not found on PATH — run this from an environment with "
                "Kiro installed, or drop --run to just print the command.",
                file=sys.stderr,
            )
            return 1
        print("+ " + shlex.join(command), file=sys.stderr)
        return subprocess.run(command).returncode
    print(shlex.join(command))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ghost-in-the-audit")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate", help="pre-flight a findings file against the evidence schema")
    p_validate.add_argument("file")

    p_audit = sub.add_parser("audit", help="print (or run) the kirocrew spawn command for a target repo")
    p_audit.add_argument("target", nargs="?", default="demo/sample-repo")
    p_audit.add_argument("--run", action="store_true", help="execute the spawn command instead of printing it")

    args = parser.parse_args(argv)
    if args.cmd == "validate":
        return cmd_validate(args)
    return cmd_audit(args)


if __name__ == "__main__":
    sys.exit(main())
