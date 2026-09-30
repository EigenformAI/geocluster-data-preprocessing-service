"""Delimited records retain decoded lexemes, empty records and physical lines."""

import csv
from pathlib import Path

from preprocessing.contracts import Extraction, ExtractionContext
from preprocessing.errors import ServiceError

from .common import (
    TableWriter,
    coverage,
    empty_extraction,
    finding,
    limits_for,
    native_table_stages,
)


class DelimitedAdapter:
    version = "1"

    def __init__(self, delimiter: str, extension: str):
        self.delimiter, self.extensions = delimiter, (extension,)

    def extract(self, snapshot: dict, profile: dict, output_dir: Path, context: ExtractionContext) -> Extraction:
        result = empty_extraction("delimited", self.version)
        limits = limits_for(profile)
        encoding = profile.get("options", {}).get("encoding", "utf-8-sig")
        delimiter = profile.get("options", {}).get("delimiter", self.delimiter)
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise ServiceError("INVALID_PROFILE", "Delimiter must be exactly one character.")
        result["provenance"].update({"encoding": encoding, "delimiter": delimiter,
                                     "lexemes": "decoded field values; quoting and line endings remain in the source snapshot"})
        previous_limit = csv.field_size_limit()
        csv.field_size_limit(limits["inputBytes"])
        try:
            with Path(snapshot["path"]).open(encoding=encoding, newline="") as source:
                reader = csv.reader(source, delimiter=delimiter, strict=True)
                with TableWriter(result, output_dir, "table_0001", {"kind": "delimited"}, limits) as table:
                    end = 0
                    for index, values in enumerate(reader, 1):
                        locator = {"row": index, "lineStart": end + 1, "lineEnd": reader.line_num}
                        table.add([{"type": "text", "value": value,
                                    "source": {"row": index, "column": col}}
                                   for col, value in enumerate(values, 1)], locator)
                        end = reader.line_num
            if len(table.widths) > 1:
                finding(result, "IRREGULAR_ROWS", "Rows have different widths; original row lengths are retained.", tableId=table.table["id"])
        except (UnicodeError, LookupError, csv.Error) as exc:
            raise ServiceError("INVALID_INPUT", "Delimited input could not be decoded or parsed; check its encoding and delimiter.") from exc
        finally:
            csv.field_size_limit(previous_limit)
        result["coverage"] = coverage("row", table.table["rows"], table.table["rows"])
        result["stages"] = native_table_stages(result["coverage"])
        return result
