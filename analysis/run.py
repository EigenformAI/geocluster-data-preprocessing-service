"""Batch orchestrator for HIE -> DKI -> metrics/flags -> PEQA."""

from __future__ import annotations

import argparse
from pathlib import Path

from pipeline.util import write_json

from .compute import compute_metrics, computed_metric_evidence
from .dki import dki
from .flags import compute_flags
from .hie import build_report_metadata
from .paths import AnalysisPaths
from .peqa import DEFAULT_QUESTION, build_answer, render_memo, validate_answer


def run_analysis(report_root: Path | str, force: bool = False, question: str = DEFAULT_QUESTION) -> dict:
    report_root = Path(report_root)
    paths = AnalysisPaths(report_root)
    metadata = build_report_metadata(report_root, force=force)
    metrics = compute_metrics(metadata, paths.metrics)
    metadata["_evidence_index"].update(computed_metric_evidence(metrics))
    write_json(paths.evidence_index_json, metadata["_evidence_index"])
    flags = compute_flags(metadata, metrics)
    write_json(paths.flags_json, flags)

    injected = dki(question, metadata)
    write_json(paths.dki / "report.json", injected)

    answer = build_answer(question, metadata, metrics, flags, injected)
    answer, validated_claims = validate_answer(answer, metadata)
    write_json(paths.peqa / "answers" / "report.json", {"answer": answer, "claims": validated_claims})
    paths.memo_md.write_text(render_memo(answer, validated_claims, metadata["report_slug"]))

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
