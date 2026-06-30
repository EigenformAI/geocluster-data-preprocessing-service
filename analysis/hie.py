"""HIE map-reduce builder for ``report_metadata.json``."""

from __future__ import annotations

import uuid
from pathlib import Path

from pipeline.util import read_json, sha256_file, write_json

from .figures import is_georeference_candidate, is_map_candidate
from .llm import FigureClassifier, mapper_cache_key, select_figure_classifier, select_table_mapper
from .paths import AnalysisPaths
from .reduce import build_entity_graph, classify_project_stage, derive_location_candidates
from .schemas import FigureClassification
from .source import Report
from .tables import TableMapper, normalize_table
from .validation import validate_report_metadata


def build_input_index(report: Report, paths: AnalysisPaths, figure_classifier: FigureClassifier | None = None) -> dict:
    figure_classifier = figure_classifier or select_figure_classifier(paths.cache / "llm")
    elements: list[dict] = []
    warnings: list[dict] = []
    for section in report.sections:
        text_file = section.get("text_file")
        exists = bool(text_file and (report.root / text_file).exists())
        if not exists:
            warnings.append({"kind": "missing_section_text", "section": section.get("slug")})
        elements.append({
            "id": section.get("slug") or f"section:{section.get('idx')}",
            "kind": "section",
            "page_start": section.get("page_start"),
            "page_end": section.get("page_end"),
            "source_ref": text_file,
            "exists": exists,
        })
    for table in report.tables:
        csv_path = table.get("csv")
        exists = bool(csv_path and (report.root / csv_path).exists())
        if not exists:
            warnings.append({"kind": "missing_table_csv", "element_id": table.get("id")})
        elements.append({
            "id": table.get("id"),
            "kind": "table",
            "page": table.get("page"),
            "source_ref": csv_path,
            "exists": exists,
        })
    for figure in report.figures:
        description = report.figure_description(figure)
        classification = figure_classifier.classify_figure(figure, description)
        image = figure.get("image")
        exists = bool(image and (report.root / image).exists())
        if not exists:
            warnings.append({"kind": "missing_figure_image", "element_id": figure.get("id")})
        elements.append({
            "id": figure.get("id"),
            "kind": "figure",
            "page": figure.get("page"),
            "source_ref": image,
            "description_ref": figure.get("description_md"),
            "figure_class": classification.figure_class,
            "classification": classification.to_dict(),
            "map_candidate": is_map_candidate(classification.figure_class),
            "georeference_candidate": is_georeference_candidate(classification.figure_class),
            "exists": exists,
        })
    index = {
        "report_slug": report.slug,
        "n_elements": len(elements),
        "elements": elements,
        "review_queue": report.manifest.get("review_queue", []),
        "warnings": warnings,
    }
    write_json(paths.input_index_json, index)
    return index


def _section_views(report: Report, elements: dict[str, dict]) -> list[dict]:
    sections = []
    for section in report.sections:
        start = section.get("page_start", 0)
        end = section.get("page_end", 0)
        element_ids = [
            element_id
            for element_id, element in elements.items()
            if start <= int(element.get("page") or 0) <= end
        ]
        sections.append({
            "idx": section.get("idx"),
            "number": section.get("number"),
            "title": section.get("title"),
            "slug": section.get("slug"),
            "page_start": start,
            "page_end": end,
            "text_file": section.get("text_file"),
            "element_ids": element_ids,
        })
    return sections


def _safe_metadata_for_write(metadata: dict) -> dict:
    return {key: value for key, value in metadata.items() if not key.startswith("_")}


def _counter(value) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _mapper_trace(mapper: TableMapper) -> dict:
    trace = {"mapper": dict(getattr(mapper, "telemetry", {}) or {})}
    fallback = getattr(mapper, "fallback", None)
    if fallback is not None:
        trace["backend"] = dict(getattr(fallback, "telemetry", {}) or {})
    return trace


def _trace_id() -> str:
    return uuid.uuid4().hex


