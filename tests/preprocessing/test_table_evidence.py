import hashlib
import json
import posixpath
import re
from urllib.parse import unquote

import pytest

from preprocessing.contracts import default_profile
from preprocessing.errors import ServiceError
from preprocessing.indexes import build_indexes
from preprocessing.results import make_manifest, validate_generation


def generation(root):
    extraction = {
        "coverage": {"unit": "page", "enumeration": "complete", "total": 2,
                     "completed": 2, "failed": 0, "unsupported": 0, "notProcessed": 0},
        "stages": [{"id": "table-confirmation", "state": "completed", "required": True}],
        "products": [], "tables": [], "figures": [], "findings": [], "provenance": {},
    }

    def product(path, kind, value, **metadata):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        text = value if isinstance(value, str) else json.dumps(value)
        target.write_text(text, encoding="utf-8")
        extraction["products"].append({"path": path, "kind": kind, **metadata})

    product("tables/t.jsonl", "typed-table", json.dumps({"index": 1, "cells": [{"type": "text", "value": "001"}]}) + "\n")
    product("tables/t.csv", "table-export", "001\n")
    product("evidence/candidates.jsonl", "table-candidates", json.dumps({
        "id": "c1", "page": 1, "bounds": [0, 0, 100, 100], "rows": [["001"]], "method": "native",
    }) + "\n")
    product("evidence/structure.json", "table-structure", {
        "tableId": "t", "selectedCandidateId": "c1", "candidates": ["c1"],
        "selectedRows": [{"index": 1, "candidateId": "c1", "sourceRows": [1]}],
        "wordAssignments": [{"wordId": "w1", "row": 1, "column": 1}],
    })
    product("evidence/decisions.json", "table-recovery-audit", {
        "tableId": "t", "selectedCandidateId": "c1", "imagePath": "evidence/source.png",
        "responsePath": "evidence/response.txt",
    })
    product("evidence/words.json", "page-evidence", {"source": {"page": 1}, "words": [{"id": "w1", "text": "001"}]}, source={"page": 1})
    product("evidence/source.png", "table-image", "image bytes", source={"page": 1}, tableId="t")
    product("evidence/response.txt", "table-model-response", "untrusted raw response", source={"page": 1}, tableId="t")
    product("work.json", "work-plan", {"version": 1, "fingerprint": "semantic-v1", "units": [
        {"id": "page1", "kind": "page-discovery", "state": "completed", "dependencies": [],
         "evidencePaths": ["evidence/candidates.jsonl"], "source": {"page": 1}},
        {"id": "confirm1", "kind": "table-confirmation", "state": "completed", "dependencies": ["page1"],
         "evidencePaths": {"decisions": "evidence/decisions.json"}, "source": {"page": 1}, "tableId": "t"},
    ]})
    extraction["tables"].append({
        "id": "t", "rows": 1, "columns": [{"id": "col_1", "position": 1}],
        "typedPath": "tables/t.jsonl", "csvPath": "tables/t.csv", "source": {"page": 1},
        "selectedCandidateId": "c1", "recovery": {"state": "unchanged", "reason": "native-grid"},
        "confirmation": {"state": "agreed", "basis": "native-vision"},
        "evidencePaths": {"candidates": "evidence/candidates.jsonl", "structure": "evidence/structure.json", "decisions": "evidence/decisions.json"},
    })
    return extraction


def change(root, path, mutate):
    target = root / path
    value = json.loads(target.read_text())
    mutate(value)
    target.write_text(json.dumps(value))


def seal(root, extraction, limits=None):
    profile = default_profile()
    profile["limits"].update(limits or {})
    build_indexes(extraction, root, profile["limits"])
    return make_manifest({"id": "job_test", "profile": profile, "source": {"filename": "source.pdf"}}, extraction, root)


