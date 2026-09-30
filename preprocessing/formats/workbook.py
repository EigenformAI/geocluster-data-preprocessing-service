"""OOXML extraction from ZIP/XML without formula execution or float conversion."""

import posixpath
import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

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


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def children(element, name: str):
    return (child for child in element if local(child.tag) == name)


def child(element, name: str):
    return next(children(element, name), None)


def rich_text(element) -> str:
    # Phonetic annotations are not part of the cell's textual value.
    return "".join(node.text or "" for node in element if local(node.tag) == "t") + "".join(
        node.text or "" for run in children(element, "r") for node in children(run, "t"))


def records(archive: ZipFile, path: str, tag: str):
    from defusedxml.ElementTree import iterparse
    with archive.open(path) as stream:
        stack = []
        for event, element in iterparse(stream, events=("start", "end"), forbid_dtd=True):
            if event == "start":
                stack.append(element)
            else:
                if local(element.tag) == tag:
                    yield element
                    element.clear()
                    if len(stack) > 1:
                        stack[-2].remove(element)
                stack.pop()


def column_index(reference: str) -> int:
    match = re.fullmatch(r"([A-Za-z]+)([1-9][0-9]*)", reference)
    if not match:
        raise ServiceError("INVALID_INPUT", "Workbook contains an invalid cell reference.")
    result = 0
    for letter in match[1].upper():
        result = result * 26 + ord(letter) - ord("A") + 1
    return result


def column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def date_format(number: int, formats: dict[int, str]) -> bool:
    if number in set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59)):
        return True
    code = formats.get(number, "")
    code = re.sub(r'"[^"]*"|\\.|\[(?![hms]+\])[^\]]*\]', "", code, flags=re.IGNORECASE)
    return bool(re.search(r"[ymdhs]", code, flags=re.IGNORECASE))


def parse_cell(element, shared: list[str], styles: list[dict], date_system: str) -> dict:
    kind = element.get("t", "n")
    value_element = child(element, "v")
    value = value_element.text if value_element is not None else None
    style_index = int(element.get("s", "0"))
    if style_index < 0 or (styles and style_index >= len(styles)):
        raise ServiceError("INVALID_INPUT", "Workbook references an unknown cell style.")
    style = styles[style_index] if styles else {"date": False, "numFmtId": 0}
    if kind == "inlineStr":
        inline = child(element, "is")
        cell = {"type": "text", "value": rich_text(inline) if inline is not None else ""}
    elif value is None:
        cell = {"type": "text", "value": ""} if kind == "str" and value_element is not None else {"type": "null"}
    elif kind == "s":
        index = int(value)
        if not 0 <= index < len(shared):
            raise ServiceError("INVALID_INPUT", "Workbook references an unknown shared string.")
        cell = {"type": "text", "value": shared[index]}
    elif kind == "b":
        if value not in {"0", "1"}:
            raise ServiceError("INVALID_INPUT", "Workbook contains an invalid boolean.")
        cell = {"type": "boolean", "value": value == "1", "raw": value}
    elif kind == "e":
        cell = {"type": "error", "value": value}
    elif kind == "d":
        cell = {"type": "date", "value": value, "representation": "iso8601"}
    elif kind == "str":
        cell = {"type": "text", "value": value}
    elif kind == "n":
        cell = ({"type": "date", "serial": value, "dateSystem": date_system, "representation": "excel-serial"}
                if style["date"] else {"type": "number", "value": value})
    else:
        raise ServiceError("INVALID_INPUT", "Workbook contains an unsupported cell type.")
    formula = child(element, "f")
    if formula is not None:
        has_cache = value_element is not None and (value is not None or kind == "str")
        cell = {"type": "formula", "formula": formula.text or "", "formulaAttributes": dict(formula.attrib),
                "cached": cell if has_cache else None}
    return cell | {"style": style_index, "numberFormat": style.get("format"), "numFmtId": style["numFmtId"]}


