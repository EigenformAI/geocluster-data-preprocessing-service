from __future__ import annotations

import csv
import json
import base64
from pathlib import Path

import pytest

from analysis.assertions import Assertion, classify_assertion_status
from analysis.claims import verify_claim
from analysis.ablation import run_ablation_harness
from analysis.compute import computed_metric_evidence, intercepts
from analysis.dki import TextRecallExpert, dki
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
    LLMExtractedSpan,
    LLMMappedTable,
    OpencodeBackend,
    select_table_mapper,
)
from analysis.mcp import handle_request
from analysis.mcp_tools import GeoReportTools
from analysis.reduce import build_entity_graph, derive_location_candidates
from analysis.run import run_analysis
from analysis.report_agent import validate_assertion_graph
from analysis.schemas import ColumnMap, EvidenceItem, FigureClassification, MappedTable, ReportArtifact
from analysis.spans import HeuristicSpanExtractor, LLMSpanExtractor, select_span_extractor
from analysis.tables import HeuristicTableMapper, build_records, guard_records
from analysis.taxonomy import item_for_section, matched_predicates, taxonomy_for_question
from analysis.textguard import text_backed, unbacked_spans
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


def _mini_legal_report(tmp_path: Path) -> Path:
    root = tmp_path / "output" / "mini_legal_report"
    (root / "text").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "tables").mkdir()

    (root / "text" / "04_property.md").write_text(
        "---\nsection: 4\ntitle: 4 Property Description and Location\npages: 4-4\n---\n\n"
        "# 4 Property Description and Location\n\n"
        "The project is within traditional territories of the Lac des Mille Lacs First Nation.\n\n"
        "The mineral claims are active and in good standing.\n"
    )
    manifest = {
        "doc_id": "mini-legal",
        "report_slug": "mini_legal_report",
        "source_name": "Mini Legal Report.pdf",
        "sha256": "def456",
        "stats": {"n_pages": 4, "n_sections": 1, "n_tables": 0, "n_figures": 0},
        "sections": [
            {
                "idx": 0,
                "number": "4",
                "title": "4 Property Description and Location",
                "page_start": 4,
                "page_end": 4,
                "slug": "4_property_description_and_location",
                "text_file": "text/04_property.md",
            }
        ],
        "tables": [],
        "figures": [],
        "review_queue": [],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def test_textguard_normalizes_whitespace_and_markdown_artifacts() -> None:
    source = "The project\n\n![img-1.jpeg](img-1.jpeg)\nrequires additional work."

    assert text_backed("The project requires additional work.", source) is True
    assert text_backed("The project is construction ready.", source) is False
    assert unbacked_spans(["The project requires additional work.", "Fabricated statement."], source) == ["Fabricated statement."]


def test_textguard_rejects_degenerate_spans_and_embedded_substrings() -> None:
    source = "The Cartesian model is discussed before Native Title consultation."

    assert text_backed("the", source) is False
    assert text_backed("is", source) is False
    assert text_backed("art", source) is False
    assert text_backed("___ ___", source) is False
    assert text_backed("native title", "The native titles process is unresolved.") is False
    assert text_backed("Native Title consultation", source) is True


def test_verify_claim_reopens_text_token_source_and_rejects_stale_span(tmp_path: Path) -> None:
    root = tmp_path / "report"
    (root / "text").mkdir(parents=True)
    (root / "text" / "section.md").write_text("The project requires additional consultation before drilling.\n")
    metadata = {
        "_report_root": str(root),
        "_evidence_index": {
            "ev:text_recall:section:q2:1": {
                "element_id": "section",
                "page": 4,
                "source_kind": "text_token",
                "source_ref": "text/section.md",
                "raw_token": "The project requires additional consultation before drilling.",
            }
        },
    }

    backed = verify_claim(
        "The project requires additional consultation before drilling.",
        [{"evidence_id": "ev:text_recall:section:q2:1"}],
        metadata,
    )
    assert backed["verified"] is True

    (root / "text" / "section.md").write_text("The project requires additional drilling before consultation begins.\n")
    stale_file = verify_claim(
        "The project requires additional consultation before drilling.",
        [{"evidence_id": "ev:text_recall:section:q2:1"}],
        metadata,
    )
    assert stale_file["verified"] is False
    assert stale_file["unbacked_text"][0]["span"] == "The project requires additional consultation before drilling."

    (root / "text" / "section.md").write_text("The project requires additional consultation before drilling.\n")
    metadata["_evidence_index"]["ev:text_recall:section:q2:1"]["raw_token"] = "The project is fully permitted."
    stale = verify_claim(
        "The project is fully permitted.",
        [{"evidence_id": "ev:text_recall:section:q2:1"}],
        metadata,
    )
    assert stale["verified"] is False
    assert stale["unbacked_text"][0]["span"] == "The project is fully permitted."


def test_verify_claim_rejects_text_token_without_reopenable_source(tmp_path: Path) -> None:
    root = tmp_path / "report"
    (root / "text").mkdir(parents=True)
    metadata = {
        "_evidence_index": {
            "ev:text_recall:section:q2:1": {
                "element_id": "section",
                "page": 4,
                "source_kind": "text_token",
                "source_ref": "text/missing.md",
                "raw_token": "The project requires consultation.",
            }
        },
    }

    no_root = verify_claim("The project requires consultation.", [{"evidence_id": "ev:text_recall:section:q2:1"}], metadata)
    metadata["_report_root"] = str(root)
    missing_file = verify_claim("The project requires consultation.", [{"evidence_id": "ev:text_recall:section:q2:1"}], metadata)

    assert no_root["verified"] is False
    assert no_root["unbacked_text"][0]["source_ref"] == "text/missing.md"
    assert missing_file["verified"] is False
    assert missing_file["unbacked_text"][0]["source_ref"] == "text/missing.md"


def test_verify_claim_rejects_qualitative_element_only_citation() -> None:
    metadata = {"elements": {"section-4": {"kind": "section"}}, "_evidence_index": {}}

    verdict = verify_claim(
        "The project has completed Native Title approvals.",
        [{"element_id": "section-4"}],
        metadata,
    )

    assert verdict["verified"] is False
    assert verdict["tagged_text"].startswith("[UNVERIFIED]")


def test_verify_claim_rejects_text_token_source_paths_outside_report_root(tmp_path: Path) -> None:
    root = tmp_path / "report"
    (root / "text").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("The project requires Native Title consultation.\n")
    metadata = {
        "_report_root": str(root),
        "_evidence_index": {
            "ev:text_recall:absolute:q2:1": {
                "element_id": "section",
                "page": 4,
                "source_kind": "text_token",
                "source_ref": str(outside),
                "raw_token": "The project requires Native Title consultation.",
            },
            "ev:text_recall:escape:q2:1": {
                "element_id": "section",
                "page": 4,
                "source_kind": "text_token",
                "source_ref": "../outside.md",
                "raw_token": "The project requires Native Title consultation.",
            },
        },
    }

    absolute = verify_claim("The project requires Native Title consultation.", [{"evidence_id": "ev:text_recall:absolute:q2:1"}], metadata)
    escaping = verify_claim("The project requires Native Title consultation.", [{"evidence_id": "ev:text_recall:escape:q2:1"}], metadata)

    assert absolute["verified"] is False
    assert escaping["verified"] is False
    assert absolute["unbacked_text"][0]["source_ref"] == str(outside)
    assert escaping["unbacked_text"][0]["source_ref"] == "../outside.md"


def test_taxonomy_maps_legal_question_and_section_items() -> None:
    taxonomy = taxonomy_for_question("Are there legal or jurisdictional risks, including native title or permits?")

    assert taxonomy is not None
    assert set(taxonomy["items"]) == {4, 20, 25}
    assert item_for_section({"number": "4", "title": "Property Description and Location"}) == 4
    assert item_for_section({"number": None, "title": "25 Interpretation and Conclusions"}) == 25
    assert item_for_section({"number": None, "title": None, "slug": "04_property_description"}) == 4


def test_legal_taxonomy_detects_australian_native_title_and_environment_terms() -> None:
    native_matches = matched_predicates(
        "legal_jurisdictional",
        "Native title matters may require an ILUA, an Indigenous Land Use Agreement, land council engagement, and heritage clearance.",
    )
    permit_matches = matched_predicates(
        "legal_jurisdictional",
        "The project requires an EIS/EIA, EPBC referral, mining lease approval, tailings approvals, closure and rehabilitation plan, and a water licence.",
    )

    assert {"native title", "ILUA", "Indigenous Land Use Agreement", "land council", "heritage"} <= set(native_matches["indigenous_or_consultation"])
    assert {"EIS", "EIA", "EPBC", "mining lease", "approval", "tailings", "closure", "rehabilitation", "licence"} <= set(
        permit_matches["permit_or_environmental"]
    )


def test_text_recall_expert_surfaces_real_gold_x2_prose_fact() -> None:
    root = Path(__file__).resolve().parents[1] / "output" / "gold_x2_mining"
    assert root.exists(), "gold_x2_mining fixture is required for text recall regression"
    metadata = json.loads((root / "analysis" / "report_metadata.json").read_text())
    metadata["_report_root"] = str(root)
    metadata["_evidence_index"] = json.loads((root / "analysis" / "evidence_index.json").read_text())

    injected = dki(
        "Are there legal or jurisdictional risks, including native title, First Nations consultation, or permits?",
        metadata,
        experts=[TextRecallExpert()],
    )
    bundle = injected["selected"]["text_recall"]
    item = next(item for item in bundle["items"] if "Lac des Mille Lacs" in item["text"])

    metadata["_evidence_index"].update(bundle["evidence"])
    verdict = verify_claim(item["text"], [{"evidence_id": item["evidence_id"]}], metadata)

    assert "First Nations" in item["text"]
    assert item["ni43101_item"] == 4
    assert verdict["verified"] is True


def test_text_recall_coverage_records_absent_predicates(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)
    metadata = build_report_metadata(root, force=True)
    injected = dki(
        "Are there legal or jurisdictional risks, including First Nations consultation and permits?",
        metadata,
        experts=[TextRecallExpert()],
    )

    coverage = injected["selected"]["text_recall"]["coverage"][0]
    assert coverage["items_covered"] == [4]
    assert "indigenous_or_consultation" not in coverage["predicates_absent"]
    assert "permit_or_environmental" not in coverage["predicates_absent"]
    assert "permit_or_environmental" in coverage["predicates_unassessed"]


def test_text_recall_emits_short_meaningful_legal_span(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)
    (root / "text" / "04_property.md").write_text(
        "---\nsection: 4\ntitle: 4 Property Description and Location\npages: 4-4\n---\n\n"
        "# 4 Property Description and Location\n\n"
        "Native Title held.\n"
    )
    metadata = build_report_metadata(root, force=True)

    injected = dki(
        "Are there legal or jurisdictional risks, including Native Title?",
        metadata,
        experts=[TextRecallExpert()],
    )

    assert any(item["text"] == "Native Title held." for item in injected["selected"]["text_recall"]["items"])


def test_text_recall_reports_absence_only_after_predicate_home_item_scanned(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)
    (root / "text" / "20_environment.md").write_text(
        "---\nsection: 20\ntitle: 20 Environmental Studies, Permitting and Social or Community Impact\npages: 20-20\n---\n\n"
        "# 20 Environmental Studies, Permitting and Social or Community Impact\n\n"
        "The report describes baseline community context and monitoring logistics.\n"
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sections"].append({
        "idx": 1,
        "number": "20",
        "title": "20 Environmental Studies, Permitting and Social or Community Impact",
        "page_start": 20,
        "page_end": 20,
        "slug": "20_environmental_studies_permitting_and_social_or_community_impact",
        "text_file": "text/20_environment.md",
    })
    manifest["stats"]["n_sections"] = len(manifest["sections"])
    manifest_path.write_text(json.dumps(manifest))

    metadata = build_report_metadata(root, force=True)
    injected = dki(
        "Are there legal or jurisdictional risks, including native title, First Nations consultation and permits?",
        metadata,
        experts=[TextRecallExpert()],
    )

    coverage = injected["selected"]["text_recall"]["coverage"][0]
    assert coverage["items_covered"] == [4, 20]
    assert "permit_or_environmental" in coverage["predicates_absent"]
    assert "permit_or_environmental" not in coverage["predicates_unassessed"]


