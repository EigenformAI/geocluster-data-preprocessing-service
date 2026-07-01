"""Read adapter over a completed extraction directory."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from pipeline.util import read_json

_FOOTNOTE = re.compile(r"\^\{?\d+\}?")
_FIGURE_ID = re.compile(r"fig_p(?P<page>\d{4})_(?P<sequence>\d+)")


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
        figures = []
        for figure in self.manifest.get("figures", []):
            enriched = dict(figure)
            self._add_ocr_bbox(enriched)
            figures.append(enriched)
        return figures

    def _add_ocr_bbox(self, figure: dict) -> None:
        if figure.get("bbox"):
            return
        match = _FIGURE_ID.fullmatch(str(figure.get("id") or ""))
        if not match:
            return
        try:
            page = int(figure.get("page") or match.group("page"))
            sequence = int(match.group("sequence"))
        except (TypeError, ValueError):
            return
        ocr_page = read_json(self.root / "ocr" / f"page_{page:04d}.json", default=None)
        images = (ocr_page or {}).get("images") or []
        if sequence < 1 or sequence > len(images):
            return
        image = images[sequence - 1]
        try:
            figure["bbox"] = {key: image[key] for key in ("x0", "y0", "x1", "y1")}
        except KeyError:
            figure.pop("bbox", None)
            return
        dimensions = (ocr_page or {}).get("dimensions")
        if dimensions:
            figure["ocr_page_dimensions"] = dimensions

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
