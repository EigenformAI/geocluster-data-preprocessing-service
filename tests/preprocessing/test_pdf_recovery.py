import hashlib
import json
import shutil

import pytest

from preprocessing.contracts import default_profile
from preprocessing.errors import ServiceError
from preprocessing.formats.pdf import PDFAdapter, Renderer
from preprocessing.formats.pdf_recovery import PDFRecovery
from preprocessing.formats.pdf_tables import candidate_from_rows, validate_visual
from preprocessing.results import validate_table_evidence

ROWS = [["Code", "Value"], ["001", "1.2300"]]


def pdf_source(tmp_path, pages=1, image=False):
    from reportlab.pdfgen import canvas
    path = tmp_path / "source.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(300, 400))
    for _ in range(pages):
        pdf.drawString(20, 370, "Native prose")
        if image:
            from PIL import Image
            from reportlab.lib.utils import ImageReader
            pdf.drawImage(ImageReader(Image.new("RGB", (100, 40), "white")), 20, 200, 100, 40)
        else:
            for x in (20, 120, 220):
                pdf.line(x, 250, x, 330)
            for y in (250, 290, 330):
                pdf.line(20, y, 220, y)
            for r, row in enumerate(ROWS):
                for c, value in enumerate(row):
                    pdf.drawString(25 + 100 * c, 305 - 40 * r, value)
        pdf.showPage()
    pdf.save()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def visual(rows=ROWS):
    return json.dumps({"tablePresent": True, "rowCount": len(rows), "columnCount": len(rows[0]),
                       "cells": [{"row": r, "column": c, "text": value}
                                 for r, row in enumerate(rows, 1) for c, value in enumerate(row, 1)]})


class Context:
    resume = None

    def __init__(self, responses=None):
        self.calls = []
        self.responses = iter(responses or [])
        self.remaining = 1000

    def work_remaining(self):
        return self.remaining

    def model(self, operation, refs):
        self.calls.append(operation)
        return {"text": next(self.responses, visual()), "requestedModel": "fixture", "returnedModel": "fixture"}


def extract(snapshot, output, context, **options):
    profile = default_profile()
    profile["options"]["figureDescriptions"] = False
    profile["options"]["tableRecovery"].update(options)
    result = PDFAdapter().extract(snapshot, profile, output, context)
    validate_table_evidence(output, result)
    assert len({p["path"] for p in result["products"]}) == len(result["products"])
    plan = json.loads((output / "work-plan.json").read_text())
    units = {u["id"]: u for u in plan["units"]}
    assert all(units[d]["state"] == "completed" for u in units.values() if u["state"] == "completed" for d in u["dependencies"])
    return result


def test_default_native_confirmation_is_independent_and_retains_evidence(tmp_path):
    context = Context()
    output = tmp_path / "products"
    result = extract(pdf_source(tmp_path), output, context)
    reads = [call for call in context.calls if call["kind"] == "table-confirmation"]
    assert len(reads) == 1
    assert "001" not in reads[0]["prompt"] and "1.2300" not in reads[0]["prompt"]
    table = result["tables"][0]
    assert table["confirmation"] == {"state": "agreed", "basis": "native-vision"}
    words = json.loads((output / "text/page_0001.json").read_text())["words"]
    assert len({word["id"] for word in words}) == len(words)
    assert any(p["kind"] == "work-plan" for p in result["products"])
    from preprocessing.indexes import build_indexes
    from preprocessing.results import make_manifest
    profile = default_profile()
    build_indexes(result, output, profile["limits"])
    manifest = make_manifest({"id": "job_fixture", "profile": profile, "source": {"filename": "source.pdf"}}, result, output)
    assert manifest["outcome"] == "succeeded"


def test_first_confirmations_precede_dispute_sweep_and_cap_keeps_pending(tmp_path):
    context = Context([visual([["Code", "Value"], ["001", "-1.2300"]]), visual()])
    result = extract(pdf_source(tmp_path, pages=2), tmp_path / "products", context, maxModelCalls=2)
    assert [(c["kind"], c["sourceUnit"]["page"]) for c in context.calls] == [
        ("table-confirmation", 1), ("table-confirmation", 2)]
    assert [t["confirmation"]["state"] for t in result["tables"]] == ["unresolved", "agreed"]
    assert next(s for s in result["stages"] if s["id"] == "table-confirmation")["state"] == "partial"


def test_small_image_on_prose_page_is_scouted_then_read_twice(tmp_path):
    scout = json.dumps({"tables": [{"bounds": [60, 380, 420, 520], "orientation": 0}]})
    context = Context([scout, visual(), visual(), "figure text"])
    result = extract(pdf_source(tmp_path, image=True), tmp_path / "products", context, maxAdditionalPasses=0)
    kinds = [c["kind"] for c in context.calls]
    assert kinds[:3] == ["table-scout", "table-extraction", "table-confirmation"]
    assert result["tables"][0]["confirmation"] == {"state": "agreed", "basis": "repeat-vision"}


def test_resume_completed_page_reuses_tables_without_model_work(tmp_path):
    snapshot = pdf_source(tmp_path)
    first_output = tmp_path / "first"
    first = extract(snapshot, first_output, Context())
    plan = json.loads((first_output / "work-plan.json").read_text())
    second_output = tmp_path / "second"
    shutil.copytree(first_output, second_output)
    context = Context()
    progress = []
    context.progress = lambda *sample: progress.append(sample)
    context.resume = {"previousExtraction": first, "products": first["products"], "workPlan": plan,
                      "completedUnits": [u for u in plan["units"] if u["state"] == "completed"],
                      "reusedFromJob": "prior", "forcePages": [], "invalidatedUnitIds": []}
    second = extract(snapshot, second_output, context)
    assert context.calls == []
    assert second["tables"] == first["tables"]
    assert second["coverage"]["completed"] == 1
    assert [sample for sample in progress if sample[0] == 'extract'] == [('extract', 0, 1), ('extract', 1, 1)]
    new_plan = json.loads((second_output / "work-plan.json").read_text())
    assert all(u.get("reusedFromJob") == "prior" for u in new_plan["units"] if u["state"] == "completed")


def test_soft_cutoff_keeps_unvisited_pages_and_pending_work(tmp_path):
    context = Context()
    progress = []
    context.progress = lambda *sample: progress.append(sample)
    def model(operation, refs):
        context.calls.append(operation)
        context.remaining = 0
        return {"text": visual()}
    context.model = model
    output = tmp_path / "products"
    result = extract(pdf_source(tmp_path, pages=2), output, context)
    assert len(context.calls) == 1
    assert result["coverage"]["notProcessed"] == 1
    assert [sample for sample in progress if sample[0] == 'extract'] == [('extract', 0, 2), ('extract', 1, 2)]
    plan = json.loads((output / "work-plan.json").read_text())
    assert any(u["source"]["page"] == 2 and u["state"] == "pending" for u in plan["units"])


