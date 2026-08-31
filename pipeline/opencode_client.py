"""Small OpenCode HTTP client for extraction-stage image descriptions."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OpencodeError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    password = os.environ.get("OPENCODE_SERVER_PASSWORD")
    if password:
        username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    return headers


def _post(server_url: str, path: str, payload: dict, timeout_s: float) -> dict:
    request = Request(
        f"{server_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers=_headers(),
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        raise OpencodeError(f"opencode HTTP {exc.code}") from exc
    except URLError as exc:
        raise OpencodeError(f"opencode connection failed: {exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise OpencodeError(f"opencode response failed: {exc}") from exc


def describe_image(
    image_path: Path,
    prompt: str,
    *,
    model: str,
    server_url: str | None = None,
    variant: str = "low",
    timeout_s: float = 180.0,
) -> str:
    server_url = server_url or os.environ.get("OPENCODE_SERVER_URL")
    if not server_url:
        raise OpencodeError("OPENCODE_SERVER_URL is required for OpenCode image descriptions")
    provider_id, model_id = model.split("/", 1)
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode()
    session = _post(server_url, "/session", {"title": "geo-report-figure-description"}, timeout_s)
    session_id = session.get("id") or session.get("sessionID")
    if not session_id:
        raise OpencodeError("opencode session creation returned no session id")
    payload = {
        "model": {"providerID": provider_id, "modelID": model_id},
        "variant": variant,
        "parts": [
            {"type": "text", "text": prompt},
            {"type": "file", "mime": mime, "filename": image_path.name, "url": f"data:{mime};base64,{encoded}"},
        ],
    }
    last_error = None
    for attempt in range(2):
        try:
            response = _post(server_url, f"/session/{session_id}/message", payload, timeout_s)
            text = "\n".join(
                part["text"]
                for part in response.get("parts", [])
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
            ).strip()
            if not text:
                raise OpencodeError("opencode response contained no text")
            return text
        except OpencodeError as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1)
    raise last_error
