"""Batch orchestrator for HIE -> DKI -> metrics/flags -> PEQA."""

from __future__ import annotations

import argparse
import sys
import traceback
import uuid
from pathlib import Path

from pipeline.util import read_json, sha256_file, write_json

from .compute import compute_metrics, computed_metric_evidence
from .dki import dki
from .flags import compute_flags
from .hie import build_report_metadata
from .llm import reset_llm_run_budget, select_answerer, select_image_reader
from .maps import select_map_processor
from .paths import AnalysisPaths
from .peqa import DEFAULT_QUESTION, build_answer, build_answer_pack, render_memo, validate_answer
from .report_agent import validate_answer_pack


def _trace_id() -> str:
    return uuid.uuid4().hex


def _update_stage_state(paths: AnalysisPaths, stage: str, entry: dict) -> None:
    state = read_json(paths.state_json, default={}) or {}
    state[stage] = {"done": True, "trace_id": _trace_id(), **entry}
    write_json(paths.state_json, state)


def _peqa_counters(validated_claims: list[dict]) -> dict:
    verified = [claim for claim in validated_claims if claim.get("validated")]
    return {
        "peqa.n_claims": len(verified),
        "peqa.n_claims_attempted": len(validated_claims),
        "peqa.n_unverified": sum(1 for claim in validated_claims if not claim.get("validated")),
        "peqa.n_bad_citations": sum(len(claim.get("missing_citations") or []) for claim in validated_claims),
        "peqa.n_bad_numbers": sum(len(claim.get("bad_numbers") or []) for claim in validated_claims),
        "peqa.n_unverified_unbacked_text": sum(1 for claim in validated_claims if claim.get("unbacked_text")),
    }


def _token_counters(prefix: str, telemetry: dict) -> dict:
    tokens = telemetry.get("tokens") if isinstance(telemetry, dict) else None
    if not isinstance(tokens, dict):
        return {}
    return {f"{prefix}.tokens.{key}": int(value) for key, value in tokens.items() if isinstance(value, int | float)}