def test_dki_routes_expert_group_by_question_class(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)
    metadata = build_report_metadata(root, force=True)

    injected = dki(
        "Are there legal or jurisdictional risks, including First Nations consultation and permits?",
        metadata,
    )

    assert injected["routing"]["question_class"] == "legal_jurisdictional"
    assert {"compliance", "legal_tenure", "environmental_permitting", "country_risk", "text_recall"} <= set(injected["selected"])
    assert "geospatial" not in injected["selected"]
    assert any(item["name"] == "geospatial" and item["reason"] == "route_not_selected" for item in injected["routing"]["experts_skipped"])


def test_dki_skips_text_recall_without_matching_question_class(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)
    metadata = build_report_metadata(root, force=True)

    injected = dki("Summarize the cover-image aesthetics.", metadata, experts=[TextRecallExpert()])

    assert injected["selected"] == {}
    assert injected["routing"]["question_class"] is None
    assert injected["routing"]["experts_skipped"] == [{"name": "text_recall", "reason": "route_not_selected"}]


def test_span_extractor_unions_llm_spans_and_drops_unbacked(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)
    (root / "text" / "04_property.md").write_text(
        "---\nsection: 4\ntitle: 4 Property Description and Location\npages: 4-4\n---\n\n"
        "# 4 Property Description and Location\n\n"
        "The project is within traditional territories of the Lac des Mille Lacs First Nation.\n\n"
        "Engagement with Lac des Mille Lacs remains ongoing.\n"
    )
    metadata = build_report_metadata(root, force=True)
    fake = FakeBackend(span_extractions={
        "4_property_description_and_location": [
            LLMExtractedSpan(
                span="Engagement with Lac des Mille Lacs remains ongoing.",
                predicate="indigenous_or_consultation",
                modality="planned",
                ni43101_item=4,
                confidence="high",
            ),
            LLMExtractedSpan(
                span="The project has completed all Native Title approvals.",
                predicate="indigenous_or_consultation",
                modality="done",
                ni43101_item=4,
                confidence="high",
            ),
        ]
    })
    expert = TextRecallExpert(span_extractor=LLMSpanExtractor(HeuristicSpanExtractor(), fake))

    injected = dki(
        "Are there legal or jurisdictional risks, including First Nations consultation?",
        metadata,
        experts=[expert],
    )
    bundle = injected["selected"]["text_recall"]

    assert any("traditional territories" in item["text"] for item in bundle["items"])
    assert any(item["text"] == "Engagement with Lac des Mille Lacs remains ongoing." for item in bundle["items"])
    assert not any("completed all Native Title approvals" in item["text"] for item in bundle["items"])
    assert bundle["telemetry"]["recall.llm_spans_added_over_lexical"] == 1
    assert bundle["telemetry"]["recall.spans_rejected_by_textguard"] == 1


