"""Private HTTP and Streamable HTTP MCP adapters for the same job owner."""
import hmac
import json
from contextlib import asynccontextmanager
from uuid import uuid4

from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolResult,
    ClientNotification,
    ClientRequest,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    TextContent,
    ToolAnnotations,
)
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

from preprocessing.contracts import DEFAULT_LIMITS
from preprocessing.errors import ServiceError
from preprocessing.table_recovery import TABLE_RECOVERY_SCHEMA

_STATES = {"queued", "running", "cancelling", "succeeded", "partial", "failed", "cancelled", "interrupted"}
_TERMINAL = _STATES - {"queued", "running", "cancelling"}


def _error_body(error):
    return {"error": error.as_dict(str(uuid4()))}


def _internal_error():
    return ServiceError("INTERNAL_ERROR", "The request could not be completed.", 500, "operator-action")


def _error_response(error):
    headers = {}
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if error.status == 429:
        headers["Retry-After"] = "1"
    return JSONResponse(_error_body(error), status_code=error.status, headers=headers)


def _limit(service, name):
    return service.capabilities().get("limits", {}).get(name, DEFAULT_LIMITS[name])


class _PrivateBoundary:
    """Authenticate before reading, and cap all bodies before adapter dispatch.

    Buffering is intentional: a receive-wrapper exception can be swallowed by
    an MCP transport, and dispatch before the final chunk risks partial admission.
    Product responses are streamed untouched.
    """

    def __init__(self, app, service, token):
        self.app, self.service = app, service
        self.token = token.encode("utf-8") if token is not None else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def track_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            headers = Headers(scope=scope)
            if self.token is not None:
                auth = headers.getlist("authorization")
                scheme, _, value = (auth[0] if len(auth) == 1 else "").partition(" ")
                matches = hmac.compare_digest(value.encode("utf-8"), self.token)
                if scheme.lower() != "bearer" or not matches:
                    raise ServiceError("UNAUTHORIZED", "A valid service credential is required.", 401, "none")
            if "origin" in headers and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                raise ServiceError("ORIGIN_FORBIDDEN", "Browser mutations must use the project proxy.", 403, "none")
            limit = _limit(self.service, "requestBytes")
            lengths = headers.getlist("content-length")
            if lengths:
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    raise ServiceError("INVALID_REQUEST", "Invalid request length.")
                length = lengths[0].lstrip("0") or "0"
                if len(length) > len(str(limit)) or int(length) > limit:
                    raise ServiceError("REQUEST_TOO_LARGE", "The request exceeds the byte limit.", 413)
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > limit:
                    raise ServiceError("REQUEST_TOO_LARGE", "The request exceeds the byte limit.", 413)
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            if scope["path"].rstrip("/") == "/mcp" and scope["method"] == "POST":
                # The SDK's validation messages include raw input. Validate the
                # protocol envelope here so malformed calls never reach its logs.
                payload = _parse_json(body)
                try:
                    message = JSONRPCMessage.model_validate(payload, strict=True).root
                    if isinstance(message, JSONRPCRequest):
                        ClientRequest.model_validate(payload, strict=True)
                    elif isinstance(message, JSONRPCNotification):
                        ClientNotification.model_validate(payload, strict=True)
                except ValueError:
                    raise ServiceError("INVALID_REQUEST", "Invalid MCP message.") from None
            consumed = False

            async def buffered_receive():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, buffered_receive, track_send)
        except Exception as error:
            if started:
                raise
            await _error_response(error if isinstance(error, ServiceError) else _internal_error())(scope, receive, send)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def _parse_json(raw):
    try:
        body = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ServiceError("INVALID_REQUEST", "A valid JSON object is required.") from None
    if not isinstance(body, dict):
        raise ServiceError("INVALID_REQUEST", "A JSON object is required.")
    return body


