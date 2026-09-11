import csv
import json

import pytest

from analysis.llm import (
    FakeBackend,
    select_answerer,
    select_figure_classifier,
    select_image_reader,
    select_table_mapper,
)
from analysis.reduce import infer_study_stage, parse_lat_lon
from analysis.assertions import harvest_assertions
from analysis.compute import qaqc, resources
from analysis.claims import verify_claim
from analysis.spans import select_span_extractor
from analysis.tables import build_records
from pipeline import parse_ocr, verify_tables
from pipeline import describe_figures
from pipeline.digital_ocr import _prepare_assay_text_rows, build_page_markdown
from pipeline.util import Paths


def test_digital_markdown_preserves_numbered_sections_and_tables():
    layout_text = """
    Beartrack-Arnett Heap Leach Project
    Preliminary Feasibility Study

    SUMMARY

    Body text.

    June 2023                                      Page 1-1
    """
    markdown = build_page_markdown(
        text="SUMMARY\nBody text.\nPage 1-1",
        layout_text=layout_text,
        tables=[[['Item', 'Value'], ['Gold | total', '12\n345']]],
    )

    assert markdown.startswith("# 1 SUMMARY\n\n")
    assert "| Item | Value |" in markdown
    assert "| Gold \\| total | 12 345 |" in markdown


def test_digital_markdown_collapses_multirow_table_headers():
    markdown = build_page_markdown(
        text="Table body",
        layout_text="Table body\nPage 11-2",
        tables=[[
            ["", "Standard Name", "Best", "Relative Std Dev"],
            ["Year", "", "Value / Average", ""],
            ["2022", "OREAS 507", "0.176", "3.409"],
        ]],
    )

    assert "| Year | Standard Name | Best / Value / Average | Relative Std Dev |" in markdown
    assert "| 2022 | OREAS 507 | 0.176 | 3.409 |" in markdown
    assert markdown.count("\n| --- | --- | --- | --- |\n") == 1


def test_digital_markdown_expands_borderless_multiline_data_rows():
    markdown = build_page_markdown(
        text="Table body",
        layout_text="Table body\nPage 1-2",
        tables=[[
            ["Deposit", "Category", "Tonnage", "Grade", "Comment"],
            ["Beartrack", "Proven\nProbable", "7,077\n17,196", "1.06\n0.82", "Estimate\n97,080\nrounded"],
            ["", "Proven + Probable", "24,273", "0.89", ""],
        ]],
    )

    assert "| Beartrack | Proven | 7,077 | 1.06 | Estimate 97,080 rounded |" in markdown
    assert "|  | Probable | 17,196 | 0.82 |  |" in markdown
    assert "Proven Probable" not in markdown


def test_digital_markdown_moves_multiline_values_into_existing_continuation_row():
    markdown = build_page_markdown(
        text="Table body",
        layout_text="Table body\nPage 1-3",
        tables=[[
            ["Mine Parameter", "Unit", "PP", "YR 1", "LOM"],
            ["Waste Rock", "(t ,000)", "3,900\n3,538", "14,872\n13,490", "97,080"],
            ["Mined", "(T ,000)", None, None, "88,058"],
        ]],
    )

    assert "| Waste Rock Mined | (t ,000) | 3,900 | 14,872 | 97,080 |" in markdown
    assert "|  | (T ,000) | 3,538 | 13,490 | 88,058 |" in markdown


def test_digital_markdown_keeps_wrapped_group_labels_with_their_data_rows():
    markdown = build_page_markdown(
        text="Table body",
        layout_text="Table body\nPage 9-2",
        tables=[[
            ["Area", "Hole", "From", "To", "Type"],
            ["Ward's\nGulch", "H1\nH2\nH3", "10\n20\n30", "11\n21\n31", "DD\nDD"],
        ]],
    )

    assert "| Ward's Gulch | H1 | 10 | 11 | DD |" in markdown
    assert "|  | H2 | 20 | 21 | DD |" in markdown
    assert "|  | H3 | 30 | 31 |  |" in markdown


def test_assay_text_rows_preserve_sparse_qualifier_alignment():
    default_rows = [
        ["Hole", "Area", "From", "To", "Grade"],
        ["H1\nincluding", "South\nPit", "1\n20\n5", "10\n30\n6", "0.5\n0.7\n2.0"],
    ]
    text_rows = [
        ["Hole", "Area", "From", "To", "Grade"],
        ["H1", "South", "1", "10", "0.5"],
        ["", "Pit", "20", "30", "0.7"],
        ["including", "", "5", "6", "2.0"],
        ["H2", "North", "40", "50", "0.8"],
    ]

    rows = _prepare_assay_text_rows(default_rows, text_rows)

    assert rows[1] == ["H1", "South Pit", "1", "10", "0.5"]
    assert rows[2] == ["", "", "20", "30", "0.7"]
    assert rows[3] == ["including", "", "5", "6", "2.0"]
    assert rows[4] == ["H2", "North", "40", "50", "0.8"]


