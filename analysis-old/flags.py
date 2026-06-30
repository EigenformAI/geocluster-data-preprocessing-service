"""Stage E DUE DILIGENCE: deterministic red-flag rules (a skeptic's checklist).

Pure functions over records + computed metrics + manifest + section text. Each
flag cites its evidence (table_id/page) and is conservative in phrasing.
"""

from __future__ import annotations

import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.util import read_json, write_json  # noqa: E402

from . import astate
from .apaths import APaths
from .source import Report

STAGE = "flags"
RULE_VERSION = "v1"

_HISTORICAL_RE = re.compile(
    r"non[- ]?(?:compliant|43-?101)|historic(?:al)? (?:estimate|resource)|pre[- ]?(?:ni )?43-?101",
    re.I,
)


def _prov_ev(rows: list[dict]) -> list[dict]:
    seen, ev = set(), []
    for r in rows:
        p = r.get("provenance") or {}
        key = (p.get("table_id"), p.get("page"))
        if p.get("table_id") and key not in seen:
            seen.add(key)
            ev.append({"table_id": p["table_id"], "page": p["page"]})
    return ev


def run(report: Report, apaths: APaths, force: bool = False) -> dict:
    m = apaths.metrics
    intercepts = read_json(m / "intercepts.json", default={}) or {}
    resource = read_json(m / "resource.json", default={}) or {}
    qaqc = read_json(m / "qaqc.json", default={}) or {}
    stage = (read_json(m / "_stage.json", default={}) or {}).get("stage")

    flags: list[dict] = []

    def add(fid, severity, category, message, evidence):
        flags.append({"id": fid, "severity": severity, "category": category,
                      "message": message, "evidence": evidence, "rule_version": RULE_VERSION})

    # 1. Missing QA/QC where assays/resources exist
    if not qaqc.get("applicable") and (intercepts.get("applicable") or resource.get("applicable")):
        add("missing_qaqc", "high", "qaqc",
            "No QA/QC (CRM/blank/duplicate) data is tabulated, yet assay/resource data is "
            "reported. Analytical quality cannot be verified.", [])

    # 2. QA/QC performance
    if qaqc.get("applicable") and qaqc.get("overall") in ("fail", "marginal"):
        bad = [c for c in qaqc.get("crms", []) if c["verdict"] in ("fail", "marginal")]
        add("qaqc_performance", "high" if qaqc["overall"] == "fail" else "medium", "qaqc",
            f"QA/QC overall '{qaqc['overall']}': "
            + "; ".join(f"{c['crm_id']} bias {c['bias_pct']}% pass {c['pass_rate_pct']}%" for c in bad[:6]),
            _prov_ev(bad))

    # 3. Headline figures resting on suspect (low/unverifiable) tables
    susp_res = [r for r in resource.get("rows", []) if r.get("confidence") in ("low", "unverifiable")]
    if susp_res:
        add("resource_on_suspect_table", "high", "data_quality",
            "Resource figures derive from tables not verified against the digital text layer "
            "(low/unverifiable confidence). Treat as unconfirmed.", _prov_ev(susp_res))
    susp_int = [r for r in intercepts.get("intervals", []) if r.get("confidence") in ("low", "unverifiable")]
    if susp_int:
        add("intercepts_on_suspect_table", "medium", "data_quality",
            "Some reported intercepts derive from low/unverifiable tables.", _prov_ev(susp_int))

    # 4. Resource recompute mismatches
    if resource.get("n_recompute_mismatches"):
        bad = [r for r in resource.get("rows", []) if r.get("recompute_ok") is False]
        add("resource_recompute_mismatch", "medium", "resource",
            f"{resource['n_recompute_mismatches']} resource row(s) where tonnes x grade does not "
            "reconcile to reported contained metal (±2%).", _prov_ev(bad))

    # 5. Resource asserted on thin drilling
    if resource.get("applicable") and intercepts.get("applicable") \
            and intercepts.get("n_parent_intervals", 0) < 20:
        add("sparse_drilling_for_resource", "medium", "resource",
            f"Resource is reported but only {intercepts.get('n_parent_intervals', 0)} drill intercepts "
            "were captured — drill support for the classification may be thin (verify against the "
            "full drillhole database).", [])

    # 6. Sub-interval containment violations (possible mis-extraction)
    if intercepts.get("containment_violations"):
        add("intercept_containment", "low", "data_quality",
            f"{intercepts['containment_violations']} sub-interval(s) are not contained within a parent "
            "interval — check extraction/true-width reporting.", [])

    # 7. Reliance on non-43-101 historical estimates (text scan)
    for s in report.sections:
        if _HISTORICAL_RE.search(report.section_text(s)):
            add("historical_reliance", "medium", "disclosure",
                f"Section '{s.get('title')}' references historical / non-43-101 estimates — confirm "
                "they are not relied upon for current resources.",
                [{"table_id": None, "page": s.get("page_start")}])
            break

    write_json(apaths.flags_json, {"stage": stage, "flags": flags})
    info = {"n_flags": len(flags), "n_high": sum(1 for f in flags if f["severity"] == "high")}
    astate.mark_done(apaths, STAGE, info)
    return info
