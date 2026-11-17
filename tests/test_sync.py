#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evidence_Rules sync consistency tests (tasks.md 8.2)."""

import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, ".kiro", "scripts"))

import ghostlib as g  # noqa: E402

AGENTS = ["orchestrator", "cartographer", "security-auditor",
          "dependency-auditor", "quality-auditor"]
BEGIN = "<!-- BEGIN evidence-rules -->"
END = "<!-- END evidence-rules -->"


import re as _re

# Same anchoring semantics as dev/sync_evidence_rules.py: BEGIN must be
# immediately followed by a newline, so inline-code markers in the body text
# cannot match
_BLOCK_RE = _re.compile(_re.escape(BEGIN) + r"\n(.*?\n)?" + _re.escape(END),
                        _re.DOTALL)


def extract_block(text):
    m = _BLOCK_RE.search(text)
    assert m is not None, "marker block missing"
    return (m.group(1) or "").strip("\n")


class TestEvidenceRulesSync(unittest.TestCase):
    def test_all_agents_carry_identical_rules(self):
        """A15 / R12.1: the five definitions' inline contents are byte-for-byte identical."""
        source_path = os.path.join(REPO, ".kiro", "steering", "evidence-rules.md")
        source = extract_block(open(source_path, encoding="utf-8").read())
        self.assertTrue(source.strip())  # the canonical source is non-empty
        for name in AGENTS:
            path = os.path.join(REPO, ".kiro", "agents", name + ".md")
            body = open(path, encoding="utf-8").read()
            self.assertIn(BEGIN, body, name)
            self.assertIn(END, body, name)
            self.assertEqual(extract_block(body), source,
                             "%s's Evidence_Rules copy differs from the canonical source" % name)

    def test_check_mode_passes(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "dev", "sync_evidence_rules.py"), "--check"],
            capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode() + proc.stderr.decode())

    def test_rules_consistent_with_requirements(self):
        """R12.3: the Evidence_Rules numbers match requirements 5/6/7."""
        source_path = os.path.join(REPO, ".kiro", "steering", "evidence-rules.md")
        text = open(source_path, encoding="utf-8").read()
        for literal in ("1 to 10", "1–200", "1–4000", "at most 1024", "at most 4000",
                        "1 to 999999999", "50 lines", "architecture", "embedded-instruction",
                        "ACCEPTED:", "REJECTED_COUNT:", "ABANDONED_COUNT:",
                        "REVIEWED_PATHS:", "SYSTEM_REASON_CODES:"):
            self.assertIn(literal, text)
        # key structural facts match the ghostlib constants
        self.assertEqual(len(g.AUDIT_AGENTS), 4)
        self.assertEqual(g.MAX_CITATIONS, 10)
        self.assertEqual(g.MAX_SPAN_LINES, 50)
        self.assertEqual(g.TITLE_MAX, 200)
        self.assertEqual(g.DESCRIPTION_MAX, 4000)


if __name__ == "__main__":
    unittest.main()
