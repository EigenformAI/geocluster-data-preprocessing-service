"""Numeric anti-fabrication helpers."""

from __future__ import annotations

from pipeline.verify_tables import _numbers


def number_set(rows: list[list[str]]) -> set[str]:
    values: set[str] = set()
    for row in rows:
        for cell in row:
            values.update(_numbers(cell))
    return values


def numbers_in(values) -> list[str]:
    out: list[str] = []
    for value in values:
        if value is not None:
            out.extend(_numbers(str(value)))
    return out


def unbacked(values, source: set[str]) -> list[str]:
    return [number for number in numbers_in(values) if number not in source]
