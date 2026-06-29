"""Stage 1 RENDER: full-page PNG render of every page (figure-of-record + crop source)."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .util import Paths, mark_stage_done

STAGE = "render"
DPI = 200


def _existing_pages(paths: Paths) -> int:
    return len(list(paths.pages.glob("page_*.png")))


def run(pdf: Path, paths: Paths, n_pages: int, force: bool = False) -> dict:
    paths.pages.mkdir(parents=True, exist_ok=True)
    if not force and _existing_pages(paths) >= n_pages:
        mark_stage_done(paths, STAGE, {"n_pages": n_pages, "dpi": DPI})
        return {"rendered": _existing_pages(paths), "skipped": True}

    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "p"
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(DPI), str(pdf), str(prefix)],
            check=True,
        )
        # pdftoppm emits p-<page>.png (zero-padded to the doc's max page width).
        for f in sorted(Path(tmp).glob("p-*.png")):
            page = int(f.stem.split("-")[-1])
            f.replace(paths.page_png(page))

    rendered = _existing_pages(paths)
    mark_stage_done(paths, STAGE, {"n_pages": n_pages, "rendered": rendered, "dpi": DPI})
    return {"rendered": rendered, "skipped": False}
