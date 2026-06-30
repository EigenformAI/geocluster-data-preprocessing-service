from __future__ import annotations

import csv
from pathlib import Path

from analysis.claims import verify_claim
from analysis.compute import computed_metric_evidence, intercepts
from analysis.figures import classify_figure_description, is_georeference_candidate, is_map_candidate
from analysis.hie import build_report_metadata
from analysis.mcp import handle_request
from analysis.mcp_tools import GeoReportTools
from analysis.run import run_analysis
from analysis.schemas import ColumnMap, MappedTable
from analysis.tables import build_records, guard_records


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def _mini_report(tmp_path: Path) -> Path:
    root = tmp_path / "output" / "mini_gold_report"
    (root / "text").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "tables").mkdir()

    (root / "text" / "01_summary.md").write_text(
        "The Mini Gold project is near 49.1 N, 57.2 W.\n"
    )
    (root / "images" / "fig_p0002_1.description.md").write_text(
        "This image is a **geological map** with a UTM grid, legend, north arrow, and scale bar.\n"
    )
    (root / "images" / "fig_p0002_1.jpg").write_bytes(b"fake-jpg")

    collar_rows = [
        ["Hole", "Easting", "Northing", "Az", "Dip", "EOH (m)"],
        ["MG-001", "500000", "5400000", "180", "-55", "150"],
    ]
    assay_rows = [
        ["Hole", "From (m)", "To (m)", "Interval (m)", "Au (g/t)", "Zone"],
        ["MG-001", "10", "20", "10", "2.5", "Main"],
        ["including", "12", "14", "2", "8.0", ""],
    ]
    _write_csv(root / "tables" / "collar_t1__p0003.csv", collar_rows)
    _write_csv(root / "tables" / "assay_t1__p0004.csv", assay_rows)

    manifest = {
        "doc_id": "mini",
        "report_slug": "mini_gold_report",
        "source_name": "Mini Gold Report.pdf",
        "sha256": "abc123",
        "stats": {"n_pages": 4, "n_sections": 1, "n_tables": 2, "n_figures": 1},
        "sections": [
            {
                "idx": 0,
                "number": "1",
                "title": "Summary",
                "page_start": 1,
                "page_end": 4,
                "slug": "01_summary",
                "text_file": "text/01_summary.md",
            }
        ],
        "tables": [
            {
                "id": "collar_t1__p0003",
                "page": 3,
                "section": "01_summary",
                "method": "digital_text",
                "n_rows": len(collar_rows),
                "n_cols": len(collar_rows[0]),
                "rotation_deg": 0,
                "text_layer_checksum": "",
                "confidence": "high",
                "csv": "tables/collar_t1__p0003.csv",
                "crop_png": None,
            },
            {
                "id": "assay_t1__p0004",
                "page": 4,
                "section": "01_summary",
                "method": "digital_text",
                "n_rows": len(assay_rows),
                "n_cols": len(assay_rows[0]),
                "rotation_deg": 0,
                "text_layer_checksum": "",
                "confidence": "high",
                "csv": "tables/assay_t1__p0004.csv",
                "crop_png": None,
            },
        ],
        "figures": [
            {
                "id": "fig_p0002_1",
                "page": 2,
                "image": "images/fig_p0002_1.jpg",
                "description_md": "images/fig_p0002_1.description.md",
            }
        ],
        "review_queue": [],
    }
    import json

    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def test_figure_classification_from_description_prose() -> None:
    geological = classify_figure_description(
        "This image is a **geological map** depicting a property with legend and UTM grid."
    )
    photo = classify_figure_description("This image is a **photo** of mineralized drill core.")
    logo = classify_figure_description("This image is a **logo or stylized text graphic**.")

    assert geological.figure_class == "geological_map"
    assert geological.confidence == "high"
    assert is_map_candidate(geological.figure_class)
    assert is_georeference_candidate(geological.figure_class)
    assert photo.figure_class == "photo"
    assert not is_map_candidate(photo.figure_class)
    assert logo.figure_class == "other"


def test_table_records_preserve_raw_tokens_and_guard_rejects_unbacked_numbers() -> None:
    rows = [
        ["Hole", "From (m)", "To (m)", "Interval (m)", "Au (g/t)"],
        ["NFG-001", "10", "20", "10", "2.5"],
    ]
    table = {"id": "assay_t1__p0004", "page": 4, "confidence": "high", "method": "digital_text"}
    mapping = MappedTable(
        table_type="assay_interval",
        columns=[
            ColumnMap(0, "hole_id"),
            ColumnMap(1, "from_m", "m"),
            ColumnMap(2, "to_m", "m"),
            ColumnMap(3, "interval_m", "m"),
            ColumnMap(4, "au_gpt", "g/t"),
        ],
        carry_fields=["hole_id"],
    )

    records, evidence = build_records(rows, mapping, table)
    assert records[0]["au_gpt"] == {
        "value": 2.5,
        "unit": "g/t",
        "raw_value": "2.5",
        "raw_unit": "g/t",
        "evidence_id": "ev:assay_t1__p0004:r1:au_gpt",
    }
    assert evidence["ev:assay_t1__p0004:r1:au_gpt"]["source_kind"] == "csv_token"

    tampered = dict(records[0])
    tampered["au_gpt"] = dict(records[0]["au_gpt"], raw_value="99.9", value=99.9)
    kept, rejected = guard_records(records + [tampered], rows, ungated=False)

    assert len(kept) == 1
    assert rejected[0]["_unbacked"] == ["99.9"]


