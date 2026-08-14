"""Deterministic red flags over metrics and metadata."""

from __future__ import annotations


def _prov_ev(rows: list[dict]) -> list[dict]:
    seen = set()
    evidence = []
    for row in rows:
        provenance = row.get("provenance") or {}
        key = (provenance.get("element_id") or provenance.get("table_id"), provenance.get("page"))
        if key[0] and key not in seen:
            seen.add(key)
            evidence.append({"element_id": key[0], "page": key[1]})
    return evidence


def compute_flags(metadata: dict, metrics: dict) -> dict:
    intercepts = metrics.get("intercepts", {})
    resource = metrics.get("resource", {})
    qaqc = metrics.get("qaqc", {})
    flags: list[dict] = []
    elements = metadata.get("elements", {}) or {}
    n_tables = sum(1 for element in elements.values() if element.get("kind") == "table")
    n_other = sum(1 for element in elements.values() if element.get("kind") == "table" and element.get("table_type") == "other")
    mapper_coverage = (metadata.get("build_trace", {}).get("counters", {}) or {}).get("hie.mapper_coverage")
    if mapper_coverage is None:
        mapper_coverage = 1 - (n_other / n_tables if n_tables else 0)

    def add(flag_id: str, severity: str, category: str, message: str, evidence: list[dict]) -> None:
        flags.append({
            "id": flag_id,
            "severity": severity,
            "category": category,
            "message": message,
            "evidence": evidence,
        })

    if not qaqc.get("applicable") and (intercepts.get("applicable") or resource.get("applicable")):
        if n_tables and mapper_coverage < 0.8:
            message = f"QA/QC data was not detected while assay/resource data was captured (mapper coverage {mapper_coverage:.0%}; {n_other}/{n_tables} table(s) unmapped)."
        else:
            message = "QA/QC data was not detected while assay/resource data was captured."
        add("missing_qaqc", "high", "qaqc", message, [])

    if qaqc.get("applicable") and qaqc.get("overall") in ("fail", "marginal"):
        bad = [crm for crm in qaqc.get("crms", []) if crm.get("verdict") in ("fail", "marginal")]
        add("qaqc_performance", "high" if qaqc.get("overall") == "fail" else "medium", "qaqc", f"QA/QC overall '{qaqc.get('overall')}'.", _prov_ev(bad))

    suspect_resource = [row for row in resource.get("rows", []) if row.get("confidence") in ("low", "unverifiable")]
    if suspect_resource:
        add("resource_on_suspect_table", "high", "data_quality", "Resource figures derive from low/unverifiable tables.", _prov_ev(suspect_resource))

    suspect_intercepts = [row for row in intercepts.get("intervals", []) if row.get("confidence") in ("low", "unverifiable")]
    if suspect_intercepts:
        add("intercepts_on_suspect_table", "medium", "data_quality", "Some intercepts derive from low/unverifiable tables.", _prov_ev(suspect_intercepts))

    if resource.get("n_recompute_mismatches"):
        bad = [row for row in resource.get("rows", []) if row.get("recompute_ok") is False]
        add("resource_recompute_mismatch", "medium", "resource", f"{resource['n_recompute_mismatches']} resource row(s) do not reconcile to contained metal.", _prov_ev(bad))

    if resource.get("applicable") and intercepts.get("applicable") and intercepts.get("n_parent_intervals", 0) < 20:
        add("sparse_drilling_for_resource", "medium", "resource", f"Resource is reported but only {intercepts.get('n_parent_intervals', 0)} parent drill intercepts were captured.", [])

    if intercepts.get("containment_violations"):
        add("intercept_containment", "low", "data_quality", f"{intercepts['containment_violations']} sub-interval(s) are not contained within a parent interval.", [])

    if any(warning.get("kind") == "map_hie_skipped" for warning in metadata.get("build_trace", {}).get("warnings", [])):
        add("map_hie_unavailable", "low", "geospatial", "One or more map-like figures were not processed because peace-tool-pool was unavailable.", [])

    return {"stage": metadata.get("project_stage"), "flags": flags}
