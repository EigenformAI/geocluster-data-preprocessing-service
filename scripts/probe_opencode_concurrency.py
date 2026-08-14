"""Probe the opencode/OAuth structured-call concurrency ceiling.

Example:
  ANALYSIS_LLM_BACKEND=opencode OPENCODE_SERVER_URL=http://127.0.0.1:4096 \
    uv run python scripts/probe_opencode_concurrency.py --max-workers 8
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from analysis.llm import OpencodeBackend


def _call(index: int, cache_dir: Path) -> dict:
    backend = OpencodeBackend(cache_dir=cache_dir)
    start = time.time()
    result = backend.classify_figure({"id": f"probe_{index}"}, f"Probe figure {index}: ambiguous report visual.")
    return {
        "index": index,
        "seconds": round(time.time() - start, 3),
        "class": result.figure_class,
        "confidence": result.confidence,
        "telemetry": backend.telemetry,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe concurrent opencode structured calls.")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--calls", type=int, default=None)
    parser.add_argument("--cache-dir", default="output/.probe_opencode/llm")
    args = parser.parse_args()

    calls = args.calls or args.max_workers
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()
    rows = []
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
        futures = [executor.submit(_call, index, cache_dir) for index in range(calls)]
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as exc:
                errors.append({"error": f"{type(exc).__name__}: {exc}"})

    print(json.dumps({
        "max_workers": args.max_workers,
        "calls": calls,
        "seconds": round(time.time() - start, 3),
        "ok": len(rows),
        "errors": errors,
        "rows": sorted(rows, key=lambda row: row["index"]),
    }, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
