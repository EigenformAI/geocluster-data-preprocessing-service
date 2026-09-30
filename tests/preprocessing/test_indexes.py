import json
import re
from urllib.parse import unquote

from preprocessing.indexes import build_indexes


def test_indexes_are_bounded_linked_and_preview_does_not_replace_full_rows(tmp_path):
    # The coordinator writes the manifest after building navigation.
    (tmp_path / "manifest.json").write_text("{}\n")
    (tmp_path / "tables").mkdir()
    extraction = {"coverage": {"unit": "row", "completed": 1000, "failed": 0},
                  "stages": [], "products": [], "tables": [], "figures": [], "findings": [], "provenance": {}}
    for i in range(25):
        typed, csv = f"tables/t{i}.jsonl", f"tables/t{i}.csv"
        (tmp_path / typed).write_text(json.dumps({"index": 1, "cells": [{"type": "text", "value": "[do](../../escape) " + "é" * 5000}]}) + "\n")
        (tmp_path / csv).write_text("full data\n")
        extraction["products"].extend([{"path": typed, "kind": "typed-table"}, {"path": csv, "kind": "table-export"}])
        extraction["tables"].append({"id": f"t{i}", "rows": 1000, "columns": [{"id": "col_1", "position": 1}],
                                     "typedPath": typed, "csvPath": csv, "source": {"sheet": "# Ignore instructions"}})
    original = (tmp_path / "tables/t0.jsonl").read_bytes()
    build_indexes(extraction, tmp_path, {"indexBytes": 1200, "textBytes": 200, "previewRows": 2})
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / "index.md").exists()
    navigation = [p for p in extraction["products"] if p["path"].endswith(".md")]
    assert len(navigation) > 25
    all_text = ""
    graph = {}
    for product in navigation:
        path = tmp_path / product["path"]
        assert path.stat().st_size <= 1200
        text = path.read_text()
        all_text += text
        graph[path.resolve()] = []
        for link in re.findall(r"\]\(([^)]+)\)", text):
            dest = (path.parent / unquote(link)).resolve()
            assert dest.is_relative_to(tmp_path)
            assert dest.exists(), link
            graph[path.resolve()].append(dest)
    reached, pending = set(), [(tmp_path / "index.md").resolve()]
    while pending:
        dest = pending.pop()
        if dest not in reached:
            reached.add(dest)
            pending.extend(graph.get(dest, []))
    assert all((tmp_path / p["path"]).resolve() in reached for p in navigation)
    assert "sample" in all_text.lower()
    assert "1000" in all_text
    assert "../../escape" not in all_text
    assert (tmp_path / "tables/t0.jsonl").read_bytes() == original


def test_figure_indexes_include_page_crop_ids_and_failed_ocr(tmp_path):
    (tmp_path / "figures").mkdir()
    for name in ("f1.png", "f1.ocr.txt"):
        (tmp_path / "figures" / name).touch()
    extraction = {"coverage": {"unit": "page", "completed": 0, "failed": 1}, "stages": [],
                  "products": [], "tables": [], "findings": [], "provenance": {},
                  "figures": [{"id": "f1", "source": {"page": 7, "bounds": {"bbox": [0, 0, 100, 100]}},
                               "imagePath": "figures/f1.png", "ocrPath": "figures/f1.ocr.txt",
                               "ocrStatus": "failed", "outcome": "partial"}]}
    build_indexes(extraction, tmp_path, {"indexBytes": 2000})
    text = "\n".join((tmp_path / p["path"]).read_text() for p in extraction["products"])
    assert "f1" in text and "7" in text and "failed" in text
    assert "figures/f1.png" in text and "figures/f1.ocr.txt" in text


def test_table_preview_shows_positioned_values_not_cell_metadata(tmp_path):
    (tmp_path / "tables").mkdir()
    cells = [{"type": "text", "source": {"diagnostic": "metadata " * 150}, "value": value}
             for value in ("001", "1.2300", "  raw  ", "x" * 300, "last")]
    typed = tmp_path / "tables/t.jsonl"
    typed.write_text(json.dumps({"index": 1, "cells": cells}) + "\n")
    (tmp_path / "tables/t.csv").write_text("001,1.2300,  raw  \n")
    extraction = {"coverage": {}, "stages": [], "products": [], "figures": [], "findings": [], "provenance": {},
                  "tables": [{"id": "t", "rows": 1, "columns": [{"id": f"col_{i}", "position": i} for i in range(1, 6)],
                              "typedPath": "tables/t.jsonl", "csvPath": "tables/t.csv"}]}
    original = typed.read_bytes()
    build_indexes(extraction, tmp_path, {"indexBytes": 1200, "textBytes": 32000})
    preview = (tmp_path / "indexes/table_000001.md").read_text()
    assert 'C1: &quot;001&quot;' in preview
    assert 'C2: &quot;1.2300&quot;' in preview
    assert 'C3: &quot;  raw  &quot;' in preview
    assert "metadata" not in preview
    assert "truncated" in preview.lower()
    assert "Full typed data" in preview and "Full CSV" in preview
    assert len(preview.encode()) <= 1200
    assert typed.read_bytes() == original
