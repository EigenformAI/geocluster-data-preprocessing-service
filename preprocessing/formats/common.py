"""Streaming product writers shared by format adapters."""

import csv
import json
from pathlib import Path

from preprocessing.contracts import DEFAULT_LIMITS, Extraction
from preprocessing.errors import ServiceError


def limits_for(profile: dict) -> dict:
    return DEFAULT_LIMITS | profile.get("limits", {})


def empty_extraction(adapter: str, version: str) -> Extraction:
    return {"coverage": {}, "stages": [], "products": [], "tables": [], "figures": [],
            "findings": [], "provenance": {"adapter": adapter, "adapterVersion": version, "schemaVersion": 1}}


def coverage(unit: str, total: int, completed: int, failed: int = 0, **extra) -> dict:
    return {"unit": unit, "scope": f"{unit}-extraction", "enumeration": "complete", "total": total,
            "completed": completed, "failed": failed, "unsupported": 0,
            "notProcessed": total - completed - failed, **extra}


def stage_outcome(stage_id: str, state: str, stage_coverage: dict | None = None, *, reason: str | None = None) -> dict:
    result = {"id": stage_id, "required": True, "state": state, "coverage": stage_coverage}
    if state == "not_applicable":
        if not reason:
            raise ValueError("A not_applicable stage requires a reason.")
        result["reason"] = reason
    return result


def native_table_stages(unit_coverage: dict) -> list[dict]:
    incomplete = any(unit_coverage[key] for key in ("failed", "unsupported", "notProcessed"))
    state = "partial" if incomplete and unit_coverage["completed"] else "failed" if incomplete else "completed"
    return [stage_outcome("native-tables", state, dict(unit_coverage)),
            stage_outcome("model-extraction", "not_applicable", reason=(
                "Native table values are extracted directly; no model interpretation task is defined for native tables."))]


def finding(result: Extraction, code: str, message: str, **source) -> None:
    # Status stays small even when a whole document fails. Detailed per-unit
    # outcomes are retained in the page inventory and figure records.
    if len(result["findings"]) < 100:
        result["findings"].append({"code": code, "severity": "warning", "message": message[:500], **source})
    else:
        result["provenance"]["omittedFindings"] = result["provenance"].get("omittedFindings", 0) + 1


def product(result: Extraction, path: str, kind: str, media_type: str, **extra) -> None:
    result["products"].append({"path": path, "kind": kind, "mediaType": media_type, "schemaVersion": 1, **extra})


def write_text(result: Extraction, output: Path, path: str, text: str, kind: str, **extra) -> None:
    target = output / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    product(result, path, kind, "text/plain; charset=utf-8", **extra)


def write_json(result: Extraction, output: Path, path: str, value: dict, kind: str, **extra) -> None:
    target = output / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    product(result, path, kind, "application/json", **extra)


def export_value(cell: dict):
    if cell["type"] == "formula":
        return "=" + cell["formula"]
    if cell["type"] == "date":
        return cell.get("serial", cell.get("value", ""))
    if cell["type"] == "boolean":
        return "TRUE" if cell["value"] else "FALSE"
    return cell.get("value", "")


class TableWriter:
    """Write each source row once; never infer or remove a header row."""

    def __init__(self, result: Extraction, output: Path, table_id: str, source: dict, limits: dict):
        self.result, self.output, self.limits = result, output, limits
        self.table = {"id": table_id, "rows": 0, "columns": [], "source": source,
                      "typedPath": f"tables/{table_id}.jsonl", "csvPath": f"tables/{table_id}.csv",
                      "header": "not-inferred", "csvFormulaPolicy": "raw-values-not-sanitized"}
        self.widths: set[int] = set()

    def __enter__(self):
        (self.output / "tables").mkdir(parents=True, exist_ok=True)
        self.typed = (self.output / self.table["typedPath"]).open("w", encoding="utf-8")
        self.csv = (self.output / self.table["csvPath"]).open("w", encoding="utf-8", newline="")
        self.writer = csv.writer(self.csv)
        return self

    def add(self, cells: list[dict], source: dict) -> None:
        if self.table["rows"] >= self.limits["rows"] or len(cells) > self.limits["columns"]:
            raise ServiceError("RESOURCE_LIMIT", "Table exceeds the configured row or column limit.")
        self.table["rows"] += 1
        self.widths.add(len(cells))
        self.typed.write(json.dumps({"index": self.table["rows"], "cells": cells, "source": source}, ensure_ascii=False) + "\n")
        self.writer.writerow([export_value(cell) for cell in cells])

    def __exit__(self, exc_type, exc, tb):
        self.typed.close()
        self.csv.close()
        if exc_type:
            return False
        self.table["columns"] = [{"id": f"col_{i}", "position": i, "label": None}
                                 for i in range(1, max(self.widths, default=0) + 1)]
        self.result["tables"].append(self.table)
        metadata = {key: self.table[key] for key in ("source", "evidenceBasis", "model") if key in self.table}
        product(self.result, self.table["typedPath"], "typed-table", "application/x-ndjson", **metadata)
        product(self.result, self.table["csvPath"], "table-export", "text/csv; charset=utf-8", **metadata)
        return False
