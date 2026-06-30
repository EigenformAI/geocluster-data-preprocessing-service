"""Deterministic table normalization once columns have been mapped."""

from __future__ import annotations

import re
from typing import Protocol

from . import numguard, units
from .schemas import FIELDS, ColumnMap, MappedTable, field_unit_kind, numeric_fields

_QUALIFIER_RE = re.compile(r"(?i)^(incl|inc\.|and incl|includes|including|sub-?interval)")


class TableMapper(Protocol):
    def map_table(self, table: dict, rows: list[list[str]]) -> MappedTable: ...


def _cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if 0 <= idx < len(row) else ""


def _is_aggregate(category: str) -> bool:
    category = category.lower()
    return any(item in category for item in ("total", "subtotal", "m&i", "measured & indicated", "measured and indicated"))


def _mapping_dict(mapping: MappedTable | dict) -> dict:
    if isinstance(mapping, MappedTable):
        return mapping.to_dict()
    return mapping


def _column_dict(column: ColumnMap | dict) -> dict:
    if isinstance(column, ColumnMap):
        return column.to_dict()
    return column


def _record_numeric_raw_values(record: dict) -> list[str]:
    values: list[str] = []
    for value in record.values():
        if isinstance(value, dict) and "raw_value" in value and value.get("raw_value"):
            values.append(str(value["raw_value"]))
    return values


def build_records(rows: list[list[str]], mapping: MappedTable | dict, table: dict) -> tuple[list[dict], dict[str, dict]]:
    mapped = _mapping_dict(mapping)
    table_type = mapped.get("table_type", "other")
    fields = {field.name for field in FIELDS.get(table_type, [])}
    colmap: dict[str, tuple[int, str | None]] = {}
    for column in mapped.get("columns", []):
        c = _column_dict(column)
        canonical = c.get("canonical_field")
        if canonical in fields and canonical not in colmap:
            colmap[canonical] = (int(c["source_index"]), c.get("unit"))
    if not colmap:
        return [], {}

    carry = [field for field in mapped.get("carry_fields", []) if field in colmap]
    numeric = numeric_fields(table_type)
    table_id = table["id"]
    provenance = {
        "element_id": table_id,
        "table_id": table_id,
        "page": table.get("page"),
        "confidence": table.get("confidence"),
        "method": table.get("method"),
    }
    carried: dict[str, str] = {}
    current_zone = ""
    records: list[dict] = []
    evidence: dict[str, dict] = {}

    for row_index, row in enumerate(rows[1:], start=1):
        raw = {field: _cell(row, idx) for field, (idx, _) in colmap.items()}
        is_subinterval = False
        if table_type == "assay_interval":
            hole_id = (raw.get("hole_id") or "").strip()
            if hole_id and _QUALIFIER_RE.match(hole_id):
                is_subinterval = True
                raw["hole_id"] = ""

        for field in carry:
            if raw.get(field):
                carried[field] = raw[field]
            elif field in carried:
                raw[field] = carried[field]

        numeric_present = any(raw.get(field) for field in numeric)
        if table_type == "assay_interval" and not numeric_present:
            label = raw.get("zone") or raw.get("hole_id") or ""
            if label:
                current_zone = label
            continue
        if not any(raw.values()):
            continue

        record_id = f"{table_id}:r{row_index}"
        record: dict = {"_id": record_id, "_type": table_type, "_row": row_index, "provenance": dict(provenance)}
        field_evidence: dict[str, str] = {}
        for field, (source_index, unit_hint) in colmap.items():
            value = raw.get(field, "")
            evidence_id = f"ev:{table_id}:r{row_index}:{field}"
            if value:
                evidence[evidence_id] = {
                    "element_id": table_id,
                    "page": table.get("page"),
                    "source_kind": "csv_token",
                    "source_ref": table.get("csv"),
                    "row": row_index,
                    "column": source_index,
                    "canonical_field": field,
                    "raw_token": value,
                }
                field_evidence[field] = evidence_id
            if field in numeric:
                normalized = units.normalize(value, unit_hint, field_unit_kind(table_type, field))
                if value:
                    normalized["evidence_id"] = evidence_id
                record[field] = normalized
            else:
                record[field] = value or None

        if table_type == "assay_interval":
            qualifier = (record.get("qualifier") or "").lower()
            record["_is_subinterval"] = is_subinterval or "includ" in qualifier or "incl" in qualifier
            if is_subinterval and not record.get("qualifier"):
                record["qualifier"] = "including"
            if not record.get("zone") and current_zone:
                record["zone"] = current_zone
        if table_type == "resource_estimate":
            record["_is_subtotal"] = _is_aggregate(record.get("category") or "")
        if field_evidence:
            record["_field_evidence"] = field_evidence
        records.append(record)
    return records, evidence


