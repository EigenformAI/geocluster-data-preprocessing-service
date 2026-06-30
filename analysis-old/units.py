"""Deterministic unit normalization.

Canonical units: grade g/t, length m, mass tonnes, contained-metal koz (Au),
area ha. The LLM never converts anything — it only passes a raw unit hint from
the column header; all arithmetic happens here.
"""

from __future__ import annotations

import re
from typing import Optional

_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_NULLS = {"", "-", "—", "–", "n/a", "na", "nil", "none"}


def parse_number(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = raw.strip().lower()
    if s in _NULLS:
        return None
    m = _NUM.search(raw)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _grade_to_gpt(v: float, u: str) -> float:
    u = u.lower()
    if "oz" in u:  # oz/t (troy) → g/t
        return v * 34.2857
    if "ppb" in u:  # 1 g/t = 1000 ppb
        return v / 1000.0
    return v  # ppm and g/t are 1:1 for Au


def _mass_to_tonnes(v: float, u: str) -> float:
    u = u.lower()
    if "mt" in u:  # megatonnes
        return v * 1e6
    if "kt" in u:  # kilotonnes
        return v * 1e3
    return v  # t / tonnes


def _metal_to_koz(v: float, u: str) -> float:
    u = u.lower()
    if "moz" in u:
        return v * 1e3
    if "koz" in u:
        return v
    if "kg" in u:
        return v * 32.1507 / 1e3
    if re.search(r"\bt\b|tonne", u):  # tonnes of metal
        return v * 32.1507
    if "oz" in u:  # plain ounces
        return v / 1e3
    return v


def _length_to_m(v: float, u: str) -> float:
    u = u.lower()
    if "ft" in u or "feet" in u or "foot" in u:
        return v * 0.3048
    return v


def _area_to_ha(v: float, u: str) -> float:
    u = u.lower()
    if "acre" in u:
        return v * 0.404686
    return v


_CONV = {
    "grade": (_grade_to_gpt, "g/t"),
    "mass": (_mass_to_tonnes, "tonnes"),
    "metal": (_metal_to_koz, "koz"),
    "length": (_length_to_m, "m"),
    "area": (_area_to_ha, "ha"),
}


def normalize(raw_value: Optional[str], unit_hint: Optional[str], unit_kind: str) -> dict:
    """Return {value, unit, raw_value, raw_unit}. value is the converted float (or None)."""
    raw_num = parse_number(raw_value)
    out = {
        "value": raw_num,
        "unit": None,
        "raw_value": raw_value,
        "raw_unit": unit_hint,
    }
    if raw_num is None:
        return out
    conv = _CONV.get(unit_kind)
    if conv:
        fn, canon = conv
        out["value"] = round(fn(raw_num, unit_hint or ""), 6)
        out["unit"] = canon
    else:  # pct / count / coord / unitless
        out["unit"] = unit_hint
    return out
