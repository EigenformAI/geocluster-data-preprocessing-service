import asyncio
import csv
import io
import json
import runpy
import socket
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fakes import FakeEngine
from test_formats import extract

from preprocessing.api import create_app
from preprocessing.contracts import default_profile
from preprocessing.errors import ServiceError
from preprocessing.service import Service

fixture = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/generate_word_fixture.py"))
write_fixture = fixture["write_fixture"]
PROSE = fixture["PROSE"]
PROSE_XML = f"<w:p><w:r><w:t>{PROSE}</w:t></w:r></w:p>"


def jsonl(text):
    return [json.loads(line) for line in text.split("\n") if line]


class NoModelEngine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def run(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("Word extraction must not ask for a model.")


@pytest.fixture
async def http_worker(tmp_path):
    workspace = tmp_path / "inputs"
    workspace.mkdir()
    engine = NoModelEngine()
    profile = default_profile()
    profile["limits"]["outputBytes"] = 64 * 1024
    profile["limits"]["expandedBytes"] = 96 * 1024
    service = Service(tmp_path / "data", workspace, profile=profile, engine=engine)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(create_app(service), log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    assert not serving.done()
                    await asyncio.sleep(.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False) as client:
                caps = (await client.get("/preprocessing/capabilities")).raise_for_status().json()
                assert ".docx" in caps["extensions"]
                assert not {".doc", ".docm"} & set(caps["extensions"])
                client.headers["X-Processing-Store-Scope"] = caps["storeScope"]
                yield client, workspace, service
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 10)
            assert engine.calls == 0


async def submit(client, filename):
    reply = await client.post("/preprocessing/jobs", headers={"Idempotency-Key": str(uuid4())},
                              json={"source": {"kind": "workspace-file", "path": filename}})
    assert reply.status_code == 202, reply.text
    location = reply.headers["location"]
    async with asyncio.timeout(10):
        while True:
            job = (await client.get(location)).raise_for_status().json()
            if job["state"] not in {"queued", "running"}:
                return location, job
            await asyncio.sleep(.02)


@pytest.mark.parametrize("prose_only", [True, False], ids=["prose-only", "prose-and-table"])
async def test_download_from_word_job(http_worker, prose_only):
    client, workspace, service = http_worker
    path = workspace / "synthetic.DOCX"
    if prose_only:
        write_fixture(path, PROSE_XML)
    else:
        write_fixture(path)
    location, job = await submit(client, path.name)
    assert job["state"] == "succeeded", job
    manifest = (await client.get(location + "/results")).raise_for_status().json()
    assert manifest["outcome"] == "succeeded"
    assert service.store.operations(job["id"]) == []
    assert {stage["id"]: stage["state"] for stage in manifest["stages"]}["model-extraction"] == "not_applicable"
    assert manifest["coverage"]["unit"] == "body-block"
    products = {item["kind"]: item for item in manifest["products"] if not item["kind"].startswith("navigation")}
    source = {"kind": "word-body", "part": "word/document.xml"}
    assert products["document-text"]["source"] == source
    downloaded = (await client.get(products["document-text"]["contentUrl"])).raise_for_status()
    assert downloaded.headers["content-type"].startswith("text/plain")
    expected_paragraphs = {1: PROSE} if prose_only else {1: "Inventory: north café", 3: "End\tof notes."}
    assert downloaded.text == "".join(text + "\n" for text in expected_paragraphs.values())
    inventory = next(item for item in manifest["products"] if item["path"] == "indexes/items_000001.md")
    index = (await client.get(inventory["contentUrl"])).raise_for_status().text
    assert "Word main-body text" in index and "Physical page" not in index
    response = (await client.get(products["document-blocks"]["contentUrl"])).raise_for_status()
    blocks = jsonl(response.text)
    assert [item["source"] for item in blocks] == [
        source | {"block": i} for i in range(1, 2 if prose_only else 4)]
    for block in blocks:
        if block["kind"] == "paragraph":
            start = block["textOffset"]
            assert downloaded.content[start:start + block["textBytes"]] == expected_paragraphs[block["source"]["block"]].encode()
    if prose_only:
        assert not manifest["tables"]
        assert [item["kind"] for item in blocks] == ["paragraph"]
        return
    assert [item["kind"] for item in blocks] == ["paragraph", "table", "paragraph"]
    table, = manifest["tables"]
    assert table["source"] == blocks[1]["source"] and table["id"] == blocks[1]["tableId"]
    assert table["columnSemantics"] == "physical-cell-order"
    rows = (await client.get(table["rowsUrl"], params={"limit": 1, "offset": 1})).raise_for_status().json()["rows"]
    assert [cell["value"] for cell in rows[0]["cells"]] == ["001", "\ncafé"]
    assert rows[0]["cells"][1]["source"] == table["source"] | {"row": 2, "cell": 2}
    export = (await client.get(products["table-export"]["contentUrl"])).raise_for_status()
    assert list(csv.reader(io.StringIO(export.text))) == [["Code", "Label"], ["001", "\ncafé"]]


@pytest.mark.parametrize("case,code", [
    ("broken.docx", "INVALID_INPUT"), ("encoding.docx", "INVALID_INPUT"), ("expanded.docx", "RESOURCE_LIMIT"),
    ("output.docx", "RESOURCE_LIMIT"), ("legacy.doc", "UNSUPPORTED_FORMAT"), ("macro.docm", "UNSUPPORTED_FORMAT"),
])
async def test_worker_reports_typed_word_failures(http_worker, case, code):
    client, workspace, _ = http_worker
    path = workspace / case
    if case == "expanded.docx":
        write_fixture(path, extra=[("word/unused.xml", " " * (97 * 1024))])
    elif case == "encoding.docx":
        write_fixture(path, document='<?xml version="1.0" encoding="unknown-encoding"?><document/>')
    elif case == "output.docx":
        write_fixture(path, "<w:p><w:r><w:t>" + "a" * 65537 + "</w:t></w:r></w:p>")
    else:
        path.write_text("synthetic invalid input")
    location, job = await submit(client, path.name)
    assert job["state"] == "failed", job
    assert job["error"]["code"] == code
    assert (await client.get(location + "/results")).is_error


def test_omissions_do_not_shift_body_positions_or_join_text(tmp_path):
    path = tmp_path / "skipped.docx"
    write_fixture(path, PROSE_XML + """<w:sdt><w:sdtContent><w:p><w:r><w:t>excluded</w:t></w:r></w:p></w:sdtContent></w:sdt>
    <w:p><w:r><w:t xml:space="preserve">Before </w:t><w:drawing/><w:t>after</w:t></w:r></w:p>
    <w:p><w:hyperlink r:id="rId2"><w:r><w:t>link label</w:t></w:r></w:hyperlink>
    <w:r><w:instrText>field instruction</w:instrText><w:t>stored value</w:t></w:r></w:p>""",
        extra=[("word/_rels/document.xml.rels", "not parsed"), ("word/header1.xml", "not parsed")])
    result, output = extract(path, tmp_path)
    assert result["coverage"]["unsupported"] == 2 and result["coverage"]["completed"] == 2
    assert result["stages"][0]["state"] == "partial"
    text = (output / "document/body.txt").read_text()
    assert text == PROSE + "\nBefore \ufffcafter\nlink labelstored value\n"
    blocks = jsonl((output / "document/blocks.jsonl").read_text())
    assert [block["kind"] for block in blocks] == ["paragraph", "omitted", "paragraph", "paragraph"]
    assert [block["source"]["block"] for block in blocks] == [1, 2, 3, 4]
    assert [block["source"]["block"] for block in blocks if block.get("incomplete")] == [2, 3]


def test_strict_word_custom_main_part(tmp_path):
    path = tmp_path / "strict.docx"
    write_fixture(path, PROSE_XML, part="content/main.xml", target="/content/main.xml",
                  word_ns="http://purl.oclc.org/ooxml/wordprocessingml/main",
                  office_ns="http://purl.oclc.org/ooxml/officeDocument/relationships")
    result, output = extract(path, tmp_path)
    assert result["products"][0]["source"]["part"] == "content/main.xml"
    assert (output / "document/body.txt").read_text() == PROSE + "\n"


def test_word_keeps_physical_cells_but_not_nested_tables(tmp_path):
    path = tmp_path / "physical.docx"
    write_fixture(path, """<w:tbl><w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>
      <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Outside</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Inside</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
      <w:p/></w:tc></w:tr></w:tbl>""")
    result, output = extract(path, tmp_path)
    table, = result["tables"]
    row, = jsonl((output / table["typedPath"]).read_text())
    cell, = row["cells"]
    assert cell["value"] == "Outside\n\ufffc\n"
    assert cell["source"]["cell"] == 1 and cell["incomplete"] is True
    assert result["coverage"]["unsupported"] == 1


@pytest.mark.parametrize("parts,code", [
    ({"document": "<!DOCTYPE document><document/>"}, "INVALID_INPUT"),
    ({"target": "https://example.invalid/word.xml", "mode": "External"}, "INVALID_INPUT"),
    ({"content_type": "application/vnd.ms-word.document.macroEnabled.main+xml"}, "UNSUPPORTED_FORMAT"),
    ({"body": "<w:p><w:r><w:drawing/></w:r></w:p>"}, "INVALID_INPUT"),
    ({"body": "<w:sdt>" * 65 + "</w:sdt>" * 65}, "RESOURCE_LIMIT"),
])
def test_rejects_unsupported_or_unusable_word_packages(tmp_path, parts, code):
    path = tmp_path / "negative.docx"
    write_fixture(path, **parts)
    with pytest.raises(ServiceError) as exc:
        extract(path, tmp_path)
    assert exc.value.code == code


@pytest.mark.parametrize("limit", ["rows", "columns"])
def test_word_table_limits(tmp_path, limit):
    path = tmp_path / "limited.docx"
    write_fixture(path)
    profile = default_profile()
    profile["limits"][limit] = 1
    with pytest.raises(ServiceError) as exc:
        extract(path, tmp_path, profile=profile)
    assert exc.value.code == "RESOURCE_LIMIT"


MARKERS = """<w:bookmarkStart w:id="1" w:name="synthetic"/>
<w:commentRangeStart w:id="2"/><w:proofErr w:type="spellStart"/>
<w:permStart w:id="3" w:edGrp="everyone"/><w:permEnd w:id="3"/>
<w:proofErr w:type="spellEnd"/><w:commentRangeEnd w:id="2"/><w:bookmarkEnd w:id="1"/>"""
WORD_NAMESPACES = [fixture["WORD"], "http://purl.oclc.org/ooxml/wordprocessingml/main"]


@pytest.mark.parametrize("word_ns", WORD_NAMESPACES, ids=["transitional", "strict"])
@pytest.mark.parametrize("context", ["body", "p", "hyperlink", "fldSimple", "tbl", "tr", "tc"])
def test_leaf_annotations_preserve_text_and_physical_positions(tmp_path, word_ns, context):
    # All these containers admit range markup. Runs do not.
    text = "<w:r><w:t>café</w:t></w:r>"
    paragraph = f"<w:p>{text}</w:p>"
    cell = f"<w:tc>{paragraph}</w:tc>"
    row = f"<w:tr>{cell}</w:tr>"
    bodies = {
        "body": MARKERS + paragraph + f"<w:tbl>{row}</w:tbl>",
        "p": f"<w:p>{MARKERS}{text}</w:p>",
        "hyperlink": f"<w:p><w:hyperlink>{MARKERS}{text}</w:hyperlink></w:p>",
        "fldSimple": f'<w:p><w:fldSimple w:instr="DATE">{MARKERS}{text}</w:fldSimple></w:p>',
        "tbl": f"<w:tbl><w:tblPr/><w:tblGrid/>{MARKERS}{row}</w:tbl>",
        "tr": f"<w:tbl><w:tr>{MARKERS}{cell}</w:tr></w:tbl>",
        "tc": f"<w:tbl><w:tr><w:tc>{MARKERS}{paragraph}</w:tc></w:tr></w:tbl>",
    }
    path = tmp_path / "annotations.docx"
    write_fixture(path, bodies[context], word_ns=word_ns)
    result, output = extract(path, tmp_path)
    assert result["coverage"]["total"] == (2 if context == "body" else 1)
    assert result["coverage"]["unsupported"] == 0
    assert result["stages"][0]["state"] == "completed" and result["findings"] == []
    blocks = jsonl((output / "document/blocks.jsonl").read_text())
    assert [block["source"]["block"] for block in blocks] == ([9, 10] if context == "body" else [1])
    assert not any(block.get("incomplete") for block in blocks)
    prose = (output / "document/body.txt").read_bytes()
    assert prose == (b"" if context in {"tbl", "tr", "tc"} else "café\n".encode())
    if prose:
        assert blocks[0]["textOffset"] == 0 and blocks[0]["textBytes"] == len("café".encode())
    if result["tables"]:
        table, = result["tables"]
        assert table["id"] == ("word_table_000010" if context == "body" else "word_table_000001")
        typed_row, = jsonl((output / table["typedPath"]).read_text())
        assert typed_row["cells"] == [{"type": "text", "value": "café",
                                      "source": table["source"] | {"row": 1, "cell": 1}}]
        assert (output / table["csvPath"]).read_bytes() == "café\r\n".encode()


@pytest.mark.parametrize("word_ns", WORD_NAMESPACES, ids=["transitional", "strict"])
async def test_bookmarked_text_finishes_through_http(http_worker, word_ns):
    client, workspace, _ = http_worker
    write_fixture(workspace / "bookmarked.docx", MARKERS + PROSE_XML, word_ns=word_ns)
    location, job = await submit(client, "bookmarked.docx")
    assert job["state"] == "succeeded"
    manifest = (await client.get(location + "/results")).raise_for_status().json()
    assert manifest["outcome"] == "succeeded" and manifest["findings"] == []
    assert manifest["coverage"]["total"] == manifest["coverage"]["completed"] == 1
    assert manifest["coverage"]["unsupported"] == 0
    products = {p["kind"]: p for p in manifest["products"]}
    text = (await client.get(products["document-text"]["contentUrl"])).raise_for_status().content
    assert text == (PROSE + "\n").encode()
    blocks = jsonl((await client.get(products["document-blocks"]["contentUrl"])).raise_for_status().text)
    assert blocks == [{"kind": "paragraph", "source": {"kind": "word-body", "part": "word/document.xml", "block": 9},
                       "textOffset": 0, "textBytes": len(PROSE.encode())}]


def test_run_annotations_do_not_insert_gaps(tmp_path):
    path = tmp_path / "comments.docx"
    write_fixture(path, """<w:p><w:r><w:t>Before</w:t><w:commentReference w:id="1"/>
      <w:lastRenderedPageBreak/><w:t>after</w:t></w:r></w:p>""")
    result, output = extract(path, tmp_path)
    assert (output / "document/body.txt").read_text() == "Beforeafter\n"
    assert result["coverage"]["completed"] == 1 and result["findings"] == []


@pytest.mark.parametrize("node", [
    '<x:bookmarkStart xmlns:x="urn:private-synthetic"/>',
    '<w:bookmarkStart w:id="1"><w:r><w:t>secret</w:t></w:r></w:bookmarkStart>',
    '<w:commentRangeStart w:id="1">secret</w:commentRangeStart>',
    '<w:proofErr w:type="spellStart"><w:drawing/></w:proofErr>',
    '<w:permEnd w:id="1">secret</w:permEnd>',
    '<w:ins><w:r><w:t>secret</w:t></w:r></w:ins>',
    '<w:moveFrom><w:r><w:t>secret</w:t></w:r></w:moveFrom>',
    '<w:moveFromRangeStart w:id="1"/>',
    '<w:customXmlInsRangeStart w:id="1"/>',
    '<w:sdt><w:sdtContent><w:r><w:drawing/><w:t>secret</w:t></w:r></w:sdtContent></w:sdt>',
    ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
     '<mc:Fallback><w:r><w:t>secret</w:t></w:r></mc:Fallback></mc:AlternateContent>'),
])
@pytest.mark.parametrize("context", ["body", "p", "tbl", "tr", "tc"])
def test_non_annotations_stay_opaque_and_incomplete(tmp_path, node, context):
    paragraph = "<w:p><w:r><w:t>kept</w:t></w:r></w:p>"
    cell = f"<w:tc>{paragraph}</w:tc>"
    bodies = {
        "body": node + paragraph,
        "p": f"<w:p><w:r><w:t>Before</w:t></w:r>{node}<w:r><w:t>after</w:t></w:r></w:p>",
        "tbl": f"<w:tbl>{node}<w:tr>{cell}</w:tr></w:tbl>",
        "tr": f"<w:tbl><w:tr>{node}{cell}</w:tr></w:tbl>",
        "tc": f"<w:tbl><w:tr><w:tc>{paragraph}{node}{paragraph}</w:tc></w:tr></w:tbl>",
    }
    path = tmp_path / "opaque.docx"
    write_fixture(path, bodies[context])
    result, output = extract(path, tmp_path)
    assert result["coverage"]["unsupported"] == 1 and result["stages"][0]["state"] == "partial"
    blocks = jsonl((output / "document/blocks.jsonl").read_text())
    assert blocks[0]["incomplete"] is True
    assert blocks[0]["kind"] == ("omitted" if context == "body" else "paragraph" if context == "p" else "table")
    note, = result["findings"]
    assert note["code"] == "WORD_CONTENT_OMITTED" and note["source"] == blocks[0]["source"]
    assert note["message"] == ("Unsupported content was skipped in this body block." +
                               ("" if context == "body" else " Stored text was retained."))
    if context in {"body", "p"}:
        assert (output / "document/body.txt").read_text() == ("kept\n" if context == "body" else "Before\ufffcafter\n")
    else:
        table, = result["tables"]
        row, = jsonl((output / table["typedPath"]).read_text())
        cell, = row["cells"]
        assert cell["value"] == ("kept\n\ufffc\nkept" if context == "tc" else "kept")
        assert bool(cell.get("incomplete")) == (context == "tc")


