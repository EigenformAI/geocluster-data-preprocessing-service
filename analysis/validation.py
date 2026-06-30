"""JSON Schema and semantic validation for analysis IR artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


SCHEMA_DIR = Path(__file__).with_name("schema")


@dataclass
class ReportMetadataValidationError(ValueError):
    issues: list[dict]

    def __str__(self) -> str:
        first = self.issues[0] if self.issues else {"message": "unknown validation error"}
        return f"report metadata validation failed: {first.get('path', '/')}: {first.get('message')}"


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def _path(parts) -> str:
    out = ""
    for part in parts:
        token = str(part).replace("~", "~0").replace("/", "~1")
        out += f"/{token}"
    return out or "/"


def _schema_issues(instance: Any, schema_name: str, prefix: str = "") -> list[dict]:
    validator = Draft202012Validator(_load_schema(schema_name))
    issues = []
    for error in validator.iter_errors(instance):
        path = prefix + _path(error.absolute_path)
        issues.append({
            "path": path,
            "code": f"schema.{error.validator}",
            "message": error.message,
        })
    return sorted(issues, key=lambda issue: (issue["path"], issue["code"], issue["message"]))


def _add_ref_issue(issues: list[dict], path: str, code: str, message: str) -> None:
    issues.append({"path": path, "code": code, "message": message})


def validate_report_metadata(metadata: dict, evidence_index: dict | None = None) -> None:
    """Validate public metadata/evidence shape and cross-artifact references."""

    public_metadata = {key: value for key, value in metadata.items() if not key.startswith("_")}
    evidence_index = evidence_index if evidence_index is not None else metadata.get("_evidence_index", {}) or {}
    issues: list[dict] = []
    issues.extend(_schema_issues(public_metadata, "report_metadata.schema.json"))
    issues.extend(_schema_issues(evidence_index, "evidence_index.schema.json", "/evidence_index"))

    elements = public_metadata.get("elements") or {}
    records = public_metadata.get("records") or {}
    entities = public_metadata.get("entities") or {}
    relationships = public_metadata.get("relationships") or []
    sections = public_metadata.get("sections") or []
    section_slugs = {section.get("slug") for section in sections if section.get("slug")}

    for section_index, section in enumerate(sections):
        for index, element_id in enumerate(section.get("element_ids") or []):
            if element_id not in elements:
                _add_ref_issue(issues, f"/sections/{section_index}/element_ids/{index}", "ref.dangling_element_id", f"element_id {element_id!r} is not present in elements")

    for element_id, element in elements.items():
        section_slug = element.get("section_slug")
        if section_slug and section_slug not in section_slugs:
            _add_ref_issue(issues, f"/elements/{element_id}/section_slug", "ref.dangling_section_slug", f"section_slug {section_slug!r} is not present in sections")
        for index, record_id in enumerate(element.get("record_ids") or []):
            if record_id not in records:
                _add_ref_issue(issues, f"/elements/{element_id}/record_ids/{index}", "ref.dangling_record_id", f"record_id {record_id!r} is not present in records")

    for record_id, record in records.items():
        if record.get("_id") and record.get("_id") != record_id:
            _add_ref_issue(issues, f"/records/{record_id}/_id", "ref.record_id_mismatch", f"record _id {record.get('_id')!r} does not match key {record_id!r}")
        element_id = (record.get("provenance") or {}).get("element_id")
        if element_id and element_id not in elements:
            _add_ref_issue(issues, f"/records/{record_id}/provenance/element_id", "ref.dangling_element_id", f"element_id {element_id!r} is not present in elements")
        for index, entity_id in enumerate(record.get("entity_ids") or []):
            if entity_id not in entities:
                _add_ref_issue(issues, f"/records/{record_id}/entity_ids/{index}", "ref.dangling_entity_id", f"entity_id {entity_id!r} is not present in entities")
        for field, evidence_id in (record.get("_field_evidence") or {}).items():
            if evidence_id not in evidence_index:
                _add_ref_issue(issues, f"/records/{record_id}/_field_evidence/{field}", "ref.dangling_evidence_id", f"evidence_id {evidence_id!r} is not present in evidence_index")
        for field, value in record.items():
            if isinstance(value, dict) and value.get("evidence_id") and value["evidence_id"] not in evidence_index:
                _add_ref_issue(issues, f"/records/{record_id}/{field}/evidence_id", "ref.dangling_evidence_id", f"evidence_id {value['evidence_id']!r} is not present in evidence_index")

    for entity_id, entity in entities.items():
        for index, record_id in enumerate(entity.get("record_ids") or []):
            if record_id not in records:
                _add_ref_issue(issues, f"/entities/{entity_id}/record_ids/{index}", "ref.dangling_record_id", f"record_id {record_id!r} is not present in records")
        for index, element_id in enumerate(entity.get("element_ids") or []):
            if element_id not in elements:
                _add_ref_issue(issues, f"/entities/{entity_id}/element_ids/{index}", "ref.dangling_element_id", f"element_id {element_id!r} is not present in elements")
        for index, evidence_id in enumerate(entity.get("evidence_ids") or []):
            if evidence_id not in evidence_index:
                _add_ref_issue(issues, f"/entities/{entity_id}/evidence_ids/{index}", "ref.dangling_evidence_id", f"evidence_id {evidence_id!r} is not present in evidence_index")

    seen_relationships: set[tuple[str, str, str]] = set()
    for index, rel in enumerate(relationships):
        key = (rel.get("type"), rel.get("from"), rel.get("to"))
        if key in seen_relationships:
            _add_ref_issue(issues, f"/relationships/{index}", "ref.duplicate_relationship", f"relationship {key!r} is duplicated")
        seen_relationships.add(key)
        for endpoint in ("from", "to"):
            entity_id = rel.get(endpoint)
            if entity_id and entity_id not in entities:
                _add_ref_issue(issues, f"/relationships/{index}/{endpoint}", "ref.dangling_entity_id", f"entity_id {entity_id!r} is not present in entities")
        for evidence_index_pos, evidence_id in enumerate(rel.get("evidence_ids") or []):
            if evidence_id not in evidence_index:
                _add_ref_issue(issues, f"/relationships/{index}/evidence_ids/{evidence_index_pos}", "ref.dangling_evidence_id", f"evidence_id {evidence_id!r} is not present in evidence_index")

    for index, candidate in enumerate(public_metadata.get("location_candidates") or []):
        for evidence_id in ([candidate.get("evidence_id")] if candidate.get("evidence_id") else []) + list(candidate.get("evidence_ids") or []):
            if evidence_id not in evidence_index:
                _add_ref_issue(issues, f"/location_candidates/{index}/evidence_id", "ref.dangling_evidence_id", f"evidence_id {evidence_id!r} is not present in evidence_index")

    valid_element_ids = set(elements) | section_slugs
    for evidence_id, evidence in evidence_index.items():
        element_id = evidence.get("element_id")
        if element_id and element_id not in valid_element_ids:
            _add_ref_issue(issues, f"/evidence_index/{evidence_id}/element_id", "ref.dangling_element_id", f"element_id {element_id!r} is not present in elements or sections")
        for index, input_evidence_id in enumerate(evidence.get("input_evidence_ids") or []):
            if input_evidence_id not in evidence_index:
                _add_ref_issue(issues, f"/evidence_index/{evidence_id}/input_evidence_ids/{index}", "ref.dangling_evidence_id", f"evidence_id {input_evidence_id!r} is not present in evidence_index")

    if issues:
        raise ReportMetadataValidationError(sorted(issues, key=lambda issue: (issue["path"], issue["code"], issue["message"])))
