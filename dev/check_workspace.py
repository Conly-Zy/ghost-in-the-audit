#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dev/check_workspace.py — check workspace leftovers and layout health.

- `.tmp-*` staging directories under runs/ (leftovers from a Start
  create-protocol crash; safe to delete)
- Run_Directories under runs/ missing state.json (would trigger STATE_CORRUPT;
  needs manual handling)
- multiple active Audit_Runs under runs/ (violates the single-instance invariant)

Usage: python3 dev/check_workspace.py [--purge-tmp]
Exit codes: 0 = healthy; 1 = problems found.
"""

import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    purge = "--purge-tmp" in sys.argv
    runs_dir = os.path.join(REPO, "runs")
    problems = []
    for name in sorted(os.listdir(runs_dir)):
        path = os.path.join(runs_dir, name)
        if name.startswith(".tmp-"):
            problems.append("staging leftover: %s" % path)
            if purge:
                shutil.rmtree(path, ignore_errors=True)
                print("deleted: %s" % path)
            continue
        if name.startswith("."):
            continue
        if not os.path.isdir(path):
            continue
        state = os.path.join(path, "state.json")
        if not os.path.exists(state):
            problems.append("missing Run_State_Record (would trigger STATE_CORRUPT): %s" % path)
    # multiple actives
    import json
    actives = []
    for name in sorted(os.listdir(runs_dir)):
        p = os.path.join(runs_dir, name, "state.json")
        if os.path.exists(p):
            try:
                st = json.load(open(p, encoding="utf-8"))
                if isinstance(st, dict) and st.get("status") == "active":
                    actives.append(os.path.dirname(p))
            except Exception:
                pass
    if len(actives) > 1:
        problems.append("multiple active Audit_Runs: %s" % ", ".join(actives))
    for p in problems:
        print(p)
    print("healthy" if not problems else "found %d problem(s)%s" % (
        len(problems), " (staging leftovers can be cleared with --purge-tmp)" if any("staging" in p for p in problems) else ""))
    sys.exit(0 if not problems else 1)


if __name__ == "__main__":
    main()
