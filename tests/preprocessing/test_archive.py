import asyncio
import csv
import gzip
import hashlib
import io
import random
import secrets
import socket
import struct
import tarfile
import zipfile
from pathlib import PurePosixPath

import httpx
import pytest
import uvicorn
from test_word import NoModelEngine, submit, write_fixture

from preprocessing import results
from preprocessing.api import create_app
from preprocessing.archive_limits import Budget
from preprocessing.contracts import default_profile
from preprocessing.errors import ServiceError
from preprocessing.formats import archive_readers
from preprocessing.formats.archive import process_member
from preprocessing.formats.archive_readers import Member, tar_members, zip_members
from preprocessing.service import Service


def archive(path, entries, *, pax_headers=None):
    name = path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for member, data in entries:
                output.writestr(member, data)
    else:
        mode = "w:gz" if name.endswith((".tgz", ".tar.gz")) else "w"
        with tarfile.open(path, mode, pax_headers=pax_headers) as output:
            for member, data in entries:
                entry = member if isinstance(member, tarfile.TarInfo) else tarfile.TarInfo(member)
                entry.size = len(data)
                output.addfile(entry, io.BytesIO(data))


@pytest.fixture
async def archive_worker(tmp_path, request):
    workspace = tmp_path / "inputs"
    workspace.mkdir()
    engine = NoModelEngine()
    profile = default_profile()
    if getattr(request, "param", None) != "default":
        profile["limits"].update(expandedBytes=16 * 1024, outputBytes=32 * 1024, rows=8)
    service = Service(tmp_path / "data", workspace, profile=profile, engine=engine)
    token = secrets.token_urlsafe()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(create_app(service, token=token), log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    assert not serving.done()
                    await asyncio.sleep(.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                                         trust_env=False) as client:
                assert (await client.get("/preprocessing/capabilities")).status_code == 401
                client.headers["Authorization"] = f"Bearer {token}"
                caps = (await client.get("/preprocessing/capabilities")).raise_for_status().json()
                client.headers["X-Processing-Store-Scope"] = caps["storeScope"]
                yield client, workspace, service, caps
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 10)
            assert engine.calls == 0


async def test_advertised_formats(archive_worker):
    *_, caps = archive_worker
    extensions = set(caps["extensions"])
    assert {".zip", ".tar", ".tar.gz", ".tgz", ".csv", ".docx", ".xlsx", ".pdf"} <= extensions
    assert not {".gz", ".7z", ".rar"} & extensions


async def inventory(client, manifest):
    product, = [item for item in manifest["products"] if item["kind"] == "archive-inventory"]
    return (await client.get(product["contentUrl"])).raise_for_status().json()


MIB = 1024 * 1024
OBSERVATION = (b"Loam at the north station after morning rain. " * 50)[:2043]


def bulk_csv_zip(path, entries):
    row = b"001," + OBSERVATION + b"\n"
    assert len(row) == 2048
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, data in entries:
            if isinstance(data, bytes):
                output.writestr(name, data)
            else:
                with output.open(name, "w") as member:
                    for _ in range(data * 8):
                        member.write(row * 64)


async def assert_failed_archive_replay(client, filename, location, job):
    replay = await client.post("/preprocessing/jobs", headers={"Idempotency-Key": job["submissionKey"]},
                               json={"source": {"kind": "workspace-file", "path": filename}})
    assert (replay.status_code, replay.headers.get("Location")) == (200, location)
    assert replay.json()["id"] == job["id"]
    assert replay.json()["state"] == "failed" and replay.json()["source"] == job["source"]
    assert (await client.get(location + "/results")).status_code == 409
    assert len((await client.get("/preprocessing/jobs")).raise_for_status().json()["jobs"]) == 1


@pytest.mark.parametrize("archive_worker", ["default"], indirect=True)
async def test_regular_csv_over_eight_mib_publishes_typed_rows(archive_worker):
    client, workspace, _, _ = archive_worker
    path = workspace / "survey.zip"
    bulk_csv_zip(path, [("east/observations.csv", 12)])
    _, job = await submit(client, path.name)
    assert job["state"] == "succeeded", job["error"]
    manifest = (await client.get(job["resultUrl"])).raise_for_status().json()
    member, = (await inventory(client, manifest))["members"]
    table, = manifest["tables"]
    assert member["bytes"] == 12 * MIB and member["tableIds"] == [table["id"]]
    assert table["rows"] == 12 * MIB // 2048
    page = (await client.get(table["rowsUrl"], params={"offset": table["rows"] - 1, "limit": 1})).raise_for_status()
    row, = page.json()["rows"]
    assert row["source"]["archiveMember"] == {"id": member["id"], "name": "east/observations.csv"}
    assert [(cell["type"], cell["value"]) for cell in row["cells"]] == [
        ("text", "001"), ("text", OBSERVATION.decode())]


