#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dev/verify_e2e_report.py — end-to-end audit result verification (tasks.md 9.2; R13, D2.2).

Usage:
    python3 dev/verify_e2e_report.py runs/<run_id> [--workspace DIR]
                                     [--require-all-dispatched]

For a completed Run_Directory, recompute what the Audit_Report should contain
from the Run_Artifacts and compare it item by item with report.json / report.md:

- state.json is completed and finished_at matches the report;
- report.json's findings list == the ledger records that pass the recheck
  (Canonical_Form byte-identical to the ledger's), and the invalidated list
  == the records that fail it;
- severity ordering; statistics (by_severity / by_category with all 5 tiers,
  invalidated count, rejected statistics excluding the Canary sha256 entries
  from gate-live.jsonl); coverage data;
- all four Audit_Agents' Completion_Status present and valid;
- every Citation's Location is valid, its Evidence_Timestamp is valid, and
  evidence.jsonl contains an entry covering that Location;
- report.md contains the same Finding_ID and Citation lines.

The recheck uses the **current** Target_Codebase: if the Target changed after
Finalize, the invalidated list will not match (which is itself a difference
that must be reported).

Older-report compatibility: reports rendered by earlier versions whose output
was Chinese are still verified where practical — the legacy agent status
"未派发" (meaning "not dispatched"), the legacy markdown section headers
"## 失效列表" / "## 覆盖数据" (meaning "Invalidated" / "Coverage"), and the
legacy Chinese statistic/coverage lines are accepted alongside the current
English forms.

Python 3.9+ standard library only; reuses the same implementation as
`.kiro/scripts/ghostlib.py` and `report_renderer.py`.
Exit codes: 0 = all PASS; 1 = at least one FAIL; 2 = usage error.
"""

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, ".kiro", "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import ghostlib as g  # noqa: E402
import report_renderer as rr  # noqa: E402

# Completion_Status values: "not_dispatched" is the current rendering; "未派发"
# is the legacy Chinese rendering, still accepted when verifying older reports.
AGENT_STATUS_VALUES = ("completed", "failed", "未派发", "not_dispatched")
NOT_DISPATCHED = ("未派发", "not_dispatched")


class Checks:
    def __init__(self):
        self.results = []  # (name, ok, detail)

    def add(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        return ok

    @property
    def failed(self):
        return [r for r in self.results if not r[1]]


def _read_jsonl(path):
    text = g.read_text(path)
    if text is None:
        return None
    out = []
    for line in text.split("\n"):
        if line == "":
            continue
        obj = g.strict_json_loads(line)
        if obj is None:
            return None
        out.append(obj)
    return out


def verify(run_dir, workspace=None, require_all_dispatched=False):
    """→ Checks. Never raises: any read/parse failure is recorded as a FAIL
    and verification continues as far as possible."""
    ck = Checks()
    run_dir = os.path.abspath(run_dir)
    if workspace is None:
        workspace = os.path.dirname(os.path.dirname(run_dir))
    workspace = os.path.abspath(workspace)
    run_name = os.path.basename(run_dir)

    # ---------- 1 artifacts readable ----------
    state_text = g.read_text(os.path.join(run_dir, "state.json"))
    state = g.parse_state(state_text) if state_text is not None else None
    if not ck.add("state.json readable and strictly parseable", state is not None):
        return ck
    entries, bad_line = g.parse_evidence_log(
        g.read_text(os.path.join(run_dir, "evidence.jsonl")))
    ck.add("evidence.jsonl parseable with strictly increasing timestamps",
           entries is not None,
           "" if entries is not None else "line %s" % bad_line)
    ledger = _read_jsonl(os.path.join(run_dir, "findings.jsonl"))
    ck.add("findings.jsonl parseable", ledger is not None)
    rejections = _read_jsonl(os.path.join(run_dir, "rejections.jsonl"))
    ck.add("rejections.jsonl parseable", rejections is not None)
    gate = _read_jsonl(os.path.join(run_dir, "gate-live.jsonl"))
    ck.add("gate-live.jsonl present and parseable", gate is not None)
    report_text = g.read_text(os.path.join(run_dir, "report.json"))
    report = g.strict_json_loads(report_text) if report_text is not None else None
    ck.add("report.json present and a JSON object", isinstance(report, dict))
    md = g.read_text(os.path.join(run_dir, "report.md"))
    ck.add("report.md present and valid UTF-8", md is not None)
    if None in (entries, ledger, rejections, gate, md) or not isinstance(report, dict):
        return ck

    run = report.get("run") or {}

    # ---------- 2 status and metadata ----------
    ck.add("state.status == completed", state["status"] == "completed",
           "status=%s" % state["status"])
    ck.add("state.run_id == Run_Directory name", state["run_id"] == run_name,
           "%s vs %s" % (state["run_id"], run_name))
    for key in ("run_id", "target", "started_at", "finished_at"):
        ck.add("report.run.%s == state.json" % key, run.get(key) == state.get(key),
               "%r vs %r" % (run.get(key), state.get(key)))
    ck.add("finished_at is a valid Evidence_Timestamp",
           g.parse_ts(state.get("finished_at")) is not None)
    ck.add("report.md contains the run_id and started/finished times",
           all(isinstance(v, str) and rr.c(v) in md
               for v in (state["run_id"], state["started_at"], state.get("finished_at"))))

    run_id = state["run_id"]
    target_dir = os.path.join(workspace, "targets", state["target"])
    ck.add("Target_Codebase directory still exists", os.path.isdir(target_dir), target_dir)

    # ---------- 3 gate / evidence liveness confirmation ----------
    gate_shas = {r.get("sha256") for r in gate if isinstance(r, dict)
                 and r.get("run_id") == run_id and isinstance(r.get("sha256"), str)}
    ck.add("gate-live.jsonl contains this run's Canary records", bool(gate_shas),
           "%d records" % len(gate_shas))
    canary_recs = [r for r in rejections if r.get("sha256") in gate_shas]
    ck.add("Rejection_Log contains Canary rejection records (CITATION_MISSING only)",
           bool(canary_recs) and all(r.get("reason_codes") == ["CITATION_MISSING"]
                                      for r in canary_recs),
           "%d records" % len(canary_recs))
    ck.add("evidence.jsonl is non-empty", len(entries) > 0, "%d entries" % len(entries))
    ck.add("all evidence.jsonl entries share the run's run_id",
           all(e["run_id"] == run_id for e in entries))

    # ---------- 4 ledger ----------
    ledger_ok = True
    for n, rec in enumerate(ledger, 1):
        if not isinstance(rec, dict) or rec.get("finding_id") != g.finding_id(n) \
                or not isinstance(rec.get("canonical"), dict):
            ledger_ok = False
            break
        finding, _ = g.parse_submission(g.reserialize_canonical_obj(rec["canonical"]))
        if finding is None or g.canonical_form(finding) != \
                g.reserialize_canonical_obj(rec["canonical"]) \
                or finding["run_id"] != run_id:
            ledger_ok = False
            break
    ck.add("Findings_Ledger: IDs sequential, canonical a valid Canonical_Form, "
           "run_id consistent", ledger_ok, "%d records" % len(ledger))
    if not ledger_ok:
        return ck

    # ---------- 5 recheck split: findings list / invalidated list ----------
    expected_pass = []
    expected_inv = []
    for rec in ledger:
        try:
            failures = rr.recheck_finding(rec["canonical"], workspace,
                                          state["target"], entries)
        except rr.Renderer_Error:
            failures = [{"reason_code": "REPORT_FAILED"}]
        if failures:
            expected_inv.append({"finding_id": rec["finding_id"], "failures": failures})
        else:
            expected_pass.append(rec)
    expected_pass.sort(key=lambda r: rr.SEVERITY_ORDER.get(
        r["canonical"].get("severity"), len(g.SEVERITIES)))  # stable sort: ties keep acceptance order

    findings = report.get("findings")
    invalidated = report.get("invalidated")
    if not ck.add("report.json contains findings / invalidated arrays",
                  isinstance(findings, list) and isinstance(invalidated, list)):
        return ck
    got_ids = [f.get("finding_id") for f in findings]
    exp_ids = [r["finding_id"] for r in expected_pass]
    ck.add("findings list == ledger records that passed the recheck (including order)",
           got_ids == exp_ids,
           "report=%s expected=%s" % (got_ids, exp_ids))
    ck.add("invalidated list == ledger records that failed the recheck "
           "(with Reason_Code and citation index/field path)",
           invalidated == expected_inv,
           "report=%s expected=%s" % (
               [i.get("finding_id") for i in invalidated],
               [i["finding_id"] for i in expected_inv]))
    all_ids = set(got_ids) | {i.get("finding_id") for i in invalidated}
    ck.add("findings ∪ invalidated == all ledger Finding_IDs, pairwise disjoint",
           all_ids == {r["finding_id"] for r in ledger}
           and len(got_ids) + len(invalidated) == len(ledger))

    # ---------- 6 Canonical_Form byte-identical (R13.13) ----------
    by_id = {r["finding_id"]: r for r in ledger}
    canon_bad = []
    for f in findings:
        rec = by_id.get(f.get("finding_id"))
        if rec is None:
            canon_bad.append(f.get("finding_id"))
            continue
        obj = {"run_id": run_id, "agent": f.get("agent"), "title": f.get("title"),
               "category": f.get("category"), "severity": f.get("severity"),
               "description": f.get("description"),
               "citations": [{k: c.get(k) for k in g.CITATION_KEYS}
                             for c in f.get("citations") or []]}
        parsed, _ = g.parse_submission(json.dumps(obj, ensure_ascii=False))
        if parsed is None or g.canonical_form(parsed) != \
                g.reserialize_canonical_obj(rec["canonical"]):
            canon_bad.append(f.get("finding_id"))
    ck.add("each finding's Canonical_Form is byte-identical to the ledger's",
           not canon_bad,
           "mismatched: %s" % canon_bad if canon_bad else "")

    # ---------- 7 severity ordering (R13.4) ----------
    ranks = [rr.SEVERITY_ORDER.get(f.get("severity"), 99) for f in findings]
    serials = [int(f.get("finding_id", "F-0")[2:] or 0) for f in findings]
    order_ok = all(
        ranks[i] < ranks[i + 1] or (ranks[i] == ranks[i + 1] and serials[i] < serials[i + 1])
        for i in range(len(findings) - 1))
    ck.add("findings list sorted critical→info, ties in acceptance order", order_ok)

    # ---------- 8 statistics (R13.7) ----------
    stats = report.get("statistics") or {}
    exp_sev = {s: 0 for s in g.SEVERITIES}
    exp_cat = {c: 0 for c in g.CATEGORIES}
    for r in expected_pass:
        exp_sev[r["canonical"]["severity"]] += 1
        exp_cat[r["canonical"]["category"]] += 1
    ck.add("statistics.by_severity (all 5 tiers) == recomputed",
           stats.get("by_severity") == exp_sev,
           "%s vs %s" % (stats.get("by_severity"), exp_sev))
    ck.add("statistics.by_category (all 5 tiers) == recomputed",
           stats.get("by_category") == exp_cat,
           "%s vs %s" % (stats.get("by_category"), exp_cat))
    ck.add("statistics.invalidated_count == invalidated list length",
           stats.get("invalidated_count") == len(expected_inv))
    exp_rej = rr.rejected_statistics(rejections, gate_shas)
    ck.add("statistics.rejected == recomputed from the Rejection_Log "
           "(Canary sha256 excluded)",
           stats.get("rejected") == exp_rej,
           "%s vs %s" % (stats.get("rejected"), exp_rej))
    ck.add("rejected total == Rejection_Log record count − Canary record count",
           exp_rej["total"] == len(rejections) - len(canary_recs))
    # report.md statistic lines: severity/category lines are language-neutral;
    # the last two lines have a current English and a legacy Chinese rendering,
    # and either satisfies the consistency check (older-report compatibility).
    neutral_lines = (["  - %s: %d" % (s, exp_sev[s]) for s in g.SEVERITIES]
                     + ["  - %s: %d" % (c, exp_cat[c]) for c in g.CATEGORIES])
    stat_line_pairs = [
        ("- Invalidated Accepted_Finding count: %d" % len(expected_inv),
         "- 失效 Accepted_Finding 数: %d" % len(expected_inv)),
        ("- Rejected Submission total (Canary records excluded): %d" % exp_rej["total"],
         "- 被拒 Submission 总数（剔除 Canary 记录）: %d" % exp_rej["total"]),
    ]
    md_lines = md.split("\n")
    missing = ([l for l in neutral_lines if l not in md_lines]
               + [cur for cur, legacy in stat_line_pairs
                  if cur not in md_lines and legacy not in md_lines])
    ck.add("report.md statistic lines match the recomputed values", not missing,
           "missing: %s" % missing if missing else "")

    # ---------- 9 coverage data (R13.8/R13.21) ----------
    exp_cov = rr.coverage_data(workspace, state["target"], entries)
    ck.add("coverage == recomputed from Target + Evidence_Log",
           report.get("coverage") == exp_cov,
           "%s vs %s" % (report.get("coverage"), exp_cov))
    # Same dual rendering as the statistic lines above (older-report compatibility).
    cov_line_pairs = [
        ("- Target regular file count: %d" % exp_cov["target_regular_file_count"],
         "- Target 常规文件总数: %d" % exp_cov["target_regular_file_count"]),
        ("- Distinct file paths in Evidence_Log: %d" % exp_cov["evidence_file_count"],
         "- Evidence_Log 不同文件路径数: %d" % exp_cov["evidence_file_count"]),
        ("- Files whose relative path cannot form a valid Location: %d"
         % exp_cov["unlocatable_file_count"],
         "- 相对路径无法写成合法 Location 的文件数: %d"
         % exp_cov["unlocatable_file_count"]),
    ]
    ck.add("report.md coverage lines match the recomputed values",
           all(cur in md_lines or legacy in md_lines for cur, legacy in cov_line_pairs))

    # ---------- 10 the four Agents' Completion_Status (R13.6) ----------
    agents = run.get("agents")
    agents_ok = isinstance(agents, dict) and set(agents) == set(g.AUDIT_AGENTS) \
        and all(v in AGENT_STATUS_VALUES for v in agents.values())
    ck.add("run.agents contains exactly the four Audit_Agents, values in "
           "completed/failed/not_dispatched",
           agents_ok, json.dumps(agents, ensure_ascii=False))
    if agents_ok:
        ck.add("report.md agent status table matches report.json",
               all("| %s | %s |" % (a, agents[a]) in md_lines for a in g.AUDIT_AGENTS))
        fin = g.strict_json_loads(g.read_text(
            os.path.join(workspace, "control", "finalize.json")) or "")
        if isinstance(fin, dict) and fin.get("run_id") == run_id \
                and isinstance(fin.get("completion"), dict):
            fin_map = fin["completion"]
            # A key absent from an older finalize.json may surface as either
            # dispatch status in the report ("not_dispatched" now, "未派发"
            # previously); only an explicit value must match exactly.
            agents_match = all(
                fin_map.get(a, "not_dispatched") == agents[a]
                or (a not in fin_map and agents[a] in NOT_DISPATCHED)
                for a in g.AUDIT_AGENTS)
            ck.add("run.agents matches control/finalize.json completion",
                   agents_match, "%s vs %s" % (agents, fin_map))
        if require_all_dispatched:
            nd = [a for a in g.AUDIT_AGENTS if agents[a] in NOT_DISPATCHED]
            ck.add("all four Audit_Agents dispatched (--require-all-dispatched)", not nd,
                   "not dispatched: %s" % nd if nd else "")

    # ---------- 11 Citations: file:line + timestamp present in evidence.jsonl ----------
    cit_bad = []
    n_cits = 0
    for f in findings:
        cits = f.get("citations") or []
        if not cits:
            cit_bad.append("%s: no Citation" % f.get("finding_id"))
        for i, c in enumerate(cits, 1):
            n_cits += 1
            tag = "%s citations[%d]" % (f.get("finding_id"), i)
            loc, err = g.parse_location(c.get("location"))
            if err is not None:
                cit_bad.append(tag + ": Location invalid")
                continue
            if (c.get("path"), c.get("start"), c.get("end")) != \
                    (loc["path"], loc["start"], loc["end"]):
                cit_bad.append(tag + ": path/start/end differ from the Location")
            ts = c.get("evidence_timestamp")
            if g.parse_ts(ts) is None:
                cit_bad.append(tag + ": Evidence_Timestamp invalid")
                continue
            match = [e for e in entries if e["ts"] == ts]
            if len(match) != 1:
                cit_bad.append(tag + ": timestamp not found in evidence.jsonl")
                continue
            e = match[0]
            if e["path"] != loc["path"] or not (e["start"] <= loc["start"]
                                                and loc["end"] <= e["end"]):
                cit_bad.append(tag + ": the evidence entry does not cover this Location")
    ck.add("each Citation has a valid file:line and a timestamp matching an "
           "Evidence_Entry that covers it",
           not cit_bad, "; ".join(cit_bad) if cit_bad else "%d Citations" % n_cits)

    # ---------- 12 Finding_ID consistency between report.md and report.json ----------
    md_heading_ids = [l[4:12] for l in md_lines
                      if l.startswith("### F-") and g.FINDING_ID_RE.match(l[4:12])]
    ck.add("report.md finding heading IDs (with order) == report.json findings list",
           md_heading_ids == got_ids, "%s vs %s" % (md_heading_ids, got_ids))
    cit_lines_missing = []
    for f in findings:
        for i, c in enumerate(f.get("citations") or [], 1):
            line = "- Citation %d: %s @ %s" % (i, rr.c(c.get("location")),
                                             c.get("evidence_timestamp"))
            if line not in md_lines:
                cit_lines_missing.append("%s#%d" % (f.get("finding_id"), i))
    ck.add("report.md lists each Citation's Location and Evidence_Timestamp",
           not cit_lines_missing, "missing: %s" % cit_lines_missing if cit_lines_missing else "")
    # Section headers: current English, with the legacy Chinese headers accepted
    # when verifying older reports.
    inv_head = "## Invalidated" if "## Invalidated" in md else "## 失效列表"
    cov_head = "## Coverage" if "## Coverage" in md else "## 覆盖数据"
    md_inv = md.split(inv_head, 1)[1].split(cov_head, 1)[0] \
        if inv_head in md and cov_head in md else ""
    md_inv_ids = [l[2:10] for l in md_inv.split("\n") if l.startswith("- F-")]
    ck.add("report.md invalidated IDs == report.json invalidated list",
           md_inv_ids == [i.get("finding_id") for i in invalidated],
           "%s" % md_inv_ids)

    # ---------- 13 summary and notes ----------
    summary = report.get("summary") or []
    ck.add("summary-referenced Finding_IDs all appear in the findings list",
           all(set(s.get("finding_ids") or []) <= set(got_ids) and s.get("finding_ids")
               for s in summary), "%d entries" % len(summary))
    notes = report.get("notes") or {}
    ck.add("notes.evidence_caveat and the report.md notes section exist (R13.20)",
           notes.get("evidence_caveat") == rr.EVIDENCE_CAVEAT and rr.EVIDENCE_CAVEAT in md)
    ck.add("empty findings list ⇔ disclaimer present (R13.9)",
           (not findings) == bool(notes.get("empty_findings_disclaimer"))
           and (not findings) == (rr.EMPTY_FINDINGS_DISCLAIMER in md))
    return ck


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="cross-check the report and ledger of a completed Audit_Run")
    ap.add_argument("run_dir", help="runs/<run_id> directory")
    ap.add_argument("--workspace", help="workspace root (default: two levels above run_dir)")
    ap.add_argument("--require-all-dispatched", action="store_true",
                    help="require that none of the four Audit_Agents is \"not_dispatched\"")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.run_dir):
        sys.stderr.write("not a directory: %s\n" % args.run_dir)
        return 2
    ck = verify(args.run_dir, args.workspace, args.require_all_dispatched)
    for name, ok, detail in ck.results:
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name,
                            ("  — " + detail) if detail else ""))
    n_fail = len(ck.failed)
    print("")
    print("RESULT: %s (%d checks, %d FAIL) run_dir=%s" % (
        "PASS" if n_fail == 0 else "FAIL", len(ck.results), n_fail,
        os.path.abspath(args.run_dir)))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
