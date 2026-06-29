"""Anti-fabrication guard: every number we keep must exist in the source CSV.

Reuses the extraction pipeline's numeric tokenizer so 'the same number' means the
same thing here as in the table verifier. With the column-mapping design values
are copied straight from the CSV, so this is an invariant check that also catches
mapping bugs (and is reused by Stage F to vet memo citations).
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.verify_tables import _numbers  # noqa: E402


def number_set(rows: list[list[str]]) -> set[str]:
    s: set[str] = set()
    for row in rows:
        for cell in row:
            s.update(_numbers(cell))
    return s


def numbers_in(values) -> list[str]:
    """All numeric tokens across an iterable of raw strings/values."""
    out: list[str] = []
    for v in values:
        if v is not None:
            out.extend(_numbers(str(v)))
    return out


def unbacked(values, source: set[str]) -> list[str]:
    """Numeric tokens in `values` that are NOT present in the source set."""
    return [n for n in numbers_in(values) if n not in source]
