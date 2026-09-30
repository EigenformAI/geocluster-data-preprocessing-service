"""Small worker contract: JSON values and generation-relative file paths only.

Format adapters implement extract(snapshot, profile, output_dir, context) and
return the fields of Extraction below. The coordinator owns identity, hashing,
validation, persistence, and publication. No parser writes the job database.
"""
from pathlib import Path
from typing import Any, Protocol, TypedDict


class Extraction(TypedDict):
    coverage: dict
    stages: list[dict]
    products: list[dict]  # path, kind, mediaType; optional id, schemaVersion
    tables: list[dict]  # id, rows, columns, typedPath, csvPath, source locator
    figures: list[dict]
    findings: list[dict]
    provenance: dict


class ExtractionContext(Protocol):
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
            "options": {"encoding": "utf-8-sig", "figureDescriptions": True},
            "models": {"vision": "opencode/space-bunny-free"},
            "retry": {"attempts": 24, "noProgress": 8, "retryAmbiguous": False},
            "promptVersion": "1", "schemaVersion": 1}
