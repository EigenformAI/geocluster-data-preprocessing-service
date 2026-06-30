"""Read adapter over a completed extraction directory."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from pipeline.util import read_json

_FOOTNOTE = re.compile(r"\^\{?\d+\}?")


def clean_cell(value: str | None) -> str:
    if value is None:
        return ""
    value = value.replace("**", "").replace("*", "")
    value = _FOOTNOTE.sub("", value)
    return value.strip().strip("*").strip()


class Report:
    def __init__(self, report_root: Path | str):
        self.root = Path(report_root)
        self.manifest = read_json(self.root / "manifest.json", default={}) or {}

    @property
    def slug(self) -> str:
        return self.manifest.get("report_slug", self.root.name)

    @property
    def doc_id(self) -> str:
        return self.manifest.get("doc_id", self.slug)

    @property
    def sha256(self) -> str | None:
        return self.manifest.get("sha256")

    @property
    def stats(self) -> dict:
        stats = dict(self.manifest.get("stats") or {})
        stats.setdefault("n_sections", len(self.sections))
        stats.setdefault("n_tables", len(self.tables))
        stats.setdefault("n_figures", len(self.figures))
        return stats

    @property
    def sections(self) -> list[dict]:
        return self.manifest.get("sections", [])

    @property
    def tables(self) -> list[dict]:
        return self.manifest.get("tables", [])

    @property
    def figures(self) -> list[dict]:
        return self.manifest.get("figures", [])

    def read_table(self, table: dict) -> list[list[str]]:
        path = self.root / table["csv"]
        with path.open(newline="") as f:
            return [[clean_cell(cell) for cell in row] for row in csv.reader(f)]

    def section_for_page(self, page: int) -> dict | None:
        best = None
        for section in self.sections:
            start = section.get("page_start", 0)
            end = section.get("page_end", 0)
            if start <= page <= end and end >= start:
                return section
            if start <= page and (best is None or start > best.get("page_start", -1)):
                best = section
        return best

    def section_text(self, section: dict) -> str:
        text_file = section.get("text_file")
        if not text_file:
            return ""
        path = self.root / text_file
        return path.read_text() if path.exists() else ""

    def figure_description(self, figure: dict) -> str:
        path = self.root / figure.get("description_md", "")
        return path.read_text() if path.exists() else ""
