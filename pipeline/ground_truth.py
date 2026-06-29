"""Stage 2 GROUND TRUTH: per-page word boxes from pdftotext -bbox.

This is the *independent auditor* for table fidelity. The digital text layer is
extracted by a different path than Mistral OCR, so cross-checking OCR table
numbers against these words catches dropped columns, split cells, and digit
corruption without a human or a second LLM. The word boxes (in PDF points) also
drive table-crop generation in Stage 5.
"""

from __future__ import annotations

import html
import re
import subprocess
from pathlib import Path

from .util import Paths, mark_stage_done, write_json

STAGE = "ground_truth"

_PAGE_RE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S)
_WORD_RE = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>',
    re.S,
)


def _ground_truth_path(paths: Paths) -> Path:
    return paths.root / "ground_truth.json"


def run(pdf: Path, paths: Paths, force: bool = False) -> dict:
    gt_path = _ground_truth_path(paths)
    if not force and gt_path.exists():
        mark_stage_done(paths, STAGE)
        return {"skipped": True}

    xml = subprocess.run(
        ["pdftotext", "-bbox", str(pdf), "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    pages = []
    for i, m in enumerate(_PAGE_RE.finditer(xml), start=1):
        width, height, body = float(m.group(1)), float(m.group(2)), m.group(3)
        words = []
        for w in _WORD_RE.finditer(body):
            x0, y0, x1, y1 = (float(w.group(k)) for k in range(1, 5))
            token = html.unescape(w.group(5)).strip()
            if token:
                words.append({"t": token, "x0": x0, "y0": y0, "x1": x1, "y1": y1})
        pages.append({"page": i, "width": width, "height": height, "words": words})

    write_json(gt_path, {"pages": pages})
    mark_stage_done(paths, STAGE, {"n_pages": len(pages)})
    return {"skipped": False, "n_pages": len(pages)}


def load(paths: Paths) -> dict:
    from .util import read_json

    return read_json(_ground_truth_path(paths), default={"pages": []})
