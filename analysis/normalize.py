"""Stage A NORMALIZE: typed geological records from verified tables.

The LLM classifies the table type and maps each source column to a canonical
field (it never sees the job as 'read the numbers'). Python then pulls values
straight from the CSV, forward-fills identifier columns, classifies rows
(zone-header / subtotal / sub-interval), normalizes units, and number-guards the
result. So values are authentic by construction; the guard is an invariant check.
"""

from __future__ import annotations

import re

from . import astate, numguard, units
from .apaths import APaths
from .llm import parse_cached
from .schemas import FIELDS, MappedTable, field_unit_kind, numeric_fields
from .source import Report

STAGE = "normalize"
MODEL = "mistral-large-2512"
PROMPT_VERSION = "v1"

_VOCAB = "\n".join(
    f"- {t}: {', '.join(f.name for f in fs)}" for t, fs in FIELDS.items()
)

SYSTEM = (
    "You map a CSV table extracted from an NI 43-101 mineral-exploration report into "
    "a typed schema. You DO NOT read or transcribe values — you only (1) choose the "
    "table type and (2) assign each column to a canonical field. Python reads the cells.\n\n"
    "Table types and their canonical fields:\n" + _VOCAB + "\n\n"
    "Rules:\n"
    "- Pick the single best table_type. If the table is not geological data (table of "
    "contents, list of figures/tables, references, narrative, signatures), return "
    'table_type="other" with empty columns.\n'
    "- For each data column return {source_index, canonical_field, unit}. Use a field "
    "from the chosen type only, or \"ignore\". `unit` = the raw unit shown in the header "
    'if any (e.g. "g/t", "kt", "ppm", "m", "ha", "%", "oz/t"), else null.\n'
    "- carry_fields = identifier fields whose value continues downward when a cell is "
    "blank (e.g. hole_id, property, deposit). List only those that actually carry."
)


def _cache_parts(table: dict, rows: list[list[str]]) -> list[bytes]:
    return [b"normalizeA", table["csv"].encode(),
            "\n".join("|".join(r) for r in rows).encode(),
            PROMPT_VERSION.encode(), MODEL.encode()]


def _preview(rows: list[list[str]], n: int = 8) -> str:
    header = rows[0] if rows else []
    lines = ["Columns (index: header):"]
    lines += [f"{i}: {h}" for i, h in enumerate(header)]
    lines.append("Sample rows:")
    for r in rows[1 : 1 + n]:
        lines.append(" | ".join(r))
    return "\n".join(lines)


def _cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if 0 <= idx < len(row) else ""


def _is_aggregate(cat: str) -> bool:
    c = cat.lower()
    return any(k in c for k in ("total", "subtotal", "m&i", "measured & indicated", "measured and indicated"))


# A sub-interval row often carries the word "including" in the hole-id column
# instead of a separate qualifier column.
_QUALIFIER_RE = re.compile(r"(?i)^(incl|inc\.|and incl|includes|sub-?interval)")


def _build_records(rows: list[list[str]], mt: dict, table: dict) -> tuple[list[dict], list[dict]]:
    ttype = mt["table_type"]
    fields = {f.name for f in FIELDS.get(ttype, [])}
    # canonical field -> (source_index, unit); first mapping wins
    colmap: dict[str, tuple[int, str | None]] = {}
    for c in mt.get("columns", []):
        cf = c.get("canonical_field")
        if cf in fields and cf not in colmap:
            colmap[cf] = (c["source_index"], c.get("unit"))
    if not colmap:
        return [], []

    carry = [f for f in mt.get("carry_fields", []) if f in colmap]
    nums = numeric_fields(ttype)
    prov = {
        "table_id": table["id"],
        "page": table["page"],
        "confidence": table.get("confidence"),
        "method": table.get("method"),
    }

    carried: dict[str, str] = {}
    current_zone = ""
    records: list[dict] = []

    for ri, row in enumerate(rows[1:], start=1):
        raw = {f: _cell(row, idx) for f, (idx, _) in colmap.items()}

        is_sub = False
        if ttype == "assay_interval":
            hid = (raw.get("hole_id") or "").strip()
            if hid and _QUALIFIER_RE.match(hid):
                is_sub = True
                raw["hole_id"] = ""  # sub-interval row: let the parent hole id carry down

        # forward-fill identifier columns
        for f in carry:
            if raw.get(f):
                carried[f] = raw[f]
            elif f in carried:
                raw[f] = carried[f]

        num_present = any(raw.get(f) for f in nums)

        # zone / section header rows (assay): no numbers, some label → set context, no record
        if ttype == "assay_interval" and not num_present:
            label = raw.get("zone") or raw.get("hole_id") or ""
            if label:
                current_zone = label
            continue
        if not any(raw.values()):
            continue

        rec: dict = {"_type": ttype, "_row": ri, "provenance": prov}
        for f, (idx, unit) in colmap.items():
            val = raw.get(f, "")
            if f in nums:
                rec[f] = units.normalize(val, unit, field_unit_kind(ttype, f))
            else:
                rec[f] = val or None

        if ttype == "assay_interval":
            q = (rec.get("qualifier") or "").lower()
            rec["_is_subinterval"] = is_sub or ("includ" in q) or ("incl" in q)
            if is_sub and not rec.get("qualifier"):
                rec["qualifier"] = "including"
            if not rec.get("zone") and current_zone:
                rec["zone"] = current_zone
        if ttype == "resource_estimate":
            rec["_is_subtotal"] = _is_aggregate(rec.get("category") or "")

        records.append(rec)
    return records, carry