@pytest.mark.parametrize("size,reason", [
    (64 * MIB, "staged and result files"), (64 * MIB + 1, "member exceeds its staging-byte limit"),
])
def test_archive_member_gate_is_inclusive_and_separate_from_storage(tmp_path, size, reason):
    # No ZIP framing or aggregate archive check here. A zero storage budget
    # distinguishes reaching staging from rejection by the declared-size gate.
    member = Member("samples.csv", "file", size, [b"001,north\n"])
    with pytest.raises(ServiceError, match=reason) as error:
        process_member(member, None, {"id": "member_0001"}, tmp_path, None, {}, {"outputBytes": 0}, 0)
    assert error.value.code == "RESOURCE_LIMIT"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("archive_worker", ["default"], indirect=True)
@pytest.mark.parametrize("entries,reason", [
    ([("observations.csv", 64)], "staged and result files"),
    ([("station.csv", b"code,label\n001,north\n"), ("observations.csv", 63)], "staged and result files"),
    ([("east.csv", 33), ("west.csv", 32)], "expanded/consumed byte limit"),
], ids=["at-expansion-limit", "near-limit-with-sibling", "over-aggregate"])
async def test_hard_archive_byte_ceilings_refuse_without_results(archive_worker, entries, reason):
    client, workspace, service, _ = archive_worker
    path = workspace / "survey.zip"
    bulk_csv_zip(path, entries)
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    location, job = await submit(client, path.name)
    assert job["state"] == "failed" and job["error"]["code"] == "RESOURCE_LIMIT", job
    assert reason in job["error"]["message"]
    assert (await client.get(location + "/results")).status_code == 409
    assert "resultId" not in job
    directory = service.private / "jobs" / job["id"]
    assert not any((directory / name).exists() for name in ("archive-scratch", "staging", "sealed"))
    assert job["source"]["sha256"] == original
    retained = service.private / "sources" / job["source"]["snapshotId"] / "source"
    assert hashlib.sha256(retained.read_bytes()).hexdigest() == original
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    if entries[-1][1] == 63:
        await assert_failed_archive_replay(client, path.name, location, job)


@pytest.mark.parametrize("suffix,format", [
    ("ZIP", "zip"), ("TAR", "tar"), ("Tar.Gz", "tar.gz"), ("TAR.GZ", "tar.gz"), ("TGZ", "tar.gz")])
async def test_archive_job_downloads_typed_tables_and_preserves_paths(archive_worker, suffix, format):
    client, workspace, service, _ = archive_worker
    folder = workspace / "samples" / "season"
    folder.mkdir(parents=True)
    path = folder / f"SURVEY.{suffix}"
    archive(path, [("east/samples.csv", b"code,label\n001,north\n"),
                   ("west/samples.csv", b"code,label\n002,south\n"),
                   ("stations.TsV", "id\tname\n003\tcafé\n".encode())])
    sibling = folder / "untouched.csv"
    sibling.write_text("unrelated")
    original = path.read_bytes()
    relative = path.relative_to(workspace).as_posix()
    _, job = await submit(client, relative)
    assert job["state"] == "succeeded", job
    manifest = (await client.get(job["resultUrl"])).raise_for_status().json()
    listing = await inventory(client, manifest)
    assert listing["format"] == format
    assert manifest["source"]["filename"] == path.name
    assert job["requestedSource"]["path"] == relative
    assert manifest["source"]["origin"]["path"] == relative
    assert [(m["name"], m["status"]) for m in listing["members"]] == [
        ("east/samples.csv", "processed"), ("west/samples.csv", "processed"), ("stations.TsV", "processed")]
    assert len({table["id"] for table in manifest["tables"]}) == 3
    previews = [item for item in manifest["products"] if item["kind"] == "table-preview"]
    for member, table, preview, expected in zip(listing["members"], manifest["tables"], previews,
                                       [["001", "north"], ["002", "south"], ["003", "café"]], strict=True):
        locator = {key: member[key] for key in ("id", "name")}
        assert member["tableIds"] == [table["id"]]
        assert table["source"]["archiveMember"] == locator
        assert preview["source"]["archiveMember"] == locator
        rows = (await client.get(table["rowsUrl"])).raise_for_status().json()["rows"]
        assert rows[1]["source"]["archiveMember"] == locator and rows[1]["source"]["lineStart"] == 2
        assert [(cell["type"], cell["value"]) for cell in rows[1]["cells"]] == [("text", value) for value in expected]
        export = next(item for item in manifest["products"] if item["id"] == table["downloads"][1]["productId"])
        assert export["source"]["archiveMember"] == locator
        content = (await client.get(export["contentUrl"])).raise_for_status().text
        assert list(csv.reader(io.StringIO(content)))[1] == expected
    index = next(item for item in manifest["products"] if item["path"] == "indexes/items_000001.md")
    assert "archiveMember" not in index.get("source", {})
    text = (await client.get(index["contentUrl"])).raise_for_status().text
    assert "Archive member inventory" in text and "east/samples.csv" in text
    assert (service.private / "sources" / manifest["source"]["snapshotId"] / "source").read_bytes() == original
    assert path.read_bytes() == original and sibling.read_text() == "unrelated"
    assert sorted(p for p in workspace.rglob("*") if p.is_file()) == sorted([path, sibling])
    assert len((await client.get("/preprocessing/jobs")).raise_for_status().json()["jobs"]) == 1
    assert service.store.operations(job["id"]) == []


