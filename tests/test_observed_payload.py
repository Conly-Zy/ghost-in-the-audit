#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Observed-payload shape regression (docs/platform-notes.md T5 / e2e-runbook B1, B2).

Drives the real Hook_Launcher's full pre/post chain with the payload shapes
recorded from live Kiro IDE observation (not via Kiro):

  B1 (the fs_write content parameter is named `text`):
    - Start, Canary, Submission, and Finalize all go through `hook_launcher.py pre`
      delivered with the `text` key; Citation_Gate / Run_Controller both extract it;
    - the `content` key stays compatible (the shape driven by all older test
      generations);
    - both keys present with different values → the actually written copy cannot be
      determined → UNDETERMINABLE fail-closed (no ledger written);
    - both keys missing → PAYLOAD_INVALID;
    - fs_append itself was not observed (not claimed as verified): only requires its
      content-extraction path to match fs_write and fail closed the same way.

  B2 (the read_file response is a `<file …>\n<content>\n…\n</content>\n</file>`[Note]
  string; the start line is determined by tool_input.offset (0-based)):
    - evidence recorded via `hook_launcher.py post`, including a non-zero offset
      (the line numbers must equal offset+1; the outer tag lines must not be counted);
    - unwrapped strings / strings missing tags / invalid offsets → 0 entries
      (R4.3 fail-closed);
    - the legacy dict form (content / start_line keys) stays compatible.

  Finally, per runbook section 5, dev/verify_e2e_report.py --require-all-dispatched
  verifies the report of the completed run (RESULT: PASS).

Run: python3 -m unittest tests.test_observed_payload -v
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
import evidence_recorder as er  # noqa: E402

VERIFY = os.path.join(REPO, "dev", "verify_e2e_report.py")

DEMO_FILES = {
    "a.py": "\n".join("line %d content" % i for i in range(1, 61)) + "\n",
    "src/b.txt": "alpha\nbeta\ngamma\n",
    "README.md": "# demo\n",
}


def submission(run_id, agent="Cartographer", category="architecture",
               severity="info", title="t", description="d", citations=None):
    return json.dumps({
        "run_id": run_id, "agent": agent, "title": title, "category": category,
        "severity": severity, "description": description,
        "citations": citations or [],
    }, ensure_ascii=False)


