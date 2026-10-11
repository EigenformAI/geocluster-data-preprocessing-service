"""Behavioral PDF table fixtures, including in-memory ruled PDFs; no model calls."""

import json
import subprocess
import sys
from copy import deepcopy
from io import BytesIO
from itertools import pairwise

import pytest

from preprocessing.errors import ServiceError
from preprocessing.formats.pdf_tables import (
    aggregate_reads,
    aggregate_tiles,
    assess_geometry,
    candidate_from_rows,
    compare_candidates,
    group_candidates,
    plan_tiles,
    propose_regions,
    recover_geometry,
    validate_visual,
    word_occurrences,
)

BOX = [0, 0, 100, 70]


def word(id, text, x, y, width=12, height=8):
    return {"id": id, "text": text, "bbox": [x, y, x + width, y + height]}


def visual(rows, *, edges=None, box=BOX, id="visual"):
    response = {"tablePresent": True, "rowCount": len(rows),
                "columnCount": max(map(len, rows), default=0),
                "cells": [{"row": r, "column": c, "text": value}
                          for r, row in enumerate(rows, 1) for c, value in enumerate(row, 1)]}
    if edges is not None:
        response["rowEdges"] = edges
    result = validate_visual(response, id, 1, box)
    assert result["valid"], result["errors"]
    return result["candidate"]


def test_geometry_pairs_collapsed_numeric_lanes_by_position_not_string_order():
    words = [word("a", "1", 5, 10), word("b", "2", 5, 35),
             word("c", "10", 55, 10), word("d", "20", 55, 35),
             word("loose", "?", 46, 10, 2)]
    raw = candidate_from_rows("raw", 1, BOX, [["1\n2", "20\n10"]], "native",
                              cell_bounds=[[BOX[:2] + [45, 70], [50, 0, 100, 70]]], words=words)
    assessment = assess_geometry(raw, words)
    assert "multiline-lanes" in assessment["triggers"]
    result = recover_geometry(raw, words, id="repaired")
    assert result["state"] == "unresolved"  # The repaired grid retains a gutter word for review.
    fixed = result["candidate"]
    assert fixed["rows"] == [["1", "10"], ["2", "20"]]
    assert fixed["rows"] != [["1", "20"], ["2", "10"]]
    assert [cell["wordIds"] for cell in fixed["cells"]] == [["a"], ["c"], ["b"], ["d"]]
    assert raw["rows"] == [["1\n2", "20\n10"]]
    assert result["unassignedWordIds"] == ["loose"]
    assert fixed["parentId"] == "raw"


@pytest.mark.parametrize("description", ["North\nPit", "12\n(estimated)"])
def test_wrapped_prose_and_numeric_annotation_are_not_new_rows(description):
    first, second = description.split("\n")
    words = [word("a", first, 5, 10, 30), word("b", second, 5, 20, 35), word("c", "9", 60, 10)]
    raw = candidate_from_rows("raw", 1, BOX, [[description, "9"]], "native",
                              cell_bounds=[[[0, 0, 50, 70], [50, 0, 100, 70]]], words=words)
    result = recover_geometry(raw, words)
    assert result["state"] == "unchanged"
    assert result["candidate"]["rows"] == [[description, "9"]]


def test_sparse_physical_row_survives_between_dense_rows():
    words = [word("a", "1", 5, 5), word("b", "10", 55, 5), word("c", "qualifier", 5, 30, 35),
             word("d", "2", 5, 55), word("e", "20", 55, 55)]
    raw = candidate_from_rows("raw", 1, BOX, [["1 qualifier 2", "10 20"]], "native",
                              cell_bounds=[[[0, 0, 50, 70], [50, 0, 100, 70]]], words=words)
    result = recover_geometry(raw, words)
    assert result["candidate"]["rows"] == [["1", "10"], ["qualifier", ""], ["2", "20"]]
    assert result["candidate"]["grid"][1][1]["state"] == "empty"


def test_native_span_anchors_once_and_word_occurrences_are_not_duplicated():
    words = [word("label", "Group", 5, 5, 30), word("a", "1", 55, 5), word("b", "2", 55, 35)]
    raw = candidate_from_rows("raw", 1, BOX, [["Group", "1"], [None, "2"]], "native",
                              cell_bounds=[[[0, 0, 50, 70], [50, 0, 100, 30]],
                                           [None, [50, 30, 100, 70]]], words=words)
    assert raw["cells"][0]["rowSpan"] == 2
    assert raw["grid"][1][0] == {"state": "covered", "anchor": [1, 1]}
    result = recover_geometry(raw, words)
    fixed = result["candidate"]
    assert fixed["rows"] == [["Group", "1"], [None, "2"]]
    assert fixed["cells"][0]["wordIds"] == ["label"]
    assert sum("label" in c["wordIds"] for c in fixed["cells"]) == 1
    response = {"tablePresent": True, "rowCount": 2, "columnCount": 2,
                "cells": [{"row": 1, "column": 1, "text": "Group", "rowSpan": 2},
                          {"row": 1, "column": 2, "text": "1"}, {"row": 2, "column": 2, "text": "2"}]}
    read = validate_visual(response, "v", 1, BOX)["candidate"]
    assert compare_candidates(fixed, read)["agreed"]
    assert not compare_candidates(fixed, visual([["Group", "1"], ["", "2"]]))["agreed"]


def test_base_occurrences_are_stable_across_candidates_and_conflicts_stay_evidence():
    words = word_occurrences(3, [{"text": "same", "x0": 5, "top": 5, "x1": 20, "bottom": 13},
                                 {"text": "same", "x0": 5, "top": 35, "x1": 20, "bottom": 43}])
    assert words[0]["id"] != words[1]["id"]
    raw = candidate_from_rows("a", 3, BOX, [["same"], ["same"]], "native",
                              cell_bounds=[[[0, 0, 100, 30]], [[0, 30, 100, 70]]], words=words)
    other = candidate_from_rows("b", 3, BOX, [["same same"]], "alternate",
                                cell_bounds=[[BOX]], words=words)
    assert raw["cells"][0]["wordIds"] == [words[0]["id"]]
    assert other["cells"][0]["wordIds"] == [w["id"] for w in words]
    conflict = candidate_from_rows("c", 3, BOX, [["same", "same"]], "native",
                                   cell_bounds=[[[0, 0, 30, 70], [0, 0, 40, 70]]], words=words)
    assert conflict["conflictingWordIds"] == [w["id"] for w in words]
    assert all(not c["wordIds"] for c in conflict["cells"])
    with pytest.raises(ValueError, match="unique"):
        candidate_from_rows("bad", 3, BOX, [["x"]], "native", words=[words[0], words[0]])


