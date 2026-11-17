#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hook_Launcher integration tests (tasks.md 2.7 ⛔).

Drives the pre/post routing matrix with synthetic payloads (each category ×
with/without an active run × payload valid/invalid); subprocess discipline
(timeout / GATE_FAILURE rewrite / exit code 2 pass-through); with 1000
Run_Directories, unrelated calls take pre+post ≤ 1s (R15.4 / T6).

Stub scripts replace the four component scripts, logging the invoked script
name and argv to a log file.
Run: python3 -m unittest tests.test_hooks -v
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

STUB_BODY = r'''
import json, os, sys, time
log = os.environ["GHOST_TEST_LOG"]
name = os.path.basename(sys.argv[0]).replace(".py", "")
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"script": name, "argv": sys.argv[1:]}) + "\n")
mode = os.environ.get("GHOST_TEST_MODE", "")
if mode == "sleep":
    time.sleep(30)
elif mode == "exit2_bare":
    sys.stderr.write("something went wrong\n")
    sys.exit(2)
elif mode == "exit2_code":
    sys.stderr.write("TIMEOUT: x\n")
    sys.exit(2)
elif mode == "crash":
    sys.exit(70)
sys.exit(0)
'''


class LauncherFixture:
    """Synthetic workspace: copies launcher/ghostlib/, stub scripts replace the components."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ghost-fixture-")
        scripts = os.path.join(self.root, ".kiro", "scripts")
        os.makedirs(scripts)
        os.makedirs(os.path.join(self.root, "runs"))
        os.makedirs(os.path.join(self.root, "control"))
        os.makedirs(os.path.join(self.root, "targets", "demo"))
        shutil.copy(os.path.join(SCRIPTS, "ghostlib.py"), scripts)
        shutil.copy(os.path.join(SCRIPTS, "tool_catalog.json"), scripts)
        shutil.copy(os.path.join(SCRIPTS, "hook_launcher.py"), scripts)
        self.log = os.path.join(self.root, "stub-log.jsonl")
        with open(self.log, "wb"):
            pass
        for name in ("citation_gate.py", "write_guard.py",
                     "run_controller.py", "evidence_recorder.py"):
            with open(os.path.join(scripts, name), "w", encoding="utf-8") as f:
                f.write(STUB_BODY)

    def write_run(self, name, state_text):
        d = os.path.join(self.root, "runs", name)
        os.makedirs(d, exist_ok=True)
        if state_text is not None:
            with open(os.path.join(d, "state.json"), "wb") as f:
                f.write(state_text.encode("utf-8"))
        return d

    def active_state(self):
        return json.dumps({"run_id": "run-1731690000123-a1b2c3", "target": "demo",
                           "status": "active",
                           "started_at": "2026-01-01T00:00:00.000Z"},
                          ensure_ascii=False)

    def run_hook(self, mode, payload, timeout=30):
        """Runs the launcher with cwd=workspace root; returns (returncode, stdout, stderr)."""
        env = dict(os.environ)
        env["GHOST_TEST_LOG"] = self.log
        env.pop("GHOST_TEST_MODE", None)
        if mode:
            env["GHOST_TEST_MODE"] = mode
        proc = subprocess.run(
            [sys.executable, os.path.join(self.root, ".kiro", "scripts", "hook_launcher.py"), "pre"],
            cwd=self.root, input=payload.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, env=env,
        )
        return proc.returncode, proc.stdout.decode(), proc.stderr.decode()

    def run_post(self, payload, timeout=30):
        env = dict(os.environ)
        env["GHOST_TEST_LOG"] = self.log
        env.pop("GHOST_TEST_MODE", None)
        proc = subprocess.run(
            [sys.executable, os.path.join(self.root, ".kiro", "scripts", "hook_launcher.py"), "post"],
            cwd=self.root, input=payload.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, env=env,
        )
        return proc.returncode, proc.stdout.decode(), proc.stderr.decode()

    def launched(self):
        name_map = {"citation_gate.py": "Citation_Gate", "write_guard.py": "Write_Guard",
                    "run_controller.py": "Run_Controller", "evidence_recorder.py": "Evidence_Recorder"}
        out = []
        with open(self.log, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                rec["script"] = name_map.get(rec["script"] + ".py", rec["script"])
                out.append(rec)
        return out

    def payload(self, tool, **kw):
        p = {"session_id": "sess-1", "hook_event_name": "PreToolUse",
             "cwd": self.root, "tool_name": tool, "tool_input": kw}
        return json.dumps(p, ensure_ascii=False)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class TestRoutingMatrix(unittest.TestCase):
    def setUp(self):
        self.fx = LauncherFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_no_active_read_tool_passes(self):
        rc, _, err = self.fx.run_hook("", self.fx.payload("read_file", path="targets/demo/a.py"))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.fx.launched(), [])

    def test_no_active_unknown_tool_passes(self):
        rc, _, err = self.fx.run_hook("", self.fx.payload("execute_bash", command="rm -rf /"))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.fx.launched(), [])

    def test_no_active_invalid_payload_passes(self):
        rc, _, err = self.fx.run_hook("", "{not json")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.fx.launched(), [])
        rc, _, err = self.fx.run_hook("", json.dumps({"tool_input": {}}))  # missing tool_name
        self.assertEqual((rc, err), (0, ""))

    def test_no_active_inbox_write_routes_gate(self):
        rc, _, err = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="runs/run-1731690000123-a1b2c3/inbox/auth-bypass.json",
            content="{}"))
        self.assertEqual(rc, 0)
        names = [s["script"] for s in self.fx.launched()]
        self.assertEqual(names, ["Citation_Gate"])

    def test_no_active_inbox_canary_routes_gate(self):
        rc, _, _ = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="runs/run-x/inbox/gate-canary.probe", content="{}"))
        self.assertEqual(rc, 0)
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Citation_Gate"])

    def test_no_active_inbox_subdir_not_gate(self):
        rc, _, _ = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="runs/run-x/inbox/sub/AGENTS.md", content="x"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.fx.launched(), [])  # not a direct child of Inbox and not reserved → passes

    def test_no_active_control_write_routes_controller(self):
        rc, _, _ = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="control/start.json",
            content=json.dumps({"target": "demo"})))
        self.assertEqual(rc, 0)
        calls = self.fx.launched()
        self.assertEqual([s["script"] for s in calls], ["Run_Controller"])
        self.assertEqual(os.path.basename(calls[0]["argv"][0]), "start.json")

    def test_no_active_control_reserved_only(self):
        """Non-reserved names under control/ also route to Run_Controller (which raises REQUEST_INVALID)."""
        rc, _, _ = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="control/other.txt", content="x"))
        self.assertEqual(rc, 0)
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Run_Controller"])

    def test_no_active_runs_artifact_routes_guard(self):
        rc, _, _ = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="runs/run-x/gate-live.jsonl", content="{}"))
        self.assertEqual(rc, 0)
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Write_Guard"])

    def test_no_active_plain_write_passes(self):
        rc, _, _ = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="targets/demo/new.txt", content="hi"))
        self.assertEqual((rc, self.fx.launched()), (0, []))

    # -- active mode --

    def setUpActive(self):
        self.fx.write_run("run-1731690000123-a1b2c3", self.fx.active_state())

    def test_active_read_passes(self):
        self.setUpActive()
        rc, _, err = self.fx.run_hook("", self.fx.payload("grep_search", pattern="x"))
        self.assertEqual((rc, err, self.fx.launched()), (0, "", []))

    def test_active_unknown_tool_blocked_by_guard(self):
        self.setUpActive()
        rc, _, err = self.fx.run_hook("", self.fx.payload("execute_bash", command="ls"))
        self.assertEqual(rc, 0)  # the stub script exits 0 → passes, but Write_Guard was indeed invoked
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Write_Guard"])

    def test_active_invalid_payload_to_guard(self):
        self.setUpActive()
        rc, _, _ = self.fx.run_hook("", "{broken")
        self.assertEqual(rc, 0)
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Write_Guard"])

    def test_active_inbox_write_routes_gate(self):
        self.setUpActive()
        rc, _, _ = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="runs/run-1731690000123-a1b2c3/inbox/x-y.json", content="{}"))
        self.assertEqual(rc, 0)
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Citation_Gate"])

    def test_active_control_write_routes_controller(self):
        self.setUpActive()
        rc, _, _ = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="control/abort.json", content=json.dumps({"run_id": "r"})))
        self.assertEqual(rc, 0)
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Run_Controller"])

    def test_active_other_write_routes_guard(self):
        self.setUpActive()
        rc, _, _ = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="targets/demo/x.txt", content="hi"))
        self.assertEqual(rc, 0)
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Write_Guard"])

    def test_active_missing_path_pointer_routes_guard(self):
        self.setUpActive()
        rc, _, _ = self.fx.run_hook("", self.fx.payload("fs_write", content="no path"))
        self.assertEqual(rc, 0)
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Write_Guard"])

    def test_active_mixed_scope_routes_guard(self):
        self.setUpActive()
        rc, _, _ = self.fx.run_hook("", self.fx.payload(
            "fs_write", path="control/start.json", content="{}"))
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Run_Controller"])
        # a mixed case spanning the Inbox and ordinary paths: delete-type tools have no directory
        # → the unknown-tool case is already covered; fs_write has a single path so it cannot mix,
        # so here the multi-path form (directory expansion) is verified
        rc, _, _ = self.fx.run_hook("", json.dumps({
            "tool_name": "fs_write", "tool_input": {
                "path": "runs/run-1731690000123-a1b2c3/inbox/nested/a.json",
                "content": "{}"}}))
        self.assertEqual(rc, 0)
        names = [s["script"] for s in self.fx.launched()]
        self.assertEqual(names[-1], "Write_Guard")  # an Inbox subdirectory path is not a direct child → Guard


class TestSubprocessDiscipline(unittest.TestCase):
    def setUp(self):
        self.fx = LauncherFixture()
        self.fx.write_run("run-1731690000123-a1b2c3", self.fx.active_state())

    def tearDown(self):
        self.fx.cleanup()

    def test_timeout_becomes_blocking_signal(self):
        t0 = time.monotonic()
        rc, _, err = self.fx.run_hook("sleep", self.fx.payload(
            "fs_write", path="targets/demo/x.txt", content="x"), timeout=60)
        elapsed = time.monotonic() - t0
        self.assertEqual(rc, 2)
        self.assertTrue(err.startswith("TIMEOUT [Write_Guard]:"), err)
        self.assertLess(elapsed, 10)  # Guard deadline 5s + margin

    def test_exit2_without_reason_code_rewritten(self):
        rc, _, err = self.fx.run_hook("exit2_bare", self.fx.payload(
            "fs_write", path="targets/demo/x.txt", content="x"))
        self.assertEqual(rc, 2)
        self.assertTrue(err.startswith("GATE_FAILURE [Write_Guard]:"), err)
        self.assertNotIn("something went wrong", err)  # the script's stderr is not forwarded (R9.11)

    def test_exit2_with_reason_code_forwarded(self):
        rc, _, err = self.fx.run_hook("exit2_code", self.fx.payload(
            "fs_write", path="targets/demo/x.txt", content="x"))
        self.assertEqual(rc, 2)
        self.assertEqual(err, "TIMEOUT: x\n")

    def test_crash_becomes_gate_failure(self):
        rc, _, err = self.fx.run_hook("crash", self.fx.payload(
            "fs_write", path="targets/demo/x.txt", content="x"))
        self.assertEqual(rc, 2)
        self.assertTrue(err.startswith("GATE_FAILURE [Write_Guard]:"), err)


class TestCorruptState(unittest.TestCase):
    def setUp(self):
        self.fx = LauncherFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_corrupt_blocks_pre(self):
        self.fx.write_run("run-bad", "not json")
        rc, _, err = self.fx.run_hook("", self.fx.payload("read_file", path="x"))
        self.assertEqual(rc, 2)
        self.assertTrue(err.startswith("STATE_CORRUPT:"), err)
        self.assertIn("run-bad", err)

    def test_corrupt_blocks_post(self):
        self.fx.write_run("run-bad", "not json")
        rc, _, err = self.fx.run_post(self.fx.payload("read_file", path="x"))
        self.assertEqual(rc, 2)
        self.assertTrue(err.startswith("STATE_CORRUPT:"), err)


class TestPostRouting(unittest.TestCase):
    def setUp(self):
        self.fx = LauncherFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_no_active_post_never_launches(self):
        rc, _, _ = self.fx.run_post(self.fx.payload("read_file", path="targets/demo/a.py"))
        self.assertEqual((rc, self.fx.launched()), (0, []))

    def test_active_post_launches_recorder(self):
        self.fx.write_run("run-1731690000123-a1b2c3", self.fx.active_state())
        rc, _, _ = self.fx.run_post(self.fx.payload("read_file", path="targets/demo/a.py"))
        self.assertEqual(rc, 0)
        self.assertEqual([s["script"] for s in self.fx.launched()], ["Evidence_Recorder"])


class TestPerformanceBudget(unittest.TestCase):
    """R15.4 / A23 / T6: 1000 Run_Directories, unrelated call pre+post ≤ 1s."""

    def test_1000_runs_pre_post_within_1s(self):
        fx = LauncherFixture()
        try:
            state = json.dumps({"run_id": "run-1731690000123-a1b2c3", "target": "demo",
                                "status": "completed",
                                "started_at": "2026-01-01T00:00:00.000Z",
                                "finished_at": "2026-01-01T00:01:00.000Z"})
            for i in range(1000):
                fx.write_run("run-%013d-%06x" % (1700000000000 + i, i % 16 ** 6), state)
            payload = fx.payload("read_file", path="targets/demo/a.py")
            t0 = time.monotonic()
            rc1, _, _ = fx.run_hook("", payload, timeout=30)
            rc2, _, _ = fx.run_post(payload, timeout=30)
            elapsed = time.monotonic() - t0
            self.assertEqual((rc1, rc2), (0, 0))
            self.assertLess(elapsed, 1.0, "pre+post exceeded the 1s budget: %.3fs" % elapsed)
        finally:
            fx.cleanup()


if __name__ == "__main__":
    unittest.main()
