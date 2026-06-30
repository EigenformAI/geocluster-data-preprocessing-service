"""HIE map-reduce builder for ``report_metadata.json``."""

from __future__ import annotations

import uuid
from pathlib import Path

from pipeline.util import read_json, sha256_file, write_json

from .assertions import harvest_assertions
from .figures import is_georeference_candidate, is_map_candidate
from .llm import FigureClassifier, mapper_cache_key, select_figure_classifier, select_table_mapper
from .paths import AnalysisPaths
from .reduce import build_entity_graph, classify_project_stage, derive_location_candidates
from .report_agent import build_assertion_graph
from .schemas import FigureClassification
from .spans import SpanExtractor
from .source import Report
from .tables import TableMapper, normalize_table
from .validation import ReportMetadataValidationError, validate_report_metadata


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


def _section_slug_for_table(report: Report, table: dict) -> str | None:
    section_ref = table.get("section")
    if section_ref is not None:
        ref = str(section_ref)
        for section in report.sections:
            if section.get("slug") == ref or (
                section.get("number") is not None and str(section.get("number")) == ref
            ):
                return section.get("slug")
    try:
        page = int(table.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    section = report.section_for_page(page) if page else None
    if section and section.get("slug"):
        return section["slug"]
    return str(section_ref) if section_ref is not None else None


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


def _process_map_figure(report: Report, paths: AnalysisPaths, figure: dict, map_processor) -> tuple[dict, dict[str, dict], list[dict]]:
    figure_id = figure["id"]
    if map_processor is None:
        return (
            {"status": "skipped", "reason": "peace_unavailable", "georef": None},
            {},
            [{"kind": "map_hie_skipped", "element_id": figure_id, "reason": "peace_unavailable"}],
        )
    output_dir = paths.hie_map_dir(figure_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = map_processor.process_map(report.root, figure, output_dir)
    components = result.get("components") or result.get("regions") or []
    evidence: dict[str, dict] = {}
    normalized_components = []
    evidence_ids = []
    for index, component in enumerate(components, start=1):
        component_id = str(component.get("id") or component.get("component_id") or component.get("type") or component.get("label") or f"component_{index}")
        label = str(component.get("label") or component.get("type") or component.get("kind") or component_id)
        evidence_id = f"ev:map_component:{figure_id}:{component_id}"
        crop_ref = component.get("crop_ref") or component.get("crop_uri") or figure.get("image")
        evidence[evidence_id] = {
            "element_id": figure_id,
            "page": figure.get("page"),
            "source_kind": "map_component",
            "source_ref": crop_ref,
            "raw_token": label,
            "component_id": component_id,
            "component_type": label,
            "bbox": component.get("bbox"),
            "confidence": component.get("confidence"),
        }
        normalized_components.append({**component, "id": component_id, "type": label, "evidence_id": evidence_id})
        evidence_ids.append(evidence_id)
    map_meta = {
        "status": result.get("status", "processed"),
        "processor": result.get("processor") or getattr(map_processor, "name", map_processor.__class__.__name__),
        "components": normalized_components,
        "component_evidence_ids": evidence_ids,
        "georef": result.get("georef"),
        "warnings": result.get("warnings", []),
    }
    write_json(paths.hie_map_components_json(figure_id), {"figure_id": figure_id, "map_meta": map_meta})
    warnings = [{"kind": "map_hie_warning", "element_id": figure_id, "message": warning} for warning in result.get("warnings", [])]
    return map_meta, evidence, warnings


def build_report_metadata(
    report_root: Path | str,
    mapper: TableMapper | None = None,
    force: bool = False,
    figure_classifier: FigureClassifier | None = None,
    span_extractor: SpanExtractor | None = None,
    map_processor=None,
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
    maps_processed = 0
    map_components = 0

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
            "section_slug": _section_slug_for_table(report, table),
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
            map_meta, map_evidence, map_warnings = _process_map_figure(report, paths, figure, map_processor)
            evidence_index.update(map_evidence)
            warnings.extend(map_warnings)
            if map_meta.get("status") == "processed":
                maps_processed += 1
                map_components += len(map_meta.get("components") or [])
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
    section_views = _section_views(report, elements)
    assertions, assertion_evidence, harvest_trace = harvest_assertions(report.root, section_views, entities, span_extractor)
    evidence_index.update(assertion_evidence)
    write_json(paths.hierarchy / "entities.json", entities)
    write_json(paths.hierarchy / "relationships.json", relationships)
    write_json(paths.hierarchy / "unresolved.json", unresolved)
    write_json(paths.hierarchy / "assertions.json", assertions)

    n_other_tables = sum(1 for element in elements.values() if element.get("kind") == "table" and element.get("table_type") == "other")
    mapper_coverage = 1 - (n_other_tables / len(report.tables) if report.tables else 0)
    llm_trace = _mapper_trace(mapper)
    llm_mapper = llm_trace.get("mapper", {})
    llm_backend = llm_trace.get("backend", {})

    counters = {
        "input_index.n_elements": input_index["n_elements"],
        "hie.n_sections": len(report.sections),
        "hie.n_tables": len(report.tables),
        "hie.n_figures": len(report.figures),
        "hie.n_maps_processed": maps_processed,
        "hie.n_map_components": map_components,
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
        "schema.validation_passed": 0,
        "schema.validation_failed": 0,
    }
    counters.update(harvest_trace)

    metadata = {
        "doc_id": report.doc_id,
        "report_slug": report.slug,
        "sha256": report.sha256,
        "stats": report.stats,
        "project_stage": classify_project_stage(records),
        "location_candidates": location_candidates,
        "sections": section_views,
        "elements": elements,
        "records": records,
        "entities": entities,
        "relationships": relationships,
        "assertions": assertions,
        "_report_root": str(report.root),
        "build_trace": {
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "warnings": warnings,
            "stage_trace_ids": trace_ids,
            "element_cache": element_cache,
            "counters": counters,
            "llm": llm_trace,
        },
        "_evidence_index": evidence_index,
    }
    assertion_graph = build_assertion_graph(metadata, evidence_index)

    def _state_payload(done: bool, produces: dict | None = None, error: str | None = None) -> dict:
        hie_state = {
            "done": done,
            "trace_id": trace_ids["hie"],
            "consumes": {"input_index_sha256": sha256_file(paths.input_index_json)},
            "counters": counters,
            **counters,
        }
        if produces is not None:
            hie_state["produces"] = produces
        if error is not None:
            hie_state["error"] = error
        return {
            "input_index": {
                "done": True,
                "trace_id": trace_ids["input_index"],
                "n_elements": input_index["n_elements"],
                "counters": {"input_index.n_elements": input_index["n_elements"]},
                "produces": {"input_index_sha256": sha256_file(paths.input_index_json)},
            },
            "hie": hie_state,
        }

    try:
        validate_report_metadata(metadata, evidence_index)
    except ReportMetadataValidationError as exc:
        counters["schema.validation_failed"] = 1
        write_json(paths.state_json, _state_payload(False, error=str(exc)))
        raise
    counters["schema.validation_passed"] = 1

    write_json(paths.evidence_index_json, evidence_index)
    write_json(paths.assertions_json, assertion_graph)
    write_json(paths.report_metadata_json, _safe_metadata_for_write(metadata))
    write_json(paths.state_json, _state_payload(True, {
        "report_metadata_sha256": sha256_file(paths.report_metadata_json),
        "evidence_index_sha256": sha256_file(paths.evidence_index_json),
        "assertions_sha256": sha256_file(paths.assertions_json),
    }))
    return metadata