@pytest.mark.parametrize("wrong", [[["2", "10"], ["1", "20"]], [["1", "20"], ["2", "10"]],
                                   [["1", "10"], ["1", "20"]], [["1", "10"]]])
def test_comparison_rejects_swapped_borrowed_and_missing_values(wrong):
    native = candidate_from_rows("n", 1, BOX, [["1", "10"], ["2", "20"]], "native")
    assert not compare_candidates(native, visual(wrong))["agreed"]


@pytest.mark.parametrize("left,right", [("1 234", "1234"), ("26,3", "26.3"), ("H001", "H1"),
                                         ("-2", "2"), ("2 mg", "2"), ("²\nkg", "² kg"),
                                         ("12\nkg", "12 kg"), ("North Pit", "NorthPit"), ("North", "north")])
def test_comparison_preserves_numeric_lexemes_punctuation_case_and_internal_whitespace(left, right):
    assert not compare_candidates(visual([[left]], id="a"), visual([[right]], id="b"))["agreed"]


def test_comparison_records_allowed_whitespace_without_rewriting_values():
    a, b = visual([[" North\nPit ", " 001 "]], id="a"), visual([["North Pit", "001"]], id="b")
    result = compare_candidates(a, b)
    assert result["agreed"]
    assert result["basis"] == "ordered-grid"
    assert result["normalizations"]
    assert a["rows"] == [[" North\nPit ", " 001 "]]


def test_missing_empty_and_unreadable_cells_are_distinct_and_never_confirmed():
    response = {"tablePresent": True, "rowCount": 1, "columnCount": 3,
                "cells": [{"row": 1, "column": 1, "text": ""},
                          {"row": 1, "column": 2, "text": None, "unreadableReason": "blur"}]}
    read = validate_visual(response, "v", 1, BOX)
    assert read["valid"]
    candidate = read["candidate"]
    assert [p["state"] for p in candidate["grid"][0]] == ["empty", "unreadable", "omitted"]
    assert not compare_candidates(candidate, candidate)["agreed"]
    raw = candidate_from_rows("ragged", 1, BOX, [[""], ["x", None]], "ocr")
    assert raw["rows"] == [[""], ["x", None]]
    assert raw["grid"][0][1]["state"] == "omitted"
    marker = visual([["[uncertain: 12]"]])
    assert not compare_candidates(marker, marker)["agreed"]


@pytest.mark.parametrize("change", [
    {"rowCount": True}, {"rowCount": 1.0}, {"columnCount": float("inf")},
    {"cells": [{"row": 0, "column": 1, "text": "a"}]},
    {"cells": [{"row": 1, "column": 1, "text": 1}]},
    {"cells": [{"row": 1, "column": 1, "text": "a", "rowSpan": 0}]},
    {"cells": [{"row": 1, "column": 1, "text": "a", "columnSpan": 2},
               {"row": 1, "column": 2, "text": "b"}]},
    {"cells": [{"row": 1, "column": 1, "text": "a"}, {"row": 1, "column": 1, "text": "a"}]},
])
def test_malformed_visual_response_is_retained_without_a_fabricated_candidate(change):
    response = {"tablePresent": True, "rowCount": 1, "columnCount": 2,
                "cells": [{"row": 1, "column": 1, "text": "a"}]} | change
    result = validate_visual(response, "v", 1, BOX, model={"requestedModel": "test"})
    assert not result["valid"]
    assert result["candidate"] is None
    assert result["raw"] == response
    assert result["errors"]


def test_auxiliary_edges_can_be_discarded_but_plausible_conflicting_geometry_disagrees():
    response = {"tablePresent": True, "rowCount": 2, "columnCount": 1,
                "cells": [{"row": 1, "column": 1, "text": "a"}, {"row": 2, "column": 1, "text": "b"}],
                "rowEdges": [0, 800, 700], "columnEdges": [0, float("nan")]}
    result = validate_visual(response, "v", 1, BOX)
    assert result["valid"]
    assert len(result["discardedEdges"]) == 2
    assert result["candidate"]["rowBands"] == []
    native = candidate_from_rows("n", 1, BOX, [["a"], ["b"]], "native",
                                 cell_bounds=[[[0, 0, 100, 35]], [[0, 35, 100, 70]]])
    wrong = visual([["a"], ["b"]], edges=[0, 800, 1000])
    comparison = compare_candidates(native, wrong)
    assert not comparison["agreed"]
    assert "row-boundaries" in [d["kind"] for d in comparison["differences"]]


def test_non_table_response_is_a_presence_dispute_not_an_empty_table():
    result = validate_visual({"tablePresent": False, "rowCount": 0, "columnCount": 0, "cells": []}, "v", 1, BOX)
    assert result["valid"]
    assert not compare_candidates(candidate_from_rows("n", 1, BOX, [["x"]], "native"), result["candidate"])["agreed"]


def test_grouping_uses_occurrence_regions_and_layout_without_text_dedup_or_overlap_chaining():
    def candidate(id, box, rows=None, page=1):
        return candidate_from_rows(id, page, box, rows or [["same", "same"]], "native")
    candidates = [candidate("a", [0, 0, 100, 40]), candidate("a2", [1, 1, 101, 41], [["different", "values"]]),
                  candidate("right", [120, 0, 220, 40]), candidate("below", [0, 60, 100, 100]),
                  candidate("other-page", [0, 0, 100, 40], page=2),
                  candidate("merged", [0, 0, 220, 40]), candidate("unlocated", None)]
    groups = group_candidates(candidates)
    assert any(g["candidateIds"] == ["a", "a2"] for g in groups)
    assert len(groups) == 6
    assert any(g["ambiguousWith"] for g in groups if "merged" in g["candidateIds"])
    assert next(g for g in groups if "unlocated" in g["candidateIds"])["issues"] == ["missing-bounds"]


