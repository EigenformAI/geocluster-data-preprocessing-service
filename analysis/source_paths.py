"""Safe resolution for report-relative source references."""

from __future__ import annotations

from pathlib import Path


def safe_source_path(report_root: Path | str | None, source_ref: str | None) -> Path | None:
    """Resolve a source reference only when it stays inside the report root."""

    if not report_root or not source_ref:
        return None
    relative = Path(str(source_ref))
    if relative.is_absolute():
        return None
    root = Path(report_root).resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        return None
    return path