def test_assay_group_label_spanning_all_numeric_lanes_stays_grouped():
    default_rows = [
        ["Hole", "Area", "From", "To", "Grade"],
        ["H1\nH2", "Ward's\nGulch", "10\n20", "11\n21", "1.0\n2.0"],
    ]
    text_rows = [
        ["Hole", "Area", "From", "To", "Grade"],
        ["H1", "Ward's", "10", "11", "1.0"],
        ["H2", "Gulch", "20", "21", "2.0"],
    ]

    rows = _prepare_assay_text_rows(default_rows, text_rows)

    assert rows[1][1] == "Ward's Gulch"
    assert rows[2][1] == ""


def test_parser_preserves_digital_table_provenance_and_verification_is_conservative(tmp_path):
    paths = Paths(tmp_path / "report")
    paths.ensure_dirs()
    ocr_dir = paths.root / "ocr"
    ocr_dir.mkdir()
    (ocr_dir / "page_0001.md").write_text(
        "# 1 Summary\n\n| Item | Value |\n| --- | --- |\n| Gold \\| total | 12 |\n"
    )
    (ocr_dir / "page_0001.json").write_text(json.dumps({
        "page": 1,
        "method": "digital_text",
        "dimensions": {"width": 612, "height": 792},
        "tables": [{"bbox": [10, 20, 200, 120]}],
        "images": [],
    }))

    parse_ocr.run(paths, n_pages=1)
    table = json.loads((paths.root / "tables_index.json").read_text())[0]
    assert table["method"] == "digital_text"
    assert table["bbox"] == [10, 20, 200, 120]

    with (paths.root / table["csv"]).open(newline="") as source:
        assert list(csv.reader(source)) == [["Item", "Value"], ["Gold | total", "12"]]
    words = [
        {"t": str(number), "x0": 10, "y0": 20, "x1": 20, "y1": 30}
        for number in range(1, 22)
    ]
    (paths.root / "ground_truth.json").write_text(json.dumps({
        "pages": [{"page": 1, "width": 612, "height": 792, "words": words}]
    }))

    verify_tables.run(paths)
    verified = json.loads((paths.root / "tables_verified.json").read_text())[0]
    assert verified["text_layer_checksum"]["pass"] is True
    assert verified["confidence"] == "low"
    assert verified["confidence_basis"] == "same_text_layer_consistency_only"


def test_analysis_routes_each_task_to_its_configured_model(tmp_path):
    env = {
        "ANALYSIS_LLM_BACKEND": "opencode",
        "OPENCODE_SERVER_URL": "http://127.0.0.1:1",
        "ANALYSIS_LLM_MODEL": "openai/fallback",
        "ANALYSIS_TABLE_MODEL": "zai-coding-plan/glm-5.3-flash",
        "ANALYSIS_FIGURE_MODEL": "opencode-go/deepseek-v4-flash",
        "ANALYSIS_SPAN_MODEL": "zai-coding-plan/glm-5.3-flash",
        "ANALYSIS_IMAGE_MODEL": "openai/gpt-5.6-luna",
        "ANALYSIS_ANSWER_MODEL": "openai/gpt-5.6-luna",
    }

    mapper = select_table_mapper(tmp_path, env=env)
    classifier = select_figure_classifier(tmp_path, env=env)
    span_extractor = select_span_extractor(tmp_path, env=env)
    answerer = select_answerer(tmp_path, env=env)
    image_reader = select_image_reader(tmp_path, env=env)

    assert mapper.fallback.model == env["ANALYSIS_TABLE_MODEL"]
    assert classifier.fallback.model == env["ANALYSIS_FIGURE_MODEL"]
    assert span_extractor.fallback.model == env["ANALYSIS_SPAN_MODEL"]
    assert answerer.model == env["ANALYSIS_ANSWER_MODEL"]
    assert image_reader.model == env["ANALYSIS_IMAGE_MODEL"]


def test_assertion_citation_uses_the_page_containing_its_span(tmp_path):
    source = tmp_path / "text" / "04_property.md"
    source.parent.mkdir()
    span = "All 562 unpatented lode claims are in good standing until September 1, 2023."
    source.write_text(
        "<!-- page: 49 -->\n\nProperty description.\n\n"
        f"<!-- page: 52 -->\n\n{span}\n"
    )
    extractor = FakeBackend(span_extractions={
        "4_property": [{
            "span": span,
            "predicate": "title_or_tenure",
            "modality": "done",
            "ni43101_item": 4,
            "confidence": "high",
        }]
    })

    assertions, evidence, _ = harvest_assertions(
        tmp_path,
        sections=[{
            "number": "4",
            "slug": "4_property",
            "page_start": 49,
            "text_file": "text/04_property.md",
        }],
        entities={},
        span_extractor=extractor,
    )

    assert assertions[0]["page"] == 52
    assert evidence[assertions[0]["evidence_id"]]["page"] == 52


