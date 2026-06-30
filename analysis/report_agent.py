"""Report-Agent HIE artifact helpers."""

from __future__ import annotations

from .validation import ReportMetadataValidationError


def build_assertion_graph(metadata: dict, evidence_index: dict) -> dict:
    assertions: list[dict] = []

    for item in metadata.get("assertions") or []:
        assertions.append({
            "id": item.get("id"),
            "predicate": item.get("predicate"),
            "subject": item.get("subject_entity_id") or item.get("section_slug") or item.get("element_id"),
            "value": item.get("object_span") or item.get("span"),
            "evidence_ids": [item.get("evidence_id")] if item.get("evidence_id") else [],
            "source_kind": item.get("source_kind", "knowledge_item"),
            "element_id": item.get("element_id"),
            "section_slug": item.get("section_slug"),
            "confidence": item.get("confidence", "medium"),
            "modality": item.get("modality"),
            "span": item.get("span"),
        })

    for record_id, record in sorted((metadata.get("records") or {}).items()):
        provenance = record.get("provenance") or {}
        for field, evidence_id in sorted((record.get("_field_evidence") or {}).items()):
            evidence = evidence_index.get(evidence_id) or {}
            value = record.get(field)
            if isinstance(value, dict):
                value = value.get("raw_value", value.get("value"))
            assertions.append({
                "id": f"assert:record:{record_id}:{field}",
                "predicate": "record_field",
                "subject": record_id,
                "field": field,
                "value": value,
                "record_id": record_id,
                "element_id": provenance.get("element_id"),
                "evidence_ids": [evidence_id],
                "source_kind": evidence.get("source_kind", "csv_token"),
                "confidence": provenance.get("confidence", "medium"),
            })

    for element_id, element in sorted((metadata.get("elements") or {}).items()):
        map_meta = element.get("map_meta") or {}
        for evidence_id in map_meta.get("component_evidence_ids") or []:
            evidence = evidence_index.get(evidence_id) or {}
            assertions.append({
                "id": f"assert:map_component:{element_id}:{len(assertions) + 1}",
                "predicate": "map_component_detected",
                "subject": element_id,
                "value": evidence.get("raw_token"),
                "element_id": element_id,
                "evidence_ids": [evidence_id],
                "source_kind": "map_component",
                "confidence": evidence.get("confidence", "medium"),
            })

    graph = {
        "schema_version": "assertion_graph/v1",
        "report_slug": metadata.get("report_slug"),
        "assertions": assertions,
    }
    validate_assertion_graph(graph, metadata, evidence_index)
    return graph


def validate_assertion_graph(graph: dict, metadata: dict, evidence_index: dict | None = None) -> None:
    evidence_index = evidence_index if evidence_index is not None else metadata.get("_evidence_index", {}) or {}
    issues: list[dict] = []
    if graph.get("schema_version") != "assertion_graph/v1":
        issues.append({"path": "/schema_version", "code": "schema.const", "message": "schema_version must be assertion_graph/v1"})
    if not isinstance(graph.get("assertions"), list):
        issues.append({"path": "/assertions", "code": "schema.type", "message": "assertions must be an array"})
    elements = set((metadata.get("elements") or {}).keys())
    sections = {section.get("slug") for section in metadata.get("sections") or [] if section.get("slug")}
    records = set((metadata.get("records") or {}).keys())
    for index, assertion in enumerate(graph.get("assertions") or []):
        if not assertion.get("id"):
            issues.append({"path": f"/assertions/{index}/id", "code": "schema.required", "message": "id is required"})
        evidence_ids = assertion.get("evidence_ids") or []
        if not evidence_ids:
            issues.append({"path": f"/assertions/{index}/evidence_ids", "code": "schema.minItems", "message": "at least one evidence id is required"})
        for evidence_index_pos, evidence_id in enumerate(evidence_ids):
            if evidence_id not in evidence_index:
                issues.append({
                    "path": f"/assertions/{index}/evidence_ids/{evidence_index_pos}",
                    "code": "ref.dangling_evidence_id",
                    "message": f"evidence_id {evidence_id!r} is not present in evidence_index",
                })
        element_id = assertion.get("element_id")
        if element_id and element_id not in elements and element_id not in sections:
            issues.append({
                "path": f"/assertions/{index}/element_id",
                "code": "ref.dangling_element_id",
                "message": f"element_id {element_id!r} is not present in elements or sections",
            })
        record_id = assertion.get("record_id")
        if record_id and record_id not in records:
            issues.append({
                "path": f"/assertions/{index}/record_id",
                "code": "ref.dangling_record_id",
                "message": f"record_id {record_id!r} is not present in records",
            })
    if issues:
        raise ReportMetadataValidationError(sorted(issues, key=lambda issue: (issue["path"], issue["code"], issue["message"])))
