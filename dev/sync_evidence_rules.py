#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dev/sync_evidence_rules.py — inject Evidence_Rules into the five agent definitions and verify consistency.

Canonical source: .kiro/steering/evidence-rules.md (the full text to inject is
between `<!-- BEGIN evidence-rules -->` and `<!-- END evidence-rules -->`).
Targets: the same-named marker blocks in
.kiro/agents/{orchestrator,cartographer,security-auditor,
dependency-auditor,quality-auditor}.md.

Usage:
    python3 dev/sync_evidence_rules.py          # inject (idempotent)
    python3 dev/sync_evidence_rules.py --check  # verify byte-for-byte equality only (for CI)
"""

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, ".kiro", "steering", "evidence-rules.md")
AGENTS = ["orchestrator", "cartographer", "security-auditor",
          "dependency-auditor", "quality-auditor"]

BEGIN = "<!-- BEGIN evidence-rules -->"
END = "<!-- END evidence-rules -->"

BLOCK_RE = re.compile(re.escape(BEGIN) + r"\n(.*?\n)?" + re.escape(END), re.DOTALL)


def extract_source(text):
    m = re.search(re.escape(BEGIN) + r"\n(.*?\n)?" + re.escape(END), text, re.DOTALL)
    if m is None:
        sys.exit("evidence-rules.md is missing its marker block")
    return m.group(1) or ""


def main():
    check_only = "--check" in sys.argv
    src_text = open(SOURCE, encoding="utf-8").read()
    payload = extract_source(src_text)
    ok = True
    for name in AGENTS:
        path = os.path.join(REPO, ".kiro", "agents", name + ".md")
        if not os.path.exists(path):
            print("missing: %s" % path)
            ok = False
            continue
        agent_text = open(path, encoding="utf-8").read()
        m = BLOCK_RE.search(agent_text)
        if m is None:
            print("missing marker block: %s" % path)
            ok = False
            continue
        current = m.group(0)
        desired = BEGIN + "\n" + payload + "\n" + END
        if current == desired:
            print("in sync: %s" % name)
        elif check_only:
            print("out of sync (needs sync): %s" % path)
            ok = False
        else:
            agent_text = agent_text[:m.start()] + desired + agent_text[m.end():]
            with open(path, "w", encoding="utf-8") as f:
                f.write(agent_text)
            print("synced: %s" % path)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