class ObservedWorkspace:
    """Workspace fixture: copies the real scripts; every call goes through the real hook_launcher.py."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="ghost-observed-")
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
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    # -- Hook channels --
    def launcher(self, mode, payload, timeout=60):
        """hook_launcher.py pre/post: the payload JSON is delivered via stdin (fields observed in T2)."""
        event = {"pre": "PreToolUse", "post": "PostToolUse"}[mode]
        obj = {"session_id": "obs-session", "hook_event_name": event, "cwd": self.root}
        obj.update(payload)
        proc = subprocess.run(
            [sys.executable, os.path.join(self.root, ".kiro", "scripts",
                                          "hook_launcher.py"), mode],
            cwd=self.root, input=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return proc.returncode, proc.stderr.decode("utf-8")

    def pre(self, tool_name, tool_input):
        return self.launcher("pre", {"tool_name": tool_name,
                                     "tool_input": tool_input})

    def post(self, tool_name, tool_input, tool_response):
        return self.launcher("post", {"tool_name": tool_name,
                                      "tool_input": tool_input,
                                      "tool_response": tool_response})

    def start(self, target="demo"):
        """Phase 1: Start (the observed text form)."""
        body = json.dumps({"target": target}, ensure_ascii=False)
        rc, err = self.pre("fs_write", {"path": os.path.join(self.root, "control",
                                                             "start.json"),
                                        "text": body})
        if rc == 0:
            self.perform_write("control/start.json", body)
        return rc, err

    def perform_write(self, rel, content):
        """The actual write the IDE performs after the Hook passes (rc 0); filled in by the test in the dry run."""
        path = rel if os.path.isabs(rel) else os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def observed_read_response(self, rel, offset=0, count=None, note=True,
                               language="python", head=None):
        """Builds the read_file response string recorded as observed in T5/B2 (offset is a 0-based start line)."""
        lines = g.read_file_lines(os.path.join(self.root, rel))
        assert lines is not None, rel
        if count is None:
            count = max(0, len(lines) - offset)
        chunk = lines[offset:offset + count]
        if head is None:
            head = '<file name="%s" language="%s" >' % (os.path.basename(rel),
                                                        language)
        body = head + "\n<content>\n" + "\n".join(chunk) + "\n</content>\n</file>"
        if note:
            body += "\nNote: To continue, use offset %d." % (offset + count)
        return body

    def read_via_post(self, rel, offset=None, tool_response=None, count=None):
        """Phase 4: records one read via launcher post; returns all entries after that call."""
        before = self.entries()
        ti = {"path": rel} if offset is None else {"path": rel, "offset": offset}
        if tool_response is None:
            tool_response = self.observed_read_response(
                rel, offset=offset if offset is not None else 0, count=count)
        rc, err = self.post("read_file", ti, tool_response)
        assert rc == 0, "post must not block: %s" % err
        return self.entries()

    # -- observation --
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

    def entries(self):
        run_id, run_dir = self.active_run()
        if run_dir is None:
            return []
        entries, corrupt = g.parse_evidence_log(
            g.read_text(os.path.join(run_dir, "evidence.jsonl")))
        assert corrupt is None
        return entries or []

    def ledger(self):
        run_id, run_dir = self.active_run()
        text = self.read("runs/%s/findings.jsonl" % run_id) or ""
        return [json.loads(l) for l in text.split("\n") if l]

    def rejections(self):
        run_id, run_dir = self.active_run()
        text = self.read("runs/%s/rejections.jsonl" % run_id) or ""
        return [json.loads(l) for l in text.split("\n") if l]

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class BaseObserved(unittest.TestCase):
    def setUp(self):
        self.ws = ObservedWorkspace()

    def tearDown(self):
        self.ws.cleanup()

    def start_active(self):
        rc, err = self.ws.start()
        self.assertEqual((rc, err), (0, ""), err)
        run_id, run_dir = self.ws.active_run()
        self.assertIsNotNone(run_id)
        return run_id, run_dir


# ============================================================
# B2: parse_read_file_text / start_line_of_read pure-function matrix
# ============================================================

class TestParseReadFileText(unittest.TestCase):
    def test_observed_shape(self):
        text = ('<file name="a.py" language="python" >\n<content>\n'
                "alpha\nbeta\n</content>\n</file>")
        self.assertEqual(er.parse_read_file_text(text), "alpha\nbeta")

    def test_note_suffix_ignored(self):
        text = ('<file name="a.py" language="python" >\n<content>\n'
                "alpha\n</content>\n</file>\nNote: To continue, use offset 1.")
        self.assertEqual(er.parse_read_file_text(text), "alpha")

    def test_no_attributes_and_multiline_attrs(self):
        self.assertEqual(er.parse_read_file_text("<file>\n<content>\nx\n</content>\n</file>"), "x")
        text = '<file name="a b.py" language="python" >\n<content>\n\n</content>\n</file>'
        self.assertEqual(er.parse_read_file_text(text), "")  # empty file → 0 lines

    def test_unknown_shapes_fail_closed(self):
        bad = [
            None,
            "",
            "line 1 content\nline 2 content",          # unwrapped string (unknown shape)
            "<filex name=\"x\">\n<content>\nx\n</content>\n</file>",
            '<file name="x" language="python" >',       # no following lines
            '<file name="x">\nline1\nline2\n',          # missing <content>
            '<file name="x">\n<content>\nx\n</content>',  # missing </file>
            '<file name="x">\n<content>\nx\n</content>\n</othertag>',  # closing tag mismatch
            '<file name="x">\n<content>\na\n</CONTENT>\n</file>',      # case mismatch
            '<file name="x">\n<content>\na\n</content>\n</file>junk',  # unknown closing shape
            '<file name="x">\n<content>\na\n</content>\n</file>\nextra',
        ]
        for text in bad:
            self.assertIsNone(er.parse_read_file_text(text), repr(text))

    def test_content_with_own_content_tag_fails_closed(self):
        # the file itself contains a "</content>" line: it is not followed by "</file>" →
        # no overall match → 0 entries
        text = ('<file name="x.html" language="html" >\n<content>\n'
                "<div>\n</content>\nmore\n</content>\n</file>")
        self.assertIsNone(er.parse_read_file_text(text))

    def test_start_line_of_read_offset_semantics(self):
        self.assertEqual(er.start_line_of_read({}), 1)                       # offset missing
        self.assertEqual(er.start_line_of_read({"offset": 0}), 1)            # 0-based
        self.assertEqual(er.start_line_of_read({"offset": 9}), 10)
        self.assertEqual(er.start_line_of_read({"path": "a.py", "offset": 12345}), 12346)
        for bad in (-1, -100, True, False, "2", 2.0, None, [2]):
            self.assertIsNone(er.start_line_of_read({"offset": bad}), repr(bad))


# ============================================================
# B2: Evidence_Recorder response shape matrix through the real post launcher
# ============================================================

class TestRecorderObservedShapes(BaseObserved):
    def setUp(self):
        super().setUp()
        self.start_active()  # without an active run, post records nothing

    def record(self, rel, offset=None, tool_response=None, count=None):
        before = self.ws.entries()
        ti = {"path": rel} if offset is None else {"path": rel, "offset": offset}
        if tool_response is None:
            tool_response = self.ws.observed_read_response(
                rel, offset=offset if offset is not None else 0, count=count)
        rc, err = self.ws.post("read_file", ti, tool_response)
        self.assertEqual((rc, err), (0, ""), err)
        return self.ws.entries()[len(before):]

    def test_wrapper_without_offset_starts_at_1(self):
        new = self.record("targets/demo/a.py", offset=None)  # full text (60 lines)
        self.assertEqual([(e["path"], e["start"], e["end"]) for e in new],
                         [("a.py", 1, 60)])

    def test_wrapper_offset_zero_equals_start_1(self):
        new = self.record("targets/demo/src/b.txt", offset=0)
        self.assertEqual([(e["path"], e["start"], e["end"]) for e in new],
                         [("src/b.txt", 1, 3)])

    def test_wrapper_nonzero_offset_lines_follow_offset(self):
        """B2 core: offset=9 (0-based) → lines 10–14; the 2 wrapper tag lines must not be counted."""
        new = self.record("targets/demo/a.py", offset=9, count=5)
        self.assertEqual([(e["path"], e["start"], e["end"]) for e in new],
                         [("a.py", 10, 14)])
        lines = g.split_lines(DEMO_FILES["a.py"].encode("utf-8"))
        self.assertEqual(new[0]["sha256"], g.evidence_hash(lines, 10, 14))

    def test_invalid_offsets_record_nothing(self):
        for offset in (-1, "9", True, 2.0):
            before = self.ws.entries()
            ti = {"path": "targets/demo/a.py", "offset": offset}
            resp = self.ws.observed_read_response("targets/demo/a.py", offset=0)
            rc, err = self.ws.post("read_file", ti, resp)
            self.assertEqual((rc, err), (0, ""), repr(offset))
            self.assertEqual(self.ws.entries(), before, repr(offset))

    def test_unwrapped_and_broken_strings_record_nothing(self):
        before = self.ws.entries()
        for resp in ("line 1 content\nline 2 content",          # unwrapped
                     "<file>\nline1\n",                          # missing <content>
                     "<file>\n<content>\nline 1 content\n</content>"):  # missing </file>
            rc, err = self.ws.post("read_file", {"path": "targets/demo/a.py"}, resp)
            self.assertEqual((rc, err), (0, ""), repr(resp[:40]))
            self.assertEqual(self.ws.entries(), before, repr(resp[:40]))

    def test_offset_beyond_eof_records_nothing(self):
        rc, err = self.ws.post("read_file", {"path": "targets/demo/a.py", "offset": 1000},
                               self.ws.observed_read_response("targets/demo/a.py",
                                                              offset=1000))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.ws.entries(), [])

    def test_legacy_dict_response_still_recorded(self):
        """Compatibility: the legacy dict form (content key) keeps its original behavior."""
        new = self.record("targets/demo/a.py",
                          tool_response={"path": "targets/demo/a.py",
                                         "content": "line 1 content\nline 2 content"})
        self.assertEqual([(e["path"], e["start"], e["end"]) for e in new],
                         [("a.py", 1, 2)])

    def test_legacy_dict_start_line_key_still_recorded(self):
        new = self.record("targets/demo/a.py",
                          tool_response={"content": "line 5 content\nline 6 content",
                                         "start_line": 5})
        self.assertEqual([(e["start"], e["end"]) for e in new], [(5, 6)])

    def test_dict_response_honors_offset_without_start_line(self):
        """For the dict form without an explicit start_line, positioning likewise follows the observed offset semantics."""
        new = self.record("targets/demo/a.py", offset=9,
                          tool_response={"content": "line 10 content\nline 11 content"})
        self.assertEqual([(e["start"], e["end"]) for e in new], [(10, 11)])

    def test_dict_invalid_start_line_and_offset_records_nothing(self):
        before = self.ws.entries()
        rc, err = self.ws.post(
            "read_file", {"path": "targets/demo/a.py", "offset": -1},
            {"content": "line 1 content"})
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.ws.entries(), before)


# ============================================================
# B1: Citation_Gate content-parameter matrix through the real pre launcher
# ============================================================

class TestGateContentKeys(BaseObserved):
    def seed_evidence(self):
        entries = self.ws.read_via_post("targets/demo/src/b.txt")
        self.assertEqual(len(entries), 1)
        return entries[0]

    def gate(self, run_id, file_name, tool_input):
        path = "runs/%s/inbox/%s" % (run_id, file_name)
        return self.ws.pre("fs_write", dict(tool_input, path=path))

    def test_text_key_accepted(self):
        run_id, _ = self.start_active()
        ts = self.seed_evidence()["ts"]
        cit = {"location": "src/b.txt:1", "evidence_timestamp": ts, "excerpt": "alpha"}
        rc, err = self.gate(run_id, "text-key.json",
                            {"text": submission(run_id, citations=[cit])})
        self.assertEqual((rc, err), (0, ""), err)
        self.assertEqual([r["finding_id"] for r in self.ws.ledger()], ["F-000001"])

    def test_content_key_still_accepted(self):
        run_id, _ = self.start_active()
        ts = self.seed_evidence()["ts"]
        cit = {"location": "src/b.txt:1", "evidence_timestamp": ts, "excerpt": "alpha"}
        rc, err = self.gate(run_id, "content-key.json",
                            {"content": submission(run_id, citations=[cit])})
        self.assertEqual((rc, err), (0, ""), err)
        self.assertEqual(len(self.ws.ledger()), 1)

    def test_equal_keys_accepted(self):
        run_id, _ = self.start_active()
        ts = self.seed_evidence()["ts"]
        cit = {"location": "src/b.txt:1", "evidence_timestamp": ts, "excerpt": "alpha"}
        sub = submission(run_id, citations=[cit])
        rc, err = self.gate(run_id, "same-key.json", {"text": sub, "content": sub})
        self.assertEqual((rc, err), (0, ""), err)
        self.assertEqual(len(self.ws.ledger()), 1)

    def test_conflicting_keys_fail_closed_undeterminable(self):
        run_id, _ = self.start_active()
        ts = self.seed_evidence()["ts"]
        cit = {"location": "src/b.txt:1", "evidence_timestamp": ts, "excerpt": "alpha"}
        rc, err = self.gate(run_id, "conflict.json",
                            {"text": submission(run_id, title="A", citations=[cit]),
                             "content": submission(run_id, title="B", citations=[cit])})
        self.assertEqual(rc, 2)
        self.assertIn("UNDETERMINABLE", err)
        self.assertEqual(self.ws.ledger(), [])
        self.assertEqual(self.ws.rejections(), [])  # order-4 short-circuit: no ledger written at all

    def test_missing_content_fails_closed_payload_invalid(self):
        run_id, _ = self.start_active()
        self.seed_evidence()
        rc, err = self.gate(run_id, "nocontent.json", {})
        self.assertEqual(rc, 2)
        self.assertIn("PAYLOAD_INVALID", err)
        self.assertEqual(self.ws.ledger(), [])
        self.assertEqual(self.ws.rejections(), [])

    def test_empty_text_keeps_schema_invalid_path(self):
        """Empty-string content behaves like the old implementation (goes to Schema validation → SCHEMA_INVALID)."""
        run_id, _ = self.start_active()
        self.seed_evidence()
        rc, err = self.gate(run_id, "empty.json", {"text": ""})
        self.assertEqual(rc, 2)
        self.assertIn("SCHEMA_INVALID", err)

    def test_fs_append_text_key_accepted(self):
        """fs_append was not observed (not claimed as verified); only its content-extraction path
        is verified to match fs_write."""
        run_id, _ = self.start_active()
        ts = self.seed_evidence()["ts"]
        cit = {"location": "src/b.txt:1", "evidence_timestamp": ts, "excerpt": "alpha"}
        rc, err = self.ws.pre(
            "fs_append",
            {"path": "runs/%s/inbox/appended.json" % run_id,
             "text": submission(run_id, agent="Dependency_Auditor",
                                category="dependency", citations=[cit])})
        self.assertEqual((rc, err), (0, ""), err)
        self.assertEqual(len(self.ws.ledger()), 1)

    def test_fs_append_missing_content_fails_closed(self):
        run_id, _ = self.start_active()
        self.seed_evidence()
        rc, err = self.ws.pre("fs_append",
                              {"path": "runs/%s/inbox/appended.json" % run_id})
        self.assertEqual(rc, 2)
        self.assertIn("PAYLOAD_INVALID", err)
        self.assertEqual(self.ws.ledger(), [])

    def test_str_replace_no_content_rule_undeterminable(self):
        """Tools whose catalog entry declares no content rule stay UNDETERMINABLE (fail-closed)."""
        run_id, _ = self.start_active()
        self.seed_evidence()
        rc, err = self.ws.pre("str_replace",
                              {"path": "runs/%s/inbox/sr.json" % run_id})
        self.assertEqual(rc, 2)
        self.assertIn("UNDETERMINABLE", err)
        self.assertEqual(self.ws.ledger(), [])


# ============================================================
# End-to-end regression: pre/post launcher × observed shapes × Start → Canary →
# evidence (non-zero offset) → accepted/rejected Findings → Finalize →
# verify_e2e_report.py
# ============================================================

class TestObservedPayloadEndToEnd(BaseObserved):
    def test_observed_payload_full_flow(self):
        ws = self.ws

        # —— Phase 1: Start (B1: the text form) ——
        rc, err = ws.start()
        self.assertEqual((rc, err), (0, ""), err)
        run_id, run_dir = ws.active_run()
        state = g.parse_state(g.read_text(os.path.join(run_dir, "state.json")))
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["target"], "demo")
        self.assertTrue(os.path.exists(os.path.join(ws.root, "control", "start.json")))

        # —— Phase 3: Canary veto (CITATION_MISSING + gate-live, no System_Reason_Code) ——
        canary = g.canary_submission_text(run_id)
        rc, err = ws.pre("fs_write",
                         {"path": "runs/%s/inbox/%s" % (run_id, g.CANARY_FILE_NAME),
                          "text": canary})
        self.assertEqual(rc, 2)
        self.assertIn("CITATION_MISSING", err)
        for code in g.SYSTEM_REASON_CODES:
            self.assertNotIn(code, err)
        self.assertEqual(os.listdir(os.path.join(run_dir, "inbox")), [])  # the write was blocked
        gate = [json.loads(l) for l in
                ws.read("runs/%s/gate-live.jsonl" % run_id).split("\n") if l]
        self.assertEqual(len(gate), 1)
        self.assertEqual(gate[0]["run_id"], run_id)
        self.assertEqual(gate[0]["sha256"], g.bytes_hash(canary))
        self.assertEqual(self.ws.rejections()[0]["reason_codes"], ["CITATION_MISSING"])

        # —— Phase 4: Cartographer reads (B2: the observed response shape with a non-zero offset) ——
        lines = g.split_lines(DEMO_FILES["a.py"].encode("utf-8"))
        entries = ws.read_via_post("targets/demo/a.py", offset=9, count=5)  # lines 10–14
        self.assertEqual(len(entries), 1)
        e_offset = entries[0]
        self.assertEqual((e_offset["path"], e_offset["start"], e_offset["end"]),
                         ("a.py", 10, 14))
        self.assertEqual(e_offset["sha256"], g.evidence_hash(lines, 10, 14))
        self.assertEqual(e_offset["session"], "obs-session")

        entries = ws.read_via_post("targets/demo/src/b.txt", offset=0)
        e_b = entries[-1]
        self.assertEqual((e_b["path"], e_b["start"], e_b["end"]),
                         ("src/b.txt", 1, 3))

        # the legacy dict response form is still recorded via launcher post (compatibility kept)
        entries = ws.read_via_post(
            "targets/demo/README.md",
            tool_response={"path": "targets/demo/README.md", "content": "# demo\n"})
        e_readme = entries[-1]
        self.assertEqual((e_readme["path"], e_readme["start"]), ("README.md", 1))

        all_entries = ws.entries()
        self.assertEqual(len(all_entries), 3)
        ts_list = [e["ts"] for e in all_entries]
        self.assertEqual(ts_list, sorted(set(ts_list)))  # strictly increasing

        # —— Phase 5: Findings accepted (fs_write text / fs_append text / fs_write content compatibility) ——
        cit_a = {"location": "a.py:10-11", "evidence_timestamp": e_offset["ts"],
                 "excerpt": "line 10 content\nline 11 content"}
        rc, err = ws.pre("fs_write",
                         {"path": "runs/%s/inbox/offset-finding.json" % run_id,
                          "text": submission(run_id, citations=[cit_a])})
        self.assertEqual((rc, err), (0, ""), err)
        ws.perform_write("runs/%s/inbox/offset-finding.json" % run_id,
                         submission(run_id, citations=[cit_a]))

        cit_b = {"location": "src/b.txt:1", "evidence_timestamp": e_b["ts"],
                 "excerpt": "alpha"}
        rc, err = ws.pre("fs_append",
                         {"path": "runs/%s/inbox/dep-pins.json" % run_id,
                          "text": submission(run_id, agent="Dependency_Auditor",
                                             category="dependency", severity="high",
                                             title="outdated dependency pin",
                                             description="See the ledger entry.",
                                             citations=[cit_b])})
        self.assertEqual((rc, err), (0, ""), err)

        cit_r = {"location": "README.md:1", "evidence_timestamp": e_readme["ts"],
                 "excerpt": "# demo"}
        rc, err = ws.pre("fs_write",
                         {"path": "runs/%s/inbox/quality-note.json" % run_id,
                          "content": submission(run_id, agent="Quality_Auditor",
                                                category="quality", severity="medium",
                                                title="README lacks a description",
                                                description="See the ledger entry.",
                                                citations=[cit_r])})
        self.assertEqual((rc, err), (0, ""), err)  # the content-key compatibility path is still accepted

        ledger = ws.ledger()
        self.assertEqual([r["finding_id"] for r in ledger],
                         ["F-000001", "F-000002", "F-000003"])
        self.assertEqual([r["submission_file"] for r in ledger],
                         ["offset-finding.json", "dep-pins.json", "quality-note.json"])

        # —— Phase 5: Finding rejected (the text form + a wrong excerpt) ——
        rej_before = len(ws.rejections())
        bad_cit = dict(cit_a, excerpt="WRONG")
        rc, err = ws.pre("fs_write",
                         {"path": "runs/%s/inbox/bad-excerpt.json" % run_id,
                          "text": submission(run_id, agent="Security_Auditor",
                                             category="security",
                                             citations=[bad_cit])})
        self.assertEqual(rc, 2)
        self.assertIn("EXCERPT_MISMATCH", err)
        rejections = ws.rejections()
        self.assertEqual(len(rejections), rej_before + 1)
        self.assertEqual(rejections[-1]["reason_codes"], ["EXCERPT_MISMATCH"])
        self.assertEqual(rejections[-1]["agent"], "Security_Auditor")

        # —— B1 fail-closed: conflicting/missing content keys, no ledger written at all ——
        ledger_before, rej_before = len(ws.ledger()), len(ws.rejections())
        rc, err = ws.pre("fs_write",
                         {"path": "runs/%s/inbox/conflict.json" % run_id,
                          "text": submission(run_id, title="A", citations=[cit_a]),
                          "content": submission(run_id, title="B", citations=[cit_a])})
        self.assertEqual(rc, 2)
        self.assertIn("UNDETERMINABLE", err)
        rc, err = ws.pre("fs_write",
                         {"path": "runs/%s/inbox/nocontent.json" % run_id})
        self.assertEqual(rc, 2)
        self.assertIn("PAYLOAD_INVALID", err)
        self.assertEqual(len(ws.ledger()), ledger_before)
        self.assertEqual(len(ws.rejections()), rej_before)

        # —— Phase 6: Finalize (the text form) → completed ——
        finalize_body = {
            "run_id": run_id,
            "completion": {"Cartographer": "completed", "Security_Auditor": "completed",
                           "Dependency_Auditor": "completed", "Quality_Auditor": "completed"},
            "summary_statements": ["See F-000001, F-000002 and F-000003."],
        }
        rc, err = ws.pre("fs_write",
                         {"path": os.path.join(ws.root, "control", "finalize.json"),
                          "text": json.dumps(finalize_body, ensure_ascii=False)})
        self.assertEqual((rc, err), (0, ""), err)
        ws.perform_write("control/finalize.json",
                         json.dumps(finalize_body, ensure_ascii=False))
        state = g.parse_state(g.read_text(os.path.join(run_dir, "state.json")))
        self.assertEqual(state["status"], "completed")
        self.assertIsNotNone(g.parse_ts(state["finished_at"]))

        # —— Phase 7: report and verification script (runbook section 5) ——
        report = json.loads(ws.read("runs/%s/report.json" % run_id))
        self.assertEqual([f["finding_id"] for f in report["findings"]],
                         ["F-000002", "F-000003", "F-000001"])  # severity ordering
        self.assertEqual(report["invalidated"], [])
        self.assertEqual(report["statistics"]["rejected"]["total"], 1)  # the Canary is excluded
        self.assertTrue(os.path.exists(os.path.join(run_dir, "report.md")))

        proc = subprocess.run(
            [sys.executable, VERIFY, run_dir, "--require-all-dispatched"],
            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        out = proc.stdout.decode("utf-8")
        self.assertEqual(proc.returncode, 0,
                         "verify failed:\n%s\n%s" % (out, proc.stderr.decode()))
        self.assertIn("RESULT: PASS", out)


if __name__ == "__main__":
    unittest.main()