def _record_answerer_fallback(paths: AnalysisPaths, exc: Exception) -> None:
    """Persist why the LLM answerer fell back to the deterministic answer.

    The fallback keeps the run alive, but swallowing the cause silently made a
    context-overflow bug look like "the answerer just didn't engage" across three of
    four reports. We write the full traceback next to the PEQA output and warn on
    stderr so the next run is diagnosable.
    """
    detail = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
    try:
        log_path = paths.peqa / "answerer_error.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(detail)
    except Exception:
        pass
    print(f"[answerer] fell back to deterministic answer: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _merge_injected_evidence(metadata: dict, injected: dict) -> int:
    evidence_index = metadata.setdefault("_evidence_index", {})
    n_merged = 0
    for bundle in (injected.get("selected") or {}).values():
        if not isinstance(bundle, dict):
            continue
        evidence = bundle.get("evidence") or {}
        if not isinstance(evidence, dict):
            continue
        for evidence_id, item in evidence.items():
            if isinstance(item, dict) and item.get("source_kind") == "knowledge_item":
                continue
            evidence_index[evidence_id] = item
            n_merged += 1
    return n_merged


def run_analysis(report_root: Path | str, force: bool = False, question: str = DEFAULT_QUESTION) -> dict:
    report_root = Path(report_root)
    reset_llm_run_budget()
    paths = AnalysisPaths(report_root)
    map_processor = select_map_processor(report_root)
    image_reader = select_image_reader(paths.cache / "llm")
    try:
        metadata = build_report_metadata(
            report_root, force=force, map_processor=map_processor, image_reader=image_reader
        )
    finally:
        if map_processor is not None and hasattr(map_processor, "close"):
            map_processor.close()
    return _finalize_analysis(paths, metadata, question)


def _finalize_analysis(paths: AnalysisPaths, metadata: dict, question: str) -> dict:
    """Post-HIE pipeline tail: metrics -> flags -> DKI -> PEQA -> findings.

    Split out so the PEQA stage can be re-run against cached HIE metadata (see
    ``rerun_peqa``) without paying for a full HIE/maps/VLM rebuild — every input here is
    deterministic Python plus the cached answerer call.
    """
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
    _merge_injected_evidence(metadata, injected)
    write_json(paths.evidence_index_json, metadata["_evidence_index"])
    write_json(paths.dki / "report.json", injected)
    _update_stage_state(paths, "dki", {
        "consumes": {
            "report_metadata_sha256": sha256_file(paths.report_metadata_json),
            "evidence_index_sha256": sha256_file(paths.evidence_index_json),
        },
        "produces": {"dki_report_sha256": sha256_file(paths.dki / "report.json")},
        "counters": injected.get("counters", {}),
    })

    deterministic_answer = build_answer(question, metadata, metrics, flags, injected)
    try:
        answerer = select_answerer(paths.cache / "llm")
    except Exception:
        answerer = None
    if answerer:
        try:
            answer = answerer.answer(question, metadata, metrics, flags, injected)
            answerer_used = True
        except Exception as exc:
            _record_answerer_fallback(paths, exc)
            answer = deterministic_answer
            answerer = None
            answerer_used = False
    else:
        answer = deterministic_answer
        answerer_used = False
    if answerer_used:
        for claim in answer.setdefault("claims", []):
            claim.setdefault("origin", "qp_assessment")
    answer_pack = answer.get("answer_pack") or deterministic_answer.get("answer_pack") or build_answer_pack(question, metadata, metrics, flags, injected)
    answer["answer_pack"] = answer_pack
    answer["answerer_used"] = answerer_used
    answer.setdefault("flags", deterministic_answer.get("flags", []))
    answer.setdefault("reason", deterministic_answer.get("reason", ""))
    answer.setdefault("limitations", [])
    validate_answer_pack(answer_pack, metadata, metadata["_evidence_index"])
    if answerer_used:
        existing = {claim.get("text") for claim in answer.setdefault("claims", [])}
        for claim in deterministic_answer.get("claims", []):
            if claim.get("text") in existing:
                continue
            answer["claims"].append(claim)
            existing.add(claim.get("text"))
        existing_limitations = set(answer.setdefault("limitations", []))
        for limitation in deterministic_answer.get("limitations", []):
            if limitation in existing_limitations:
                continue
            answer["limitations"].append(limitation)
            existing_limitations.add(limitation)
    answer, validated_claims = validate_answer(answer, metadata)
    verified_claims = [claim for claim in validated_claims if claim["validated"]]
    unverified_claims = [claim for claim in validated_claims if not claim["validated"]]
    write_json(paths.peqa / "answers" / "report.json", {
        "answer": answer,
        "answer_pack": answer_pack,
        "claims": verified_claims,
        "unverified_claims": unverified_claims,
    })
    paths.memo_md.write_text(render_memo(answer, verified_claims, metadata["report_slug"]))
    answerer_telemetry = dict(getattr(answerer, "telemetry", {}) or {}) if answerer is not None else {}
    peqa_counters = _peqa_counters(validated_claims)
    peqa_counters.update(_token_counters("answer", answerer_telemetry))
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
        "counters": peqa_counters,
        "llm": {"answerer": answerer_telemetry},
    })

    findings = {
        "report_slug": metadata["report_slug"],
        "project_stage": metadata.get("project_stage"),
        "resource": metrics["resource"].get("rows", []),
        "intercepts": metrics["intercepts"].get("top_intercepts", []),
        "qaqc": {"overall": metrics["qaqc"].get("overall"), "crms": metrics["qaqc"].get("crms", [])},
        "tenure": metrics["tenure"],
        "flags": flags.get("flags", []),
        "claims": verified_claims,
        "unverified_claims": unverified_claims,
        "dki_warnings": injected.get("warnings", []),
        "grounded_digest": answer_pack.get("grounded_digest", {}),
        "qp_assessment": answer.get("answer") if answerer_used else None,
    }
    write_json(paths.findings_json, findings)
    return {"report_slug": metadata["report_slug"], "metadata": metadata, "metrics": metrics, "flags": flags, "findings": findings}


def rerun_peqa(report_root: Path | str, question: str = DEFAULT_QUESTION) -> dict:
    """Re-run only metrics/flags/DKI/PEQA against cached HIE artifacts — no peace/HIE rebuild.

    Loads the persisted report_metadata + evidence_index (the index is stripped from the
    metadata file at rest, so it is reattached here) and re-derives the downstream stages.
    The answerer call is served from cache when unchanged, so this is ~$0 and runs in seconds,
    which makes PEQA-only iteration (validation, memo rendering) cheap.
    """
    report_root = Path(report_root)
    reset_llm_run_budget()
    paths = AnalysisPaths(report_root)
    metadata = read_json(paths.report_metadata_json)
    if metadata is None:
        raise FileNotFoundError(f"no cached report_metadata at {paths.report_metadata_json}")
    # Restore the runtime-only fields that _safe_metadata_for_write strips (any "_"-prefixed
    # key). _report_root lets textguard re-open source files; _evidence_index backs citations.
    metadata["_report_root"] = str(report_root)
    metadata["_evidence_index"] = read_json(paths.evidence_index_json, default={}) or {}
    return _finalize_analysis(paths, metadata, question)


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
