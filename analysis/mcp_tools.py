"""Safe in-process tool surface used by the stdio MCP wrapper and tests."""

from __future__ import annotations

import re
from pathlib import Path

from pipeline.util import read_json

from .claims import verify_claim
from .compute import compute_metrics, intercepts, qaqc, resources, tenure
from .flags import compute_flags

_SAFE_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")


class GeoReportTools:
    def __init__(self, output_root: Path | str = "output"):
        self.output_root = Path(output_root)

    def _report_root(self, slug: str) -> Path:
        if not _SAFE_SLUG.match(slug):
            raise ValueError("report slug must contain only letters, numbers, underscores, and dashes")
        root = (self.output_root / slug).resolve()
        allowed = self.output_root.resolve()
        if allowed not in root.parents and root != allowed:
            raise ValueError("report slug resolves outside output root")
        if not (root / "manifest.json").exists():
            raise FileNotFoundError(f"unknown report slug: {slug}")
        return root

    def _metadata(self, slug: str) -> dict:
        root = self._report_root(slug)
        metadata = read_json(root / "analysis" / "report_metadata.json", default=None)
        if metadata is None:
            raise FileNotFoundError(f"analysis/report_metadata.json not found for {slug}; run analysis.hie first")
        evidence = read_json(root / "analysis" / "evidence_index.json", default={}) or {}
        metadata["_evidence_index"] = evidence
        return metadata

    def list_tools(self) -> list[dict]:
        slug_schema = {"type": "string", "description": "Report slug under output/."}
        evidence_citation = {
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "element_id": {"type": "string"},
            },
            "additionalProperties": False,
        }
        return [
            {"name": "report_load", "description": "Load report metadata summary.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema}, "required": ["slug"], "additionalProperties": False}},
            {"name": "report_get_records", "description": "Return normalized records by type and optional exact-match filters.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema, "type": {"type": "string"}, "filters": {"type": "object"}, "limit": {"type": "integer", "minimum": 1}}, "required": ["slug"], "additionalProperties": False}},
            {"name": "report_compute_intercepts", "description": "Compute significant intercepts deterministically.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema, "cutoff": {"type": "number"}}, "required": ["slug"], "additionalProperties": False}},
            {"name": "report_compute_resource", "description": "Recompute resource contained metal.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema}, "required": ["slug"], "additionalProperties": False}},
            {"name": "report_check_qaqc", "description": "Compute QA/QC CRM checks.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema}, "required": ["slug"], "additionalProperties": False}},
            {"name": "report_compute_tenure", "description": "Summarize tenure records.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema}, "required": ["slug"], "additionalProperties": False}},
            {"name": "report_flags", "description": "Return deterministic red flags.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema}, "required": ["slug"], "additionalProperties": False}},
            {"name": "report_verify_claim", "description": "Verify numeric claim text against cited evidence.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema, "text": {"type": "string"}, "citations": {"type": "array", "items": evidence_citation}}, "required": ["slug", "text"], "additionalProperties": False}},
            {"name": "report_get_element", "description": "Return one metadata element by id.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema, "element_id": {"type": "string"}}, "required": ["slug", "element_id"], "additionalProperties": False}},
            {"name": "report_get_evidence", "description": "Return one evidence record by id.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema, "evidence_id": {"type": "string"}}, "required": ["slug", "evidence_id"], "additionalProperties": False}},
            {"name": "report_get_element_image", "description": "Return a safe resource URI for a figure or table crop image.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema, "element_id": {"type": "string"}}, "required": ["slug", "element_id"], "additionalProperties": False}},
            {"name": "report_query_hierarchy", "description": "Query entities and relationships.", "inputSchema": {"type": "object", "properties": {"slug": slug_schema, "level": {"type": "string"}, "relation": {"type": "string"}, "name": {"type": "string"}}, "required": ["slug"], "additionalProperties": False}},
        ]

    def call(self, name: str, arguments: dict) -> dict:
        if name == "report_load":
            metadata = self._metadata(arguments["slug"])
            return {
                "report_slug": metadata.get("report_slug"),
                "doc_id": metadata.get("doc_id"),
                "stats": metadata.get("stats"),
                "project_stage": metadata.get("project_stage"),
                "location_candidates": metadata.get("location_candidates", [])[:3],
                "n_entities": len(metadata.get("entities", {})),
                "n_relationships": len(metadata.get("relationships", [])),
            }
        if name == "report_get_records":
            metadata = self._metadata(arguments["slug"])
            records = list(metadata.get("records", {}).values())
            record_type = arguments.get("type")
            if record_type:
                records = [record for record in records if record.get("_type") == record_type]
            filters = arguments.get("filters") or {}
            for key, value in filters.items():
                records = [record for record in records if record.get(key) == value]
            limit = int(arguments.get("limit") or 100)
            records = records[:limit]
            return {"records": records}
        if name == "report_compute_intercepts":
            metadata = self._metadata(arguments["slug"])
            assays = [record for record in metadata.get("records", {}).values() if record.get("_type") == "assay_interval"]
            return intercepts(assays, float(arguments.get("cutoff", 0.3)))
        if name == "report_compute_resource":
            metadata = self._metadata(arguments["slug"])
            rows = [record for record in metadata.get("records", {}).values() if record.get("_type") == "resource_estimate"]
            return resources(rows)
        if name == "report_check_qaqc":
            metadata = self._metadata(arguments["slug"])
            rows = [record for record in metadata.get("records", {}).values() if record.get("_type") == "qaqc_crm"]
            return qaqc(rows)
        if name == "report_compute_tenure":
            metadata = self._metadata(arguments["slug"])
            rows = [record for record in metadata.get("records", {}).values() if record.get("_type") == "tenure"]
            return tenure(rows)
        if name == "report_flags":
            metadata = self._metadata(arguments["slug"])
            metrics = compute_metrics(metadata)
            return compute_flags(metadata, metrics)
        if name == "report_verify_claim":
            metadata = self._metadata(arguments["slug"])
            return verify_claim(arguments["text"], arguments.get("citations", []), metadata)
        if name == "report_get_element":
            metadata = self._metadata(arguments["slug"])
            element = metadata.get("elements", {}).get(arguments["element_id"])
            if element is None:
                raise KeyError(arguments["element_id"])
            return element
        if name == "report_get_evidence":
            metadata = self._metadata(arguments["slug"])
            evidence = metadata.get("_evidence_index", {}).get(arguments["evidence_id"])
            if evidence is None:
                raise KeyError(arguments["evidence_id"])
            return evidence
        if name == "report_get_element_image":
            metadata = self._metadata(arguments["slug"])
            element_id = arguments["element_id"]
            element = metadata.get("elements", {}).get(element_id)
            if element is None:
                raise KeyError(element_id)
            image_ref = element.get("image_path") or element.get("crop_png")
            if not image_ref:
                raise FileNotFoundError(f"element has no image resource: {element_id}")
            return {"element_id": element_id, "uri": f"geo-report://{arguments['slug']}/elements/{element_id}/image", "source_ref": image_ref}
        if name == "report_query_hierarchy":
            metadata = self._metadata(arguments["slug"])
            level = arguments.get("level")
            relation = arguments.get("relation")
            name_filter = (arguments.get("name") or "").lower()
            entities = [{"id": eid, **entity} for eid, entity in metadata.get("entities", {}).items()]
            if level:
                entities = [entity for entity in entities if entity.get("level") == level]
            if name_filter:
                entities = [entity for entity in entities if name_filter in str(entity.get("name", "")).lower()]
            relationships = list(metadata.get("relationships", []))
            if relation:
                relationships = [rel for rel in relationships if rel.get("type") == relation]
            return {"entities": entities, "relationships": relationships}
        raise KeyError(f"unknown tool: {name}")
