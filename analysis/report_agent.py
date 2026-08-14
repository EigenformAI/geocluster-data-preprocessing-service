"""Report-Agent HIE artifact helpers."""

from __future__ import annotations

from .validation import ReportMetadataValidationError


def _issue(issues: list[dict], path: str, code: str, message: str) -> None:
    issues.append({"path": path, "code": code, "message": message})


def _raise_if_issues(issues: list[dict]) -> None:
    if issues:
        raise ReportMetadataValidationError(sorted(issues, key=lambda issue: (issue["path"], issue["code"], issue["message"])))


def _citation_evidence_ids(item: dict) -> list[str]:
    evidence_ids = []
    if item.get("evidence_id"):
        evidence_ids.append(item["evidence_id"])
    for evidence_id in item.get("evidence_ids") or []:
        evidence_ids.append(evidence_id)
    for citation in item.get("citations") or []:
        if citation.get("evidence_id"):
            evidence_ids.append(citation["evidence_id"])
    return evidence_ids


def validate_knowledge_bundle(bundle: dict) -> None:
    """Validate one DKI expert/report bundle with evidence-local references."""

    issues: list[dict] = []
    if not isinstance(bundle, dict):
        _issue(issues, "/", "schema.type", "knowledge bundle must be an object")
        _raise_if_issues(issues)

    items = bundle.get("items")
    evidence = bundle.get("evidence") or {}
    if not isinstance(items, list):
        _issue(issues, "/items", "schema.type", "items must be an array")
        items = []
    if not isinstance(evidence, dict):
        _issue(issues, "/evidence", "schema.type", "evidence must be an object")
        evidence = {}

    source_kind = bundle.get("source_kind")
    if not source_kind:
        _issue(issues, "/source_kind", "schema.required", "source_kind is required")
    if bundle.get("evidence_origin") not in {"report", "dki"}:
        _issue(issues, "/evidence_origin", "schema.enum", "evidence_origin must be report or dki")
    if "coverage" in bundle and not isinstance(bundle.get("coverage"), list):
        _issue(issues, "/coverage", "schema.type", "coverage must be an array")
    if "warnings" in bundle and not isinstance(bundle.get("warnings"), list):
        _issue(issues, "/warnings", "schema.type", "warnings must be an array")
    if source_kind == "knowledge_item" and items and not evidence:
        _issue(issues, "/evidence", "schema.required", "knowledge_item bundles with items must include evidence")

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            _issue(issues, f"/items/{index}", "schema.type", "item must be an object")
            continue
        if not item.get("id"):
            _issue(issues, f"/items/{index}/id", "schema.required", "id is required")
        if not item.get("text") and not item.get("span"):
            _issue(issues, f"/items/{index}/text", "schema.required", "text or span is required")
        evidence_ids = _citation_evidence_ids(item)
        if source_kind == "knowledge_item" and not evidence_ids:
            _issue(issues, f"/items/{index}/citations", "schema.minItems", "knowledge item must cite bundle evidence")
        for evidence_id in evidence_ids:
            if evidence and evidence_id not in evidence:
                _issue(
                    issues,
                    f"/items/{index}/citations",
                    "ref.dangling_evidence_id",
                    f"evidence_id {evidence_id!r} is not present in bundle evidence",
                )
            elif source_kind == "knowledge_item" and evidence_id not in evidence:
                _issue(
                    issues,
                    f"/items/{index}/citations",
                    "ref.dangling_evidence_id",
                    f"evidence_id {evidence_id!r} is not present in bundle evidence",
                )

    for evidence_id, evidence_item in evidence.items():
        if not isinstance(evidence_item, dict):
            _issue(issues, f"/evidence/{evidence_id}", "schema.type", "evidence item must be an object")
            continue
        if not evidence_item.get("source_kind"):
            _issue(issues, f"/evidence/{evidence_id}/source_kind", "schema.required", "source_kind is required")
        if evidence_item.get("raw_token") is None:
            _issue(issues, f"/evidence/{evidence_id}/raw_token", "schema.required", "raw_token is required")
        if source_kind == "knowledge_item" and evidence_item.get("source_kind") != "knowledge_item":
            _issue(issues, f"/evidence/{evidence_id}/source_kind", "schema.const", "knowledge bundle evidence must use source_kind knowledge_item")

    _raise_if_issues(issues)


