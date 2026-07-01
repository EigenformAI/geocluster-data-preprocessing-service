"""Domain Knowledge Injection seams.

The MVP keeps DKI deterministic and local: experts return structured bundles and
the selector preserves all relevant local knowledge. Live providers can plug into
the same ``Expert`` protocol later without changing the orchestrator.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

from .assertions import assertion_gap_analysis, query_assertions
from .source_paths import safe_source_path
from .spans import SpanExtractor, candidate_spans, select_span_extractor
from .taxonomy import QUESTION_TAXONOMY, item_for_section, matched_predicates, taxonomy_for_question
from .textguard import text_backed


TOOL_POOL = [
    "text_recall",
    "assertion_graph",
    "table_metrics",
    "map_component_detection",
    "geospatial_knowledge",
    "country_legal_references",
    "textguard",
    "numguard",
    "claim_verification",
]


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


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower() or "item"


def _knowledge_bundle(expert_name: str, items: list[dict], warnings: list[str] | None = None, coverage: list[dict] | None = None) -> dict:
    evidence: dict[str, dict] = {}
    normalized_items: list[dict] = []
    for index, item in enumerate(items, start=1):
        item_id = str(item.get("id") or f"item-{index}")
        text = str(item.get("text") or "")
        evidence_id = str(item.get("evidence_id") or f"ev:dki:{_safe_id(expert_name)}:{_safe_id(item_id)}")
        evidence[evidence_id] = {
            "source_kind": "knowledge_item",
            "raw_token": text,
            "knowledge_id": item_id,
            "expert": expert_name,
            "evidence_origin": "dki",
        }
        if item.get("source_ref") is not None:
            evidence[evidence_id]["source_ref"] = item.get("source_ref")
        normalized_items.append({
            **item,
            "id": item_id,
            "text": text,
            "evidence_id": evidence_id,
            "citations": [{"evidence_id": evidence_id}],
            "provenance": {"expert": expert_name, "evidence_origin": "dki"},
        })
    return {
        "evidence_origin": "dki",
        "source_kind": "knowledge_item",
        "items": normalized_items,
        "evidence": evidence,
        "coverage": coverage or [],
        "warnings": warnings or [],
    }


class ComplianceExpert:
    name = "compliance"

    def is_relevant(self, ctx: AnalysisContext) -> bool:
        return True

    def get_knowledge(self, ctx: AnalysisContext) -> dict:
        return _knowledge_bundle(
            self.name,
            [
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
        )


class FixedKnowledgeExpert:
    name = "fixed"
    items: list[dict] = []

    def is_relevant(self, ctx: AnalysisContext) -> bool:
        return True

    def get_knowledge(self, ctx: AnalysisContext) -> dict:
        return _knowledge_bundle(self.name, list(self.items))


class MiningEngineerExpert(FixedKnowledgeExpert):
    name = "mining_engineer"
    items = [
        {"id": "mine-plan-maturity", "text": "Mine plans and economic conclusions should disclose the study level, modifying factors, and remaining engineering work."},
        {"id": "recommendation-budget", "text": "Recommended work programs should be scoped, phased, and tied to the stated project maturity."},
    ]


class ResourceGeologistExpert(FixedKnowledgeExpert):
    name = "resource_geologist"
    items = [
        {"id": "resource-confidence", "text": "Resource confidence depends on drill density, data verification, geological continuity, and category support."},
        {"id": "qaqc-resource-risk", "text": "QA/QC omissions and historical data limitations are material geological-risk indicators."},
    ]


class LegalTenureExpert(FixedKnowledgeExpert):
    name = "legal_tenure"
    items = [
        {"id": "title-good-standing", "text": "Tenure findings should distinguish active title, good standing, royalties, encumbrances, and surface-access limitations."},
    ]


class EnvironmentalPermittingExpert(FixedKnowledgeExpert):
    name = "environmental_permitting"
    items = [
        {"id": "permit-social-baseline", "text": "Permitting review should cover environmental studies, water, closure, rehabilitation, and social or Indigenous consultation obligations."},
    ]


class SeismicStructuralExpert(FixedKnowledgeExpert):
    name = "seismic_structural"
    items = [
        {"id": "fault-structure-risk", "text": "Faults and structural controls can affect geological continuity, geotechnical risk, and regional hazard context."},
    ]


class CountryRiskExpert(FixedKnowledgeExpert):
    name = "country_risk"
    items = [
        {"id": "jurisdiction-risk", "text": "Jurisdictional review should distinguish legal title risks, fiscal terms, rule-of-law issues, and political or community constraints."},
    ]


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
                "evidence": {},
                "coverage": [],
                "warnings": ["skipped_no_reliable_location"],
            }
        return {
            "evidence_origin": "dki",
            "source_kind": "knowledge_item",
            "location": ctx.location,
            "items": [],
            "evidence": {},
            "coverage": [{"location_method": ctx.location.get("method"), "confidence": ctx.location.get("confidence")}],
            "warnings": ["live_geospatial_provider_not_configured"],
        }


class TextRecallExpert:
    name = "text_recall"

    def __init__(self, span_extractor: SpanExtractor | None = None):
        self.span_extractor = span_extractor

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
        extractor = self.span_extractor or select_span_extractor(root / "analysis" / ".cache" / "llm")
        if hasattr(extractor, "reset_telemetry"):
            extractor.reset_telemetry()
        seen_evidence: set[tuple[str, str, str]] = set()

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
            section_for_extract = {**section, "ni43101_item": item}
            for extracted in extractor.extract_spans(taxonomy["question_class"], section_for_extract, source_text, taxonomy):
                span = extracted.span
                matches = matched_predicates(taxonomy["question_class"], span)
                valid_predicates = set(taxonomy["predicates"])
                if extracted.predicate not in valid_predicates and not matches:
                    continue
                if not text_backed(span, source_text):
                    rejected_by_textguard += 1
                    continue
                predicate = extracted.predicate if extracted.predicate in valid_predicates else next(iter(matches))
                dedupe_key = (source_ref, predicate, span)
                if dedupe_key in seen_evidence:
                    continue
                seen_evidence.add(dedupe_key)
                predicates_found.update(matches)
                predicates_found.add(predicate)
                evidence_id = f"ev:text_recall:{section.get('slug') or section.get('idx') or 'section'}:{taxonomy['question_class']}:{len(evidence) + 1}"
                item_id = f"text_recall:{section.get('slug') or section.get('idx') or 'section'}:{len(evidence) + 1}"
                matched_keywords = matches.get(predicate, [])
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
                    "matched_keywords": matched_keywords,
                    "modality": extracted.modality,
                    "extraction_source": getattr(extracted, "source", "span_extractor"),
                }
                items_by_id[item_id] = {
                    "id": item_id,
                    "evidence_id": evidence_id,
                    "text": span,
                    "span": span,
                    "predicate": predicate,
                    "matched_keywords": matched_keywords,
                    "modality": extracted.modality,
                    "extraction_source": getattr(extracted, "source", "span_extractor"),
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
                "recall.spans_rejected_by_textguard": rejected_by_textguard
                + int((getattr(extractor, "telemetry", {}) or {}).get("recall.spans_rejected_by_textguard") or 0),
                "recall.llm_spans_added_over_lexical": int(
                    (getattr(extractor, "telemetry", {}) or {}).get("recall.llm_spans_added_over_lexical") or 0
                ),
                "recall.llm_degraded": int((getattr(extractor, "telemetry", {}) or {}).get("recall.llm_degraded") or 0),
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
                "recall.llm_spans_added_over_lexical": 0,
                "recall.llm_degraded": 0,
                "coverage.items_covered": [],
            },
        }


def _candidate_spans(source_text: str) -> list[str]:
    return candidate_spans(source_text)


class AssertionGraphExpert:
    name = "assertion_graph"

    def is_relevant(self, ctx: AnalysisContext) -> bool:
        return bool(ctx.report_metadata.get("assertions"))

    def get_knowledge(self, ctx: AnalysisContext) -> dict:
        assertions = query_assertions(ctx.report_metadata.get("assertions", []))
        section_items = {item_for_section(section) for section in ctx.report_metadata.get("sections", [])}
        gaps: list[dict] = []
        coverage: list[dict] = []
        for question_class, taxonomy in QUESTION_TAXONOMY.items():
            wanted_items = set(taxonomy["items"])
            expected_predicates = set(taxonomy["predicates"])
            class_assertions = query_assertions(
                assertions,
                question_class=question_class,
                predicates=expected_predicates,
                items=wanted_items,
            )
            items_covered = {item for item in section_items if item in wanted_items}
            predicate_items = {predicate: set(items) for predicate, items in taxonomy.get("predicate_items", {}).items()}
            assessable_predicates = {
                predicate
                for predicate in expected_predicates
                if (predicate_items.get(predicate) or wanted_items) & items_covered
            }
            if not class_assertions and not items_covered:
                continue
            gaps.extend(
                {"question_class": question_class, **gap}
                for gap in assertion_gap_analysis(class_assertions, sorted(assessable_predicates))
            )
            coverage.append({
                "question_class": question_class,
                "items_covered": sorted(items_covered),
                "predicates_assessed": sorted(assessable_predicates),
                "n_assertions": len(class_assertions),
            })
        return {
            "evidence_origin": "report",
            "source_kind": "text_token",
            "question_class": "all",
            "items": [
                {
                    "id": item.get("id"),
                    "evidence_id": item.get("evidence_id"),
                    "text": item.get("span"),
                    "span": item.get("span"),
                    "predicate": item.get("predicate"),
                    "modality": item.get("modality"),
                    "question_class": item.get("question_class"),
                    "section_slug": item.get("section_slug"),
                    "ni43101_item": item.get("ni43101_item"),
                    "page": item.get("page"),
                    "citations": [{"evidence_id": item.get("evidence_id")}],
                }
                for item in assertions
                if item.get("evidence_id")
            ],
            "gaps": gaps,
            "coverage": coverage,
            "warnings": [],
        }


def _default_experts() -> list[Expert]:
    return [
        AssertionGraphExpert(),
    ]


def _route_names(question: str, report_metadata: dict) -> tuple[str | None, set[str]]:
    taxonomy = taxonomy_for_question(question)
    question_class = taxonomy["question_class"] if taxonomy else None
    names: set[str] = set()
    if question_class:
        # Keep explicit TextRecallExpert tests usable, but do not include it in the
        # default expert set; HIE already harvested the same spans question-agnostically.
        names.add("text_recall")
    if report_metadata.get("assertions"):
        names.add("assertion_graph")
    return question_class, names


def dki(question: str, report_metadata: dict, experts: list[Expert] | None = None) -> dict:
    experts = experts or _default_experts()
    ctx = AnalysisContext(question, report_metadata, pick_reliable(report_metadata.get("location_candidates", [])))
    question_class, routed_names = _route_names(question, report_metadata)
    gathered = {}
    skipped = []
    for expert in experts:
        if expert.name not in routed_names:
            skipped.append({"name": expert.name, "reason": "route_not_selected"})
            continue
        if not expert.is_relevant(ctx):
            skipped.append({"name": expert.name, "reason": "expert_not_relevant"})
            continue
        gathered[expert.name] = expert.get_knowledge(ctx)
    warnings = [warning for bundle in gathered.values() for warning in bundle.get("warnings", [])]
    assertion_items = (gathered.get("assertion_graph") or {}).get("items") or []
    assertions_by_class = Counter(str(item.get("question_class") or "unknown") for item in assertion_items)
    items_covered = sorted({item.get("ni43101_item") for item in assertion_items if item.get("ni43101_item") is not None})
    counters = {
        "dki.n_queries": len(gathered),
        "dki.n_experts_considered": len(experts),
        "dki.n_experts_selected": len(gathered),
        "dki.n_experts_skipped": len(skipped),
        "dki.n_provider_warnings": sum(1 for warning in warnings if "provider" in warning or "configured" in warning),
        "dki.n_skipped_no_bounds": sum(1 for warning in warnings if warning == "skipped_no_reliable_location"),
        "dki.n_assertions_surfaced_by_class": dict(sorted(assertions_by_class.items())),
        "coverage.items_covered": items_covered,
    }
    return {
        "question": question,
        "location": ctx.location,
        "routing": {
            "question_class": question_class,
            "experts_considered": [expert.name for expert in experts],
            "experts_selected": list(gathered),
            "experts_skipped": skipped,
        },
        "tool_pool": TOOL_POOL,
        "gathered": gathered,
        "selected": gathered,
        "warnings": warnings,
        "counters": counters,
    }
