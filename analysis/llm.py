"""Pluggable LLM boundary for analysis mapping and answering.

The LLM is allowed to classify structures and map columns only. Cell values stay
owned by Python so the existing number guard remains the anti-fabrication gate.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
import time
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pipeline.util import read_json, sha256_bytes, sha256_file, write_json

from .figures import FIGURE_CLASSES, classify_figure_description
from .schemas import CONFIDENCE_LEVELS, FIELDS, TABLE_TYPES, ColumnMap, FigureClassification, MappedTable
from .tables import HeuristicTableMapper, TableMapper

DEFAULT_MODEL = "openai/gpt-5.5"
DEFAULT_BACKEND = "heuristic"
DEFAULT_MAX_CALLS_PER_RUN = 1000
_RUN_BUDGET_LOCK = threading.Lock()
_RUN_TOKENS_USED = 0


def reset_llm_run_budget() -> None:
    global _RUN_TOKENS_USED
    with _RUN_BUDGET_LOCK:
        _RUN_TOKENS_USED = 0


def _run_budget_exhausted(token_budget: int | None) -> bool:
    if token_budget is None:
        return False
    with _RUN_BUDGET_LOCK:
        return _RUN_TOKENS_USED >= token_budget


def _consume_run_budget(delta_tokens: int, token_budget: int | None) -> None:
    global _RUN_TOKENS_USED
    if token_budget is None or delta_tokens <= 0:
        return
    with _RUN_BUDGET_LOCK:
        _RUN_TOKENS_USED += delta_tokens
        exhausted = _RUN_TOKENS_USED > token_budget
    if exhausted:
        raise LLMThrottled("opencode token budget reached")

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


class LLMLegendRead(BaseModel):
    """Verbatim legend entry labels read from one cropped legend image."""

    model_config = ConfigDict(extra="forbid")

    readable: bool = False
    entries: list[str] = Field(default_factory=list)


class LLMGeoRead(BaseModel):
    """Geospatial annotations transcribed from one cropped map image.

    These are descriptive reads (CRS/scale/coordinate labels/place names) printed on
    the map. Numbers here are NOT promoted to authoritative values — numguard remains
    the only path for asserted numbers.
    """

    model_config = ConfigDict(extra="forbid")

    readable: bool = False
    crs: str | None = None
    scale: str | None = None
    coordinates: list[str] = Field(default_factory=list)
    place_names: list[str] = Field(default_factory=list)


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


class ImageReader(Protocol):
    model: str
    telemetry: dict[str, Any]

    def read_legend(self, image_path: Path | str, *, figure_id: str, component_id: str) -> LLMLegendRead: ...

    def read_geospatial(self, image_path: Path | str, *, figure_id: str, component_id: str) -> LLMGeoRead: ...


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

LEGEND_READ_SYSTEM = (
    "You transcribe text labels from a cropped map-legend image. Return each legend "
    "entry label VERBATIM as printed, across every column or sub-legend in the crop. "
    "Do not invent, translate, paraphrase, or infer entries you cannot read. If the "
    "crop has no readable legend text, set readable=false and return an empty list. "
    "These labels are descriptive geological categories only; do not compute or assert numbers."
)

GEO_READ_SYSTEM = (
    "You transcribe geospatial annotations printed on a cropped map image: the "
    "coordinate reference system (CRS / datum / projection), scale text, any coordinate "
    "grid labels, and place names. Copy text VERBATIM as printed. Do not infer or "
    "compute coordinates that are not visibly printed. If nothing is readable, set "
    "readable=false. Transcribed numbers are descriptive context only, never asserted values."
)

LEGEND_READ_USER = "List every legend entry label in this crop, verbatim, across all columns and sub-legends."
GEO_READ_USER = "Transcribe the CRS/datum, scale text, printed coordinate labels, and place names visible on this map crop. Verbatim only."


# ---------------------------------------------------------------------------
# Answer-payload compaction
#
# The raw answer inputs (full per-sample metric rows + DKI's duplicated
# gathered/selected evidence bundles) run 260k-575k tokens and overflow gpt-5.5's
# context in a single message — opencode then fails to compact and returns an
# empty error message. The answerer only needs metric *summaries*, the flags, and
# each selected-evidence item trimmed to {evidence_id, page, predicate, span} so it
# can ground citations. compact_answer_payload() builds that digest under a hard
# character budget.
# ---------------------------------------------------------------------------

ANSWER_PAYLOAD_CHAR_BUDGET = 320_000  # ~80k tokens; ample headroom under the context ceiling
_ANSWER_SPAN_CHARS = 320
_METRIC_RAW_ROWS = ("intervals",)  # raw per-sample rows superseded by curated summaries
_METRIC_ROW_CAP = 30  # cap any other row-list metric field


def _trim_span(text: Any, limit: int = _ANSWER_SPAN_CHARS) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _digest_assertion_item(item: dict) -> dict:
    out: dict[str, Any] = {}
    for key in ("id", "evidence_id", "predicate", "modality", "ni43101_item", "question_class", "page", "section_slug"):
        if item.get(key) is not None:
            out[key] = item[key]
    span = item.get("span") or item.get("object_span") or item.get("text")
    if span:
        out["span"] = _trim_span(span)
    return out


def _summarize_metrics(metrics: Any) -> Any:
    if not isinstance(metrics, dict):
        return metrics
    summary: dict[str, Any] = {}
    for name, metric in metrics.items():
        if not isinstance(metric, dict):
            summary[name] = metric
            continue
        slim: dict[str, Any] = {}
        for key, value in metric.items():
            if key in _METRIC_RAW_ROWS:
                continue
            if isinstance(value, list) and len(value) > _METRIC_ROW_CAP:
                slim[key] = value[:_METRIC_ROW_CAP]
            else:
                slim[key] = value
        summary[name] = slim
    return summary


def _enforce_char_budget(payload: dict, budget: int) -> None:
    def size() -> int:
        return len(json.dumps(payload, ensure_ascii=False))

    for _ in range(64):
        if size() <= budget:
            return
        candidates = [
            ((payload.get("grounded_digest") or {}).get("assertions"), payload.get("grounded_digest") or {}, "assertions"),
            ((payload.get("injected_knowledge") or {}).get("open_gaps"), payload.get("injected_knowledge") or {}, "open_gaps"),
            ((payload.get("injected_knowledge") or {}).get("coverage"), payload.get("injected_knowledge") or {}, "coverage"),
            ((payload.get("flags") or {}).get("flags"), payload.get("flags") or {}, "flags"),
        ]
        candidates = [(items, parent, key) for items, parent, key in candidates if isinstance(items, list) and len(items) > 1]
        if candidates:
            items, parent, key = max(candidates, key=lambda candidate: len(candidate[0]))
            parent[key] = items[: len(items) // 2]
            continue
        break
    if size() > budget:
        payload["metrics"] = {"_truncated_for_budget": True}


def compact_answer_payload(
    question: str,
    metadata: dict,
    metrics: dict,
    flags: dict,
    injected: dict,
    *,
    char_budget: int = ANSWER_PAYLOAD_CHAR_BUDGET,
) -> dict:
    """Build a bounded, citation-preserving digest of the answer inputs.

    Replaces full per-sample metric rows with curated summaries and sends the
    harvested assertion graph as a trimmed, citation-preserving digest. Guarantees
    the JSON-serialized payload is <= ``char_budget`` where possible.
    """
    selected = (injected or {}).get("selected") or {}
    coverage = []
    open_gaps = []
    for bundle in selected.values():
        if isinstance(bundle, dict):
            coverage.extend(bundle.get("coverage") or [])
            open_gaps.extend(bundle.get("gaps") or [])
    assertions = [_digest_assertion_item(item) for item in metadata.get("assertions") or []]
    by_item: dict[str, int] = {}
    by_class: dict[str, int] = {}
    for item in assertions:
        if item.get("ni43101_item") is not None:
            key = str(item["ni43101_item"])
            by_item[key] = by_item.get(key, 0) + 1
        if item.get("question_class"):
            key = str(item["question_class"])
            by_class[key] = by_class.get(key, 0) + 1
    payload = {
        "question": question,
        "metadata": {
            "report_slug": metadata.get("report_slug"),
            "project_stage": metadata.get("project_stage"),
            "location_candidates": metadata.get("location_candidates", [])[:5],
        },
        "metrics": _summarize_metrics(metrics),
        "flags": flags,
        "grounded_digest": {
            "assertions": assertions,
            "assertions_by_item": dict(sorted(by_item.items(), key=lambda pair: int(pair[0]))),
            "assertions_by_class": dict(sorted(by_class.items())),
        },
        "injected_knowledge": {
            "routing": (injected or {}).get("routing"),
            "warnings": (injected or {}).get("warnings"),
            "coverage": coverage,
            "open_gaps": open_gaps,
        },
    }
    _enforce_char_budget(payload, char_budget)
    return payload


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


def _image_file_part(image_path: Path | str) -> dict:
    """Build an opencode FilePartInput from a local image (inline base64 data URL)."""
    path = Path(image_path)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode()
    return {"type": "file", "mime": mime, "filename": path.name, "url": f"data:{mime};base64,{encoded}"}


class FakeBackend:
    """Offline backend for tests and no-key CI."""

    def __init__(
        self,
        table_mappings: dict[str, MappedTable] | None = None,
        figure_classifications: dict[str, FigureClassification] | None = None,
        span_extractions: dict[str, list[LLMExtractedSpan | dict]] | None = None,
        answer_payload: dict | None = None,
        legend_reads: dict[str, dict] | None = None,
        geo_reads: dict[str, dict] | None = None,
        model: str = DEFAULT_MODEL,
    ):
        self.table_mappings = table_mappings or {}
        self.figure_classifications = figure_classifications or {}
        self.span_extractions = span_extractions or {}
        self.answer_payload = answer_payload
        self.legend_reads = legend_reads or {}
        self.geo_reads = geo_reads or {}
        self.model = model
        self._telemetry_lock = threading.Lock()
        self.telemetry = {
            "backend": "fake",
            "model": model,
            "n_calls": 0,
            "n_vlm_calls": 0,
            "n_cache_hits": 0,
            "n_retries": 0,
            "n_degraded": 0,
            "n_throttled": 0,
        }

    def _inc(self, key: str) -> None:
        with self._telemetry_lock:
            self.telemetry[key] = self.telemetry.get(key, 0) + 1

    def cache_identity(self) -> str:
        return f"fake:{self.model}"

    def map_table(self, table: dict, rows: list[list[str]]) -> MappedTable:
        self._inc("n_calls")
        return self.table_mappings.get(table.get("id"), MappedTable("other"))

    def classify_figure(self, figure: dict, description: str) -> FigureClassification:
        self._inc("n_calls")
        return self.figure_classifications.get(figure.get("id"), FigureClassification("other", "low", "fake default"))

    def read_legend(self, image_path: Path | str, *, figure_id: str, component_id: str) -> LLMLegendRead:
        self._inc("n_calls")
        self._inc("n_vlm_calls")
        canned = self.legend_reads.get(component_id, self.legend_reads.get(figure_id, {}))
        return LLMLegendRead.model_validate(canned)

    def read_geospatial(self, image_path: Path | str, *, figure_id: str, component_id: str) -> LLMGeoRead:
        self._inc("n_calls")
        self._inc("n_vlm_calls")
        canned = self.geo_reads.get(component_id, self.geo_reads.get(figure_id, {}))
        return LLMGeoRead.model_validate(canned)

    def extract_spans(self, question_class: str, section: dict, source_text: str, taxonomy: dict) -> list[LLMExtractedSpan]:
        self._inc("n_calls")
        key = section.get("slug") or section.get("id") or question_class
        values = self.span_extractions.get(key, self.span_extractions.get(question_class, []))
        return [value if isinstance(value, LLMExtractedSpan) else LLMExtractedSpan.model_validate(value) for value in values]

    def answer(self, question: str, metadata: dict, metrics: dict, flags: dict, injected_knowledge: dict) -> dict:
        self._inc("n_calls")
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
            "n_fallback_other": 0,
            "n_confident_other_skipped": 0,
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
        primary_meta = dict(getattr(self.primary, "last_mapping_meta", {}) or {})
        if primary_meta.get("verdict") == "non_geological":
            self.telemetry["n_confident_other_skipped"] += 1
            self.last_mapping_meta = {
                "source": "primary_confident_other",
                "table_type": primary_mapping.table_type,
                "verdict": "non_geological",
                "reason": primary_meta.get("reason"),
            }
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
            if mapping.table_type == "other":
                self.telemetry["n_fallback_other"] += 1
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
        token_budget: int | None = None,
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
        self.token_budget = token_budget if token_budget is not None else _token_budget_from_env(os.environ)
        self._tokens_used = 0
        self._telemetry_lock = threading.Lock()
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
            "n_vlm_calls": 0,
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
        delta_total = 0
        if isinstance(usage, dict):
            with self._telemetry_lock:
                tokens = dict(self.telemetry.get("tokens", {}) or {})
                for key, value in usage.items():
                    if isinstance(value, int | float):
                        tokens[key] = tokens.get(key, 0) + value
                delta_total = int(usage.get("total") or sum(value for key, value in usage.items() if key in {"input", "output", "reasoning"} and isinstance(value, int | float)) or 0)
                if "total" not in usage and delta_total:
                    tokens["total"] = tokens.get("total", 0) + delta_total
                self.telemetry["tokens"] = tokens
                self._tokens_used = int(tokens.get("total") or 0)
        cost = source.get("cost")
        if isinstance(cost, (int, float)):
            with self._telemetry_lock:
                self.telemetry["cost"] = self.telemetry.get("cost", 0) + cost
        try:
            _consume_run_budget(delta_total, self.token_budget)
        except LLMThrottled:
            with self._telemetry_lock:
                self.telemetry["n_throttled"] += 1
            raise

    def _open_session(self) -> str:
        if self.session_id:
            return self.session_id
        created = self._post_json("/session", {"title": "geo-report-analysis"})
        session_id = created.get("id") or created.get("sessionID")
        if not session_id:
            raise LLMBackendError("opencode session creation returned no session id")
        return session_id

    def _call_schema(self, *, system: str, user: str, schema: type[BaseModel], cache_payload: dict, variant: str | None = None, images: list[Path | str] | None = None) -> dict:
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
        parts: list[dict] = [{"type": "text", "text": user}]
        for image in images or []:
            parts.append(_image_file_part(image))
        payload = {
            "model": {"providerID": self.provider_id, "modelID": self.model_id},
            "variant": resolved_variant,
            "system": system,
            "parts": parts,
            "format": {"type": "json_schema", "schema": schema.model_json_schema(), "retryCount": 2},
        }
        last: Exception | None = None
        for attempt in range(2):
            if _run_budget_exhausted(self.token_budget):
                with self._telemetry_lock:
                    self.telemetry["n_throttled"] += 1
                raise LLMThrottled("opencode token budget reached")
            if self.max_calls is not None and self.telemetry["n_calls"] >= self.max_calls:
                with self._telemetry_lock:
                    self.telemetry["n_throttled"] += 1
                raise LLMThrottled("opencode max-call cap reached") from last
            try:
                session_id = self._open_session()
                with self._telemetry_lock:
                    self.telemetry["n_calls"] += 1
                if images:
                    with self._telemetry_lock:
                        self.telemetry["n_vlm_calls"] = self.telemetry.get("n_vlm_calls", 0) + 1
                response = self._post_json(f"/session/{session_id}/message", payload)
                structured = self._extract_structured(response)
                model_obj = schema.model_validate(structured)
                data = model_obj.model_dump()
                write_json(cache_file, data)
                return data
            except LLMThrottled:
                raise
            except Exception as exc:
                last = exc
                if attempt == 0:
                    with self._telemetry_lock:
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

    def read_legend(self, image_path: Path | str, *, figure_id: str, component_id: str) -> LLMLegendRead:
        cache_payload = {
            "task": "read_legend",
            "figure_id": figure_id,
            "component_id": component_id,
            "source_sha256": sha256_file(Path(image_path)),
        }
        data = self._call_schema(
            system=LEGEND_READ_SYSTEM,
            user=LEGEND_READ_USER,
            schema=LLMLegendRead,
            cache_payload=cache_payload,
            variant=self.variant,
            images=[image_path],
        )
        return LLMLegendRead.model_validate(data)

    def read_geospatial(self, image_path: Path | str, *, figure_id: str, component_id: str) -> LLMGeoRead:
        cache_payload = {
            "task": "read_geospatial",
            "figure_id": figure_id,
            "component_id": component_id,
            "source_sha256": sha256_file(Path(image_path)),
        }
        data = self._call_schema(
            system=GEO_READ_SYSTEM,
            user=GEO_READ_USER,
            schema=LLMGeoRead,
            cache_payload=cache_payload,
            variant=self.variant,
            images=[image_path],
        )
        return LLMGeoRead.model_validate(data)

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
        # Compact to a bounded, citation-preserving digest: the raw metrics+DKI blob
        # overflows the model context in one message (see compact_answer_payload).
        prompt_payload = compact_answer_payload(question, metadata, metrics, flags, injected_knowledge)
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
    if raw is None or raw == "":
        return DEFAULT_MAX_CALLS_PER_RUN
    try:
        value = int(raw)
    except ValueError as exc:
        raise BackendConfigError("ANALYSIS_LLM_MAX_CALLS_PER_RUN must be an integer") from exc
    return max(0, value)


def _token_budget_from_env(env: dict[str, str]) -> int | None:
    raw = env.get("ANALYSIS_LLM_TOKEN_BUDGET_PER_RUN")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise BackendConfigError("ANALYSIS_LLM_TOKEN_BUDGET_PER_RUN must be an integer") from exc
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
                token_budget=_token_budget_from_env(env),
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
                token_budget=_token_budget_from_env(env),
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
                token_budget=_token_budget_from_env(env),
            )
        except BackendConfigError:
            return None
    raise BackendConfigError("ANALYSIS_LLM_BACKEND must be one of heuristic, fake, opencode")


def select_image_reader(cache_dir: Path | str, env: dict[str, str] | None = None) -> ImageReader | None:
    """Select a VLM map-crop reader (legend/geospatial), or None to skip the read pass.

    Gated by ANALYSIS_LLM_BACKEND like the other seams, plus an ANALYSIS_MAP_VLM=off
    escape hatch so the deterministic crop slice can run without any image reads.
    """
    env = env or os.environ
    if env.get("ANALYSIS_MAP_VLM", "on").strip().lower() in ("off", "0", "false", "no"):
        return None
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
                token_budget=_token_budget_from_env(env),
            )
        except BackendConfigError:
            return None
    raise BackendConfigError("ANALYSIS_LLM_BACKEND must be one of heuristic, fake, opencode")