def test_text_alignment_proposals_use_right_alignment_multiple_rows_and_vertical_regions():
    words = []
    for index, (y, value, width) in enumerate([(5, "1", 5), (25, "200", 15), (45, "30", 10),
                                             (110, "1", 5), (130, "200", 15), (150, "30", 10)]):
        words.extend([word(f"l{index}", "label", 5, y, 20), word(f"r{index}", value, 95 - width, y, width)])
    proposals = propose_regions(words, 1)
    assert len(proposals) == 2
    assert all(len(p["rowBands"]) == 3 for p in proposals)
    assert all(len(p["columnIntervals"]) == 2 for p in proposals)
    assert propose_regions(words[:2], 1) == []
    assert propose_regions([word("p1", "prose", 0, 0), word("p2", "prose", 0, 20),
                            word("p3", "prose", 0, 40)], 1) == []
    wide = words[:6] + [word(f"w{i}", "wide", 170, y, 20) for i, y in enumerate([5, 25, 45])]
    assert len(propose_regions(wide, 1)) == 1
    assert len(propose_regions(wide, 1)[0]["columnIntervals"]) == 3


def test_tile_plan_covers_entire_region_rows_and_overlapping_seams():
    bands = [[5, 15], [28, 48], [62, 78], [90, 108], [125, 139]]
    plan = plan_tiles([0, 0, 100, 145], max_height=60, overlap=20, row_bands=bands)
    tiles = plan["tiles"]
    assert tiles[0]["bounds"][1] == 0
    assert tiles[-1]["bounds"][3] == 145
    assert all(t["bounds"][3] - t["bounds"][1] <= 60 for t in tiles)
    assert all(any(t["bounds"][1] <= top and bottom <= t["bounds"][3] for t in tiles) for top, bottom in bands)
    assert all(a["bounds"][3] > b["bounds"][1] for a, b in pairwise(tiles))
    for seam in plan["seams"]:
        a, b = (next(t for t in tiles if t["id"] == id) for id in seam["tileIds"])
        assert max(a["bounds"][1], b["bounds"][1]) < seam["position"] < min(a["bounds"][3], b["bounds"][3])
    with pytest.raises(ValueError, match="row band"):
        plan_tiles(BOX, max_height=20, overlap=5, row_bands=[[10, 40]])


def tile_read(tile, bands, rows):
    read = visual(rows, box=tile["bounds"], id=tile["id"])
    read["rowBands"] = bands
    return {"tileId": tile["id"], "candidate": read}


def test_runtime_filtered_rows_preserve_visual_grid_capacity_error():
    box = [0, 0, 100, 100]
    tile = {"id": "tile", "bounds": box}
    plan = {"bounds": box, "tiles": [tile], "seams": []}
    read = tile_read(tile, [[0, 10], [20, 30], [40, 50]],
                     [["clipped", "value"], ["001", "1.2300"], ["002", "2.0000"]])
    with pytest.raises(ServiceError) as error:
        aggregate_reads(plan, [read], "joined", 1, box, {"max_grid_cells": 3})
    assert error.value.code == "RESOURCE_LIMIT"
    assert error.value.message == "Visual table gridCells count 4 exceeds configured capacity 3."


def test_tile_aggregation_preserves_identical_positioned_rows_and_reconciles_edge_reads():
    plan = plan_tiles([0, 0, 100, 100], max_height=65, overlap=30)
    a, b = plan["tiles"]
    reads = [tile_read(a, [[10, 20], [40, 50], [60, 65]], [["same"], ["same"], ["clipped"]]),
             tile_read(b, [[40, 50], [60, 70], [80, 90]], [["same"], ["complete"], ["same"]])]
    result = aggregate_tiles(plan, reads, "joined", 1, [0, 0, 100, 100])
    assert result["state"] == "agreed", result["issues"]
    assert result["candidate"]["rows"] == [["same"], ["same"], ["complete"], ["same"]]
    assert result["candidate"]["rowBands"] == [[10, 20], [40, 50], [60, 70], [80, 90]]
    assert len(result["rowSources"][1]) == 2
    bad = deepcopy(reads)
    bad[1]["candidate"]["cells"][0]["text"] = "disputed"
    assert aggregate_tiles(plan, bad, "joined", 1, [0, 0, 100, 100])["state"] == "unresolved"


def test_unpositioned_unreadable_missing_or_edge_only_tile_rows_do_not_agree():
    plan = plan_tiles([0, 0, 100, 100], max_height=65, overlap=30)
    a, b = plan["tiles"]
    reads = [tile_read(a, [[10, 20], [40, 50]], [["a"], ["b"]]),
             tile_read(b, [[40, 50], [90, 100]], [["b"], ["edge"]])]
    assert aggregate_tiles(plan, reads, "j", 1, [0, 0, 100, 100])["state"] == "unresolved"
    assert aggregate_tiles(plan, reads[:1], "j", 1, [0, 0, 100, 100])["state"] == "unresolved"
    reads[1]["candidate"]["rowBands"] = []
    assert aggregate_tiles(plan, reads, "j", 1, [0, 0, 100, 100])["state"] == "unresolved"


def test_words_elsewhere_on_page_do_not_become_unassigned_table_evidence():
    words = [word("cell", "1", 5, 5), word("prose", "unrelated", 110, 5, 40)]
    candidate = candidate_from_rows("n", 1, BOX, [["1"]], "native", cell_bounds=[[BOX]], words=words)
    assert candidate["unassignedWordIds"] == []
    assert candidate["outsideWordIds"] == ["prose"]
    assert compare_candidates(candidate, visual([["1"]]))["agreed"]


def test_seam_with_one_matched_row_still_rejects_another_missing_overlap_row():
    plan = plan_tiles([0, 0, 100, 100], max_height=65, overlap=30)
    a, b = plan["tiles"]
    reads = [tile_read(a, [[10, 20], [40, 50], [55, 60]], [["a"], ["b"], ["missing"]]),
             tile_read(b, [[40, 50], [80, 90]], [["b"], ["c"]])]
    result = aggregate_tiles(plan, reads, "j", 1, [0, 0, 100, 100])
    assert result["state"] == "unresolved"
    assert "missing-overlap-row" in [issue["kind"] for issue in result["issues"]]


