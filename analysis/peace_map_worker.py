"""PEACE map-component worker — runs inside the peace-tool-pool detector venv.

This script is intentionally standalone: it imports ONLY the standard library and
``peace_tool_pool`` so it can run under peace's isolated venv (whose torch/cv2 stack
is incompatible with the analysis venv). It is driven over stdin/stdout as a warm
worker: ``MapProcessingService`` is constructed once, then one JSONL request is read
and one JSONL response is written per line.

Protocol (one JSON object per line):
  startup  -> ``{"event": "ready"}``  (or ``{"event": "error", "error": ...}`` on load failure)
  request  <- ``{"image_path": str, "figure_id": str, "output_dir": str}``
  response -> ``{"event": "result", "figure_id": str, "regions": {...}, "legend": [...],
                 "artifacts": [...], "size": {...}}``
              or ``{"event": "error", "figure_id": str, "error": str}``
"""

from __future__ import annotations

import json
import sys


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    try:
        from peace_tool_pool.map_processing import MapProcessingService

        service = MapProcessingService()
    except Exception as exc:  # pragma: no cover - exercised only with real peace env
        _emit({"event": "error", "error": f"load_failed: {type(exc).__name__}: {exc}"})
        return

    _emit({"event": "ready"})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request: dict = {}
        try:
            request = json.loads(line)
            result = service.process_image(request["image_path"]).to_dict()
            _emit({
                "event": "result",
                "figure_id": request.get("figure_id"),
                "regions": result.get("regions"),
                "legend": result.get("legend"),
                "artifacts": result.get("artifacts"),
                "size": result.get("size"),
            })
        except Exception as exc:  # pragma: no cover - per-figure failure path
            _emit({
                "event": "error",
                "figure_id": request.get("figure_id"),
                "error": f"{type(exc).__name__}: {exc}",
            })


if __name__ == "__main__":
    main()
