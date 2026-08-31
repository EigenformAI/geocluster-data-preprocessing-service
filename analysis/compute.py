"""Deterministic metrics over normalized records."""

from __future__ import annotations

from pathlib import Path

from pipeline.util import write_json

TROY_OZ_G = 31.1035
DEFAULT_CUTOFF_GPT = 0.3


def _v(record: dict, field: str):
    value = record.get(field)
    return value.get("value") if isinstance(value, dict) else None


def _conf(record: dict) -> str:
    return (record.get("provenance") or {}).get("confidence") or "high"


def _prov(record: dict) -> dict:
    return record.get("provenance") or {}


def intercepts(assays: list[dict], cutoff_gpt: float = DEFAULT_CUTOFF_GPT) -> dict:
    if not assays:
        return {"applicable": False, "reason": "no assay-interval tables"}
    rows = []
    for record in assays:
        from_m = _v(record, "from_m")
        to_m = _v(record, "to_m")
        interval_m = _v(record, "interval_m")
        au_gpt = _v(record, "au_gpt")
        length = interval_m if interval_m is not None else ((to_m - from_m) if from_m is not None and to_m is not None else None)
        gram_metre = length * au_gpt if length is not None and au_gpt is not None else None
        rows.append({
            "record_id": record.get("_id"),
            "hole_id": record.get("hole_id"),
            "zone": record.get("zone"),
            "from_m": from_m,
            "to_m": to_m,
            "interval_m": length,
            "au_gpt": au_gpt,
            "gram_metre": round(gram_metre, 3) if gram_metre is not None else None,
            "is_subinterval": bool(record.get("_is_subinterval")),
            "confidence": _conf(record),
            "provenance": _prov(record),
            "evidence_ids": list((record.get("_field_evidence") or {}).values()),
        })

    by_hole: dict[str | None, list[dict]] = {}
    for row in rows:
        by_hole.setdefault(row.get("hole_id"), []).append(row)

    violations = 0
    for row in rows:
        if not row["is_subinterval"]:
            row["contained_ok"] = None
            continue
        parents = [
            parent
            for parent in by_hole.get(row.get("hole_id"), [])
            if not parent["is_subinterval"]
            and parent.get("from_m") is not None
            and parent.get("to_m") is not None
            and row.get("from_m") is not None
            and row.get("to_m") is not None
            and parent["from_m"] - 0.01 <= row["from_m"]
            and row["to_m"] <= parent["to_m"] + 0.01
        ]
        row["contained_ok"] = bool(parents)
        if not parents:
            violations += 1

    parents = [row for row in rows if not row["is_subinterval"]]
    significant = [
        row for row in parents
        if row.get("au_gpt") is not None and row["au_gpt"] >= cutoff_gpt and row.get("interval_m") is not None
    ]
    significant.sort(key=lambda item: item.get("gram_metre") or 0, reverse=True)
    return {
        "applicable": True,
        "cutoff_gpt": cutoff_gpt,
        "formula": "gram_metre = interval_m * au_gpt",
        "n_intervals": len(rows),
        "n_parent_intervals": len(parents),
        "n_significant_at_cutoff": len(significant),
        "containment_violations": violations,
        "top_intercepts": significant[:20],
        "intervals": rows,
    }


def resources(resource_records: list[dict]) -> dict:
    if not resource_records:
        return {"applicable": False, "reason": "no resource tables (early-stage or not estimated)"}
    rows = []
    low_confidence_differences = 0
    for record in resource_records:
        tonnage = _v(record, "tonnage")
        grade = _v(record, "grade_gpt")
        contained = _v(record, "contained")
        recomputed = tonnage * grade / TROY_OZ_G / 1000.0 if tonnage is not None and grade is not None else None
        confidence = _conf(record)
        difference = None
        if recomputed is not None and contained is not None:
            difference = abs(recomputed - contained) <= max(1.0, 0.02 * contained)
        ok = difference if confidence == "high" else None
        if difference is False and confidence != "high":
            low_confidence_differences += 1
        rows.append({
            "record_id": record.get("_id"),
            "property": record.get("property"),
            "deposit": record.get("deposit"),
            "category": record.get("category"),
            "tonnage_t": tonnage,
            "grade_gpt": grade,
            "contained_reported_koz": contained,
            "contained_recomputed_koz": round(recomputed, 2) if recomputed is not None else None,
            "recompute_ok": ok,
            "is_subtotal": bool(record.get("_is_subtotal")),
            "confidence": confidence,
            "provenance": _prov(record),
            "evidence_ids": list((record.get("_field_evidence") or {}).values()),
        })
    mismatches = [row for row in rows if row["recompute_ok"] is False]
    return {
        "applicable": True,
        "formula": "contained_koz = tonnage_t * grade_gpt / 31.1035 / 1000",
        "n_rows": len(rows),
        "n_recompute_mismatches": len(mismatches),
        "n_low_confidence_recompute_differences": low_confidence_differences,
        "rows": rows,
    }