def guard_records(records: list[dict], rows_or_source: list[list[str]] | set[str], ungated: bool = False) -> tuple[list[dict], list[dict]]:
    source = rows_or_source if isinstance(rows_or_source, set) else numguard.number_set(rows_or_source)
    kept: list[dict] = []
    rejected: list[dict] = []
    for record in records:
        bad = numguard.unbacked(_record_numeric_raw_values(record), source)
        if bad and not ungated:
            rejected_record = dict(record)
            rejected_record["_unbacked"] = bad
            rejected.append(rejected_record)
        else:
            kept.append(record)
    return kept, rejected


def _header_unit(header: str) -> str | None:
    match = re.search(r"\(([^)]+)\)", header)
    if match:
        return match.group(1).strip()
    lower = header.lower()
    if "g/t" in lower:
        return "g/t"
    if "ppm" in lower:
        return "ppm"
    if "ppb" in lower:
        return "ppb"
    if "%" in lower:
        return "%"
    if "$" in header:
        return "$"
    if "koz" in lower:
        return "koz"
    if "moz" in lower:
        return "Moz"
    if "tonne" in lower or re.search(r"\bt\b", lower):
        return "t"
    return None


def _norm_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", header.lower()).strip()


def _contains(header: str, *needles: str) -> bool:
    return any(needle in header for needle in needles)