def test_span_extractor_degrades_to_heuristic_floor_on_backend_error(tmp_path: Path) -> None:
    class FailingBackend(FakeBackend):
        def extract_spans(self, question_class: str, section: dict, source_text: str, taxonomy: dict) -> list[LLMExtractedSpan]:
            self.telemetry["n_calls"] += 1
            raise LLMBackendError("temporary outage")

    root = _mini_legal_report(tmp_path)
    metadata = build_report_metadata(root, force=True)
    expert = TextRecallExpert(span_extractor=LLMSpanExtractor(HeuristicSpanExtractor(), FailingBackend()))

    injected = dki(
        "Are there legal or jurisdictional risks, including First Nations consultation?",
        metadata,
        experts=[expert],
    )
    bundle = injected["selected"]["text_recall"]

    assert any("traditional territories" in item["text"] for item in bundle["items"])
    assert bundle["telemetry"]["recall.llm_degraded"] == 1


def test_span_extractor_selector_defaults_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANALYSIS_LLM_BACKEND", raising=False)

    extractor = select_span_extractor(tmp_path / "cache")

    assert isinstance(extractor, HeuristicSpanExtractor)


def test_run_analysis_merges_text_recall_evidence_and_claims(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)

    result = run_analysis(
        root,
        force=True,
        question="Are there legal or jurisdictional risks, including First Nations consultation and permits?",
    )

    text_claim = next(claim for claim in result["findings"]["claims"] if "traditional territories" in claim["text"])
    assert text_claim["validated"] is True
    assert text_claim["ni43101_item"] == 4
    assert text_claim["section_slug"] == "4_property_description_and_location"
    answer_payload = json.loads((root / "analysis" / "peqa" / "answers" / "report.json").read_text())
    assert not any("No text-recall evidence for permit_or_environmental" in limitation for limitation in answer_payload["answer"]["limitations"])
    assert any("did not assess permit_or_environmental" in limitation and "Items 20" in limitation for limitation in answer_payload["answer"]["limitations"])
    evidence = json.loads((root / "analysis" / "evidence_index.json").read_text())
    assert any(evidence_id.startswith("ev:text_recall:") for evidence_id in evidence)
    persisted_metadata = json.loads((root / "analysis" / "report_metadata.json").read_text())
    assert "_report_root" not in persisted_metadata