def test_close_sparse_band_with_no_continuation_spacing_evidence_stays_ambiguous():
    words = [word("a", "1", 5, 5), word("b", "10", 55, 5), word("c", "qualifier", 5, 15, 35),
             word("d", "2", 5, 25), word("e", "20", 55, 25)]
    raw = candidate_from_rows("raw", 1, BOX, [["1\nqualifier\n2", "10\n20"]], "native",
                              cell_bounds=[[[0, 0, 50, 70], [50, 0, 100, 70]]], words=words)
    result = recover_geometry(raw, words)
    assert result["state"] == "unresolved"
    assert result["candidate"]["rows"] == [["1", "10"], ["qualifier", ""], ["2", "20"]]
    assert result["competingRowBands"]


def test_shared_native_rectangle_is_one_span_even_without_invented_axis_edges():
    words = [word("label", "Group", 5, 5, 30)]
    candidate = candidate_from_rows("n", 1, BOX, [["Group"], ["Group"]], "native",
                                    cell_bounds=[[BOX], [BOX]], words=words)
    assert len(candidate["cells"]) == 1
    assert candidate["cells"][0]["rowSpan"] == 2
    assert candidate["cells"][0]["wordIds"] == ["label"]
    assert candidate["grid"][1][0] == {"state": "covered", "anchor": [1, 1]}
    assert candidate["rows"] == [["Group"], ["Group"]]  # Original parser evidence survives.
    assert candidate["logicalRows"] == [["Group"], [None]]


def test_complete_tile_rows_must_agree_on_columns_as_well_as_row_positions():
    plan = plan_tiles([0, 0, 100, 100], max_height=65, overlap=30)
    a, b = plan["tiles"]
    reads = [tile_read(a, [[10, 20], [40, 50]], [["a", "1"], ["b", "2"]]),
             tile_read(b, [[40, 50], [80, 90]], [["b", "2"], ["c", "3"]])]
    reads[0]["candidate"]["columnIntervals"] = [[0, 50], [50, 100]]
    reads[1]["candidate"]["columnIntervals"] = [[0, 80], [80, 100]]
    assert aggregate_tiles(plan, reads, "j", 1, [0, 0, 100, 100])["state"] == "unresolved"


def test_retokenized_base_word_ids_cannot_silently_be_reused():
    words = [word("a", "1", 5, 5)]
    raw = candidate_from_rows("n", 1, BOX, [["1"]], "native", cell_bounds=[[BOX]], words=words)
    retokenized = [word("a", "12", 5, 5)]
    with pytest.raises(ValueError, match="base occurrence"):
        recover_geometry(raw, retokenized)
    other = candidate_from_rows("alt", 1, BOX, [["1"]], "alternate", cell_bounds=[[BOX]], words=retokenized)
    assert not compare_candidates(raw, other)["agreed"]


def test_flattened_numeric_lanes_are_split_only_with_positioned_word_evidence():
    raw = candidate_from_rows("n", 1, BOX, [["1 2", "20 10"]], "native",
                              cell_bounds=[[[0, 0, 50, 70], [50, 0, 100, 70]]])
    assert recover_geometry(raw)["candidate"]["rows"] == [["1 2", "20 10"]]
    words = [word("a", "1", 5, 10), word("b", "2", 5, 35),
             word("c", "10", 55, 10), word("d", "20", 55, 35)]
    fixed = recover_geometry(raw, words)
    assert fixed["state"] == "recovered"
    assert fixed["candidate"]["rows"] == [["1", "10"], ["2", "20"]]


@pytest.mark.parametrize("cell_bounds", [None, [[[0, 0, 50, 70], [50, 0, 100, 70]]]])
def test_recovered_ink_bands_match_enclosing_visual_cell_bands_not_invented_ink_edges(cell_bounds):
    words = [word("a", "1", 5, 10), word("b", "2", 5, 40),
             word("c", "10", 55, 10), word("d", "20", 55, 40)]
    raw = candidate_from_rows("n", 1, BOX, [["1 2", "10 20"]], "native", cell_bounds=cell_bounds, words=words)
    fixed = recover_geometry(raw)["candidate"]
    read = visual([["1", "10"], ["2", "20"]], edges=[0, 500, 1000])
    read["columnIntervals"] = [[0, 50], [50, 100]]
    comparison = compare_candidates(fixed, read)
    assert comparison["agreed"], comparison["differences"]
    assert comparison["basis"] == "positioned-grid"


def test_missing_region_bounds_cannot_establish_a_physical_table_occurrence():
    unlocated = candidate_from_rows("unknown", 1, None, [["1"]], "ocr")
    result = compare_candidates(unlocated, visual([["1"]]))
    assert not result["agreed"]
    assert "missing-bounds" in [d["kind"] for d in result["differences"]]


def ruled_native_table(kind):
    """Extract real blanks, missing rectangles and both span axes with pdfplumber."""
    import pdfplumber
    from reportlab.pdfgen import canvas

    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(200, 140))
    pdf.line(20, 100, 180, 100)
    pdf.line(20, 20, 20, 100)
    pdf.line(20, 20, 100 if kind == "missing-rectangle" else 180, 20)
    pdf.line(180, 60 if kind == "missing-rectangle" else 20, 180, 100)
    pdf.line(100 if kind == "row-span" else 20, 60, 180, 60)
    pdf.line(100, 20, 100, 60 if kind == "column-span" else 100)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(25, 80, "Group" if "span" in kind else "A")
    if kind != "column-span":
        pdf.drawString(105, 80, "B")
    if kind != "row-span":
        pdf.drawString(25, 40, "C")
    if "span" in kind:
        pdf.drawString(105, 40, "D")
    pdf.save()
    stream.seek(0)
    with pdfplumber.open(stream) as document:
        page = document.pages[0]
        table, = page.find_tables()
        rows = table.extract()
        rectangles = [row.cells for row in table.rows]
        words = word_occurrences(1, page.extract_words())
        candidate = candidate_from_rows("native", 1, table.bbox, rows, "native-text-layer",
                                        cell_bounds=rectangles, words=words)
    return rows, rectangles, candidate


def test_real_native_blank_is_empty_and_agrees_but_ocr_null_remains_unavailable():
    rows, rectangles, native = ruled_native_table("blank")
    assert rows == [["A", "B"], ["C", ""]]
    assert rectangles[1][1] is not None
    assert native["grid"][1][1]["state"] == "empty"
    assert native["cells"][-1]["wordIds"] == []
    read = visual(rows, box=native["bounds"])
    assert compare_candidates(native, read)["agreed"]

    ocr_rows = [["A", "B"], ["C", None]]
    ocr = candidate_from_rows("ocr", 1, native["bounds"], ocr_rows, "model-transcription")
    unavailable_visual = visual(ocr_rows, box=native["bounds"])
    assert ocr["rows"] == ocr_rows
    assert ocr["logicalRows"][1][1] is None
    assert ocr["grid"][1][1]["state"] == "unreadable"
    assert unavailable_visual["grid"][1][1]["state"] == "unreadable"
    assert not compare_candidates(ocr, read)["agreed"]
    assert not compare_candidates(native, unavailable_visual)["agreed"]


