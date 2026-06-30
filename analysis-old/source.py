"""Read adapter over a completed extraction (`output/<slug>/`).

The only module that knows the pipeline's on-disk layout. Also owns deterministic
cell cleaning (markdown/footnote artifacts) so the rest of analysis sees tidy text.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.util import read_json  # noqa: E402

_FOOTNOTE = re.compile(r"\^\{?\d+\}?")


def clean_cell(s: str) -> str:
    """Strip markdown emphasis + footnote/asterisk artifacts; keep the content."""
    if s is None:
        return ""
    s = s.replace("**", "").replace("*", "")  # leaves 'including' / 'Goldhawk Zone' intact
    s = _FOOTNOTE.sub("", s)
    return s.strip().strip("*").strip()


class Report:
    def __init__(self, report_root: Path):
        self.root = Path(report_root)
        self.manifest = read_json(self.root / "manifest.json", default={}) or {}

    @property
    def slug(self) -> str:
        return self.manifest.get("report_slug", self.root.name)

    @property
    def tables(self) -> list[dict]:
        return self.manifest.get("tables", [])

    @property
    def sections(self) -> list[dict]:
        return self.manifest.get("sections", [])

    @property
    def figures(self) -> list[dict]:
        return self.manifest.get("figures", [])

    def read_table(self, table: dict) -> list[list[str]]:
        """Cleaned rows for a table (relative `csv` path resolved against root)."""
        path = self.root / table["csv"]
        with open(path, newline="") as f:
            return [[clean_cell(c) for c in row] for row in csv.reader(f)]

    def section_for_page(self, page: int) -> dict | None:
        best = None
        for s in self.sections:
            ps, pe = s.get("page_start", 0), s.get("page_end", 0)
            if ps <= page <= pe and pe >= ps:
                return s
            if ps <= page and (best is None or ps > best.get("page_start", -1)):
                best = s  # nearest preceding section start
        return best

    def section_text(self, section: dict) -> str:
        tf = section.get("text_file")
        if not tf:
            return ""
        p = self.root / tf
        return p.read_text() if p.exists() else ""
