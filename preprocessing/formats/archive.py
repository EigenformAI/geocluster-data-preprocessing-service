"""One archive source, one complete inventory, and native member tables."""
import json
import tempfile
import unicodedata
from contextlib import closing
from pathlib import Path

from preprocessing.archive_limits import (
    ARCHIVE_EXTENSIONS,
    ARCHIVE_OUTPUT_BYTES,
    ARCHIVE_SCRATCH,
    Budget,
    limit,
)
from preprocessing.errors import ServiceError

from .archive_readers import malformed, tar_members, zip_members
from .common import (
    coverage,
    empty_extraction,
    finding,
    limits_for,
    product,
    stage_outcome,
)
from .delimited import DelimitedAdapter

ADAPTERS = {".csv": DelimitedAdapter(",", ".csv"), ".tsv": DelimitedAdapter("\t", ".tsv")}
INVENTORY = "archive/inventory.json"
MEMBER_BYTES = 64 * 1024 * 1024
OUTPUT_MESSAGE = "Archive staged and result files exceed the output-byte limit."


class MemberNames:
    def __init__(self):
        self.nodes = {}

    def add(self, name, kind, *, tar):
        if tar:
            name = name.removeprefix("./") if name != "./" else "."
        if kind == "directory":
            name = name.removesuffix("/")
        if len(name.encode("utf-8")) > 512:
            raise limit("Archive member name exceeds its byte limit.")
        if any(unicodedata.category(c).startswith("C") for c in name):
            raise malformed()
        if tar and name == "." and kind == "directory":
            parts = ["."]
        else:
            parts = name.split("/")
            if (len(parts) > 32 or any(not part or part in {".", ".."} or part.endswith((" ", "."))
                                       or any(c in part for c in '\\:<>|?*"') for part in parts)):
                raise malformed()
        canonical = []
        for index, part in enumerate(parts):
            canonical.append(unicodedata.normalize("NFC", part.casefold()))
            key = tuple(canonical)
            final = index == len(parts) - 1
            existing_part, existing_kind = self.nodes.get(key, (part, None))
            if existing_part != part or (final and existing_kind is not None):
                raise malformed()
            if (not final and existing_kind not in {None, "directory"}
                    or final and key in self.nodes and kind != "directory"):
                raise malformed()
            self.nodes[key] = (part, kind if final else existing_kind)
        return name


def process_member(member, adapter, locator, scratch, output, result, limits, published):
    # A single temporary input and a single tentative table at a time. Neither
    # is under the result tree until the member has finished and closed cleanly.
    if member.size > MEMBER_BYTES:
        raise limit("Delimited archive member exceeds its staging-byte limit.")
    storage = Budget(limits["outputBytes"] - published, OUTPUT_MESSAGE)
    table_id = "table_" + locator["id"].removeprefix("member_")
    with tempfile.TemporaryDirectory(prefix="member-", dir=scratch) as directory:
        pending = Path(directory)
        source = pending / "input"
        with source.open("xb") as handle:
            for block in member.blocks:
                storage.take(len(block))
                handle.write(block)
        # Emit the native table and product records directly to the extraction.
        # TableWriter only appends them after the final read succeeds.
        try:
            table = adapter.write_table(source, result, pending, table_id,
                                        {"kind": "delimited", "archiveMember": locator},
                                        limits, delimiter=adapter.delimiter, charge=storage.take)
        except ServiceError as exc:
            if exc.code != "INVALID_INPUT":
                raise
            return "failed", "invalid-delimited", 0
        if not table["rows"]:
            result["tables"].pop()
            del result["products"][-2:]
            return "skipped", "empty-table", 0
        for key in ("typedPath", "csvPath"):
            name = table[key]
            (output / name).parent.mkdir(exist_ok=True)
            (pending / name).rename(output / name)
        return "processed", None, limits["outputBytes"] - published - storage.remaining - member.size


class ArchiveAdapter:
    version = "1"
    extensions = ARCHIVE_EXTENSIONS

    def extract(self, snapshot, profile, output, context):
        filename = snapshot["filename"].lower()
        format = "zip" if filename.endswith(".zip") else "tar" if filename.endswith(".tar") else "tar.gz"
        limits = limits_for(profile)
        limits["outputBytes"] = min(limits["outputBytes"], ARCHIVE_OUTPUT_BYTES)
        budget = Budget(min(limits["expandedBytes"], 64 * 1024 * 1024),
                        "Archive exceeds its expanded/consumed byte limit.")
        path = Path(snapshot["path"])
        entries = zip_members(path, budget) if format == "zip" else tar_members(path, budget, compressed=format == "tar.gz")
        result = empty_extraction("archive", self.version)
        result["provenance"]["archive"] = {"format": format}
        members = []
        names = MemberNames()
        published_bytes = rows = columns = directories = 0
        scratch = output.parent / ARCHIVE_SCRATCH
        scratch.mkdir()
        try:
            with closing(entries):
                for index, member in enumerate(entries, 1):
                    name = names.add(member.name, member.kind, tar=format != "zip")
                    identity = {"id": f"member_{index:04d}", "name": name}
                    adapter = ADAPTERS.get(Path(name).suffix.lower()) if member.kind == "file" else None
                    table_ids = []
                    reason = {"directory": "directory", "other": "non-regular"}.get(member.kind) or member.reason
                    if reason or not adapter:
                        for _ in member.blocks:
                            pass
                        status, reason = "skipped", reason or "unsupported-format"
                    else:
                        member_limits = limits | {"rows": min(limits["rows"], 250_000) - rows,
                                                  "columns": min(limits["columns"], 256, 4096 - columns)}
                        status, reason, size = process_member(member, adapter, identity, scratch, output, result,
                                                              member_limits, published_bytes)
                        published_bytes += size
                        if status == "processed":
                            table = result["tables"][-1]
                            rows += table["rows"]
                            columns += len(table["columns"])
                            table_ids.append(table["id"])
                    directories += member.kind == "directory"
                    members.append(identity | {"kind": member.kind, "bytes": member.size, "status": status,
                                               "reason": reason, "tableIds": table_ids})
        finally:
            scratch.rmdir()
        completed = len(result["tables"]) + directories
        failed = sum(member["status"] == "failed" for member in members)
        cov = coverage("archive-member", len(members), completed, failed,
                       unsupported=len(members) - completed - failed, notProcessed=0)
        result["coverage"] = cov
        result["stages"] = [
            stage_outcome("archive-inventory", "completed", cov),
            stage_outcome("native-tables", "completed" if result["tables"] and completed == len(members) else "partial", cov),
            stage_outcome("model-extraction", "not_applicable", reason="Archives do not dispatch model work."),
        ]
        if not result["tables"]:
            finding(result, "NO_ARCHIVE_TABLES", "The archive has no usable CSV or TSV tables; see the member inventory.")
        encoded = json.dumps({"schemaVersion": 1, "format": format, "members": members},
                             ensure_ascii=False).encode("utf-8")
        if published_bytes + len(encoded) > limits["outputBytes"]:
            raise limit(OUTPUT_MESSAGE)
        target = output / INVENTORY
        target.parent.mkdir()
        target.write_bytes(encoded)
        product(result, INVENTORY, "archive-inventory", "application/json")
        return result