async def test_mixed_inventory_does_not_publish_failed_members(archive_worker):
    client, workspace, service, _ = archive_worker
    path = workspace / "mixed.zip"
    archive(path, [("ok.csv", b"a\n1\n"), ("bad.csv", b'a\n1\n"unfinished\n'), ("notes.docx", b"not parsed"),
                   ("scans.pdf", b"not parsed"), ("book.xlsx", b"not parsed"), ("inner.tgz", b"not opened"),
                   ("notes.gz", gzip.compress(b"field notes")), ("empty.csv", b"")])
    _, job = await submit(client, path.name)
    assert job["state"] == "partial", job
    manifest = (await client.get(job["resultUrl"])).raise_for_status().json()
    assert len(manifest["tables"]) == 1
    members = (await inventory(client, manifest))["members"]
    assert [m["reason"] for m in members] == [
        None, "invalid-delimited", *(["unsupported-format"] * 5), "empty-table"]
    async with asyncio.timeout(5):
        while (await client.get(job["resultUrl"].removesuffix("/results"))).json()["documents"]["state"] != "ready":
            await asyncio.sleep(.01)
    for tree in (service.private / "jobs" / job["id"] / "sealed", service.root / "documents" / manifest["resultId"]):
        files = {"manifest.json", *(p["path"] for p in manifest["products"])}
        directories = {str(parent) for name in files for parent in PurePosixPath(name).parents} - {"."}
        assert {p.relative_to(tree).as_posix() for p in tree.rglob("*")} == files | directories


@pytest.mark.parametrize("name", ["empty.zip", "empty.TGZ"])
async def test_empty_archive_has_inventory_but_is_not_success(archive_worker, name):
    client, workspace, _, _ = archive_worker
    path = workspace / name
    archive(path, [])
    _, job = await submit(client, name)
    assert job["state"] == "partial", job
    manifest = (await client.get(job["resultUrl"])).raise_for_status().json()
    assert (await inventory(client, manifest))["members"] == []
    assert manifest["tables"] == []


@pytest.mark.parametrize("case,code", [
    ("broken.zip", "INVALID_INPUT"), ("wrong.tar.gz", "INVALID_INPUT"), ("notes.gz", "UNSUPPORTED_FORMAT"),
    ("skipped.TGZ", "RESOURCE_LIMIT"), ("rows.zip", "RESOURCE_LIMIT"), ("output.zip", "RESOURCE_LIMIT"),
    ("entries.zip", "RESOURCE_LIMIT"),
])
async def test_archive_refusals_keep_original_without_result(archive_worker, case, code):
    client, workspace, service, _ = archive_worker
    path = workspace / case
    if case == "skipped.TGZ":
        archive(path, [("notes.txt", b"a" * (16 * 1024))])
    elif case == "rows.zip":
        archive(path, [("one.csv", b"1\n" * 5), ("two.csv", b"2\n" * 5)])
    elif case == "output.zip":
        wide = (b"a" * 2200 + b"\n") * 4
        archive(path, [("first.csv", (b"b" * 2000 + b"\n") * 3), ("wide.csv", wide)])
    elif case == "entries.zip":
        archive(path, [(f"note{i}.txt", b"") for i in range(129)])
    elif case == "wrong.tar.gz":
        with tarfile.open(path, "w"):
            pass
    elif case == "notes.gz":
        path.write_bytes(gzip.compress(b"field notes"))
    else:
        path.write_bytes(b"synthetic unrecognized content")
    original = path.read_bytes()
    location, job = await submit(client, path.name)
    assert job["state"] == "failed" and job["error"]["code"] == code, job
    assert (await client.get(location + "/results")).status_code == 409
    assert not (service.private / "jobs" / job["id"] / "sealed").exists()
    assert path.read_bytes() == original
    assert (service.private / "sources" / job["source"]["snapshotId"] / "source").read_bytes() == original
    if case == "output.zip":
        assert "staged and result files" in job["error"]["message"]
        directory = service.private / "jobs" / job["id"]
        assert not (directory / "staging").exists()
        assert not (directory / "archive-scratch").exists()
        await assert_failed_archive_replay(client, path.name, location, job)
        archive(workspace / "wide-only.zip", [("wide.csv", wide)])
        _, following = await submit(client, "wide-only.zip")
        assert following["state"] == "succeeded", following