@pytest.mark.parametrize("damage", [
    "selected-candidate", "candidate-page", "wrong-kind", "structure-table", "audit-selection",
    "audit-path", "word-id", "source-row", "selected-row", "dependency", "dependency-cycle",
    "work-evidence", "work-page", "duplicate-candidate", "malformed-json", "confirmation-state",
])
def test_sealing_rejects_dangling_or_inconsistent_table_evidence(tmp_path, damage):
    extraction = generation(tmp_path)
    table = extraction["tables"][0]
    if damage == "selected-candidate":
        table["selectedCandidateId"] = "missing"
    elif damage == "candidate-page":
        change(tmp_path, "evidence/candidates.jsonl", lambda v: v.update(page=2))
    elif damage == "wrong-kind":
        table["evidencePaths"]["structure"] = "evidence/response.txt"
    elif damage == "structure-table":
        change(tmp_path, "evidence/structure.json", lambda v: v.update(tableId="other"))
    elif damage == "audit-selection":
        change(tmp_path, "evidence/decisions.json", lambda v: v.update(selectedCandidateId="missing"))
    elif damage == "audit-path":
        change(tmp_path, "evidence/decisions.json", lambda v: v.update(responsePath="../outside.txt"))
    elif damage == "word-id":
        change(tmp_path, "evidence/structure.json", lambda v: v["wordAssignments"][0].update(wordId="missing"))
    elif damage == "source-row":
        change(tmp_path, "evidence/structure.json", lambda v: v["selectedRows"][0].update(sourceRows=[2]))
    elif damage == "selected-row":
        change(tmp_path, "evidence/structure.json", lambda v: v["selectedRows"][0].update(index=2))
    elif damage == "dependency":
        change(tmp_path, "work.json", lambda v: v["units"][1].update(dependencies=["missing"]))
    elif damage == "dependency-cycle":
        change(tmp_path, "work.json", lambda v: v["units"][0].update(dependencies=["confirm1"]))
    elif damage == "work-evidence":
        change(tmp_path, "work.json", lambda v: v["units"][0].update(evidencePaths=["missing.json"]))
    elif damage == "work-page":
        change(tmp_path, "work.json", lambda v: v["units"][0].update(source={"page": 3}))
    elif damage == "duplicate-candidate":
        path = tmp_path / "evidence/candidates.jsonl"
        path.write_text(path.read_text() * 2)
    elif damage == "malformed-json":
        (tmp_path / "evidence/decisions.json").write_text("{broken")
    else:
        table["confirmation"]["state"] = "verified"
    with pytest.raises(ServiceError) as error:
        seal(tmp_path, extraction)
    assert error.value.code == "OUTPUT_INVALID"
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize("budget", [768, 1200])
def test_table_navigation_exposes_selection_status_and_all_evidence_with_bounded_pages(tmp_path, budget):
    extraction = generation(tmp_path)
    # Long, numerous artifact links must paginate, not overflow or silently disappear.
    for index in range(18):
        path = f"evidence/confirmation_{index:02d}_" + "x" * 90 + ".txt"
        (tmp_path / path).write_text("raw model text")
        extraction["products"].append({"path": path, "kind": "table-model-response", "tableId": "t", "source": {"page": 1}})
    manifest = seal(tmp_path, extraction, {"indexBytes": budget})
    preview = (tmp_path / "indexes/table_000001.md").read_text()
    assert "unchanged" in preview and "agreed" in preview and "native-vision" in preview
    guide = (tmp_path / "AGENTS.md").read_text()
    assert "selected rows" in guide and "original rows" in guide
    listed = {p["path"] for p in manifest["products"]} | {"manifest.json"}
    reached, pending = set(), ["index.md"]
    while pending:
        path = pending.pop()
        if path in reached:
            continue
        reached.add(path)
        if path.endswith(".md"):
            raw = (tmp_path / path).read_bytes()
            assert len(raw) <= budget
            for target in re.findall(r"\]\(([^)]+)\)", raw.decode()):
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(target)))
                assert resolved in listed
                pending.append(resolved)
    assert listed <= reached


def test_generation_validation_rechecks_references_even_with_matching_product_hashes(tmp_path):
    manifest = seal(tmp_path, generation(tmp_path))
    change(tmp_path, "evidence/decisions.json", lambda v: v.update(imagePath="missing.png"))
    item = next(p for p in manifest["products"] if p["path"] == "evidence/decisions.json")
    raw = (tmp_path / item["path"]).read_bytes()
    item.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ServiceError, match="unlisted"):
        validate_generation(tmp_path, manifest)