async def _json_body(request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ServiceError("INVALID_REQUEST", "Content-Type must be application/json.", 415)
    return _parse_json(await request.body())


def _integer(value, default, minimum, maximum=None):
    if value is None:
        return default
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            value = int(value)
        except ValueError:
            raise ServiceError("INVALID_QUERY", "Invalid pagination number.") from None
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ServiceError("INVALID_QUERY", "Pagination is outside the supported range.")
    return value


def _query(request, allowed):
    pairs = request.query_params.multi_items()
    if any(key not in allowed for key, _ in pairs) or len(dict(pairs)) != len(pairs):
        raise ServiceError("INVALID_QUERY", "Unknown or repeated query parameter.")
    return dict(pairs)


def _list_jobs(service, query):
    if "submissionKey" in query:
        if len(query) != 1:
            raise ServiceError("INVALID_QUERY", "Submission lookup cannot be combined with history filters.")
        return {"job": service.lookup(query["submissionKey"])}
    query = dict(query)
    query["limit"] = _integer(query.get("limit"), 50, 1, 200)
    if "state" in query and query["state"] not in _STATES:
        raise ServiceError("INVALID_QUERY", "Unknown job state.")
    return service.list_jobs(**query)


def _result_summary(service, job_id):
    manifest = service.get_result(job_id)
    budget = _limit(service, "indexBytes")
    fields = {
        "products": ("id", "kind", "mediaType", "bytes", "path", "contentUrl"),
        "tables": ("id", "rows", "columns", "rowsUrl", "downloads", "recovery", "confirmation", "evidencePaths"),
        "figures": ("id", "sourcePage", "imageProductId", "ocrProductId"),
    }
    summary = {
        "jobId": job_id, "resultId": manifest.get("resultId"),
        "outcome": manifest.get("outcome"),
        "manifestUrl": f"/preprocessing/jobs/{job_id}/results",
        "counts": {name: len(manifest.get(name, [])) for name in fields},
        "truncated": True, **{name: [] for name in fields},
    }

    def fits():
        return len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) <= budget

    if not fits():
        raise ServiceError("RESPONSE_LIMIT", "The configured summary byte limit is too small.", 422, "operator-action")
    for name, keys in fields.items():
        for entry in manifest.get(name, []):
            summary[name].append({key: entry[key] for key in keys if key in entry})
            if not fits():
                summary[name].pop()
                break
    # 'false' takes one more byte than 'true'. Account for that last change.
    if all(len(summary[name]) == summary["counts"][name] for name in fields):
        summary["truncated"] = False
        if not fits():
            summary["truncated"] = True
    return summary


class _PrivateMCP(FastMCP):
    """Keep SDK validation and arbitrary exception strings off the wire."""

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.inputSchema["additionalProperties"] = False
            if tool.name == 'documents_submit':
                tool.inputSchema['properties']['tableRecovery'] = TABLE_RECOVERY_SCHEMA
        return tools

    async def call_tool(self, name, arguments):
        try:
            tool = next((tool for tool in await self.list_tools() if tool.name == name), None)
            if tool is None or not Draft202012Validator(tool.inputSchema).is_valid(arguments):
                raise ServiceError("INVALID_REQUEST", "Unknown tool or invalid tool arguments.")
            return await super().call_tool(name, arguments)
        except Exception as error:  # noqa: BLE001 - redact both SDK and service failures
            # FastMCP wraps function exceptions in ToolError, preserving the cause.
            while error.__cause__ is not None and not isinstance(error, ServiceError):
                error = error.__cause__
            error = error if isinstance(error, ServiceError) else _internal_error()
            return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(_error_body(error)))])


def _mcp_server(service, token):
    mcp = _PrivateMCP(
        "documents", stateless_http=True, json_response=True,
        # With a credential, the proxy may use a non-loopback Host. Without one,
        # retain the SDK's loopback DNS-rebinding checks on the MCP endpoint.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=token is None,
            allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"],
        ),
    )

    def mutation_allowed():
        metadata = mcp.get_context().request_context.meta
        session_id = getattr(metadata, "ai.opencode/sessionID", None)
        if isinstance(session_id, str) and session_id in service.internal_sessions:
            raise ServiceError("INTERNAL_SESSION_FORBIDDEN", "Processing sessions cannot mutate document jobs.", 403, "none")

    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    mutation = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=mutation)
    async def documents_submit(submissionKey: str, source: dict, profileId: str = "default",
                               correlation: dict | None = None, rerunOf: str | None = None,
                               resumeOf: str | None = None, tableRecovery: dict | None = None) -> dict:
        """Admit processing immediately; reuse the submission UUID to recover it.

        source is {kind: "workspace-file", path, expectedSha256?},
        {kind: "snapshot", snapshotId}, or {kind: "upload", receiptId}.
        Paths are workspace-relative. profileId selects a configured whole pipeline.
        """
        mutation_allowed()
        body = {"source": source, "profileId": profileId, "rerunOf": rerunOf}
        if resumeOf is not None:
            body['resumeOf'] = resumeOf
        if tableRecovery is not None:
            body['tableRecovery'] = tableRecovery
        if correlation is not None:
            body["correlation"] = correlation
        job, _ = await service.submit(submissionKey, body)
        return job

    @mcp.tool(annotations=read_only)
    def documents_get_job(jobId: str) -> dict:
        """Read a job's current state without starting processing."""
        return service.get(jobId)

    @mcp.tool(annotations=read_only)
    def documents_list_jobs(submissionKey: str | None = None, cursor: str | None = None,
                            limit: int | None = None, state: str | None = None,
                            snapshotId: str | None = None, uploadBatchId: str | None = None) -> dict:
        """List bounded history, or recover one submission key without other filters."""
        query = {"submissionKey": submissionKey, "cursor": cursor, "limit": limit,
                 "state": state, "snapshotId": snapshotId, "uploadBatchId": uploadBatchId}
        return _list_jobs(service, {key: value for key, value in query.items() if value is not None})

    @mcp.tool(annotations=mutation)
    async def documents_cancel(jobId: str) -> dict:
        """Request cancellation; a cancelling job has not yet settled."""
        mutation_allowed()
        return await service.cancel(jobId)

    @mcp.tool(annotations=read_only)
    def documents_get_result(jobId: str) -> dict:
        """Read bounded navigation and a link to the full immutable manifest."""
        return _result_summary(service, jobId)

    @mcp.tool(annotations=read_only)
    def documents_read_table(jobId: str, tableId: str, offset: int = 0, limit: int = 100) -> dict:
        """Read bounded typed rows; full-download links remain in the manifest."""
        return service.read_table(jobId, tableId, _integer(offset, 0, 0), _integer(limit, 100, 1, 1000))

    @mcp.tool(annotations=read_only)
    def documents_read_text(jobId: str, productId: str, offset: int = 0, limit: int | None = None) -> dict:
        """Read a bounded manifest-listed text product with continuation offsets."""
        return service.read_text(jobId, productId, _integer(offset, 0, 0), _integer(limit, None, 1))

    return mcp


