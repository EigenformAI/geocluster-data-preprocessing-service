"""Pluggable LLM boundary for analysis mapping and answering.

The LLM is allowed to classify structures and map columns only. Cell values stay
owned by Python so the existing number guard remains the anti-fabrication gate.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pipeline.util import read_json, sha256_bytes, write_json

from .figures import FIGURE_CLASSES, classify_figure_description
from .schemas import CONFIDENCE_LEVELS, FIELDS, TABLE_TYPES, ColumnMap, FigureClassification, MappedTable
from .tables import HeuristicTableMapper, TableMapper

DEFAULT_MODEL = "openai/gpt-5.5"
DEFAULT_BACKEND = "heuristic"

TableType = Literal[
    "drillhole_collar",
    "assay_interval",
    "resource_estimate",
    "qaqc_crm",
    "tenure",
    "drill_history",
    "budget",
    "other",
]
CanonicalField = Literal[
    "hole_id",
    "easting",
    "northing",
    "elevation_m",
    "azimuth_deg",
    "dip_deg",
    "eoh_m",
    "zone",
    "from_m",
    "to_m",
    "interval_m",
    "qualifier",
    "au_gpt",
    "ag_gpt",
    "cu_pct",
    "property",
    "deposit",
    "mining_method",
    "category",
    "tonnage",
    "grade_gpt",
    "contained",
    "cutoff",
    "crm_id",
    "n_submitted",
    "certified_value",
    "std_dev",
    "n_failures",
    "mean_measured",
    "pct_passing",
    "claim_id",
    "legacy_claims",
    "latitude",
    "longitude",
    "area",
    "holder",
    "status",
    "issue_date",
    "expiry_date",
    "target_area",
    "year",
    "company",
    "program",
    "n_holes",
    "metres_drilled",
    "phase",
    "item",
    "amount",
    "currency",
    "ignore",
]
FigureClass = Literal[
    "geological_map",
    "location_map",
    "claim_map",
    "drill_plan",
    "cross_section",
    "stratigraphic_column",
    "chart",
    "photo",
    "other",
]
Confidence = Literal["high", "medium", "low", "none"]
SpanModality = Literal["done", "planned", "required", "recommended", "risk", "absent"]


class BackendConfigError(RuntimeError):
    """Raised for missing or invalid backend configuration."""


class LLMBackendError(RuntimeError):
    """Raised for runtime backend failures that can degrade to heuristics."""


class LLMThrottled(LLMBackendError):
    """Raised when a per-run LLM cap has been reached."""


class LLMColumnMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_index: int = Field(ge=0)
    canonical_field: CanonicalField
    unit: str | None = None

    def to_dataclass(self) -> ColumnMap:
        return ColumnMap(self.source_index, self.canonical_field, self.unit)


class LLMMappedTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_type: TableType
    columns: list[LLMColumnMap] = Field(default_factory=list)
    carry_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_vocab(self) -> "LLMMappedTable":
        if self.table_type not in TABLE_TYPES:
            raise ValueError(f"table_type must be one of {TABLE_TYPES}")
        fields = {field.name for field in FIELDS.get(self.table_type, [])}
        for column in self.columns:
            if column.canonical_field != "ignore" and column.canonical_field not in fields:
                raise ValueError(f"canonical_field {column.canonical_field!r} is invalid for {self.table_type}")
        for carry_field in self.carry_fields:
            if carry_field not in fields:
                raise ValueError(f"carry_field {carry_field!r} is invalid for {self.table_type}")
        return self

    def to_dataclass(self) -> MappedTable:
        fields = {field.name for field in FIELDS.get(self.table_type, [])}
        columns = [column.to_dataclass() for column in self.columns if column.canonical_field in fields]
        carry_fields = [field for field in self.carry_fields if field in fields]
        return MappedTable(self.table_type, columns, carry_fields)


class LLMFigureClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    figure_class: FigureClass
    confidence: Confidence
    reason: str

    @model_validator(mode="after")
    def validate_vocab(self) -> "LLMFigureClassification":
        if self.figure_class not in FIGURE_CLASSES:
            raise ValueError(f"figure_class must be one of {FIGURE_CLASSES}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {CONFIDENCE_LEVELS}")
        return self

    def to_dataclass(self) -> FigureClassification:
        return FigureClassification(self.figure_class, self.confidence, self.reason)


class LLMClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    evidence_origin: str = "report"
    citations: list[dict[str, str]] = Field(default_factory=list)
    confidence: Confidence = "medium"

    @model_validator(mode="after")
    def validate_confidence(self) -> "LLMClaim":
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {CONFIDENCE_LEVELS}")
        return self


class LLMExtractedSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    span: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    modality: SpanModality = "done"
    ni43101_item: int | None = Field(default=None, ge=1, le=28)
    entity_hint: str | None = None
    confidence: Confidence = "medium"

    @model_validator(mode="after")
    def validate_confidence(self) -> "LLMExtractedSpan":
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence must be one of {CONFIDENCE_LEVELS}")
        return self


class LLMExtractedSpans(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spans: list[LLMExtractedSpan] = Field(default_factory=list)


class LLMAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str
    reason: str
    claims: list[LLMClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    injected_knowledge: dict[str, Any] = Field(default_factory=dict)
    report_context: dict[str, Any] = Field(default_factory=dict)


class LLMBackend(Protocol):
    model: str
    telemetry: dict[str, Any]

    def map_table(self, table: dict, rows: list[list[str]]) -> MappedTable: ...

    def classify_figure(self, figure: dict, description: str) -> FigureClassification: ...

    def extract_spans(self, question_class: str, section: dict, source_text: str, taxonomy: dict) -> list[LLMExtractedSpan]: ...

    def answer(self, question: str, metadata: dict, metrics: dict, flags: dict, injected_knowledge: dict) -> dict: ...


class FigureClassifier(Protocol):
    def classify_figure(self, figure: dict, description: str) -> FigureClassification: ...


class Answerer(Protocol):
    def answer(self, question: str, metadata: dict, metrics: dict, flags: dict, injected_knowledge: dict) -> dict: ...


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _schema_bytes(model: type[BaseModel]) -> bytes:
    return _json_bytes(model.model_json_schema())


def mapper_cache_key(mapper: TableMapper) -> str:
    identity = getattr(mapper, "cache_identity", None)
    if callable(identity):
        return str(identity())
    return f"{mapper.__class__.__module__}.{mapper.__class__.__qualname__}"


_VOCAB = "\n".join(f"- {table_type}: {', '.join(field.name for field in fields)}" for table_type, fields in FIELDS.items())

TABLE_MAPPING_SYSTEM = (
    "You map a CSV table extracted from an NI 43-101 mineral-exploration report into "
    "a typed schema. You DO NOT read or transcribe values; you only choose the table "
    "type and assign each source column to a canonical field. Python reads the cells.\n\n"
    "Table types and their canonical fields:\n"
    + _VOCAB
    + "\n\nRules:\n"
    "- Pick the single best table_type. If the table is not geological data, return "
    'table_type="other" with empty columns.\n'
    "- For each data column return {source_index, canonical_field, unit}. Use a field "
    "from the chosen type only, or \"ignore\". `unit` is the raw unit shown in the "
    "header if any, else null.\n"
    "- carry_fields are identifier fields whose value continues downward when a cell is blank."
)

FIGURE_CLASSIFICATION_SYSTEM = (
    "Classify one extracted report figure from its description prose. Return only the "
    "taxonomy class, confidence, and a short reason. Do not infer map coordinates."
)

SPAN_EXTRACTION_SYSTEM = (
    "Extract qualitative NI 43-101 report assertions from one section. Return only "
    "VERBATIM spans copied from the supplied section text. Do not paraphrase, "
    "summarize, or add facts. Assign each span a predicate from the provided "
    "taxonomy and a modality in done, planned, required, recommended, risk, or absent."
)

ANSWER_SYSTEM = (
    "Answer the QP review question using only the provided metadata, metrics, flags, "
    "and injected knowledge. Every quantitative claim must cite evidence ids already "
    "present in the payload; do not invent numbers or citations."
)


def _preview_table(rows: list[list[str]], n: int = 8) -> str:
    header = rows[0] if rows else []
    lines = ["Columns (index: header):"]
    lines += [f"{index}: {header}" for index, header in enumerate(header)]
    lines.append("Sample rows:")
    for row in rows[1 : 1 + n]:
        lines.append(" | ".join(row))
    return "\n".join(lines)


def _cache_key(*, backend: str, model: str, schema: type[BaseModel], system: str, payload: Any, source_sha256: str | None, variant: str = "") -> str:
    return sha256_bytes(
        f"backend:{backend}".encode(),
        f"model:{model}".encode(),
        f"variant:{variant}".encode(),
        f"source:{source_sha256 or ''}".encode(),
        system.encode(),
        _schema_bytes(schema),
        _json_bytes(payload),
    )


class FakeBackend:
    """Offline backend for tests and no-key CI."""

    def __init__(
        self,
        table_mappings: dict[str, MappedTable] | None = None,
        figure_classifications: dict[str, FigureClassification] | None = None,
        span_extractions: dict[str, list[LLMExtractedSpan | dict]] | None = None,
        answer_payload: dict | None = None,
        model: str = DEFAULT_MODEL,
    ):
        self.table_mappings = table_mappings or {}
        self.figure_classifications = figure_classifications or {}
        self.span_extractions = span_extractions or {}
        self.answer_payload = answer_payload
        self.model = model
        self.telemetry = {
            "backend": "fake",
            "model": model,
            "n_calls": 0,
            "n_cache_hits": 0,
            "n_retries": 0,
            "n_degraded": 0,
            "n_throttled": 0,
        }

    def cache_identity(self) -> str:
        return f"fake:{self.model}"

    def map_table(self, table: dict, rows: list[list[str]]) -> MappedTable:
        self.telemetry["n_calls"] += 1
        return self.table_mappings.get(table.get("id"), MappedTable("other"))

    def classify_figure(self, figure: dict, description: str) -> FigureClassification:
        self.telemetry["n_calls"] += 1
        return self.figure_classifications.get(figure.get("id"), FigureClassification("other", "low", "fake default"))

    def extract_spans(self, question_class: str, section: dict, source_text: str, taxonomy: dict) -> list[LLMExtractedSpan]:
        self.telemetry["n_calls"] += 1
        key = section.get("slug") or section.get("id") or question_class
        values = self.span_extractions.get(key, self.span_extractions.get(question_class, []))
        return [value if isinstance(value, LLMExtractedSpan) else LLMExtractedSpan.model_validate(value) for value in values]

    def answer(self, question: str, metadata: dict, metrics: dict, flags: dict, injected_knowledge: dict) -> dict:
        self.telemetry["n_calls"] += 1
        if self.answer_payload is not None:
            return dict(self.answer_payload)
        return {
            "question": question,
            "answer": "Fake backend answer.",
            "reason": "Offline fake backend.",
            "claims": [],
            "limitations": [],
            "injected_knowledge": injected_knowledge.get("selected", {}),
            "report_context": {"project": metadata.get("report_slug"), "stage": metadata.get("project_stage")},
        }


class FallbackMapper:
    """Heuristic-first table mapper with a capped LLM fallback."""

    def __init__(self, primary: TableMapper, fallback: LLMBackend, max_calls: int | None = None):
        self.primary = primary
        self.fallback = fallback
        self.max_calls = max_calls
        self._fallback_attempts = 0
        self.last_mapping_meta: dict[str, Any] = {}
        self.telemetry = {
            "backend": getattr(fallback, "telemetry", {}).get("backend", fallback.__class__.__name__),
            "model": getattr(fallback, "model", DEFAULT_MODEL),
            "n_primary": 0,
            "n_fallback_calls": 0,
            "n_degraded": 0,
            "n_throttled": 0,
        }

    def cache_identity(self) -> str:
        return "|".join([
            "fallback",
            mapper_cache_key(self.primary),
            getattr(self.fallback, "cache_identity", lambda: f"{self.fallback.__class__.__module__}.{self.fallback.__class__.__qualname__}")(),
        ])

    def map_table(self, table: dict, rows: list[list[str]]) -> MappedTable:
        primary_mapping = self.primary.map_table(table, rows)
        self.telemetry["n_primary"] += 1
        if primary_mapping.table_type != "other":
            self.last_mapping_meta = {"source": "primary", "table_type": primary_mapping.table_type}
            return primary_mapping
        if self.max_calls is not None and self._fallback_attempts >= self.max_calls:
            self.telemetry["n_throttled"] += 1
            self.last_mapping_meta = {"source": "throttled", "table_type": primary_mapping.table_type}
            return primary_mapping
        self._fallback_attempts += 1
        self.telemetry["n_fallback_calls"] += 1
        try:
            mapping = self.fallback.map_table(table, rows)
            self.last_mapping_meta = {
                "source": "fallback",
                "backend": getattr(self.fallback, "telemetry", {}).get("backend", self.fallback.__class__.__name__),
                "model": getattr(self.fallback, "model", DEFAULT_MODEL),
                "table_type": mapping.table_type,
            }
            return mapping
        except LLMThrottled as exc:
            self.telemetry["n_throttled"] += 1
            self.last_mapping_meta = {"source": "throttled", "error": type(exc).__name__, "table_type": primary_mapping.table_type}
            return primary_mapping
        except Exception as exc:
            self.telemetry["n_degraded"] += 1
            self.last_mapping_meta = {"source": "degraded", "error": type(exc).__name__, "table_type": primary_mapping.table_type}
            return primary_mapping


class OpencodeBackend:
    """Structured-output backend for an externally managed `opencode serve`."""

    backend_name = "opencode"

    def __init__(
        self,
        cache_dir: Path | str,
        server_url: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout_s: float = 120.0,
        max_calls: int | None = None,
        session_id: str | None = None,
        variant: str | None = None,
        heavy_variant: str | None = None,
    ):
        server_url = server_url or os.environ.get("OPENCODE_SERVER_URL")
        if not server_url:
            raise BackendConfigError("OPENCODE_SERVER_URL is required when ANALYSIS_LLM_BACKEND=opencode")
        self.cache_dir = Path(cache_dir)
        self.server_url = server_url.rstrip("/")
        self.model = model
        if "/" in model:
            self.provider_id, self.model_id = model.split("/", 1)
        else:
            self.provider_id, self.model_id = "openai", model
        self.timeout_s = timeout_s
        self.max_calls = max_calls
        # Reuse a caller-pinned server session when given; otherwise each structured
        # call opens a fresh, isolated session. opencode messages accumulate
        # conversation state, which we must not share across independent extractions.
        self.session_id = session_id
        # Reasoning effort (opencode model "variant"): mechanical extraction runs at
        # the light default; the answer/synthesis step uses the heavier one.
        self.variant = variant or os.environ.get("ANALYSIS_LLM_VARIANT") or "low"
        self.heavy_variant = heavy_variant or os.environ.get("ANALYSIS_LLM_HEAVY_VARIANT") or "medium"
        self.telemetry = {
            "backend": self.backend_name,
            "model": model,
            "variant": self.variant,
            "n_calls": 0,
            "n_cache_hits": 0,
            "n_retries": 0,
            "n_degraded": 0,
            "n_throttled": 0,
            "cost": 0,
            "tokens": {},
        }

    def cache_identity(self) -> str:
        return "|".join([
            self.backend_name,
            self.model,
            sha256_bytes(TABLE_MAPPING_SYSTEM.encode(), _schema_bytes(LLMMappedTable)),
        ])

    def _cache_file(self, key: str) -> Path:
        return self.cache_dir / self.backend_name / f"{key}.json"

    def _post_json(self, path: str, payload: dict) -> dict:
        request = Request(
            f"{self.server_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                body = response.read().decode()
        except HTTPError as exc:
            raise LLMBackendError(f"opencode HTTP {exc.code}") from exc
        except URLError as exc:
            raise LLMBackendError(f"opencode connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMBackendError("opencode request timed out") from exc
        try:
            return json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            raise LLMBackendError("opencode returned malformed JSON") from exc

    def _extract_structured(self, response: Any) -> dict:
        if isinstance(response, dict):
            self._merge_usage(response)
            # opencode 1.17 enforces json_schema via a forced StructuredOutput tool
            # call; the validated object is published at info.structured and mirrored
            # in the corresponding tool part.
            info = response.get("info")
            if isinstance(info, dict) and isinstance(info.get("structured"), dict):
                return info["structured"]
            parts = response.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool" and part.get("tool") == "StructuredOutput":
                        state = part.get("state")
                        if isinstance(state, dict) and isinstance(state.get("input"), dict):
                            return state["input"]
            for key in ("structuredContent", "output", "result", "data", "content"):
                value = response.get(key)
                if isinstance(value, dict):
                    return value
                if isinstance(value, str):
                    try:
                        parsed = json.loads(value)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        return parsed
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        try:
                            parsed = json.loads(part["text"])
                        except json.JSONDecodeError:
                            continue
                        if isinstance(parsed, dict):
                            return parsed
        if isinstance(response, str):
            try:
                parsed = json.loads(response)
            except json.JSONDecodeError as exc:
                raise LLMBackendError("opencode response text was not JSON") from exc
            if isinstance(parsed, dict):
                return parsed
        raise LLMBackendError("opencode response did not contain structured JSON")

    def _merge_usage(self, response: dict) -> None:
        info = response.get("info")
        source = info if isinstance(info, dict) else response
        usage = source.get("tokens") or source.get("usage")
        if isinstance(usage, dict):
            self.telemetry["tokens"] = {**self.telemetry.get("tokens", {}), **usage}
        cost = source.get("cost")
        if isinstance(cost, (int, float)):
            self.telemetry["cost"] = cost

    def _open_session(self) -> str:
        if self.session_id:
            return self.session_id
        created = self._post_json("/session", {"title": "geo-report-analysis"})
        session_id = created.get("id") or created.get("sessionID")
        if not session_id:
            raise LLMBackendError("opencode session creation returned no session id")
        return session_id

    def _call_schema(self, *, system: str, user: str, schema: type[BaseModel], cache_payload: dict, variant: str | None = None) -> dict:
        resolved_variant = variant or self.variant
        source_sha256 = cache_payload.get("source_sha256")
        key = _cache_key(
            backend=self.backend_name,
            model=self.model,
            schema=schema,
            system=system,
            payload=cache_payload,
            source_sha256=source_sha256,
            variant=resolved_variant,
        )
        cache_file = self._cache_file(key)
        cached = read_json(cache_file, default=None)
        if cached is not None:
            self.telemetry["n_cache_hits"] += 1
            return cached
        payload = {
            "model": {"providerID": self.provider_id, "modelID": self.model_id},
            "variant": resolved_variant,
            "system": system,
            "parts": [{"type": "text", "text": user}],
            "format": {"type": "json_schema", "schema": schema.model_json_schema(), "retryCount": 2},
        }
        last: Exception | None = None
        for attempt in range(2):
            if self.max_calls is not None and self.telemetry["n_calls"] >= self.max_calls:
                self.telemetry["n_throttled"] += 1
                raise LLMThrottled("opencode max-call cap reached") from last
            try:
                session_id = self._open_session()
                self.telemetry["n_calls"] += 1
                response = self._post_json(f"/session/{session_id}/message", payload)
                structured = self._extract_structured(response)
                model_obj = schema.model_validate(structured)
                data = model_obj.model_dump()
                write_json(cache_file, data)
                return data
            except Exception as exc:
                last = exc
                if attempt == 0:
                    self.telemetry["n_retries"] += 1
                    time.sleep(0.1)
                    continue
                break
        raise LLMBackendError(f"opencode structured call failed: {last}") from last

    def map_table(self, table: dict, rows: list[list[str]]) -> MappedTable:
        preview = _preview_table(rows)
        cache_payload = {
            "task": "map_table",
            "table_id": table.get("id"),
            "csv": table.get("csv"),
            "source_sha256": table.get("source_sha256"),
            "preview": preview,
        }
        data = self._call_schema(system=TABLE_MAPPING_SYSTEM, user=preview, schema=LLMMappedTable, cache_payload=cache_payload)
        return LLMMappedTable.model_validate(data).to_dataclass()

    def classify_figure(self, figure: dict, description: str) -> FigureClassification:
        cache_payload = {
            "task": "classify_figure",
            "figure_id": figure.get("id"),
            "description": description,
        }
        data = self._call_schema(
            system=FIGURE_CLASSIFICATION_SYSTEM,
            user=description,
            schema=LLMFigureClassification,
            cache_payload=cache_payload,
        )
        return LLMFigureClassification.model_validate(data).to_dataclass()

    def extract_spans(self, question_class: str, section: dict, source_text: str, taxonomy: dict) -> list[LLMExtractedSpan]:
        section_payload = {
            "slug": section.get("slug"),
            "number": section.get("number"),
            "title": section.get("title"),
            "page_start": section.get("page_start"),
            "page_end": section.get("page_end"),
        }
        prompt_payload = {
            "question_class": question_class,
            "section": section_payload,
            "taxonomy": {
                "predicates": taxonomy.get("predicates", {}),
                "items": taxonomy.get("items", []),
            },
            "section_text": source_text,
        }
        data = self._call_schema(
            system=SPAN_EXTRACTION_SYSTEM,
            user=json.dumps(prompt_payload, indent=2, ensure_ascii=False),
            schema=LLMExtractedSpans,
            cache_payload={
                "task": "extract_spans",
                "question_class": question_class,
                "section": section_payload,
                "source_sha256": sha256_bytes(source_text.encode()),
                "taxonomy": prompt_payload["taxonomy"],
            },
        )
        return list(LLMExtractedSpans.model_validate(data).spans)

    def answer(self, question: str, metadata: dict, metrics: dict, flags: dict, injected_knowledge: dict) -> dict:
        prompt_payload = {
            "question": question,
            "metadata": {
                "report_slug": metadata.get("report_slug"),
                "project_stage": metadata.get("project_stage"),
                "location_candidates": metadata.get("location_candidates", [])[:5],
            },
            "metrics": metrics,
            "flags": flags,
            "injected_knowledge": injected_knowledge,
        }
        data = self._call_schema(
            system=ANSWER_SYSTEM,
            user=json.dumps(prompt_payload, indent=2, ensure_ascii=False),
            schema=LLMAnswer,
            cache_payload={"task": "answer", **prompt_payload},
            variant=self.heavy_variant,
        )
        return data


class HeuristicFigureClassifier:
    """Local figure classifier used for offline/default runs."""

    def classify_figure(self, figure: dict, description: str) -> FigureClassification:
        return classify_figure_description(description)


class FallbackFigureClassifier:
    """Heuristic-first figure classifier with LLM fallback for uncertain classes."""

    def __init__(self, primary: FigureClassifier, fallback: LLMBackend, max_calls: int | None = None):
        self.primary = primary
        self.fallback = fallback
        self.max_calls = max_calls
        self._fallback_attempts = 0
        self.telemetry = {
            "backend": getattr(fallback, "telemetry", {}).get("backend", fallback.__class__.__name__),
            "model": getattr(fallback, "model", DEFAULT_MODEL),
            "n_primary": 0,
            "n_fallback_calls": 0,
            "n_degraded": 0,
            "n_throttled": 0,
        }

    def classify_figure(self, figure: dict, description: str) -> FigureClassification:
        classification = self.primary.classify_figure(figure, description)
        self.telemetry["n_primary"] += 1
        if classification.figure_class != "other" and classification.confidence not in {"low", "none"}:
            return classification
        if self.max_calls is not None and self._fallback_attempts >= self.max_calls:
            self.telemetry["n_throttled"] += 1
            return classification
        self._fallback_attempts += 1
        self.telemetry["n_fallback_calls"] += 1
        try:
            return self.fallback.classify_figure(figure, description)
        except LLMThrottled:
            self.telemetry["n_throttled"] += 1
            return classification
        except Exception:
            self.telemetry["n_degraded"] += 1
            return classification


def _max_calls_from_env(env: dict[str, str]) -> int | None:
    raw = env.get("ANALYSIS_LLM_MAX_CALLS_PER_RUN")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise BackendConfigError("ANALYSIS_LLM_MAX_CALLS_PER_RUN must be an integer") from exc
    return max(0, value)


def select_table_mapper(cache_dir: Path | str, env: dict[str, str] | None = None) -> TableMapper:
    env = env or os.environ
    backend = env.get("ANALYSIS_LLM_BACKEND", DEFAULT_BACKEND).strip().lower()
    model = env.get("ANALYSIS_LLM_MODEL", DEFAULT_MODEL)
    primary = HeuristicTableMapper()
    if backend in ("", "heuristic"):
        return primary
    max_calls = _max_calls_from_env(env)
    if backend == "fake":
        return FallbackMapper(primary=primary, fallback=FakeBackend(model=model), max_calls=max_calls)
    if backend == "opencode":
        try:
            fallback = OpencodeBackend(
                cache_dir=cache_dir,
                server_url=env.get("OPENCODE_SERVER_URL"),
                model=model,
                max_calls=max_calls,
            )
        except BackendConfigError:
            return primary
        return FallbackMapper(primary=primary, fallback=fallback, max_calls=max_calls)
    raise BackendConfigError("ANALYSIS_LLM_BACKEND must be one of heuristic, fake, opencode")


def select_figure_classifier(cache_dir: Path | str, env: dict[str, str] | None = None) -> FigureClassifier:
    env = env or os.environ
    backend = env.get("ANALYSIS_LLM_BACKEND", DEFAULT_BACKEND).strip().lower()
    model = env.get("ANALYSIS_LLM_MODEL", DEFAULT_MODEL)
    primary = HeuristicFigureClassifier()
    if backend in ("", "heuristic"):
        return primary
    max_calls = _max_calls_from_env(env)
    if backend == "fake":
        return FallbackFigureClassifier(primary=primary, fallback=FakeBackend(model=model), max_calls=max_calls)
    if backend == "opencode":
        try:
            fallback = OpencodeBackend(
                cache_dir=cache_dir,
                server_url=env.get("OPENCODE_SERVER_URL"),
                model=model,
                max_calls=max_calls,
            )
        except BackendConfigError:
            return primary
        return FallbackFigureClassifier(primary=primary, fallback=fallback, max_calls=max_calls)
    raise BackendConfigError("ANALYSIS_LLM_BACKEND must be one of heuristic, fake, opencode")


def select_answerer(cache_dir: Path | str, env: dict[str, str] | None = None) -> Answerer | None:
    env = env or os.environ
    backend = env.get("ANALYSIS_LLM_BACKEND", DEFAULT_BACKEND).strip().lower()
    model = env.get("ANALYSIS_LLM_MODEL", DEFAULT_MODEL)
    if backend in ("", "heuristic"):
        return None
    if backend == "fake":
        return FakeBackend(model=model)
    if backend == "opencode":
        try:
            return OpencodeBackend(
                cache_dir=cache_dir,
                server_url=env.get("OPENCODE_SERVER_URL"),
                model=model,
                max_calls=_max_calls_from_env(env),
            )
        except BackendConfigError:
            return None
    raise BackendConfigError("ANALYSIS_LLM_BACKEND must be one of heuristic, fake, opencode")
