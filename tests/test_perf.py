#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Performance acceptance (tasks.md 9.4 / T7): measured documented deadline budgets.

- 1000 Run_Directories pre+post ≤ 1s → tests/test_hooks.py
- Gate check ≤ 15s with a 10⁴-entry Evidence_Log (A17, design 9)
- Start tree scan ≤ 20s for a Target with ~10⁴ files (design 9)
Run: python3 -m unittest tests.test_perf -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, ".kiro", "scripts")
sys.path.insert(0, SCRIPTS)

import ghostlib as g  # noqa: E402


def make_ws():
    root = tempfile.mkdtemp(prefix="ghost-perf-")
    os.makedirs(os.path.join(root, "runs"))
    os.makedirs(os.path.join(root, "control"))
    scripts = os.path.join(root, ".kiro", "scripts")
    os.makedirs(scripts)
    for name in os.listdir(SCRIPTS):
        if name.endswith(".py") or name.endswith(".json"):
            shutil.copy(os.path.join(SCRIPTS, name), scripts)
    return root


def cleanup(root):
    shutil.rmtree(root, ignore_errors=True)


class TestGateWithLargeEvidenceLog(unittest.TestCase):
    def test_gate_within_15s_with_10k_entries(self):
        root = make_ws()
        try:
            os.makedirs(os.path.join(root, "targets", "big", "src"))
            with open(os.path.join(root, "targets", "big", "src", "a.py"), "w") as f:
                f.write("hello = 1\nworld = 2\n")
            run_dir = os.path.join(root, "runs", "run-1731690000123-abcdef")
            os.makedirs(os.path.join(run_dir, "inbox"))
            for name in ("findings.jsonl", "rejections.jsonl"):
                with open(os.path.join(run_dir, name), "wb"):
                    pass
            with open(os.path.join(run_dir, "state.json"), "wb") as f:
                f.write(json.dumps({"run_id": "run-1731690000123-abcdef",
                                    "target": "big", "status": "active",
                                    "started_at": "2026-01-01T00:00:00.000Z"},
                                   ensure_ascii=False).encode())
            # 10⁴ evidence entries: the vast majority point at unrelated files; the last one covers a.py:1-2
            lines = g.split_lines(b"hello = 1\nworld = 2\n")
            target_entry = {
                "ts": "2026-12-31T23:59:59.999Z", "run_id": "run-1731690000123-abcdef",
                "path": "src/a.py", "start": 1, "end": 2,
                "sha256": g.evidence_hash(lines, 1, 2), "session": "s",
            }
            entries = []
            for i in range(9999):
                entries.append(g.evidence_entry_dump({
                    "ts": "2026-01-01T00:00:00.%03dZ" % (i % 1000) if False else
                          "2026-%02d-%02dT%02d:%02d:%02d.%03dZ" % (
                              1 + i // 2800, 1 + (i % 28), i % 24, i % 60, i % 60,
                              i % 1000),
                    "run_id": "run-1731690000123-abcdef",
                    "path": "src/other_%d.py" % (i % 97),
                    "start": 1, "end": 1, "sha256": "b" * 64, "session": "s",
                }))
            entries.append(g.evidence_entry_dump(target_entry))
            with open(os.path.join(run_dir, "evidence.jsonl"), "wb") as f:
                f.write(("\n".join(entries) + "\n").encode())
            # do the timestamps need to be strictly increasing? parse_evidence_log checks
            # increasing order — the generation above is non-increasing and would corrupt!
            # rebuild as needed after verifying:
            entries_parsed, corrupt = g.parse_evidence_log("\n".join(entries) + "\n")
            if entries_parsed is None:
                # strictly increasing rebuild
                prev = None
                entries = []
                base = g.parse_ts("2026-01-01T00:00:00.000Z")
                for i in range(9999):
                    dt = g.ts_plus_ms(base, i)
                    entries.append(g.evidence_entry_dump({
                        "ts": g.format_ts(dt),
                        "run_id": "run-1731690000123-abcdef",
                        "path": "src/other_%d.py" % (i % 97),
                        "start": 1, "end": 1, "sha256": "b" * 64, "session": "s",
                    }))
                entries.append(g.evidence_entry_dump(
                    dict(target_entry, ts="2027-01-01T00:00:00.000Z")))
                target_entry = dict(target_entry, ts="2027-01-01T00:00:00.000Z")
                with open(os.path.join(run_dir, "evidence.jsonl"), "wb") as f:
                    f.write(("\n".join(entries) + "\n").encode())
            sub = json.dumps({
                "run_id": "run-1731690000123-abcdef", "agent": "Security_Auditor",
                "title": "t", "category": "security", "severity": "high",
                "description": "d",
                "citations": [{"location": "src/a.py:1-2",
                               "evidence_timestamp": target_entry["ts"],
                               "excerpt": "hello = 1\nworld = 2"}],
            }, ensure_ascii=False)
            payload = os.path.join(root, "payload.json")
            with open(payload, "wb") as f:
                f.write(json.dumps({
                    "tool_name": "fs_write", "session_id": "s",
                    "tool_input": {"path": "runs/run-1731690000123-abcdef/inbox/x.json",
                                   "content": sub}}).encode())
            scripts2 = os.path.join(root, ".kiro", "scripts")
            t0 = time.monotonic()
            proc = subprocess.run(
                [sys.executable, os.path.join(scripts2, "citation_gate.py"), payload],
                cwd=root, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            elapsed = time.monotonic() - t0
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            self.assertLess(elapsed, 15.0, "Gate check %.2fs exceeded the 15s budget" % elapsed)
        finally:
            cleanup(root)


class TestStartScanLargeTarget(unittest.TestCase):
    def test_start_within_20s_with_10k_files(self):
        root = make_ws()
        try:
            target = os.path.join(root, "targets", "huge")
            for i in range(100):
                d = os.path.join(target, "d%02d" % i)
                os.makedirs(d)
                for j in range(100):
                    with open(os.path.join(d, "f%03d.py" % j), "w") as f:
                        f.write("pass\n")
            payload = os.path.join(root, "payload.json")
            with open(payload, "wb") as f:
                f.write(json.dumps({
                    "tool_name": "fs_write",
                    "tool_input": {"path": os.path.join(root, "control", "start.json"),
                                   "content": json.dumps({"target": "huge"})}}).encode())
            scripts2 = os.path.join(root, ".kiro", "scripts")
            t0 = time.monotonic()
            proc = subprocess.run(
                [sys.executable, os.path.join(scripts2, "hook_launcher.py"), "pre"],
                cwd=root, stdin=open(payload, "rb"), capture_output=True, timeout=60)
            elapsed = time.monotonic() - t0
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            scan = g.scan_runs(os.path.join(root, "runs"))
            self.assertIsNotNone(scan["active_run_id"])
            self.assertLess(elapsed, 20.0, "Start scan %.2fs exceeded the 20s budget" % elapsed)
        finally:
            cleanup(root)


if __name__ == "__main__":
    unittest.main()
