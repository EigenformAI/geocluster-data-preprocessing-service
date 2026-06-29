"""Stage B COMPUTE: deterministic, cited geological metrics. No LLM.

Every output carries the provenance of its inputs and the formula used. Metrics
files record `applicable` + `reason` so Stage F can tell 'skipped by design'
(early-stage, no resource) from 'missing'.
"""

from __future__ import annotations

from . import astate, report_meta
from .apaths import APaths

STAGE = "compute"
CUTOFF_GPT = 0.3  # default significant-intercept cutoff for Au (g/t)
TROY_OZ_G = 31.1035


def _v(rec: dict, field: str):
    """Normalized numeric value of a record field (or None)."""
    cell = rec.get(field)
    return cell.get("value") if isinstance(cell, dict) else None


def _conf(rec: dict) -> str:
    return (rec.get("provenance") or {}).get("confidence") or "high"


def _prov(rec: dict) -> dict:
    return rec.get("provenance") or {}


# ---- intercepts -------------------------------------------------------------

def intercepts(assays: list[dict]) -> dict:
    if not assays:
        return {"applicable": False, "reason": "no assay-interval tables"}
    rows = []
    for r in assays:
        fr, to, intv, au = _v(r, "from_m"), _v(r, "to_m"), _v(r, "interval_m"), _v(r, "au_gpt")
        length = intv if intv is not None else ((to - fr) if (fr is not None and to is not None) else None)
        gm = (length * au) if (length is not None and au is not None) else None
        rows.append({
            "hole_id": r.get("hole_id"), "zone": r.get("zone"),
            "from_m": fr, "to_m": to, "interval_m": length, "au_gpt": au,
            "gram_metre": round(gm, 3) if gm is not None else None,
            "is_subinterval": bool(r.get("_is_subinterval")),
            "confidence": _conf(r), "provenance": _prov(r),
        })

    # containment check: each sub-interval must lie within a parent of the same hole
    by_hole: dict[str, list[dict]] = {}
    for x in rows:
        by_hole.setdefault(x["hole_id"], []).append(x)
    violations = 0
    for x in rows:
        if not x["is_subinterval"]:
            x["contained_ok"] = None
            continue
        parents = [p for p in by_hole.get(x["hole_id"], [])
                   if not p["is_subinterval"] and p["from_m"] is not None and p["to_m"] is not None
                   and x["from_m"] is not None and x["to_m"] is not None
                   and p["from_m"] - 0.01 <= x["from_m"] and x["to_m"] <= p["to_m"] + 0.01]
        x["contained_ok"] = bool(parents)
        if not parents:
            violations += 1

    parents = [x for x in rows if not x["is_subinterval"]]
    significant = [x for x in parents if x["au_gpt"] is not None and x["au_gpt"] >= CUTOFF_GPT
                   and x["interval_m"] is not None]
    significant.sort(key=lambda x: (x["gram_metre"] or 0), reverse=True)
    return {
        "applicable": True, "cutoff_gpt": CUTOFF_GPT, "formula": "gram_metre = interval_m * au_gpt",
        "n_intervals": len(rows), "n_parent_intervals": len(parents),
        "n_significant_at_cutoff": len(significant),
        "containment_violations": violations,
        "top_intercepts": significant[:20], "intervals": rows,
    }


# ---- resource ---------------------------------------------------------------

def resources(res: list[dict]) -> dict:
    if not res:
        return {"applicable": False, "reason": "no resource tables (early-stage or not estimated)"}
    rows = []
    for r in res:
        t, g, c = _v(r, "tonnage"), _v(r, "grade_gpt"), _v(r, "contained")
        recomp = (t * g / TROY_OZ_G / 1000.0) if (t is not None and g is not None) else None
        ok = None
        if recomp is not None and c is not None:
            ok = abs(recomp - c) <= max(1.0, 0.02 * c)
        rows.append({
            "property": r.get("property"), "deposit": r.get("deposit"),
            "category": r.get("category"), "tonnage_t": t, "grade_gpt": g,
            "contained_reported_koz": c,
            "contained_recomputed_koz": round(recomp, 2) if recomp is not None else None,
            "recompute_ok": ok, "is_subtotal": bool(r.get("_is_subtotal")),
            "confidence": _conf(r), "provenance": _prov(r),
        })
    mismatches = [x for x in rows if x["recompute_ok"] is False]
    return {
        "applicable": True,
        "formula": "contained_koz = tonnage_t * grade_gpt / 31.1035 / 1000",
        "n_rows": len(rows), "n_recompute_mismatches": len(mismatches),
        "rows": rows,
    }