def test_build_report_metadata_writes_hierarchy_and_location(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    metadata = build_report_metadata(root, force=True)

    analysis_root = root / "analysis"
    assert (analysis_root / "input_index.json").exists()
    assert (analysis_root / "report_metadata.json").exists()
    assert (analysis_root / "evidence_index.json").exists()

    assert metadata["stats"] == {"n_pages": 4, "n_sections": 1, "n_tables": 2, "n_figures": 1}
    assert metadata["project_stage"] == "target"
    assert metadata["elements"]["fig_p0002_1"]["figure_class"] == "geological_map"
    assert metadata["elements"]["fig_p0002_1"]["map_candidate"] is True

    hole = metadata["entities"]["hole:mg-001"]
    assert hole["attributes"]["collar"]["eoh_m"]["value"] == 150.0
    assert any(rel for rel in metadata["relationships"] if rel["from"] == "hole:mg-001" and rel["to"] == "zone:main")
    assert metadata["records"]["assay_t1__p0004:r1"]["entity_ids"] == ["hole:mg-001", "zone:main"]
    assert metadata["location_candidates"][0]["method"] == "text_latlon"


def test_compute_and_claim_verification(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    metadata = build_report_metadata(root, force=True)
    assays = [r for r in metadata["records"].values() if r["_type"] == "assay_interval"]
    metrics = intercepts(assays)

    assert metrics["top_intercepts"][0]["gram_metre"] == 25.0
    claim = "The report captures 10 m at 2.5 g/t Au."
    verdict = verify_claim(
        claim,
        [
            {"evidence_id": "ev:assay_t1__p0004:r1:interval_m"},
            {"evidence_id": "ev:assay_t1__p0004:r1:au_gpt"},
        ],
        metadata,
    )
    assert verdict["verified"] is True
    assert verdict["bad_numbers"] == []

    bad = verify_claim(claim.replace("2.5", "99.9"), [{"evidence_id": "ev:assay_t1__p0004:r1:au_gpt"}], metadata)
    assert bad["verified"] is False
    assert bad["bad_numbers"] == ["10", "99.9"]
    assert bad["tagged_text"].startswith("[UNVERIFIED]")

    qualitative = verify_claim("The project has meaningful exploration upside.", [], metadata)
    assert qualitative["verified"] is False

    metric_evidence = computed_metric_evidence({"intercepts": metrics})
    metadata["_evidence_index"].update(metric_evidence)
    metric_verdict = verify_claim(
        "The interval has 25.0 gram-metres.",
        [{"evidence_id": "ev:metric:intercepts:assay_t1__p0004:r1:gram_metre"}],
        metadata,
    )
    assert metric_verdict["verified"] is True


def test_cached_hie_preserves_evidence_for_claim_verification(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    first = build_report_metadata(root, force=True)
    second = build_report_metadata(root, force=False)

    assert len(first["_evidence_index"]) == len(second["_evidence_index"])
    verdict = verify_claim(
        "The report captures 10 m at 2.5 g/t Au.",
        [
            {"evidence_id": "ev:assay_t1__p0004:r1:interval_m"},
            {"evidence_id": "ev:assay_t1__p0004:r1:au_gpt"},
        ],
        second,
    )
    assert verdict["verified"] is True


def test_tool_dispatcher_loads_report_and_queries_records(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    build_report_metadata(root, force=True)

    tools = GeoReportTools(output_root=root.parent)
    names = {tool["name"] for tool in tools.list_tools()}
    assert {"report_load", "report_get_records", "report_verify_claim", "report_query_hierarchy"} <= names

    loaded = tools.call("report_load", {"slug": "mini_gold_report"})
    assert loaded["report_slug"] == "mini_gold_report"
    assert loaded["stats"]["n_tables"] == 2

    records = tools.call("report_get_records", {"slug": "mini_gold_report", "type": "assay_interval"})
    assert len(records["records"]) == 2

    hierarchy = tools.call("report_query_hierarchy", {"slug": "mini_gold_report", "level": "hole"})
    assert hierarchy["entities"][0]["id"] == "hole:mg-001"


def test_batch_run_writes_metrics_flags_findings_and_memo(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    result = run_analysis(root, force=True)

    assert result["report_slug"] == "mini_gold_report"
    assert (root / "analysis" / "metrics" / "intercepts.json").exists()
    assert (root / "analysis" / "flags.json").exists()
    assert (root / "analysis" / "findings.json").exists()
    assert (root / "analysis" / "memo.md").exists()
    assert result["findings"]["claims"][0]["validated"] is True
    assert result["findings"]["intercepts"][0]["gram_metre"] == 25.0


def test_stdio_mcp_handlers_list_and_call_tools(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    build_report_metadata(root, force=True)

    tools = GeoReportTools(output_root=root.parent)
    listed = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, tools)
    assert listed["id"] == 1
    assert any(tool["name"] == "report_load" for tool in listed["result"]["tools"])
    assert all("inputSchema" in tool for tool in listed["result"]["tools"])
    assert handle_request({"jsonrpc": "2.0", "id": 9, "method": "ping"}, tools)["result"] == {}

    called = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "report_load", "arguments": {"slug": "mini_gold_report"}},
        },
        tools,
    )
    assert called["result"]["structuredContent"]["report_slug"] == "mini_gold_report"
