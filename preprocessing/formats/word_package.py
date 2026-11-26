"""Read only the package metadata and main part of a macro-free Word document."""

import io
import re
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from preprocessing.errors import ServiceError

MAX_ZIP_READ = 512 * 1024  # Bounds the central-directory allocation by ZipFile.
MAX_MEMBERS = 4096
MAX_METADATA_BYTES = 1024 * 1024
MAX_MAIN_BYTES = 32 * 1024 * 1024
MAX_XML_ELEMENTS = 250_000
MAX_XML_DEPTH = 64
WORD_NAMESPACES = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
)
OFFICE_RELATIONSHIPS = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument",
}
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


def invalid_package():
    return ServiceError("INVALID_INPUT", "Word package is malformed or contains unsafe XML.")


class BoundedZipInput(io.BufferedReader):
    def read(self, size=-1):
        if size > MAX_ZIP_READ:
            raise ServiceError("RESOURCE_LIMIT", "Word ZIP directory exceeds its read limit.")
        if size < 0:  # zipfile on some Python versions reads the ZIP trailer to EOF.
            data = super().read(MAX_ZIP_READ + 1)
            if len(data) > MAX_ZIP_READ:
                raise ServiceError("RESOURCE_LIMIT", "Word ZIP directory exceeds its read limit.")
            return data
        return super().read(size)


def read_xml(archive, part, limit):
    from defusedxml.ElementTree import iterparse

    info = archive.getinfo(part)
    if info.file_size > limit or info.compress_size > limit + 64 * 1024:
        raise ServiceError("RESOURCE_LIMIT", "A Word XML part exceeds its byte limit.")
    if info.compress_type not in {ZIP_DEFLATED, ZIP_STORED}:
        raise ServiceError("UNSUPPORTED_FORMAT", "Word XML must use stored or deflated ZIP entries.")
    with archive.open(info) as stream:
        events = iterparse(stream, events=("start", "end"), forbid_dtd=True,
                           forbid_entities=True, forbid_external=True)
        count = depth = 0
        for event, _ in events:
            if event == "start":
                count += 1
                depth += 1
                if count > MAX_XML_ELEMENTS or depth > MAX_XML_DEPTH:
                    raise ServiceError("RESOURCE_LIMIT", "Word XML exceeds its element count or depth limit.")
            else:
                depth -= 1
        return events.root


def main_part(archive, limits):
    members = archive.infolist()
    if len(members) > MAX_MEMBERS:
        raise ServiceError("RESOURCE_LIMIT", "Word package exceeds its ZIP entry limit.")
    if sum(info.file_size for info in members) > limits["expandedBytes"]:
        raise ServiceError("RESOURCE_LIMIT", "Word package exceeds the configured expanded-byte limit.")
    names = [info.filename.lower() for info in members]
    if len(set(names)) != len(names) or any(info.orig_filename != info.filename for info in members):
        raise invalid_package()
    if any(info.flag_bits & 1 for info in members):
        raise ServiceError("UNSUPPORTED_FORMAT", "Encrypted Word documents are unsupported.")

    relationships = read_xml(archive, "_rels/.rels", MAX_METADATA_BYTES)
    types = read_xml(archive, "[Content_Types].xml", MAX_METADATA_BYTES)
    if relationships.tag != REL + "Relationships" or types.tag != CT + "Types":
        raise invalid_package()
    main = [entry for entry in relationships if entry.tag == REL + "Relationship"
            and entry.get("Type") in OFFICE_RELATIONSHIPS]
    if len(main) != 1 or main[0].get("TargetMode", "Internal") != "Internal":
        raise invalid_package()
    target = main[0].get("Target", "").removeprefix("/")
    # Match a package member literally. Do not URL-decode, normalize traversal,
    # resolve a filesystem path, or open any other relationship target.
    if (len(target) > 256 or not re.fullmatch(r"[A-Za-z0-9._~!$&'()+,;=@/-]+", target)
            or any(not segment or segment.endswith(".") for segment in target.split("/"))):
        raise invalid_package()
    defaults, overrides = {}, {}
    for entry in types:
        value = entry.get("ContentType", "")
        if "macroenabled" in value.lower() or "vbaproject" in value.lower():
            raise ServiceError("UNSUPPORTED_FORMAT", "Macro-enabled Word packages are unsupported.")
        if entry.tag == CT + "Override":
            mapping, key = overrides, entry.get("PartName", "").lower()
        elif entry.tag == CT + "Default":
            mapping, key = defaults, entry.get("Extension", "").lower()
        else:
            continue
        if not key or not value or key in mapping:
            raise invalid_package()
        mapping[key] = value
    if any(name.rsplit("/", 1)[-1] == "vbaproject.bin" for name in names):
        raise ServiceError("UNSUPPORTED_FORMAT", "Macro-enabled Word packages are unsupported.")
    extension = target.rsplit("/", 1)[-1].rpartition(".")[2].lower()
    content_type = overrides.get("/" + target.lower(), defaults.get(extension))
    if not content_type:
        raise invalid_package()
    if content_type != DOCX_TYPE:
        raise ServiceError("UNSUPPORTED_FORMAT", "The package does not contain a DOCX main document.")
    return target


def read_body(path, limits):
    from xml.etree.ElementTree import ParseError
    from zlib import error as InflateError

    from defusedxml.common import DefusedXmlException

    try:
        with BoundedZipInput(io.FileIO(path)) as stream:
            if stream.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                raise ServiceError("UNSUPPORTED_FORMAT", "Legacy or encrypted Word files are unsupported.")
            stream.seek(0)
            with ZipFile(stream) as archive:
                part = main_part(archive, limits)
                document = read_xml(archive, part, MAX_MAIN_BYTES)
        for namespace in WORD_NAMESPACES:
            word = f"{{{namespace}}}"
            if document.tag == word + "document":
                bodies = document.findall(word + "body")
                if len(bodies) == 1:
                    return part, word, bodies[0]
        raise invalid_package()
    except NotImplementedError:
        raise ServiceError("UNSUPPORTED_FORMAT", "Word package uses unsupported ZIP features.") from None
    except (BadZipFile, LookupError, ValueError, ParseError, DefusedXmlException, RuntimeError, InflateError, EOFError):
        raise invalid_package() from None