def create_app(service, token: str | None = None) -> Starlette:
    """Create one listener. The host owns both service and MCP lifetimes."""
    if token is not None and not token:
        raise ValueError("The service token must not be empty.")
    mcp = _mcp_server(service, token)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        try:
            await service.start()
            async with mcp.session_manager.run():
                yield
        finally:
            await service.close()

    async def capabilities(request):
        return JSONResponse(await service.refresh_capabilities())

    async def jobs(request):
        if request.method == "POST":
            keys = request.headers.getlist("idempotency-key")
            if len(keys) != 1 or not keys[0]:
                raise ServiceError("INVALID_SUBMISSION_KEY", "One UUID Idempotency-Key is required.")
            job, replayed = await service.submit(keys[0], await _json_body(request))
            return JSONResponse(job, status_code=200 if replayed else 202,
                                headers={"Location": f"/preprocessing/jobs/{job['id']}"})
        query = _query(request, {"submissionKey", "cursor", "limit", "state", "snapshotId", "uploadBatchId"})
        return JSONResponse(_list_jobs(service, query))

    async def get_job(request):
        return JSONResponse(service.get(request.path_params["job_id"]))

    async def cancel(request):
        job = await service.cancel(request.path_params["job_id"])
        return JSONResponse(job, status_code=200 if job["state"] in _TERMINAL else 202)

    async def results(request):
        return JSONResponse(service.get_result(request.path_params["job_id"]))

    async def reconcile(request):
        return JSONResponse(await service.reconcile_documents(request.path_params["job_id"]))

    async def reconcile_engine(request):
        report = await service.reconcile_engine()
        # Recovery may include private model evidence. Return only admission state.
        body = {"settled": report["settled"], "dependency": service.capabilities().get("dependency")}
        if report.get("reason"):
            body["reason"] = report["reason"]
        return JSONResponse(body, status_code=200 if report["settled"] else 503)

    async def product(request):
        path, metadata = service.product(request.path_params["job_id"], request.path_params["product_id"])
        return FileResponse(path, media_type=metadata["mediaType"], filename=path.name,
                            headers={"X-Content-Type-Options": "nosniff"})

    async def table(request):
        query = _query(request, {"offset", "limit"})
        return JSONResponse(service.read_table(
            request.path_params["job_id"], request.path_params["table_id"],
            _integer(query.get("offset"), 0, 0), _integer(query.get("limit"), 100, 1, 1000),
        ))

    async def service_error(request, error):
        return _error_response(error)

    base = "/preprocessing/jobs/{job_id}"
    return Starlette(
        routes=[
            Route("/preprocessing/capabilities", capabilities),
            Route("/preprocessing/engine/reconcile", reconcile_engine, methods=["POST"]),
            Route("/preprocessing/jobs", jobs, methods=["GET", "POST"]),
            Route(base, get_job),
            Route(base + "/cancel", cancel, methods=["POST"]),
            Route(base + "/results", results),
            Route(base + "/reconcile", reconcile, methods=["POST"]),
            Route(base + "/products/{product_id}", product),
            Route(base + "/tables/{table_id}/rows", table),
            # Preserve the exact /mcp endpoint without a slash redirect. The MCP
            # sub-app declares /mcp itself; both surfaces share the outer guard.
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
        middleware=[Middleware(_PrivateBoundary, service=service, token=token)],
        exception_handlers={ServiceError: service_error},
    )