def test_region_transform_matches_pdfium_fractional_crop_conversion(tmp_path):
    import pypdfium2
    snapshot = pdf_source(tmp_path)
    box = [20.17, 70.37, 219.82, 150.29]
    renderer = Renderer(snapshot["path"], 16_000_000)
    try:
        metadata = renderer.render_region(0, tmp_path / "fractional.png", box, dpi=300)
    finally:
        renderer.close()
    assert metadata["pixelsPerPoint"] == 300 / 72
    assert metadata["width"] < 850 and metadata["height"] < 350
    with pypdfium2.PdfDocument(snapshot["path"]) as document:
        page = document[0]
        try:
            page.set_cropbox(*page.get_mediabox())
            bitmap = page.render(scale=300 / 72, crop=(box[0], 400 - box[3], 300 - box[2], box[1]))
            try:
                assert (metadata["width"], metadata["height"]) == (bitmap.width, bitmap.height)
                px, py = bitmap.width // 3, bitmap.height // 2
                pdf_x, pdf_y = bitmap.get_posconv(page).to_page(px, py)
            finally:
                bitmap.close()
        finally:
            page.close()
    a, b, c, d, e, f = metadata["imageToPage"]
    # PDFium rounds native coordinate conversions to float precision; the
    # affine interpolation must still match within a tiny fraction of a pixel.
    assert [a * px + c * py + e, b * px + d * py + f] == pytest.approx([pdf_x, 400 - pdf_y], abs=2e-5)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_region_maps_translated_media_and_intrinsic_rotation_despite_crop_box(tmp_path, rotation):
    import pypdfium2
    from PIL import Image
    from reportlab.pdfgen import canvas
    original = tmp_path / "translated.pdf"
    pdf = canvas.Canvas(str(original), pagesize=(400, 500))
    pdf.setFillColorRGB(1, 0, 0)
    pdf.rect(70, 380, 20, 10, fill=1, stroke=0)
    pdf.showPage()
    pdf.save()
    source = tmp_path / "rotated-media.pdf"
    with pypdfium2.PdfDocument(str(original)) as document:
        page = document[0]
        try:
            page.set_mediabox(50, 60, 350, 460)
            page.set_cropbox(100, 100, 200, 300)
            page.set_rotation(rotation)
        finally:
            page.close()
        document.save(str(source))
    x, y = {0: (30, 75), 90: (325, 30), 180: (270, 325), 270: (75, 270)}[rotation]
    target = tmp_path / "marker.png"
    renderer = Renderer(str(source), 16_000_000)
    try:
        metadata = renderer.render_region(0, target, [x - 10, y - 10, x + 20, y + 20], orientation=90)
    finally:
        renderer.close()
    a, b, c, d, e, f = metadata["pageToImage"]
    px, py = a * x + c * y + e, b * x + d * y + f
    with Image.open(target) as image:
        red, green, blue = image.convert("RGB").getpixel((round(px), round(py)))
    assert red > 240 and green < 20 and blue < 20
    a, b, c, d, e, f = metadata["imageToPage"]
    assert [a * px + c * py + e, b * px + d * py + f] == pytest.approx([x, y])
    with Image.open(target.with_name(metadata["sourceImage"])) as image:
        assert image.size == (metadata["sourceRender"]["width"], metadata["sourceRender"]["height"])


def resume_context(first, plan, output, previous_output):
    shutil.copytree(previous_output, output)
    context = Context()
    context.resume = {"previousExtraction": first, "products": first["products"], "workPlan": plan,
                      "completedUnits": [u for u in plan["units"] if u["state"] == "completed"],
                      "reusedFromJob": "prior", "forcePages": [], "invalidatedUnitIds": []}
    return context


def test_partial_resume_finishes_pending_page_without_reconfirming_agreed_table(tmp_path):
    snapshot = pdf_source(tmp_path, pages=2)
    first_output = tmp_path / "first"
    first = extract(snapshot, first_output, Context(), maxModelCalls=1)
    assert [t["confirmation"]["state"] for t in first["tables"]] == ["agreed", "pending"]
    plan = json.loads((first_output / "work-plan.json").read_text())
    output = tmp_path / "second"
    context = resume_context(first, plan, output, first_output)
    second = extract(snapshot, output, context, maxModelCalls=2)
    assert [(c["kind"], c["sourceUnit"]["page"]) for c in context.calls] == [("table-confirmation", 2)]
    assert [t["confirmation"]["state"] for t in second["tables"]] == ["agreed", "agreed"]
    assert second["coverage"]["completed"] == 2


def test_resume_does_not_reset_spent_dispute_allowance(tmp_path):
    snapshot = pdf_source(tmp_path)
    first_output = tmp_path / "first"
    wrong = visual([["Code", "Value"], ["001", "-1.2300"]])
    different = visual([["Code", "Value"], ["001", "2.2300"]])
    first = extract(snapshot, first_output, Context([wrong, different]))
    assert first["tables"][0]["confirmation"]["state"] == "unresolved"
    plan = json.loads((first_output / "work-plan.json").read_text())
    assert next(u for u in plan["units"] if u["kind"] == "table-confirmation")["passesSpent"] == 1
    output = tmp_path / "second"
    context = resume_context(first, plan, output, first_output)
    second = extract(snapshot, output, context)
    assert context.calls == []
    assert second["tables"][0]["confirmation"]["state"] == "unresolved"


def test_invalid_visual_response_is_raw_evidence_and_native_fallback(tmp_path):
    output = tmp_path / "products"
    result = extract(pdf_source(tmp_path), output, Context(["not JSON"]))
    table = result["tables"][0]
    assert table["confirmation"]["state"] == "failed"
    assert table["rows"] == 2
    raw = next(p for p in result["products"] if p["kind"] == "table-model-response")
    assert (output / raw["path"]).read_text() == "not JSON"


def test_vision_opt_out_has_no_table_calls_and_explicit_states(tmp_path):
    context = Context()
    result = extract(pdf_source(tmp_path), tmp_path / "products", context, vision="off", geometry="off")
    assert context.calls == []
    assert result["tables"][0]["confirmation"]["state"] == "not_checked"
    assert all(s["state"] == "not_applicable" for s in result["stages"] if s["id"].startswith("table-"))


def test_rotated_region_retains_original_and_maps_visible_marker(tmp_path):
    from PIL import Image
    from reportlab.pdfgen import canvas
    path = tmp_path / "marker.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(300, 400))
    pdf.setFillColorRGB(1, 0, 0)
    pdf.rect(20, 320, 20, 10, fill=1, stroke=0)
    pdf.showPage()
    pdf.save()
    renderer = Renderer(str(path), 16_000_000)
    target = tmp_path / "upright.png"
    try:
        metadata = renderer.render_region(0, target, [20, 70, 220, 150], dpi=300, orientation=90)
    finally:
        renderer.close()
    assert (tmp_path / metadata["sourceImage"]).is_file()
    a, b, c, d, e, f = metadata["pageToImage"]
    x, y = a * 30 + c * 75 + e, b * 30 + d * 75 + f
    with Image.open(target) as image:
        red, green, blue = image.convert("RGB").getpixel((round(x), round(y)))
    assert red > 240 and green < 20 and blue < 20
    a, b, c, d, e, f = metadata["imageToPage"]
    assert abs(a * x + c * y + e - 30) < 0.01
    assert abs(b * x + d * y + f - 75) < 0.01