def test_same_candidate_id_on_distinct_pages_is_valid_and_optional_details_are_not_required(tmp_path):
    extraction = generation(tmp_path)
    path = tmp_path / "evidence/candidates.jsonl"
    candidate = json.loads(path.read_text())
    candidate.update(page=2, bounds=None, cells=[{"opaqueGeometry": True}])
    path.write_text(path.read_text() + json.dumps(candidate) + "\n")
    change(tmp_path, "evidence/structure.json", lambda v: [v.pop(key) for key in ("selectedRows", "wordAssignments", "candidates")])
    assert seal(tmp_path, extraction)["outcome"] == "succeeded"


@pytest.mark.parametrize("path,update", [
    ("evidence/structure.json", {"candidates": None}),
    ("evidence/structure.json", {"wordAssignments": [{"source": []}]}),
    ("evidence/decisions.json", {"source": []}),
    ("work.json", {"units": [None]}),
])
def test_malformed_reference_containers_are_output_errors(tmp_path, path, update):
    extraction = generation(tmp_path)
    build_indexes(extraction, tmp_path, default_profile()["limits"])
    change(tmp_path, path, lambda value: value.update(update))
    with pytest.raises(ServiceError) as error:
        make_manifest({"id": "job_test", "profile": default_profile(), "source": {}}, extraction, tmp_path)
    assert error.value.code == "OUTPUT_INVALID"


def test_dangling_typed_row_source_reference_is_not_sealed(tmp_path):
    extraction = generation(tmp_path)
    change(tmp_path, "tables/t.jsonl", lambda row: row["cells"][0].update(source={"wordIds": ["missing"]}))
    with pytest.raises(ServiceError) as error:
        seal(tmp_path, extraction)
    assert error.value.code == "OUTPUT_INVALID"


def test_two_canonical_tables_cannot_select_the_same_page_occurrence(tmp_path):
    extraction = generation(tmp_path)
    other = json.loads(json.dumps(extraction["tables"][0]))
    other.update(id="duplicate")
    for key, kind in (("structure", "table-structure"), ("decisions", "table-recovery-audit")):
        path = f"evidence/duplicate.{key}.json"
        value = {"tableId": "duplicate", "selectedCandidateId": "c1"}
        (tmp_path / path).write_text(json.dumps(value))
        extraction["products"].append({"path": path, "kind": kind})
        other["evidencePaths"][key] = path
    extraction["tables"].append(other)
    with pytest.raises(ServiceError) as error:
        seal(tmp_path, extraction)
    assert error.value.code == "OUTPUT_INVALID"


def test_canonical_export_cannot_omit_a_selected_candidate_row(tmp_path):
    extraction = generation(tmp_path)
    change(tmp_path, "evidence/candidates.jsonl", lambda candidate: candidate["rows"].append(["002"]))
    with pytest.raises(ServiceError) as error:
        seal(tmp_path, extraction)
    assert error.value.code == "OUTPUT_INVALID"


@pytest.mark.parametrize("dependency_state", ["pending", "failed", "unresolved"])
def test_completed_confirmation_cannot_seal_with_an_incomplete_dependency(tmp_path, dependency_state):
    extraction = generation(tmp_path)
    change(tmp_path, "work.json", lambda plan: plan["units"][0].update(state=dependency_state))
    with pytest.raises(ServiceError) as error:
        seal(tmp_path, extraction)
    assert error.value.code == "OUTPUT_INVALID"
    assert not (tmp_path / "manifest.json").exists()


def test_unreadable_table_source_image_and_raw_response_can_seal_a_partial_result(tmp_path):
    extraction = generation(tmp_path)
    extraction["tables"] = []
    extraction["stages"][0]["state"] = "partial"
    retained = {"evidence/candidates.jsonl", "evidence/source.png", "evidence/response.txt"}
    extraction["products"] = [p for p in extraction["products"] if p["path"] in retained]
    for product in extraction["products"]:
        product.pop("tableId", None)
    change(tmp_path, "evidence/candidates.jsonl", lambda candidate: candidate.update(rows=[], method="scout"))
    manifest = seal(tmp_path, extraction)
    assert manifest["outcome"] == "partial"
    assert manifest["tables"] == []
    assert any(p["kind"] == "table-image" for p in manifest["products"])
