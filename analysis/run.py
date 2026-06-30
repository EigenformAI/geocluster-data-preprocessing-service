"""Batch orchestrator for HIE -> DKI -> metrics/flags -> PEQA."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from pipeline.util import read_json, sha256_file, write_json

from .compute import compute_metrics, computed_metric_evidence
from .dki import dki
from .flags import compute_flags
from .hie import build_report_metadata
from .llm import select_answerer
from .paths import AnalysisPaths
from .peqa import DEFAULT_QUESTION, build_answer, render_memo, validate_answer


def _trace_id() -> str:
    return uuid.uuid4().hex


def _update_stage_state(paths: AnalysisPaths, stage: str, entry: dict) -> None:
    state = read_json(paths.state_json, default={}) or {}
    state[stage] = {"done": True, "trace_id": _trace_id(), **entry}
    write_json(paths.state_json, state)


def _peqa_counters(validated_claims: list[dict]) -> dict:
    return {
        "peqa.n_claims": len(validated_claims),
        "peqa.n_unverified": sum(1 for claim in validated_claims if not claim.get("validated")),
        "peqa.n_bad_citations": sum(len(claim.get("missing_citations") or []) for claim in validated_claims),
        "peqa.n_bad_numbers": sum(len(claim.get("bad_numbers") or []) for claim in validated_claims),
    }


def run_analysis(report_root: Path | str, force: bool = False, question: str = DEFAULT_QUESTION) -> dict:
    report_root = Path(report_root)
    paths = AnalysisPaths(report_root)
    metadata = build_report_metadata(report_root, force=force)
    metrics = compute_metrics(metadata, paths.metrics)
    _update_stage_state(paths, "metrics", {
        "consumes": {"report_metadata_sha256": sha256_file(paths.report_metadata_json)},
        "produces": {name: sha256_file(paths.metrics / f"{name}.json") for name in ("intercepts", "resource", "qaqc", "tenure")},
        "counters": {"metrics.n_record_types": len(metrics.get("stage", {}).get("record_counts", {}))},
    })
    metadata["_evidence_index"].update(computed_metric_evidence(metrics))
    write_json(paths.evidence_index_json, metadata["_evidence_index"])
    flags = compute_flags(metadata, metrics)
    write_json(paths.flags_json, flags)
    _update_stage_state(paths, "flags", {
        "consumes": {
            "report_metadata_sha256": sha256_file(paths.report_metadata_json),
            "evidence_index_sha256": sha256_file(paths.evidence_index_json),
        },
        "produces": {"flags_sha256": sha256_file(paths.flags_json)},
        "counters": {"flags.n_flags": len(flags.get("flags", []))},
    })

    injected = dki(question, metadata)
    write_json(paths.dki / "report.json", injected)
    _update_stage_state(paths, "dki", {
        "consumes": {
            "report_metadata_sha256": sha256_file(paths.report_metadata_json),
            "evidence_index_sha256": sha256_file(paths.evidence_index_json),
        },
        "produces": {"dki_report_sha256": sha256_file(paths.dki / "report.json")},
        "counters": injected.get("counters", {}),
    })

    answerer = select_answerer(paths.cache / "llm")
    answer = answerer.answer(question, metadata, metrics, flags, injected) if answerer else build_answer(question, metadata, metrics, flags, injected)
    answer, validated_claims = validate_answer(answer, metadata)
    write_json(paths.peqa / "answers" / "report.json", {"answer": answer, "claims": validated_claims})
    paths.memo_md.write_text(render_memo(answer, validated_claims, metadata["report_slug"]))
    _update_stage_state(paths, "peqa", {
        "consumes": {
            "report_metadata_sha256": sha256_file(paths.report_metadata_json),
            "evidence_index_sha256": sha256_file(paths.evidence_index_json),
            "dki_report_sha256": sha256_file(paths.dki / "report.json"),
        },
        "produces": {
            "peqa_answer_sha256": sha256_file(paths.peqa / "answers" / "report.json"),
            "memo_sha256": sha256_file(paths.memo_md),
        },
        "counters": _peqa_counters(validated_claims),
    })

    findings = {
        "report_slug": metadata["report_slug"],
        "project_stage": metadata.get("project_stage"),
        "resource": metrics["resource"].get("rows", []),
        "intercepts": metrics["intercepts"].get("top_intercepts", []),
        "qaqc": {"overall": metrics["qaqc"].get("overall"), "crms": metrics["qaqc"].get("crms", [])},
        "tenure": metrics["tenure"],
        "flags": flags.get("flags", []),
        "claims": validated_claims,
        "unverified_claims": [claim for claim in validated_claims if not claim["validated"]],
        "dki_warnings": injected.get("warnings", []),
    }
    write_json(paths.findings_json, findings)
    return {"report_slug": metadata["report_slug"], "metadata": metadata, "metrics": metrics, "flags": flags, "findings": findings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run hierarchical analysis for one extracted report directory.")
    parser.add_argument("report_root", help="Path to output/<slug> report directory")
    parser.add_argument("--force", action="store_true", help="Rebuild cached HIE artifacts")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Question to drive DKI/PEQA")
    args = parser.parse_args(argv)
    result = run_analysis(Path(args.report_root), force=args.force, question=args.question)
    print(result["report_slug"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