async def test_manifest_has_its_own_byte_limit(archive_worker, monkeypatch):
    client, workspace, service, _ = archive_worker
    path = workspace / "wide.zip"
    archive(path, [("wide.csv", b",".join([b"1"] * 40))])
    monkeypatch.setattr(results, "ARCHIVE_MANIFEST_BYTES", 4096)
    _, job = await submit(client, path.name)
    assert job["state"] == "failed" and job["error"]["code"] == "RESOURCE_LIMIT", job
    assert "manifest" in job["error"]["message"]
    assert not (service.private / "jobs" / job["id"] / "sealed").exists()


async def test_plain_csv_and_word_still_use_native_adapters(archive_worker):
    client, workspace, _, _ = archive_worker
    (workspace / "plain.csv").write_text("code\n001\n")
    write_fixture(workspace / "ordinary.docx")
    for filename, adapter in [("plain.csv", "delimited"), ("ordinary.docx", "docx")]:
        _, job = await submit(client, filename)
        assert job["state"] == "succeeded", job
        manifest = (await client.get(job["resultUrl"])).raise_for_status().json()
        assert manifest["provenance"]["adapter"] == adapter
        assert not any(p["kind"] == "archive-inventory" for p in manifest["products"])
        if adapter == "docx":
            text, = [item for item in manifest["products"] if item["kind"] == "document-text"]
            assert "Inventory:" in (await client.get(text["contentUrl"])).raise_for_status().text


async def test_tar_structural_and_inventory_only_members(archive_worker):
    client, workspace, _, _ = archive_worker
    name = "inventory.tar"
    root = tarfile.TarInfo("./")
    root.type = tarfile.DIRTYPE
    long_name = "./notes/" + ("observations-" * 8) + "café.txt"
    linked = tarfile.TarInfo("./notes/link.csv")
    linked.type = tarfile.SYMTYPE
    linked.linkname = "notes.txt"
    note = tarfile.TarInfo(long_name)
    note.mtime = 123.5
    archive(workspace / name, [(root, b""), (note, b"notes"), (linked, b"")],
            pax_headers={"comment": "ordinary global metadata"})
    _, job = await submit(client, name)
    assert job["state"] == "partial", job
    manifest = (await client.get(job["resultUrl"])).raise_for_status().json()
    members = (await inventory(client, manifest))["members"]
    assert [(m["name"], m["kind"], m["reason"]) for m in members] == [
        (".", "directory", "directory"), (long_name[2:], "file", "unsupported-format"),
        ("notes/link.csv", "other", "non-regular")]
    assert job["coverage"] == manifest["coverage"] == {
        "unit": "archive-member", "scope": "archive-member-extraction", "enumeration": "complete",
        "total": 3, "completed": 1, "failed": 0, "unsupported": 2, "notProcessed": 0}


@pytest.mark.parametrize("length", [512, 513])
async def test_tar_directory_display_name_byte_limit(archive_worker, length):
    client, workspace, _, _ = archive_worker
    name = "inventory.tar"
    display_name = "/".join(["survey-" * 12] * 6 + ["x" * (length - 510)])
    assert len(display_name.encode()) == length
    folder = tarfile.TarInfo(f"./{display_name}/")
    folder.type = tarfile.DIRTYPE
    archive(workspace / name, [(folder, b"")])
    _, job = await submit(client, name)
    if length == 512:
        assert job["state"] == "partial", job
        manifest = (await client.get(job["resultUrl"])).raise_for_status().json()
        member, = (await inventory(client, manifest))["members"]
        assert member["name"] == display_name
    else:
        assert job["state"] == "failed" and job["error"]["code"] == "RESOURCE_LIMIT", job


