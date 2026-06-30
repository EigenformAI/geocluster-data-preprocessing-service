"""PEACE-style ablation harness for Report-Agent modules."""

from __future__ import annotations

from pathlib import Path

from pipeline.util import sha256_file, write_json

from .compute import compute_metrics, computed_metric_evidence
from .dki import dki
from .flags import compute_flags
from .hie import build_report_metadata
from .paths import AnalysisPaths
from .peqa import DEFAULT_QUESTION, build_answer, validate_answer

ABLATION_MODES = ("baseline", "hie_only", "hie_dki", "hie_dki_peqa")


def run_ablation_harness(
    report_root: Path | str,
    question: str = DEFAULT_QUESTION,
    modes: list[str] | None = None,
    force: bool = False,
) -> dict:
    report_root = Path(report_root)
    paths = AnalysisPaths(report_root)
    paths.ensure_dirs()
    selected_modes = modes or list(ABLATION_MODES)

    metadata = build_report_metadata(report_root, force=force)
    metrics = compute_metrics(metadata, paths.metrics)
    metadata["_evidence_index"].update(computed_metric_evidence(metrics))
    flags = compute_flags(metadata, metrics)

    results: dict[str, dict] = {}
    for mode in selected_modes:
        if mode not in ABLATION_MODES:
            raise ValueError(f"unknown ablation mode: {mode}")
        if mode == "baseline":
            results[mode] = _mode_result(mode, metadata, {}, [], [])
            continue
        if mode == "hie_only":
            results[mode] = _mode_result(mode, metadata, {}, [], [])
            continue
        injected = dki(question, metadata)
        if mode == "hie_dki":
            results[mode] = _mode_result(mode, metadata, injected, [], [])
            continue
        answer = build_answer(question, metadata, metrics, flags, injected)
        _answer, validated = validate_answer(answer, metadata)
        results[mode] = _mode_result(
            mode,
            metadata,
            injected,
            [claim for claim in validated if claim.get("validated")],
            [claim for claim in validated if not claim.get("validated")],
        )

    payload = {
        "schema_version": "report_agent_ablation/v1",
        "report_slug": metadata.get("report_slug"),
        "question": question,
        "modes": results,
    }
    out_path = paths.ablations / "report.json"
    write_json(out_path, payload)
    payload["sha256"] = sha256_file(out_path)
    return payload


def _mode_result(mode: str, metadata: dict, injected: dict, verified_claims: list[dict], unverified_claims: list[dict]) -> dict:
    evidence_count = len(metadata.get("_evidence_index") or {}) if mode != "baseline" else 0
    assertion_count = len(metadata.get("assertions") or []) if mode != "baseline" else 0
    bundles = injected.get("selected") or {}
    return {
        "mode": mode,
        "abilities": {
            "extracting": len(metadata.get("records") or {}) if mode != "baseline" else 0,
            "grounding": evidence_count,
            "referring": assertion_count,
            "reasoning": len(verified_claims),
            "analyzing": len(bundles),
            "absence_coverage": sum(len((bundle or {}).get("coverage") or []) for bundle in bundles.values() if isinstance(bundle, dict)),
        },
        "n_verified_claims": len(verified_claims),
        "n_unverified_claims": len(unverified_claims),
        "selected_experts": list(bundles),
    }