def _table_numbers(rec: dict) -> list[str]:
    out = []
    for v in rec.values():
        if isinstance(v, dict) and "raw_value" in v and v["raw_value"]:
            out.append(str(v["raw_value"]))
    return out


def run(report: Report, apaths: APaths, force: bool = False) -> dict:
    apaths.ensure_dirs()
    from pipeline.util import mark_stage_done, write_json

    index = []
    typed = skipped = quarantined_tables = 0

    for table in report.tables:
        out = apaths.record_json(table["id"])
        if out.exists() and not force:
            from pipeline.util import read_json

            d = read_json(out)
            index.append({k: d.get(k) for k in ("table_id", "table_type", "page", "confidence", "n_records")})
            continue

        rows = report.read_table(table)
        source = numguard.number_set(rows)
        # cheap pre-filter: tables with almost no numbers aren't data tables
        if len(source) < 3 or len(rows) < 2:
            write_json(out, {"table_id": table["id"], "page": table["page"], "table_type": "other",
                             "confidence": table.get("confidence"), "method": table.get("method"),
                             "records": [], "rejected": [], "n_records": 0, "guard": {"status": "skipped"}})
            skipped += 1
            index.append({"table_id": table["id"], "table_type": "other", "page": table["page"],
                          "confidence": table.get("confidence"), "n_records": 0})
            continue

        parts = _cache_parts(table, rows)
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": _preview(rows)}]
        try:
            mt = parse_cached(apaths, parts, MODEL, messages, MappedTable)
        except Exception as e:  # one bad table shouldn't abort the report
            write_json(out, {"table_id": table["id"], "page": table["page"], "table_type": "error",
                             "error": f"{type(e).__name__}: {e}", "records": [], "rejected": [], "n_records": 0,
                             "guard": {"status": "error"}})
            index.append({"table_id": table["id"], "table_type": "error", "page": table["page"],
                          "confidence": table.get("confidence"), "n_records": 0})
            continue

        records, carry = _build_records(rows, mt, table)

        # number guard: every numeric token in a record must exist in the source CSV
        ungated = table.get("confidence") == "unverifiable"
        kept, rejected = [], []
        for rec in records:
            bad = numguard.unbacked(_table_numbers(rec), source)
            if bad and not ungated:
                rec["_unbacked"] = bad
                rejected.append(rec)
            else:
                kept.append(rec)
        if rejected:
            quarantined_tables += 1

        write_json(out, {
            "table_id": table["id"], "page": table["page"], "table_type": mt["table_type"],
            "confidence": table.get("confidence"), "method": table.get("method"),
            "carry_fields": carry, "records": kept, "rejected": rejected, "n_records": len(kept),
            "guard": {"status": "ungated" if ungated else "checked",
                      "n_rejected": len(rejected), "source_numbers": len(source)},
        })
        typed += 1 if mt["table_type"] != "other" else 0
        index.append({"table_id": table["id"], "table_type": mt["table_type"], "page": table["page"],
                      "confidence": table.get("confidence"), "n_records": len(kept)})

    write_json(apaths.records_index, index)
    info = {"n_tables": len(report.tables), "typed": typed, "skipped": skipped,
            "tables_with_quarantine": quarantined_tables}
    astate.mark_done(apaths, STAGE, info)
    return info
