"""Minimal stdio MCP-compatible JSON-RPC wrapper for geo-report tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .mcp_tools import GeoReportTools


def _ok(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _content(payload) -> list[dict]:
    return [{"type": "text", "text": json.dumps(payload, indent=2)}]


def handle_request(request: dict, tools: GeoReportTools | None = None) -> dict | None:
    tools = tools or GeoReportTools()
    method = request.get("method")
    request_id = request.get("id")
    try:
        if method == "initialize":
            return _ok(request_id, {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "geo-report-mcp", "version": "0.1.0"},
            })
        if method == "ping":
            return _ok(request_id, {})
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _ok(request_id, {"tools": tools.list_tools()})
        if method == "tools/call":
            params = request.get("params") or {}
            result = tools.call(params["name"], params.get("arguments") or {})
            return _ok(request_id, {"content": _content(result), "structuredContent": result})
        return _error(request_id, -32601, f"method not found: {method}")
    except Exception as exc:  # MCP transports tool errors through JSON-RPC responses.
        return _error(request_id, -32000, f"{type(exc).__name__}: {exc}")


def main() -> int:
    output_root = Path("output")
    tools = GeoReportTools(output_root=output_root)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = handle_request(request, tools)
        except Exception as exc:
            response = _error(None, -32700, f"{type(exc).__name__}: {exc}")
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