class WorkbookAdapter:
    extensions = (".xlsx", ".xlsm")
    version = "1"

    def extract(self, snapshot: dict, profile: dict, output_dir: Path, context: ExtractionContext) -> Extraction:
        from xml.etree.ElementTree import ParseError

        from defusedxml.common import DefusedXmlException
        from defusedxml.ElementTree import parse

        result = empty_extraction("ooxml", self.version)
        limits = limits_for(profile)
        try:
            with ZipFile(snapshot["path"]) as archive:
                if sum(info.file_size for info in archive.infolist()) > limits["expandedBytes"]:
                    raise ServiceError("RESOURCE_LIMIT", "Workbook exceeds the configured expanded-byte limit.")
                if any(info.flag_bits & 1 for info in archive.infolist()):
                    raise ServiceError("UNSUPPORTED_FORMAT", "Encrypted workbooks are unsupported; provide an unencrypted workbook.")
                if len(set(archive.namelist())) != len(archive.namelist()):
                    raise ServiceError("INVALID_INPUT", "Workbook contains duplicate ZIP members.")

                def xml(path):
                    with archive.open(path) as stream:
                        return parse(stream, forbid_dtd=True).getroot()

                workbook = xml("xl/workbook.xml")
                properties = child(workbook, "workbookPr")
                date_system = "1904" if properties is not None and properties.get("date1904") in {"1", "true"} else "1900"
                relationships = {r.get("Id"): r for r in xml("xl/_rels/workbook.xml.rels")}
                shared = [rich_text(item) for item in records(archive, "xl/sharedStrings.xml", "si")] if "xl/sharedStrings.xml" in archive.namelist() else []
                styles = []
                if "xl/styles.xml" in archive.namelist():
                    style_root = xml("xl/styles.xml")
                    formats = {int(e.get("numFmtId")): e.get("formatCode", "") for e in style_root.iter() if local(e.tag) == "numFmt"}
                    xfs = child(style_root, "cellXfs")
                    for xf in xfs if xfs is not None else []:
                        number = int(xf.get("numFmtId", "0"))
                        styles.append({"date": date_format(number, formats), "format": formats.get(number), "numFmtId": number})
                sheets_element = child(workbook, "sheets")
                if sheets_element is None:
                    raise ServiceError("INVALID_INPUT", "Workbook has no sheet inventory.")
                sheets = list(children(sheets_element, "sheet"))
                unsupported = 0
                total_rows = 0
                for index, sheet in enumerate(sheets, 1):
                    relation_id = next((value for key, value in sheet.attrib.items() if local(key) == "id"), None)
                    relation = relationships.get(relation_id)
                    if relation is None or relation.get("TargetMode") == "External":
                        raise ServiceError("INVALID_INPUT", "Workbook sheet relationship is missing or external.")
                    if not relation.get("Type", "").endswith("/worksheet"):
                        unsupported += 1
                        finding(result, "UNSUPPORTED_SHEET", "A non-worksheet sheet could not be extracted.", sheetId=sheet.get("sheetId"))
                        continue
                    target = relation.get("Target", "")
                    target = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
                    if not target.startswith("xl/") or "\\" in target:
                        raise ServiceError("INVALID_INPUT", "Workbook sheet target is outside its package root.")
                    locator = {"kind": "sheet", "sheet": sheet.get("name", ""), "sheetId": sheet.get("sheetId"),
                               "visibility": sheet.get("state", "visible")}
                    with TableWriter(result, output_dir, f"sheet_{index:04d}", locator, limits) as table:
                        table.table["dateSystem"] = date_system
                        previous_row = 0
                        for row in records(archive, target, "row"):
                            row_number = int(row.get("r", previous_row + 1))
                            if row_number <= previous_row:
                                raise ServiceError("INVALID_INPUT", "Workbook row positions are not increasing.")
                            cells = []
                            for element in children(row, "c"):
                                reference = element.get("r", f"{column_name(len(cells) + 1)}{row_number}")
                                col = column_index(reference)
                                if int(re.search(r"\d+$", reference)[0]) != row_number or col <= len(cells):
                                    raise ServiceError("INVALID_INPUT", "Workbook cell positions are inconsistent.")
                                if col > limits["columns"]:
                                    raise ServiceError("RESOURCE_LIMIT", "Workbook exceeds the configured column limit.")
                                while len(cells) < col - 1:
                                    cells.append({"type": "absent", "source": {"sheetId": locator["sheetId"], "cell": f"{column_name(len(cells) + 1)}{row_number}"}})
                                cells.append(parse_cell(element, shared, styles, date_system) | {"source": {"sheetId": locator["sheetId"], "cell": reference}})
                            total_rows += 1
                            if total_rows > limits["rows"]:
                                raise ServiceError("RESOURCE_LIMIT", "Workbook exceeds the configured row limit.")
                            table.add(cells, locator | {"row": row_number, "hidden": row.get("hidden") in {"1", "true"}})
                            previous_row = row_number
                result["coverage"] = coverage("sheet", len(sheets), len(sheets) - unsupported,
                                               excluded=["embedded pictures and charts", "macros", "formula calculation", "external-link refresh"])
                result["coverage"].update({"unsupported": unsupported, "notProcessed": 0})
                result["provenance"].update({"dateSystem": date_system, "numericRepresentation": "decimal-string",
                                             "formulaPolicy": "source expression and stored cache; no calculation"})
        except (BadZipFile, KeyError, ValueError, ParseError, DefusedXmlException, RuntimeError) as exc:
            raise ServiceError("INVALID_INPUT", "Workbook is malformed, encrypted, or contains unsafe XML; provide an unencrypted XLSX/XLSM file.") from exc
        result["stages"] = native_table_stages(result["coverage"])
        return result