class Pipe(io.BytesIO):
    def seek(self, *args):
        raise io.UnsupportedOperation()


@pytest.mark.parametrize("sink,wide", [(Pipe, False), (Pipe, True), (io.BytesIO, True)])
async def test_zip_local_sizes_and_data_descriptors(archive_worker, sink, wide):
    client, workspace, _, _ = archive_worker
    stream = sink()
    entry = zipfile.ZipInfo("results.csv")
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.extra = struct.pack("<HHBI", 0x5455, 5, 1, 1_700_000_000)
    entry.comment = b"Field measurements"
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as output:
        with output.open(entry, "w", force_zip64=wide) as member:
            member.write(b"code,value\n001,0\n")
        output.writestr("stations.csv", "code,value\n002,1\n")
    (workspace / "results.zip").write_bytes(stream.getvalue())
    _, job = await submit(client, "results.zip")
    assert job["state"] == "succeeded", job
    manifest = (await client.get(job["resultUrl"])).raise_for_status().json()
    listing = await inventory(client, manifest)
    assert [member["name"] for member in listing["members"]] == ["results.csv", "stations.csv"]


async def test_truncated_gzip_member_has_an_invalid_input_error(archive_worker):
    client, workspace, service, _ = archive_worker
    name = "transfer.tgz"
    path = workspace / name
    note = random.Random(4).randbytes(9000).hex()[:12_000].encode()
    archive(path, [("results.csv", b"code,value\n001,0\n"), ("notes.txt", note)])
    # An interrupted copy starts with a usable table but ends inside notes.txt.
    original = path.read_bytes()
    path.write_bytes(original[:len(original) // 2])
    with gzip.open(path) as copied:
        assert b"001,0\n" in copied.read(1024)
    location, job = await submit(client, name)
    assert job["state"] == "failed" and job["error"]["code"] == "INVALID_INPUT", job
    assert (await client.get(location + "/results")).status_code == 409
    assert not (service.private / "jobs" / job["id"] / "sealed").exists()


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_zip_reads_multiblock_members(tmp_path, method):
    path = tmp_path / "results.zip"
    data = b"code,value\n" + b"001,field\n" * 9000
    with zipfile.ZipFile(path, "w", compression=method) as output:
        output.writestr("results.csv", data)
    budget = Budget(len(data), "fixture budget")
    assert [(member.name, b"".join(member.blocks)) for member in zip_members(path, budget)] == [
        ("results.csv", data)]
    assert budget.remaining == 0


def test_zip_extra_reader_rejects_incomplete_metadata_fields():
    # Extended timestamp field with one modification time.
    field = struct.pack("<HHBI", 0x5455, 5, 1, 1_700_000_000)
    [(kind, data)] = archive_readers.zip_extra_fields(field)
    assert (kind, bytes(data)) == (0x5455, field[4:])
    for prefix in (field[:3], field[:-1]):
        with pytest.raises(ServiceError) as error:
            list(archive_readers.zip_extra_fields(prefix))
        assert error.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("dialect", [tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT])
def test_tar_names_across_header_dialects(tmp_path, dialect):
    path = tmp_path / "results.tar"
    directory = "data/" + "survey-" * 14
    name = directory + "/results.csv"
    data = b"code,value\n001,0\n"
    with tarfile.open(path, "w", format=dialect) as output:
        folder = tarfile.TarInfo(directory + "/")
        folder.type = tarfile.DIRTYPE
        output.addfile(folder)
        member = tarfile.TarInfo(name)
        member.size = len(data)
        output.addfile(member, io.BytesIO(data))
    # Both writers have the same logical names; only GNU needs an extra header.
    assert [(m.name, m.kind, b"".join(m.blocks)) for m in
            tar_members(path, Budget(16_384, "fixture budget"), compressed=False)] == [
        (directory + "/", "directory", b""), (name, "file", data)]


async def test_zip_structural_directory_does_not_require_a_table(archive_worker):
    client, workspace, _, _ = archive_worker
    name = "fieldwork.zip"
    with zipfile.ZipFile(workspace / name, "w") as output:
        output.writestr("stations/", b"", compress_type=zipfile.ZIP_BZIP2)
        output.writestr("stations/readings.csv", b"code,value\n001,0\n")
    _, job = await submit(client, name)
    assert job["state"] == "succeeded", job
    manifest = (await client.get(job["resultUrl"])).raise_for_status().json()
    directory = (await inventory(client, manifest))["members"][0]
    assert (directory["kind"], directory["reason"]) == ("directory", "directory")
