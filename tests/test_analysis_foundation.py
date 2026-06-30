from __future__ import annotations

import csv
import json
import base64
from pathlib import Path

import pytest

from analysis.claims import verify_claim
from analysis.compute import computed_metric_evidence, intercepts
from analysis.figures import classify_figure_description, is_georeference_candidate, is_map_candidate
from analysis.hie import build_report_metadata
from analysis.llm import (
    BackendConfigError,
    FakeBackend,
    FallbackFigureClassifier,
    FallbackMapper,
    HeuristicFigureClassifier,
    LLMBackendError,
    LLMColumnMap,
    LLMMappedTable,
    OpencodeBackend,
    select_table_mapper,
)
from analysis.mcp import handle_request
from analysis.mcp_tools import GeoReportTools
from analysis.run import run_analysis
from analysis.schemas import ColumnMap, FigureClassification, MappedTable
from analysis.tables import HeuristicTableMapper, build_records, guard_records
from analysis.validation import ReportMetadataValidationError, validate_report_metadata


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
    assert metadata["build_trace"]["counters"]["llm.n_calls"] == 0


def test_report_metadata_validation_rejects_malformed_ir(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    metadata = build_report_metadata(root, force=True)
    validate_report_metadata(metadata, metadata["_evidence_index"])

    missing_provenance = json.loads(json.dumps(metadata))
    del missing_provenance["records"]["assay_t1__p0004:r1"]["provenance"]
    with pytest.raises(ReportMetadataValidationError) as exc_info:
        validate_report_metadata(missing_provenance, metadata["_evidence_index"])
    assert exc_info.value.issues[0]["path"] == "/records/assay_t1__p0004:r1"

    evidence = json.loads(json.dumps(metadata["_evidence_index"]))
    evidence["ev:bad"] = {"element_id": "assay_t1__p0004", "source_kind": "invented", "raw_token": "x"}
    with pytest.raises(ReportMetadataValidationError) as exc_info:
        validate_report_metadata(metadata, evidence)
    assert any(issue["path"].endswith("/source_kind") and issue["code"] == "schema.enum" for issue in exc_info.value.issues)

    dangling = json.loads(json.dumps(metadata))
    dangling["records"]["assay_t1__p0004:r1"]["entity_ids"].append("hole:missing")
    with pytest.raises(ReportMetadataValidationError) as exc_info:
        validate_report_metadata(dangling, metadata["_evidence_index"])
    assert any(issue["code"] == "ref.dangling_entity_id" for issue in exc_info.value.issues)

    bad_relationship = json.loads(json.dumps(metadata))
    bad_relationship["relationships"][0]["type"] = "touches"
    with pytest.raises(ReportMetadataValidationError) as exc_info:
        validate_report_metadata(bad_relationship, metadata["_evidence_index"])
    assert any(issue["path"].endswith("/type") and issue["code"] == "schema.enum" for issue in exc_info.value.issues)


def test_contract_fixes_emit_utm_nts_supports_target_and_honest_dirs(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    (root / "text" / "01_summary.md").write_text(
        "The claims cover NTS 52B10 and are centered near UTM zone 15U, NAD83, "
        "665424 m East and 5378951 m North.\n"
    )
    resource_rows = [
        ["Property", "Deposit", "Resource Category", "Tonnage (t)", "Au Grade (g/t)", "Contained (koz)"],
        ["Mini", "Main", "Inferred", "1000000", "1.5", "48"],
    ]
    drill_history_rows = [
        ["Target Area", "Year", "Holes", "Metres Drilled"],
        ["North Main", "2024", "5", "750"],
    ]
    _write_csv(root / "tables" / "resource_t1__p0004.csv", resource_rows)
    _write_csv(root / "tables" / "drill_history_t1__p0004.csv", drill_history_rows)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tables"].extend([
        {"id": "resource_t1__p0004", "page": 4, "section": "01_summary", "method": "digital_text", "n_rows": len(resource_rows), "n_cols": len(resource_rows[0]), "rotation_deg": 0, "text_layer_checksum": "", "confidence": "high", "csv": "tables/resource_t1__p0004.csv", "crop_png": None},
        {"id": "drill_history_t1__p0004", "page": 4, "section": "01_summary", "method": "digital_text", "n_rows": len(drill_history_rows), "n_cols": len(drill_history_rows[0]), "rotation_deg": 0, "text_layer_checksum": "", "confidence": "high", "csv": "tables/drill_history_t1__p0004.csv", "crop_png": None},
    ])
    manifest["stats"]["n_tables"] = len(manifest["tables"])
    manifest_path.write_text(json.dumps(manifest))

    metadata = build_report_metadata(root, force=True)
    methods = {candidate["method"] for candidate in metadata["location_candidates"]}

    assert "text_utm" in methods
    assert "text_nts" in methods
    assert "target:north-main" in metadata["entities"]
    assert any(rel for rel in metadata["relationships"] if rel["type"] == "supports" and rel["to"] == "deposit:main")
    assert not (root / "analysis" / "hie" / "sections").exists()
    assert not (root / "analysis" / "peqa" / "prompts").exists()


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


