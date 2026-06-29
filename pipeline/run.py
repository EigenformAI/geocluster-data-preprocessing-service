"""CLI / stage runner.

Usage:
    uv run python -m pipeline.run <pdf> [<pdf> ...] [options]

Options:
    --output DIR     output root (default: output)
    --stages LIST    comma-separated subset of stages to run
    --no-llm         run only the free deterministic core (render + ground truth)
    --force          re-run stages even if already completed
    --all-vision     rebuild EVERY table-bearing page with the Sonnet vision hybrid
                     (not just N-up pages) — for a report the default mangles;
                     numbers still come from the text layer. Needs ANTHROPIC_API_KEY.
    --vision-image-tables
                     transcribe only the IMAGE-ONLY table pages (no text layer —
                     scanned/rotated tables) with vision, rotated upright. Results
                     are unverifiable (no text layer to check). Needs ANTHROPIC_API_KEY.

Stages: ingest, render, ground_truth, ocr, parse, verify, describe, manifest
(ingest always runs; it is cheap and provides the page count.)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import (
    describe_figures,
    det_tables,
    ground_truth,
    ingest,
    manifest,
    ocr,
    parse_ocr,
    render,
    verify_tables,
)
from .mistral_client import MissingKey
from .util import Paths

DETERMINISTIC = ["render", "ground_truth"]
LLM = ["ocr", "parse", "det_tables", "verify", "describe", "manifest"]
ALL = DETERMINISTIC + LLM


def _log(msg: str) -> None:
    print(msg, flush=True)


def process(pdf: Path, output_root: Path, stages: set[str], no_llm: bool, force: bool,
            all_vision: bool = False, image_vision: bool = False) -> None:
    paths = Paths.for_pdf(pdf, output_root)
    paths.ensure_dirs()
    _log(f"\n=== {pdf.name} -> {paths.root} ===")

    t = time.time()
    doc = ingest.run(pdf, paths)
    n = doc["n_pages"]
    _log(f"[ingest] {n} pages, doc_id={doc['doc_id']} ({time.time() - t:.1f}s)")

    def step(name: str, fn):
        if name not in stages:
            return
        start = time.time()
        res = fn()
        _log(f"[{name}] {res} ({time.time() - start:.1f}s)")

    step("render", lambda: render.run(pdf, paths, n, force))
    step("ground_truth", lambda: ground_truth.run(pdf, paths, force))

    if no_llm:
        _log("[skip] --no-llm: OCR/vision stages skipped (set MISTRAL_API_KEY to run them).")
        return

    try:
        step("ocr", lambda: ocr.run(paths, n, force))
        step("parse", lambda: parse_ocr.run(paths, n, force))
        step("det_tables", lambda: det_tables.run(paths, force, all_vision, image_vision))
        step("verify", lambda: verify_tables.run(paths, force))
        step("describe", lambda: describe_figures.run(paths, force))
        step("manifest", lambda: manifest.run(paths))
    except MissingKey as e:
        _log(f"[stop] {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Decompose NI 43-101 PDFs into structured directories.")
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--output", type=Path, default=Path("output"))
    ap.add_argument("--stages", default=None, help="comma-separated subset of: " + ",".join(ALL))
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--all-vision", action="store_true",
                    help="rebuild every table page with the Sonnet vision hybrid (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--vision-image-tables", dest="image_vision", action="store_true",
                    help="vision-transcribe only image-only table pages, rotated upright "
                         "(unverifiable; needs ANTHROPIC_API_KEY)")
    args = ap.parse_args(argv)

    if args.stages:
        requested = {s.strip() for s in args.stages.split(",") if s.strip()}
        unknown = requested - set(ALL)
        if unknown:
            ap.error(f"unknown stage(s): {', '.join(sorted(unknown))}")
        stages = requested
    else:
        stages = set(ALL)

    failures = 0
    for pdf in args.pdfs:
        if not pdf.exists():
            _log(f"[error] not found: {pdf}")
            continue
        try:
            process(pdf, args.output, stages, args.no_llm, args.force, args.all_vision, args.image_vision)
        except Exception as e:  # don't let one report abort the rest (resume-safe)
            failures += 1
            _log(f"[error] {pdf.name} failed: {type(e).__name__}: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
