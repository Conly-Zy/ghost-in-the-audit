#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Report_Renderer — generates the Audit_Report (design 5.5).

Called by Run_Controller inside the Finalize transaction (holding L_R exclusive):

    report_renderer.render_and_write(run_dir, cwd, state, finalize_body,
                                     finished_at, deadline)

Flow: parse the four artifacts → recheck each Accepted_Finding (R7.6–R7.16,
order and short-circuiting as in R7.20) → sort → validate Summary_Statements
(R13.11) → statistics → overwrite data → write `report.*.tmp` fully to disk
first, then os.replace to the final names.
On failure (including deadline expiry) raises Renderer_Error → the controller
cleans up tmp and the status is unchanged (R13.14/R13.17).
"""

import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ghostlib as g  # noqa: E402

SEVERITY_ORDER = {s: i for i, s in enumerate(g.SEVERITIES)}
EVIDENCE_CAVEAT = ("Each Citation only proves that the cited code was indeed read and still "
                   "matches the Target_Codebase content when the Audit_Report was "
                   "generated; it does not prove that the Finding's conclusion is "
                   "semantically correct.")
EMPTY_FINDINGS_DISCLAIMER = "An empty findings list does not prove the Target_Codebase has no defects."


class Renderer_Error(Exception):
    def __init__(self, reason_code, lines):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.lines = lines


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise Renderer_Error("REPORT_FAILED", [g.rejection_line("REPORT_FAILED")])


# ============================================================
# Artifact parsing
# ============================================================

def parse_artifacts(run_dir):
    """Parse the four artifacts; any unreadable/unparseable artifact → REPORT_FAILED (R13.14)."""
    def _read(name):
        text = g.read_text(os.path.join(run_dir, name))
        if text is None:
            raise Renderer_Error("REPORT_FAILED",
                                 [g.rejection_line("REPORT_FAILED", name)])
        return text

    entries, corrupt = g.parse_evidence_log(_read("evidence.jsonl"))
    if entries is None:
        raise Renderer_Error("REPORT_FAILED",
                             [g.rejection_line("REPORT_FAILED", "evidence.jsonl")])

    ledger = []
    for line in _read("findings.jsonl").split("\n"):
        if line == "":
            continue
        rec = g.strict_json_loads(line)
        if not isinstance(rec, dict) or not isinstance(rec.get("canonical"), dict) \
                or not isinstance(rec.get("finding_id"), str):
            raise Renderer_Error("REPORT_FAILED",
                                 [g.rejection_line("REPORT_FAILED", "findings.jsonl")])
        ledger.append(rec)

    rejections = []
    for line in _read("rejections.jsonl").split("\n"):
        if line == "":
            continue
        rec = g.strict_json_loads(line)
        if not isinstance(rec, dict) or not isinstance(rec.get("reason_codes"), list) \
                or not isinstance(rec.get("sha256"), str):
            raise Renderer_Error("REPORT_FAILED",
                                 [g.rejection_line("REPORT_FAILED", "rejections.jsonl")])
        rejections.append(rec)

    gate_shas = set()
    gt = g.read_text(os.path.join(run_dir, "gate-live.jsonl"))
    if gt is not None:
        for line in gt.split("\n"):
            if line == "":
                continue
            rec = g.strict_json_loads(line)
            if isinstance(rec, dict) and isinstance(rec.get("sha256"), str):
                gate_shas.add(rec["sha256"])
    return entries, ledger, rejections, gate_shas


# ============================================================
# Recheck (R13.2/R13.3)
# ============================================================

def recheck_finding(canonical_obj, cwd, target_name, entries):
    """Re-run R7.6–R7.16 for a single Accepted_Finding; returns the list of invalidated items.

    Each item: {"reason_code", "citation_index"?, "field_path"?}.
    """
    finding = {
        "run_id": canonical_obj.get("run_id"),
        "agent": canonical_obj.get("agent"),
        "title": canonical_obj.get("title"),
        "category": canonical_obj.get("category"),
        "severity": canonical_obj.get("severity"),
        "description": canonical_obj.get("description"),
        "citations": canonical_obj.get("citations"),
    }
    if any(finding[k] is None for k in
           ("run_id", "agent", "title", "category", "severity", "description")) \
            or not isinstance(finding["citations"], list):
        raise Renderer_Error("REPORT_FAILED",
                             [g.rejection_line("REPORT_FAILED", "canonical")])
    target_root = os.path.join(cwd, "targets", target_name)
    failures = []
    lines_cache = {}
    for i, cit in enumerate(finding["citations"], 1):
        if not isinstance(cit, dict):
            failures.append({"reason_code": "SCHEMA_INVALID", "citation_index": i})
            continue
        code = g.check_citation(cit.get("location"), cit.get("evidence_timestamp"),
                                cit.get("excerpt"), target_root, entries, lines_cache)
        if code is not None:
            failures.append({"reason_code": code, "citation_index": i})
    for ref in g.uncited_location_refs(finding):
        failures.append({"reason_code": "UNCITED_LOCATION",
                         "field_path": g.format_location(ref)})
    return failures


# ============================================================
# Summary_Statement validation (R13.11)
# ============================================================

def validate_summaries(statements, findings_by_id):
    """→ list of failed statement ordinals (1-based)."""
    failed = []
    for idx, stmt in enumerate(statements, 1):
        bad = False
        if idx > g.MAX_SUMMARY_STATEMENTS:
            bad = True
        if len(stmt) > g.MAX_SUMMARY_STATEMENT_LEN:
            bad = True
        ids = g.SUMMARY_FINDING_ID_RE.findall(stmt)
        if not ids:
            bad = True
        referenced = []
        for fid in dict.fromkeys(ids):
            f = findings_by_id.get(fid)
            if f is None:
                bad = True       # referenced a Finding_ID outside the findings list
            else:
                referenced.append(f)
        for ref in g.extract_location_references(stmt):
            covered = any(
                loc["path"] == ref["path"] and loc["start"] <= ref["start"]
                and ref["end"] <= loc["end"]
                for f in referenced
                for c in f["citations"]
                for loc in [g.parse_location(c["location"])[0]]
                if loc is not None)
            if not covered:
                bad = True       # no Citation in any referenced Finding covers this Location_Reference
        if bad:
            failed.append(idx)
    return failed


# ============================================================
# Statistics (R13.7) and coverage data (R13.8/R13.21)
# ============================================================

def rejected_statistics(rejections, gate_shas):
    total = 0
    by_code = {}
    by_agent = {}
    for rec in rejections:
        if rec.get("sha256") in gate_shas:
            continue  # exclude the Canary record (13#7)
        total += 1
        for code in rec.get("reason_codes") or []:
            by_code[code] = by_code.get(code, 0) + 1
        agent = rec.get("agent")
        if agent not in g.AUDIT_AGENTS:
            agent = "unknown"
        by_agent[agent] = by_agent.get(agent, 0) + 1
    return {"total": total, "by_reason_code": by_code, "by_agent": by_agent}


def coverage_data(cwd, target_name, entries):
    """Single os.walk: total regular files, files that cannot form a valid Location, evidence file paths."""
    target_dir = os.path.join(cwd, "targets", target_name)
    total = 0
    unlocatable = []
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, target_dir).replace(os.sep, "/")
            total += 1
            loc_str = rel + ":1"
            _, err = g.parse_location(loc_str)
            if err is not None or len(loc_str) > g.LOCATION_MAX \
                    or len(loc_str.encode("utf-8")) > 4096 + 2:
                unlocatable.append(rel)
    unlocatable.sort()
    evidence_paths = sorted({e["path"] for e in entries})
    return {
        "evidence_file_count": len(evidence_paths),
        "evidence_paths": evidence_paths,
        "target_regular_file_count": total,
        "unlocatable_file_count": len(unlocatable),
        "unlocatable_paths": unlocatable,
    }


# ============================================================
# Markdown rendering (R13.10: all external text goes through md_escape into code spans / fences)
# ============================================================

def c(text):
    """code span: escaped, then wrapped in backticks."""
    return "`" + g.md_escape(str(text)) + "`"


def render_markdown(doc):
    L = []
    run = doc["run"]
    L.append("# Audit Report " + c(run["run_id"]))
    L.append("")
    L.append("## Metadata")
    L.append("")
    L.append("- Run: " + c(run["run_id"]))
    L.append("- Target: " + c(run["target"]))
    L.append("- Started: " + c(run["started_at"]))
    L.append("- Finished: " + c(run["finished_at"]))
    L.append("")
    L.append("| Agent | Completion_Status |")
    L.append("| --- | --- |")
    for agent in g.AUDIT_AGENTS:
        L.append("| " + agent + " | " + str(run["agents"][agent]) + " |")
    L.append("")
    L.append("## Summary")
    L.append("")
    if doc["summary"]:
        for s in doc["summary"]:
            L.append("- " + str(s["index"]) + ": " + c(s["statement"])
                     + " (" + ", ".join(s["finding_ids"]) + ")")
    else:
        L.append("(no Summary_Statement)")
    L.append("")
    L.append("## Statistics")
    L.append("")
    L.append("- By severity:")
    for sev in g.SEVERITIES:
        L.append("  - " + sev + ": " + str(doc["statistics"]["by_severity"][sev]))
    L.append("- By category:")
    for cat in g.CATEGORIES:
        L.append("  - " + cat + ": " + str(doc["statistics"]["by_category"][cat]))
    L.append("- Invalidated Accepted_Finding count: "
             + str(doc["statistics"]["invalidated_count"]))
    rej = doc["statistics"]["rejected"]
    L.append("- Rejected Submission total (Canary records excluded): " + str(rej["total"]))
    for code in sorted(rej["by_reason_code"]):
        L.append("  - " + code + ": " + str(rej["by_reason_code"][code]))
    for agent in sorted(rej["by_agent"]):
        L.append("  - agent " + agent + ": " + str(rej["by_agent"][agent]))
    L.append("")
    L.append("## Findings")
    L.append("")
    if not doc["findings"]:
        L.append("(empty)")
    for f in doc["findings"]:
        L.append("")
        L.append("### " + f["finding_id"] + " " + c(f["title"]))
        L.append("")
        L.append("- severity: " + f["severity"] + " / category: " + f["category"]
                 + " / agent(self-reported): " + f["agent"])
        L.append("- description: " + c(f["description"]))
        for i, cit in enumerate(f["citations"], 1):
            L.append("- Citation " + str(i) + ": " + c(cit["location"])
                     + " @ " + cit["evidence_timestamp"])
            L.append("")
            L.append("```text")
            L.append(g.md_escape(cit["excerpt"]))
            L.append("```")
    L.append("")
    L.append("## Invalidated")
    L.append("")
    if not doc["invalidated"]:
        L.append("(empty)")
    for inv in doc["invalidated"]:
        parts = []
        for fail in inv["failures"]:
            part = fail["reason_code"]
            if "citation_index" in fail:
                part += " citations[" + str(fail["citation_index"]) + "]"
            if "field_path" in fail:
                part += " " + fail["field_path"]
            parts.append(part)
        L.append("- " + inv["finding_id"] + ": " + c("; ".join(parts)))
    L.append("")
    L.append("## Coverage")
    L.append("")
    cov = doc["coverage"]
    L.append("- Target regular file count: " + str(cov["target_regular_file_count"]))
    L.append("- Files whose relative path cannot form a valid Location: "
             + str(cov["unlocatable_file_count"]))
    for p in cov["unlocatable_paths"]:
        L.append("  - " + c(p))
    L.append("- Distinct file paths in Evidence_Log: " + str(cov["evidence_file_count"]))
    for p in cov["evidence_paths"]:
        L.append("  - " + c(p))
    if not doc["findings"]:
        L.append("")
        L.append(EMPTY_FINDINGS_DISCLAIMER)  # R13.9
    L.append("")
    L.append("## Notes")
    L.append("")
    L.append(EVIDENCE_CAVEAT)  # R13.20
    L.append("")
    return "\n".join(L)


# ============================================================
# Main entry
# ============================================================

def render_and_write(run_dir, cwd, state, finalize_body, finished_at, deadline):
    check_deadline(deadline)
    entries, ledger, rejections, gate_shas = parse_artifacts(run_dir)
    check_deadline(deadline)

    target_name = state["target"]
    findings_pass = []
    invalidated = []
    for rec in ledger:
        failures = recheck_finding(rec["canonical"], cwd, target_name, entries)
        if failures:
            invalidated.append({"finding_id": rec["finding_id"], "failures": failures})
        else:
            findings_pass.append(rec)
        check_deadline(deadline)

    # Findings list sort: severity critical→info, ties in acceptance order (R13.4)
    findings_pass.sort(key=lambda r: (SEVERITY_ORDER.get(
        r["canonical"].get("severity"), len(g.SEVERITIES)),))

    findings_by_id = {r["finding_id"]: r["canonical"] for r in findings_pass}

    # Summary validation (R13.11)
    statements = finalize_body.get("summary_statements") or []
    failed_idx = validate_summaries(statements, findings_by_id)
    if failed_idx:
        lines = [g.rejection_line("SUMMARY_UNSUPPORTED", str(i)) for i in failed_idx]
        raise Renderer_Error("SUMMARY_UNSUPPORTED", lines)
    check_deadline(deadline)

    summary = []
    for idx, stmt in enumerate(statements, 1):
        summary.append({"index": idx, "statement": stmt,
                        "finding_ids": list(dict.fromkeys(
                            g.SUMMARY_FINDING_ID_RE.findall(stmt)))})

    completion = finalize_body.get("completion") or {}
    agents = {}
    for agent in g.AUDIT_AGENTS:
        agents[agent] = completion.get(agent, "not_dispatched")

    by_sev = {s: 0 for s in g.SEVERITIES}
    by_cat = {c_: 0 for c_ in g.CATEGORIES}
    for r in findings_pass:
        sev = r["canonical"].get("severity")
        cat = r["canonical"].get("category")
        if sev in by_sev:
            by_sev[sev] += 1
        if cat in by_cat:
            by_cat[cat] += 1

    doc = {
        "schema_version": 1,
        "run": {
            "run_id": state["run_id"],
            "target": target_name,
            "started_at": state["started_at"],
            "finished_at": finished_at,
            "agents": agents,
        },
        "summary": summary,
        "findings": [
            {
                "finding_id": r["finding_id"],
                "agent": r["canonical"].get("agent"),
                "category": r["canonical"].get("category"),
                "severity": r["canonical"].get("severity"),
                "title": r["canonical"].get("title"),
                "description": r["canonical"].get("description"),
                "citations": [
                    {
                        "location": c["location"],
                        "evidence_timestamp": c["evidence_timestamp"],
                        "excerpt": c["excerpt"],
                        "path": loc["path"], "start": loc["start"], "end": loc["end"],
                    }
                    for c in r["canonical"].get("citations") or []
                    for loc in [g.parse_location(c["location"])[0]]
                    if loc is not None
                ],
            }
            for r in findings_pass
        ],
        "invalidated": invalidated,
        "statistics": {
            "by_severity": by_sev,
            "by_category": by_cat,
            "invalidated_count": len(invalidated),
            "rejected": rejected_statistics(rejections, gate_shas),
        },
        "coverage": coverage_data(cwd, target_name, entries),
        "notes": {
            "evidence_caveat": EVIDENCE_CAVEAT,
        },
    }
    if not findings_pass:
        doc["notes"]["empty_findings_disclaimer"] = True

    check_deadline(deadline)

    md_text = render_markdown(doc)
    json_text = json.dumps(doc, ensure_ascii=False, indent=1)

    # Ledger reread consistency (R13.14)
    ledger_text2 = g.read_text(os.path.join(run_dir, "findings.jsonl"))
    ledger2 = [l for l in (ledger_text2 or "").split("\n") if l != ""]
    if len(ledger2) != len(ledger):
        raise Renderer_Error("REPORT_FAILED",
                             [g.rejection_line("REPORT_FAILED", "findings.jsonl")])

    # Write tmp fully to disk first, then os.replace to the final names (R13.14/R13.17)
    tmp_md = os.path.join(run_dir, "report.md.tmp")
    tmp_json = os.path.join(run_dir, "report.json.tmp")
    try:
        with open(tmp_md, "wb") as f:
            f.write(md_text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        with open(tmp_json, "wb") as f:
            f.write(json_text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_md, os.path.join(run_dir, "report.md"))
        os.replace(tmp_json, os.path.join(run_dir, "report.json"))
    except OSError:
        for name in (tmp_md, tmp_json):
            try:
                os.unlink(name)
            except OSError:
                pass
        raise Renderer_Error("REPORT_FAILED",
                             [g.rejection_line("REPORT_FAILED")])


if __name__ == "__main__":
    sys.stderr.write("report_renderer is a library module (called by run_controller)\n")
    sys.exit(2)
