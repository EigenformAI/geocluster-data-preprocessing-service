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

### Table recovery overrides

PDF submissions accept a top-level `tableRecovery` object. Its fields override
`options.tableRecovery` in the configured profile. Other options, models,
credentials, and resource limits cannot be overridden per request. Omitted
fields inherit administrator defaults. `forcePages` replaces the whole list.
The resolved policy and profile digest appear in the job and sealed manifest.
See [PDF table recovery](profiles.md#pdf-table-recovery) for all fields,
`geometry: auto`, `vision: confirm-all` defaults, and budget scopes.

```json
{
	"source": {"kind": "workspace-file", "path": "report.pdf"},
	"tableRecovery": {
		"vision": "on-demand",
		"forcePages": [2, 5],
		"maxModelCalls": 20
	}
}
```

Overrides form part of submission identity. `forcePages` is sorted and
deduplicated, and integral numbers such as `renderDpi: 300.0` normalize to `300`.
Equivalent normalized requests replay the same job. Changing an override under
the same UUID returns `IDEMPOTENCY_CONFLICT` (HTTP 409), even when both requests
resolve to the same effective policy. An omitted object and an explicit `{}`
are different requests. A new extraction, rerun, or resume requires a fresh UUID.

Unknown fields, invalid types, and invalid ranges return `INVALID_REQUEST`.
A valid override on a supported non-PDF source is admitted, then the job fails
during processing with `UNSUPPORTED_OPTION`. An out-of-range physical PDF page
fails during processing with `INVALID_REQUEST`. Neither check is an admission
check of the source file.

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
| `documents_submit` | Submit immediately using a `submissionKey` UUID and the same submission fields as HTTP, including `tableRecovery`, `rerunOf`, and `resumeOf`. |
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

Table summaries include `recovery`, `confirmation`, and `evidencePaths` when
present. Typed rows contain the canonical selected readings. Original candidate
rows, cell spans, images, and comparison decisions are separate products; see
[PDF table evidence](provenance.md#pdf-table-evidence).

## Resume extraction

`resumeOf` identifies a terminal parent job with a sealed PDF result, retained
source bytes, and a valid work plan. Partial results are eligible. Publication
may still be pending or failed; resume reads the private sealed generation.
The source must be exactly the parent's snapshot reference. `resumeOf` and
`rerunOf` are mutually exclusive. `rerunOf` records a prior job without reusing
its completed work.

```json
{
	"source": {"kind": "snapshot", "snapshotId": "SOURCE_ID"},
	"resumeOf": "JOB_ID",
	"tableRecovery": {
		"maxAdditionalPasses": 2,
		"maxModelCalls": 20,
		"workSeconds": 3000
	}
}
```

Replace both IDs with the parent's values and submit to `POST /preprocessing/jobs`
with a new `Idempotency-Key`. MCP uses `documents_submit` with the same fields
and a new `submissionKey`. The example's work window requires `jobSeconds > 3000`.

Resume creates a new job and result generation. It validates the retained
source hash, sealed manifest, products, work-plan identity, and dependency
closure before reuse. Completed evidence is copied into independent files.
The child preserves completed selections, render and model metadata, comparison
history, and spent additional passes. Product IDs and download URLs belong to
the new generation. The parent remains immutable.

The semantic fingerprint must match the source, service build, PDF adapter,
parser versions, extraction options, requested models, and prompt and schema
versions. Changes to `geometry`, `vision`, or `renderDpi` are incompatible.
`forcePages`, `workSeconds`, `maxModelCalls`, `maxAdditionalPasses`, and resource
limits can change, subject to retained evidence fitting the new limits.
Semantic changes return `RESUME_INCOMPATIBLE`. Invalid evidence or unsettled
parent model work returns `RESUME_INVALID`. An active, unsealed, or non-PDF
parent or a different snapshot returns `INVALID_REQUEST`.

Only `forcePages` explicitly supplied in the new submission invalidates work
on those pages and dependent units, including page OCR and figure work.
An inherited force list does not invalidate completed units. Ordinary resumes
reuse completed work and continue unfinished units. Additional-pass spending
is cumulative per reusable table occurrence: a spent pass is not restored by
resume. Each child has a fresh work window and table-call cap. Raise
`maxAdditionalPasses` to grant more total dispute passes.

The CLI builds the retained-snapshot request and generates a new UUID:

```sh
uv run preprocess resume --url http://127.0.0.1:8765 --job JOB_ID \
	--table-force-pages 2
```

Remote resume prints the admitted job and exits `0` without waiting for
extraction. Poll its new job ID. Add `--token-env PREPROCESSING_TOKEN` for an
authenticated owner. Remote resume uses the configured server profile;
`--profile` and `--model` return `UNSUPPORTED_OPTION` with `--url`.

After stopping the owner, local resume opens an exclusive processing owner:

```sh
uv run preprocess resume --data-root /path/to/data --job JOB_ID \
	--engine-descriptor /private/engine.json
```

Local resume uses the parent's effective profile unless `--profile` or `--model`
is supplied. It requires engine readiness and an engine descriptor, waits for
completion, and prints the child job. Exit status is `0` for success, `2` for
partial, and `1` otherwise. Another owner returns `OWNER_ACTIVE`.
The table flags and budget fields are described in the
[profile reference](profiles.md#cli-overrides).

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

Local status and reconciliation need no engine descriptor or model. They return
`OWNER_ACTIVE` if another process owns the root. Reconciliation validates and
publishes the sealed result without extracting again; exit status is zero only
when publication is `ready`. Remote errors never trigger a local fallback.

If engine ownership is unresolved, the server owner must first supply fencing
or replacement evidence described in the [engine reference](engine.md).
Then call `POST /preprocessing/engine/reconcile`. Neither recovery operation
resends prompts. Unsealed interrupted extraction requires a new extraction
submission. It has no sealed work plan to resume.
