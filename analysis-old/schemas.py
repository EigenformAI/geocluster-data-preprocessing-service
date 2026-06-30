"""Canonical geological record vocabulary + the LLM column-mapping I/O model.

The LLM's only job in Stage A is to (a) classify a table's type and (b) map each
source column to a canonical field. It never emits cell values — Python reads the
values straight from the CSV — so it has zero numeric authority by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel


@dataclass(frozen=True)
class Field:
    name: str
    numeric: bool = False
    unit_kind: str = ""  # grade | length | mass | metal | area | pct | count | coord | ""


# Canonical fields per table type. Unit_kind drives deterministic conversion in units.py.
FIELDS: dict[str, list[Field]] = {
    "drillhole_collar": [
        Field("hole_id"),
        Field("easting", True, "coord"),
        Field("northing", True, "coord"),
        Field("elevation_m", True, "length"),
        Field("azimuth_deg", True),
        Field("dip_deg", True),
        Field("eoh_m", True, "length"),
    ],
    "assay_interval": [
        Field("hole_id"),
        Field("zone"),
        Field("from_m", True, "length"),
        Field("to_m", True, "length"),
        Field("interval_m", True, "length"),
        Field("qualifier"),  # "including" / "incl" marks a sub-interval
        Field("au_gpt", True, "grade"),
        Field("ag_gpt", True, "grade"),
        Field("cu_pct", True, "pct"),
    ],
    "resource_estimate": [
        Field("property"),
        Field("deposit"),
        Field("mining_method"),
        Field("category"),  # Measured | Indicated | M&I | Inferred | Total
        Field("tonnage", True, "mass"),
        Field("grade_gpt", True, "grade"),
        Field("contained", True, "metal"),
        Field("cutoff", True, "grade"),
    ],
    "qaqc_crm": [
        Field("crm_id"),
        Field("n_submitted", True, "count"),
        Field("certified_value", True, "grade"),
        Field("std_dev", True, "grade"),
        Field("n_failures", True, "count"),
        Field("mean_measured", True, "grade"),
        Field("pct_passing", True, "pct"),
    ],
    "tenure": [
        Field("claim_id"),
        Field("legacy_claims"),
        Field("property"),
        Field("area", True, "area"),
        Field("holder"),
        Field("status"),
        Field("issue_date"),
        Field("expiry_date"),
    ],
    "drill_history": [
        Field("target_area"),
        Field("year"),
        Field("company"),
        Field("program"),
        Field("n_holes", True, "count"),
        Field("metres_drilled", True, "length"),
    ],
    "budget": [
        Field("phase"),
        Field("item"),
        Field("amount", True),
        Field("currency"),
    ],
}

TABLE_TYPES = list(FIELDS) + ["other"]


def field_names(table_type: str) -> list[str]:
    return [f.name for f in FIELDS.get(table_type, [])]


def numeric_fields(table_type: str) -> set[str]:
    return {f.name for f in FIELDS.get(table_type, []) if f.numeric}


def field_unit_kind(table_type: str, name: str) -> str:
    for f in FIELDS.get(table_type, []):
        if f.name == name:
            return f.unit_kind
    return ""


# ---- LLM structured-output model (chat.parse response_format) ----------------

class ColumnMap(BaseModel):
    source_index: int  # 0-based column index in the CSV
    canonical_field: str  # one of FIELDS[table_type] names, or "ignore"
    unit: Optional[str] = None  # raw unit from the header if any (e.g. "g/t", "kt", "ppm", "m", "ha", "%")


class MappedTable(BaseModel):
    table_type: str  # one of TABLE_TYPES
    columns: list[ColumnMap] = []
    carry_fields: list[str] = []  # identifier fields that forward-fill when blank
