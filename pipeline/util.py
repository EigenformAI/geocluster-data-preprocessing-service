"""Shared helpers: paths, hashing, JSON, and stage-state tracking."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


def slugify(name: str) -> str:
    """Filesystem-safe lowercase slug, e.g. 'Gold X2 Mining.pdf' -> 'gold_x2_mining'."""
    stem = Path(name).stem
    slug = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    return slug or "report"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(*parts: bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.hexdigest()


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


@dataclass
class Paths:
    """All output locations for one report."""

    root: Path

    @classmethod
    def for_pdf(cls, pdf: Path, output_root: Path) -> "Paths":
        return cls(output_root / slugify(pdf.name))

    @property
    def pages(self) -> Path:
        return self.root / "pages"

    @property
    def text(self) -> Path:
        return self.root / "text"

    @property
    def tables(self) -> Path:
        return self.root / "tables"

    @property
    def images(self) -> Path:
        return self.root / "images"

    @property
    def cache(self) -> Path:
        return self.root / ".cache" / "mistral"

    @property
    def source_pdf(self) -> Path:
        return self.root / "source.pdf"

    @property
    def doc_json(self) -> Path:
        return self.root / "doc.json"

    @property
    def sections_json(self) -> Path:
        return self.root / "sections.json"

    @property
    def page_class_json(self) -> Path:
        return self.root / "page_class.json"

    @property
    def manifest_json(self) -> Path:
        return self.root / "manifest.json"

    @property
    def summary_md(self) -> Path:
        return self.root / "summary.md"

    @property
    def stage_state_json(self) -> Path:
        return self.root / "stage_state.json"

    def page_png(self, page: int) -> Path:
        return self.pages / f"page_{page:04d}.png"

    def ensure_dirs(self) -> None:
        for d in (self.root, self.pages, self.text, self.tables, self.images, self.cache):
            d.mkdir(parents=True, exist_ok=True)


def load_stage_state(paths: Paths) -> dict:
    return read_json(paths.stage_state_json, default={}) or {}


def mark_stage_done(paths: Paths, stage: str, info: dict | None = None) -> None:
    state = load_stage_state(paths)
    state[stage] = {"done": True, **(info or {})}
    write_json(paths.stage_state_json, state)


def is_stage_done(paths: Paths, stage: str) -> bool:
    return bool(load_stage_state(paths).get(stage, {}).get("done"))
