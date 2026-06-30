"""Analysis output layout rooted under ``output/<slug>/analysis``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AnalysisPaths:
    report_root: Path

    @property
    def root(self) -> Path:
        return self.report_root / "analysis"

    @property
    def state_json(self) -> Path:
        return self.root / "analysis_state.json"

    @property
    def input_index_json(self) -> Path:
        return self.root / "input_index.json"

    @property
    def report_metadata_json(self) -> Path:
        return self.root / "report_metadata.json"

    @property
    def evidence_index_json(self) -> Path:
        return self.root / "evidence_index.json"

    @property
    def findings_json(self) -> Path:
        return self.root / "findings.json"

    @property
    def memo_md(self) -> Path:
        return self.root / "memo.md"

    @property
    def flags_json(self) -> Path:
        return self.root / "flags.json"

    @property
    def records(self) -> Path:
        return self.root / "records"

    @property
    def hierarchy(self) -> Path:
        return self.root / "hierarchy"

    @property
    def metrics(self) -> Path:
        return self.root / "metrics"

    @property
    def dki(self) -> Path:
        return self.root / "dki"

    @property
    def hie(self) -> Path:
        return self.root / "hie"

    @property
    def peqa(self) -> Path:
        return self.root / "peqa"

    @property
    def cache(self) -> Path:
        return self.root / ".cache"

    def record_json(self, table_id: str) -> Path:
        return self.records / f"{table_id}.json"

    def hie_table_json(self, element_id: str) -> Path:
        return self.hie / "tables" / f"{element_id}.json"

    def hie_figure_json(self, element_id: str) -> Path:
        return self.hie / "figures" / f"{element_id}.json"

    def ensure_dirs(self) -> None:
        for path in (
            self.root,
            self.records,
            self.hierarchy,
            self.metrics,
            self.dki,
            self.hie / "tables",
            self.hie / "figures",
            self.hie / "maps",
            self.peqa / "answers",
            self.cache / "llm",
            self.cache / "mcp",
        ):
            path.mkdir(parents=True, exist_ok=True)
