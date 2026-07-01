"""HIE map-reduce builder for ``report_metadata.json``."""

from __future__ import annotations

import hashlib
import re
import unicodedata
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


IMAGEGUARD_VERIFIER_VERSION = "inline-v1"
_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d+(?:,\d{3})*(?:\.\d+)?")
_EASTING = re.compile(r"(?:(?:easting|east)\D{0,12}(?P<prefix>\d[\d,]*(?:\.\d+)?)|(?P<suffix>\d[\d,]*(?:\.\d+)?)\s*(?:m\s*)?E\b)", re.I)
_NORTHING = re.compile(r"(?:(?:northing|north)\D{0,12}(?P<prefix>\d[\d,]*(?:\.\d+)?)|(?P<suffix>\d[\d,]*(?:\.\d+)?)\s*(?:m\s*)?N\b)", re.I)
_UTM_ZONE = re.compile(r"\b(?:UTM\s+)?(?:NAD\s*83\s+|N83\s+|WGS\s*84\s+)?zone\s+(?P<zone>\d{1,2})(?P<band>[A-Z])?\b", re.I)
_TOKEN = re.compile(r"[a-z0-9]+")


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


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


def _tokens(value: str | None) -> list[str]:
    return _TOKEN.findall(_fold(value))


def _image_label_text_match(label: str, source_text: str) -> bool:
    label_tokens = _tokens(label)
    if not label_tokens:
        return False
    haystack = f" {' '.join(_tokens(source_text))} "
    needle = f" {' '.join(label_tokens)} "
    return needle in haystack


def _text_match_evidence(report: Report, label: str, annotation_kind: str) -> tuple[str | None, dict[str, dict]]:
    for section in report.sections:
        text = report.section_text(section)
        if not _image_label_text_match(label, text):
            continue
        slug = str(section.get("slug") or section.get("idx") or "section")
        digest = hashlib.sha1(f"{slug}:{annotation_kind}:{label}".encode("utf-8")).hexdigest()[:12]
        evidence_id = f"ev:text:{slug}:image_label:{annotation_kind}:{digest}"
        return evidence_id, {
            evidence_id: {
                "element_id": section.get("slug"),
                "page": section.get("page_start"),
                "source_kind": "text_token",
                "source_ref": section.get("text_file"),
                "raw_token": label,
                "span": label,
            }
        }
    return None, {}


def _number_values(value: str) -> list[float]:
    numbers = []
    for match in _NUMBER.finditer(value):
        try:
            numbers.append(float(match.group(0).replace(",", "")))
        except ValueError:
            continue
    return numbers


def _matched_number(match: re.Match[str] | None) -> float | None:
    if not match:
        return None
    raw = match.group("prefix") or match.group("suffix")
    if raw is None:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _datum(value: str) -> str | None:
    folded = _fold(value).replace(" ", "")
    if "nad83" in folded or "n83" in folded:
        return "NAD83"
    if "nad27" in folded or "n27" in folded:
        return "NAD27"
    if "wgs84" in folded:
        return "WGS84"
    return None


def _utm_zone_number(value: str) -> str | None:
    match = _UTM_ZONE.search(value)
    return match.group("zone") if match else None


def _text_utm_candidates(location_candidates: list[dict] | None) -> list[dict]:
    return [candidate for candidate in location_candidates or [] if candidate.get("method") == "text_utm"]


def _text_utm_crs_match(value: str, location_candidates: list[dict] | None) -> str | None:
    zone = _utm_zone_number(value)
    datum = _datum(value)
    if not zone:
        return None
    for candidate in _text_utm_candidates(location_candidates):
        candidate_zone = re.match(r"\d+", str(candidate.get("utm_zone") or ""))
        if not candidate_zone or candidate_zone.group(0) != zone:
            continue
        candidate_datum = str(candidate.get("datum") or "").replace(" ", "").upper() or None
        if datum and candidate_datum and datum != candidate_datum:
            continue
        return candidate.get("evidence_id")
    return None


def _text_utm_coordinate_match(value: str, location_candidates: list[dict] | None) -> str | None:
    numbers = _number_values(value)
    if not numbers:
        return None
    for candidate in _text_utm_candidates(location_candidates):
        candidate_values = [candidate.get("easting"), candidate.get("northing")]
        for number in numbers:
            if any(candidate_value is not None and abs(float(candidate_value) - number) <= 1 for candidate_value in candidate_values):
                return candidate.get("evidence_id")
    return None


def _verification(status: str, methods: list[str] | None = None, evidence_ids: list[str] | None = None) -> dict:
    return {
        "verification_status": status,
        "verification_methods": methods or [],
        "verification_evidence_ids": evidence_ids or [],
        "imageguard_version": IMAGEGUARD_VERIFIER_VERSION,
    }


