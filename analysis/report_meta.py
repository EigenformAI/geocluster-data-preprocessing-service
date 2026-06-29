"""Project-stage classification + record loading.

Stage adaptivity is driven by which record types actually exist, not by report
name — so an early-stage project (no resource) and a producer-grade one are
handled differently and we never flag a 'missing' resource that isn't expected.
"""

from __future__ import annotations

import collections
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.util import read_json  # noqa: E402

from .apaths import APaths


def load_records(apaths: APaths) -> list[dict]:
    """All kept records across every normalized table."""
    out: list[dict] = []
    for f in sorted(apaths.records.glob("*.json")):
        if f.name == "_index.json":
            continue
        d = read_json(f, default={})
        out.extend(d.get("records", []))
    return out


def by_type(records: list[dict]) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = collections.defaultdict(list)
    for r in records:
        g[r.get("_type", "other")].append(r)
    return g


def project_stage(groups: dict[str, list[dict]]) -> dict:
    counts = {t: len(rs) for t, rs in groups.items() if rs}
    if groups.get("resource_estimate"):
        stage = "resource"
    elif groups.get("assay_interval"):
        stage = "target"
    elif groups.get("drillhole_collar") or groups.get("drill_history"):
        stage = "early"
    else:
        stage = "desktop"
    return {"stage": stage, "record_counts": counts}
