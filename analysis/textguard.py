"""Verbatim-text grounding checks for prose evidence."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Iterable


_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_WHITESPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"\w+")
MIN_BACKED_SPAN_TOKENS = 2
MIN_BACKED_SPAN_ALNUM_CHARS = 6


def normalize_text(value: str | None) -> str:
    """Normalize text for span containment without changing its lexical content."""

    if value is None:
        return ""
    text = html.unescape(str(value))
    text = unicodedata.normalize("NFKC", text)
    text = _MARKDOWN_IMAGE.sub(" ", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def _meaningful_span(value: str) -> bool:
    tokens = _TOKEN.findall(value)
    n_alnum = sum(1 for token in tokens for char in token if char.isalnum())
    return len(tokens) >= MIN_BACKED_SPAN_TOKENS and n_alnum >= MIN_BACKED_SPAN_ALNUM_CHARS


def _token_char(value: str) -> bool:
    return bool(value) and (value.isalnum() or value == "_")


def _contains_with_token_boundaries(needle: str, haystack: str) -> bool:
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        before = haystack[start - 1] if start else ""
        after = haystack[end] if end < len(haystack) else ""
        left_ok = not (_token_char(before) and _token_char(needle[0]))
        right_ok = not (_token_char(after) and _token_char(needle[-1]))
        if left_ok and right_ok:
            return True
        start = haystack.find(needle, start + 1)
    return False


def text_backed(span: str | None, source_text: str | None) -> bool:
    """Return True when ``span`` is present in ``source_text`` after light normalization."""

    normalized_span = normalize_text(span)
    if not normalized_span or not _meaningful_span(normalized_span):
        return False
    return _contains_with_token_boundaries(normalized_span.casefold(), normalize_text(source_text).casefold())


def _span_value(item) -> str:
    if isinstance(item, dict):
        return str(item.get("span") or item.get("raw_token") or item.get("text") or "")
    return str(item or "")


def unbacked_spans(claim_spans: Iterable[str | dict], source_text: str | None) -> list[str]:
    """Return spans that are not meaningfully backed by the source text."""

    bad: list[str] = []
    for item in claim_spans:
        span = _span_value(item)
        if not text_backed(span, source_text):
            bad.append(span)
    return bad