@pytest.mark.parametrize("kind,rows,span,covered", [
    ("column-span", [["Group", None], ["C", "D"]], (1, 2), (0, 1)),
    ("row-span", [["Group", "B"], [None, "D"]], (2, 1), (1, 0)),
])
def test_real_native_missing_rectangle_covered_by_span_is_not_empty_or_unreadable(kind, rows, span, covered):
    raw, rectangles, native = ruled_native_table(kind)
    assert raw == rows
    r, c = covered
    assert rectangles[r][c] is None
    assert native["rows"] == rows
    assert native["logicalRows"] == rows
    assert native["grid"][r][c] == {"state": "covered", "anchor": [1, 1]}
    anchor = native["cells"][0]
    assert (anchor["rowSpan"], anchor["columnSpan"]) == span
    response = {"tablePresent": True, "rowCount": 2, "columnCount": 2,
                "cells": [{k: cell[k] for k in ("row", "column", "text", "rowSpan", "columnSpan")}
                          for cell in native["cells"]]}
    read = validate_visual(response, "v", 1, native["bounds"])["candidate"]
    assert compare_candidates(native, read)["agreed"]
    false_empty = [["" if value is None else value for value in row] for row in rows]
    assert not compare_candidates(native, visual(false_empty, box=native["bounds"]))["agreed"]


def test_real_native_missing_uncovered_rectangle_does_not_invent_empty_cell():
    rows, rectangles, native = ruled_native_table("missing-rectangle")
    assert rows == [["A", "B"], ["C", None]]
    assert rectangles[1][1] is None
    assert native["rows"] == rows
    assert native["logicalRows"][1][1] is None
    assert native["grid"][1][1]["state"] == "unreadable"
    assert not compare_candidates(native, visual([["A", "B"], ["C", ""]], box=native["bounds"]))["agreed"]


@pytest.mark.parametrize("rows,columns,limits,name,requested,limit", [
    (11, 1, {"max_rows": 10}, "rows", 11, 10),
    (1, 11, {"max_columns": 10}, "columns", 11, 10),
    (3, 3, {"max_rows": 3, "max_columns": 3, "max_grid_cells": 8}, "gridCells", 9, 8),
    (101, 101, {"max_grid_cells": 1000}, "gridCells", 10201, 1000),
])
def test_visual_counts_are_preflighted_before_grid_allocation(rows, columns, limits, name, requested, limit):
    response = {"tablePresent": True, "rowCount": rows, "columnCount": columns, "cells": []}
    result = validate_visual(response, "v", 1, BOX, **limits)
    assert not result["valid"]
    assert result["candidate"] is None
    assert result["raw"] == response
    assert result["resourceLimit"] == {"name": name, "requested": requested, "limit": limit}
    assert result["errors"]


@pytest.mark.parametrize("limit", [False, -1, 1.5, float("inf")])
@pytest.mark.parametrize("name", ["max_rows", "max_columns", "max_grid_cells"])
def test_visual_limit_configuration_rejects_invalid_values(name, limit):
    response = {"tablePresent": False, "rowCount": 0, "columnCount": 0, "cells": []}
    with pytest.raises(ValueError, match=name):
        validate_visual(response, "v", 1, BOX, **{name: limit})


def test_visual_limits_accept_exact_boundary_and_remain_optional_without_padding_omissions():
    response = {"tablePresent": True, "rowCount": 2, "columnCount": 2,
                "cells": [{"row": 1, "column": 1, "text": "001"}]}
    for limits in ({}, {"max_rows": 2, "max_columns": 2, "max_grid_cells": 4}):
        result = validate_visual(response, "v", 1, BOX, **limits)
        assert result["valid"]
        assert result["resourceLimit"] is None
        assert result["candidate"]["logicalRows"] == [["001", None], [None, None]]
        assert result["candidate"]["grid"][1][1]["state"] == "omitted"
    response["rowCount"] = 20
    result = validate_visual(response, "v", 1, BOX, max_rows=None, max_columns=None, max_grid_cells=None)
    assert result["valid"]
    assert result["candidate"]["rowCount"] == 20


def test_zero_visual_limits_allow_non_table_response_but_reject_present_table():
    no_table = {"tablePresent": False, "rowCount": 0, "columnCount": 0, "cells": []}
    result = validate_visual(no_table, "v", 1, BOX, max_rows=0, max_columns=0, max_grid_cells=0)
    assert result["valid"]
    assert result["resourceLimit"] is None
    table = {"tablePresent": True, "rowCount": 1, "columnCount": 1, "cells": []}
    for option, resource in (("max_rows", "rows"), ("max_columns", "columns"), ("max_grid_cells", "gridCells")):
        result = validate_visual(table, "v", 1, BOX, **{option: 0})
        assert not result["valid"]
        assert result["resourceLimit"] == {"name": resource, "requested": 1, "limit": 0}


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS fixture is Linux-specific")
@pytest.mark.parametrize("encoded", [False, True])
def test_visual_grid_product_is_rejected_in_memory_limited_worker_before_allocation(encoded):
    # An allocation-before-validation regression fails in the child, not by
    # exhausting the pytest worker or the host. Both axes fit service defaults.
    script = f"""
import json
import resource
from preprocessing.formats.pdf_tables import validate_visual
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
response = {{"tablePresent": True, "rowCount": 2_000_000, "columnCount": 16_384, "cells": []}}
if {encoded}:
    response = json.dumps(response)
result = validate_visual(response, "v", 1, [0, 0, 100, 70],
                         max_rows=2_000_000, max_columns=16_384, max_grid_cells=100_000)
assert not result["valid"] and result["candidate"] is None
assert result["raw"] == response
print(json.dumps(result["resourceLimit"]))
"""
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"name": "gridCells", "requested": 32_768_000_000, "limit": 100_000}


