"""Span-grounded assertion graph for prose facts."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from pipeline.util import sha256_bytes

from .schemas import ASSERTION_MODALITIES, CONFIDENCE_LEVELS, SOURCE_KINDS
from .source_paths import safe_source_path
from .spans import SpanExtractor, infer_modality, select_span_extractor
from .taxonomy import QUESTION_TAXONOMY, item_for_section
from .textguard import text_backed

_PAGE_MARKER = re.compile(r"<!--\s*page:\s*(\d+)\s*-->", re.I)


@dataclass(frozen=True)
class Assertion:
    id: str
    subject_entity_id: str | None
    predicate: str
    object_span: str
    modality: str
    confidence: str
    source_kind: str
    span: str
    evidence_id: str
    text_evidence_id: str
    element_id: str | None
    page: int | None
    section_slug: str | None
    ni43101_item: int | None
    question_class: str | None = None

    def __post_init__(self) -> None:
        if not self.span or not self.span.strip():
            raise ValueError("assertion span is required")
        if not self.evidence_id:
            raise ValueError("assertion evidence_id is required")
        if not self.text_evidence_id:
            raise ValueError("assertion text_evidence_id is required")
        if self.modality not in ASSERTION_MODALITIES:
            raise ValueError(f"assertion modality must be one of {ASSERTION_MODALITIES}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"assertion confidence must be one of {CONFIDENCE_LEVELS}")
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(f"assertion source_kind must be one of {SOURCE_KINDS}")

    def to_dict(self) -> dict:
        return asdict(self)


def _taxonomy_for_question_class(question_class: str) -> dict:
    taxonomy = QUESTION_TAXONOMY[question_class]
    return {
        "question_class": question_class,
        "items": list(taxonomy["items"]),
        "predicates": {key: list(value) for key, value in taxonomy["predicates"].items()},
        "predicate_items": {key: list(value) for key, value in taxonomy.get("predicate_items", {}).items()},
    }


def _taxonomies_for_item(item: int | None) -> list[dict]:
    if item is None:
        return []
    return [
        _taxonomy_for_question_class(question_class)
        for question_class, taxonomy in QUESTION_TAXONOMY.items()
        if item in taxonomy["items"]
    ]


def _assertion_suffix(section_slug: str, question_class: str, span: str, predicate: str) -> str:
    digest = sha256_bytes(section_slug.encode(), question_class.encode(), predicate.encode(), span.encode())[:12]
    return f"{section_slug}:{question_class}:{digest}"


def _link_subject_entity(span: str, entities: dict[str, dict]) -> str | None:
    folded = span.casefold()
    project_id = None
    for entity_id, entity in entities.items():
        if entity.get("level") == "project" and project_id is None:
            project_id = entity_id
        name = str(entity.get("name") or "").strip()
        if len(name) >= 4 and name.casefold() in folded:
            return entity_id
    return project_id


def _page_for_span(root: Path, section: dict, source_text: str, span: str) -> int | None:
    start = int(section.get("page_start") or 0)
    end = int(section.get("page_end") or start)
    for page in range(start, end + 1):
        page_path = root / "ocr" / f"page_{page:04d}.md"
        if page_path.exists() and text_backed(span, page_path.read_text(encoding="utf-8", errors="replace")):
            return page

    matches = list(_PAGE_MARKER.finditer(source_text))
    for index, marker in enumerate(matches):
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(source_text)
        if text_backed(span, source_text[marker.end():block_end]):
            return int(marker.group(1))
    return section.get("page_start")


def harvest_assertions(
    report_root: Path | str,
    sections: Iterable[dict],
    entities: dict[str, dict],
    span_extractor: SpanExtractor | None = None,
) -> tuple[list[dict], dict[str, dict], dict]:
    root = Path(report_root).resolve()
    extractor = span_extractor or select_span_extractor(root / "analysis" / ".cache" / "llm")
    if hasattr(extractor, "reset_telemetry"):
        extractor.reset_telemetry()

    assertions: list[dict] = []
    evidence: dict[str, dict] = {}
    rejected_by_textguard = 0
    seen: set[tuple[str | None, str, str]] = set()

    for section in sections:
        item = item_for_section(section)
        taxonomies = _taxonomies_for_item(item)
        if not taxonomies:
            continue
        source_ref = section.get("text_file")
        if not source_ref:
            continue
        source_path = safe_source_path(root, source_ref)
        if source_path is None or not source_path.exists():
            continue
        try:
            source_text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        section_slug = section.get("slug") or str(section.get("idx") or item or "section")
        section_for_extract = {**section, "ni43101_item": item}
        for taxonomy in taxonomies:
            question_class = taxonomy["question_class"]
            for extracted in extractor.extract_spans(question_class, section_for_extract, source_text, taxonomy):
                if extracted.predicate not in taxonomy["predicates"]:
                    continue
                if not text_backed(extracted.span, source_text):
                    rejected_by_textguard += 1
                    continue
                key = (section_slug, extracted.predicate, extracted.span)
                if key in seen:
                    continue
                seen.add(key)
                suffix = _assertion_suffix(section_slug, question_class, extracted.span, extracted.predicate)
                text_evidence_id = f"ev:assertion_text:{suffix}"
                evidence_id = f"ev:assertion:{suffix}"
                modality = extracted.modality or infer_modality(extracted.span, extracted.predicate)
                page = _page_for_span(root, section, source_text, extracted.span)
                assertion = Assertion(
                    id=f"as:{suffix}",
                    subject_entity_id=_link_subject_entity(extracted.span, entities),
                    predicate=extracted.predicate,
                    object_span=extracted.span,
                    modality=modality,
                    confidence=extracted.confidence,
                    source_kind="text_token",
                    span=extracted.span,
                    evidence_id=evidence_id,
                    text_evidence_id=text_evidence_id,
                    element_id=section.get("slug"),
                    page=page,
                    section_slug=section.get("slug"),
                    ni43101_item=extracted.ni43101_item or item,
                    question_class=question_class,
                ).to_dict()
                evidence[text_evidence_id] = {
                    "element_id": section.get("slug"),
                    "page": page,
                    "source_kind": "text_token",
                    "source_ref": source_ref,
                    "raw_token": extracted.span,
                    "span": extracted.span,
                    "section_slug": section.get("slug"),
                    "ni43101_item": item,
                    "question_class": question_class,
                    "predicate": extracted.predicate,
                    "modality": modality,
                }
                evidence[evidence_id] = {
                    "element_id": section.get("slug"),
                    "page": page,
                    "source_kind": "text_token",
                    "source_ref": source_ref,
                    "raw_token": extracted.span,
                    "span": extracted.span,
                    "assertion_id": assertion["id"],
                    "predicate": extracted.predicate,
                    "modality": modality,
                    "input_evidence_ids": [text_evidence_id],
                }
                assertions.append(assertion)

    by_modality = Counter(item["modality"] for item in assertions)
    by_item = Counter(str(item["ni43101_item"]) for item in assertions if item.get("ni43101_item") is not None)
    telemetry = {
        "harvest.n_assertions": len(assertions),
        "harvest.n_rejected_by_textguard": rejected_by_textguard + int((getattr(extractor, "telemetry", {}) or {}).get("recall.spans_rejected_by_textguard") or 0),
        "harvest.n_llm_spans_added_over_lexical": int((getattr(extractor, "telemetry", {}) or {}).get("recall.llm_spans_added_over_lexical") or 0),
        "harvest.n_llm_degraded": int((getattr(extractor, "telemetry", {}) or {}).get("recall.llm_degraded") or 0),
        "harvest.assertions_by_modality": dict(sorted(by_modality.items())),
        "harvest.assertions_by_item": dict(sorted(by_item.items())),
    }
    return assertions, evidence, telemetry


def query_assertions(
    assertions: Iterable[dict],
    predicate: str | None = None,
    modality: str | None = None,
    question_class: str | None = None,
    predicates: set[str] | None = None,
    items: set[int] | None = None,
    limit: int | None = None,
) -> list[dict]:
    out = []
    for assertion in assertions:
        if predicate and assertion.get("predicate") != predicate:
            continue
        if predicates and assertion.get("predicate") not in predicates:
            continue
        if modality and assertion.get("modality") != modality:
            continue
        if question_class and assertion.get("question_class") != question_class:
            continue
        if items and assertion.get("ni43101_item") not in items:
            continue
        out.append(assertion)
        if limit is not None and len(out) >= limit:
            break
    return out


def classify_assertion_status(assertions: Iterable[dict], predicate: str) -> str:
    relevant = [item for item in assertions if item.get("predicate") == predicate]
    if not relevant:
        return "gap"
    modalities = {item.get("modality") for item in relevant}
    # Recall-safe precedence: an open item dominates. Without verified
    # risk<->mitigation pairing (Open Question #4), a coexisting `done` must not
    # silently retire an open `risk`/`required`/`recommended`/`planned` item.
    if modalities & {"risk", "required", "recommended", "planned"}:
        return "open"
    if "done" in modalities:
        return "retired"
    if "absent" in modalities:
        return "absent"
    return "open"


def assertion_gap_analysis(assertions: Iterable[dict], expected_predicates: Iterable[str]) -> list[dict]:
    assertion_list = list(assertions)
    return [
        {
            "predicate": predicate,
            "status": classify_assertion_status(assertion_list, predicate),
            "supporting_assertion_ids": [item.get("id") for item in assertion_list if item.get("predicate") == predicate],
        }
        for predicate in expected_predicates
    ]
