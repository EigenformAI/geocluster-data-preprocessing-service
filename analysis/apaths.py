"""Output locations for the analysis package, rooted under a report's output dir.

Analysis writes only under `output/<slug>/analysis/` so it never collides with
the extraction pipeline's artifacts (or its `stage_state.json`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class APaths:
    report_root: Path  # output/<slug>  (a completed extraction)

    @property
    def root(self) -> Path:
        return self.report_root / "analysis"

    @property
    def records(self) -> Path:
        return self.root / "records"

    @property
    def metrics(self) -> Path:
        return self.root / "metrics"

    @property
    def cache(self) -> Path:
        return self.root / ".cache" / "mistral"

    @property
    def state_json(self) -> Path:
        return self.root / "analysis_state.json"

    @property
    def records_index(self) -> Path:
        return self.records / "_index.json"

    @property
    def flags_json(self) -> Path:
        return self.root / "flags.json"

    @property
    def findings_json(self) -> Path:
        return self.root / "findings.json"

    @property
    def memo_md(self) -> Path:
        return self.root / "memo.md"

    def record_json(self, table_id: str) -> Path:
        return self.records / f"{table_id}.json"

    def ensure_dirs(self) -> None:
        for d in (self.root, self.records, self.metrics, self.cache):
            d.mkdir(parents=True, exist_ok=True)