def test_native_cut_through_one_whole_word_is_repaired_even_with_unchanged_baseline_count():
    words = [word("name", "Aeolian", 5, 5, 25), word("code", "eo", 60, 5, 8)]
    raw = candidate_from_rows("raw", 1, [0, 0, 80, 20], [["Aeo", "lian", "eo"]], "native",
                              cell_bounds=[[[0, 0, 20, 20], [20, 0, 50, 20], [50, 0, 80, 20]]], words=words)
    result = recover_geometry(raw)
    assert result["state"] == "recovered"
    assert result["candidate"]["logicalRows"] == [["Aeolian", "eo"]]
    assert [c["wordIds"] for c in result["candidate"]["cells"]] == [["name"], ["code"]]
    assert result["unassignedWordIds"] == []
    assert raw["rows"] == [["Aeo", "lian", "eo"]]


@pytest.mark.parametrize("alignment", ["left", "centre", "right"])
def test_repeated_subcolumns_replace_collapsed_native_numeric_lane_and_preserve_pairing(alignment):
    expected = [["S1", "0.67", "0.008"], ["S2", "1.82", "<0.1"]]
    words = []
    for r, (y, row) in enumerate(zip([5, 25], expected), 1):
        words.append(word(f"s{r}", row[0], 5, y, 7))
        for c, (value, anchor, width) in enumerate(zip(row[1:], [40, 75], [15, 20 if r == 1 else 15]), 1):
            x = anchor if alignment == "left" else anchor - width / 2 if alignment == "centre" else anchor - width
            words.append(word(f"v{r}{c}", value, x, y, width))
    raw = candidate_from_rows("raw", 1, [0, 0, 110, 40], [["S1\nS2", "0.008 0.67\n<0.1 1.82"]], "native",
                              cell_bounds=[[[0, 0, 20, 40], [20, 0, 110, 40]]], words=words)
    result = recover_geometry(raw)
    assert result["state"] == "recovered"
    assert result["candidate"]["logicalRows"] == expected
    assert result["candidate"]["columnCount"] == 3
    assert result["candidate"]["logicalRows"] != [["S1", "0.008", "0.67"], ["S2", "<0.1", "1.82"]]
    assert [c["wordIds"] for c in result["candidate"]["cells"]] == [["s1"], ["v11"], ["v12"], ["s2"], ["v21"], ["v22"]]


def test_two_native_lanes_recover_fifteen_repeated_measurement_positions_without_text_dedup():
    expected = [[f"S{r}", *["001" if c % 2 else "<0.5" for c in range(15)]] for r in range(1, 4)]
    words = [word(f"r{r}c{c}", value, 5 if c == 0 else 30 + (c - 1) * 25, 5 + r * 20,
                  10 if c == 0 else 12 if value == "001" else 16)
             for r, row in enumerate(expected) for c, value in enumerate(row)]
    raw = candidate_from_rows("raw", 1, [0, 0, 410, 75],
                              [["\n".join(row[0] for row in expected), "\n".join(" ".join(row[1:]) for row in expected)]], "native",
                              cell_bounds=[[[0, 0, 20, 75], [20, 0, 410, 75]]], words=words)
    fixed = recover_geometry(raw)["candidate"]
    assert (fixed["rowCount"], fixed["columnCount"]) == (3, 16)
    assert fixed["logicalRows"] == expected
    assert len({id for cell in fixed["cells"] for id in cell["wordIds"]}) == 48


def test_redundant_graphic_strips_and_row_edges_do_not_turn_wrapped_cell_into_spans_or_rows():
    # The two real columns each have a narrow background strip on either side.
    # The background also cuts the description between its two source baselines.
    rows = [["ID", None, None, "Description", None, None],
            ["A1", None, None, "", "North fault", ""],
            [None, None, None, None, "(12 km)", None],
            ["A2", None, None, "Second", None, None]]
    rectangles = [
        [[0, 0, 50, 15], None, None, [50, 0, 120, 15], None, None],
        [[0, 15, 50, 40], None, None, [50, 15, 52, 40], [52, 15, 118, 27], [118, 15, 120, 40]],
        [None, None, None, None, [52, 27, 118, 40], None],
        [[0, 40, 50, 60], None, None, [50, 40, 120, 60], None, None],
    ]
    # Extra strip boundaries in the first column are genuinely retained input,
    # not a manually cleaned logical grid.
    rectangles[1][1] = [2, 15, 48, 40]
    rectangles[1][2] = [48, 15, 50, 40]
    words = [word("header-id", "ID", 5, 4, 10), word("header-description", "Description", 55, 4, 55),
             word("a1", "A1", 5, 18, 10), word("north", "North", 55, 18, 22), word("fault", "fault", 80, 18, 18),
             word("annotation", "(12", 55, 29, 15), word("units", "km)", 73, 29, 15),
             word("a2", "A2", 5, 45, 10), word("second", "Second", 55, 45, 30)]
    raw = candidate_from_rows("raw", 1, [0, 0, 120, 60], rows, "native", cell_bounds=rectangles, words=words)
    assert raw["columnCount"] == 6
    assert any(c["rowSpan"] > 1 or c["columnSpan"] > 1 for c in raw["cells"])
    result = recover_geometry(raw)
    fixed = result["candidate"]
    assert result["state"] == "recovered"
    assert fixed["logicalRows"] == [["ID", "Description"], ["A1", "North fault\n(12 km)"], ["A2", "Second"]]
    assert all(c["rowSpan"] == c["columnSpan"] == 1 for c in fixed["cells"])
    assert sum("a1" in c["wordIds"] for c in fixed["cells"]) == 1
    assert fixed["cells"][3]["wordIds"] == ["north", "fault", "annotation", "units"]


