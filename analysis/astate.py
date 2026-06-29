"""Analysis stage-state (separate file from the pipeline's stage_state.json)."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.util import read_json, write_json  # noqa: E402

from .apaths import APaths


def load(apaths: APaths) -> dict:
    return read_json(apaths.state_json, default={}) or {}


def mark_done(apaths: APaths, stage: str, info: dict | None = None) -> None:
    state = load(apaths)
    state[stage] = {"done": True, **(info or {})}
    write_json(apaths.state_json, state)


def is_done(apaths: APaths, stage: str) -> bool:
    return bool(load(apaths).get(stage, {}).get("done"))