def _verify_image_label(
    report: Report,
    component_type: str,
    annotation_kind: str,
    value: str,
    location_candidates: list[dict] | None = None,
) -> tuple[dict, dict[str, dict]]:
    text_evidence_id, text_evidence = _text_match_evidence(report, value, annotation_kind)
    if annotation_kind == "legend_entry":
        if component_type != "legend":
            return _verification("unverifiable", ["legend_entry_from_non_legend_component"]), {}
        if text_evidence_id:
            return _verification("corroborated", ["text_label_match"], [text_evidence_id]), text_evidence
        return _verification("unverifiable", ["no_text_label_match"]), {}

    if annotation_kind == "place_name":
        if text_evidence_id:
            return _verification("corroborated", ["text_label_match"], [text_evidence_id]), text_evidence
        return _verification("unverifiable", ["no_text_place_match"]), {}

    if annotation_kind == "crs":
        evidence_id = _text_utm_crs_match(value, location_candidates)
        if evidence_id:
            return _verification("corroborated", ["text_crs_match"], [evidence_id]), {}
        if _utm_zone_number(value):
            return _verification("plausible", ["utm_zone_well_formed"]), {}
        return _verification("unverifiable", ["crs_not_well_formed"]), {}

    if annotation_kind == "coordinate":
        easting = _matched_number(_EASTING.search(value))
        northing = _matched_number(_NORTHING.search(value))
        if easting is not None and not 166021 <= easting <= 833978:
            return _verification("unverifiable", ["utm_easting_out_of_range"]), {}
        if northing is not None and not 0 <= northing <= 10000000:
            return _verification("unverifiable", ["utm_northing_out_of_range"]), {}
        evidence_id = _text_utm_coordinate_match(value, location_candidates)
        if evidence_id:
            return _verification("corroborated", ["text_utm_coordinate_match"], [evidence_id]), {}
        numbers = _number_values(value)
        if numbers and all(number < 10000 for number in numbers):
            return _verification("plausible", ["partial_grid_tick"]), {}
        if easting is not None or northing is not None:
            return _verification("plausible", ["utm_coordinate_range_plausible"]), {}
        return _verification("unverifiable", ["coordinate_not_well_formed"]), {}

    if annotation_kind == "scale":
        if text_evidence_id:
            return _verification("corroborated", ["text_label_match"], [text_evidence_id]), text_evidence
        if re.search(r"\b1\s*[:/]\s*\d{2,}\b", value) or re.search(r"\b\d+(?:\.\d+)?\s*(?:m|km)\b", value, re.I):
            return _verification("plausible", ["scale_format_plausible"]), {}
        return _verification("unverifiable", ["scale_not_well_formed"]), {}

    if text_evidence_id:
        return _verification("corroborated", ["text_label_match"], [text_evidence_id]), text_evidence
    return _verification("unverifiable", ["no_offline_corroboration"]), {}


