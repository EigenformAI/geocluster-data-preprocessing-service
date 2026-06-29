"""CLI / stage runner for the analysis pipeline.

Usage:
    uv run python -m analysis.run output/<slug> [output/<slug2> ...] [options]

Options:
    --stages LIST   subset of: normalize, compute, flags, assess
    --no-llm        run only deterministic stages (compute, flags)
    --force         re-run even if already done / cached

Input is a completed extraction directory (output/<slug>), not a PDF.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import assess, compute, flags, normalize
from .apaths import APaths
from .source import Report

LLM = {"normalize", "assess"}
ALL = ["normalize", "compute", "flags", "assess"]


def _log(msg: str) -> None:
    print(msg, flush=True)


def process(report_dir: Path, stages: set[str], no_llm: bool, force: bool) -> None:
    report = Report(report_dir)
    if not report.manifest:
        _log(f"[skip] {report_dir}: no manifest.json (run the extraction pipeline first)")
        return
    apaths = APaths(report.root)
    apaths.ensure_dirs()
    _log(f"\n=== {report.slug} ===")

    def step(name, fn):
        if name not in stages:
            return
        if name in LLM and no_llm:
            _log(f"[{name}] skipped (--no-llm)")
            return
        t = time.time()
        res = fn()
        _log(f"[{name}] {res} ({time.time() - t:.1f}s)")

    step("normalize", lambda: normalize.run(report, apaths, force))
    step("compute", lambda: compute.run(apaths, force))
    step("flags", lambda: flags.run(report, apaths, force))
    step("assess", lambda: assess.run(report, apaths, force))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Conservative-QP analysis over extracted reports.")
    ap.add_argument("reports", nargs="+", type=Path, help="output/<slug> directories")
    ap.add_argument("--stages", default=None)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    stages = set(ALL)
    if args.stages:
        stages = {s.strip() for s in args.stages.split(",") if s.strip()}
        unknown = stages - set(ALL)
        if unknown:
            ap.error(f"unknown stage(s): {', '.join(sorted(unknown))}")

    failures = 0
    for d in args.reports:
        try:
            process(d, stages, args.no_llm, args.force)
        except Exception as e:
            failures += 1
            _log(f"[error] {d}: {type(e).__name__}: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
