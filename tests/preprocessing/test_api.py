import asyncio
import json
import random
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
from fakes import FakeEngine
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from preprocessing.api import create_app
from preprocessing.contracts import DEFAULT_LIMITS
from preprocessing.errors import ServiceError


class FakeService:
    """An observable service boundary, not a substitute for persistence tests."""

    def __init__(self, tmp_path):
        self.events = []
        self.jobs = {}
        self.bodies = {}
        self.internal_sessions = {"internal-1"}
        self.limits = dict(DEFAULT_LIMITS)
        self.file = tmp_path / "table.csv"
        self.file.write_bytes(b"column\n0123456789\n")
        self.manifest = {
            "schemaVersion": 1, "jobId": "job-1", "resultId": "result-1",
            "outcome": "succeeded", "products": [
                {"id": "product-1", "kind": "table-export", "path": "tables/a.csv",
                 "contentUrl": "/preprocessing/jobs/job-1/products/product-1",
                 "mediaType": "text/csv", "bytes": self.file.stat().st_size}
            ], "tables": [], "figures": [],
        }

    async def start(self):
        self.events.append("start")

    async def close(self):
        self.events.append("close")

    async def submit(self, key, body):
        try:
            UUID(key)
        except (ValueError, TypeError, AttributeError):
            raise ServiceError("INVALID_SUBMISSION_KEY", "A UUID submission key is required.")
        if set(body) - {"source", "profileId", "correlation", "rerunOf"}:
            raise ServiceError("INVALID_REQUEST", "Unknown submission field.")
        normalized = {"profileId": "default", "correlation": None, "rerunOf": None, **body}
        if key in self.jobs:
            if self.bodies[key] != normalized:
                raise ServiceError("IDEMPOTENCY_CONFLICT", "Submission key already used.", 409)
            return self.jobs[key], True
        job = {"id": f"job-{len(self.jobs) + 1}", "state": "queued", "submissionKey": key}
        self.events.append(("submit", key, body))
        self.bodies[key] = normalized
        self.jobs[key] = job
        return job, False

    def get(self, job_id):
        if job_id == "explode":
            raise RuntimeError("SECRET document text /private/source.csv")
        for job in self.jobs.values():
            if job["id"] == job_id:
                return job
        raise ServiceError("NOT_FOUND", "Job not found.", 404)

    def lookup(self, key):
        return self.jobs.get(key)

    def list_jobs(self, **filters):
        self.events.append(("list", filters))
        return {"jobs": list(self.jobs.values()), "nextCursor": None}

    async def cancel(self, job_id):
        self.events.append(("cancel", job_id))
        job = self.get(job_id)
        if job["state"] not in {"succeeded", "failed", "cancelled"}:
            job["state"] = "cancelling"
        return job

    def get_result(self, job_id):
        self.get(job_id)
        return self.manifest

    def product(self, job_id, product_id):
        self.get(job_id)
        if product_id != "product-1":
            raise ServiceError("NOT_FOUND", "Product not found.", 404)
        return self.file, self.manifest["products"][0]

    def read_table(self, job_id, table_id, offset=0, limit=100):
        self.events.append(("table", job_id, table_id, offset, limit))
        return {"offset": offset, "count": 1, "total": 2, "rows": [["001"]],
                "columns": [{"id": "c1"}], "nextOffset": None}

    def read_text(self, job_id, product_id, offset=0, limit=None):
        self.events.append(("text", job_id, product_id, offset, limit))
        return {"text": "bounded", "offset": offset, "nextOffset": None}

    def capabilities(self):
        return {"formats": [".csv"], "limits": self.limits, 'storeScope': 'scope_fixture', 'serviceGeneration': 'generation_fixture'}

    async def refresh_capabilities(self):
        self.events.append('engine-observation')
        return self.capabilities()

    async def reconcile_documents(self, job_id):
        self.events.append(("reconcile", job_id))
        return {"state": "ready"}


@pytest.fixture
def service(tmp_path):
    return FakeService(tmp_path)


