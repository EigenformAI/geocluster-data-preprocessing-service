"""Worker-only format registry; expensive parsers are imported at extraction time."""

from pathlib import Path

from preprocessing.contracts import FormatAdapter
from preprocessing.errors import ServiceError

from .archive import ArchiveAdapter
from .delimited import DelimitedAdapter
from .pdf import PDFAdapter
from .word import WordAdapter
from .workbook import WorkbookAdapter

_ADAPTERS = (DelimitedAdapter(",", ".csv"), DelimitedAdapter("\t", ".tsv"), WorkbookAdapter(),
             PDFAdapter(), WordAdapter(), ArchiveAdapter())
_REGISTRY = {extension: adapter for adapter in _ADAPTERS for extension in adapter.extensions}
_BY_SUFFIX_LENGTH = sorted(_REGISTRY, key=len, reverse=True)


def enabled_extensions() -> list[str]:
    return sorted(_REGISTRY)


def get_adapter(extension: str) -> FormatAdapter:
    extension = "." + extension.lower().lstrip(".")
    try:
        return _REGISTRY[extension]
    except KeyError:
        raise ServiceError("UNSUPPORTED_FORMAT", f"Unsupported extension {extension}; use {', '.join(enabled_extensions())}.") from None


def adapter_for_filename(filename: str) -> FormatAdapter:
    # Preserve ordinary extension semantics but match registered compounds first.
    name = Path(filename).name.lower()
    return get_adapter(next((suffix for suffix in _BY_SUFFIX_LENGTH if len(name) > len(suffix) and name.endswith(suffix)),
                            Path(name).suffix))