def test_repaired_column_grid_keeps_real_spanning_title_at_one_anchor():
    rows = [["Group label", None, None], ["Aeo", "lian", "eo"], ["Oth", "er", "ot"]]
    rectangles = [[[0, 0, 100, 12], None, None],
                  [[0, 12, 20, 30], [20, 12, 50, 30], [50, 12, 100, 30]],
                  [[0, 30, 20, 50], [20, 30, 50, 50], [50, 30, 100, 50]]]
    words = [word("title1", "Group", 5, 2, 25), word("title2", "label", 32, 2, 20),
             word("name1", "Aeolian", 5, 17, 25), word("code1", "eo", 60, 17, 8),
             word("name2", "Other", 5, 35, 24), word("code2", "ot", 60, 35, 8)]
    raw = candidate_from_rows("raw", 1, [0, 0, 100, 50], rows, "native", cell_bounds=rectangles, words=words)
    fixed = recover_geometry(raw)["candidate"]
    assert fixed["logicalRows"] == [["Group label", None], ["Aeolian", "eo"], ["Other", "ot"]]
    assert fixed["cells"][0]["columnSpan"] == 2
    assert fixed["grid"][0][1] == {"state": "covered", "anchor": [1, 1]}
    assert fixed["cells"][0]["wordIds"] == ["title1", "title2"]
    assert sum("title1" in cell["wordIds"] for cell in fixed["cells"]) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_parent_linked_layout_disagreement_is_one_occurrence_in_either_input_order(reverse):
    native = candidate_from_rows("native", 1, BOX, [["Name", "Value"], ["A", "1"]], "native")
    alternate = candidate_from_rows("alternate", 1, BOX, [["Name", "", "Value"], ["A", "", "1"]], "line-text")
    alternate["parentId"] = "native"
    groups = group_candidates([alternate, native] if reverse else [native, alternate])
    assert len(groups) == 1
    assert set(groups[0]["candidateIds"]) == {"native", "alternate"}
    assert "layout-disagreement" in groups[0]["issues"]
    assert not compare_candidates(native, alternate)["agreed"]


@pytest.mark.parametrize("neighbour_bounds", [[90, 0, 180, 40], [10, 0, 110, 40]])
def test_parent_relation_does_not_merge_neighbouring_overlapping_or_unlocated_regions(neighbour_bounds):
    native = candidate_from_rows("native", 1, [0, 0, 100, 40], [["same", "same"]], "native")
    neighbour = candidate_from_rows("neighbour", 1, neighbour_bounds, [["same", "same", "same"]], "line-text")
    neighbour["parentId"] = "native"
    unrelated = candidate_from_rows("unrelated", 1, [0, 0, 100, 40], [["same"]], "native")
    unlocated = candidate_from_rows("unlocated", 1, None, [["same"]], "line-text")
    unlocated["parentId"] = "native"
    groups = group_candidates([native, neighbour, unrelated, unlocated])
    assert len(groups) == 4
    assert next(g for g in groups if g["candidateIds"] == ["native"])["ambiguousWith"]


def test_contiguous_wrapped_paragraphs_do_not_supply_multiple_column_alignments():
    words = []
    for r in range(4):
        # Repeated word positions alone used to propose a three-column table.
        for c, (text, x) in enumerate(zip(["These", "faults", "continue"], [5, 27, 49])):
            words.append(word(f"r{r}c{c}", text, x, 10 + r * 12, 20))
    assert propose_regions(words, 1) == []


def test_bullet_prefix_and_hanging_prose_indent_are_not_two_table_columns():
    words = []
    for r in range(3):
        words.extend([word(f"bullet{r}", "•", 5, 5 + r * 30, 4),
                      word(f"text{r}", "A sentence", 30, 5 + r * 30, 75),
                      word(f"wrap{r}", "continues here", 30, 16 + r * 30, 65)])
    assert propose_regions(words, 1) == []
    assert len(propose_regions(words, 1, include_lists=True)) == 1
    # Repeated equal numeric codes are occurrences, not bullets or duplicates.
    for w in words:
        if w["text"] == "•":
            w["text"] = "1"
    assert len(propose_regions(words, 1)) == 1


def test_unused_graphic_spacer_inside_repeated_column_pairs_is_not_a_logical_field():
    edges = [0, 35, 50, 64, 95, 110, 145, 160]
    expected = [["Alpha", "a", "Beta", "b", "Gamma", "g"],
                ["Delta", "d", "Epsilon", "e", "Zeta", "z"]]
    raw_rows = [["Group", *[None] * 6], *[[*row[:2], "", *row[2:]] for row in expected]]
    boxes = [[[0, 0, 160, 15], *[None] * 6],
             *[[[left, top, right, top + 20] for left, right in pairwise(edges)] for top in (15, 35)]]
    words = [word("title", "Group", 5, 4, 24)]
    for r, row in enumerate(expected):
        for c, (value, x) in enumerate(zip(row, [5, 40, 65, 100, 115, 150])):
            words.append(word(f"r{r}c{c}", value, x, 19 + r * 20, 25 if c % 2 == 0 else 5))
    raw = candidate_from_rows("raw", 1, [0, 0, 160, 55], raw_rows, "native", cell_bounds=boxes, words=words)
    fixed = recover_geometry(raw)["candidate"]
    assert fixed["logicalRows"] == [["Group", *[None] * 5], *expected]
    assert fixed["cells"][0]["columnSpan"] == 6


def test_a_wide_native_empty_field_is_not_deleted_just_because_no_words_occupy_it():
    rows = [["Left", "", "Right"], ["Again", "", "Next"]]
    rectangles = [[[left, top, right, top + 20] for left, right in ((0, 40), (40, 80), (80, 120))] for top in (0, 20)]
    words = [word(f"l{r}", row[0], 5, 5 + r * 20, 25) for r, row in enumerate(rows)]
    words += [word(f"r{r}", row[2], 85, 5 + r * 20, 25) for r, row in enumerate(rows)]
    raw = candidate_from_rows("raw", 1, [0, 0, 120, 40], rows, "native", cell_bounds=rectangles, words=words)
    assert recover_geometry(raw)["candidate"]["logicalRows"] == rows


def test_true_row_span_is_preserved_when_other_native_lane_splits_into_subcolumns():
    words = [word("label", "Group", 5, 5, 20), word("a1", "1", 40, 5, 5), word("b1", "10", 70, 5, 10),
             word("a2", "2", 40, 25, 5), word("b2", "20", 70, 25, 10)]
    raw = candidate_from_rows("raw", 1, [0, 0, 100, 45], [["Group", "1 10"], [None, "2 20"]], "native",
                              cell_bounds=[[[0, 0, 30, 45], [30, 0, 100, 20]], [None, [30, 20, 100, 45]]], words=words)
    fixed = recover_geometry(raw)["candidate"]
    assert fixed["logicalRows"] == [["Group", "1", "10"], [None, "2", "20"]]
    assert fixed["cells"][0]["rowSpan"] == 2
    assert fixed["grid"][1][0] == {"state": "covered", "anchor": [1, 1]}
    assert sum("label" in cell["wordIds"] for cell in fixed["cells"]) == 1


