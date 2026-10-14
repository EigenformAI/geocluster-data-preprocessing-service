"""Small worker contract: JSON values and generation-relative file paths only.

Format adapters implement extract(snapshot, profile, output_dir, context) and
return the fields of Extraction below. The coordinator owns identity, hashing,
validation, persistence, and publication. No parser writes the job database.
"""
import copy
from pathlib import Path
from typing import Any, Protocol, TypedDict

from preprocessing.table_recovery import DEFAULT_TABLE_RECOVERY


class Extraction(TypedDict):
    coverage: dict
    stages: list[dict]
    products: list[dict]  # path, kind, mediaType; optional id, schemaVersion
    tables: list[dict]  # id, rows, columns, typedPath, csvPath, source locator
    figures: list[dict]
    findings: list[dict]
    provenance: dict


class ExtractionContext(Protocol):
    resume: dict | None

    def work_remaining(self) -> float:
        """Seconds left in the coordinator-owned PDF work window."""
        ...

    def event(self, name: str, **data) -> None:
        """Emit compact table.* recovery events without document cell content."""
        ...

    def progress(self, stage_id: str, completed_units: int, total_units: int | None) -> None:
        """Report phase counts; the coordinator owns labels and timestamps."""
        ...

    def model(self, operation: dict, input_refs: list[dict]) -> dict[str, Any]:
        """Synchronous worker RPC; coordinator records intent before dispatch.

        operation: kind, sourceUnit, prompt. input_refs: path, mediaType for
        staging images. Response: text, requestedModel, returnedModel, usage,
        sessionId. Raises ServiceError when unavailable/failed.
        """
        ...


class FormatAdapter(Protocol):
    extensions: tuple[str, ...]
    version: str

    def extract(self, snapshot: dict, profile: dict, output_dir: Path,
                context: ExtractionContext) -> Extraction: ...


DEFAULT_LIMITS = {
    "inputBytes": 256 * 1024 * 1024, "outputBytes": 512 * 1024 * 1024,
    "expandedBytes": 512 * 1024 * 1024, "pages": 1000,
    "rows": 2_000_000, "columns": 16_384, "pixels": 16_000_000,
    "queueDepth": 100, "requestBytes": 64 * 1024, "responseBytes": 256 * 1024,
    "textBytes": 32 * 1024, "indexBytes": 32 * 1024,
    "jobSeconds": 3600, "modelSeconds": 180, "workerMemoryBytes": 2 * 1024**3,
    "orphanGraceSeconds": 86400,
}


def default_profile() -> dict:
    return {"id": "default", "limits": dict(DEFAULT_LIMITS),
            "options": {"encoding": "utf-8-sig", "figureDescriptions": True,
                        "tableRecovery": copy.deepcopy(DEFAULT_TABLE_RECOVERY)},
            "models": {"vision": ""},
            "retry": {"retryAmbiguous": False},
            "promptVersion": "1", "schemaVersion": 1}