# ---- QA/QC ------------------------------------------------------------------

def _crm_verdict(bias_pct, pass_rate):
    b = abs(bias_pct) if bias_pct is not None else None
    if b is not None and b <= 5 and (pass_rate is None or pass_rate >= 95):
        return "pass"
    if b is not None and b <= 10 and (pass_rate is None or pass_rate >= 90):
        return "marginal"
    return "fail"


def qaqc(crms: list[dict]) -> dict:
    if not crms:
        return {"applicable": False, "reason": "no QA/QC tables tabulated"}
    rows = []
    for r in crms:
        cert, mean = _v(r, "certified_value"), _v(r, "mean_measured")
        n_sub, n_fail, pct = _v(r, "n_submitted"), _v(r, "n_failures"), _v(r, "pct_passing")
        bias = ((mean - cert) / cert * 100.0) if (cert and mean is not None) else None
        pass_rate = pct if pct is not None else (
            ((n_sub - n_fail) / n_sub * 100.0) if (n_sub and n_fail is not None) else None)
        rows.append({
            "crm_id": r.get("crm_id"), "certified": cert, "mean_measured": mean,
            "bias_pct": round(bias, 2) if bias is not None else None,
            "pass_rate_pct": round(pass_rate, 1) if pass_rate is not None else None,
            "verdict": _crm_verdict(bias, pass_rate),
            "confidence": _conf(r), "provenance": _prov(r),
        })
    verdicts = [x["verdict"] for x in rows]
    overall = "fail" if "fail" in verdicts else ("marginal" if "marginal" in verdicts else "pass")
    return {
        "applicable": True, "formula": "bias_pct = (mean_measured - certified)/certified * 100",
        "n_crms": len(rows), "overall": overall, "crms": rows,
    }


# ---- tenure -----------------------------------------------------------------

def tenure(tens: list[dict]) -> dict:
    if not tens:
        return {"applicable": False, "reason": "no tenure tables"}
    areas = [(_v(r, "area"), _prov(r)) for r in tens if _v(r, "area") is not None]
    expiries = [r.get("expiry_date") for r in tens if r.get("expiry_date")]
    total = round(sum(a for a, _ in areas), 2) if areas else None
    return {
        "applicable": True, "n_records": len(tens),
        "total_area_ha": total, "n_with_area": len(areas),
        "expiry_dates": sorted(set(expiries))[:20],
    }


def run(apaths: APaths, force: bool = False) -> dict:
    from pipeline.util import write_json

    records = report_meta.load_records(apaths)
    groups = report_meta.by_type(records)
    stage = report_meta.project_stage(groups)

    out = {
        "stage": stage,
        "intercepts": intercepts(groups.get("assay_interval", [])),
        "resource": resources(groups.get("resource_estimate", [])),
        "qaqc": qaqc(groups.get("qaqc_crm", [])),
        "tenure": tenure(groups.get("tenure", [])),
    }
    for name, data in out.items():
        if name != "stage":
            write_json(apaths.metrics / f"{name}.json", data)
    write_json(apaths.metrics / "_stage.json", stage)

    info = {
        "project_stage": stage["stage"],
        "n_intervals": out["intercepts"].get("n_intervals", 0),
        "n_resource_rows": out["resource"].get("n_rows", 0),
        "qaqc_overall": out["qaqc"].get("overall", "absent"),
    }
    astate.mark_done(apaths, STAGE, info)
    return info