def _crm_verdict(bias_pct, pass_rate):
    if bias_pct is None and pass_rate is None:
        return "unknown"
    abs_bias = abs(bias_pct) if bias_pct is not None else None
    if abs_bias is not None and abs_bias <= 5 and (pass_rate is None or pass_rate >= 95):
        return "pass"
    if abs_bias is not None and abs_bias <= 10 and (pass_rate is None or pass_rate >= 90):
        return "marginal"
    return "fail"


def qaqc(crms: list[dict]) -> dict:
    if not crms:
        return {"applicable": False, "reason": "no QA/QC tables tabulated"}
    rows = []
    for record in crms:
        certified = _v(record, "certified_value")
        mean = _v(record, "mean_measured")
        submitted = _v(record, "n_submitted")
        failures = _v(record, "n_failures")
        pct = _v(record, "pct_passing")
        bias = (mean - certified) / certified * 100.0 if certified and mean is not None else None
        pass_rate = pct if pct is not None else ((submitted - failures) / submitted * 100.0 if submitted and failures is not None else None)
        confidence = _conf(record)
        verdict = _crm_verdict(bias, pass_rate) if confidence == "high" else "unknown"
        rows.append({
            "record_id": record.get("_id"),
            "crm_id": record.get("crm_id"),
            "certified": certified,
            "mean_measured": mean,
            "bias_pct": round(bias, 2) if bias is not None else None,
            "pass_rate_pct": round(pass_rate, 1) if pass_rate is not None else None,
            "verdict": verdict,
            "confidence": confidence,
            "provenance": _prov(record),
            "evidence_ids": list((record.get("_field_evidence") or {}).values()),
        })
    verdicts = [row["verdict"] for row in rows if row["verdict"] != "unknown"]
    overall = "fail" if "fail" in verdicts else ("marginal" if "marginal" in verdicts else ("pass" if verdicts else "unknown"))
    return {
        "applicable": True,
        "formula": "bias_pct = (mean_measured - certified)/certified * 100",
        "n_crms": len(rows),
        "overall": overall,
        "crms": rows,
    }


def tenure(records: list[dict]) -> dict:
    if not records:
        return {"applicable": False, "reason": "no tenure tables"}
    areas = [(_v(record, "area"), _prov(record)) for record in records if _v(record, "area") is not None]
    expiries = [record.get("expiry_date") for record in records if record.get("expiry_date")]
    total = round(sum(area for area, _ in areas), 2) if areas else None
    return {
        "applicable": True,
        "n_records": len(records),
        "total_area_ha": total,
        "n_with_area": len(areas),
        "expiry_dates": sorted(set(expiries))[:20],
    }


def compute_metrics(metadata: dict, metrics_dir: Path | None = None) -> dict:
    records = list(metadata.get("records", {}).values())
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record.get("_type", "other"), []).append(record)
    metrics = {
        "stage": {"stage": metadata.get("project_stage"), "record_counts": {key: len(value) for key, value in grouped.items() if value}},
        "intercepts": intercepts(grouped.get("assay_interval", [])),
        "resource": resources(grouped.get("resource_estimate", [])),
        "qaqc": qaqc(grouped.get("qaqc_crm", [])),
        "tenure": tenure(grouped.get("tenure", [])),
    }
    if metrics_dir:
        metrics_dir.mkdir(parents=True, exist_ok=True)
        write_json(metrics_dir / "_stage.json", metrics["stage"])
        for name in ("intercepts", "resource", "qaqc", "tenure"):
            write_json(metrics_dir / f"{name}.json", metrics[name])
    return metrics


def computed_metric_evidence(metrics: dict) -> dict[str, dict]:
    evidence: dict[str, dict] = {}
    for row in (metrics.get("intercepts") or {}).get("intervals", []):
        if row.get("gram_metre") is None or not row.get("record_id"):
            continue
        evidence_id = f"ev:metric:intercepts:{row['record_id']}:gram_metre"
        evidence[evidence_id] = {
            "element_id": (row.get("provenance") or {}).get("element_id"),
            "page": (row.get("provenance") or {}).get("page"),
            "source_kind": "computed_metric",
            "source_ref": "metrics/intercepts.json",
            "raw_token": str(row["gram_metre"]),
            "value": row["gram_metre"],
            "formula": "gram_metre = interval_m * au_gpt",
            "input_evidence_ids": row.get("evidence_ids", []),
        }
    for row in (metrics.get("resource") or {}).get("rows", []):
        if row.get("contained_recomputed_koz") is None or not row.get("record_id"):
            continue
        evidence_id = f"ev:metric:resource:{row['record_id']}:contained_recomputed_koz"
        evidence[evidence_id] = {
            "element_id": (row.get("provenance") or {}).get("element_id"),
            "page": (row.get("provenance") or {}).get("page"),
            "source_kind": "computed_metric",
            "source_ref": "metrics/resource.json",
            "raw_token": str(row["contained_recomputed_koz"]),
            "value": row["contained_recomputed_koz"],
            "formula": "contained_koz = tonnage_t * grade_gpt / 31.1035 / 1000",
            "input_evidence_ids": row.get("evidence_ids", []),
        }
    return evidence