def test_observability_state_and_coverage_aware_missing_qaqc(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    unknown_rows = [["Caption", "Value"], ["foo", "1"]]
    _write_csv(root / "tables" / "unknown_t1__p0004.csv", unknown_rows)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tables"].append({"id": "unknown_t1__p0004", "page": 4, "section": "01_summary", "method": "digital_text", "n_rows": len(unknown_rows), "n_cols": len(unknown_rows[0]), "rotation_deg": 0, "text_layer_checksum": "", "confidence": "high", "csv": "tables/unknown_t1__p0004.csv", "crop_png": None})
    manifest["stats"]["n_tables"] = len(manifest["tables"])
    manifest_path.write_text(json.dumps(manifest))

    result = run_analysis(root, force=True)
    counters = result["metadata"]["build_trace"]["counters"]
    state = json.loads((root / "analysis" / "analysis_state.json").read_text())
    missing_qaqc = next(flag for flag in result["flags"]["flags"] if flag["id"] == "missing_qaqc")

    assert "not detected" in missing_qaqc["message"]
    assert "coverage" in missing_qaqc["message"]
    assert "rule_version" not in missing_qaqc
    assert counters["guard.n_rejected"] == 0
    assert counters["hie.cache_misses"] == 3
    assert counters["schema.validation_passed"] == 1
    assert counters["vlm.n_calls"] == 0
    assert counters["georef.n_processed"] == 0
    assert set(state) >= {"input_index", "hie", "metrics", "flags", "dki", "peqa"}
    assert all("trace_id" in state[stage] for stage in ("input_index", "hie", "metrics", "flags", "dki", "peqa"))
    assert "report_metadata_sha256" in state["metrics"]["consumes"]
    assert state["dki"]["counters"]["dki.n_queries"] >= 1
    assert state["peqa"]["counters"]["peqa.n_claims"] == len(result["findings"]["claims"])


def test_batch_run_uses_selected_fake_answerer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANALYSIS_LLM_BACKEND", "fake")
    monkeypatch.setenv("ANALYSIS_LLM_MODEL", "test/model")
    root = _mini_report(tmp_path)

    result = run_analysis(root, force=True)

    assert result["findings"]["claims"] == []
    assert "Fake backend answer." in (root / "analysis" / "memo.md").read_text()


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


def test_mcp_resources_round_trip_and_redacts_source_ref(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    build_report_metadata(root, force=True)
    tools = GeoReportTools(output_root=root.parent)

    initialized = handle_request({"jsonrpc": "2.0", "id": 0, "method": "initialize"}, tools)
    image = tools.call("report_get_element_image", {"slug": root.name, "element_id": "fig_p0002_1"})
    listed = handle_request({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, tools)
    read = handle_request({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": image["uri"]}}, tools)

    assert "resources" in initialized["result"]["capabilities"]
    assert image == {"element_id": "fig_p0002_1", "uri": "geo-report://mini_gold_report/elements/fig_p0002_1/image", "mimeType": "image/jpeg"}
    assert image["uri"] in {resource["uri"] for resource in listed["result"]["resources"]}
    content = read["result"]["contents"][0]
    assert content["mimeType"] == "image/jpeg"
    assert base64.b64decode(content["blob"]) == b"fake-jpg"

def test_llm_boundary_models_validate_vocab() -> None:
    valid = LLMMappedTable(
        table_type="assay_interval",
        columns=[LLMColumnMap(source_index=0, canonical_field="hole_id")],
        carry_fields=["hole_id"],
    )
    assert valid.to_dataclass().table_type == "assay_interval"

    with pytest.raises(ValueError):
        LLMMappedTable(table_type="invented", columns=[])


def test_figure_fallback_classifier_is_wired_into_hie(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    (root / "images" / "fig_p0002_1.description.md").write_text("Ambiguous extracted visual description.\n")
    fake = FakeBackend(figure_classifications={
        "fig_p0002_1": FigureClassification("claim_map", "high", "fake structured classification")
    })

    metadata = build_report_metadata(
        root,
        force=True,
        figure_classifier=FallbackFigureClassifier(HeuristicFigureClassifier(), fake),
    )

    assert metadata["elements"]["fig_p0002_1"]["figure_class"] == "claim_map"
    assert fake.telemetry["n_calls"] == 1


def test_fallback_mapper_calls_fake_only_for_other_and_preserves_numguard() -> None:
    primary = HeuristicTableMapper()
    fake = FakeBackend(table_mappings={
        "llm_assay": MappedTable(
            table_type="assay_interval",
            columns=[ColumnMap(0, "hole_id"), ColumnMap(1, "from_m", "m"), ColumnMap(2, "to_m", "m"), ColumnMap(3, "au_gpt", "g/t")],
            carry_fields=["hole_id"],
        )
    })
    mapper = FallbackMapper(primary=primary, fallback=fake, max_calls=5)

    heuristic_rows = [["Hole", "Easting", "Northing"], ["A1", "500000", "5400000"]]
    assert mapper.map_table({"id": "collar"}, heuristic_rows).table_type == "drillhole_collar"
    assert fake.telemetry["n_calls"] == 0

    llm_rows = [["Drillhole", "Start", "Finish", "Gold"], ["A1", "10", "20", "2.5"]]
    mapping = mapper.map_table({"id": "llm_assay"}, llm_rows)
    records, _ = build_records(llm_rows, mapping, {"id": "llm_assay", "page": 1})
    tampered = dict(records[0])
    tampered["au_gpt"] = dict(records[0]["au_gpt"], raw_value="99.9", value=99.9)

    assert mapping.table_type == "assay_interval"
    assert fake.telemetry["n_calls"] == 1
    assert guard_records(records + [tampered], llm_rows)[1][0]["_unbacked"] == ["99.9"]


def test_table_mapper_selector_defaults_offline_and_passes_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANALYSIS_LLM_BACKEND", raising=False)
    mapper = select_table_mapper(cache_dir=tmp_path / "cache")
    assert isinstance(mapper, HeuristicTableMapper)

    monkeypatch.setenv("ANALYSIS_LLM_BACKEND", "fake")
    monkeypatch.setenv("ANALYSIS_LLM_MODEL", "test/model")
    mapper = select_table_mapper(cache_dir=tmp_path / "cache")
    assert isinstance(mapper, FallbackMapper)
    assert mapper.fallback.model == "test/model"


def test_opencode_backend_requires_server_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENCODE_SERVER_URL", raising=False)
    with pytest.raises(BackendConfigError, match="OPENCODE_SERVER_URL"):
        OpencodeBackend(cache_dir=tmp_path / "cache")


def test_opencode_backend_parses_structured_response_and_caches(tmp_path: Path) -> None:
    calls: list[dict] = []

    class StubOpencodeBackend(OpencodeBackend):
        def _post_json(self, path: str, payload: dict) -> dict:
            calls.append({"path": path, "payload": payload})
            return {"content": {"table_type": "assay_interval", "columns": [{"source_index": 0, "canonical_field": "hole_id", "unit": None}, {"source_index": 1, "canonical_field": "from_m", "unit": "m"}], "carry_fields": ["hole_id"]}}

    backend = StubOpencodeBackend(cache_dir=tmp_path / "cache", server_url="http://127.0.0.1:4099", model="openai/gpt-5.5")
    table = {"id": "t1", "csv": "tables/t1.csv", "source_sha256": "abc"}
    rows = [["Hole", "From"], ["A1", "10"]]

    first = backend.map_table(table, rows)
    second = backend.map_table(table, rows)

    assert first == second == MappedTable("assay_interval", [ColumnMap(0, "hole_id", None), ColumnMap(1, "from_m", "m")], ["hole_id"])
    assert len(calls) == 1
    assert calls[0]["path"].startswith("/session/")
    assert calls[0]["payload"]["model"] == "openai/gpt-5.5"
    assert calls[0]["payload"]["format"]["type"] == "json_schema"
    assert backend.telemetry["n_cache_hits"] == 1


def test_fallback_mapper_degrades_on_backend_error() -> None:
    class FailingBackend(FakeBackend):
        def map_table(self, table: dict, rows: list[list[str]]) -> MappedTable:
            self.telemetry["n_calls"] += 1
            raise LLMBackendError("boom")

    mapper = FallbackMapper(primary=HeuristicTableMapper(), fallback=FailingBackend(), max_calls=5)
    mapping = mapper.map_table({"id": "unknown"}, [["Sample", "Value"], ["A", "1"]])

    assert mapping.table_type == "other"
    assert mapper.telemetry["n_degraded"] == 1


def test_hie_cache_invalidates_when_mapper_identity_changes(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    rows = [["Drillhole", "Start", "Finish", "Gold"], ["A1", "10", "20", "2.5"]]
    _write_csv(root / "tables" / "llm_assay.csv", rows)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tables"].append({"id": "llm_assay", "page": 4, "section": "01_summary", "method": "digital_text", "n_rows": len(rows), "n_cols": len(rows[0]), "rotation_deg": 0, "text_layer_checksum": "", "confidence": "high", "csv": "tables/llm_assay.csv", "crop_png": None})
    manifest["stats"]["n_tables"] = len(manifest["tables"])
    manifest_path.write_text(json.dumps(manifest))

    first = build_report_metadata(root, force=True)
    assert first["elements"]["llm_assay"]["table_type"] == "other"

    fake = FakeBackend(table_mappings={"llm_assay": MappedTable("assay_interval", [ColumnMap(0, "hole_id"), ColumnMap(1, "from_m", "m"), ColumnMap(2, "to_m", "m"), ColumnMap(3, "au_gpt", "g/t")], ["hole_id"])})
    second = build_report_metadata(root, mapper=FallbackMapper(HeuristicTableMapper(), fake), force=False)

    assert second["elements"]["llm_assay"]["table_type"] == "assay_interval"
    assert fake.telemetry["n_calls"] == 1
    assert second["build_trace"]["counters"]["llm.n_calls"] == 1
    assert second["build_trace"]["counters"]["hie.mapper_coverage"] == 1


def test_hie_retries_after_degraded_fallback_cache_entry(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    rows = [["Drillhole", "Start", "Finish", "Gold"], ["A1", "10", "20", "2.5"]]
    _write_csv(root / "tables" / "llm_assay.csv", rows)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["tables"].append({"id": "llm_assay", "page": 4, "section": "01_summary", "method": "digital_text", "n_rows": len(rows), "n_cols": len(rows[0]), "rotation_deg": 0, "text_layer_checksum": "", "confidence": "high", "csv": "tables/llm_assay.csv", "crop_png": None})
    manifest["stats"]["n_tables"] = len(manifest["tables"])
    manifest_path.write_text(json.dumps(manifest))

    class FailingBackend(FakeBackend):
        def map_table(self, table: dict, rows: list[list[str]]) -> MappedTable:
            self.telemetry["n_calls"] += 1
            raise LLMBackendError("temporary outage")

    first = build_report_metadata(root, mapper=FallbackMapper(HeuristicTableMapper(), FailingBackend()), force=True)
    assert first["elements"]["llm_assay"]["table_type"] == "other"

    fake = FakeBackend(table_mappings={"llm_assay": MappedTable("assay_interval", [ColumnMap(0, "hole_id"), ColumnMap(1, "from_m", "m"), ColumnMap(2, "to_m", "m"), ColumnMap(3, "au_gpt", "g/t")], ["hole_id"])})
    second = build_report_metadata(root, mapper=FallbackMapper(HeuristicTableMapper(), fake), force=False)

    assert second["elements"]["llm_assay"]["table_type"] == "assay_interval"
    assert fake.telemetry["n_calls"] == 1
