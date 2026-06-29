"""Cached Mistral structured-output wrapper (chat.parse).

Caches by content hash under the report's analysis cache, mirroring the extraction
pipeline's describe-stage caching, so re-runs never re-bill.
"""

from __future__ import annotations

import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.mistral_client import get_client  # noqa: E402
from pipeline.util import read_json, sha256_bytes, write_json  # noqa: E402

from .apaths import APaths

TIMEOUT_MS = 180_000
ATTEMPTS = 6


def parse_cached(apaths: APaths, parts: list[bytes], model: str, messages: list, schema) -> dict:
    """Run chat.parse(response_format=schema) with caching; return model_dump dict."""
    key = sha256_bytes(*parts)
    cache_file = apaths.cache / f"{key}.json"
    cached = read_json(cache_file)
    if cached is not None:
        return cached

    client = get_client()
    last = None
    for i in range(ATTEMPTS):
        try:
            resp = client.chat.parse(
                model=model, messages=messages, response_format=schema, timeout_ms=TIMEOUT_MS
            )
            msg = resp.choices[0].message
            obj = getattr(msg, "parsed", None)
            data = obj.model_dump() if obj is not None else schema.model_validate_json(msg.content).model_dump()
            write_json(cache_file, data)
            return data
        except Exception as e:
            last = e
            rate_limited = "429" in str(e) or "rate" in str(e).lower()
            time.sleep(min(60, (20 if rate_limited else 5) * (i + 1)))
    raise last