def test_low_confidence_tables_cannot_drive_metric_verdicts():
    provenance = {"confidence": "low", "table_id": "table-1", "page": 1}
    numeric = lambda value: {"value": value}
    resource = resources([{
        "_id": "resource-1",
        "provenance": provenance,
        "tonnage": numeric(7_434_000),
        "grade_gpt": numeric(1.03),
        "contained": numeric(224),
    }])
    qaqc_result = qaqc([{
        "_id": "crm-1",
        "provenance": provenance,
        "crm_id": "OREAS 507",
        "certified_value": numeric(0.176),
        "std_dev": numeric(0.006),
        "pct_passing": numeric(3.409),
    }])

    assert resource["rows"][0]["recompute_ok"] is None
    assert resource["n_recompute_mismatches"] == 0
    assert resource["n_low_confidence_recompute_differences"] == 1
    assert qaqc_result["crms"][0]["verdict"] == "unknown"
    assert qaqc_result["overall"] == "unknown"


def test_resource_normalization_skips_footnote_rows_without_numeric_values():
    rows = [
        ["Deposit", "Category", "Tonnage", "Grade", "Contained"],
        ["Beartrack", "Proven", "7,077", "1.06", "219"],
        ["Notes: Numbers may not sum due to rounding.", "", "", "", ""],
        ["Note: Underground cutoff", "", "", "0.069 oz/t (2.37 g/T)", ""],
    ]
    mapping = {
        "table_type": "resource_estimate",
        "columns": [
            {"source_index": 0, "canonical_field": "deposit"},
            {"source_index": 1, "canonical_field": "category"},
            {"source_index": 2, "canonical_field": "tonnage", "unit": "kt"},
            {"source_index": 3, "canonical_field": "grade_gpt", "unit": "g/t"},
            {"source_index": 4, "canonical_field": "contained", "unit": "koz"},
        ],
        "carry_fields": ["deposit"],
    }

    records, _ = build_records(rows, mapping, {
        "id": "resource-table",
        "page": 27,
        "confidence": "low",
        "method": "digital_text",
        "csv": "tables/resource-table.csv",
    })

    assert len(records) == 1
    assert records[0]["deposit"] == "Beartrack"


def test_study_stage_is_distinct_from_resource_evidence_stage():
    text = "Preliminary Feasibility Study NI 43-101 Technical Report"

    assert infer_study_stage(text) == "pre-feasibility"
    assert infer_study_stage("Table 1-4: PFS Capital Cost Summary") == "pre-feasibility"


def test_location_parser_accepts_dms_and_rejects_wrapped_cardinal_letters():
    parsed = parse_lat_lon("Beartrack is at 45°14’13”N and 114°6’12”W.")

    assert parsed is not None
    assert parsed[0] == 45 + 14 / 60 + 13 / 3600
    assert parsed[1] == -(114 + 6 / 60 + 12 / 3600)
    assert parse_lat_lon("60\ns 140\ne") is None
    assert parse_lat_lon("95.0 N, 114.0 W") is None


def test_claim_verifier_accepts_evidence_id_as_a_citation_object_key():
    evidence_id = "ev:assertion:4_property:legal:abc123"
    assertion_id = "as:4_property:legal:abc123"
    metadata = {
        "assertions": [{"id": assertion_id, "evidence_id": evidence_id}],
        "_evidence_index": {
            evidence_id: {
                "source_kind": "computed_metric",
                "element_id": "assertion-1",
                "page": 52,
            }
        }
    }

    verdict = verify_claim(
        "The claim is grounded.",
        [{evidence_id: "grounded assertion"}],
        metadata,
    )
    unknown = verify_claim(
        "The claim is not grounded.",
        [{"ev:unknown": "grounded assertion"}],
        metadata,
    )
    assertion_alias = verify_claim(
        "The assertion alias is grounded.",
        [{"evidence_id": assertion_id}],
        metadata,
    )

    assert verdict["verified"] is True
    assert verdict["resolved_citations"][0]["evidence_id"] == evidence_id
    assert assertion_alias["verified"] is True
    assert assertion_alias["resolved_citations"][0]["evidence_id"] == evidence_id
    assert unknown["verified"] is False


def test_opencode_figure_configuration_fails_before_parallel_work(tmp_path, monkeypatch):
    paths = Paths(tmp_path / "report")
    paths.ensure_dirs()
    (paths.images / "fig_p0001_1.jpg").write_bytes(b"not-read-before-validation")
    monkeypatch.setenv("PIPELINE_VISION_BACKEND", "opencode")
    monkeypatch.delenv("OPENCODE_SERVER_URL", raising=False)

    with pytest.raises(ValueError, match="OPENCODE_SERVER_URL"):
        describe_figures.run(paths)

    monkeypatch.setenv("OPENCODE_SERVER_URL", "http://127.0.0.1:8787")
    monkeypatch.setenv("PIPELINE_VISION_WORKERS", "many")
    with pytest.raises(ValueError, match="PIPELINE_VISION_WORKERS"):
        describe_figures.run(paths)