def test_same_positioned_word_occurrences_link_tight_proposal_to_native_region_despite_layout_disagreement():
    words = [word("name", "A", 10, 5, 10), word("value", "1", 70, 25, 10)]
    native = candidate_from_rows("native", 1, [0, 0, 100, 40], [["A", "1"]], "native", words=words)
    tight = candidate_from_rows("proposal", 1, [4, 3, 97, 38], [["A", "", "1"]], "text-alignment", words=words)
    groups = group_candidates([native, tight])
    assert len(groups) == 1
    assert groups[0]["candidateIds"] == ["native", "proposal"]
    assert "layout-disagreement" in groups[0]["issues"]
    assert groups[0]["relations"][0]["kind"] == "word-occurrences"
    assert set(groups[0]["relations"][0]["wordIds"]) == {"name", "value"}


def test_overlapping_regions_with_different_source_occurrences_are_not_grouped_by_equal_text():
    words = [word("left", "same", 1, 5, 5), word("shared", "same", 50, 5, 10), word("right", "same", 103, 5, 5)]
    first = candidate_from_rows("first", 1, [0, 0, 100, 40], [["same", "same"]], "native", words=words)
    second = candidate_from_rows("second", 1, [10, 0, 110, 40], [["same", "same", "same"]], "alternate", words=words)
    first["parentId"] = second["parentId"] = "outer"
    groups = group_candidates([first, second])
    assert len(groups) == 2
    assert all(group["ambiguousWith"] for group in groups)


def test_close_numeric_rows_under_genuine_group_spans_survive_graphic_column_repair():
    edges = [0, 2, 48, 50, 52, 98, 100]
    rows = [["Group A", None, None, "1", None, None],
            [None, None, None, "2", None, None],
            ["Group B", None, None, "3", None, None],
            [None, None, None, "4", None, None]]
    boxes = [[[left, top, right, top + 10] for left, right in pairwise(edges)] for top in (0, 10, 20, 30)]
    for r in (0, 2):
        boxes[r][0] = [0, r * 10, 50, r * 10 + 20]
        boxes[r][1:3] = [None, None]
        boxes[r + 1][:3] = [None, None, None]
    for r in range(4):
        boxes[r][3:] = [[50, r * 10, 100, r * 10 + 10], None, None]
    words = [word("group-a", "Group A", 5, 1, 30), word("group-b", "Group B", 5, 21, 30)]
    words += [word(f"value{r}", str(r + 1), 55, 1 + r * 10, 5) for r in range(4)]
    raw = candidate_from_rows("raw", 1, [0, 0, 100, 40], rows, "native", cell_bounds=boxes, words=words)
    fixed = recover_geometry(raw)["candidate"]
    assert fixed["logicalRows"] == [["Group A", "1"], [None, "2"], ["Group B", "3"], [None, "4"]]
    assert [(c["text"], c["rowSpan"]) for c in fixed["cells"] if c["column"] == 1] == [("Group A", 2), ("Group B", 2)]


def test_false_horizontal_row_spans_recover_from_consistent_outer_anchor_extents_without_column_change():
    intervals = [[0, 30], [30, 60], [60, 130], [130, 160], [160, 190], [190, 220]]
    rows = [["ID", "Rank", "Text", "X", "Y", "Kind"],
            ["A1", "1", "North fault", "10", "20", "Li"],
            [None, None, "then here", None, None, None]]
    boxes = [[[left, 0, right, 15] for left, right in intervals],
             [[left, 15, right, 40] for left, right in intervals], [None] * 6]
    boxes[1][2] = [60, 15, 130, 27]
    boxes[2][2] = [60, 27, 130, 40]
    words = [word(f"header{c}", text, x, 4, 20 if c in (1, 2) else 10)
             for c, (text, x) in enumerate(zip(rows[0], [5, 35, 65, 135, 165, 195]))]
    words += [word(f"data{c}", text, x, 18, 10)
              for c, (text, x) in enumerate(zip(["A1", "1", "North", "10", "20", "Li"], [5, 35, 65, 135, 165, 195]))]
    words += [word("fault", "fault", 78, 18, 20), word("then", "then", 65, 29, 20), word("here", "here", 88, 29, 20)]
    raw = candidate_from_rows("raw", 1, [0, 0, 220, 40], rows, "native", cell_bounds=boxes, words=words)
    result = recover_geometry(raw)
    assert result["state"] == "recovered"
    assert result["candidate"]["logicalRows"] == [rows[0], ["A1", "1", "North fault\nthen here", "10", "20", "Li"]]
    assert all(c["rowSpan"] == c["columnSpan"] == 1 for c in result["candidate"]["cells"])


def test_wholly_empty_native_row_is_not_deleted_by_word_only_row_assessment():
    rows = [["A", "1"], ["", ""], ["B", "2"]]
    boxes = [[[left, top, right, top + 10] for left, right in ((0, 50), (50, 100))] for top in (0, 10, 20)]
    words = [word("a", "A", 5, 1), word("v1", "1", 55, 1), word("b", "B", 5, 21), word("v2", "2", 55, 21)]
    raw = candidate_from_rows("raw", 1, [0, 0, 100, 30], rows, "native", cell_bounds=boxes, words=words)
    assert recover_geometry(raw)["candidate"]["logicalRows"] == rows


def test_column_recovery_does_not_convert_unavailable_source_field_without_words_to_empty():
    words = [word("v11", "1", 40, 5, 5), word("v12", "10", 70, 5, 10),
             word("v21", "2", 40, 25, 5), word("v22", "20", 70, 25, 10)]
    raw = candidate_from_rows("ocr", 1, [0, 0, 100, 45], [[None, "1 10"], [None, "2 20"]], "model-transcription",
                              cell_bounds=[[[0, 0, 20, 20], [20, 0, 100, 20]],
                                           [[0, 20, 20, 45], [20, 20, 100, 45]]], words=words)
    fixed = recover_geometry(raw)["candidate"]
    assert fixed["logicalRows"] == [[None, "1", "10"], [None, "2", "20"]]
    assert fixed["grid"][0][0]["state"] == fixed["grid"][1][0]["state"] == "unreadable"
    assert not compare_candidates(fixed, visual([["", "1", "10"], ["", "2", "20"]], box=fixed["bounds"]))["agreed"]
