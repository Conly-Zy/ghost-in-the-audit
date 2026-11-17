#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-check for dev/verify_e2e_report.py (the verification tool of tasks.md 9.2).

Uses the dry-run test bench (tests/test_dry_run.SimWorkspace — real scripts, not
via Kiro) to walk dev/e2e-sample-target/ through a full lifecycle: Start →
Canary → the four Agents' Submissions (including one rejected and one
invalidated by a Target change before Finalize) → Finalize. Then:
- positive case: the verify script reports PASS (exit code 0) on the really
  generated report;
- negative cases: after tampering with report.json / report.md / state.json it
  reports FAIL (exit code 1).
Run: python3 -m unittest tests.test_verify_e2e -v
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, ".kiro", "scripts"))
sys.path.insert(0, os.path.join(REPO, "dev"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ghostlib as g  # noqa: E402
import verify_e2e_report as v  # noqa: E402
from test_dry_run import SimWorkspace, submission  # noqa: E402

SAMPLE = os.path.join(REPO, "dev", "e2e-sample-target")
VERIFY = os.path.join(REPO, "dev", "verify_e2e_report.py")


def _entry_ts(ws, run_id, path):
    for line in ws.read("runs/%s/evidence.jsonl" % run_id).split("\n"):
        if line and json.loads(line)["path"] == path:
            return json.loads(line)["ts"]
    raise AssertionError("no evidence for " + path)


class VerifyE2EReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ws = cls.ws = SimWorkspace()
        shutil.copytree(SAMPLE, os.path.join(ws.root, "targets", "pinger"))
        run_id, err = ws.orchestrator_start("pinger")
        assert run_id is not None, "sample target must pass Start checks: " + err
        assert ws.orchestrator_canary(run_id)
        cls.run_id = run_id
        cls.run_dir = os.path.join(ws.root, "runs", run_id)

        for rel in ("pinger/main.py", "pinger/ops.py", "pinger/config.py",
                    "requirements.txt"):
            ws.read_tool("targets/pinger/" + rel)
        ts = {p: _entry_ts(ws, run_id, p) for p in
              ("pinger/main.py", "pinger/ops.py", "pinger/config.py", "requirements.txt")}

        def cit(loc, path, excerpt):
            return {"location": loc, "evidence_timestamp": ts[path], "excerpt": excerpt}

        subs = [
            ("entry-point.json", "Cartographer", "architecture", "info",
             "CLI entry at pinger/main.py:15-16", "the module entry is at pinger/main.py:15-16",
             [cit("pinger/main.py:15-16", "pinger/main.py",
                  'if __name__ == "__main__":\n    sys.exit(main(sys.argv))')]),
            ("hardcoded-secret.json", "Security_Auditor", "security", "high",
             "Hardcoded password pinger/config.py:1", "the admin password is hardcoded in source",
             [cit("pinger/config.py:1", "pinger/config.py",
                  'ADMIN_PASSWORD = "changeme-sample-not-a-real-secret"')]),
            ("cmd-injection.json", "Security_Auditor", "security", "critical",
             "Command injection at pinger/ops.py:12", "host is concatenated into os.system unescaped",
             [cit("pinger/ops.py:12", "pinger/ops.py",
                  'os.system("ping -c 1 -W " + str(timeout) + " " + host)')]),
            ("old-requests.json", "Dependency_Auditor", "dependency", "medium",
             "requests pinned to an old version", "requirements.txt:1 pins requests==2.19.0",
             [cit("requirements.txt:1", "requirements.txt", "requests==2.19.0")]),
            ("swallowed-exception.json", "Quality_Auditor", "quality", "low",
             "Swallowed exception pinger/ops.py:13-14", "except Exception: pass",
             [cit("pinger/ops.py:13-14", "pinger/ops.py",
                  "except Exception:\n        pass")]),
        ]
        # one rejected (wrong timestamp → EVIDENCE_NOT_FOUND), counted in the rejected statistics
        bad = dict(subs[1][6][0], evidence_timestamp="2020-01-01T00:00:00.000Z")
        rc, err = ws.submit(run_id, "rejected-once.json", submission(
            run_id, "Security_Auditor", "security", "high", "t", "d", [bad]))
        assert rc == 2 and "EVIDENCE_NOT_FOUND" in err, err
        for name, agent, cat, sev, title, desc, cits in subs:
            rc, err = ws.submit(run_id, name,
                                submission(run_id, agent, cat, sev, title, desc, cits))
            assert (rc, err) == (0, ""), (name, err)

        # modify requirements.txt before Finalize → old-requests is CONTENT_CHANGED in the
        # recheck (line 3 changes: still inside the Evidence_Entry 1-3 span so the hash
        # changes; the Citation cites only line 1)
        req = os.path.join(ws.root, "targets", "pinger", "requirements.txt")
        text = open(req, encoding="utf-8").read().replace("flask\n", "flask==0.12\n")
        with open(req, "w", encoding="utf-8") as f:
            f.write(text)

        body = {"run_id": run_id,
                "completion": {"Cartographer": "completed",
                               "Security_Auditor": "completed",
                               "Dependency_Auditor": "completed",
                               "Quality_Auditor": "failed"},
                "summary_statements": ["The most severe item: see F-000003 (pinger/ops.py:12)."]}
        text = json.dumps(body, ensure_ascii=False)
        rc, err = ws.write_file("control/finalize.json", text)
        assert (rc, err) == (0, ""), err
        # the test bench only runs the Hook and does not really write the file; write it
        # manually to simulate the state after Kiro's write succeeded
        with open(os.path.join(ws.root, "control", "finalize.json"), "w",
                  encoding="utf-8") as f:
            f.write(text)
        cls.report = json.loads(ws.read("runs/%s/report.json" % run_id))
        cls.originals = {name: open(os.path.join(cls.run_dir, name), "rb").read()
                         for name in ("report.json", "report.md", "state.json")}

    @classmethod
    def tearDownClass(cls):
        cls.ws.cleanup()

    def tearDown(self):
        for name, data in self.originals.items():
            with open(os.path.join(self.run_dir, name), "wb") as f:
                f.write(data)

    def _cli(self, *extra):
        return subprocess.run([sys.executable, VERIFY, self.run_dir] + list(extra),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)

    # ---------- positive cases ----------
    def test_fixture_shape(self):
        """The fixture really covers: a non-empty invalidated list, non-zero rejected
        statistics, all four Agent statuses."""
        self.assertEqual([f["finding_id"] for f in self.report["findings"]],
                         ["F-000003", "F-000002", "F-000005", "F-000001"])
        self.assertEqual([i["finding_id"] for i in self.report["invalidated"]],
                         ["F-000004"])
        self.assertEqual(self.report["statistics"]["rejected"]["total"], 1)
        self.assertEqual(self.report["run"]["agents"]["Quality_Auditor"], "failed")

    def test_real_report_passes(self):
        ck = v.verify(self.run_dir)
        self.assertEqual([r for r in ck.results if not r[1]], [])
        proc = self._cli()
        out = proc.stdout.decode()
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("RESULT: PASS", out)
        self.assertIn("PASS  run.agents matches control/finalize.json completion", out)

    def test_require_all_dispatched(self):
        self.assertEqual(self._cli("--require-all-dispatched").returncode, 0)

    # ---------- negative cases ----------
    def _tamper_json(self, mutate):
        p = os.path.join(self.run_dir, "report.json")
        doc = json.loads(open(p, encoding="utf-8").read())
        mutate(doc)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, ensure_ascii=False, indent=1))

    def _assert_fails(self, expected_check):
        ck = v.verify(self.run_dir)
        failed = [r[0] for r in ck.results if not r[1]]
        self.assertIn(expected_check, failed)
        proc = self._cli()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("RESULT: FAIL", proc.stdout.decode())

    def test_tampered_severity_count(self):
        self._tamper_json(lambda d: d["statistics"]["by_severity"].update(high=5))
        self._assert_fails("statistics.by_severity (all 5 tiers) == recomputed")

    def test_dropped_finding(self):
        self._tamper_json(lambda d: d["findings"].pop())
        self._assert_fails("findings list == ledger records that passed the recheck (including order)")

    def test_reordered_findings(self):
        self._tamper_json(lambda d: d["findings"].reverse())
        self._assert_fails("findings list sorted critical→info, ties in acceptance order")

    def test_edited_finding_text(self):
        self._tamper_json(lambda d: d["findings"][0].update(title="tampered title"))
        self._assert_fails("each finding's Canonical_Form is byte-identical to the ledger's")

    def test_emptied_invalidated_list(self):
        self._tamper_json(lambda d: d.update(invalidated=[]))
        self._assert_fails("invalidated list == ledger records that failed the recheck "
                           "(with Reason_Code and citation index/field path)")

    def test_rejected_counts_include_canary(self):
        self._tamper_json(lambda d: d["statistics"]["rejected"].update(total=2))
        self._assert_fails("statistics.rejected == recomputed from the Rejection_Log "
                           "(Canary sha256 excluded)")

    def test_bad_citation_timestamp(self):
        def mutate(d):
            d["findings"][0]["citations"][0]["evidence_timestamp"] = "2001-01-01T00:00:00.000Z"
        self._tamper_json(mutate)
        self._assert_fails("each Citation has a valid file:line and a timestamp matching an "
                           "Evidence_Entry that covers it")

    def test_invalid_agent_status(self):
        self._tamper_json(lambda d: d["run"]["agents"].update(Cartographer="done"))
        self._assert_fails("run.agents contains exactly the four Audit_Agents, values in "
                           "completed/failed/not_dispatched")

    def test_coverage_mismatch(self):
        self._tamper_json(lambda d: d["coverage"].update(target_regular_file_count=1))
        self._assert_fails("coverage == recomputed from Target + Evidence_Log")

    def test_markdown_missing_finding(self):
        p = os.path.join(self.run_dir, "report.md")
        md = open(p, encoding="utf-8").read().replace("### F-000002", "### X-000002")
        with open(p, "w", encoding="utf-8") as f:
            f.write(md)
        self._assert_fails("report.md finding heading IDs (with order) == report.json findings list")

    def test_state_not_completed(self):
        p = os.path.join(self.run_dir, "state.json")
        st = json.loads(open(p, encoding="utf-8").read())
        st["finished_at"] = "2030-01-01T00:00:00.000Z"
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(st))
        self._assert_fails("report.run.finished_at == state.json")


if __name__ == "__main__":
    unittest.main()
