#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Component integration tests (tasks.md 3.7, 4.5, 5.12, 6.3, 7.x).

Drives the five real scripts directly with synthetic payloads (not via Kiro):
  Run_Controller (Start/Abort/Finalize concurrency and rejection matrix)
  Evidence_Recorder (filtering/merging/normalization/concurrent appends/corruption detection)
  Citation_Gate (Canary path, defect matrix, acceptance protocol, concurrency semantics, truncation)
  Write_Guard (path classification/protection matrix/UNDETERMINABLE)
  Report_Renderer (recheck invalidation, Summary validation, canonical consistency)
Run: python3 -m unittest tests.test_components -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, ".kiro", "scripts")
sys.path.insert(0, SCRIPTS)

import ghostlib as g  # noqa: E402

DEMO_FILES = {
    "a.py": "\n".join("line %d content" % i for i in range(1, 61)) + "\n",
    "src/b.txt": "alpha\nbeta\ngamma\n",
    "README.md": "# demo\n",
}


class Workspace:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ghost-ws-")
        os.makedirs(os.path.join(self.root, "runs"))
        os.makedirs(os.path.join(self.root, "control"))
        os.makedirs(os.path.join(self.root, "targets", "demo", "src"))
        scripts = os.path.join(self.root, ".kiro", "scripts")
        os.makedirs(scripts)
        for name in os.listdir(SCRIPTS):
            if name.endswith(".py") or name.endswith(".json"):
                shutil.copy(os.path.join(SCRIPTS, name), scripts)
        for rel, content in DEMO_FILES.items():
            path = os.path.join(self.root, "targets", "demo", rel)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    def script(self, name):
        return os.path.join(self.root, ".kiro", "scripts", name)

    def write_payload(self, payload):
        import tempfile as tf
        fd, tmp = tf.mkstemp(prefix="ghost-test-payload-")
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return tmp

    def _run(self, script, argv, timeout=60):
        proc = subprocess.run([sys.executable, self.script(script)] + argv,
                              cwd=self.root, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
        return proc.returncode, proc.stdout.decode(), proc.stderr.decode()

    # -- Run_Controller --
    def controller(self, request_name, body):
        request_file = os.path.join(self.root, "control", request_name)
        payload = self.write_payload({
            "tool_name": "fs_write",
            "tool_input": {"path": request_file, "content": body},
        })
        try:
            return self._run("run_controller.py", [request_file, payload])
        finally:
            os.unlink(payload)

    def start(self, target="demo", focus=None):
        body = {"target": target}
        if focus is not None:
            body["focus"] = focus
        return self.controller("start.json", json.dumps(body, ensure_ascii=False))

    def abort(self, run_id):
        return self.controller("abort.json", json.dumps({"run_id": run_id}))

    def finalize(self, run_id, completion=None, summary=None):
        body = {"run_id": run_id, "completion": completion or {}}
        if summary is not None:
            body["summary_statements"] = summary
        return self.controller("finalize.json", json.dumps(body, ensure_ascii=False))

    # -- Citation_Gate --
    def gate(self, run_id, file_name, content, session="sess-1", timeout=30):
        payload = {
            "tool_name": "fs_write", "session_id": session,
            "tool_input": {"path": "runs/%s/inbox/%s" % (run_id, file_name),
                           "content": content},
        }
        tmp = self.write_payload(payload)
        try:
            return self._run("citation_gate.py", [tmp], timeout=timeout)
        finally:
            os.unlink(tmp)

    def canary(self, run_id):
        return self.gate(run_id, g.CANARY_FILE_NAME,
                         g.canary_submission_text(run_id))

    # -- Evidence_Recorder --
    def record(self, tool_response, read_path="targets/demo/a.py", tool="read_file",
               session="sess-1", timeout=30):
        payload = {
            "tool_name": tool, "session_id": session,
            "tool_input": {"path": read_path},
            "tool_response": tool_response,
        }
        tmp = self.write_payload(payload)
        try:
            return self._run("evidence_recorder.py", [tmp], timeout=timeout)
        finally:
            os.unlink(tmp)

    def read_file_response(self, rel="targets/demo/a.py"):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return {"path": rel, "content": f.read()}

    # -- Write_Guard --
    def guard(self, tool_input, tool="fs_write", timeout=30):
        payload = {"tool_name": tool, "session_id": "sess-1",
                   "tool_input": tool_input}
        tmp = self.write_payload(payload)
        try:
            return self._run("write_guard.py", [tmp], timeout=timeout)
        finally:
            os.unlink(tmp)

    # -- run directory helpers --
    def runs_dir(self):
        return os.path.join(self.root, "runs")

    def active_run(self):
        scan = g.scan_runs(self.runs_dir())
        return scan["active_run_id"], scan["active_dir"]

    def read(self, rel):
        path = os.path.join(self.root, rel)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def submission_text(run_id, agent="Cartographer", category="architecture",
                    severity="info", title="t", description="d", citations=None):
    return json.dumps({
        "run_id": run_id, "agent": agent, "title": title, "category": category,
        "severity": severity, "description": description,
        "citations": citations or [],
    }, ensure_ascii=False)


def citation_for(lines, start, end, ts):
    return {"location": "a.py:%d-%d" % (start, end),
            "evidence_timestamp": ts,
            "excerpt": "\n".join(lines[start - 1:end])}


def get_evidence_entries(run_dir):
    entries, _ = g.parse_evidence_log(g.read_text(os.path.join(run_dir, "evidence.jsonl")))
    return entries or []


class TestStart(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.cleanup()

    def test_happy_start(self):
        rc, _, err = self.ws.start(focus="auth module")
        self.assertEqual((rc, err), (0, ""), err)
        run_id, run_dir = self.ws.active_run()
        self.assertIsNotNone(run_id)
        self.assertRegex(run_id, g.RUN_ID_RE.pattern.strip("^$").replace("^", "").replace("$", ""))
        for name in ("state.json", "evidence.jsonl", "findings.jsonl",
                     "rejections.jsonl"):
            self.assertTrue(os.path.exists(os.path.join(run_dir, name)), name)
        # gate-live.jsonl is created on demand (R2.3); absent initially
        self.assertFalse(os.path.exists(os.path.join(run_dir, "gate-live.jsonl")))
        self.assertTrue(os.path.isdir(os.path.join(run_dir, "inbox")))
        state = g.parse_state(g.read_text(os.path.join(run_dir, "state.json")))
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["target"], "demo")
        self.assertEqual(state["focus"], "auth module")
        self.assertNotIn("finished_at", state)

    def test_ten_parallel_starts_one_active(self):
        """R1.7: multiple Starts processed concurrently → exactly one active."""
        procs = []
        for _ in range(10):
            payload = self.ws.write_payload({
                "tool_name": "fs_write",
                "tool_input": {"path": os.path.join(self.ws.root, "control", "start.json"),
                               "content": json.dumps({"target": "demo"})},
            })
            procs.append(subprocess.Popen(
                [sys.executable, self.ws.script("run_controller.py"),
                 os.path.join(self.ws.root, "control", "start.json"), payload],
                cwd=self.ws.root, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        errs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            errs.append((p.returncode, err.decode()))
        os.unlink(payload)
        active = [e for e in errs if e[0] == 0]
        self.assertEqual(len(active), 1)
        for rc, err in errs:
            if rc != 0:
                self.assertIn("RUN_ALREADY_ACTIVE", err)
        scan = g.scan_runs(self.ws.runs_dir())
        self.assertEqual(scan["active_count"], 1)
        leftovers = [n for n in os.listdir(self.ws.runs_dir()) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_target_invalid_matrix(self):
        cases = [
            ("nonexistent", "TARGET_INVALID"),
            ("..", "TARGET_INVALID"),
            ("a/b", "TARGET_INVALID"),
            ("", "TARGET_INVALID"),
            (None, "TARGET_INVALID"),  # missing target field
        ]
        for target, code in cases:
            body = {} if target is None else {"target": target}
            rc, _, err = self.ws.controller("start.json", json.dumps(body))
            self.assertEqual(rc, 2, repr(target))
            self.assertIn(code, err, repr(target))

    def test_symlink_target_rejected(self):
        """A22: an entry of Targets_Directory that is itself a symlink → TARGET_INVALID."""
        os.symlink(os.path.join(self.ws.root, "targets", "demo"),
                   os.path.join(self.ws.root, "targets", "link"))
        rc, _, err = self.ws.start(target="link")
        self.assertEqual(rc, 2)
        self.assertIn("TARGET_INVALID", err)

    def test_target_empty(self):
        os.makedirs(os.path.join(self.ws.root, "targets", "empty", "subdir"))
        rc, _, err = self.ws.start(target="empty")
        self.assertEqual(rc, 2)
        self.assertIn("TARGET_EMPTY", err)

    def test_auto_loaded_context(self):
        """R1.5: case-insensitive AGENTS.md + .kiro directories at any depth (incl. symlink names).

        Note: the case-variant symlinks live in different directories to avoid
        same-name collisions on case-insensitive filesystems.
        """
        t = os.path.join(self.ws.root, "targets", "messy")
        os.makedirs(os.path.join(t, "deep", "inner"))
        os.makedirs(os.path.join(t, "alt"))
        with open(os.path.join(t, "agents.md"), "w") as f:
            f.write("x")
        with open(os.path.join(t, "deep", "AGENTS.MD"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(t, "deep", "inner", ".kiro"))
        # symlink name is a case variant (points inside the Target → not an
        # escape, but the name still counts)
        os.symlink(os.path.join(t, "agents.md"), os.path.join(t, "alt", "Agents.md"))
        with open(os.path.join(t, "normal.py"), "w") as f:
            f.write("pass\n")
        rc, _, err = self.ws.start(target="messy")
        self.assertEqual(rc, 2)
        self.assertIn("AUTO_LOADED_CONTEXT_PRESENT", err)
        self.assertIn("count=4", err)  # agents.md, deep/AGENTS.MD, .kiro dir, alt/Agents.md
        self.assertNotIn("normal.py", err)
        self.assertNotIn("SYMLINK_ESCAPE", err)

    def test_symlink_escape(self):
        """R1.6: links pointing outside / cyclic / dangling external; internal links don't count."""
        t = os.path.join(self.ws.root, "targets", "esc")
        os.makedirs(os.path.join(t, "inner"))
        with open(os.path.join(t, "inner", "f.py"), "w") as f:
            f.write("pass\n")
        os.symlink("/etc/hosts", os.path.join(t, "out.py"))
        os.symlink("loop2.py", os.path.join(t, "loop1.py"))
        os.symlink("loop1.py", os.path.join(t, "loop2.py"))
        os.symlink("/nonexistent-xyz/q", os.path.join(t, "dangling.py"))
        os.symlink("f.py", os.path.join(t, "inner", "ok.py"))  # internal: not an escape
        rc, _, err = self.ws.start(target="esc")
        self.assertEqual(rc, 2)
        self.assertIn("SYMLINK_ESCAPE", err)
        self.assertIn("count=4", err)  # out/loop1/loop2/dangling
        self.assertNotIn("ok.py", err)

    def test_accumulated_codes(self):
        """R1.9: one Blocking_Signal lists every Reason_Code."""
        with open(os.path.join(self.ws.runs_dir(), "run-bad"), "w") as f:
            pass  # not a run directory (no state.json) → not a Run_Directory (non-directory entries ignored)
        os.makedirs(os.path.join(self.ws.root, "targets", "messy2", ".kiro"))
        rc, _, err = self.ws.start(target="ghost")  # nonexistent → TARGET_INVALID
        self.assertEqual(rc, 2)
        self.assertIn("TARGET_INVALID", err)
        # create a corrupt state + an active run, then start again
        self.ws.start()
        run_id, run_dir = self.ws.active_run()
        with open(os.path.join(run_dir, "state.json"), "w") as f:
            f.write("{broken")
        self.ws.abort(run_id)  # will be rejected because of the corrupt state
        rc, _, err = self.ws.start()
        self.assertEqual(rc, 2)
        self.assertIn("STATE_CORRUPT", err)


class TestAbort(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()

    def tearDown(self):
        self.ws.cleanup()

    def test_abort_flow(self):
        self.ws.start()
        run_id, run_dir = self.ws.active_run()
        rc, _, err = self.ws.abort(run_id)
        self.assertEqual((rc, err), (0, ""), err)
        state = g.parse_state(g.read_text(os.path.join(run_dir, "state.json")))
        self.assertEqual(state["status"], "aborted")
        self.assertIsNotNone(g.parse_ts(state["finished_at"]))

    def test_abort_not_active(self):
        rc, _, err = self.ws.abort("run-1731690000123-a1b2c3")
        self.assertEqual(rc, 2)
        self.assertIn("RUN_NOT_ACTIVE", err)
        self.ws.start()
        run_id, run_dir = self.ws.active_run()
        self.ws.abort(run_id)
        rc, _, err = self.ws.abort(run_id)  # already aborted
        self.assertEqual(rc, 2)
        self.assertIn("RUN_NOT_ACTIVE", err)

    def test_abort_bad_id(self):
        for bad in ("", "not-an-id", "../evil"):
            rc, _, err = self.ws.abort(bad)
            self.assertEqual(rc, 2)
            self.assertIn("RUN_NOT_ACTIVE", err)

    def test_finalize_then_abort_rejected(self):
        """R14.8: Finalize/Abort are serialized; after completed, Abort → RUN_NOT_ACTIVE."""
        self.ws.start()
        run_id, run_dir = self.ws.active_run()
        self.ws.canary(run_id)
        self.ws.record(self.ws.read_file_response())
        sub = submission_text(run_id, citations=[])
        # build gate-live + evidence + one finding directly (through the real gate)
        lines = g.split_lines(DEMO_FILES["a.py"].encode("utf-8"))
        ent = get_evidence_entries(run_dir)[0]
        cit = {"location": "a.py:1-2", "evidence_timestamp": ent["ts"],
               "excerpt": "\n".join(lines[0:2])}
        rc, _, err = self.ws.gate(run_id, "arch-map.json",
                                  submission_text(run_id, citations=[cit]))
        self.assertEqual(rc, 0, err)
        rc, _, err = self.ws.finalize(run_id, {"Cartographer": "completed"})
        self.assertEqual((rc, err), (0, ""), err)
        rc, _, err = self.ws.abort(run_id)
        self.assertEqual(rc, 2)
        self.assertIn("RUN_NOT_ACTIVE", err)


class TestFinalize(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        rc, _, err = self.ws.start()
        assert rc == 0, err
        self.run_id, self.run_dir = self.ws.active_run()
        assert self.ws.canary(self.run_id)[0] == 2  # Canary rejected + gate-live written

    def tearDown(self):
        self.ws.cleanup()

    def seed_evidence_and_finding(self, filename="arch-map.json", title="t",
                                  severity="info"):
        self.ws.record(self.ws.read_file_response())
        entries = get_evidence_entries(self.run_dir)
        self.assertGreater(len(entries), 0)
        lines = g.split_lines(DEMO_FILES["a.py"].encode("utf-8"))
        cit = citation_for(lines, 1, 2, entries[0]["ts"])
        rc, _, err = self.ws.gate(self.run_id, filename,
                                  submission_text(self.run_id, title=title,
                                                  severity=severity,
                                                  citations=[cit]))
        self.assertEqual(rc, 0, err)

    def test_gate_not_verified(self):
        """R2.5: no Gate_Live_Record → GATE_NOT_VERIFIED, status unchanged."""
        os.unlink(os.path.join(self.run_dir, "gate-live.jsonl"))
        rc, _, err = self.ws.finalize(self.run_id, {})
        self.assertEqual(rc, 2)
        self.assertIn("GATE_NOT_VERIFIED", err)
        state = g.parse_state(g.read_text(os.path.join(self.run_dir, "state.json")))
        self.assertEqual(state["status"], "active")
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "report.md")))

    def test_recorder_not_verified(self):
        """R2.9: Evidence_Log has no entries → RECORDER_NOT_VERIFIED."""
        rc, _, err = self.ws.finalize(self.run_id, {})
        self.assertEqual(rc, 2)
        self.assertIn("RECORDER_NOT_VERIFIED", err)
        state = g.parse_state(g.read_text(os.path.join(self.run_dir, "state.json")))
        self.assertEqual(state["status"], "active")

    def test_finalize_success(self):
        self.seed_evidence_and_finding()
        rc, _, err = self.ws.finalize(self.run_id, {
            "Cartographer": "completed", "Security_Auditor": "failed"})
        self.assertEqual((rc, err), (0, ""), err)
        state = g.parse_state(g.read_text(os.path.join(self.run_dir, "state.json")))
        self.assertEqual(state["status"], "completed")
        report_md = self.ws.read(os.path.join("runs", os.path.basename(self.run_dir), "report.md"))
        report = json.loads(self.ws.read(os.path.join("runs", os.path.basename(self.run_dir), "report.json")))
        self.assertIn(self.run_id, report_md)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["run"]["agents"]["Cartographer"], "completed")
        self.assertEqual(report["run"]["agents"]["Security_Auditor"], "failed")
        self.assertEqual(report["run"]["agents"]["Dependency_Auditor"], "not_dispatched")
        self.assertEqual(report["run"]["agents"]["Quality_Auditor"], "not_dispatched")
        # R13.13: the canonical rebuilt from report.json is byte-identical to the Ledger's
        ledger_line = [l for l in self.ws.read("x").split("\n") if l] \
            if False else None
        ld = [l for l in g.read_text(os.path.join(self.run_dir, "findings.jsonl")).split("\n") if l]
        rec = json.loads(ld[0])
        rebuilt = {
            "run_id": self.run_id,
            "agent": report["findings"][0]["agent"],
            "title": report["findings"][0]["title"],
            "category": report["findings"][0]["category"],
            "severity": report["findings"][0]["severity"],
            "description": report["findings"][0]["description"],
            "citations": [
                {"location": c["location"], "evidence_timestamp": c["evidence_timestamp"],
                 "excerpt": c["excerpt"]} for c in report["findings"][0]["citations"]],
        }
        self.assertEqual(g.canonical_form(rebuilt),
                         g.reserialize_canonical_obj(rec["canonical"]))

    def test_summary_unsupported(self):
        """R13.11: various invalid Summary_Statements carry 1-based ordinals."""
        self.seed_evidence_and_finding()
        ledger_text = g.read_text(os.path.join(self.run_dir, "findings.jsonl"))
        rec = json.loads([l for l in ledger_text.split("\n") if l][0])
        fid = rec["finding_id"]
        long_stmt = "see " + fid + " " + "x" * 501
        cases = [
            (["a statement citing no finding id"], ["1"]),
            ([long_stmt], ["1"]),
            (["see the conclusion of F-999999"], ["1"]),
            (["see %s; see also src/b.txt:1" % fid], ["1"]),  # not covered by the Finding's Citations
            ([("see %s." % fid)] * 21, ["21"]),
            (["see %s" % fid, "no citation"], ["2"]),
        ]
        for summary, want in cases:
            rc, _, err = self.ws.finalize(self.run_id, {}, summary=summary)
            self.assertEqual(rc, 2, summary[:1])
            self.assertIn("SUMMARY_UNSUPPORTED", err)
            for n in want:
                self.assertIn("SUMMARY_UNSUPPORTED %s:" % n, err, summary[:1])
            state = g.parse_state(g.read_text(os.path.join(self.run_dir, "state.json")))
            self.assertEqual(state["status"], "active")
        # a valid statement passes (new finding, new file name, to avoid the duplicate rule)
        self.seed_evidence_and_finding(filename="arch-map-2.json", title="t2")
        ledger2 = g.read_text(os.path.join(self.run_dir, "findings.jsonl"))
        fid2 = json.loads([l for l in ledger2.split("\n") if l][-1])["finding_id"]
        rc, _, err = self.ws.finalize(
            self.run_id, {},
            summary=["See %s for the architecture conclusion at a.py:1-2." % fid2])
        self.assertEqual((rc, err), (0, ""), err)

    def test_finalize_structural_internal_error(self):
        """O1: invalid completion values → INTERNAL_ERROR."""
        rc, _, err = self.ws.finalize(self.run_id, {"Cartographer": "ok"})
        self.assertEqual(rc, 2)
        self.assertIn("INTERNAL_ERROR", err)
        rc, _, err = self.ws.controller(
            "finalize.json", json.dumps({"run_id": self.run_id, "completion": {},
                                         "unknown": 1}))
        self.assertEqual(rc, 2)
        self.assertIn("INTERNAL_ERROR", err)

    def test_finalize_not_active(self):
        rc, _, err = self.ws.finalize("run-1731690000123-a1b2c3", {})
        self.assertEqual(rc, 2)
        self.assertIn("RUN_NOT_ACTIVE", err)


class TestGate(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        rc, _, err = self.ws.start()
        assert rc == 0, err
        self.run_id, self.run_dir = self.ws.active_run()
        assert self.ws.canary(self.run_id)[0] == 2
        self.ws.record(self.ws.read_file_response())  # full a.py → lines 1-60 evidence
        self.ws.record({"path": "targets/demo/a.py",
                        "content": "line 1 content"})  # narrow evidence 1-1
        self.entries = get_evidence_entries(self.run_dir)
        self.lines = g.split_lines(DEMO_FILES["a.py"].encode("utf-8"))

    def tearDown(self):
        self.ws.cleanup()

    def ts(self):
        return self.entries[0]["ts"]

    def ts_narrow(self):
        return self.entries[-1]["ts"]

    def test_valid_submission_accepted(self):
        """R10.1: all checks pass → ledger appends F-000001, exit 0 silently."""
        cit = citation_for(self.lines, 1, 2, self.ts())
        rc, out, err = self.ws.gate(self.run_id, "arch-map.json",
                                    submission_text(self.run_id, citations=[cit]))
        self.assertEqual((rc, out.strip(), err), (0, "", ""), err)
        ld = [l for l in g.read_text(os.path.join(self.run_dir, "findings.jsonl")).split("\n") if l]
        self.assertEqual(len(ld), 1)
        rec = json.loads(ld[0])
        self.assertEqual(rec["finding_id"], "F-000001")
        self.assertEqual(rec["submission_file"], "arch-map.json")
        self.assertEqual(rec["session"], "sess-1")
        self.assertEqual(list(rec.keys()),
                         ["finding_id", "accepted_at", "submission_file", "session", "canonical"])

    def test_canary_rejected_and_gate_live(self):
        """R2.2/R2.3: Canary → CITATION_MISSING + Gate_Live_Record (one record per well-formed Canary)."""
        rc, _, err = self.ws.canary(self.run_id)
        self.assertEqual(rc, 2)
        self.assertIn("CITATION_MISSING", err)
        self.assertNotIn("SCHEMA_INVALID", err)
        gt = g.read_text(os.path.join(self.run_dir, "gate-live.jsonl"))
        recs = [json.loads(l) for l in gt.split("\n") if l]
        self.assertEqual(len(recs), 2)  # one from setUp + this one
        self.assertEqual(recs[0]["run_id"], self.run_id)
        self.assertEqual(recs[0]["sha256"],
                         g.bytes_hash(g.canary_submission_text(self.run_id)))
        self.assertEqual(list(recs[0].keys()), ["run_id", "at", "sha256"])

    def test_canary_with_citations_invalid_name(self):
        """R10.9: Canary name with non-empty citations → FILE_NAME_INVALID."""
        content = submission_text(self.run_id, citations=[citation_for(self.lines, 1, 2, self.ts())])
        rc, _, err = self.ws.gate(self.run_id, g.CANARY_FILE_NAME, content)
        self.assertEqual(rc, 2)
        self.assertIn("FILE_NAME_INVALID", err)
        gt = g.read_text(os.path.join(self.run_dir, "gate-live.jsonl"))
        self.assertEqual(len([l for l in gt.split("\n") if l]), 1)  # no new record

    def test_duplicate_submission(self):
        """R10.3: duplicate → DUPLICATE_SUBMISSION + the existing Finding_ID."""
        cit = citation_for(self.lines, 1, 2, self.ts())
        content = submission_text(self.run_id, citations=[cit])
        self.assertEqual(self.ws.gate(self.run_id, "one.json", content)[0], 0)
        rc, _, err = self.ws.gate(self.run_id, "two.json", content)
        self.assertEqual(rc, 2)
        self.assertIn("DUPLICATE_SUBMISSION F-000001", err)
        ld = [l for l in g.read_text(os.path.join(self.run_dir, "findings.jsonl")).split("\n") if l]
        self.assertEqual(len(ld), 1)

    def test_filename_immutable(self):
        """R10.4: a new file name already recorded in the Ledger → INBOX_IMMUTABLE."""
        cit = citation_for(self.lines, 1, 2, self.ts())
        rc, _o, _e = self.ws.gate(self.run_id, "arch-map.json",
                                  submission_text(self.run_id, citations=[cit]))
        self.assertEqual(rc, 0)
        other = submission_text(self.run_id, title="different",
                                citations=[citation_for(self.lines, 3, 4, self.ts())])
        rc, _, err = self.ws.gate(self.run_id, "arch-map.json", other)
        self.assertEqual(rc, 2)
        self.assertIn("INBOX_IMMUTABLE", err)
        # modify an existing file
        existing = os.path.join(self.run_dir, "inbox", "draft.json")
        with open(existing, "w") as f:
            f.write("{}")
        rc, _, err = self.ws.gate(self.run_id, "draft.json",
                                  submission_text(self.run_id, citations=[citation_for(self.lines, 1, 1, self.ts())]))
        self.assertEqual(rc, 2)
        self.assertIn("INBOX_IMMUTABLE", err)
        os.unlink(existing)

    def test_run_mismatch(self):
        cit = citation_for(self.lines, 1, 2, self.ts())
        content = submission_text("run-1731690000123-a1b2c3", citations=[cit])
        rc, _, err = self.ws.gate(self.run_id, "x.json", content)
        self.assertEqual(rc, 2)
        self.assertIn("RUN_MISMATCH", err)

    def test_citation_defect_matrix(self):
        """R7.6–R7.18: each defect class yields its Reason_Code (stop at first failure)."""
        outside_file = os.path.join(self.ws.root, "outside.txt")
        with open(outside_file, "w", encoding="utf-8") as f:
            f.write("secret\n")
        esc_link = os.path.join(self.ws.root, "targets", "demo", "esc.txt")
        os.symlink(outside_file, esc_link)
        bad_bin = os.path.join(self.ws.root, "targets", "demo", "bad.bin")
        with open(bad_bin, "wb") as f:
            f.write(b"\xff\xfe\x00\x80")
        try:
            cases = [
                ("LOCATION_MALFORMED", [{"location": "a.py:10-10",
                                         "evidence_timestamp": self.ts(),
                                         "excerpt": "x"}]),
                ("SPAN_TOO_LARGE", [citation_for(self.lines, 1, 52, self.ts())]),
                ("TIMESTAMP_MALFORMED", [{"location": "a.py:1-2",
                                          "evidence_timestamp": "2026-13-01T00:00:00.000Z",
                                          "excerpt": "line 1 content"}]),
                ("PATH_OUTSIDE_TARGET", [{"location": "esc.txt:1",
                                          "evidence_timestamp": self.ts(),
                                          "excerpt": "secret"}]),
                ("FILE_NOT_FOUND", [{"location": "ghost.py:1",
                                     "evidence_timestamp": self.ts(),
                                     "excerpt": "x"}]),
                ("FILE_UNREADABLE", [{"location": "bad.bin:1",
                                      "evidence_timestamp": self.ts(),
                                      "excerpt": "x"}]),
                ("LINE_OUT_OF_RANGE", [citation_for(self.lines, 59, 61, self.ts())]),  # span=3 valid, line 61 > 60 lines
                ("EVIDENCE_NOT_FOUND", [{"location": "a.py:1-2",
                                         "evidence_timestamp": "2020-01-01T00:00:00.000Z",
                                         "excerpt": "line 1 content\nline 2 content"}]),
                ("EVIDENCE_MISMATCH", [{"location": "a.py:2-3",
                                        "evidence_timestamp": self.ts_narrow(),
                                        "excerpt": "line 2 content"}]),
                ("EXCERPT_MISMATCH", [{"location": "a.py:1-2",
                                       "evidence_timestamp": self.ts(),
                                       "excerpt": "totally different text"}]),
            ]
            for want_code, cits in cases:
                content = submission_text(self.run_id, citations=cits)
                rc, _, err = self.ws.gate(self.run_id, "defect.json", content)
                self.assertEqual(rc, 2, want_code)
                self.assertIn(want_code + " citations[1]:", err, want_code)
        finally:
            if os.path.lexists(esc_link):
                os.unlink(esc_link)
            if os.path.exists(bad_bin):
                os.unlink(bad_bin)

    def test_content_changed(self):
        """R7.14: cited line range disagrees with the hash recorded at read time → CONTENT_CHANGED."""
        cit = citation_for(self.lines, 1, 2, self.ts())
        path = os.path.join(self.ws.root, "targets", "demo", "a.py")
        original = DEMO_FILES["a.py"]
        with open(path, "w", encoding="utf-8") as f:
            f.write("CHANGED\n" + original.split("\n", 1)[1])
        content = submission_text(self.run_id, citations=[cit])
        rc, _, err = self.ws.gate(self.run_id, "defect.json", content)
        self.assertEqual(rc, 2)
        self.assertIn("CONTENT_CHANGED", err)
        with open(path, "w", encoding="utf-8") as f:
            f.write(original)

    def test_uncited_location(self):
        """R7.16: references in title/description not covered by any Citation."""
        cit = citation_for(self.lines, 1, 2, self.ts())
        content = submission_text(self.run_id, title="the issue is at src/b.txt:1",
                                  citations=[cit])
        rc, _, err = self.ws.gate(self.run_id, "x.json", content)
        self.assertEqual(rc, 2)
        self.assertIn("UNCITED_LOCATION src/b.txt:1", err)
        # covered → accepted: add a citation for src/b.txt (record b.txt evidence first)
        self.ws.record(self.ws.read_file_response("targets/demo/src/b.txt"),
                       read_path="targets/demo/src/b.txt")
        entries = get_evidence_entries(self.run_dir)
        ts2 = entries[-1]["ts"]
        cit2 = {"location": "src/b.txt:1", "evidence_timestamp": ts2,
                "excerpt": "alpha"}
        content = submission_text(self.run_id, title="the issue is at src/b.txt:1",
                                  citations=[cit, cit2])
        rc, _, err = self.ws.gate(self.run_id, "x2.json", content)
        self.assertEqual((rc, err), (0, ""), err)

    def test_rejection_message_format_and_record(self):
        """R9.2–R9.5: line format, agent, sha256 recorded."""
        content = submission_text(self.run_id, agent="Security_Auditor", citations=[])
        rc, _, err = self.ws.gate(self.run_id, "sec-x.json", content)
        self.assertEqual(rc, 2)
        lines = err.strip().split("\n")
        self.assertEqual(lines[0], g.rejection_line("CITATION_MISSING", "citations"))
        rj = g.read_text(os.path.join(self.run_dir, "rejections.jsonl"))
        recs = [json.loads(l) for l in rj.split("\n") if l]
        self.assertEqual(len(recs), 2)  # Canary + this one
        rec = recs[-1]
        self.assertEqual(rec["reason_codes"], ["CITATION_MISSING"])
        self.assertEqual(rec["agent"], "Security_Auditor")
        self.assertEqual(rec["sha256"], g.bytes_hash(content))

    def test_truncation_at_50(self):
        """R9.2: >50 items truncated + remaining-count line."""
        desc = " ".join("see a.py:%d" % i for i in range(3, 65))
        cit = citation_for(self.lines, 1, 2, self.ts())
        content = submission_text(self.run_id, description=desc, citations=[cit])
        rc, _, err = self.ws.gate(self.run_id, "many.json", content)
        self.assertEqual(rc, 2)
        lines = err.strip().split("\n")
        self.assertEqual(len(lines), 51)
        self.assertEqual(lines[50], "… and 12 more not listed")
        self.assertTrue(all(l.startswith("UNCITED_LOCATION") for l in lines[:50]))

    def test_schema_short_circuit(self):
        """R7.19: Schema failures → only Schema-class + file-name-class codes output."""
        content = submission_text(self.run_id, citations=[citation_for(self.lines, 1, 2, self.ts())])
        obj = json.loads(content)
        obj["bad_severity"] = "x"   # undefined field
        obj["severity"] = "nope"    # invalid value
        obj["citations"][0]["evidence_timestamp"] = "bad-ts"  # still a valid Schema type (string)
        rc, _, err = self.ws.gate(self.run_id, "x.json", json.dumps(obj))
        self.assertEqual(rc, 2)
        self.assertIn("SCHEMA_INVALID", err)
        self.assertNotIn("TIMESTAMP_MALFORMED", err)
        self.assertNotIn("RUN_MISMATCH", err)

    def test_after_completion_rejected_run_not_active(self):
        """7#5/R14.3: after completed, a submission → RUN_NOT_ACTIVE only; 9#10 writes no Rejection_Log."""
        self.ws.record(self.ws.read_file_response("targets/demo/src/b.txt"),
                       read_path="targets/demo/src/b.txt")
        entries = get_evidence_entries(self.run_dir)
        cit = {"location": "src/b.txt:1-2", "evidence_timestamp": entries[-1]["ts"],
               "excerpt": "alpha\nbeta"}
        self.assertEqual(self.ws.gate(self.run_id, "one.json",
                                      submission_text(self.run_id, citations=[cit]))[0], 0)
        rc, _, err = self.ws.finalize(self.run_id, {"Cartographer": "completed"})
        self.assertEqual(rc, 0, err)
        content = submission_text(self.run_id, citations=[citation_for(self.lines, 5, 6, entries[0]["ts"])])
        rc, _, err = self.ws.gate(self.run_id, "late.json", content)
        self.assertEqual(rc, 2)
        self.assertEqual([l.split(":")[0] for l in err.strip().split("\n")], ["RUN_NOT_ACTIVE"])
        rj = g.read_text(os.path.join(self.run_dir, "rejections.jsonl")) or ""
        recs = [l for l in rj.split("\n") if l]
        self.assertEqual(len(recs), 1)  # only the Canary record from setUp (9#10 does not append)

    def test_concurrent_accepts_unique_ids(self):
        """R10.5/R10.8: parallel submissions → unique IDs; identical content accepted at most once."""
        # evidence for b.txt and README.md
        self.ws.record(self.ws.read_file_response("targets/demo/src/b.txt"),
                       read_path="targets/demo/src/b.txt")
        self.ws.record(self.ws.read_file_response("targets/demo/README.md"),
                       read_path="targets/demo/README.md")
        entries = get_evidence_entries(self.run_dir)
        ts_map = {e["path"]: e["ts"] for e in entries}
        cits_a = [{"location": "src/b.txt:1-2", "evidence_timestamp": ts_map["src/b.txt"],
                   "excerpt": "alpha\nbeta"}]
        cits_b = [{"location": "README.md:1", "evidence_timestamp": ts_map["README.md"],
                   "excerpt": "# demo"}]

        def launch(name, content):
            payload = self.ws.write_payload({
                "tool_name": "fs_write", "session_id": "s",
                "tool_input": {"path": "runs/%s/inbox/%s" % (self.run_id, name),
                               "content": content}})
            return subprocess.Popen(
                [sys.executable, self.ws.script("citation_gate.py"), payload],
                cwd=self.ws.root, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # two different Submissions in parallel → both accepted, distinct IDs
        p1 = launch("par-a.json", submission_text(self.run_id, citations=cits_a))
        p2 = launch("par-b.json", submission_text(self.run_id, citations=cits_b))
        rcs = [p1.wait(timeout=30), p2.wait(timeout=30)]
        self.assertEqual(sorted(rcs), [0, 0])
        ld = [l for l in g.read_text(os.path.join(self.run_dir, "findings.jsonl")).split("\n") if l]
        ids = sorted(json.loads(l)["finding_id"] for l in ld)
        self.assertEqual(ids, ["F-000001", "F-000002"])

        # identical content 3-way parallel → exactly one accepted
        dup = submission_text(self.run_id, title="dup", citations=cits_a)
        procs = [launch("dup-%d.json" % i, dup) for i in range(3)]
        results = []
        for p in procs:
            _, err = p.communicate(timeout=30)
            results.append((p.returncode, err.decode()))
        accepted = [r for r in results if r[0] == 0]
        self.assertEqual(len(accepted), 1)
        dup_rejected = [r for r in results if r[0] == 2]
        self.assertTrue(all("DUPLICATE_SUBMISSION" in r[1] for r in dup_rejected))
        ld = [l for l in g.read_text(os.path.join(self.run_dir, "findings.jsonl")).split("\n") if l]
        self.assertEqual(len(ld), 3)


class TestRecorder(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        rc, _, err = self.ws.start()
        assert rc == 0, err
        self.run_id, self.run_dir = self.ws.active_run()

    def tearDown(self):
        self.ws.cleanup()

    def test_filter_merge_ranges(self):
        """R4.1/R4.2: only lines equal after normalization count; maximal contiguous ranges merge."""
        content = "\n".join([
            "line 1 content",     # matches
            "line 2  content",    # matches after normalization
            "MUTATED line 3",     # no match
            "line 4 content",     # matches
            "",                   # empty line (that file line is "line 5 content") → no match
            "line 6 content",     # matches
        ])
        rc, _, err = self.ws.record({"path": "targets/demo/a.py", "content": content})
        self.assertEqual((rc, err), (0, ""), err)
        entries = get_evidence_entries(self.run_dir)
        ranges = [(e["start"], e["end"]) for e in entries]
        self.assertEqual(ranges, [(1, 2), (4, 4), (6, 6)])
        # path normalization + correct hash
        self.assertEqual(entries[0]["path"], "a.py")
        self.assertEqual(entries[0]["sha256"],
                         g.evidence_hash(g.split_lines(DEMO_FILES["a.py"].encode()), 1, 2))
        self.assertEqual(entries[0]["run_id"], self.run_id)
        self.assertEqual(entries[0]["session"], "sess-1")

    def test_outside_target_no_entry(self):
        """R4.4: outside the Target → not recorded."""
        os.makedirs(os.path.join(self.ws.root, "other"))
        with open(os.path.join(self.ws.root, "other", "x.txt"), "w") as f:
            f.write("hello\n")
        rc, _, _ = self.ws.record({"path": "other/x.txt", "content": "hello"},
                                  read_path="other/x.txt")
        self.assertEqual(rc, 0)
        self.assertEqual(get_evidence_entries(self.run_dir), [])

    def test_non_utf8_no_entry(self):
        """R4.12 / task 9.1: non-UTF-8 file read → 0 entries written, no crash."""
        bad = os.path.join(self.ws.root, "targets", "demo", "bad.bin")
        with open(bad, "wb") as f:
            f.write(b"\xff\xfe\x00binary\x80")
        rc, _, err = self.ws.record({"path": "targets/demo/bad.bin", "content": "�"},
                                    read_path="targets/demo/bad.bin")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(get_evidence_entries(self.run_dir), [])

    def test_no_active_no_write(self):
        """R15.2: no active run → no writes at all."""
        run_id, run_dir = self.ws.active_run()
        self.ws.abort(run_id)
        before = g.read_text(os.path.join(run_dir, "evidence.jsonl"))
        rc, _, _ = self.ws.record({"path": "targets/demo/a.py", "content": "line 1 content"})
        self.assertEqual(rc, 0)
        self.assertEqual(g.read_text(os.path.join(run_dir, "evidence.jsonl")), before)

    def test_corrupt_last_line_state_corrupt(self):
        """Task 4.4: unparseable last line → exit code 2 STATE_CORRUPT, nothing written."""
        self.ws.record({"path": "targets/demo/a.py", "content": "line 1 content"})
        with open(os.path.join(self.run_dir, "evidence.jsonl"), "a") as f:
            f.write("{half-written line\n")
        before = g.read_text(os.path.join(self.run_dir, "evidence.jsonl"))
        rc, _, err = self.ws.record({"path": "targets/demo/a.py",
                                     "content": "line 1 content\nline 2 content"})
        self.assertEqual(rc, 2)
        self.assertIn("STATE_CORRUPT", err)
        self.assertEqual(g.read_text(os.path.join(self.run_dir, "evidence.jsonl")), before)

    def test_concurrent_recordings(self):
        """R4.5/R4.8: overlapping processing → each recorded exactly once, timestamps strictly
        increasing, no interleaving."""
        targets = [("targets/demo/a.py", DEMO_FILES["a.py"]),
                   ("targets/demo/src/b.txt", DEMO_FILES["src/b.txt"]),
                   ("targets/demo/README.md", DEMO_FILES["README.md"])]
        procs = []
        for rel, content in targets:
            payload = self.ws.write_payload({
                "tool_name": "read_file", "session_id": "s",
                "tool_input": {"path": rel},
                "tool_response": {"path": rel, "content": content}})
            procs.append(subprocess.Popen(
                [sys.executable, self.ws.script("evidence_recorder.py"), payload],
                cwd=self.ws.root, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for p in procs:
            p.communicate(timeout=30)
        entries = get_evidence_entries(self.run_dir)
        self.assertEqual(len(entries), 3)
        ts_list = [g.parse_ts(e["ts"]) for e in entries]
        self.assertEqual(ts_list, sorted(ts_list))
        self.assertEqual(len(set(e["ts"] for e in entries)), 3)
        paths = sorted(e["path"] for e in entries)
        self.assertEqual(paths, ["README.md", "a.py", "src/b.txt"])


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        rc, _, err = self.ws.start()
        assert rc == 0, err
        self.run_id, self.run_dir = self.ws.active_run()

    def tearDown(self):
        self.ws.cleanup()

    def test_active_protection_matrix(self):
        """R11.1: Target ∪ Toolkit_Config ∪ runs-except-Inbox."""
        blocked = [
            "targets/demo/a.py",
            "targets/demo",
            ".kiro/scripts/evil.py",
            ".kiro/agents/x.md",
            ".kiro/steering/y.md",
            ".kiro/hooks/z.json",
            "runs/%s/state.json" % self.run_id,
            "runs/%s/gate-live.jsonl" % self.run_id,
            "runs/%s/inbox" % self.run_id,
            "runs/%s" % self.run_id,
        ]
        for p in blocked:
            rc, _, err = self.ws.guard({"path": p, "content": "x"})
            self.assertEqual(rc, 2, p)
            self.assertIn("PROTECTED_PATH", err, p)
            self.assertIn(p, err, p)
        allowed = [
            "targets/other/x.py",
            ".kiro/specs/notes.md",
            ".kiro/settings.json",
            "runs/%s/inbox/new-sub.json" % self.run_id,
            "control/start.json",
            "src/local.py",
        ]
        for p in allowed:
            rc, _, err = self.ws.guard({"path": p, "content": "x"})
            self.assertEqual((rc, err), (0, ""), p)

    def test_no_active_only_artifacts(self):
        """R11.2: without an active run, Run_Artifacts and reserved paths are blocked
        (including the not-yet-generated gate-live/report)."""
        self.ws.abort(self.run_id)
        blocked = ["runs/%s/state.json" % self.run_id,
                   "runs/%s/gate-live.jsonl" % self.run_id,
                   "runs/%s/report.md" % self.run_id,
                   "runs/%s/report.json" % self.run_id]
        for p in blocked:
            rc, _, err = self.ws.guard({"path": p, "content": "x"})
            self.assertEqual(rc, 2, p)
            self.assertIn("PROTECTED_PATH", err, p)
        for p in ("targets/demo/a.py", "runs/%s/inbox/x-y.json" % self.run_id,
                  "runs/random/dir/whatever.txt"):
            rc, _, _ = self.ws.guard({"path": p, "content": "x"})
            self.assertEqual(rc, 0, p)

    def test_undeterminable_and_payload(self):
        """R11.3/R11.12."""
        rc, _, err = self.ws.guard({"command": "rm -rf /"}, tool="execute_bash")
        self.assertEqual(rc, 2)
        self.assertIn("UNDETERMINABLE", err)
        payload_path = os.path.join(self.ws.root, ".kiro", "scripts", "broken.json")
        with open(payload_path, "w") as f:
            f.write("{broken")
        proc = subprocess.run([sys.executable, self.ws.script("write_guard.py"), payload_path],
                              cwd=self.ws.root, capture_output=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("PAYLOAD_INVALID", proc.stderr.decode())
        # with no active run, the same inputs pass
        self.ws.abort(self.run_id)
        rc, _, _ = self.ws.guard({"command": "ls"}, tool="execute_bash")
        self.assertEqual(rc, 0)
        proc = subprocess.run([sys.executable, self.ws.script("write_guard.py"), payload_path],
                              cwd=self.ws.root, capture_output=True)
        self.assertEqual(proc.returncode, 0)

    def test_delete_dir_containing_protected(self):
        """R11.9: deleting a directory containing a Protected_Path → blocked."""
        rc, _, err = self.ws.guard({"path": "targets/demo"}, tool="delete_file")
        self.assertEqual(rc, 2)
        self.assertIn("PROTECTED_PATH", err)
        rc, _, err = self.ws.guard({"path": "runs/%s" % self.run_id}, tool="delete_file")
        self.assertEqual(rc, 2)
        self.assertIn("PROTECTED_PATH", err)


class TestRendererInvalidation(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        rc, _, err = self.ws.start()
        assert rc == 0, err
        self.run_id, self.run_dir = self.ws.active_run()
        assert self.ws.canary(self.run_id)[0] == 2
        self.ws.record(self.ws.read_file_response())
        entries = get_evidence_entries(self.run_dir)
        lines = g.split_lines(DEMO_FILES["a.py"].encode("utf-8"))
        cit = citation_for(lines, 1, 2, entries[0]["ts"])
        rc, _, err = self.ws.gate(self.run_id, "f1.json",
                                  submission_text(self.run_id, title="finding one",
                                                  citations=[cit]))
        assert rc == 0, err
        # second finding: b.txt
        self.ws.record(self.ws.read_file_response("targets/demo/src/b.txt"),
                       read_path="targets/demo/src/b.txt")
        entries = get_evidence_entries(self.run_dir)
        cit2 = {"location": "src/b.txt:1-2", "evidence_timestamp": entries[-1]["ts"],
                "excerpt": "alpha\nbeta"}
        rc, _, err = self.ws.gate(self.run_id, "f2.json",
                                  submission_text(self.run_id, title="finding two",
                                                  severity="critical",
                                                  citations=[cit2]))
        assert rc == 0, err

    def tearDown(self):
        self.ws.cleanup()

    def test_invalidation_and_order(self):
        """R13.2–R13.4: recheck failures go to the invalidated list; findings sorted by severity."""
        # modify a.py so f1 is invalidated (CONTENT_CHANGED)
        path = os.path.join(self.ws.root, "targets", "demo", "a.py")
        original = DEMO_FILES["a.py"]
        with open(path, "w", encoding="utf-8") as f:
            f.write("TAMPERED\n" + "\n".join(original.split("\n")[1:]))
        rc, _, err = self.ws.finalize(self.run_id, {"Cartographer": "completed"})
        self.assertEqual((rc, err), (0, ""), err)
        report = json.loads(self.ws.read(os.path.join("runs", os.path.basename(self.run_dir), "report.json")))
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["finding_id"], "F-000002")
        self.assertEqual(report["findings"][0]["severity"], "critical")
        self.assertEqual(report["invalidated"][0]["finding_id"], "F-000001")
        self.assertEqual(report["invalidated"][0]["failures"][0]["reason_code"], "CONTENT_CHANGED")
        self.assertEqual(report["invalidated"][0]["failures"][0]["citation_index"], 1)
        self.assertEqual(report["statistics"]["invalidated_count"], 1)
        md = self.ws.read(os.path.join("runs", os.path.basename(self.run_dir), "report.md"))
        self.assertIn("CONTENT_CHANGED", md)
        # the markdown injection specimen produces no structure
        self.assertIn("```text", md)

    def test_md_escape_no_structure(self):
        """R13.10: a malicious description and a special-character file-name Location produce no
        headings/links/fence boundaries or other structure."""
        entries = get_evidence_entries(self.run_dir)
        evil_file = os.path.join(self.ws.root, "targets", "demo", "[link](evil).txt")
        with open(evil_file, "w", encoding="utf-8") as f:
            f.write("bad path content\n")
        self.ws.record({"path": "targets/demo/[link](evil).txt",
                        "content": "bad path content"},
                       read_path="targets/demo/[link](evil).txt")
        entries2 = get_evidence_entries(self.run_dir)
        evil = "# injected\n```python\nEVIL\n```\n[link](x) | table |"
        cit = {"location": "[link](evil).txt:1", "evidence_timestamp": entries2[-1]["ts"],
               "excerpt": "bad path content"}
        rc, _, err = self.ws.gate(self.run_id, "evil.json",
                                  submission_text(self.run_id, title="evil",
                                                  description=evil, citations=[cit]))
        self.assertEqual(rc, 0, err)
        rc, _, err = self.ws.finalize(self.run_id, {"Cartographer": "completed"})
        self.assertEqual(rc, 0, err)
        md = self.ws.read(os.path.join("runs", os.path.basename(self.run_dir), "report.md"))
        self.assertNotIn("```python", md)          # no fence boundary produced
        self.assertNotIn("\n# injected", md)        # no heading produced
        for line in md.split("\n"):
            if "[link](x)" in line or "| table |" in line or "```python" in line:
                # malicious text may only appear inside the description's code-span line
                self.assertTrue(line.startswith("- description: `"), repr(line))
            if "[link](evil).txt" in line:
                # the file-name Location and covered paths must be inside code spans
                self.assertTrue("`[link](evil).txt" in line, repr(line))

    def test_rejected_statistics(self):
        """R13.7: rejected statistics exclude Canary records, count each code once, agent unknown."""
        # create rejections: one multi-code rejection + an invalid agent
        ts0 = get_evidence_entries(self.run_dir)[0]["ts"]
        content = submission_text(self.run_id, agent="Security_Auditor",
                                  title="the issue is at src/b.txt:1",
                                  citations=[citation_for(
                                      g.split_lines(DEMO_FILES["a.py"].encode()), 1, 2, ts0)])
        self.ws.gate(self.run_id, "rej1.json", content)
        self.ws.gate(self.run_id, "rej2.json",
                     submission_text(self.run_id, agent="EvilAgent"))  # SCHEMA_INVALID
        # the Canary record already exists (canary in setUp) → statistics must exclude it
        rc, _, err = self.ws.finalize(self.run_id, {"Cartographer": "completed"})
        self.assertEqual((rc, err), (0, ""), err)
        report = json.loads(self.ws.read(os.path.join("runs", os.path.basename(self.run_dir), "report.json")))
        rej = report["statistics"]["rejected"]
        self.assertEqual(rej["total"], 2)  # rej1 + rej2 (the canary is excluded)
        # rej2: invalid agent + empty citations → CITATION_MISSING short-circuits before SCHEMA_INVALID
        self.assertEqual(rej["by_reason_code"],
                         {"UNCITED_LOCATION": 1, "CITATION_MISSING": 1})
        self.assertEqual(rej["by_agent"], {"Security_Auditor": 1, "unknown": 1})

    def test_empty_findings_disclaimer_and_unlocatable(self):
        """R13.9/R13.20/R13.21: empty-findings disclaimer, evidence-boundary caveat, and the count
        of files that cannot form a valid Location."""
        # delete both cited files so f1/f2 are all invalidated → findings is empty
        os.unlink(os.path.join(self.ws.root, "targets", "demo", "a.py"))
        os.unlink(os.path.join(self.ws.root, "targets", "demo", "src", "b.txt"))
        # create a regular file that cannot form a valid Location (POSIX allows backslashes in
        # file names; the Location syntax forbids "\")
        unloc_file = os.path.join(self.ws.root, "targets", "demo", "bad\\name.txt")
        with open(unloc_file, "w", encoding="utf-8") as f:
            f.write("unlocatable\n")
        rc, _, err = self.ws.finalize(self.run_id, {"Cartographer": "completed"})
        self.assertEqual((rc, err), (0, ""), err)
        run_rel = os.path.join("runs", os.path.basename(self.run_dir))
        report = json.loads(self.ws.read(os.path.join(run_rel, "report.json")))
        md = self.ws.read(os.path.join(run_rel, "report.md"))
        self.assertEqual(report["findings"], [])
        self.assertEqual(len(report["invalidated"]), 2)
        self.assertTrue(report["notes"].get("empty_findings_disclaimer"))
        self.assertIn("semantically correct", report["notes"]["evidence_caveat"])
        self.assertIn("does not prove the Target_Codebase has no defects", md)
        self.assertIn("semantically correct", md)
        self.assertEqual(report["coverage"]["unlocatable_file_count"], 1)
        self.assertEqual(report["coverage"]["unlocatable_paths"], ["bad\\name.txt"])


if __name__ == "__main__":
    unittest.main()
