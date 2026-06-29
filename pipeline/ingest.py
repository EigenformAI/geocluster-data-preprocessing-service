"""Stage 0 INGEST: identity + metadata for one report PDF."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .util import Paths, mark_stage_done, sha256_file, write_json

STAGE = "ingest"


def _pdfinfo(pdf: Path) -> dict:
    out = subprocess.run(
        ["pdfinfo", str(pdf)], capture_output=True, text=True, check=True
    ).stdout
    info: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            info[key.strip()] = val.strip()
    return info


def run(pdf: Path, paths: Paths) -> dict:
    paths.ensure_dirs()
    info = _pdfinfo(pdf)
    n_pages = int(info.get("Pages", "0"))
    doc = {
        "report_slug": paths.root.name,
        "source_name": pdf.name,
        "doc_id": sha256_file(pdf)[:12],
        "sha256": sha256_file(pdf),
        "n_pages": n_pages,
        "producer": info.get("Producer", ""),
        "page_size": info.get("Page size", ""),
        "encrypted": info.get("Encrypted", ""),
    }
    if not paths.source_pdf.exists():
        shutil.copy2(pdf, paths.source_pdf)
    write_json(paths.doc_json, doc)
    mark_stage_done(paths, STAGE, {"n_pages": n_pages})
    return doc
