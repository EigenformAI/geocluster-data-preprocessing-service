"""Worker-only format registry; expensive parsers are imported at extraction time."""

from preprocessing.contracts import FormatAdapter
from preprocessing.errors import ServiceError

from .delimited import DelimitedAdapter
from .pdf import PDFAdapter
from .workbook import WorkbookAdapter

_ADAPTERS = (DelimitedAdapter(",", ".csv"), DelimitedAdapter("\t", ".tsv"), WorkbookAdapter(), PDFAdapter())
_REGISTRY = {extension: adapter for adapter in _ADAPTERS for extension in adapter.extensions}


def enabled_extensions() -> list[str]:
    return sorted(_REGISTRY)


def get_adapter(extension: str) -> FormatAdapter:
    extension = "." + extension.lower().lstrip(".")
    try:
        return _REGISTRY[extension]
    except KeyError:
        raise ServiceError("UNSUPPORTED_FORMAT", f"Unsupported extension {extension}; use {', '.join(enabled_extensions())}.") from None