def test_tiled_confirmation_reconciles_clipped_rows_and_resumes_completed_tiles(tmp_path):
    snapshot = pdf_source(tmp_path)
    profile = default_profile()
    profile["limits"]["pixels"] = 230_000
    profile["options"]["figureDescriptions"] = False
    profile["options"]["tableRecovery"]["maxModelCalls"] = 1

    class TileContext(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            x0, top, x1, bottom = operation["sourceUnit"]["bounds"]
            rows, edges = [], []
            for index, (start, end) in enumerate(((70, 110), (110, 150))):
                if max(top, start) < min(bottom, end):
                    rows.append(ROWS[index])
                    if not edges:
                        edges.append(max(top, start))
                    edges.append(min(bottom, end))
            response = json.loads(visual(rows))
            response["rowEdges"] = [(edge - top) / (bottom - top) * 1000 for edge in edges]
            response["columnEdges"] = [(edge - x0) / (x1 - x0) * 1000 for edge in (20, 120, 220)]
            return {"text": json.dumps(response)}

    first_output = tmp_path / "first"
    first = PDFAdapter().extract(snapshot, profile, first_output, TileContext())
    validate_table_evidence(first_output, first)
    assert first["tables"][0]["confirmation"]["state"] == "pending"
    plan = json.loads((first_output / "work-plan.json").read_text())
    assert sum(u["kind"] == "table-read-tile" and u["state"] == "completed" for u in plan["units"]) == 1
    output = tmp_path / "second"
    context = TileContext()
    context.resume = resume_context(first, plan, output, first_output).resume
    profile["options"]["tableRecovery"]["maxModelCalls"] = None
    second = PDFAdapter().extract(snapshot, profile, output, context)
    validate_table_evidence(output, second)
    assert second["tables"][0]["confirmation"]["state"] == "agreed", next(u.get("seamIssues") for u in json.loads((output / "work-plan.json").read_text())["units"] if u["kind"] == "table-confirmation")
    assert second["tables"][0]["rows"] == 2
    resumed = json.loads((output / "work-plan.json").read_text())
    tile_units = [u for u in resumed["units"] if u["kind"] == "table-read-tile"]
    assert len(context.calls) == len(tile_units) - 1
    assert any(u.get("reusedFromJob") == "prior" for u in tile_units)


def test_raw_agreement_cannot_conceal_unresolved_word_geometry(tmp_path, monkeypatch):
    from pdfplumber.page import Page
    monkeypatch.setattr(Page, "extract_words", lambda self, **kwargs: [])
    result = extract(pdf_source(tmp_path), tmp_path / "products", Context(), maxModelCalls=2)
    table = result["tables"][0]
    assert table["recovery"]["state"] == "unresolved"
    assert table["confirmation"]["state"] == "unresolved"
    assert next(s for s in result["stages"] if s["id"] == "table-confirmation")["state"] == "partial"


def test_incomplete_aggregate_is_not_a_second_whole_table_read(tmp_path):
    snapshot = pdf_source(tmp_path)
    profile = default_profile()
    result = {"products": [], "provenance": {}}
    recovery = PDFRecovery(snapshot, profile, tmp_path, Context(), result, None, profile["limits"],
                           adapter_version=PDFAdapter.version)
    full = candidate_from_rows("native", 1, [20, 70, 220, 190], ROWS + [["002", "2.0000"]], "native-text-layer")
    partial = validate_visual(visual(), "partial", 1, full["bounds"])["candidate"]
    partial.update(method="tile-aggregation", transcriptScope={"kind": "aggregate", "complete": False},
                   aggregationIssues=[{"kind": "missing-tile"}])
    fresh = validate_visual(visual(), "fresh", 1, full["bounds"])["candidate"]
    fresh["transcriptScope"] = {"kind": "region", "complete": True}
    item = {"page": 1, "selected": full, "candidates": [full, partial, fresh], "comparisons": [],
            "recovery": {"state": "unchanged"}, "weakProposal": False, "ambiguity": [],
            "orientation": 0, "pageSize": [300, 400], "unit": {"state": "unresolved"}}
    recovery.compare(item, fresh)
    assert item["confirmation"]["state"] == "unresolved"
    another = validate_visual(visual(), "another", 1, full["bounds"])["candidate"]
    another["transcriptScope"] = {"kind": "region", "complete": True}
    item["candidates"].append(another)
    recovery.compare(item, another)
    assert item["confirmation"] == {"state": "agreed", "basis": "repeat-vision"}


def test_dispute_renders_union_of_competing_physical_placements(tmp_path, monkeypatch):
    original = PDFRecovery.native
    wrong_rows = [["Code", "Value"], ["001", "-1.2300"]]
    def native(self, page, words):
        original(self, page, words)
        other = candidate_from_rows(self.next_id(), self.page, [10, 65, 230, 155], wrong_rows, "line-text")
        self.add(other)
    monkeypatch.setattr(PDFRecovery, "native", native)
    context = Context([visual(wrong_rows), visual(wrong_rows)])
    extract(pdf_source(tmp_path), tmp_path / "products", context)
    reread = next(c for c in context.calls if c["kind"] == "table-reread")
    assert reread["sourceUnit"].get("requestedBounds", reread["sourceUnit"]["bounds"]) == [7, 62, 233, 158]
    assert reread["sourceUnit"]["bounds"][0] < 10 and reread["sourceUnit"]["bounds"][2] > 230
    assert "1.2300" not in reread["prompt"] and "001" not in reread["prompt"]


@pytest.mark.parametrize("layout", ["side-by-side", "stacked"])
def test_scout_splits_merged_proposal_into_two_identical_physical_tables(tmp_path, monkeypatch, layout):
    from reportlab.pdfgen import canvas
    path = tmp_path / "neighbors.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(600, 400))
    offsets = [(0, 0), (280, 0)] if layout == "side-by-side" else [(0, 0), (0, -150)]
    for offset, y_offset in offsets:
        for x in (20, 120, 220):
            pdf.line(x + offset, 250 + y_offset, x + offset, 330 + y_offset)
        for y in (250, 290, 330):
            pdf.line(20 + offset, y + y_offset, 220 + offset, y + y_offset)
        for r, row in enumerate(ROWS):
            for c, value in enumerate(row):
                pdf.drawString(25 + 100 * c + offset, 305 - 40 * r + y_offset, value)
    pdf.drawString(20, 370, "Native prose")
    pdf.showPage()
    pdf.save()
    original = PDFRecovery.native
    def native(self, page, words):
        original(self, page, words)
        box = [20, 70, 500, 150] if layout == "side-by-side" else [20, 70, 220, 300]
        merged_rows = [ROWS[0] + ROWS[0], ROWS[1] + ROWS[1]] if layout == "side-by-side" else ROWS + ROWS
        merged = candidate_from_rows(self.next_id(), self.page, box, merged_rows, "text-alignment")
        merged["weakProposal"] = True
        self.add(merged)
    monkeypatch.setattr(PDFRecovery, "native", native)
    boxes = [[20, 70, 220, 150], [300, 70, 500, 150]] if layout == "side-by-side" else [[20, 70, 220, 150], [20, 220, 220, 300]]
    scout = {"tables": [{"bounds": [box[0] / 600 * 1000, box[1] / 400 * 1000,
                                     box[2] / 600 * 1000, box[3] / 400 * 1000]} for box in boxes]}
    context = Context([json.dumps(scout), visual(), visual()])
    output = tmp_path / "products"
    result = extract({"path": str(path)}, output, context)
    assert len(result["tables"]) == 2
    assert all(t["confirmation"]["state"] == "agreed" for t in result["tables"])
    assert [t["source"]["bounds"] for t in result["tables"]] == boxes
    assert [c["kind"] for c in context.calls] == ["table-scout", "table-confirmation", "table-confirmation"]
    candidates = [json.loads(line) for line in (output / "tables/evidence/page_0001.candidates.jsonl").read_text().splitlines()]
    assert any(c["method"] == "text-alignment" and c.get("superseded") for c in candidates)


@pytest.mark.parametrize("orientation", [90, 270])
def test_sideways_tall_scan_preserves_positioned_identical_rows(tmp_path, orientation):
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    path = tmp_path / "sideways.pdf"
    rows = [["Header", "Code"]] + [["same", "001"] for _ in range(24)] + [["Last", "099"]]
    image = Image.new("RGB", (100, 520), "white")
    draw = ImageDraw.Draw(image)
    for index, row in enumerate(rows):
        draw.line((0, index * 20, 100, index * 20), fill="black")
        draw.text((4, index * 20 + 3), " ".join(row), fill="black")
    physical = image.transpose(Image.Transpose.ROTATE_90 if orientation == 90 else Image.Transpose.ROTATE_270)
    pdf = canvas.Canvas(str(path), pagesize=(600, 300))
    pdf.drawString(20, 280, "Native prose")
    pdf.drawImage(ImageReader(physical), 40, 120, 520, 100)
    pdf.showPage()
    pdf.save()
    image.close()
    physical.close()

    class Sideways(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            if operation["kind"] == "table-scout":
                return {"text": json.dumps({"tables": [{"bounds": [40 / 600 * 1000, 80 / 300 * 1000,
                    560 / 600 * 1000, 180 / 300 * 1000], "orientation": orientation}]})}
            if not operation["kind"].startswith("table-"):
                return {"text": "figure text"}
            # Source positions are independently annotated; model coordinates
            # describe the actual upright image, including clipped edge rows.
            source = operation["sourceUnit"]["bounds"]
            assert source[3] - source[1] >= 100
            assert source[2] - source[0] < 140
            upright = operation["sourceUnit"]["uprightBounds"]
            left, top, right, bottom = upright
            visible, edges = [], []
            for index, row in enumerate(rows):
                start, end = 40 + index * 20, 60 + index * 20
                if max(top, start) < min(bottom, end):
                    visible.append(row)
                    if not edges:
                        edges.append(max(top, start))
                    edges.append(min(bottom, end))
            response = json.loads(visual(visible))
            response["rowEdges"] = [(edge - top) / (bottom - top) * 1000 for edge in edges]
            columns = [120, 170, 220] if orientation == 90 else [80, 130, 180]
            response["columnEdges"] = [(edge - left) / (right - left) * 1000 for edge in columns]
            return {"text": json.dumps(response)}

    profile = default_profile()
    profile["limits"]["pixels"] = 200_000
    profile["options"]["figureDescriptions"] = False
    profile["options"]["tableRecovery"]["maxAdditionalPasses"] = 0
    context = Sideways()
    output = tmp_path / "products"
    result = PDFAdapter().extract({"path": str(path)}, profile, output, context)
    validate_table_evidence(output, result)
    table = result["tables"][0]
    assert table["confirmation"] == {"state": "agreed", "basis": "repeat-vision"}
    actual = [[cell.get("value") for cell in json.loads(line)["cells"]] for line in (output / table["typedPath"]).read_text().splitlines()]
    assert actual == rows
    structure = json.loads((output / table["evidencePaths"]["structure"]).read_text())
    assert len(structure["physicalRowBands"]) == 26
    expected = [40, 80, 60, 180] if orientation == 90 else [540, 80, 560, 180]
    assert structure["physicalRowBands"][0] == pytest.approx(expected)
    assert all(p["render"]["orientation"] == orientation for p in result["products"] if p["kind"] == "table-image" and "render" in p and "imageToPage" in p["render"] and not p["path"].endswith(".source.png"))
    assert all(p["render"]["orientation"] == 0 for p in result["products"] if p["path"].endswith(".source.png"))


def test_changed_visual_row_bands_receive_complete_support_tiles_in_one_pass(tmp_path):
    snapshot = pdf_source(tmp_path)
    profile = default_profile()
    profile["limits"]["pixels"] = 230_000
    profile["options"]["figureDescriptions"] = False
    annotations = [(70, 80), (80, 139)]

    class ChangedBands(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            x0, top, x1, bottom = operation["sourceUnit"]["bounds"]
            rows, edges = [], []
            for index, (start, end) in enumerate(annotations):
                if max(top, start) < min(bottom, end):
                    rows.append(ROWS[index])
                    if not edges:
                        edges.append(max(top, start))
                    edges.append(min(bottom, end))
            response = json.loads(visual(rows))
            response["rowEdges"] = [(edge - top) / (bottom - top) * 1000 for edge in edges]
            response["columnEdges"] = [(edge - x0) / (x1 - x0) * 1000 for edge in (20, 120, 220)]
            return {"text": json.dumps(response)}

    context = ChangedBands()
    output = tmp_path / "products"
    result = PDFAdapter().extract(snapshot, profile, output, context)
    validate_table_evidence(output, result)
    rereads = [c for c in context.calls if c["kind"] == "table-reread"]
    assert any(c["sourceUnit"]["bounds"][1] < 80 and c["sourceUnit"]["bounds"][3] > 139 for c in rereads)
    plan = json.loads((output / "work-plan.json").read_text())
    confirmation = next(u for u in plan["units"] if u["kind"] == "table-confirmation")
    assert confirmation["passesSpent"] == 1
    assert confirmation["readPasses"]["dispute_1"]["regenerated"]
    # A single complete correction read is useful fallback evidence, but cannot
    # agree with the old grid or an earlier aggregate made only from half rows.
    assert result["tables"][0]["confirmation"]["state"] == "unresolved"
    assert next(s for s in result["stages"] if s["id"] == "table-confirmation")["state"] == "partial"
    assert len([u for u in plan["units"] if u["kind"] == "tile-seam" and u["state"] == "completed"]) >= 1


@pytest.mark.parametrize("orientation", [90, 270])
def test_native_sideways_grid_exports_upright_rows_and_once_assigned_words(tmp_path, orientation):
    from reportlab.pdfgen import canvas
    path = tmp_path / "native-sideways.pdf"
    rows = [["H", "C"]] + [["A", "1"] for _ in range(24)] + [["Z", "9"]]
    pdf = canvas.Canvas(str(path), pagesize=(600, 300))
    pdf.drawString(20, 280, "Native prose")
    for x in range(40, 561, 20):
        pdf.line(x, 120, x, 220)
    for y in (120, 170, 220):
        pdf.line(40, y, 560, y)
    pdf.setFont("Helvetica", 8)
    for r, row in enumerate(rows):
        x = 40 + r * 20 if orientation == 90 else 540 - r * 20
        for c, value in enumerate(row):
            source_top = 130 - c * 50 if orientation == 90 else 80 + c * 50
            y = 300 - source_top - 5 if orientation == 90 else 300 - source_top - 45
            pdf.saveState()
            pdf.translate(x + 10, y)
            pdf.rotate(-90 if orientation == 90 else 90)
            pdf.drawString(0, 0, value)
            pdf.restoreState()
    pdf.showPage()
    pdf.save()

    class NativeSideways(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            if operation["kind"] == "table-scout":
                return {"text": json.dumps({"tables": [{"bounds": [40 / 600 * 1000, 80 / 300 * 1000,
                    560 / 600 * 1000, 180 / 300 * 1000], "orientation": orientation}]})}
            left, top, right, bottom = operation["sourceUnit"]["uprightBounds"]
            selected, edges = [], []
            for r, row in enumerate(rows):
                start, end = 40 + r * 20, 60 + r * 20
                if max(top, start) < min(bottom, end):
                    selected.append(row)
                    if not edges:
                        edges.append(max(top, start))
                    edges.append(min(bottom, end))
            data = json.loads(visual(selected))
            data["rowEdges"] = [(v - top) / (bottom - top) * 1000 for v in edges]
            columns = [120, 170, 220] if orientation == 90 else [80, 130, 180]
            data["columnEdges"] = [(v - left) / (right - left) * 1000 for v in columns]
            return {"text": json.dumps(data)}

    profile = default_profile()
    profile["limits"]["pixels"] = 200_000
    profile["options"]["tableRecovery"].update(forcePages=[1], maxAdditionalPasses=0)
    context = NativeSideways()
    output = tmp_path / "products"
    result = PDFAdapter().extract({"path": str(path)}, profile, output, context)
    validate_table_evidence(output, result)
    table = result["tables"][0]
    audit = json.loads((output / table["evidencePaths"]["decisions"]).read_text())
    assert table["confirmation"] == {"state": "agreed", "basis": "native-vision"}, (table["recovery"],
        [c["differences"][:3] for c in audit["comparisons"]])
    actual = [[cell.get("value") for cell in json.loads(line)["cells"]] for line in (output / table["typedPath"]).read_text().splitlines()]
    assert actual == rows
    structure = json.loads((output / table["evidencePaths"]["structure"]).read_text())
    assigned = [word for cell in structure["cells"] for word in cell["wordIds"]]
    assert len(assigned) == 52 and len(set(assigned)) == 52
    assert all(cell["bbox"] and cell["uprightBBox"] for cell in structure["cells"])


def test_model_completed_half_rows_never_supply_aggregate_values(tmp_path):
    snapshot = pdf_source(tmp_path)
    profile = default_profile()
    profile["limits"]["pixels"] = 230_000
    profile["options"]["tableRecovery"]["maxAdditionalPasses"] = 0

    class HalfRows(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            left, top, right, bottom = operation["sourceUnit"]["bounds"]
            rows, edges = [], []
            for index, (start, end) in enumerate(((70, 110), (110, 150))):
                if max(top, start) < min(bottom, end):
                    clipped = start <= top or end >= bottom
                    rows.append(["fabricated", "999"] if clipped else ROWS[index])
                    if not edges:
                        edges.append(max(top, start))
                    edges.append(min(bottom, end))
            data = json.loads(visual(rows))
            data["rowEdges"] = [(v - top) / (bottom - top) * 1000 for v in edges]
            data["columnEdges"] = [(v - left) / (right - left) * 1000 for v in (20, 120, 220)]
            return {"text": json.dumps(data)}

    output = tmp_path / "products"
    result = PDFAdapter().extract(snapshot, profile, output, HalfRows())
    validate_table_evidence(output, result)
    assert result["tables"][0]["confirmation"]["state"] == "agreed"
    candidates = [json.loads(line) for line in (output / "tables/evidence/page_0001.candidates.jsonl").read_text().splitlines()]
    aggregate = next(c for c in candidates if c["method"] == "tile-aggregation")
    assert aggregate["logicalRows"] == ROWS
    fragments = [source for row in aggregate["rowSources"] for source in row if source.get("clipped")]
    assert fragments and all(source["valuesUsed"] is False for source in fragments)
    assert any("fabricated" in str(c["rows"]) for c in candidates if c["method"] == "visual")


def test_interrupted_support_pass_resumes_same_plan_without_replaying_reads(tmp_path):
    snapshot = pdf_source(tmp_path)
    profile = default_profile()
    profile["limits"]["pixels"] = 230_000
    profile["options"]["tableRecovery"]["maxModelCalls"] = 3

    class Bands(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            left, top, right, bottom = operation["sourceUnit"]["bounds"]
            visible, edges = [], []
            for index, (start, end) in enumerate(((70, 80), (80, 139))):
                if max(top, start) < min(bottom, end):
                    visible.append(ROWS[index])
                    if not edges:
                        edges.append(max(top, start))
                    edges.append(min(bottom, end))
            data = json.loads(visual(visible))
            data["rowEdges"] = [(v - top) / (bottom - top) * 1000 for v in edges]
            data["columnEdges"] = [(v - left) / (right - left) * 1000 for v in (20, 120, 220)]
            return {"text": json.dumps(data)}

    first_output = tmp_path / "first"
    first_context = Bands()
    first = PDFAdapter().extract(snapshot, profile, first_output, first_context)
    validate_table_evidence(first_output, first)
    plan = json.loads((first_output / "work-plan.json").read_text())
    unit = next(u for u in plan["units"] if u["kind"] == "table-confirmation")
    assert unit["activePass"] == "dispute_1" and unit["passesSpent"] == 1
    original_support = unit["readPasses"]["dispute_1"]["supportBands"]
    completed = [u for u in plan["units"] if u["kind"] == "table-read-tile" and u["state"] == "completed"]
    tile_bytes = {p: (first_output / p).read_bytes() for u in completed for p in u["evidencePaths"] if p.endswith(".png")}
    output = tmp_path / "second"
    context = Bands()
    context.resume = resume_context(first, plan, output, first_output).resume
    profile["options"]["tableRecovery"]["maxModelCalls"] = None
    second = PDFAdapter().extract(snapshot, profile, output, context)
    validate_table_evidence(output, second)
    assert context.calls and all(c["kind"] == "table-reread" for c in context.calls)
    assert all((output / path).read_bytes() == data for path, data in tile_bytes.items())
    resumed = json.loads((output / "work-plan.json").read_text())
    unit = next(u for u in resumed["units"] if u["kind"] == "table-confirmation")
    assert unit["activePass"] is None and unit["passesSpent"] == 1
    assert unit["readPasses"]["dispute_1"]["supportBands"] == original_support
    assert len(context.calls) == sum(u["kind"] == "table-read-tile" and u["state"] == "pending" for u in plan["units"])


def test_scout_refinement_reextracts_borderless_regions_from_one_word_inventory(tmp_path):
    from reportlab.pdfgen import canvas
    path = tmp_path / "borderless.pdf"
    rows = ROWS + [["002", "2.0000"]]
    pdf = canvas.Canvas(str(path), pagesize=(600, 400))
    pdf.drawString(20, 370, "Native prose")
    for offset in (0, 280):
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                pdf.drawString(25 + 100 * c + offset, 305 - 40 * r, value)
    pdf.showPage()
    pdf.save()
    scout = {"tables": [{"bounds": [20 / 600 * 1000, 200, 220 / 600 * 1000, 450]},
                        {"bounds": [500, 200, 500 / 600 * 1000, 450]}]}
    context = Context([json.dumps(scout)] + [visual(rows)] * 4)
    output = tmp_path / "products"
    result = extract({"path": str(path)}, output, context, forcePages=[1])
    assert len(result["tables"]) == 2
    assert all(t["confirmation"] == {"state": "agreed", "basis": "native-vision"} for t in result["tables"])
    assert [c["kind"] for c in context.calls] == ["table-scout", "table-confirmation", "table-confirmation"]
    word_ids = {w["id"] for w in json.loads((output / "text/page_0001.json").read_text())["words"]}
    assigned = []
    for table in result["tables"]:
        structure = json.loads((output / table["evidencePaths"]["structure"]).read_text())
        assigned.extend(word for cell in structure["cells"] for word in cell["wordIds"])
    assert len(assigned) == 12 and len(set(assigned)) == 12 and set(assigned) <= word_ids


def test_unlocated_page_ocr_survives_failed_refinement_as_pending_fallback(tmp_path):
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    path = tmp_path / "unlocated.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(300, 400))
    pdf.drawString(20, 380, "Native footer")
    image = Image.new("RGB", (200, 300), "white")
    pdf.drawImage(ImageReader(image), 20, 40, 200, 300)
    pdf.showPage()
    pdf.save()
    image.close()
    context = Context([json.dumps({"text": "Code Value", "tables": [{"rows": ROWS}]}),
                       json.dumps({"tables": []}), "figure text"])
    result = extract({"path": str(path)}, tmp_path / "products", context)
    assert result["tables"][0]["rows"] == 2
    assert result["tables"][0]["source"]["bounds"] is None
    assert result["tables"][0]["confirmation"]["state"] == "pending"
    assert next(s for s in result["stages"] if s["id"] == "table-confirmation")["state"] == "partial"


def test_resume_restores_image_transforms_from_minimal_copied_product_descriptors(tmp_path):
    snapshot = pdf_source(tmp_path)
    first_output = tmp_path / "first"
    first = extract(snapshot, first_output, Context())
    plan = json.loads((first_output / "work-plan.json").read_text())
    output = tmp_path / "second"
    context = resume_context(first, plan, output, first_output)
    descriptors = [{key: p[key] for key in ("path", "kind", "mediaType")} for p in first["products"]]
    context.resume["products"] = descriptors
    context.resume["previousExtraction"] = first | {"products": descriptors}
    second = extract(snapshot, output, context)
    assert context.calls == []
    original_images = {p["path"]: p for p in first["products"] if p["kind"] == "table-image"}
    for image in (p for p in second["products"] if p["kind"] == "table-image"):
        assert image["render"] == original_images[image["path"]]["render"]
        assert image["source"] == original_images[image["path"]]["source"]
    assert all(p.get("model", {}).get("requestedModel") == "fixture" for p in second["products"] if p["kind"] == "table-model-response")


def test_newly_scouted_region_cannot_reassign_an_agreed_table_unit(tmp_path):
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    path = tmp_path / "new-region.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(300, 500))
    pdf.drawString(20, 480, "Native prose")
    image = Image.new("RGB", (200, 80), "white")
    draw = ImageDraw.Draw(image)
    for r, row in enumerate(ROWS):
        for c, value in enumerate(row):
            draw.text((5 + 100 * c, 5 + 40 * r), value, fill="black")
    pdf.drawImage(ImageReader(image), 20, 380, 200, 80)
    image.close()
    for x in (20, 120, 220):
        pdf.line(x, 220, x, 300)
    for y in (220, 260, 300):
        pdf.line(20, y, 220, y)
    for r, row in enumerate(ROWS):
        for c, value in enumerate(row):
            pdf.drawString(25 + 100 * c, 285 - 40 * r, value)
    pdf.showPage()
    pdf.save()
    snapshot = {"path": str(path)}
    first_output = tmp_path / "first"
    first = extract(snapshot, first_output, Context(["invalid scout", visual(), "figure text"]))
    agreed = first["tables"][0]
    assert agreed["confirmation"]["state"] == "agreed"
    plan = json.loads((first_output / "work-plan.json").read_text())
    output = tmp_path / "second"
    context = resume_context(first, plan, output, first_output)
    scout = {"tables": [{"bounds": [20 / 300 * 1000, 80, 220 / 300 * 1000, 240]},
                        {"bounds": [20 / 300 * 1000, 400, 220 / 300 * 1000, 560]}]}
    context.responses = iter([json.dumps(scout), visual(), visual()])
    second = extract(snapshot, output, context)
    boxes = [t["source"]["bounds"]["bbox"] if isinstance(t["source"]["bounds"], dict) else t["source"]["bounds"] for t in second["tables"]]
    assert sorted(boxes) == [[20, 40, 220, 120], [20, 200, 220, 280]]
    assert next(t for t in second["tables"] if t["id"] == agreed["id"]) == agreed
    assert [c["kind"] for c in context.calls] == ["table-scout", "table-extraction", "table-confirmation"]
    assert all(c["sourceUnit"].get("requestedBounds", [0, 0, 0, 0])[3] < 130 for c in context.calls if c["kind"] != "table-scout")


def test_completed_unusable_pass_does_not_block_explicit_remaining_allowance(tmp_path):
    snapshot = pdf_source(tmp_path)
    profile = default_profile()
    profile["limits"]["pixels"] = 230_000
    profile["options"]["tableRecovery"]["maxAdditionalPasses"] = 2

    class PresenceDispute(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            if len(self.calls) <= 4:
                return {"text": json.dumps({"tablePresent": False, "rowCount": 0, "columnCount": 0, "cells": []})}
            left, top, right, bottom = operation["sourceUnit"]["bounds"]
            rows, edges = [], []
            for index, (start, end) in enumerate(((70, 110), (110, 150))):
                if max(top, start) < min(bottom, end):
                    rows.append(ROWS[index])
                    if not edges:
                        edges.append(max(top, start))
                    edges.append(min(bottom, end))
            data = json.loads(visual(rows))
            data["rowEdges"] = [(v - top) / (bottom - top) * 1000 for v in edges]
            data["columnEdges"] = [(v - left) / (right - left) * 1000 for v in (20, 120, 220)]
            return {"text": json.dumps(data)}

    context = PresenceDispute()
    output = tmp_path / "products"
    result = PDFAdapter().extract(snapshot, profile, output, context)
    validate_table_evidence(output, result)
    plan = json.loads((output / "work-plan.json").read_text())
    unit = next(u for u in plan["units"] if u["kind"] == "table-confirmation")
    assert unit["passesSpent"] == 2 and unit["activePass"] is None
    assert result["tables"][0]["confirmation"] == {"state": "agreed", "basis": "native-vision"}
    assert len(context.calls) == 6
    units = {u["id"]: u for u in plan["units"]}
    assert all(units[d]["state"] == "completed" for u in units.values() if u["state"] == "completed" for d in u["dependencies"])


def image_heavy_source(tmp_path):
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    path = tmp_path / "image-heavy.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(300, 400))
    pdf.drawString(20, 380, "Native footer")
    image = Image.new("RGB", (200, 300), "white")
    pdf.drawImage(ImageReader(image), 20, 40, 200, 300)
    image.close()
    pdf.showPage()
    pdf.save()
    return {"path": str(path), "filename": path.name}


def seal_pdf(result, output, profile, job_id):
    from preprocessing.indexes import build_indexes
    from preprocessing.results import make_manifest
    build_indexes(result, output, profile["limits"])
    return make_manifest({"id": job_id, "profile": profile, "source": {"filename": "source.pdf"}}, result, output)


def test_page_ocr_cutoff_is_pending_and_resume_seals_without_duplicate_image(tmp_path):
    snapshot = image_heavy_source(tmp_path)
    profile = default_profile()
    profile["options"]["figureDescriptions"] = False
    profile["options"]["tableRecovery"].update(geometry="off", vision="off")
    first_context = Context()
    def cutoff(operation, refs):
        first_context.calls.append(operation)
        first_context.remaining = 0
        raise ServiceError("WORK_BUDGET_EXHAUSTED", "Settled soft cutoff")
    first_context.model = cutoff
    first_output = tmp_path / "first"
    first = PDFAdapter().extract(snapshot, profile, first_output, first_context)
    assert next(u for u in json.loads((first_output / "work-plan.json").read_text())["units"] if u["kind"] == "page-ocr")["state"] == "pending"
    assert json.loads((first_output / "text/pages.jsonl").read_text())["ocrStatus"] == "pending"
    assert not any(f["code"].endswith("FAILED") for f in first["findings"])
    assert seal_pdf(first, first_output, profile, "job_first")["outcome"] == "partial"
    plan = json.loads((first_output / "work-plan.json").read_text())
    output = tmp_path / "second"
    context = resume_context(first, plan, output, first_output)
    context.responses = iter([json.dumps({"text": "Visible OCR", "tables": []}), "figure text"])
    second = PDFAdapter().extract(snapshot, profile, output, context)
    manifest = seal_pdf(second, output, profile, "job_second")
    assert manifest["outcome"] == "succeeded"
    assert len([p for p in second["products"] if p["path"] == "text/page_0001.png"]) == 1
    assert [c["kind"] for c in context.calls] == ["page-ocr", "figure-ocr"]


def test_resume_full_outputs_rebuilds_navigation_and_seals(tmp_path):
    snapshot = pdf_source(tmp_path)
    profile = default_profile()
    first_output = tmp_path / "first"
    first = extract(snapshot, first_output, Context())
    seal_pdf(first, first_output, profile, "job_first")
    plan = json.loads((first_output / "work-plan.json").read_text())
    output = tmp_path / "second"
    context = resume_context(first, plan, output, first_output)
    second = PDFAdapter().extract(snapshot, profile, output, context)
    assert seal_pdf(second, output, profile, "job_second")["outcome"] == "succeeded"
    assert context.calls == []
    assert len({p["path"] for p in second["products"]}) == len(second["products"])


@pytest.mark.parametrize("kind", ["figure-ocr", "figure-description"])
def test_figure_soft_cutoff_has_pending_status_without_failure_finding(tmp_path, kind):
    context = Context()
    def model(operation, refs):
        context.calls.append(operation)
        if operation["kind"] == kind:
            context.remaining = 0
            raise ServiceError("WORK_BUDGET_EXHAUSTED", "Settled soft cutoff")
        return {"text": "visible figure text"}
    context.model = model
    profile = default_profile()
    profile["options"]["tableRecovery"]["vision"] = "off"
    output = tmp_path / "products"
    result = PDFAdapter().extract(pdf_source(tmp_path, image=True), profile, output, context)
    figure = result["figures"][0]
    assert figure["ocrStatus" if kind == "figure-ocr" else "interpretationStatus"] == "pending"
    assert not any(f["code"].endswith("FAILED") for f in result["findings"])
    plan = json.loads((output / "work-plan.json").read_text())
    assert next(u for u in plan["units"] if u["kind"] == kind)["state"] == "pending"


def test_native_cutoff_during_words_does_not_report_parser_failure(tmp_path, monkeypatch):
    from pdfplumber.page import Page
    context = Context()
    original = Page.extract_text
    def text(self, **kwargs):
        result = original(self, **kwargs)
        context.remaining = 0
        return result
    monkeypatch.setattr(Page, "extract_text", text)
    output = tmp_path / "products"
    result = extract(pdf_source(tmp_path), output, context)
    assert not any(f["code"].endswith("FAILED") for f in result["findings"])
    plan = json.loads((output / "work-plan.json").read_text())
    assert next(u for u in plan["units"] if u["kind"] == "native-evidence")["state"] == "pending"


def test_genuine_ocr_failure_near_cutoff_remains_failed(tmp_path):
    context = Context()
    def failure(operation, refs):
        context.remaining = 0
        raise ServiceError("ENGINE_UNAVAILABLE", "Genuine provider failure")
    context.model = failure
    output = tmp_path / "products"
    result = extract(image_heavy_source(tmp_path), output, context)
    plan = json.loads((output / "work-plan.json").read_text())
    assert next(u for u in plan["units"] if u["kind"] == "page-ocr")["state"] == "failed"
    assert any(f["code"] == "PAGE_OCR_FAILED" for f in result["findings"])


@pytest.mark.parametrize("limit", ["rows", "columns", "memory"])
def test_visual_grid_capacity_is_resource_error_before_dense_allocation(tmp_path, limit):
    profile = default_profile()
    profile["options"]["tableRecovery"]["maxAdditionalPasses"] = 0
    nr, nc = 3, 3
    if limit == "rows":
        profile["limits"]["rows"] = 2
    elif limit == "columns":
        profile["limits"]["columns"] = 2
    else:
        profile["limits"]["workerMemoryBytes"] = 128 * 1024
        nr = nc = 8
    response = json.dumps({"tablePresent": True, "rowCount": nr, "columnCount": nc, "cells": []})
    output = tmp_path / "products"
    with pytest.raises(ServiceError) as error:
        PDFAdapter().extract(pdf_source(tmp_path), profile, output, Context([response]))
    assert error.value.code == "RESOURCE_LIMIT"
    assert any(p.read_text() == response for p in output.glob("tables/evidence/*.response.txt"))


def test_padded_image_edges_map_once_and_agree_with_native_grid(tmp_path):
    class Edges(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            left, top, right, bottom = operation["sourceUnit"]["bounds"]
            assert left < 20 and top < 70 and right > 220 and bottom > 150
            data = json.loads(visual())
            data["rowEdges"] = [(v - top) / (bottom - top) * 1000 for v in (70, 110, 150)]
            data["columnEdges"] = [(v - left) / (right - left) * 1000 for v in (20, 120, 220)]
            return {"text": json.dumps(data)}
    context = Edges()
    output = tmp_path / "products"
    result = extract(pdf_source(tmp_path), output, context)
    assert result["tables"][0]["confirmation"] == {"state": "agreed", "basis": "native-vision"}
    assert [c["kind"] for c in context.calls] == ["table-confirmation"]
    candidates = [json.loads(line) for line in (output / "tables/evidence/page_0001.candidates.jsonl").read_text().splitlines()]
    candidate = next(c for c in candidates if c["method"] == "visual")
    assert candidate["rowBands"] == [[70, 110], [110, 150]]
    assert candidate["bounds"] == [20, 70, 220, 150]


@pytest.mark.parametrize("geometry", ["auto", "force"])
def test_alternate_parser_is_admitted_by_geometry_policy(tmp_path, monkeypatch, geometry):
    from pdfplumber.page import Page
    original = Page.find_tables
    def tables(self, table_settings=None):
        if table_settings and table_settings.get("horizontal_strategy") == "text":
            raise RuntimeError("Alternate parser unavailable")
        return original(self, table_settings)
    monkeypatch.setattr(Page, "find_tables", tables)
    context = Context([json.dumps({"tables": []}), visual()] if geometry == "force" else [visual()])
    result = extract(pdf_source(tmp_path), tmp_path / "products", context, geometry=geometry)
    assert result["tables"][0]["rows"] == 2
    native = next(s for s in result["stages"] if s["id"] == "native-evidence")
    assert native["state"] == "completed"
    if geometry == "auto":
        assert result["tables"][0]["confirmation"]["state"] == "agreed"
        assert not any(f["code"] == "TABLE_GEOMETRY_FAILED" for f in result["findings"])
    else:
        assert any(f["code"] == "TABLE_GEOMETRY_FAILED" for f in result["findings"])
        assert result["tables"][0]["confirmation"] == {"state": "agreed", "basis": "repeat-vision"}
        assert result["tables"][0]["recovery"]["state"] == "recovered"
        assert next(s for s in result["stages"] if s["id"] == "table-recovery")["state"] == "completed"


def test_memory_grid_capacity_scales_with_configured_worker_memory(tmp_path):
    profile = default_profile()
    profile["limits"]["workerMemoryBytes"] = 256 * 1024
    profile["options"]["tableRecovery"]["maxAdditionalPasses"] = 0
    response = json.dumps({"tablePresent": True, "rowCount": 8, "columnCount": 8, "cells": []})
    result = PDFAdapter().extract(pdf_source(tmp_path), profile, tmp_path / "products", Context([response]))
    assert result["provenance"]["visualGridAllocation"]["maxGridCells"] == 64
    assert result["tables"][0]["confirmation"]["state"] == "unresolved"


def test_real_native_blank_and_span_coverage_remain_distinct(tmp_path):
    import pdfplumber
    from reportlab.pdfgen import canvas
    path = tmp_path / "blank-span.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(300, 400))
    for x in (20, 220):
        pdf.line(x, 250, x, 330)
    pdf.line(120, 250, 120, 290)
    for y in (250, 290, 330):
        pdf.line(20, y, 220, y)
    pdf.drawString(25, 305, "Group")
    pdf.drawString(25, 265, "001")
    pdf.showPage()
    pdf.save()
    with pdfplumber.open(path) as document:
        assert document.pages[0].find_tables()[0].extract() == [["Group", None], ["001", ""]]
    transcript = json.dumps({"tablePresent": True, "rowCount": 2, "columnCount": 2, "cells": [
        {"row": 1, "column": 1, "text": "Group", "columnSpan": 2},
        {"row": 2, "column": 1, "text": "001"}, {"row": 2, "column": 2, "text": ""}]})
    output = tmp_path / "products"
    result = extract({"path": str(path)}, output, Context([transcript]))
    table = result["tables"][0]
    assert table["confirmation"]["state"] == "agreed"
    structure = json.loads((output / table["evidencePaths"]["structure"]).read_text())
    assert structure["grid"][0][1]["state"] == "covered"
    assert structure["grid"][1][1]["state"] == "empty"


def test_unlocated_ocr_and_tiny_image_are_scouted_without_figure_size_gate(tmp_path):
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    path = tmp_path / "tiny.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(300, 400))
    pdf.drawString(20, 380, "Native footer")
    large = Image.new("RGB", (200, 300), "white")
    tiny = Image.new("RGB", (8, 8), "red")
    pdf.drawImage(ImageReader(large), 20, 40, 200, 300)
    pdf.drawImage(ImageReader(tiny), 250, 30, 8, 8)
    large.close()
    tiny.close()
    pdf.showPage()
    pdf.save()
    ocr = json.dumps({"text": "Code Value", "tables": [{"rows": ROWS}]})
    scout = json.dumps({"tables": [{"bounds": [20 / 300 * 1000, 150, 220 / 300 * 1000, 900]}]})
    context = Context([ocr, scout, visual(), "figure text"])
    output = tmp_path / "products"
    result = extract({"path": str(path)}, output, context, maxAdditionalPasses=0)
    assert [c["kind"] for c in context.calls][:3] == ["page-ocr", "table-scout", "table-confirmation"]
    assert result["tables"][0]["confirmation"] == {"state": "agreed", "basis": "ocr-vision"}
    images = json.loads((output / "tables/evidence/page_0001.discovery.json").read_text())["images"]
    assert next(i for i in images if i["bounds"][2] - i["bounds"][0] == 8)["disposition"] == "non-table"
    assert next(i for i in images if i["bounds"][2] - i["bounds"][0] == 200)["disposition"] == "covered-by-table-read"


def test_ordinary_region_render_error_retains_partial_native_fallback(tmp_path, monkeypatch):
    def render(*args, **kwargs):
        raise RuntimeError("Ordinary rendering failure")
    monkeypatch.setattr(Renderer, "render_region", render)
    result = extract(pdf_source(tmp_path), tmp_path / "products", Context())
    assert result["tables"][0]["rows"] == 2
    assert result["tables"][0]["confirmation"]["state"] == "failed"
    assert next(s for s in result["stages"] if s["id"] == "table-confirmation")["state"] == "partial"


def test_impossible_target_row_pixels_remain_strict_resource_error(tmp_path):
    profile = default_profile()
    profile["limits"]["pixels"] = 20_000
    with pytest.raises(ServiceError) as error:
        PDFAdapter().extract(pdf_source(tmp_path), profile, tmp_path / "products", Context())
    assert error.value.code == "RESOURCE_LIMIT"
    assert "complete table row" in str(error.value)


# Minimal native occurrences retained from the Dundas legend's INTENSITY panel.
# Coordinates and IDs are the extracted evidence, not manually reconstructed cells.
INTENSITY_WORDS = [
    {"id": "p0001_w000228", "text": "INTENSITY", "bbox": [434.4, 344.10428, 476.18572, 352.14428]},
    {"id": "p0001_w000235", "text": "1", "bbox": [434.4, 355.02428, 438.87024, 363.06428]},
    {"id": "p0001_w000236", "text": "weak", "bbox": [462, 355.02428, 480.55146, 363.06428]},
    {"id": "p0001_w000243", "text": "2", "bbox": [434.4, 365.94428, 438.87024, 373.98428]},
    {"id": "p0001_w000244", "text": "moderate", "bbox": [462, 365.94428, 496.06526, 373.98428]},
    {"id": "p0001_w000251", "text": "3", "bbox": [434.4, 376.62428, 438.87024, 384.66428]},
    {"id": "p0001_w000252", "text": "strong", "bbox": [462, 376.62428, 484.32366, 384.66428]},
]
INTENSITY_BOX = [432.5, 342.8, 539.7, 387.2]


def extracted_words_pdf(tmp_path, words, edges=()):
    from reportlab.pdfgen import canvas
    path = tmp_path / "extracted-words.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(595.32001, 842.03998))
    pdf.setFont("Helvetica", 8.04)
    for word in words:
        pdf.drawString(word["bbox"][0], 842.03998 - word["bbox"][3] + 1.8, word["text"])
    for left, top, right, bottom in edges:
        pdf.line(left, 842.03998 - top, right, 842.03998 - bottom)
    pdf.showPage()
    pdf.save()
    return {"path": str(path)}


def replay_words(monkeypatch, words):
    def occurrences(self, page, number):
        self.base_words = json.loads(json.dumps(words))
        return self.base_words
    monkeypatch.setattr(PDFRecovery, "words", occurrences)


def intensity_response(operation):
    data = {"tablePresent": True, "rowCount": 4, "columnCount": 2, "cells": [
        {"row": 1, "column": 1, "text": "INTENSITY", "columnSpan": 2},
        {"row": 2, "column": 1, "text": "1"}, {"row": 2, "column": 2, "text": "weak"},
        {"row": 3, "column": 1, "text": "2"}, {"row": 3, "column": 2, "text": "moderate"},
        {"row": 4, "column": 1, "text": "3"}, {"row": 4, "column": 2, "text": "strong"}]}
    left, top, right, bottom = operation["sourceUnit"]["bounds"]
    data["rowEdges"] = [(v - top) / (bottom - top) * 1000 for v in (342.8, 353.3, 364.5, 375.2, 387.2)]
    data["columnEdges"] = [(v - left) / (right - left) * 1000 for v in (432.5, 460.3, 539.7)]
    return json.dumps(data)


def test_two_full_visual_reads_resolve_extracted_native_span_ambiguity(tmp_path, monkeypatch):
    replay_words(monkeypatch, INTENSITY_WORDS)
    def native(self, page, words):
        raw = candidate_from_rows(self.next_id(), self.page, INTENSITY_BOX,
            [["INTENSITY\n1 weak\n2 moderate\n3 strong", None]], "native-text-layer",
            cell_bounds=[[INTENSITY_BOX, INTENSITY_BOX]], words=words)
        raw["issues"].append({"kind": "alternate-extraction-failed", "reason": "ambiguous-native-grid"})
        self.add(raw)
    monkeypatch.setattr(PDFRecovery, "native", native)
    class Reads(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            return {"text": json.dumps({"tables": []}) if operation["kind"] == "table-scout" else intensity_response(operation)}
    context = Reads()
    output = tmp_path / "products"
    result = extract(extracted_words_pdf(tmp_path, INTENSITY_WORDS), output, context)
    table = result["tables"][0]
    assert table["confirmation"] == {"state": "agreed", "basis": "repeat-vision"}
    assert table["recovery"] == {"state": "recovered", "reason": "visual-structure-correction"}
    assert table["rows"] == 4 and len(table["columns"]) == 2
    assert [c["kind"] for c in context.calls] == ["table-scout", "table-confirmation", "table-reread"]
    audit = json.loads((output / table["evidencePaths"]["decisions"]).read_text())
    assert audit["nativeMismatchHistory"] and audit["recoveryHistory"]["state"] == "unresolved"
    structure = json.loads((output / table["evidencePaths"]["structure"]).read_text())
    assert structure["selectedRows"] == [["INTENSITY", None], ["1", "weak"], ["2", "moderate"], ["3", "strong"]]


def test_parent_linked_different_grids_have_one_adapter_occurrence(tmp_path, monkeypatch):
    import preprocessing.formats.pdf_recovery as recovery_module
    original = PDFRecovery.native
    def native(self, page, words):
        original(self, page, words)
        parent = self.candidates[0]
        alternate = candidate_from_rows(self.next_id(), self.page, parent["bounds"],
            [["Code", "", "Value"], ["001", "", "1.2300"]], "line-text")
        alternate["parentId"] = parent["id"]
        self.add(alternate)
    monkeypatch.setattr(PDFRecovery, "native", native)
    # Replay the conservative grouping result that originally split the actual
    # native/line-text grids. Parent identity is the effectful adapter's safeguard.
    def separate(candidates):
        return [{"id": "region:" + c["id"], "page": c["page"], "bounds": c["bounds"],
                 "candidateIds": [c["id"]], "ambiguousWith": [], "issues": []} for c in candidates]
    monkeypatch.setattr(recovery_module, "group_candidates", separate)
    output = tmp_path / "products"
    result = extract(pdf_source(tmp_path), output, Context(), geometry="off", vision="off")
    assert len(result["tables"]) == 1
    candidates = [json.loads(line) for line in (output / "tables/evidence/page_0001.candidates.jsonl").read_text().splitlines()]
    assert len(candidates) == 2 and candidates[1]["parentId"] == candidates[0]["id"]


@pytest.mark.parametrize("vision", ["off", "confirm-all"])
def test_aligned_prose_retains_uncertainty_until_explicit_non_table_read(tmp_path, monkeypatch, vision):
    # Small replay of the repeated alignments in the targeting prose diagnosis.
    words = [{"id": f"p0001_w{r * 3 + c:06d}", "text": text, "bbox": [x, y, x + 20, y + 8]}
             for r, (y, texts) in enumerate(((15, ["These", "areas", "need"]), (30, ["Other", "faults", "have"]),
                                              (45, ["Target", "zones", "show"])))
             for c, (x, text) in enumerate(zip((5, 35, 65), texts), 1)]
    replay_words(monkeypatch, words)
    response = json.dumps({"tablePresent": False, "rowCount": 0, "columnCount": 0, "cells": []})
    class Prose(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            return {"text": json.dumps({"tables": []}) if operation["kind"] == "table-scout" else response}
    output = tmp_path / "products"
    result = extract(extracted_words_pdf(tmp_path, words), output, Prose(), vision=vision)
    candidates = [json.loads(line) for line in (output / "tables/evidence/page_0001.candidates.jsonl").read_text().splitlines()]
    assert any(c.get("weakProposal") for c in candidates)
    if vision == "off":
        assert result["tables"] and all(t["recovery"]["state"] == "unresolved" for t in result["tables"])
        assert next(s for s in result["stages"] if s["id"] == "table-recovery")["state"] == "partial"
    else:
        assert result["tables"] == []
        assert result["coverage"]["tableDiscovery"]["rejectedProposals"] > 0


def test_contained_real_panel_alignment_is_retained_and_scouted(tmp_path, monkeypatch):
    replay_words(monkeypatch, INTENSITY_WORDS)
    from types import SimpleNamespace

    from pdfplumber.page import Page
    huge = [64.85, 24.32, 539.7, 749.36]
    class Merged:
        bbox = huge
        def __init__(self):
            self.rows = [SimpleNamespace(cells=[huge])]
        def extract(self):
            return [["INTENSITY\n1 weak\n2 moderate\n3 strong"]]
    original = Page.find_tables
    def tables(self, table_settings=None):
        return [Merged()] if self.bbox[0] == 0 and self.bbox[1] == 0 and self.width > 590 and self.height > 840 else original(self, table_settings)
    monkeypatch.setattr(Page, "find_tables", tables)
    class Scout(Context):
        def model(self, operation, refs):
            self.calls.append(operation)
            if operation["kind"] == "table-scout":
                return {"text": json.dumps({"tables": [{"bounds": [INTENSITY_BOX[0] / 595.32001 * 1000,
                    INTENSITY_BOX[1] / 842.03998 * 1000, INTENSITY_BOX[2] / 595.32001 * 1000,
                    INTENSITY_BOX[3] / 842.03998 * 1000]}]})}
            return {"text": intensity_response(operation)}
    context = Scout()
    output = tmp_path / "products"
    result = extract(extracted_words_pdf(tmp_path, INTENSITY_WORDS), output, context)
    assert context.calls[0]["kind"] == "table-scout"
    candidates = [json.loads(line) for line in (output / "tables/evidence/page_0001.candidates.jsonl").read_text().splitlines()]
    assert any(c["method"] == "text-alignment" and c.get("containedInCandidateIds") for c in candidates)
    assert len(result["tables"]) == 1 and result["tables"][0]["rows"] == 4
    assert result["tables"][0]["confirmation"]["basis"] == "repeat-vision"


def test_closed_panel_keeps_heading_above_repeated_alignment_at_one_spanning_anchor(tmp_path, monkeypatch):
    replay_words(monkeypatch, INTENSITY_WORDS)
    from types import SimpleNamespace

    from pdfplumber.page import Page
    huge = [64.85, 24.32, 539.7, 749.36]
    class Merged:
        bbox = huge
        def __init__(self):
            self.rows = [SimpleNamespace(cells=[huge])]
        def extract(self):
            return [["INTENSITY\n1 weak\n2 moderate\n3 strong"]]
    monkeypatch.setattr(Page, "find_tables", lambda self, table_settings=None: [Merged()] if self.width > 590 else [])
    left, top, right, bottom = INTENSITY_BOX
    edges = [(left, top, right, top), (left, bottom, right, bottom),
             (left, top, left, bottom), (right, top, right, bottom),
             (460.3, 353.3, 460.3, bottom)]
    output = tmp_path / "products"
    result = extract(extracted_words_pdf(tmp_path, INTENSITY_WORDS, edges), output, Context(), vision="off")
    bounded = [t for t in result["tables"] if t["rows"] == 4 and len(t["columns"]) == 2
               and all(abs(a - b) < 2 for a, b in zip(t["source"]["bounds"].get("bbox", [])
                       if isinstance(t["source"]["bounds"], dict) else t["source"]["bounds"], INTENSITY_BOX))]
    assert len(bounded) == 1
    structure = json.loads((output / bounded[0]["evidencePaths"]["structure"]).read_text())
    assert structure["selectedRows"] == [["INTENSITY", None], ["1", "weak"], ["2", "moderate"], ["3", "strong"]]
    assert structure["cells"][0]["columnSpan"] == 2
    assert structure["grid"][0][1] == {"state": "covered", "anchor": [1, 1]}
    assert structure["cells"][0]["wordIds"] == [INTENSITY_WORDS[0]["id"]]