@pytest.mark.parametrize("node", [
    '<w:bookmarkStart w:id="1"/>', '<w:proofErr w:type="spellStart"/>',
    '<w:commentReference w:id="1"><w:t>secret</w:t></w:commentReference>',
    '<w:lastRenderedPageBreak>secret</w:lastRenderedPageBreak>',
    '<x:commentReference xmlns:x="urn:private-synthetic" x:id="1"/>',
])
def test_misplaced_or_content_bearing_run_markers_are_omitted(tmp_path, node):
    path = tmp_path / "run-marker.docx"
    write_fixture(path, f"<w:p><w:r><w:t>Before</w:t>{node}<w:t>after</w:t></w:r></w:p>")
    result, output = extract(path, tmp_path)
    assert (output / "document/body.txt").read_text() == "Before\ufffcafter\n"
    assert result["coverage"]["unsupported"] == 1 and len(result["findings"]) == 1


@pytest.mark.parametrize("body,expected", [
    ('<w:p><w:r><w:drawing/></w:r></w:p>', "Drawing content was skipped in this body block."),
    ('<w:p><w:r><w:t>kept</w:t><w:drawing/><w:drawing/></w:r></w:p>',
     "Drawing content was skipped in this body block. Stored text was retained."),
    ('<w:p><w:r><w:t>kept</w:t><w:drawing/><w:object/></w:r></w:p>',
     "Unsupported content was skipped in this body block. Stored text was retained."),
    ('<w:p><w:r><w:t> </w:t><w:tab/><w:drawing/></w:r></w:p>',
     "Drawing content was skipped in this body block."),
    (('<w:tbl><w:tr><w:tc><w:p><w:r><w:drawing/></w:r></w:p></w:tc>'
      '<w:tc><w:p><w:r><w:t>kept</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'),
     "Drawing content was skipped in this body block. Stored text was retained."),
    ('<w:p><w:sdt><w:sdtContent><w:r><w:t>hidden</w:t></w:r></w:sdtContent></w:sdt></w:p>',
     "Unsupported content was skipped in this body block."),
])
def test_omission_feedback_is_once_per_block_and_describes_retained_text(tmp_path, body, expected):
    path = tmp_path / "feedback.docx"
    write_fixture(path, PROSE_XML + body)
    result, _ = extract(path, tmp_path)
    assert result["coverage"]["total"] == 2 and result["coverage"]["unsupported"] == 1
    note, = result["findings"]
    assert note["message"] == expected
    assert note["source"]["block"] == 2


