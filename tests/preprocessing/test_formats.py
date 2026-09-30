import csv
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from preprocessing.contracts import default_profile
from preprocessing.errors import ServiceError
from preprocessing.formats import enabled_extensions, get_adapter


class Model:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def model(self, operation, input_refs):
        self.calls.append((operation, input_refs))
        assert Path(input_refs[0]["path"]).is_file()
        if self.fail:
            raise ServiceError("ENGINE_UNAVAILABLE", "Unavailable", 503)
        return {"text": "visible 001" if operation["kind"].endswith("ocr") else "An interpretation",
                "requestedModel": "test/requested", "returnedModel": "test/returned"}


def extract(path, tmp_path, model=None, profile=None):
    output = tmp_path / (path.stem + "-products")
    output.mkdir(exist_ok=True)
    snapshot = {"path": str(path), "filename": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    return get_adapter(path.suffix).extract(snapshot, profile or default_profile(), output, model or Model()), output


def rows(output, table):
    return [json.loads(line) for line in (output / table["typedPath"]).read_text().splitlines()]


def test_registry_is_explicit_and_case_insensitive():
    assert set(enabled_extensions()) == {".csv", ".tsv", ".xlsx", ".xlsm", ".pdf"}
    assert get_adapter("CSV") is get_adapter(".csv")
    with pytest.raises(ServiceError, match="xls"):
        get_adapter(".xls")


@pytest.mark.parametrize("extension,delimiter", [("csv", ","), ("tsv", "\t")])
def test_delimited_preserves_lexemes_headers_and_irregular_rows(tmp_path, extension, delimiter):
    path = tmp_path / f"source.{extension}"
    expected = [["same", "same", ""], ["001", "1.2300", ""], ["a\nb"], [], ["=1+1", "NaN", "", "extra"]]
    with path.open("w", newline="") as handle:
        csv.writer(handle, delimiter=delimiter).writerows(expected)
    result, output = extract(path, tmp_path)
    table = result["tables"][0]
    actual = rows(output, table)
    assert [[c["value"] for c in row["cells"]] for row in actual] == expected
    assert [row["index"] for row in actual] == [1, 2, 3, 4, 5]
    assert actual[2]["source"]["lineStart"] == 3
    assert actual[2]["source"]["lineEnd"] == 4
    assert all(c["type"] == "text" for row in actual for c in row["cells"])
    assert [c["id"] for c in table["columns"]] == ["col_1", "col_2", "col_3", "col_4"]
    assert table["rows"] == 5
    with (output / table["csvPath"]).open(newline="") as handle:
        assert list(csv.reader(handle)) == expected
    assert result["coverage"]["completed"] == 5
    assert any(f["code"] == "IRREGULAR_ROWS" for f in result["findings"])


def workbook(path, sheet_xml=None):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet_xml = sheet_xml or f'''<worksheet xmlns="{ns}"><sheetData>
      <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t></t></is></c></row>
      <row r="3"><c r="A3"><v>12345678901234567890.12300</v></c><c r="C3" s="1"><v>60.500</v></c>
      <c r="D3"><f>A3*2</f><v>24691357802469135780.24600</v></c><c r="E3"><f>NOW()</f></c>
      <c r="F3" t="b"><v>0</v></c><c r="G3" t="e"><v>#DIV/0!</v></c><c r="H3"/>
      <c r="I3" t="str"><f>""</f><v/></c></row>
      </sheetData></worksheet>'''
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", f'''<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><workbookPr date1904="1"/><sheets><sheet name="Hidden Data" sheetId="1" state="hidden" r:id="rId1"/></sheets></workbook>''')
        archive.writestr("xl/_rels/workbook.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>''')
        archive.writestr("xl/styles.xml", f'''<styleSheet xmlns="{ns}"><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>''')
        archive.writestr("xl/sharedStrings.xml", f'''<sst xmlns="{ns}"><si><r><t>duplicate </t></r><r><t>label</t></r></si></sst>''')
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


@pytest.mark.parametrize("extension", ["xlsx", "xlsm"])
def test_workbook_preserves_sparse_cells_precision_dates_formula_cache_and_hidden_sheets(tmp_path, extension):
    path = tmp_path / f"book.{extension}"
    workbook(path)
    result, output = extract(path, tmp_path)
    table = result["tables"][0]
    actual = rows(output, table)
    assert table["source"]["visibility"] == "hidden"
    assert table["dateSystem"] == "1904"
    assert actual[0]["cells"][0]["value"] == "duplicate label"
    assert actual[0]["cells"][1]["value"] == ""
    assert actual[1]["source"]["row"] == 3
    cells = actual[1]["cells"]
    assert cells[0]["value"] == "12345678901234567890.12300"
    assert cells[1]["type"] == "absent"
    assert cells[2]["type"] == "date" and cells[2]["serial"] == "60.500"
    assert cells[3]["formula"] == "A3*2"
    assert cells[3]["cached"]["value"] == "24691357802469135780.24600"
    assert cells[4]["cached"] is None
    assert cells[5]["value"] is False
    assert cells[6]["type"] == "error"
    assert cells[7]["type"] == "null"
    assert cells[8]["cached"] == {"type": "text", "value": ""}
    assert cells[2]["source"]["cell"] == "C3"
    assert "embedded pictures and charts" in result["coverage"]["excluded"]


def test_workbook_rejects_entities_and_expansion_limit(tmp_path):
    path = tmp_path / "hostile.xlsx"
    workbook(path, '<!DOCTYPE worksheet [<!ENTITY x "boom">]><worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&x;</t></is></c></row></sheetData></worksheet>')
    with pytest.raises(ServiceError):
        extract(path, tmp_path)
    workbook(path)
    profile = default_profile()
    profile["limits"]["expandedBytes"] = 10
    with pytest.raises(ServiceError) as exc:
        extract(path, tmp_path, profile=profile)
    assert exc.value.code == "RESOURCE_LIMIT"


def test_row_limit_is_explicit(tmp_path):
    path = tmp_path / "input.csv"
    path.write_text("a\nb\nc\n")
    profile = default_profile()
    profile["limits"]["rows"] = 2
    with pytest.raises(ServiceError) as exc:
        extract(path, tmp_path, profile=profile)
    assert exc.value.code == "RESOURCE_LIMIT"


def make_pdf(path, scanned=False, crop_box=None):
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    pdf = canvas.Canvas(str(path), pagesize=(300, 400))
    if crop_box:
        pdf.setCropBox(crop_box)
    if scanned:
        image = Image.new("RGB", (200, 200), "white")
        ImageDraw.Draw(image).text((20, 20), "visible 001", fill="black")
        pdf.drawImage(ImageReader(image), 20, 100, width=200, height=200)
    else:
        pdf.drawString(20, 370, "Native report")
        for x in (20, 120, 220):
            pdf.line(x, 250, x, 330)
        for y in (250, 290, 330):
            pdf.line(20, y, 220, y)
        pdf.drawString(25, 305, "Code")
        pdf.drawString(125, 305, "Value")
        pdf.drawString(25, 265, "001")
        pdf.drawString(125, 265, "1.2300")
        pdf.drawString(20, 60, "Figure 1: vector diagram")
        pdf.circle(100, 130, 20)
    pdf.showPage()
    pdf.save()


def test_pdf_native_evidence_and_separate_figure_ocr(tmp_path):
    path = tmp_path / "native.pdf"
    make_pdf(path)
    model = Model()
    result, output = extract(path, tmp_path, model)
    table = result["tables"][0]
    assert rows(output, table)[0]["cells"][0]["value"] == "Code"
    assert table["source"]["page"] == 1
    assert table["source"]["bounds"]["units"] == "pt"
    figure = result["figures"][0]
    assert figure["source"]["page"] == 1
    assert figure["detection"] == "caption-page"
    assert (output / figure["ocrPath"]).read_text() == "visible 001"
    assert (output / figure["interpretationPath"]).read_text() == "An interpretation"
    assert {call[0]["kind"] for call in model.calls} == {"figure-ocr", "figure-description"}
    assert result["coverage"]["completed"] == 1
    evidence_product = next(p for p in result["products"] if p["kind"] == "page-evidence")
    evidence = json.loads((output / evidence_product["path"]).read_text())
    assert evidence["words"] and evidence["source"]["bounds"]["pageWidth"] == 300


def test_pdf_scanned_ocr_failures_keep_detected_figures_and_partial_coverage(tmp_path):
    path = tmp_path / "scanned.pdf"
    make_pdf(path, scanned=True)
    model = Model(fail=True)
    result, output = extract(path, tmp_path, model)
    assert result["coverage"]["failed"] == 1
    assert result["coverage"]["completed"] == 0
    assert {call[0]["kind"] for call in model.calls} >= {"page-ocr", "figure-ocr"}
    figure = result["figures"][0]
    assert figure["outcome"] == "partial"
    assert figure["ocrStatus"] == "failed"
    assert (output / figure["imagePath"]).is_file()
    assert (output / figure["ocrPath"]).is_file()
    assert result["findings"]


def test_pdf_detector_is_replaceable(tmp_path):
    from preprocessing.formats.pdf import PDFAdapter
    path = tmp_path / "report.pdf"
    make_pdf(path)
    class Detector:
        version = "fixture-detector"
        limitations = "fixture only"
        def detect(self, page, text):
            return [{"bbox": [80, 240, 130, 290], "caption": "crop", "method": "fixture"}]
    output = tmp_path / "custom"
    output.mkdir()
    result = PDFAdapter(figure_detector=Detector()).extract(
        {"path": str(path), "filename": path.name}, default_profile(), output, Model())
    assert result["figures"][0]["detection"] == "fixture"
    assert result["figures"][0]["source"]["bounds"]["bbox"] == [80, 240, 130, 290]


def test_pdf_crop_box_does_not_shift_media_box_figure_evidence(tmp_path):
    from PIL import Image
    path = tmp_path / "cropped.pdf"
    make_pdf(path, crop_box=(50, 60, 250, 350))
    result, output = extract(path, tmp_path)
    figure = result["figures"][0]
    with Image.open(output / figure["imagePath"]) as image:
        assert image.width / image.height == pytest.approx(300 / 400)


def test_scanned_pdf_structured_ocr_preserves_table_lexemes_and_model_provenance(tmp_path):
    path = tmp_path / "scan.pdf"
    make_pdf(path, scanned=True)
    class StructuredModel(Model):
        def model(self, operation, input_refs):
            response = super().model(operation, input_refs)
            if operation["kind"] == "page-ocr":
                response["text"] = json.dumps({"text": "Code Value\n001 1.2300", "tables": [{"rows": [["Code", "Value"], ["001", "1.2300"]]}]})
            return response
    result, output = extract(path, tmp_path, StructuredModel())
    assert result["coverage"]["completed"] == 1
    table = result["tables"][0]
    assert table["evidenceBasis"] == "model-transcription"
    assert table["source"]["bounds"] is None
    assert rows(output, table)[1]["cells"][0]["value"] == "001"
    assert table["model"]["returnedModel"] == "test/returned"


def test_pdf_resource_limits_do_not_become_partial_ocr_results(tmp_path):
    path = tmp_path / "many.pdf"
    make_pdf(path, scanned=True)
    class StructuredModel(Model):
        def model(self, operation, input_refs):
            return {"text": json.dumps({"text": "rows", "tables": [{"rows": [["1"], ["2"]]}, {"rows": [["3"]]}]})}
    profile = default_profile()
    profile["limits"]["rows"] = 2
    with pytest.raises(ServiceError) as exc:
        extract(path, tmp_path, StructuredModel(), profile)
    assert exc.value.code == "RESOURCE_LIMIT"


@pytest.mark.parametrize("case", ["csv", "tsv", "xlsx", "xlsm", "pdf", "pdf-no-figures", "pdf-failed-ocr"])
def test_adapter_stages_publish_success_or_partial_without_contract_drift(tmp_path, case):
    from preprocessing.indexes import build_indexes
    from preprocessing.results import make_manifest

    extension = case.split("-")[0]
    source = tmp_path / f"input.{extension}"
    if extension in {"csv", "tsv"}:
        source.write_text("001,1.2300\n" if extension == "csv" else "001\t1.2300\n")
    elif extension in {"xlsx", "xlsm"}:
        workbook(source)
    elif case == "pdf-no-figures":
        from reportlab.pdfgen import canvas
        pdf = canvas.Canvas(str(source))
        pdf.drawString(30, 300, "Native text without figures")
        pdf.showPage()
        pdf.save()
    else:
        make_pdf(source, scanned=case == "pdf-failed-ocr")
    model = Model(fail=case == "pdf-failed-ocr")
    extraction, output = extract(source, tmp_path, model)
    profile = default_profile()
    build_indexes(extraction, output, profile["limits"])
    manifest = make_manifest({"id": "job_fixture", "profile": profile, "source": {"filename": source.name}}, extraction, output)
    assert manifest["outcome"] == ("partial" if case == "pdf-failed-ocr" else "succeeded")
    stages = {stage["id"]: stage for stage in manifest["stages"]}
    assert stages["index-and-guide"]["state"] == "completed"
    for stage in stages.values():
        assert stage["required"] is True
        assert stage["state"] in {"completed", "partial", "failed", "not_applicable"}
        assert stage["coverage"] is None or isinstance(stage["coverage"], dict)
        if stage["state"] == "not_applicable":
            assert stage["reason"]
    if extension != "pdf":
        assert not model.calls
        assert stages["native-tables"]["state"] == "completed"
        assert stages["native-tables"]["coverage"] == extraction["coverage"]
        assert stages["model-extraction"]["state"] == "not_applicable"
    elif case == "pdf-no-figures":
        assert not model.calls
        assert stages["page-ocr"]["state"] == "not_applicable"
        assert stages["figure-ocr"]["state"] == "not_applicable"
        assert stages["figure-description"]["state"] == "not_applicable"
    elif case == "pdf-failed-ocr":
        assert stages["figure-ocr"]["state"] == "failed"
        assert stages["figure-ocr"]["coverage"]["failed"] == 1


def test_composite_caption_page_is_one_coherent_figure_with_labels(tmp_path):
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    path = tmp_path / "composite.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(300, 400))
    for x, y, width, height, color in [(60, 160, 100, 90, "red"), (120, 200, 90, 90, "blue"), (240, 310, 2, 3, "green")]:
        pdf.drawImage(ImageReader(Image.new("RGB", (20, 20), color)), x, y, width=width, height=height)
    pdf.drawString(20, 350, "Legend: red zone, blue zone")
    pdf.drawString(20, 50, "Figure 1. Composite map")
    pdf.showPage()
    pdf.save()
    model = Model()
    result, output = extract(path, tmp_path, model)
    assert len(result["figures"]) == 1
    figure = result["figures"][0]
    assert figure["detection"] == "caption-page"
    assert figure["source"]["bounds"]["bbox"] == [0, 0, 300, 400]
    assert [operation["kind"] for operation, _ in model.calls] == ["figure-ocr", "figure-description"]
    with Image.open(output / figure["imagePath"]) as image:
        assert image.size == (600, 800)
        # Native legend ink must be in the supplied image, outside fragment bounds.
        assert image.crop((40, 75, 450, 110)).convert("L").getextrema()[0] < 100
    evidence = next(p for p in result["products"] if p["kind"] == "figure-detection")
    audit = json.loads((output / evidence["path"]).read_text())
    assert len(audit["images"]) == 3
    assert {item["disposition"] for item in audit["images"]} == {"subsumed-caption-page"}


def test_uncaptioned_fragments_group_before_size_filter_and_exclusions_are_recorded(tmp_path):
    import pdfplumber
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    from preprocessing.formats.pdf import NativeFigureDetector

    path = tmp_path / "fragments.pdf"
    pdf = canvas.Canvas(str(path), pagesize=(300, 400))
    pdf.drawString(20, 350, "Map without a caption")
    for x, y, width, height, color in [(20, 160, 80, 80, "red"), (90, 160, 80, 80, "blue"),
                                     (160, 180, 2, 3, "green"), (240, 310, 2, 3, "black")]:
        pdf.drawImage(ImageReader(Image.new("RGB", (20, 20), color)), x, y, width=width, height=height)
    pdf.showPage()
    pdf.save()
    result, output = extract(path, tmp_path)
    assert len(result["figures"]) == 1
    assert result["figures"][0]["source"]["bounds"]["bbox"] == [20, 160, 170, 240]
    assert any(f["code"] == "FIGURE_DETECTOR_EXCLUSIONS" for f in result["findings"])
    evidence = next(p for p in result["products"] if p["kind"] == "figure-detection")
    audit = json.loads((output / evidence["path"]).read_text())
    assert [item["disposition"] for item in audit["images"]].count("excluded-tiny") == 1
    assert audit["excludedCount"] == 1
    with pdfplumber.open(path) as document:
        assert len(NativeFigureDetector(min_dimension_points=0, group_overlaps=False).detect(document.pages[0], "")) == 4
    profile = default_profile()
    profile["options"]["figureDetector"] = {"minDimensionPoints": 0, "groupOverlaps": False}
    overridden, _ = extract(path, tmp_path, profile=profile)
    assert len(overridden["figures"]) == 4