def build_report_metadata(
    report_root: Path | str,
    mapper: TableMapper | None = None,
    force: bool = False,
    figure_classifier: FigureClassifier | None = None,
) -> dict:
    report = Report(report_root)
    paths = AnalysisPaths(report.root)
    paths.ensure_dirs()
    mapper = mapper or select_table_mapper(paths.cache / "llm")
    mapper_key = mapper_cache_key(mapper)
    figure_classifier = figure_classifier or select_figure_classifier(paths.cache / "llm")
    trace_ids = {"input_index": _trace_id(), "hie": _trace_id()}

    input_index = build_input_index(report, paths, figure_classifier)
    input_figure_classifications = {
        element.get("id"): FigureClassification(**element["classification"])
        for element in input_index.get("elements", [])
        if element.get("kind") == "figure" and element.get("classification")
    }
    elements: dict[str, dict] = {}
    records: dict[str, dict] = {}
    evidence_index: dict[str, dict] = {}
    warnings: list[dict] = list(input_index.get("warnings", []))
    cache_hits = 0
    cache_misses = 0
    element_cache: dict[str, str] = {}
    guard_n_rejected = 0

    for table in report.tables:
        out_path = paths.hie_table_json(table["id"])
        csv_path = report.root / table["csv"]
        source_sha256 = sha256_file(csv_path) if csv_path.exists() else None
        cached = out_path.exists() and not force
        table_hie = read_json(out_path, default=None) if cached else None
        if table_hie is not None and (
            table_hie.get("source_sha256") != source_sha256
            or table_hie.get("mapper_cache_key") != mapper_key
            or "evidence" not in table_hie
        ):
            table_hie = None
        if table_hie is None:
            rows = report.read_table(table)
            table_hie = normalize_table({**table, "source_sha256": source_sha256}, rows, mapper)
            table_hie["source_sha256"] = source_sha256
            mapping_source = (table_hie.get("mapping_meta") or {}).get("source")
            table_hie["mapper_cache_key"] = f"{mapper_key}:retryable:{mapping_source}" if mapping_source in {"degraded", "throttled"} else mapper_key
            write_json(out_path, table_hie)
            write_json(paths.record_json(table["id"]), {
                "table_id": table["id"],
                "table_type": table_hie["table_type"],
                "records": table_hie["records"],
                "rejected": table_hie["rejected"],
                "guard": table_hie["guard"],
                "n_records": table_hie["n_records"],
            })
            cache_misses += 1
            element_cache[table["id"]] = "miss"
        else:
            cache_hits += 1
            element_cache[table["id"]] = "hit"
        guard_n_rejected += int((table_hie.get("guard") or {}).get("n_rejected") or 0)

        for record in table_hie.get("records", []):
            records[record["_id"]] = record
        evidence_index.update(table_hie.get("evidence", {}))
        elements[table["id"]] = {
            "kind": "table",
            "page": table.get("page"),
            "section_slug": table.get("section"),
            "table_type": table_hie.get("table_type"),
            "confidence": table.get("confidence"),
            "method": table.get("method"),
            "csv_path": table.get("csv"),
            "crop_png": table.get("crop_png"),
            "mapping": table_hie.get("mapping"),
            "mapping_meta": table_hie.get("mapping_meta", {}),
            "record_ids": [record["_id"] for record in table_hie.get("records", [])],
            "guard": table_hie.get("guard"),
        }

    for figure in report.figures:
        classification = input_figure_classifications.get(figure["id"])
        if classification is None:
            classification = figure_classifier.classify_figure(figure, report.figure_description(figure))
        map_candidate = is_map_candidate(classification.figure_class)
        map_meta = None
        if map_candidate:
            map_meta = {"status": "skipped", "reason": "peace_unavailable", "georef": None}
            warnings.append({"kind": "map_hie_skipped", "element_id": figure["id"], "reason": "peace_unavailable"})
        figure_hie = {
            "figure_id": figure["id"],
            "page": figure.get("page"),
            "figure_class": classification.figure_class,
            "classification": classification.to_dict(),
            "map_candidate": map_candidate,
            "georeference_candidate": is_georeference_candidate(classification.figure_class),
            "map_meta": map_meta,
        }
        write_json(paths.hie_figure_json(figure["id"]), figure_hie)
        elements[figure["id"]] = {
            "kind": "figure",
            "page": figure.get("page"),
            "section_slug": (report.section_for_page(figure.get("page", 0)) or {}).get("slug"),
            "image_path": figure.get("image"),
            "description_md_path": figure.get("description_md"),
            "figure_class": classification.figure_class,
            "map_candidate": map_candidate,
            "georeference_candidate": is_georeference_candidate(classification.figure_class),
            "map_meta": map_meta,
        }

    entities, relationships, unresolved = build_entity_graph(records, report.manifest)
    location_candidates, location_evidence = derive_location_candidates(report, elements, records)
    evidence_index.update(location_evidence)
    write_json(paths.hierarchy / "entities.json", entities)
    write_json(paths.hierarchy / "relationships.json", relationships)
    write_json(paths.hierarchy / "unresolved.json", unresolved)

    n_other_tables = sum(1 for element in elements.values() if element.get("kind") == "table" and element.get("table_type") == "other")
    mapper_coverage = 1 - (n_other_tables / len(report.tables) if report.tables else 0)
    llm_trace = _mapper_trace(mapper)
    llm_mapper = llm_trace.get("mapper", {})
    llm_backend = llm_trace.get("backend", {})

    metadata = {
        "doc_id": report.doc_id,
        "report_slug": report.slug,
        "sha256": report.sha256,
        "stats": report.stats,
        "project_stage": classify_project_stage(records),
        "location_candidates": location_candidates,
        "sections": _section_views(report, elements),
        "elements": elements,
        "records": records,
        "entities": entities,
        "relationships": relationships,
        "_report_root": str(report.root),
        "build_trace": {
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "warnings": warnings,
            "stage_trace_ids": trace_ids,
            "element_cache": element_cache,
            "counters": {
                "input_index.n_elements": input_index["n_elements"],
                "hie.n_sections": len(report.sections),
                "hie.n_tables": len(report.tables),
                "hie.n_figures": len(report.figures),
                "hie.n_maps_processed": 0,
                "hie.cache_hits": cache_hits,
                "hie.cache_misses": cache_misses,
                "hierarchy.n_entities": len(entities),
                "hierarchy.n_relationships": len(relationships),
                "hierarchy.n_unresolved": len(unresolved),
                "hie.mapper_coverage": mapper_coverage,
                "guard.n_rejected": guard_n_rejected,
                "llm.n_calls": _counter(llm_backend.get("n_calls")),
                "llm.n_cache_hits": _counter(llm_backend.get("n_cache_hits")),
                "llm.n_retries": _counter(llm_backend.get("n_retries")),
                "llm.n_degraded": _counter(llm_mapper.get("n_degraded")) + _counter(llm_backend.get("n_degraded")),
                "llm.n_throttled": _counter(llm_mapper.get("n_throttled")) + _counter(llm_backend.get("n_throttled")),
                "vlm.n_calls": 0,
                "georef.n_processed": 0,
                "schema.validation_passed": 1,
                "schema.validation_failed": 0,
            },
            "llm": llm_trace,
        },
        "_evidence_index": evidence_index,
    }
    validate_report_metadata(metadata, evidence_index)
    write_json(paths.evidence_index_json, evidence_index)
    write_json(paths.report_metadata_json, _safe_metadata_for_write(metadata))
    write_json(paths.state_json, {
        "input_index": {
            "done": True,
            "trace_id": trace_ids["input_index"],
            "n_elements": input_index["n_elements"],
            "counters": {"input_index.n_elements": input_index["n_elements"]},
            "produces": {"input_index_sha256": sha256_file(paths.input_index_json)},
        },
        "hie": {
            "done": True,
            "trace_id": trace_ids["hie"],
            "consumes": {"input_index_sha256": sha256_file(paths.input_index_json)},
            "produces": {
                "report_metadata_sha256": sha256_file(paths.report_metadata_json),
                "evidence_index_sha256": sha256_file(paths.evidence_index_json),
            },
            "counters": metadata["build_trace"]["counters"],
            **metadata["build_trace"]["counters"],
        },
    })
    return metadata