def _image_labels_for_component(
    report: Report,
    image_reader,
    figure: dict,
    component_id: str,
    component_type: str,
    crop_ref: str | None,
    parent_evidence_id: str,
    location_candidates: list[dict] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """VLM-read one detected crop into ``image_label`` evidence grounded to the crop.

    Reads are typed (``read_by="vlm:..."``) and carry ``input_evidence_ids`` back to the
    parent ``map_component`` crop. They are descriptive labels by default; numbers only
    become claim-authorizing evidence after an explicit imageguard corroboration verdict.
    """
    figure_id = figure["id"]
    if not crop_ref:
        return [], {}
    crop_abs = report.root / crop_ref
    base = {
        "element_id": figure_id,
        "page": figure.get("page"),
        "source_kind": "image_label",
        "source_ref": crop_ref,
        "component_id": component_id,
        "component_type": component_type,
        "input_evidence_ids": [parent_evidence_id],
        "read_by": f"vlm:{getattr(image_reader, 'model', 'vlm')}",
    }
    annotations: list[tuple[str, str]] = []
    try:
        if component_type == "legend":
            read = image_reader.read_legend(crop_abs, figure_id=figure_id, component_id=component_id)
            if read.readable:
                annotations = [("legend_entry", entry) for entry in read.entries if entry and entry.strip()]
        elif component_type == "main_map":
            read = image_reader.read_geospatial(crop_abs, figure_id=figure_id, component_id=component_id)
            if read.readable:
                if read.crs:
                    annotations.append(("crs", read.crs))
                if read.scale:
                    annotations.append(("scale", read.scale))
                annotations += [("coordinate", value) for value in read.coordinates if value and value.strip()]
                annotations += [("place_name", value) for value in read.place_names if value and value.strip()]
    except Exception:
        return [], {}
    labels = []
    verification_evidence: dict[str, dict] = {}
    for kind, value in annotations:
        verdict, backing = _verify_image_label(report, component_type, kind, value, location_candidates)
        verification_evidence.update(backing)
        labels.append({**base, "raw_token": value, "span": value, "annotation_kind": kind, **verdict})
    return labels, verification_evidence


def _process_map_figure(
    report: Report,
    paths: AnalysisPaths,
    figure: dict,
    map_processor,
    image_reader=None,
    location_candidates: list[dict] | None = None,
) -> tuple[dict, dict[str, dict], list[dict]]:
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
    image_label_ids: list[str] = []
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
        if image_reader is not None:
            label_evidence_items, verification_evidence = _image_labels_for_component(
                report,
                image_reader,
                figure,
                component_id,
                label,
                crop_ref,
                evidence_id,
                location_candidates,
            )
            evidence.update(verification_evidence)
            for offset, label_evidence in enumerate(label_evidence_items):
                image_label_id = f"ev:image_label:{figure_id}:{component_id}:{offset}"
                evidence[image_label_id] = label_evidence
                image_label_ids.append(image_label_id)
    map_meta = {
        "status": result.get("status", "processed"),
        "processor": result.get("processor") or getattr(map_processor, "name", map_processor.__class__.__name__),
        "components": normalized_components,
        "component_evidence_ids": evidence_ids,
        "image_label_evidence_ids": image_label_ids,
        "n_image_labels": len(image_label_ids),
        "georef": result.get("georef"),
        "warnings": result.get("warnings", []),
        "imageguard_version": IMAGEGUARD_VERIFIER_VERSION,
    }
    write_json(paths.hie_map_components_json(figure_id), {"figure_id": figure_id, "map_meta": map_meta})
    warnings = [{"kind": "map_hie_warning", "element_id": figure_id, "message": warning} for warning in result.get("warnings", [])]
    return map_meta, evidence, warnings


def _component_types(map_meta: dict | None) -> set[str]:
    return {str(component.get("type") or "") for component in (map_meta or {}).get("components") or []}


def _has_main_map(map_meta: dict | None) -> bool:
    return "main_map" in _component_types(map_meta)


def _description_fragment_hint(description: str) -> bool:
    text = description.casefold()
    return any(term in text for term in ("legend", "scale", "north arrow", "stratigraphic column", "map fragment"))


def _should_process_figure_with_map_path(classification: FigureClassification, page_has_map_candidate: bool, description: str) -> bool:
    if is_map_candidate(classification.figure_class):
        return True
    if classification.figure_class == "other" and classification.confidence in {"low", "none"}:
        return True
    return page_has_map_candidate and _description_fragment_hint(description)


def _fragment_relation(component_types: set[str]) -> str:
    if "legend" in component_types:
        return "legend_for"
    if "scale" in component_types:
        return "scale_for"
    if "stratigraphic_column" in component_types:
        return "stratigraphic_column_for"
    return "related_map_fragment"


def _link_same_page_map_fragments(elements: dict[str, dict]) -> int:
    by_page: dict[int, list[tuple[str, dict]]] = {}
    for element_id, element in elements.items():
        if element.get("kind") != "figure" or not element.get("map_meta"):
            continue
        try:
            page = int(element.get("page") or 0)
        except (TypeError, ValueError):
            continue
        by_page.setdefault(page, []).append((element_id, element))

    n_links = 0
    for figures in by_page.values():
        parent_maps = [(element_id, element) for element_id, element in figures if _has_main_map(element.get("map_meta"))]
        if len(parent_maps) != 1:
            continue
        parent_id, parent = parent_maps[0]
        parent_meta = parent.get("map_meta") or {}
        for fragment_id, fragment in figures:
            if fragment_id == parent_id:
                continue
            fragment_meta = fragment.get("map_meta") or {}
            component_types = _component_types(fragment_meta)
            if "main_map" in component_types or not (component_types & {"legend", "scale", "stratigraphic_column"}):
                continue
            image_label_ids = list(fragment_meta.get("image_label_evidence_ids") or [])
            link = {
                "figure_id": fragment_id,
                "relation": _fragment_relation(component_types),
                "confidence": "low",
                "basis": "same_page_legend_fragment",
                "image_label_evidence_ids": image_label_ids,
            }
            links = parent_meta.setdefault("related_figures", [])
            if any(existing.get("figure_id") == fragment_id for existing in links):
                continue
            links.append(link)
            related_ids = parent_meta.setdefault("related_image_label_evidence_ids", [])
            for evidence_id in image_label_ids:
                if evidence_id not in related_ids:
                    related_ids.append(evidence_id)
            n_links += 1
    return n_links


def build_report_metadata(
    report_root: Path | str,
    mapper: TableMapper | None = None,
    force: bool = False,
    figure_classifier: FigureClassifier | None = None,
    span_extractor: SpanExtractor | None = None,
    map_processor=None,
    image_reader=None,
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
    image_labels = 0
    related_figure_links = 0

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

    preliminary_location_candidates, preliminary_location_evidence = derive_location_candidates(report, elements, records)
    evidence_index.update(preliminary_location_evidence)

    figures = report.figures
    figure_descriptions = {figure["id"]: report.figure_description(figure) for figure in figures}
    page_has_map_candidate: dict[int, bool] = {}
    for figure in figures:
        classification = input_figure_classifications.get(figure["id"])
        if classification is None:
            classification = figure_classifier.classify_figure(figure, figure_descriptions.get(figure["id"], ""))
        try:
            page = int(figure.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        page_has_map_candidate[page] = page_has_map_candidate.get(page, False) or is_map_candidate(classification.figure_class)

    for figure in figures:
        classification = input_figure_classifications.get(figure["id"])
        if classification is None:
            classification = figure_classifier.classify_figure(figure, figure_descriptions.get(figure["id"], ""))
        description_map_candidate = is_map_candidate(classification.figure_class)
        try:
            page = int(figure.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        should_process_map = _should_process_figure_with_map_path(
            classification,
            page_has_map_candidate.get(page, False),
            figure_descriptions.get(figure["id"], ""),
        )
        map_candidate = description_map_candidate
        map_candidate_provenance = "description" if description_map_candidate else "none"
        map_meta = None
        if should_process_map:
            map_meta, map_evidence, map_warnings = _process_map_figure(
                report,
                paths,
                figure,
                map_processor,
                image_reader,
                preliminary_location_candidates,
            )
            evidence_index.update(map_evidence)
            warnings.extend(map_warnings)
            peace_map_like = map_meta.get("status") == "processed" and _has_main_map(map_meta)
            if map_meta.get("status") == "processed":
                map_meta["map_like"] = peace_map_like
                map_meta["map_like_provenance"] = "peace"
            if peace_map_like:
                map_candidate = True
                if not description_map_candidate:
                    map_candidate_provenance = "peace"
            if map_meta.get("status") == "processed":
                maps_processed += 1
                map_components += len(map_meta.get("components") or [])
                image_labels += int(map_meta.get("n_image_labels") or 0)
        figure_hie = {
            "figure_id": figure["id"],
            "page": figure.get("page"),
            "figure_class": classification.figure_class,
            "figure_class_provenance": "description",
            "classification": classification.to_dict(),
            "map_candidate": map_candidate,
            "map_candidate_provenance": map_candidate_provenance,
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
            "bbox": figure.get("bbox"),
            "ocr_page_dimensions": figure.get("ocr_page_dimensions"),
            "figure_class": classification.figure_class,
            "figure_class_provenance": "description",
            "map_candidate": map_candidate,
            "map_candidate_provenance": map_candidate_provenance,
            "georeference_candidate": is_georeference_candidate(classification.figure_class),
            "map_meta": map_meta,
        }

    related_figure_links = _link_same_page_map_fragments(elements)
    for element_id, element in elements.items():
        if element.get("kind") == "figure" and element.get("map_meta"):
            write_json(paths.hie_map_components_json(element_id), {"figure_id": element_id, "map_meta": element["map_meta"]})

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
    image_label_items = [item for item in evidence_index.values() if item.get("source_kind") == "image_label"]

    counters = {
        "input_index.n_elements": input_index["n_elements"],
        "hie.n_sections": len(report.sections),
        "hie.n_tables": len(report.tables),
        "hie.n_figures": len(report.figures),
        "hie.n_maps_processed": maps_processed,
        "hie.n_map_components": map_components,
        "hie.n_image_labels": image_labels,
        "hie.n_related_figure_links": related_figure_links,
        "hie.cache_hits": cache_hits,
        "hie.cache_misses": cache_misses,
        "imageguard.version": IMAGEGUARD_VERIFIER_VERSION,
        "imageguard.n_corroborated": sum(1 for item in image_label_items if item.get("verification_status") == "corroborated"),
        "imageguard.n_plausible": sum(1 for item in image_label_items if item.get("verification_status") == "plausible"),
        "imageguard.n_unverifiable": sum(1 for item in image_label_items if item.get("verification_status") == "unverifiable"),
        "imageguard.n_rejected": sum(1 for item in image_label_items if item.get("verification_status") == "rejected"),
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
        "vlm.n_calls": _counter((getattr(image_reader, "telemetry", {}) or {}).get("n_vlm_calls")),
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