@pytest.mark.parametrize("body", [MARKERS, MARKERS + "<w:p/>",
                               MARKERS + "<w:p><w:r><w:drawing/></w:r></w:p>"])
def test_annotations_are_not_usable_evidence(tmp_path, body):
    path = tmp_path / "empty.docx"
    write_fixture(path, body)
    with pytest.raises(ServiceError) as exc:
        extract(path, tmp_path)
    assert exc.value.code == "INVALID_INPUT"


def test_annotation_only_paragraph_retains_empty_record(tmp_path):
    path = tmp_path / "empty-paragraph.docx"
    write_fixture(path, PROSE_XML + f"<w:p>{MARKERS}</w:p>")
    result, output = extract(path, tmp_path)
    assert result["coverage"]["completed"] == result["coverage"]["total"] == 2
    assert result["findings"] == []
    blocks = jsonl((output / "document/blocks.jsonl").read_text())
    assert blocks[1] == {"kind": "paragraph", "source": blocks[0]["source"] | {"block": 2},
                         "textOffset": len((PROSE + "\n").encode()), "textBytes": 0}
    assert (output / "document/body.txt").read_text() == PROSE + "\n\n"


@pytest.mark.parametrize("word_ns,other_ns", [(WORD_NAMESPACES[0], WORD_NAMESPACES[1]),
                                          (WORD_NAMESPACES[1], WORD_NAMESPACES[0])])
