"""Canonical records and lightweight structured-output models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Field:
    name: str
    numeric: bool = False
    unit_kind: str = ""


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
        Field("qualifier"),
        Field("au_gpt", True, "grade"),
        Field("ag_gpt", True, "grade"),
        Field("cu_pct", True, "pct"),
    ],
    "resource_estimate": [
        Field("property"),
        Field("deposit"),
        Field("mining_method"),
        Field("category"),
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
        Field("latitude", True, "coord"),
        Field("longitude", True, "coord"),
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
ENTITY_LEVELS = ["project", "property", "deposit", "zone", "target", "hole"]
RELATIONSHIP_TYPES = ["part_of", "intersects", "supports", "near"]
SOURCE_KINDS = ["csv_token", "text_token", "image_label", "computed_metric", "knowledge_item", "human_review"]
CONFIDENCE_LEVELS = ["high", "medium", "low", "none"]


def field_names(table_type: str) -> list[str]:
    return [item.name for item in FIELDS.get(table_type, [])]


def numeric_fields(table_type: str) -> set[str]:
    return {item.name for item in FIELDS.get(table_type, []) if item.numeric}


def field_unit_kind(table_type: str, name: str) -> str:
    for item in FIELDS.get(table_type, []):
        if item.name == name:
            return item.unit_kind
    return ""


@dataclass(frozen=True)
class ColumnMap:
    source_index: int
    canonical_field: str
    unit: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MappedTable:
    table_type: str
    columns: list[ColumnMap] = field(default_factory=list)
    carry_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "table_type": self.table_type,
            "columns": [column.to_dict() for column in self.columns],
            "carry_fields": list(self.carry_fields),
        }


@dataclass(frozen=True)
class FigureClassification:
    figure_class: str
    confidence: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)
