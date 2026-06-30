"""Domain Knowledge Injection seams.

The MVP keeps DKI deterministic and local: experts return structured bundles and
the selector preserves all relevant local knowledge. Live providers can plug into
the same ``Expert`` protocol later without changing the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AnalysisContext:
    question: str
    report_metadata: dict
    location: dict | None


class Expert(Protocol):
    name: str

    def is_relevant(self, ctx: AnalysisContext) -> bool: ...

    def get_knowledge(self, ctx: AnalysisContext) -> dict: ...


def pick_reliable(location_candidates: list[dict]) -> dict | None:
    for candidate in location_candidates:
        if candidate.get("confidence") in ("high", "medium"):
            return candidate
    return None


class ComplianceExpert:
    name = "compliance"

    def is_relevant(self, ctx: AnalysisContext) -> bool:
        return True

    def get_knowledge(self, ctx: AnalysisContext) -> dict:
        return {
            "evidence_origin": "dki",
            "source_kind": "knowledge_item",
            "items": [
                {
                    "id": "ni43-101-qaqc",
                    "text": "Assay/resource conclusions should disclose adequate QA/QC evidence, including standards, blanks, duplicates, or equivalent controls.",
                },
                {
                    "id": "cim-resource-categories",
                    "text": "Mineral resources should distinguish Measured, Indicated, and Inferred categories and support contained-metal arithmetic from tonnes and grade.",
                },
                {
                    "id": "historical-estimates",
                    "text": "Historical or non-NI 43-101 estimates should not be treated as current resources unless the report provides the required cautionary disclosure and validation.",
                },
            ],
            "warnings": [],
        }


class GeospatialExpert:
    name = "geospatial"

    def is_relevant(self, ctx: AnalysisContext) -> bool:
        question = ctx.question.lower()
        return any(term in question for term in ("location", "map", "fault", "occurrence", "geospatial", "risk"))

    def get_knowledge(self, ctx: AnalysisContext) -> dict:
        if not ctx.location:
            return {
                "evidence_origin": "dki",
                "source_kind": "knowledge_item",
                "items": [],
                "warnings": ["skipped_no_reliable_location"],
            }
        return {
            "evidence_origin": "dki",
            "source_kind": "knowledge_item",
            "location": ctx.location,
            "items": [],
            "warnings": ["live_geospatial_provider_not_configured"],
        }


def dki(question: str, report_metadata: dict, experts: list[Expert] | None = None) -> dict:
    experts = experts or [ComplianceExpert(), GeospatialExpert()]
    ctx = AnalysisContext(question, report_metadata, pick_reliable(report_metadata.get("location_candidates", [])))
    gathered = {expert.name: expert.get_knowledge(ctx) for expert in experts if expert.is_relevant(ctx)}
    warnings = [warning for bundle in gathered.values() for warning in bundle.get("warnings", [])]
    return {
        "question": question,
        "location": ctx.location,
        "gathered": gathered,
        "selected": gathered,
        "warnings": warnings,
    }
