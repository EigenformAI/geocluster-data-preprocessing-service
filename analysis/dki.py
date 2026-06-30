"""Domain Knowledge Injection seams.

The MVP keeps DKI deterministic and local: experts return structured bundles and
the selector preserves all relevant local knowledge. Live providers can plug into
the same ``Expert`` protocol later without changing the orchestrator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .source_paths import safe_source_path
from .taxonomy import item_for_section, matched_predicates, taxonomy_for_question
from .textguard import text_backed


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


class TextRecallExpert:
    name = "text_recall"

    def is_relevant(self, ctx: AnalysisContext) -> bool:
        return taxonomy_for_question(ctx.question) is not None

    def get_knowledge(self, ctx: AnalysisContext) -> dict:
        taxonomy = taxonomy_for_question(ctx.question)
        if taxonomy is None:
            return self._empty_bundle(warnings=["skipped_no_matching_question_class"])

        report_root = ctx.report_metadata.get("_report_root")
        if not report_root:
            return self._empty_bundle(
                question_class=taxonomy["question_class"],
                warnings=["skipped_no_report_root"],
            )

        root = Path(report_root).resolve()
        items_by_id: dict[str, dict] = {}
        evidence: dict[str, dict] = {}
        sections_scanned: list[str] = []
        items_covered: set[int] = set()
        predicates_found: set[str] = set()
        rejected_by_textguard = 0
        wanted_items = set(taxonomy["items"])

        for section in ctx.report_metadata.get("sections", []):
            item = item_for_section(section)
            if item not in wanted_items:
                continue
            source_ref = section.get("text_file")
            if not source_ref:
                continue
            source_path = safe_source_path(root, source_ref)
            if source_path is None:
                continue
            if not source_path.exists():
                continue
            try:
                source_text = source_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            sections_scanned.append(section.get("slug") or str(section.get("idx") or item))
            items_covered.add(item)
            for span in _candidate_spans(source_text):
                matches = matched_predicates(taxonomy["question_class"], span)
                if not matches:
                    continue
                if not text_backed(span, source_text):
                    rejected_by_textguard += 1
                    continue
                predicate = next(iter(matches))
                predicates_found.update(matches)
                evidence_id = f"ev:text_recall:{section.get('slug') or section.get('idx') or 'section'}:{taxonomy['question_class']}:{len(evidence) + 1}"
                item_id = f"text_recall:{section.get('slug') or section.get('idx') or 'section'}:{len(evidence) + 1}"
                evidence[evidence_id] = {
                    "element_id": section.get("slug"),
                    "page": section.get("page_start"),
                    "source_kind": "text_token",
                    "source_ref": source_ref,
                    "raw_token": span,
                    "span": span,
                    "section_slug": section.get("slug"),
                    "ni43101_item": item,
                    "question_class": taxonomy["question_class"],
                    "predicate": predicate,
                    "matched_keywords": matches[predicate],
                }
                items_by_id[item_id] = {
                    "id": item_id,
                    "evidence_id": evidence_id,
                    "text": span,
                    "span": span,
                    "predicate": predicate,
                    "matched_keywords": matches[predicate],
                    "section_slug": section.get("slug"),
                    "ni43101_item": item,
                    "page": section.get("page_start"),
                    "citations": [{"evidence_id": evidence_id}],
                }

        expected_predicates = set(taxonomy["predicates"])
        predicate_items = {predicate: set(items) for predicate, items in taxonomy.get("predicate_items", {}).items()}
        assessable_predicates = {
            predicate
            for predicate in expected_predicates
            if (predicate_items.get(predicate) or wanted_items) & items_covered
        }
        coverage = {
            "question_class": taxonomy["question_class"],
            "items_targeted": sorted(wanted_items),
            "items_covered": sorted(items_covered),
            "sections_scanned": sections_scanned,
            "predicates_found": sorted(predicates_found),
            "predicates_absent": sorted(assessable_predicates - predicates_found),
            "predicates_unassessed": sorted(expected_predicates - predicates_found - assessable_predicates),
            "predicate_items_required": {predicate: sorted(predicate_items.get(predicate) or wanted_items) for predicate in sorted(expected_predicates)},
        }
        return {
            "evidence_origin": "report",
            "source_kind": "text_token",
            "question_class": taxonomy["question_class"],
            "items": list(items_by_id.values()),
            "evidence": evidence,
            "coverage": [coverage],
            "warnings": [],
            "telemetry": {
                "recall.sections_scanned": len(sections_scanned),
                "recall.spans_emitted": len(evidence),
                "recall.spans_rejected_by_textguard": rejected_by_textguard,
                "coverage.items_covered": sorted(items_covered),
            },
        }

    def _empty_bundle(self, question_class: str | None = None, warnings: list[str] | None = None) -> dict:
        return {
            "evidence_origin": "report",
            "source_kind": "text_token",
            "question_class": question_class,
            "items": [],
            "evidence": {},
            "coverage": [],
            "warnings": warnings or [],
            "telemetry": {
                "recall.sections_scanned": 0,
                "recall.spans_emitted": 0,
                "recall.spans_rejected_by_textguard": 0,
                "coverage.items_covered": [],
            },
        }


def _candidate_spans(source_text: str) -> list[str]:
    spans: list[str] = []
    seen: set[str] = set()
    for block in re.split(r"\n\s*\n", source_text):
        lines = []
        table_lines = 0
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped == "---" or stripped.startswith(("section:", "title:", "pages:")):
                continue
            if stripped.startswith("|"):
                table_lines += 1
                continue
            if stripped.startswith("#") or stripped.startswith("!["):
                continue
            lines.append(stripped)
        if table_lines and not lines:
            continue
        span = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if not span or span in seen:
            continue
        seen.add(span)
        spans.append(span)
    return spans


def dki(question: str, report_metadata: dict, experts: list[Expert] | None = None) -> dict:
    experts = experts or [ComplianceExpert(), GeospatialExpert(), TextRecallExpert()]
    ctx = AnalysisContext(question, report_metadata, pick_reliable(report_metadata.get("location_candidates", [])))
    gathered = {expert.name: expert.get_knowledge(ctx) for expert in experts if expert.is_relevant(ctx)}
    warnings = [warning for bundle in gathered.values() for warning in bundle.get("warnings", [])]
    counters = {
        "dki.n_queries": len(gathered),
        "dki.n_provider_warnings": sum(1 for warning in warnings if "provider" in warning or "configured" in warning),
        "dki.n_skipped_no_bounds": sum(1 for warning in warnings if warning == "skipped_no_reliable_location"),
    }
    return {
        "question": question,
        "location": ctx.location,
        "gathered": gathered,
        "selected": gathered,
        "warnings": warnings,
        "counters": counters,
    }