def test_run_analysis_writes_answer_pack_and_filters_unverified_claims(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANALYSIS_LLM_BACKEND", raising=False)
    root = _mini_legal_report(tmp_path)
    fabricated = "The project has completed all Native Title approvals."
    evidence_id = "ev:text_recall:fabricated:q2:1"

    def fake_dki(question: str, metadata: dict) -> dict:
        return {
            "question": question,
            "location": None,
            "routing": {"question_class": "legal_jurisdictional", "experts_considered": ["text_recall"], "experts_selected": ["text_recall"], "experts_skipped": []},
            "gathered": {},
            "selected": {
                "text_recall": {
                    "evidence_origin": "report",
                    "source_kind": "text_token",
                    "question_class": "legal_jurisdictional",
                    "items": [
                        {
                            "id": "text_recall:fabricated:1",
                            "evidence_id": evidence_id,
                            "text": fabricated,
                            "span": fabricated,
                            "predicate": "indigenous_or_consultation",
                            "citations": [{"evidence_id": evidence_id}],
                        }
                    ],
                    "evidence": {
                        evidence_id: {
                            "element_id": "4_property_description_and_location",
                            "page": 4,
                            "source_kind": "text_token",
                            "source_ref": "text/04_property.md",
                            "raw_token": fabricated,
                            "span": fabricated,
                        }
                    },
                    "coverage": [],
                    "warnings": [],
                }
            },
            "warnings": [],
            "counters": {"dki.n_queries": 1},
        }

    monkeypatch.setattr("analysis.run.dki", fake_dki)
    result = run_analysis(root, force=True, question="Are there legal or jurisdictional risks, including Native Title approvals?")
    answer_payload = json.loads((root / "analysis" / "peqa" / "answers" / "report.json").read_text())

    assert not any(fabricated in claim["text"] for claim in result["findings"]["claims"])
    assert any(claim["text"] == fabricated and claim["unbacked_text"] for claim in result["findings"]["unverified_claims"])
    assert answer_payload["answer_pack"]["schema_version"] == "peqa_answer_pack/v1"
    assert answer_payload["answer_pack"]["question_class"] == "legal_jurisdictional"


def test_run_analysis_marks_fabricated_text_recall_span_unverified(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ANALYSIS_LLM_BACKEND", raising=False)
    root = _mini_legal_report(tmp_path)
    fabricated = "The project has completed all Native Title approvals."
    evidence_id = "ev:text_recall:fabricated:q2:1"

    def fake_dki(question: str, metadata: dict) -> dict:
        return {
            "question": question,
            "location": None,
            "gathered": {},
            "selected": {
                "text_recall": {
                    "evidence_origin": "report",
                    "source_kind": "text_token",
                    "question_class": "legal_jurisdictional",
                    "items": [
                        {
                            "id": "text_recall:fabricated:1",
                            "evidence_id": evidence_id,
                            "text": fabricated,
                            "span": fabricated,
                            "predicate": "indigenous_or_consultation",
                            "citations": [{"evidence_id": evidence_id}],
                        }
                    ],
                    "evidence": {
                        evidence_id: {
                            "element_id": "4_property_description_and_location",
                            "page": 4,
                            "source_kind": "text_token",
                            "source_ref": "text/04_property.md",
                            "raw_token": fabricated,
                            "span": fabricated,
                        }
                    },
                    "coverage": [],
                    "warnings": [],
                }
            },
            "warnings": [],
        }

    monkeypatch.setattr("analysis.run.dki", fake_dki)
    result = run_analysis(root, force=True, question="Are there legal or jurisdictional risks, including Native Title approvals?")

    claim = next(claim for claim in result["findings"]["unverified_claims"] if fabricated in claim["text"])
    assert claim["validated"] is False
    assert claim["tagged_text"].startswith("[UNVERIFIED]")
    assert not any(claim["text"] == fabricated for claim in result["findings"]["claims"])


def test_mcp_loaded_metadata_can_verify_text_recall_claim(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)
    run_analysis(
        root,
        force=True,
        question="Are there legal or jurisdictional risks, including First Nations consultation?",
    )
    evidence = json.loads((root / "analysis" / "evidence_index.json").read_text())
    evidence_id, token = next(
        (evidence_id, item["raw_token"])
        for evidence_id, item in evidence.items()
        if evidence_id.startswith("ev:text_recall:") and "traditional territories" in item["raw_token"]
    )

    verdict = GeoReportTools(output_root=root.parent).call(
        "report_verify_claim",
        {"slug": root.name, "text": token, "citations": [{"evidence_id": evidence_id}]},
    )

    assert verdict["verified"] is True


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


def test_report_agent_contract_models_round_trip() -> None:
    evidence = EvidenceItem(
        id="ev:figure:main_map",
        element_id="fig_p0002_1",
        source_kind="map_component",
        raw_token="main_map",
        page=2,
        source_ref="analysis/hie/maps/fig_p0002_1/main_map.png",
        attributes={"bbox": [1, 2, 3, 4]},
    )
    assertion = {
        "id": "assert:figure:main_map",
        "predicate": "map_component_detected",
        "subject": "fig_p0002_1",
        "value": "main_map",
        "evidence_ids": [evidence.id],
        "source_kind": "map_component",
        "element_id": "fig_p0002_1",
    }
    artifact = ReportArtifact(
        doc_id="mini",
        report_slug="mini_gold_report",
        evidence_index={evidence.id: evidence.to_dict()},
        assertions=[assertion],
    )

    assert artifact.to_dict()["schema_version"] == "report_artifact/v1"
    assert artifact.to_dict()["assertions"][0]["evidence_ids"] == ["ev:figure:main_map"]


def test_report_agent_hie_writes_assertion_graph_and_validates(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    metadata = build_report_metadata(root, force=True)
    assertions = json.loads((root / "analysis" / "assertions.json").read_text())

    validate_assertion_graph(assertions, metadata, metadata["_evidence_index"])
    assert assertions["schema_version"] == "assertion_graph/v1"
    assert any(item["record_id"] == "assay_t1__p0004:r1" and item["predicate"] == "record_field" for item in assertions["assertions"])

    malformed = json.loads(json.dumps(assertions))
    malformed["assertions"][0]["evidence_ids"] = ["ev:missing"]
    with pytest.raises(ReportMetadataValidationError) as exc_info:
        validate_assertion_graph(malformed, metadata, metadata["_evidence_index"])
    assert any(issue["code"] == "ref.dangling_evidence_id" for issue in exc_info.value.issues)


def test_fake_map_processor_records_components_as_map_component_evidence(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)

    class FakeMapProcessor:
        name = "fake_peace"

        def process_map(self, report_root: Path, figure: dict, output_dir: Path) -> dict:
            return {
                "status": "processed",
                "processor": self.name,
                "components": [
                    {
                        "id": "main_map",
                        "type": "main_map",
                        "bbox": [10, 20, 300, 400],
                        "confidence": 0.97,
                        "crop_ref": "analysis/hie/maps/fig_p0002_1/main_map.png",
                    }
                ],
                "warnings": [],
                "georef": None,
            }

    metadata = build_report_metadata(root, force=True, map_processor=FakeMapProcessor())
    map_meta = metadata["elements"]["fig_p0002_1"]["map_meta"]
    evidence_id = map_meta["component_evidence_ids"][0]
    assertions = json.loads((root / "analysis" / "assertions.json").read_text())

    assert map_meta["status"] == "processed"
    assert map_meta["components"][0]["evidence_id"] == evidence_id
    assert metadata["_evidence_index"][evidence_id]["source_kind"] == "map_component"
    assert metadata["_evidence_index"][evidence_id]["raw_token"] == "main_map"
    assert (root / "analysis" / "hie" / "maps" / "fig_p0002_1" / "components.json").exists()
    assert metadata["build_trace"]["counters"]["hie.n_maps_processed"] == 1
    assert any(item["source_kind"] == "map_component" for item in assertions["assertions"])


def test_build_report_metadata_normalizes_numeric_table_sections(tmp_path: Path) -> None:
    root = _mini_report(tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for table in manifest["tables"]:
        table["section"] = "1"
    manifest_path.write_text(json.dumps(manifest))

    metadata = build_report_metadata(root, force=True)

    assert metadata["elements"]["assay_t1__p0004"]["section_slug"] == "01_summary"
    validate_report_metadata(metadata, metadata["_evidence_index"])


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

    dangling_evidence = json.loads(json.dumps(metadata))
    dangling_evidence["records"]["assay_t1__p0004:r1"]["_field_evidence"]["au_gpt"] = "ev:missing"
    with pytest.raises(ReportMetadataValidationError) as exc_info:
        validate_report_metadata(dangling_evidence, metadata["_evidence_index"])
    assert any(issue["code"] == "ref.dangling_evidence_id" for issue in exc_info.value.issues)

    bad_relationship = json.loads(json.dumps(metadata))
    bad_relationship["relationships"][0]["type"] = "touches"
    with pytest.raises(ReportMetadataValidationError) as exc_info:
        validate_report_metadata(bad_relationship, metadata["_evidence_index"])
    assert any(issue["path"].endswith("/type") and issue["code"] == "schema.enum" for issue in exc_info.value.issues)


def test_assertion_model_requires_grounded_span() -> None:
    assertion = Assertion(
        id="as:1",
        subject_entity_id="project:mini",
        predicate="indigenous_or_consultation",
        object_span="Native Title held.",
        modality="done",
        confidence="high",
        source_kind="knowledge_item",
        span="Native Title held.",
        evidence_id="ev:assertion:1",
        text_evidence_id="ev:text:1",
        element_id="section-4",
        page=4,
        section_slug="section-4",
        ni43101_item=4,
    )

    assert assertion.to_dict()["span"] == "Native Title held."
    with pytest.raises(ValueError, match="span"):
        Assertion(
            id="as:bad",
            subject_entity_id=None,
            predicate="indigenous_or_consultation",
            object_span="",
            modality="done",
            confidence="high",
            source_kind="knowledge_item",
            span="",
            evidence_id="ev:assertion:bad",
            text_evidence_id="ev:text:bad",
            element_id="section-4",
            page=4,
            section_slug="section-4",
            ni43101_item=4,
        )


def test_hie_harvest_writes_assertions_json_and_grounded_evidence(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)
    metadata = build_report_metadata(root, force=True)

    assertions = json.loads((root / "analysis" / "hierarchy" / "assertions.json").read_text())
    assertion = next(item for item in assertions if "traditional territories" in item["span"])
    evidence = metadata["_evidence_index"][assertion["evidence_id"]]
    text_evidence = metadata["_evidence_index"][assertion["text_evidence_id"]]
    verdict = verify_claim(assertion["span"], [{"evidence_id": assertion["evidence_id"]}], metadata)

    assert assertion["predicate"] == "indigenous_or_consultation"
    assert evidence["source_kind"] == "knowledge_item"
    assert evidence["input_evidence_ids"] == [assertion["text_evidence_id"]]
    assert text_evidence["source_kind"] == "text_token"
    assert verdict["verified"] is True
    assert metadata["build_trace"]["counters"]["harvest.n_assertions"] >= 1


def test_hie_harvest_drops_unbacked_llm_assertion_span(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)
    fake = FakeBackend(span_extractions={
        "4_property_description_and_location": [
            LLMExtractedSpan(
                span="The project has completed all Native Title approvals.",
                predicate="indigenous_or_consultation",
                modality="done",
                ni43101_item=4,
                confidence="high",
            )
        ]
    })

    metadata = build_report_metadata(
        root,
        force=True,
        span_extractor=LLMSpanExtractor(HeuristicSpanExtractor(), fake),
    )

    assert not any("completed all Native Title approvals" in item["span"] for item in metadata["assertions"])
    assert metadata["build_trace"]["counters"]["harvest.n_rejected_by_textguard"] >= 1


def test_assertion_query_and_status_classification(tmp_path: Path) -> None:
    root = _mini_legal_report(tmp_path)
    build_report_metadata(root, force=True)

    queried = GeoReportTools(output_root=root.parent).call(
        "report_query_assertions",
        {"slug": root.name, "predicate": "indigenous_or_consultation"},
    )

    assert any("traditional territories" in item["span"] for item in queried["assertions"])
    # An open risk dominates a coexisting mitigation: without risk<->mitigation
    # pairing we must not silently retire it (recall-safe; Open Question #4).
    assert classify_assertion_status([
        {"predicate": "geological_risk", "modality": "risk", "span": "A grade continuity risk remains."},
        {"predicate": "geological_risk", "modality": "done", "span": "Infill drilling mitigated grade continuity risk."},
    ], "geological_risk") == "open"
    # A mitigation with no coexisting open item retires the predicate.
    assert classify_assertion_status([
        {"predicate": "geological_risk", "modality": "done", "span": "Infill drilling closed the grade continuity gap."},
    ], "geological_risk") == "retired"
    assert classify_assertion_status([
        {"predicate": "geological_risk", "modality": "risk", "span": "A grade continuity risk remains."},
    ], "geological_risk") == "open"
    assert classify_assertion_status([], "data_verification") == "gap"


def test_hie_records_schema_validation_failure_counter_before_reraising(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _mini_report(tmp_path)

    def fail_validation(metadata: dict, evidence_index: dict) -> None:
        raise ReportMetadataValidationError([{"path": "/records", "code": "schema.required", "message": "boom"}])

    monkeypatch.setattr("analysis.hie.validate_report_metadata", fail_validation)

    with pytest.raises(ReportMetadataValidationError):
        build_report_metadata(root, force=True)

    state = json.loads((root / "analysis" / "analysis_state.json").read_text())
    assert state["hie"]["done"] is False
    assert state["hie"]["counters"]["schema.validation_passed"] == 0
    assert state["hie"]["counters"]["schema.validation_failed"] == 1


def test_entity_graph_emits_supports_and_drill_history_targets_from_records() -> None:
    records = {
        "assay:r1": {
            "_id": "assay:r1",
            "_type": "assay_interval",
            "hole_id": "MG-001",
            "zone": "Main",
            "au_gpt": {"value": 2.5},
            "provenance": {"element_id": "assay_t1", "confidence": "high"},
            "_field_evidence": {"hole_id": "ev:assay:hole", "zone": "ev:assay:zone", "au_gpt": "ev:assay:au"},
        },
        "resource:r1": {
            "_id": "resource:r1",
            "_type": "resource_estimate",
            "property": "Mini",
            "deposit": "Main",
            "tonnage": {"value": 1000000.0},
            "grade_gpt": {"value": 1.5},
            "provenance": {"element_id": "resource_t1", "confidence": "high"},
            "_field_evidence": {
                "property": "ev:resource:property",
                "deposit": "ev:resource:deposit",
                "tonnage": "ev:resource:tonnage",
                "grade_gpt": "ev:resource:grade",
            },
        },
        "drill_history:r1": {
            "_id": "drill_history:r1",
            "_type": "drill_history",
            "target_area": "North Main",
            "provenance": {"element_id": "drill_history_t1", "confidence": "high"},
            "_field_evidence": {"target_area": "ev:drill_history:target"},
        },
    }

    entities, relationships, unresolved = build_entity_graph(
        records,
        {"report_slug": "mini_gold_report", "source_name": "Mini Gold Report.pdf"},
    )

    assert "target:north-main" in entities
    assert records["drill_history:r1"]["entity_ids"] == ["target:north-main"]
    assert any(
        rel
        for rel in relationships
        if rel["type"] == "supports" and rel["from"] == "zone:main" and rel["to"] == "deposit:main"
    )
    assert any(
        rel for rel in relationships if rel["type"] == "part_of" and rel["from"] == "target:north-main"
    )
    assert unresolved == [
        {
            "record_id": "assay:r1",
            "entity_id": "hole:mg-001",
            "reason": "assay_interval hole_id has no matching collar",
        }
    ]


def test_derive_location_candidates_extracts_text_utm_and_nts() -> None:
    class ReportStub:
        sections = [{"slug": "04_property", "page_start": 4, "text_file": "text/04_property.md"}]

        def section_text(self, section: dict) -> str:
            return (
                "The claims cover NTS 52B10 and are centered near UTM zone 15U, NAD83, "
                "665424 m East and 5378951 m North."
            )

    candidates, evidence = derive_location_candidates(ReportStub(), {}, [])
    by_method = {candidate["method"]: candidate for candidate in candidates}

    assert by_method["text_utm"]["utm_zone"] == "15U"
    assert by_method["text_utm"]["datum"] == "NAD83"
    assert by_method["text_utm"]["easting"] == 665424.0
    assert by_method["text_utm"]["northing"] == 5378951.0
    assert by_method["text_nts"]["nts"] == "52B10"
    assert set(evidence) == {by_method["text_utm"]["evidence_id"], by_method["text_nts"]["evidence_id"]}


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
        {
            "id": "resource_t1__p0004",
            "page": 4,
            "section": "01_summary",
            "method": "digital_text",
            "n_rows": len(resource_rows),
            "n_cols": len(resource_rows[0]),
            "rotation_deg": 0,
            "text_layer_checksum": "",
            "confidence": "high",
            "csv": "tables/resource_t1__p0004.csv",
            "crop_png": None,
        },
        {
            "id": "drill_history_t1__p0004",
            "page": 4,
            "section": "01_summary",
            "method": "digital_text",
            "n_rows": len(drill_history_rows),
            "n_cols": len(drill_history_rows[0]),
            "rotation_deg": 0,
            "text_layer_checksum": "",
            "confidence": "high",
            "csv": "tables/drill_history_t1__p0004.csv",
            "crop_png": None,
        },
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
    manifest["tables"].append({
        "id": "unknown_t1__p0004",
        "page": 4,
        "section": "01_summary",
        "method": "digital_text",
        "n_rows": len(unknown_rows),
        "n_cols": len(unknown_rows[0]),
        "rotation_deg": 0,
        "text_layer_checksum": "",
        "confidence": "high",
        "csv": "tables/unknown_t1__p0004.csv",
        "crop_png": None,
    })
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


def test_selected_answerer_preserves_text_recall_floor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANALYSIS_LLM_BACKEND", "fake")
    root = _mini_legal_report(tmp_path)

    result = run_analysis(
        root,
        force=True,
        question="Are there legal or jurisdictional risks, including First Nations consultation?",
    )

    assert any("traditional territories" in claim["text"] and claim["validated"] for claim in result["findings"]["claims"])


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
    read = handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": image["uri"]}},
        tools,
    )

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
            columns=[
                ColumnMap(0, "hole_id"),
                ColumnMap(1, "from_m", "m"),
                ColumnMap(2, "to_m", "m"),
                ColumnMap(3, "au_gpt", "g/t"),
            ],
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


def _opencode_message_envelope(structured: dict) -> dict:
    """The real opencode 1.17 assistant-message envelope for a json_schema call.

    Structured output is enforced via a forced ``StructuredOutput`` tool call; the
    validated object lands at ``info.structured`` and is mirrored in the tool part.
    Reusable by callers that stub ``_post_json`` (e.g. the span-extractor test).
    """

    return {
        "info": {
            "structured": structured,
            "finish": "tool-calls",
            "cost": 0,
            "tokens": {"total": 12700, "input": 12680, "output": 20},
        },
        "parts": [
            {"type": "step-start"},
            {"type": "reasoning", "text": ""},
            {
                "type": "tool",
                "tool": "StructuredOutput",
                "callID": "call_x",
                "state": {"status": "completed", "input": structured},
            },
            {"type": "step-finish", "reason": "tool-calls"},
        ],
    }


def test_opencode_backend_parses_structured_response_and_caches(tmp_path: Path) -> None:
    calls: list[dict] = []
    structured = {
        "table_type": "assay_interval",
        "columns": [
            {"source_index": 0, "canonical_field": "hole_id", "unit": None},
            {"source_index": 1, "canonical_field": "from_m", "unit": "m"},
        ],
        "carry_fields": ["hole_id"],
    }

    class StubOpencodeBackend(OpencodeBackend):
        def _post_json(self, path: str, payload: dict) -> dict:
            calls.append({"path": path, "payload": payload})
            if path == "/session":
                return {"id": "ses_stub"}
            return _opencode_message_envelope(structured)

    backend = StubOpencodeBackend(
        cache_dir=tmp_path / "cache",
        server_url="http://127.0.0.1:4099",
        model="openai/gpt-5.5",
    )
    table = {"id": "t1", "csv": "tables/t1.csv", "source_sha256": "abc"}
    rows = [["Hole", "From"], ["A1", "10"]]

    first = backend.map_table(table, rows)
    second = backend.map_table(table, rows)

    assert first == second == MappedTable(
        "assay_interval",
        [ColumnMap(0, "hole_id", None), ColumnMap(1, "from_m", "m")],
        ["hole_id"],
    )
    # opencode 1.17: a session is created via POST /session, then the message targets it.
    assert calls[0]["path"] == "/session"
    assert calls[1]["path"] == "/session/ses_stub/message"
    message = calls[1]["payload"]
    assert message["model"] == {"providerID": "openai", "modelID": "gpt-5.5"}
    assert "messages" not in message
    assert message["system"]
    assert message["parts"][0]["type"] == "text"
    assert message["format"]["type"] == "json_schema"
    # Second call is served from cache: no extra session create or message POST.
    assert [c["path"] for c in calls] == ["/session", "/session/ses_stub/message"]
    assert backend.telemetry["n_cache_hits"] == 1


def test_opencode_backend_reads_structured_from_tool_part_without_info(tmp_path: Path) -> None:
    """If info.structured is absent, fall back to the StructuredOutput tool part."""

    class StubOpencodeBackend(OpencodeBackend):
        def _post_json(self, path: str, payload: dict) -> dict:
            if path == "/session":
                return {"id": "ses_tool"}
            return {
                "info": {"cost": 0},
                "parts": [
                    {"type": "reasoning", "text": ""},
                    {
                        "type": "tool",
                        "tool": "StructuredOutput",
                        "state": {
                            "status": "completed",
                            "input": {"figure_class": "claim_map", "confidence": "medium", "reason": "claims"},
                        },
                    },
                ],
            }

    backend = StubOpencodeBackend(cache_dir=tmp_path / "cache", server_url="http://x", model="openai/gpt-5.5")
    classification = backend.classify_figure({"id": "f1"}, "Claim map with tenement boundaries.")
    assert classification.figure_class == "claim_map"
    assert classification.confidence == "medium"


def test_opencode_backend_opens_isolated_session_per_call(tmp_path: Path) -> None:
    """Independent extractions must not share opencode conversation state."""
    session_creates: list[str] = []
    counter = {"n": 0}

    class StubOpencodeBackend(OpencodeBackend):
        def _post_json(self, path: str, payload: dict) -> dict:
            if path == "/session":
                counter["n"] += 1
                session_id = f"ses_{counter['n']}"
                session_creates.append(session_id)
                return {"id": session_id}
            return _opencode_message_envelope({"figure_class": "other", "confidence": "low", "reason": "r"})

    backend = StubOpencodeBackend(cache_dir=tmp_path / "cache", server_url="http://x", model="openai/gpt-5.5")
    backend.classify_figure({"id": "f1"}, "Figure one description.")
    backend.classify_figure({"id": "f2"}, "Figure two description.")
    assert session_creates == ["ses_1", "ses_2"]


def test_opencode_backend_reuses_pinned_session(tmp_path: Path) -> None:
    """A caller-pinned session is reused and never re-created."""
    paths: list[str] = []

    class StubOpencodeBackend(OpencodeBackend):
        def _post_json(self, path: str, payload: dict) -> dict:
            paths.append(path)
            return _opencode_message_envelope({"figure_class": "other", "confidence": "low", "reason": "r"})

    backend = StubOpencodeBackend(
        cache_dir=tmp_path / "cache",
        server_url="http://x",
        model="openai/gpt-5.5",
        session_id="ses_pinned",
    )
    backend.classify_figure({"id": "f1"}, "Figure one description.")
    assert paths == ["/session/ses_pinned/message"]
    assert "/session" not in paths


def test_opencode_backend_splits_provider_and_model() -> None:
    bare = OpencodeBackend(cache_dir="/tmp/x", server_url="http://x", model="gpt-5.5")
    qualified = OpencodeBackend(cache_dir="/tmp/x", server_url="http://x", model="openai/gpt-5.5")
    assert (bare.provider_id, bare.model_id) == ("openai", "gpt-5.5")
    assert (qualified.provider_id, qualified.model_id) == ("openai", "gpt-5.5")


def test_opencode_backend_uses_light_variant_by_default_and_heavy_for_answer(tmp_path: Path) -> None:
    seen: list[str | None] = []

    class StubOpencodeBackend(OpencodeBackend):
        def _post_json(self, path: str, payload: dict) -> dict:
            if path == "/session":
                return {"id": "ses_v"}
            seen.append(payload.get("variant"))
            if "figure" in payload.get("system", "").lower():
                return _opencode_message_envelope({"figure_class": "other", "confidence": "low", "reason": "r"})
            return _opencode_message_envelope({"question": "q", "answer": "a", "reason": "r", "claims": [], "limitations": []})

    backend = StubOpencodeBackend(cache_dir=tmp_path / "cache", server_url="http://x", model="openai/gpt-5.5")
    backend.classify_figure({"id": "f1"}, "A geological figure.")
    backend.answer("q", {"report_slug": "x"}, {}, {}, {"selected": {}})
    # mechanical extraction -> light variant; answer synthesis -> heavy variant
    assert seen == ["low", "medium"]


def test_opencode_backend_variant_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYSIS_LLM_VARIANT", "none")
    monkeypatch.setenv("ANALYSIS_LLM_HEAVY_VARIANT", "high")
    backend = OpencodeBackend(cache_dir=tmp_path / "cache", server_url="http://x", model="openai/gpt-5.5")
    assert (backend.variant, backend.heavy_variant) == ("none", "high")


def test_opencode_backend_extracts_spans_and_caches(tmp_path: Path) -> None:
    calls: list[dict] = []

    class StubOpencodeBackend(OpencodeBackend):
        def _post_json(self, path: str, payload: dict) -> dict:
            calls.append({"path": path, "payload": payload})
            return _opencode_message_envelope({
                "spans": [
                    {
                        "span": "Engagement with Lac des Mille Lacs remains ongoing.",
                        "predicate": "indigenous_or_consultation",
                        "modality": "planned",
                        "ni43101_item": 4,
                        "entity_hint": None,
                        "confidence": "high",
                    }
                ]
            })

    backend = StubOpencodeBackend(
        cache_dir=tmp_path / "cache",
        server_url="http://127.0.0.1:4099",
        model="openai/gpt-5.5",
        session_id="ses_spans",
    )
    section = {"slug": "4_property_description_and_location", "number": "4", "title": "Property"}
    taxonomy = taxonomy_for_question("Are there legal or jurisdictional risks?")
    source_text = "Engagement with Lac des Mille Lacs remains ongoing."

    first = backend.extract_spans("legal_jurisdictional", section, source_text, taxonomy)
    second = backend.extract_spans("legal_jurisdictional", section, source_text, taxonomy)

    assert first == second
    assert first[0].span == "Engagement with Lac des Mille Lacs remains ongoing."
    assert len(calls) == 1
    assert calls[0]["path"] == "/session/ses_spans/message"
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
    manifest["tables"].append({
        "id": "llm_assay",
        "page": 4,
        "section": "01_summary",
        "method": "digital_text",
        "n_rows": len(rows),
        "n_cols": len(rows[0]),
        "rotation_deg": 0,
        "text_layer_checksum": "",
        "confidence": "high",
        "csv": "tables/llm_assay.csv",
        "crop_png": None,
    })
    manifest["stats"]["n_tables"] = len(manifest["tables"])
    manifest_path.write_text(json.dumps(manifest))

    first = build_report_metadata(root, force=True)
    assert first["elements"]["llm_assay"]["table_type"] == "other"

    fake = FakeBackend(table_mappings={
        "llm_assay": MappedTable(
            table_type="assay_interval",
            columns=[
                ColumnMap(0, "hole_id"),
                ColumnMap(1, "from_m", "m"),
                ColumnMap(2, "to_m", "m"),
                ColumnMap(3, "au_gpt", "g/t"),
            ],
            carry_fields=["hole_id"],
        )
    })
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
    manifest["tables"].append({
        "id": "llm_assay",
        "page": 4,
        "section": "01_summary",
        "method": "digital_text",
        "n_rows": len(rows),
        "n_cols": len(rows[0]),
        "rotation_deg": 0,
        "text_layer_checksum": "",
        "confidence": "high",
        "csv": "tables/llm_assay.csv",
        "crop_png": None,
    })
    manifest["stats"]["n_tables"] = len(manifest["tables"])
    manifest_path.write_text(json.dumps(manifest))

    class FailingBackend(FakeBackend):
        def map_table(self, table: dict, rows: list[list[str]]) -> MappedTable:
            self.telemetry["n_calls"] += 1
            raise LLMBackendError("temporary outage")

    first = build_report_metadata(root, mapper=FallbackMapper(HeuristicTableMapper(), FailingBackend()), force=True)
    assert first["elements"]["llm_assay"]["table_type"] == "other"

    fake = FakeBackend(table_mappings={
        "llm_assay": MappedTable(
            table_type="assay_interval",
            columns=[
                ColumnMap(0, "hole_id"),
                ColumnMap(1, "from_m", "m"),
                ColumnMap(2, "to_m", "m"),
                ColumnMap(3, "au_gpt", "g/t"),
            ],
            carry_fields=["hole_id"],
        )
    })
    second = build_report_metadata(root, mapper=FallbackMapper(HeuristicTableMapper(), fake), force=False)

    assert second["elements"]["llm_assay"]["table_type"] == "assay_interval"
    assert fake.telemetry["n_calls"] == 1