class HeuristicTableMapper:
    """Cheap local mapper used for tests and offline runs when no LLM is configured."""

    def map_table(self, table: dict, rows: list[list[str]]) -> MappedTable:
        if not rows:
            return MappedTable("other")
        headers = [_norm_header(header) for header in rows[0]]
        joined = " | ".join(headers)

        if _contains(joined, "hole") and _contains(joined, "from") and _contains(joined, "to") and _contains(joined, "au", "gold"):
            return self._map_assay(rows[0], headers)
        if _contains(joined, "hole") and _contains(joined, "easting") and _contains(joined, "northing"):
            return self._map_collar(rows[0], headers)
        if _contains(joined, "measured", "indicated", "inferred", "resource") and _contains(joined, "grade", "au"):
            return self._map_resource(rows[0], headers)
        if _contains(joined, "crm", "standard", "certified") and _contains(joined, "failure", "mean", "passing"):
            return self._map_qaqc(rows[0], headers)
        if _contains(joined, "claim") and _contains(joined, "area", "expiry", "expiration", "holder"):
            return self._map_tenure(rows[0], headers)
        if _contains(joined, "year") and _contains(joined, "holes", "metres", "meters", "drilled"):
            return self._map_drill_history(rows[0], headers)
        if _contains(joined, "cost", "budget", "amount") and _contains(joined, "phase", "item", "subtotal"):
            return self._map_budget(rows[0], headers)
        return MappedTable("other")

    def _columns(self, raw_headers: list[str], headers: list[str], table_type: str, rules: list[tuple[str, tuple[str, ...]]]) -> MappedTable:
        columns: list[ColumnMap] = []
        used: set[str] = set()
        for idx, header in enumerate(headers):
            for field, needles in rules:
                if field in used:
                    continue
                if any(needle in header for needle in needles):
                    columns.append(ColumnMap(idx, field, _header_unit(raw_headers[idx])))
                    used.add(field)
                    break
        carry = [field for field in ("hole_id", "zone", "property", "deposit", "claim_id", "target_area") if field in used]
        return MappedTable(table_type, columns, carry)

    def _map_assay(self, raw_headers: list[str], headers: list[str]) -> MappedTable:
        return self._columns(raw_headers, headers, "assay_interval", [
            ("hole_id", ("hole", "drillhole", "drill hole")),
            ("from_m", ("from",)),
            ("to_m", ("to",)),
            ("interval_m", ("interval", "length", "width")),
            ("qualifier", ("qualifier", "incl")),
            ("au_gpt", ("au", "gold")),
            ("ag_gpt", ("ag", "silver")),
            ("cu_pct", ("cu", "copper")),
            ("zone", ("zone", "target", "area")),
        ])

    def _map_collar(self, raw_headers: list[str], headers: list[str]) -> MappedTable:
        return self._columns(raw_headers, headers, "drillhole_collar", [
            ("hole_id", ("hole", "drillhole", "drill hole")),
            ("easting", ("easting", "east")),
            ("northing", ("northing", "north")),
            ("elevation_m", ("elevation", "elev")),
            ("azimuth_deg", ("azimuth", "az")),
            ("dip_deg", ("dip",)),
            ("eoh_m", ("eoh", "depth", "length")),
        ])

    def _map_resource(self, raw_headers: list[str], headers: list[str]) -> MappedTable:
        return self._columns(raw_headers, headers, "resource_estimate", [
            ("property", ("property",)),
            ("deposit", ("deposit", "zone")),
            ("mining_method", ("method", "open pit", "underground")),
            ("category", ("category", "classification", "class")),
            ("tonnage", ("tonnage", "tonnes", "tonne", "kt", "mt")),
            ("grade_gpt", ("grade", "au")),
            ("contained", ("contained", "ounces", "oz", "metal")),
            ("cutoff", ("cutoff", "cut off", "cut")),
        ])

    def _map_qaqc(self, raw_headers: list[str], headers: list[str]) -> MappedTable:
        return self._columns(raw_headers, headers, "qaqc_crm", [
            ("crm_id", ("crm", "standard", "id")),
            ("n_submitted", ("submitted", "count", "number")),
            ("certified_value", ("certified", "expected")),
            ("std_dev", ("std", "standard deviation")),
            ("n_failures", ("failure", "fail")),
            ("mean_measured", ("mean", "measured")),
            ("pct_passing", ("passing", "pass")),
        ])

    def _map_tenure(self, raw_headers: list[str], headers: list[str]) -> MappedTable:
        return self._columns(raw_headers, headers, "tenure", [
            ("claim_id", ("claim", "title", "tenure")),
            ("legacy_claims", ("legacy", "old")),
            ("property", ("property",)),
            ("latitude", ("latitude", " lat", "north latitude")),
            ("longitude", ("longitude", " lon", "west longitude")),
            ("area", ("area", "ha", "hectare", "acre")),
            ("holder", ("holder", "owner")),
            ("status", ("status",)),
            ("issue_date", ("issue", "grant")),
            ("expiry_date", ("expiry", "expiration", "expires")),
        ])

    def _map_drill_history(self, raw_headers: list[str], headers: list[str]) -> MappedTable:
        return self._columns(raw_headers, headers, "drill_history", [
            ("target_area", ("target", "area", "zone")),
            ("year", ("year",)),
            ("company", ("company", "operator")),
            ("program", ("program", "campaign")),
            ("n_holes", ("holes", "hole")),
            ("metres_drilled", ("metres", "meters", "drilled")),
        ])

    def _map_budget(self, raw_headers: list[str], headers: list[str]) -> MappedTable:
        return self._columns(raw_headers, headers, "budget", [
            ("phase", ("phase",)),
            ("item", ("item", "description")),
            ("amount", ("cost", "amount", "subtotal", "total")),
            ("currency", ("currency",)),
        ])


def normalize_table(table: dict, rows: list[list[str]], mapper: TableMapper | None = None) -> dict:
    mapper = mapper or HeuristicTableMapper()
    source_numbers = numguard.number_set(rows)
    if len(rows) < 2 or len(source_numbers) < 1:
        mapping = MappedTable("other")
        return {
            "table_id": table["id"],
            "page": table.get("page"),
            "table_type": "other",
            "mapping": mapping.to_dict(),
            "records": [],
            "rejected": [],
            "n_records": 0,
            "evidence": {},
            "guard": {"status": "skipped", "n_rejected": 0, "source_numbers": len(source_numbers)},
        }

    mapping = mapper.map_table(table, rows)
    records, evidence = build_records(rows, mapping, table)
    ungated = table.get("confidence") == "unverifiable"
    kept, rejected = guard_records(records, source_numbers, ungated=ungated)
    kept_ids = {record["_id"] for record in kept}
    kept_evidence = {eid: ev for eid, ev in evidence.items() if f"{ev['element_id']}:r{ev['row']}" in kept_ids}
    return {
        "table_id": table["id"],
        "page": table.get("page"),
        "table_type": mapping.table_type,
        "confidence": table.get("confidence"),
        "method": table.get("method"),
        "mapping": mapping.to_dict(),
        "records": kept,
        "rejected": rejected,
        "n_records": len(kept),
        "evidence": kept_evidence,
        "guard": {
            "status": "ungated" if ungated else "checked",
            "n_rejected": len(rejected),
            "source_numbers": len(source_numbers),
        },
    }