def validate_answer_pack(answer_pack: dict, metadata: dict | None = None, evidence_index: dict | None = None) -> None:
    """Validate the deterministic PEQA input contract and source references."""

    metadata = metadata or {}
    evidence_index = evidence_index if evidence_index is not None else metadata.get("_evidence_index", {}) or {}
    issues: list[dict] = []
    if not isinstance(answer_pack, dict):
        _issue(issues, "/", "schema.type", "answer pack must be an object")
        _raise_if_issues(issues)

    if answer_pack.get("schema_version") != "peqa_answer_pack/v1":
        _issue(issues, "/schema_version", "schema.const", "schema_version must be peqa_answer_pack/v1")
    if not answer_pack.get("question"):
        _issue(issues, "/question", "schema.required", "question is required")
    if not isinstance(answer_pack.get("source_snippets"), dict):
        _issue(issues, "/source_snippets", "schema.type", "source_snippets must be an object")
    if not isinstance(answer_pack.get("dki_bundles"), dict):
        _issue(issues, "/dki_bundles", "schema.type", "dki_bundles must be an object")
    if not isinstance(answer_pack.get("coverage"), list):
        _issue(issues, "/coverage", "schema.type", "coverage must be an array")
    if not isinstance(answer_pack.get("open_gaps"), list):
        _issue(issues, "/open_gaps", "schema.type", "open_gaps must be an array")
    confidence = answer_pack.get("confidence")
    if not isinstance(confidence, dict):
        _issue(issues, "/confidence", "schema.type", "confidence must be an object")
    elif confidence.get("level") not in {"high", "medium", "low"}:
        _issue(issues, "/confidence/level", "schema.enum", "confidence level must be high, medium, or low")

    required_schema = answer_pack.get("required_output_schema")
    required_keys = {"answer", "reason", "claims", "open_gaps", "confidence", "coverage"}
    if not isinstance(required_schema, dict):
        _issue(issues, "/required_output_schema", "schema.type", "required_output_schema must be an object")
        required_schema = {}
    for key in sorted(required_keys - set(required_schema)):
        _issue(issues, f"/required_output_schema/{key}", "schema.required", f"{key} is required")

    for index, assertion in enumerate(answer_pack.get("selected_assertions") or []):
        for evidence_id in _citation_evidence_ids(assertion):
            if evidence_id not in evidence_index:
                _issue(
                    issues,
                    f"/selected_assertions/{index}",
                    "ref.dangling_evidence_id",
                    f"evidence_id {evidence_id!r} is not present in evidence_index",
                )

    for bundle_name, snippets in (answer_pack.get("source_snippets") or {}).items():
        if not isinstance(snippets, list):
            _issue(issues, f"/source_snippets/{bundle_name}", "schema.type", "source snippet group must be an array")
            continue
        for index, snippet in enumerate(snippets):
            if not isinstance(snippet, dict):
                _issue(issues, f"/source_snippets/{bundle_name}/{index}", "schema.type", "source snippet must be an object")
                continue
            for evidence_id in _citation_evidence_ids(snippet):
                if evidence_id not in evidence_index:
                    _issue(
                        issues,
                        f"/source_snippets/{bundle_name}/{index}",
                        "ref.dangling_evidence_id",
                        f"evidence_id {evidence_id!r} is not present in evidence_index",
                    )

    for index, crop in enumerate(answer_pack.get("figure_crops") or []):
        evidence_id = crop.get("evidence_id") if isinstance(crop, dict) else None
        if evidence_id and evidence_id not in evidence_index:
            _issue(issues, f"/figure_crops/{index}/evidence_id", "ref.dangling_evidence_id", f"evidence_id {evidence_id!r} is not present in evidence_index")

    for bundle_name, bundle in (answer_pack.get("dki_bundles") or {}).items():
        try:
            validate_knowledge_bundle(bundle)
        except ReportMetadataValidationError as exc:
            for issue in exc.issues:
                _issue(issues, f"/dki_bundles/{bundle_name}{issue['path']}", issue["code"], issue["message"])

    _raise_if_issues(issues)


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
        # Promote VLM-read figure semantics (legend labels, map annotations) into the
        # graph as low-confidence assertions: queryable but flagged as reads, not
        # verbatim source text. They stay grounded to their parent map_component crop.
        image_label_ids = list(map_meta.get("image_label_evidence_ids") or [])
        for evidence_id in map_meta.get("related_image_label_evidence_ids") or []:
            if evidence_id not in image_label_ids:
                image_label_ids.append(evidence_id)
        for link in map_meta.get("related_figures") or []:
            for evidence_id in link.get("image_label_evidence_ids") or []:
                if evidence_id not in image_label_ids:
                    image_label_ids.append(evidence_id)
        for evidence_id in image_label_ids:
            evidence = evidence_index.get(evidence_id) or {}
            annotation_kind = evidence.get("annotation_kind") or "label"
            predicate = "map_legend_entry" if annotation_kind == "legend_entry" else "map_annotation"
            assertions.append({
                "id": f"assert:image_label:{element_id}:{len(assertions) + 1}",
                "predicate": predicate,
                "subject": element_id,
                "value": evidence.get("raw_token"),
                "element_id": element_id,
                "evidence_ids": [evidence_id],
                "source_kind": "image_label",
                "confidence": "low",
                "annotation_kind": annotation_kind,
                "read_by": evidence.get("read_by"),
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
