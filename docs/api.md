# HTTP and MCP reference

HTTP and MCP share one job queue and result store. The default listener is
`http://127.0.0.1:8765`; the Streamable HTTP MCP endpoint is `/mcp`.
When authentication is enabled, both accept `Authorization: Bearer <token>`.
Requests with an `Origin` header cannot mutate jobs.

## Submit jobs

`POST /preprocessing/jobs` requires `Content-Type: application/json` and one
UUID `Idempotency-Key`. A new submission returns HTTP 202; an identical replay
returns HTTP 200 and the original job. Reusing a key with a different request
fails.

```json
{
	"source": {"kind": "workspace-file", "path": "input.csv"},
	"profileId": "default"
}
```

`profileId` defaults to `default`, which selects the service's configured profile.
An explicit ID must be `default` or the configured profile's ID.
Workspace paths are relative to `--workspace`; symlink traversal is rejected.
An optional `expectedSha256` on a workspace source checks the input hash.
To process retained bytes, use `{"kind":"snapshot","snapshotId":"SOURCE_ID"}`
as the source. The optional `rerunOf` field identifies a prior job; a rerun still
requires a new submission UUID. Upload sources require an external upload owner;
the shipped CLI does not configure one.

## HTTP routes

| Method | Path | Result |
| --- | --- | --- |
| GET | `/preprocessing/capabilities` | Formats, resolved profile, limits, and dependency readiness. |
| POST | `/preprocessing/jobs` | Submit a job. |
| GET | `/preprocessing/jobs` | Job history or submission-key lookup. |
| GET | `/preprocessing/jobs/{job_id}` | Current job state. |
| POST | `/preprocessing/jobs/{job_id}/cancel` | Request cancellation. |
| POST | `/preprocessing/jobs/{job_id}/reconcile` | Retry publication without extraction. |
| GET | `/preprocessing/jobs/{job_id}/results` | Full immutable manifest. |
| GET | `/preprocessing/jobs/{job_id}/products/{product_id}` | Complete product download. |
| GET | `/preprocessing/jobs/{job_id}/tables/{table_id}/rows` | Bounded typed rows. |
| POST | `/preprocessing/engine/reconcile` | Recheck engine-owner evidence and attempt to reopen admission. |

Job history accepts `cursor`, `limit`, `state`, `snapshotId`, and `uploadBatchId`.
`limit` defaults to 50 and permits 1–200 entries. A `submissionKey` lookup cannot
be combined with history filters. Table reads accept a zero-based `offset` and
a `limit` of 1–1,000 rows, defaulting to 100.

## MCP tools

| Tool | Result |
| --- | --- |
| `documents_submit` | Submit immediately using a `submissionKey` UUID and the same source fields as HTTP. |
| `documents_get_job` | Current job state. |
| `documents_list_jobs` | History with the same filters as HTTP. |
| `documents_cancel` | Request cancellation. |
| `documents_get_result` | Bounded navigation and a link to the full manifest. |
| `documents_read_table` | Typed rows using `jobId`, `tableId`, `offset`, and `limit`. |
| `documents_read_text` | Text using `jobId`, `productId`, and UTF-8 byte offsets. |

Poll `documents_get_job` after submission. Result summaries may be truncated;
use `manifestUrl` for the complete manifest. Text reads return continuation
offsets and obey `textBytes`. Table reads also obey `responseBytes`. A single
oversized row returns `ROW_TOO_LARGE`; oversized column metadata returns
`RESPONSE_TOO_LARGE`. Both include complete-download references.

## Job state and recovery

Jobs move through `queued`, `running`, and, when requested, `cancelling`.
Terminal states are `succeeded`, `partial`, `failed`, `cancelled`, and
`interrupted`. `cancelling` does not mean settlement is complete. Cancellation
reports local settlement; remote provider outcomes can remain unknown.

Extraction and publication have separate states. To repair publication through
a running owner:

```sh
uv run preprocess reconcile --url http://127.0.0.1:8765 --job JOB_ID
```

For an authenticated owner, add `--token-env PREPROCESSING_TOKEN`.
After stopping the owner, use the local administrative commands instead:

```sh
uv run preprocess reconcile --data-root /path/to/data --job JOB_ID
uv run preprocess status --data-root /path/to/data --job JOB_ID
```

Local administration needs no engine descriptor or model. It fails with
`OWNER_ACTIVE` if another process owns the root. Reconciliation validates and
publishes the sealed result without extracting again; exit status is zero only
when publication is `ready`. Remote errors never trigger a local fallback.

If engine ownership is unresolved, the server owner must first supply fencing
or replacement evidence described in the [engine reference](engine.md).
Then call `POST /preprocessing/engine/reconcile`. Neither recovery operation
resends prompts. Interrupted extraction requires a new submission.
