"""Span extraction seam for prose assertion recall."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol

from .llm import BackendConfigError, FakeBackend, LLMBackend, LLMBackendError, LLMExtractedSpan, OpencodeBackend, _max_calls_from_env
from .taxonomy import matched_predicates
from .textguard import text_backed


class SpanExtractor(Protocol):
    telemetry: dict

    def extract_spans(self, question_class: str, section: dict, source_text: str, taxonomy: dict) -> list[LLMExtractedSpan]: ...


def candidate_spans(source_text: str) -> list[str]:
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


def infer_modality(span: str, predicate: str | None = None) -> str:
    text = span.casefold()
    if predicate and any(term in text for term in ("no ", "not ", "none", "absent", "no significant", "no currently known", "no known")):
        return "absent"
    if any(term in text for term in ("risk", "uncertain", "uncertainty", "significant factors", "adverse")):
        return "risk"
    if any(term in text for term in ("recommend", "recommended", "recommendation")):
        return "recommended"
    if any(term in text for term in ("required", "must", "should", "requires", "permit", "approval", "licence", "license")):
        return "required"
    if any(term in text for term in ("planned", "proposed", "will", "ongoing", "next phase")):
        return "planned"
    if any(term in text for term in ("no ", "not ", "none", "absent")) and predicate:
        return "absent"
    return "done"


class HeuristicSpanExtractor:
    """Lexical floor that only emits source-backed spans with taxonomy keywords."""

    telemetry: dict[str, int]

    def __init__(self) -> None:
        self.telemetry = {
            "recall.llm_spans_added_over_lexical": 0,
            "recall.llm_degraded": 0,
            "recall.spans_rejected_by_textguard": 0,
        }

    def reset_telemetry(self) -> None:
        for key in list(self.telemetry):
            self.telemetry[key] = 0

    def extract_spans(self, question_class: str, section: dict, source_text: str, taxonomy: dict) -> list[LLMExtractedSpan]:
        spans: list[LLMExtractedSpan] = []
        for span in candidate_spans(source_text):
            matches = matched_predicates(question_class, span)
            if not matches:
                continue
            predicate = next(iter(matches))
            spans.append(LLMExtractedSpan(
                span=span,
                predicate=predicate,
                modality=infer_modality(span, predicate),
                ni43101_item=_section_item(section),
                confidence="medium",
            ))
        return spans


class LLMSpanExtractor:
    """Heuristic-first extractor that unions in verbatim LLM spans."""

    def __init__(self, primary: HeuristicSpanExtractor, fallback: LLMBackend, max_calls: int | None = None):
        self.primary = primary
        self.fallback = fallback
        self.max_calls = max_calls
        self._fallback_attempts = 0
        self.telemetry = {
            "recall.llm_spans_added_over_lexical": 0,
            "recall.llm_degraded": 0,
            "recall.spans_rejected_by_textguard": 0,
        }

    def reset_telemetry(self) -> None:
        for key in list(self.telemetry):
            self.telemetry[key] = 0

    def extract_spans(self, question_class: str, section: dict, source_text: str, taxonomy: dict) -> list[LLMExtractedSpan]:
        primary_spans = self.primary.extract_spans(question_class, section, source_text, taxonomy)
        seen = {_span_key(item.span) for item in primary_spans}
        if self.max_calls is not None and self._fallback_attempts >= self.max_calls:
            return primary_spans
        self._fallback_attempts += 1
        try:
            llm_spans = self.fallback.extract_spans(question_class, section, source_text, taxonomy)
        except LLMBackendError:
            self.telemetry["recall.llm_degraded"] += 1
            return primary_spans
        except Exception:
            self.telemetry["recall.llm_degraded"] += 1
            return primary_spans

        merged = list(primary_spans)
        for span in llm_spans:
            key = _span_key(span.span)
            if key in seen:
                continue
            if not text_backed(span.span, source_text):
                self.telemetry["recall.spans_rejected_by_textguard"] += 1
                continue
            seen.add(key)
            merged.append(span)
            self.telemetry["recall.llm_spans_added_over_lexical"] += 1
        return merged


def select_span_extractor(cache_dir: Path | str, env: dict[str, str] | None = None):
    env = env or os.environ
    backend = env.get("ANALYSIS_LLM_BACKEND", "heuristic").strip().lower()
    primary = HeuristicSpanExtractor()
    if backend in ("", "heuristic"):
        return primary
    model = env.get("ANALYSIS_LLM_MODEL", "openai/gpt-5.5")
    max_calls = _max_calls_from_env(env)
    if backend == "fake":
        return LLMSpanExtractor(primary, FakeBackend(model=model), max_calls=max_calls)
    if backend == "opencode":
        try:
            fallback = OpencodeBackend(cache_dir=cache_dir, server_url=env.get("OPENCODE_SERVER_URL"), model=model, max_calls=max_calls)
        except BackendConfigError:
            return primary
        return LLMSpanExtractor(primary, fallback, max_calls=max_calls)
    raise BackendConfigError("ANALYSIS_LLM_BACKEND must be one of heuristic, fake, opencode")


def _span_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).casefold().strip()


def _section_item(section: dict) -> int | None:
    for value in (section.get("number"), section.get("title"), section.get("slug")):
        match = re.search(r"(?<!\d)(\d{1,2})(?:\.\d+)?(?!\d)", str(value or "").replace("_", " "))
        if match:
            item = int(match.group(1))
            if 1 <= item <= 28:
                return item
    return None