def test_annotations_must_match_the_document_namespace(tmp_path, word_ns, other_ns):
    path = tmp_path / "mixed-namespace.docx"
    write_fixture(path, f'<x:bookmarkEnd xmlns:x="{other_ns}" x:id="1"/>' + PROSE_XML, word_ns=word_ns)
    result, output = extract(path, tmp_path)
    assert result["coverage"]["total"] == 2 and result["coverage"]["unsupported"] == 1
    assert jsonl((output / "document/blocks.jsonl").read_text())[0]["kind"] == "omitted"


def test_findings_cap_keeps_all_omission_locations(tmp_path):
    path = tmp_path / "many-drawings.docx"
    write_fixture(path, MARKERS + PROSE_XML + '<w:p><w:r><w:drawing/></w:r></w:p>' * 105)
    result, output = extract(path, tmp_path)
    assert result["coverage"]["total"] == 106 and result["coverage"]["unsupported"] == 105
    assert len(result["findings"]) == 100 and result["provenance"]["omittedFindings"] == 5
    assert {note["message"] for note in result["findings"]} == {"Drawing content was skipped in this body block."}
    blocks = jsonl((output / "document/blocks.jsonl").read_text())
    assert [b["source"]["block"] for b in blocks if b.get("incomplete")] == list(range(10, 115))
