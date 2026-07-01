"""Map-figure component extraction via a warm PEACE subprocess worker.

peace-tool-pool's detector stack (torch/cv2 + a vendored Ultralytics YOLOv10 fork)
is incompatible with the analysis venv, so we do not import it here. Instead
``PeaceMapProcessor`` spawns ``peace_map_worker.py`` under peace's own interpreter and
drives it over stdin/stdout. The worker constructs ``MapProcessingService`` once and
serves every map figure of a report from that one warm process — torch loads once, not
once per figure. The adapter reshapes peace's ``regions``/``artifacts`` output into the
``map_component`` contract the HIE seam (`analysis/hie._process_map_figure`) expects.
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
from pathlib import Path

DEFAULT_MAP_BACKEND = "none"
_WORKER_SCRIPT = Path(__file__).resolve().with_name("peace_map_worker.py")
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Known-good provisioned peace checkouts, searched only as a fallback when the standard
# GEOMAP_* env vars do not already resolve to a weights tree. Order = preference.
_PEACE_ASSET_ROOTS = (
    Path.home() / "peace-tool-pool",
    Path.home() / "peace",
    _REPO_ROOT / ".cache" / "peace-tool-pool",
)
_DEFAULT_PEACE_PYTHON = _REPO_ROOT / ".cache" / "peace-tool-pool" / ".venv" / "bin" / "python"


def _relativize(path: str | os.PathLike, root: Path) -> str:
    """Express a crop path relative to the report root so evidence never leaks absolutes."""
    try:
        return os.path.relpath(os.path.abspath(os.fspath(path)), os.path.abspath(root))
    except (ValueError, TypeError):
        return str(path)


def peace_result_to_components(result: dict, report_root: Path | str) -> list[dict]:
    """Flatten peace ``regions`` (``{label: [Detection, ...]}``) into seam components.

    Empty region buckets yield nothing. Each component carries the bbox, confidence and a
    report-relative crop reference the seam records as ``map_component`` evidence.
    """
    root = Path(report_root)
    components: list[dict] = []
    for label, detections in (result.get("regions") or {}).items():
        for index, detection in enumerate(detections or []):
            crop = detection.get("artifact_path") or detection.get("crop_ref")
            components.append({
                "id": f"{label}_{index}",
                "type": label,
                "label": label,
                "bbox": detection.get("bbox"),
                "confidence": detection.get("confidence"),
                "crop_ref": _relativize(crop, root) if crop else None,
            })
    return components


def _skip(processor: str, reason: str, message: str) -> dict:
    return {
        "status": "skipped",
        "reason": reason,
        "processor": processor,
        "components": [],
        "legend": [],
        "georef": None,
        "warnings": [message],
    }


class PeaceMapProcessor:
    """Warm-worker adapter implementing the HIE ``process_map`` seam contract."""

    name = "peace"

    def __init__(self, report_root: Path | str, *, worker_argv: list[str], env: dict[str, str] | None = None):
        self.report_root = Path(report_root)
        self._argv = list(worker_argv)
        self._env = {**os.environ, **(env or {})}
        self._proc: subprocess.Popen | None = None
        self._stderr = None
        self._unavailable = False
        atexit.register(self.close)

    def _ensure(self) -> None:
        if self._proc is not None or self._unavailable:
            return
        log_path = self.report_root / "analysis" / "hie" / "maps" / "_peace_worker.stderr.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # stderr -> file so torch/ultralytics chatter can never fill an undrained pipe.
            self._stderr = open(log_path, "ab")
            self._proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                text=True,
                bufsize=1,
                env=self._env,
            )
            handshake = self._proc.stdout.readline() if self._proc.stdout else ""
            message = json.loads(handshake) if handshake.strip() else {}
            if message.get("event") != "ready":
                self._unavailable = True
                self.close()
        except Exception:
            self._unavailable = True
            self.close()

    def process_map(self, report_root: Path | str, figure: dict, output_dir: Path | str) -> dict:
        self._ensure()
        if self._unavailable or self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            return _skip(self.name, "peace_unavailable", "peace worker unavailable")
        image_path = (Path(report_root) / str(figure.get("image") or "")).resolve()
        request = {"image_path": str(image_path), "figure_id": figure.get("id"), "output_dir": str(output_dir)}
        try:
            self._proc.stdin.write(json.dumps(request) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            self._unavailable = True
            return _skip(self.name, "peace_io_error", f"peace worker io error: {exc}")
        if not line.strip():
            self._unavailable = True
            return _skip(self.name, "peace_eof", "peace worker closed unexpectedly")
        message = json.loads(line)
        if message.get("event") == "error":
            return _skip(self.name, "peace_error", str(message.get("error")))
        return {
            "status": "processed",
            "processor": self.name,
            "components": peace_result_to_components(message, report_root),
            "legend": message.get("legend") or [],
            "georef": message.get("georef"),
            "warnings": message.get("warnings") or [],
        }

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        stderr, self._stderr = self._stderr, None
        if stderr is not None:
            try:
                stderr.close()
            except Exception:
                pass


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "off", "false", "no")


def _resolve_peace_python(env) -> Path | None:
    explicit = env.get("ANALYSIS_MAP_PEACE_PYTHON")
    candidates = [Path(explicit)] if explicit else [_DEFAULT_PEACE_PYTHON]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _valid_model_root(path: Path) -> bool:
    return (
        (path / "det_component" / "weights" / "best.pt").exists()
        and (path / "det_legend" / "weights" / "best.pt").exists()
    )


def _resolve_model_root(env, search: bool) -> Path | None:
    explicit = env.get("GEOMAP_MODEL_ROOT")
    if explicit and _valid_model_root(Path(explicit)):
        return Path(explicit)
    if search:
        for root in _PEACE_ASSET_ROOTS:
            candidate = root / "dependencies" / "models"
            if _valid_model_root(candidate):
                return candidate
    return None


def _resolve_ultralytics_root(env, search: bool) -> Path | None:
    explicit = env.get("GEOMAP_ULTRALYTICS_ROOT")
    if explicit and Path(explicit).exists():
        return Path(explicit)
    if search:
        for root in _PEACE_ASSET_ROOTS:
            candidate = root / "dependencies" / "ultralytics"
            if candidate.exists():
                return candidate
    return None


def select_map_processor(report_root: Path | str, env: dict[str, str] | None = None):
    """Select a map processor by ``ANALYSIS_MAP_BACKEND`` (none|peace).

    Returns ``None`` (so the HIE seam cleanly skips maps) when the backend is ``none`` or
    when the peace interpreter/weights cannot be resolved — a missing detector env must
    degrade to "maps skipped", never crash the run.
    """
    env_map = env if env is not None else os.environ
    backend = env_map.get("ANALYSIS_MAP_BACKEND", DEFAULT_MAP_BACKEND).strip().lower()
    if backend in ("", "none", "off"):
        return None
    if backend != "peace":
        raise ValueError("ANALYSIS_MAP_BACKEND must be one of none, peace")

    search = _truthy(env_map.get("ANALYSIS_MAP_PEACE_ASSET_SEARCH", "on"))
    python = _resolve_peace_python(env_map)
    model_root = _resolve_model_root(env_map, search)
    ultralytics_root = _resolve_ultralytics_root(env_map, search)
    if not (python and model_root and ultralytics_root):
        return None

    report_root = Path(report_root)
    worker_env = {
        "GEOMAP_MODEL_ROOT": str(model_root),
        "GEOMAP_ULTRALYTICS_ROOT": str(ultralytics_root),
        "GEOMAP_CACHE_ROOT": str(report_root / "analysis" / "hie" / "maps" / "_peace_cache"),
        "GEOMAP_DATASET_SOURCE": report_root.name,
    }
    return PeaceMapProcessor(
        report_root,
        worker_argv=[str(python), "-u", str(_WORKER_SCRIPT)],
        env=worker_env,
    )
