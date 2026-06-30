"""Report-level HIE reduce: hierarchy, locations, and project stage."""

from __future__ import annotations

import re
from collections import defaultdict

from .schemas import FIELDS


def entity_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(name).lower()).strip("-")
    return re.sub(r"-+", "-", slug) or "unknown"


def entity_id(level: str, name: str) -> str:
    return f"{level}:{entity_slug(name)}"


def by_type(records: dict[str, dict] | list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    iterable = records.values() if isinstance(records, dict) else records
    for record in iterable:
        grouped[record.get("_type", "other")].append(record)
    return dict(grouped)


def classify_project_stage(records: dict[str, dict] | list[dict]) -> str:
    grouped = by_type(records)
    if grouped.get("resource_estimate"):
        return "resource"
    if grouped.get("assay_interval"):
        return "target"
    if grouped.get("drillhole_collar") or grouped.get("drill_history"):
        return "early"
    return "desktop"


def _first_evidence(record: dict, *fields: str) -> list[str]:
    field_evidence = record.get("_field_evidence") or {}
    out = []
    for field in fields:
        evidence_id = field_evidence.get(field)
        if evidence_id:
            out.append(evidence_id)
    return out


def _confidence_from_records(records: list[dict]) -> str:
    confidences = [(record.get("provenance") or {}).get("confidence") for record in records]
    present = [conf for conf in confidences if conf]
    if not present:
        return "none"
    if all(conf == "high" for conf in present):
        return "high"
    if any(conf in ("high", "medium") for conf in present):
        return "medium"
    return "low"


def _record_element_id(record: dict) -> str | None:
    return (record.get("provenance") or {}).get("element_id")


def _new_entity(level: str, name: str, confidence: str = "medium") -> dict:
    return {
        "level": level,
        "name": name,
        "aliases": [],
        "attributes": {},
        "confidence": confidence,
        "parent_id": None,
        "record_ids": [],
        "element_ids": [],
        "evidence_ids": [],
    }


def _merge_entity(entities: dict[str, dict], eid: str, level: str, name: str, confidence: str = "medium") -> dict:
    entity = entities.setdefault(eid, _new_entity(level, name, confidence))
    if confidence == "high" or entity.get("confidence") in ("low", "none", None):
        entity["confidence"] = confidence
    return entity


def _attach(entity: dict, record: dict, evidence_ids: list[str] | None = None) -> None:
    record_id = record.get("_id")
    if record_id and record_id not in entity["record_ids"]:
        entity["record_ids"].append(record_id)
    element_id = _record_element_id(record)
    if element_id and element_id not in entity["element_ids"]:
        entity["element_ids"].append(element_id)
    for evidence_id in evidence_ids or []:
        if evidence_id not in entity["evidence_ids"]:
            entity["evidence_ids"].append(evidence_id)


def _add_relationship(relationships: list[dict], rel_type: str, source: str, target: str, confidence: str, evidence_ids: list[str] | None = None) -> None:
    key = (rel_type, source, target)
    if any((rel["type"], rel["from"], rel["to"]) == key for rel in relationships):
        return
    relationships.append({
        "type": rel_type,
        "from": source,
        "to": target,
        "confidence": confidence,
        "evidence_ids": evidence_ids or [],
    })


def build_entity_graph(records: dict[str, dict] | list[dict], manifest: dict) -> tuple[dict[str, dict], list[dict], list[dict]]:
    record_list = list(records.values()) if isinstance(records, dict) else list(records)
    grouped = by_type(record_list)
    entities: dict[str, dict] = {}
    relationships: list[dict] = []
    unresolved: list[dict] = []

    project_name = manifest.get("source_name") or manifest.get("report_slug") or "report"
    project_id = entity_id("project", manifest.get("report_slug", project_name))
    entities[project_id] = _new_entity("project", project_name, "high")

    collar_holes: set[str] = set()
    for record in grouped.get("drillhole_collar", []):
        hole_name = record.get("hole_id")
        if not hole_name:
            unresolved.append({"record_id": record.get("_id"), "reason": "drillhole_collar missing hole_id"})
            continue
        hid = entity_id("hole", hole_name)
        collar_holes.add(hid)
        entity = _merge_entity(entities, hid, "hole", hole_name, (record.get("provenance") or {}).get("confidence") or "high")
        entity["attributes"]["collar"] = {
            field.name: record[field.name]
            for field in FIELDS["drillhole_collar"]
            if field.name in record and field.name != "hole_id"
        }
        evidence_ids = _first_evidence(record, "hole_id", "easting", "northing", "eoh_m")
        _attach(entity, record, evidence_ids)
        record["entity_ids"] = [hid]

    zone_records: dict[str, list[dict]] = defaultdict(list)
    for record in grouped.get("assay_interval", []):
        record_entities: list[str] = []
        hole_name = record.get("hole_id")
        if hole_name:
            hid = entity_id("hole", hole_name)
            confidence = "high" if hid in collar_holes else "medium"
            entity = _merge_entity(entities, hid, "hole", hole_name, confidence)
            _attach(entity, record, _first_evidence(record, "hole_id"))
            record_entities.append(hid)
            if hid not in collar_holes:
                unresolved.append({"record_id": record.get("_id"), "entity_id": hid, "reason": "assay_interval hole_id has no matching collar"})

        zone_name = record.get("zone")
        if zone_name:
            zid = entity_id("zone", zone_name)
            zone_entity = _merge_entity(entities, zid, "zone", zone_name, "medium")
            _attach(zone_entity, record, _first_evidence(record, "zone"))
            zone_records[zid].append(record)
            record_entities.append(zid)
            _add_relationship(relationships, "part_of", zid, project_id, "medium", _first_evidence(record, "zone"))
            if hole_name:
                hid = entity_id("hole", hole_name)
                _add_relationship(relationships, "intersects", hid, zid, "medium", _first_evidence(record, "hole_id", "zone"))
                entities[hid]["parent_id"] = zid

        if record_entities:
            record["entity_ids"] = record_entities

    for record in grouped.get("resource_estimate", []):
        resource_entity_ids = []
        property_name = record.get("property")
        deposit_name = record.get("deposit")
        if property_name:
            pid = entity_id("property", property_name)
            property_entity = _merge_entity(entities, pid, "property", property_name, _confidence_from_records([record]))
            _attach(property_entity, record, _first_evidence(record, "property"))
            _add_relationship(relationships, "part_of", pid, project_id, "medium", _first_evidence(record, "property"))
            resource_entity_ids.append(pid)
        if deposit_name:
            did = entity_id("deposit", deposit_name)
            deposit_entity = _merge_entity(entities, did, "deposit", deposit_name, _confidence_from_records([record]))
            _attach(deposit_entity, record, _first_evidence(record, "deposit"))
            parent = entity_id("property", property_name) if property_name else project_id
            deposit_entity["parent_id"] = parent
            _add_relationship(relationships, "part_of", did, parent, "medium", _first_evidence(record, "deposit"))
            matching_zone_id = entity_id("zone", deposit_name)
            if matching_zone_id in zone_records:
                support_evidence = _first_evidence(record, "deposit", "tonnage", "grade_gpt")
                for zone_record in zone_records[matching_zone_id][:3]:
                    support_evidence.extend(_first_evidence(zone_record, "zone", "hole_id", "au_gpt"))
                _add_relationship(relationships, "supports", matching_zone_id, did, "medium", support_evidence)
            resource_entity_ids.append(did)
        if resource_entity_ids:
            record["entity_ids"] = resource_entity_ids

    for record in grouped.get("drill_history", []):
        target_name = record.get("target_area")
        if not target_name:
            continue
        tid = entity_id("target", target_name)
        entity = _merge_entity(entities, tid, "target", target_name, _confidence_from_records([record]))
        _attach(entity, record, _first_evidence(record, "target_area"))
        _add_relationship(relationships, "part_of", tid, project_id, "medium", _first_evidence(record, "target_area"))
        record.setdefault("entity_ids", []).append(tid)

    for record in grouped.get("tenure", []):
        property_name = record.get("property")
        if not property_name:
            continue
        pid = entity_id("property", property_name)
        entity = _merge_entity(entities, pid, "property", property_name, _confidence_from_records([record]))
        _attach(entity, record, _first_evidence(record, "property", "claim_id"))
        _add_relationship(relationships, "part_of", pid, project_id, "medium", _first_evidence(record, "property"))
        record.setdefault("entity_ids", []).append(pid)

    for eid, entity in entities.items():
        if eid == project_id:
            continue
        if not entity.get("parent_id"):
            for rel in relationships:
                if rel["from"] == eid and rel["type"] == "part_of":
                    entity["parent_id"] = rel["to"]
                    break
    return entities, relationships, unresolved


_LAT_LON_PAIR = re.compile(
    r"(?P<lat>\d{1,2}(?:\.\d+)?)\s*(?:°)?\s*(?P<ns>[NS])[^\dA-Z-]{0,20}"
    r"(?P<lon>\d{1,3}(?:\.\d+)?)\s*(?:°)?\s*(?P<ew>[EW])",
    re.I,
)

_UTM_REF = re.compile(
    r"\bUTM\s+zone\s+(?P<zone>\d{1,2}[A-Z]?)\s*,?\s*(?P<datum>NAD\s?\d{2}|WGS\s?84)?"
    r"[^0-9]{0,80}(?P<easting>\d{5,7}(?:\.\d+)?)\s*m?\s*(?:E|East|Easting)\b"
    r"[^0-9]{0,80}(?P<northing>\d{6,8}(?:\.\d+)?)\s*m?\s*(?:N|North|Northing)\b",
    re.I,
)
_NTS_REF = re.compile(r"\bNTS\s+(?P<nts>\d{2}[A-Z]\d{2})\b", re.I)


def _numeric_value(record: dict, field: str):
    value = record.get(field)
    return value.get("value") if isinstance(value, dict) else None


def derive_location_candidates(report, elements: dict[str, dict], records: dict[str, dict] | list[dict] | None = None) -> tuple[list[dict], dict[str, dict]]:
    candidates: list[dict] = []
    evidence: dict[str, dict] = {}
    record_list = list(records.values()) if isinstance(records, dict) else list(records or [])

    for record in record_list:
        if record.get("_type") != "tenure":
            continue
        lat = _numeric_value(record, "latitude")
        lon = _numeric_value(record, "longitude")
        if lat is None or lon is None:
            continue
        provenance = record.get("provenance") or {}
        confidence = "high" if provenance.get("confidence") == "high" else "medium"
        candidates.append({
            "rank": len(candidates) + 1,
            "method": "tenure_table",
            "element_id": provenance.get("element_id"),
            "lat": lat,
            "lon": lon,
            "crs": "EPSG:4326",
            "bounds": {"min_lon": lon - 0.05, "min_lat": lat - 0.05, "max_lon": lon + 0.05, "max_lat": lat + 0.05},
            "confidence": confidence,
            "evidence_ids": [eid for field, eid in (record.get("_field_evidence") or {}).items() if field in ("latitude", "longitude")],
        })
        break

    for element_id, element in elements.items():
        georef = (element.get("map_meta") or {}).get("georef")
        if georef and georef.get("bounds"):
            bounds = georef["bounds"]
            candidate = {
                "rank": len(candidates) + 1,
                "method": "georef_figure",
                "element_id": element_id,
                "lat": (bounds["min_lat"] + bounds["max_lat"]) / 2,
                "lon": (bounds["min_lon"] + bounds["max_lon"]) / 2,
                "bounds": bounds,
                "confidence": georef.get("confidence", "medium"),
            }
            candidates.append(candidate)

    for section in report.sections:
        text = report.section_text(section)
        match = _LAT_LON_PAIR.search(text)
        if not match:
            continue
        lat = float(match.group("lat"))
        lon = float(match.group("lon"))
        if match.group("ns").upper() == "S":
            lat *= -1
        if match.group("ew").upper() == "W":
            lon *= -1
        evidence_id = f"ev:text:{section.get('slug', section.get('idx', 'section'))}:latlon"
        evidence[evidence_id] = {
            "element_id": section.get("slug"),
            "page": section.get("page_start"),
            "source_kind": "text_token",
            "source_ref": section.get("text_file"),
            "raw_token": match.group(0),
        }
        candidates.append({
            "rank": len(candidates) + 1,
            "method": "text_latlon",
            "element_id": section.get("slug"),
            "lat": lat,
            "lon": lon,
            "crs": "EPSG:4326",
            "confidence": "medium",
            "evidence_id": evidence_id,
        })
        break

    seen_utm: set[tuple[str, float, float]] = set()
    for section in report.sections:
        text = report.section_text(section)
        for match in _UTM_REF.finditer(text):
            zone = match.group("zone").upper()
            easting = float(match.group("easting"))
            northing = float(match.group("northing"))
            key = (zone, easting, northing)
            if key in seen_utm:
                continue
            seen_utm.add(key)
            datum = (match.group("datum") or "").replace(" ", "").upper() or None
            evidence_id = f"ev:text:{section.get('slug', section.get('idx', 'section'))}:utm:{len(seen_utm)}"
            evidence[evidence_id] = {
                "element_id": section.get("slug"),
                "page": section.get("page_start"),
                "source_kind": "text_token",
                "source_ref": section.get("text_file"),
                "raw_token": match.group(0),
            }
            candidates.append({
                "rank": len(candidates) + 1,
                "method": "text_utm",
                "element_id": section.get("slug"),
                "utm_zone": zone,
                "datum": datum,
                "easting": easting,
                "northing": northing,
                "crs": f"{datum or 'UTM'} zone {zone}",
                "confidence": "medium",
                "evidence_id": evidence_id,
            })

    seen_nts: set[str] = set()
    for section in report.sections:
        text = report.section_text(section)
        for match in _NTS_REF.finditer(text):
            nts = match.group("nts").upper()
            if nts in seen_nts:
                continue
            seen_nts.add(nts)
            evidence_id = f"ev:text:{section.get('slug', section.get('idx', 'section'))}:nts:{len(seen_nts)}"
            evidence[evidence_id] = {
                "element_id": section.get("slug"),
                "page": section.get("page_start"),
                "source_kind": "text_token",
                "source_ref": section.get("text_file"),
                "raw_token": match.group(0),
            }
            candidates.append({
                "rank": len(candidates) + 1,
                "method": "text_nts",
                "element_id": section.get("slug"),
                "nts": nts,
                "confidence": "low",
                "evidence_id": evidence_id,
            })

    if not candidates:
        for section in report.sections:
            text = report.section_text(section)
            match = re.search(r"\bnear\s+([A-Z][A-Za-z .'-]{2,80})", text)
            if not match:
                continue
            evidence_id = f"ev:text:{section.get('slug', section.get('idx', 'section'))}:place"
            evidence[evidence_id] = {
                "element_id": section.get("slug"),
                "page": section.get("page_start"),
                "source_kind": "text_token",
                "source_ref": section.get("text_file"),
                "raw_token": match.group(0),
            }
            candidates.append({
                "rank": 1,
                "method": "place_name",
                "element_id": section.get("slug"),
                "place_name": match.group(1).strip(),
                "confidence": "low",
                "evidence_id": evidence_id,
            })
            break

    confidence_rank = {"high": 0, "medium": 1, "low": 2, "none": 3}
    method_rank = {"tenure_table": 0, "georef_figure": 1, "text_latlon": 2, "text_utm": 3, "text_nts": 4, "place_name": 5}
    candidates.sort(key=lambda item: (method_rank.get(item.get("method"), 9), confidence_rank.get(item.get("confidence", "none"), 9)))
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    return candidates, evidence
