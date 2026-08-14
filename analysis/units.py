"""Deterministic unit normalization."""

from __future__ import annotations

import re

_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_NULLS = {"", "-", "—", "–", "n/a", "na", "nil", "none"}


def parse_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in _NULLS:
        return None
    match = _NUM.search(raw)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _grade_to_gpt(value: float, unit: str) -> float:
    unit = unit.lower()
    if "oz" in unit:
        return value * 34.2857
    if "ppb" in unit:
        return value / 1000.0
    return value


def _mass_to_tonnes(value: float, unit: str) -> float:
    unit = unit.lower()
    if "mt" in unit or "million" in unit:
        return value * 1e6
    if "kt" in unit or "000 t" in unit or "000t" in unit:
        return value * 1e3
    return value


def _metal_to_koz(value: float, unit: str) -> float:
    unit = unit.lower()
    if "moz" in unit or "million oz" in unit:
        return value * 1e3
    if "koz" in unit or "000 oz" in unit or "000oz" in unit:
        return value
    if "kg" in unit:
        return value * 32.1507 / 1e3
    if re.search(r"\bt\b|tonne", unit):
        return value * 32.1507
    if "oz" in unit:
        return value / 1e3
    return value


def _length_to_m(value: float, unit: str) -> float:
    unit = unit.lower()
    if "ft" in unit or "feet" in unit or "foot" in unit:
        return value * 0.3048
    return value


def _area_to_ha(value: float, unit: str) -> float:
    unit = unit.lower()
    if "acre" in unit:
        return value * 0.404686
    return value


_CONVERSIONS = {
    "grade": (_grade_to_gpt, "g/t"),
    "mass": (_mass_to_tonnes, "tonnes"),
    "metal": (_metal_to_koz, "koz"),
    "length": (_length_to_m, "m"),
    "area": (_area_to_ha, "ha"),
}


def normalize(raw_value: str | None, unit_hint: str | None, unit_kind: str) -> dict:
    raw_number = parse_number(raw_value)
    out = {
        "value": raw_number,
        "unit": None,
        "raw_value": raw_value,
        "raw_unit": unit_hint,
    }
    if raw_number is None:
        return out
    conversion = _CONVERSIONS.get(unit_kind)
    if conversion:
        fn, canonical_unit = conversion
        out["value"] = round(fn(raw_number, unit_hint or ""), 6)
        out["unit"] = canonical_unit
    else:
        out["unit"] = unit_hint
    return out