@asynccontextmanager
async def client_for(service, token=None):
    app = create_app(service, token=token)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://localhost", follow_redirects=False,
        ) as client,
    ):
        client.headers['X-Processing-Store-Scope'] = service.capabilities()['storeScope']
        yield client


@asynccontextmanager
async def mcp_session(client):
    async with (
        streamable_http_client("http://localhost/mcp", http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def tool_data(result):
    assert not result.isError, result
    return result.structuredContent or json.loads(result.content[0].text)


def tool_error(result):
    assert result.isError
    return json.loads(result.content[0].text)["error"]


async def admit(client, key=None, **body):
    return await client.post(
        "/preprocessing/jobs", headers={"Idempotency-Key": key or str(uuid4())},
        json={"source": {"kind": "workspace-file", "path": "input.csv"}, **body},
    )


async def test_lifespan_http_admission_lookup_and_conflict(service):
    key = str(uuid4())
    async with client_for(service) as client:
        assert service.events == ["start"]
        first = await admit(client, key)
        assert first.status_code == 202
        assert first.headers["location"] == "/preprocessing/jobs/job-1"
        assert (await client.get(first.headers["location"])).json() == first.json()
        replay = await admit(client, key, profileId="default")
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert (await client.get("/preprocessing/jobs", params={"submissionKey": key})).json() == {"job": first.json()}
        assert (await client.get("/preprocessing/jobs", params={"submissionKey": str(uuid4())})).json() == {"job": None}
        conflict = await admit(client, key, source={"kind": "workspace-file", "path": "other.csv"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        missing_key = await client.post("/preprocessing/jobs", json={"source": {}})
        assert missing_key.status_code == 400
        assert missing_key.json()["error"]["code"] == "INVALID_SUBMISSION_KEY"
        assert (await admit(client, unexpected="not allowed")).status_code == 400
    assert service.events[-1] == "close"


@pytest.mark.parametrize("path", ["/preprocessing/capabilities", "/preprocessing/jobs",
                                  "/preprocessing/jobs/job-1/products/product-1", "/mcp", "/missing"])
async def test_bearer_auth_covers_entire_listener(service, path):
    async with client_for(service, token="scoped-secret") as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic scoped-secret"}):
            response = await client.get(path, headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert "scoped-secret" not in response.text
        response = await client.get("/preprocessing/capabilities", headers={"Authorization": "bEaReR scoped-secret"})
        assert response.status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost", "null"])
@pytest.mark.parametrize("path", ["/preprocessing/jobs", "/mcp"])
async def test_private_listener_rejects_browser_origin_mutations(service, origin, path):
    async with client_for(service, token="secret") as client:
        response = await client.post(path, headers={"Authorization": "Bearer secret", "Origin": origin}, json={})
        assert response.status_code == 403
        assert not service.jobs


@pytest.mark.parametrize("path", ["/preprocessing/jobs", "/mcp"])
@pytest.mark.parametrize("chunked", [False, True])
async def test_request_byte_limit_precedes_json_parsing_and_dispatch(service, path, chunked):
    service.limits["requestBytes"] = 128
    consumed = []

    async def chunks():
        for i in range(20):
            consumed.append(i)
            yield b"SECRET" * 10

    async with client_for(service) as client:
        response = await client.post(path, content=chunks() if chunked else b"SECRET" * 100,
                                     headers={"Content-Type": "application/json"})
        assert response.status_code == 413
        assert "SECRET" not in response.text
        assert not service.jobs
        if chunked:
            assert len(consumed) <= 3


@pytest.mark.parametrize("payload", [b"[]", b"null", b'{"source": NaN}', b'{"source":{},"source":{}}', b'{"SECRET":'])
async def test_invalid_json_never_reaches_service_or_echoes_content(service, payload):
    async with client_for(service) as client:
        response = await client.post("/preprocessing/jobs", content=payload,
                                     headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid4())})
        assert response.status_code == 400
        assert "SECRET" not in response.text
        assert not service.jobs


async def test_queries_cancellation_and_manifest_downloads(service):
    async with client_for(service) as client:
        await admit(client)
        for query in ("submissionKey=x&state=queued", "limit=0", "limit=201", "limit=x",
                      "state=unknown", "limit=2&limit=3", "unknown=x"):
            response = await client.get("/preprocessing/jobs?" + query)
            assert response.status_code == 400, query
            assert response.json()["error"]["code"] == "INVALID_QUERY"
        assert (await client.get("/preprocessing/jobs?limit=2&state=queued")).status_code == 200
        assert (await client.post("/preprocessing/jobs/job-1/cancel")).status_code == 202
        next(iter(service.jobs.values()))["state"] = "succeeded"
        assert (await client.post("/preprocessing/jobs/job-1/cancel")).status_code == 200
        assert (await client.get("/preprocessing/jobs/job-1/results")).json() == service.manifest
        url = "/preprocessing/jobs/job-1/products/product-1"
        full = await client.get(url)
        assert full.content == service.file.read_bytes()
        head = await client.head(url)
        assert head.status_code == 200 and head.content == b""
        assert head.headers["content-length"] == str(len(full.content))
        part = await client.get(url, headers={"Range": "bytes=7-10"})
        assert part.status_code == 206 and part.content == b"0123"
        assert part.headers["content-range"] == f"bytes 7-10/{len(full.content)}"
        assert (await client.get(url, headers={"Range": "bytes=999-"})).status_code == 416
        assert (await client.get(url.replace("product-1", "unlisted"))).status_code == 404
        rows = await client.get("/preprocessing/jobs/job-1/tables/table-1/rows?offset=1&limit=2")
        assert rows.json()["rows"] == [["001"]]
        assert ("table", "job-1", "table-1", 1, 2) in service.events
        for query in ("offset=-1", "limit=1001", "offset=1.2", "limit=0", "extra=x"):
            assert (await client.get("/preprocessing/jobs/job-1/tables/table-1/rows?" + query)).status_code == 400


async def test_actual_mcp_sdk_shares_admission_lookup_reads_and_auth(service):
    key = str(uuid4())
    async with client_for(service, token="secret") as client:
        client.headers["Authorization"] = "Bearer secret"
        first = await admit(client, key)
        async with mcp_session(client) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert {"documents_submit", "documents_get_job", "documents_list_jobs", "documents_cancel",
                    "documents_get_result", "documents_read_text", "documents_read_table"} <= tools.keys()
            assert "sessionID" not in json.dumps(tools["documents_submit"].inputSchema)
            replay = tool_data(await session.call_tool("documents_submit", {
                "submissionKey": key, "source": {"kind": "workspace-file", "path": "input.csv"},
            }))
            assert replay == first.json()
            assert tool_data(await session.call_tool("documents_get_job", {"jobId": "job-1"})) == first.json()
            assert tool_data(await session.call_tool("documents_list_jobs", {"submissionKey": key})) == {"job": first.json()}
            conflict = await session.call_tool("documents_submit", {
                "submissionKey": key, "source": {"kind": "workspace-file", "path": "other.csv"},
            })
            assert tool_error(conflict)["code"] == "IDEMPOTENCY_CONFLICT"
            rows = tool_data(await session.call_tool("documents_read_table", {"jobId": "job-1", "tableId": "t", "offset": 2, "limit": 3}))
            assert rows["rows"] == [["001"]]
            assert ("table", "job-1", "t", 2, 3) in service.events
            assert tool_data(await session.call_tool("documents_read_text", {"jobId": "job-1", "productId": "p"}))["text"] == "bounded"
            mcp_key = str(uuid4())
            submitted = tool_data(await session.call_tool("documents_submit", {
                "submissionKey": mcp_key, "source": {"kind": "workspace-file", "path": "input.csv"},
            }))
            http_replay = await admit(client, mcp_key)
            assert http_replay.status_code == 200 and http_replay.json() == submitted
        # Closing an MCP connection is not a cancellation request.
        assert (await client.get("/preprocessing/jobs/job-1")).json()["state"] == "queued"
        assert not any(isinstance(e, tuple) and e[0] == "cancel" for e in service.events)
        assert len(service.jobs) == 2


async def test_real_job_liveness_projects_through_every_http_and_mcp_job_path(tmp_path, monkeypatch):
    from preprocessing.service import Service

    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    # Isolate transport projections from parser scheduling; durable state is real.
    async def idle():
        await service.wake.wait()
        while not service.stopping:
            await asyncio.sleep(.01)
    monkeypatch.setattr(service, '_schedule', idle)
    key = str(uuid4())
    body = {'source': {'kind': 'workspace-file', 'path': 'scan.pdf'}}
    async with client_for(service) as client:
        first = await admit(client, key, **body)
        assert first.status_code == 202
        job = first.json()
        assert job['queueAhead'] == 0 and job['progress'] is None
        assert job['interruptedAt'] is None
        assert job['startedAt'] is None and job['finishedAt'] is None
        async with mcp_session(client) as session:
            second = tool_data(await session.call_tool('documents_submit', {
                'submissionKey': str(uuid4()), **body,
            }))
            assert second['queueAhead'] == 1
            assert second['interruptedAt'] is None
            running = service.get(job['id'])
            running.update(state='running', startedAt='2026-10-14T10:00:00.123456+00:00')
            service.store.save(running)
            await service._record_progress(job['id'], {'id': 'extract', 'completedUnits': 12, 'totalUnits': 80})
            expected = service.get(job['id'])
            assert expected['interruptedAt'] is None
            assert expected['queueAhead'] is None
            assert (await client.get(first.headers['location'])).json() == expected
            assert (await admit(client, key, **body)).json() == expected
            assert (await client.get('/preprocessing/jobs', params={'submissionKey': key})).json() == {'job': expected}
            assert expected in (await client.get('/preprocessing/jobs')).json()['jobs']
            assert tool_data(await session.call_tool('documents_get_job', {'jobId': job['id']})) == expected
            assert tool_data(await session.call_tool('documents_submit', {'submissionKey': key, **body})) == expected
            assert tool_data(await session.call_tool('documents_list_jobs', {'submissionKey': key})) == {'job': expected}
            assert expected in tool_data(await session.call_tool('documents_list_jobs', {}))['jobs']
            stopped = tool_data(await session.call_tool('documents_cancel', {'jobId': job['id']}))
            assert stopped['state'] == 'cancelled' and stopped['stage'] is None
            assert stopped['progress'] == expected['progress']
            assert stopped['interruptedAt'] is None
            assert stopped['startedAt'] == expected['startedAt'] and stopped['finishedAt'] is not None
            assert (await client.post(first.headers['location'] + '/cancel')).json() == stopped
            assert (await client.get('/preprocessing/jobs/' + second['id'])).json()['queueAhead'] == 0
            cancelled = (await client.post('/preprocessing/jobs/' + second['id'] + '/cancel')).json()
            assert cancelled['queueAhead'] is None and cancelled['startedAt'] is None


async def test_internal_session_metadata_denies_only_mutation_tools(service):
    async with client_for(service, token="secret") as client:
        client.headers["Authorization"] = "Bearer secret"
        await admit(client)
        async with mcp_session(client) as session:
            meta = {"ai.opencode/sessionID": "internal-1"}
            denied = await session.call_tool("documents_submit", {
                "submissionKey": str(uuid4()), "source": {"kind": "workspace-file", "path": "input.csv"},
            }, meta=meta)
            assert tool_error(denied)["code"] == "INTERNAL_SESSION_FORBIDDEN"
            denied = await session.call_tool("documents_cancel", {"jobId": "job-1"}, meta=meta)
            assert tool_error(denied)["code"] == "INTERNAL_SESSION_FORBIDDEN"
            assert tool_data(await session.call_tool("documents_get_job", {"jobId": "job-1"}, meta=meta))["state"] == "queued"
            forged = await session.call_tool("documents_cancel", {"jobId": "job-1", "sessionID": "interactive"}, meta=meta)
            assert forged.isError
            assert len(service.jobs) == 1
            assert not any(isinstance(e, tuple) and e[0] == "cancel" for e in service.events)
            assert tool_data(await session.call_tool("documents_cancel", {"jobId": "job-1"}, meta={"ai.opencode/sessionID": "interactive"}))["state"] == "cancelling"


async def test_mcp_scope_fences_mutations_but_not_observation(service):
    async with client_for(service, token='secret') as client:
        client.headers['Authorization'] = 'Bearer secret'
        await admit(client)
        del client.headers['X-Processing-Store-Scope']
        async with mcp_session(client) as session:
            assert tool_data(await session.call_tool('documents_get_job', {'jobId': 'job-1'}))['state'] == 'queued'
            assert tool_error(await session.call_tool('documents_cancel', {'jobId': 'job-1'}))['code'] == 'PROCESSING_STORE_CHANGED'
            assert tool_error(await session.call_tool('documents_submit', {
                'submissionKey': str(uuid4()), 'source': {'kind': 'workspace-file', 'path': 'input.csv'},
            }))['code'] == 'PROCESSING_STORE_CHANGED'
        assert len(service.jobs) == 1
        assert not any(isinstance(e, tuple) and e[0] == 'cancel' for e in service.events)


async def test_mcp_result_summary_is_byte_bounded_and_links_to_full_manifest(service):
    service.limits["indexBytes"] = 2048
    service.manifest["products"] = [
        {"id": f"p{i}", "kind": "text", "path": f"text/{i}.txt", "mediaType": "text/plain",
         "contentUrl": f"/preprocessing/jobs/job-1/products/p{i}", "text": "SECRET" * 1000}
        for i in range(1000)
    ]
    async with client_for(service) as client:
        await admit(client)
        async with mcp_session(client) as session:
            result = tool_data(await session.call_tool("documents_get_result", {"jobId": "job-1"}))
            encoded = json.dumps(result, ensure_ascii=False).encode()
            assert len(encoded) <= 2048
            assert b"SECRET" not in encoded
            assert result["manifestUrl"] == "/preprocessing/jobs/job-1/results"
            assert result["truncated"] is True
            assert 0 < len(result["products"]) < 1000
            assert result["counts"]["products"] == 1000


async def test_transport_errors_do_not_disclose_exception_or_validation_input(service):
    async with client_for(service) as client:
        response = await client.get("/preprocessing/jobs/explode")
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert "SECRET" not in response.text and "/private" not in response.text
        async with mcp_session(client) as session:
            for tool, arguments in (
                ("documents_get_job", {"jobId": "explode"}),
                ("documents_read_table", {"jobId": "j", "tableId": "t", "limit": "SECRET"}),
                ("documents_get_job", {"jobId": "j", "extra": "SECRET"}),
            ):
                result = await session.call_tool(tool, arguments)
                assert result.isError
                assert "SECRET" not in result.model_dump_json()
                assert "/private" not in result.model_dump_json()


@pytest.mark.parametrize("payload", [
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"documents_submit","arguments":{"source":NaN}}}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"documents_submit","name":"documents_cancel"}}',
    b'{"jsonrpc":"SECRET","id":1,"method":"tools/call"}',
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":{"SECRET":"document"}}}',
])
async def test_invalid_mcp_envelopes_are_rejected_without_input_disclosure(service, payload, caplog):
    async with client_for(service) as client:
        response = await client.post("/mcp", content=payload, headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        })
        assert response.status_code == 400
        assert "SECRET" not in response.text
        assert "SECRET" not in caplog.text
        assert not service.jobs


async def test_service_limits_and_errors_are_shared_without_rewriting(service, monkeypatch):
    def unavailable(job_id):
        raise ServiceError("RESULT_NOT_AVAILABLE", "No result is available.", 409, "check-status")

    async def full_queue(key, body):
        raise ServiceError("QUEUE_FULL", "Try again later.", 429, "check-status")

    monkeypatch.setattr(service, "get_result", unavailable)
    monkeypatch.setattr(service, "submit", full_queue)
    async with client_for(service) as client:
        full = await admit(client)
        assert full.status_code == 429 and int(full.headers["retry-after"]) > 0
        unavailable_http = await client.get("/preprocessing/jobs/j/results")
        assert unavailable_http.status_code == 409
        async with mcp_session(client) as session:
            error = tool_error(await session.call_tool("documents_get_result", {"jobId": "j"}))
            for field in ("code", "message", "retry"):
                assert error[field] == unavailable_http.json()["error"][field]


async def test_real_service_cross_surface_admission_and_published_csv(tmp_path):
    service_module = pytest.importorskip("preprocessing.service", reason="Parent Service implementation is not available yet")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.csv").write_text("sample,value\n001,12.3400\n002,5\n")
    service = service_module.Service(tmp_path / "state", workspace, engine=FakeEngine())
    key = str(uuid4())
    rng = random.Random(741)
    async with client_for(service, token="integration-secret") as client:
        client.headers["Authorization"] = "Bearer integration-secret"
        async with mcp_session(client) as session:
            async def submit(index, delay):
                await asyncio.sleep(delay)
                if index % 2:
                    return tool_data(await session.call_tool("documents_submit", {
                        "submissionKey": key, "source": {"kind": "workspace-file", "path": "input.csv"},
                    }))["id"]
                response = await admit(client, key)
                assert response.status_code in {200, 202}, response.text
                return response.json()["id"]

            ids = await asyncio.gather(*(submit(i, rng.uniform(0, .025)) for i in range(16)))
            assert len(set(ids)) == 1
            job_id = ids[0]
            assert (await client.get("/preprocessing/jobs", params={"submissionKey": key})).json()["job"]["id"] == job_id
        # Finishing the MCP transport must not cancel the admitted extraction.
        async with asyncio.timeout(20):
            while True:
                job = (await client.get(f"/preprocessing/jobs/{job_id}")).json()
                if job["state"] not in {"queued", "running", "cancelling"}:
                    break
                await asyncio.sleep(.025)
        assert job["state"] == "succeeded", job
        manifest = (await client.get(f"/preprocessing/jobs/{job_id}/results")).json()
        table_id = manifest["tables"][0]["id"]
        rows = await client.get(f"/preprocessing/jobs/{job_id}/tables/{table_id}/rows")
        assert rows.status_code == 200
        async with mcp_session(client) as session:
            assert tool_data(await session.call_tool("documents_read_table", {
                "jobId": job_id, "tableId": table_id,
            })) == rows.json()
            summary = tool_data(await session.call_tool("documents_get_result", {"jobId": job_id}))
            assert summary["resultId"] == manifest["resultId"]
        exported = next(product for product in manifest["products"] if product["kind"] == "table-export")
        product_url = f"/preprocessing/jobs/{job_id}/products/{exported['id']}"
        download = await client.get(product_url)
        assert download.status_code == 200 and b"001" in download.content
        partial = await client.get(product_url, headers={"Range": "bytes=0-5"})
        assert partial.status_code == 206 and partial.content == download.content[:6]
        history = (await client.get("/preprocessing/jobs")).json()
        assert [job["id"] for job in history["jobs"]] == [job_id]


async def test_engine_reconciliation_is_authenticated_and_reopens_native_admission(tmp_path):
    from preprocessing.service import Service
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.csv").write_text("a,b\n001,2\n")
    engine = FakeEngine()
    engine.available = False
    service = Service(tmp_path / "state", workspace, engine=engine)
    async with client_for(service, token="recovery-secret") as client:
        assert (await client.post("/preprocessing/engine/reconcile")).status_code == 401
        client.headers["Authorization"] = "Bearer recovery-secret"
        unavailable = await admit(client)
        assert unavailable.status_code == 503
        assert not (await client.get("/preprocessing/jobs")).json()["jobs"]
        engine.available = True
        reconciled = await client.post("/preprocessing/engine/reconcile")
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["settled"] is True
        assert "operations" not in reconciled.json()
        admitted = await admit(client)
        assert admitted.status_code == 202
        assert (await client.get("/preprocessing/capabilities")).json()["engineAvailable"] is True
