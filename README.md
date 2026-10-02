# Data preprocessing service

Extract PDF, CSV, TSV, XLSX, and XLSM files into versioned document products.
The service retains source snapshots, typed tables, PDF evidence, coverage,
and navigation files for people and agents. CLI, HTTP, and MCP use the same
job owner and result store.

## Process a file

Use Linux, Python 3.11 or later, and [uv](https://docs.astral.sh/uv/).
The worker uses Linux process controls. Run these commands from the checkout:

```sh
uv sync
uv run preprocess process /path/to/input.csv --output /path/to/data \
	--engine-descriptor /private/engine.json --model deployment/vision
```

`python -m preprocessing.cli` also runs the CLI. `process` resolves the source
path, uses its parent as the workspace, and submits its basename. Each invocation
creates a new job. Production `process` and `serve` require an externally owned
OpenCode **2.0.19** HTTP server and explicit model selection, even for native files.
The server owner supplies the private descriptor and configures provider credentials.
Native extraction checks readiness but creates no model session or prompt.
See [engine configuration and ownership](docs/engine.md) for descriptor fields,
standalone supervision, and isolated development commands.

For an opt-in container with a dedicated OpenCode 2.0.19 engine, follow
[Run the standalone container](deployment/standalone/README.md). Its image and
pair supervisor are separate from the ordinary Python wheel. Runtime deployments
can install that wheel and supply their own shared-engine descriptor.

The command writes one JSON object to stdout. Service events go to stderr.
The output includes `jobId`, `state`, `resultId`, `manifestPath`,
`documentsPath`, `documents`, and `error`.

- `manifestPath` points to the sealed, immutable manifest under
  `.preprocessing/jobs/<job-id>/sealed/manifest.json`.
- `documentsPath` points to the shared generation, `documents/<result-id>`.
  It is null if shared publication has not succeeded.
- `documents.state` records publication separately from extraction success.
- Exit status is **0** for `succeeded`, **2** for `partial`, and **1** for
  `failed`, `cancelled`, or `interrupted`. CLI and I/O errors return 1.
  Argument syntax errors return 2 and do not create a job.

Read `documents/index.md` to find ready generations. Each generation includes
`AGENTS.md`, `index.md`, a manifest, and manifest-listed products with SHA-256
hashes. Typed JSONL contains the table evidence. CSV exports and previews are
convenience views. Match the source hash and pipeline profile before reusing a
result. A later job does not replace earlier evidence.

## Run the HTTP and MCP owner

```sh
uv run preprocess serve \
	--data-root /path/to/data \
	--workspace /path/to/inputs \
	--host 127.0.0.1 --port 8765 \
	--engine-descriptor /private/engine.json --model deployment/vision
```

The default listener is `127.0.0.1:8765`. One process owns each data root.
HTTP workspace paths are relative to `--workspace`; symlink traversal is rejected.
The app starts and closes the service through its lifespan.

To require authentication, supply the **name** of an environment variable:

```sh
read -r -s -p 'Service token: ' PREPROCESSING_TOKEN
export PREPROCESSING_TOKEN
uv run preprocess serve \
	--data-root /path/to/data --workspace /path/to/inputs \
	--host 0.0.0.0 --port 8765 --token-env PREPROCESSING_TOKEN \
	--engine-descriptor /private/engine.json --model deployment/vision
```

Non-loopback hosts require a token. A named token variable must contain a
nonempty printable ASCII value without spaces, including for loopback listeners.
There is no token-value argument. HTTP and MCP accept the same
`Authorization: Bearer <token>` header. Browser mutation requests carrying an
`Origin` header are rejected; project-proxy integration remains pending.

To submit a file to the loopback owner without authentication:

```sh
curl --fail-with-body http://127.0.0.1:8765/preprocessing/jobs \
	-H 'Content-Type: application/json' \
	-H 'Idempotency-Key: 3548179d-27ee-48f8-9430-e58d5f0c6fe3' \
	-d '{"source":{"kind":"workspace-file","path":"input.csv"}}'
uv run preprocess status --url http://127.0.0.1:8765 --job JOB_ID
```

Reuse the submission UUID with the identical request to recover that job.
Use a new UUID for a new extraction. The API accepts an optional `rerunOf` job
ID. Submit `{"kind":"snapshot","snapshotId":"SOURCE_ID"}` as the source to
rerun the retained exact bytes, even if the workspace file has changed. Upload
receipts are not connected to an upload owner yet.

The HTTP routes are:

| Method | Path | Result |
| --- | --- | --- |
| GET | `/preprocessing/capabilities` | Enabled formats, resolved profile, limits, dependency readiness, execution mode, and deferred integrations. |
| POST | `/preprocessing/engine/reconcile` | Recheck owner evidence, settle known operations, and reopen admission when safe. |
| POST | `/preprocessing/jobs` | Admit a job with a UUID `Idempotency-Key`. |
| GET | `/preprocessing/jobs` | Job history or submission-key lookup. |
| GET | `/preprocessing/jobs/{job_id}` | Current job state. |
| POST | `/preprocessing/jobs/{job_id}/cancel` | Request cancellation. |
| POST | `/preprocessing/jobs/{job_id}/reconcile` | Retry shared publication without extraction. |
| GET | `/preprocessing/jobs/{job_id}/results` | Full immutable manifest. |
| GET | `/preprocessing/jobs/{job_id}/products/{product_id}` | Complete product download. |
| GET | `/preprocessing/jobs/{job_id}/tables/{table_id}/rows` | Bounded typed rows. |

## Connect an MCP client

Configure your client's Streamable HTTP transport with
`http://127.0.0.1:8765/mcp`. Supply the bearer header if the owner uses a token.
The same listener exposes these tools:

- `documents_submit`, `documents_get_job`, and `documents_list_jobs`
- `documents_cancel` and `documents_get_result`
- `documents_read_table` and `documents_read_text`

`documents_submit` admits work immediately. Poll `documents_get_job` for its
outcome. `documents_get_result` returns bounded navigation with a link to the
full manifest. Table reads accept 1–1,000 rows, default to 100 rows, and also obey
the response-byte limit. A single oversized row returns `ROW_TOO_LARGE` with
complete-download references. Oversized column metadata returns
`RESPONSE_TOO_LARGE` with the same downloads. Text reads use UTF-8 byte offsets and continuation
offsets. Job lists default to 50 entries and allow at most 200 per page.

## Select a model engine

Production commands connect through `OpenCodeServerEngine`. They do not launch,
install, or restart OpenCode. Supply `--engine-descriptor PATH` or set
`PREPROCESSING_ENGINE_DESCRIPTOR` to the descriptor path. Select every model
role through `--model provider/model` or an explicit `models` object in a profile.
The current role is `vision`; `--model` overrides all roles. There is no automatic
model fallback or native-only production mode.

For development, use a pinned executable and a private extraction-only configuration:

```sh
uv run preprocess dev process /path/to/report.pdf --output /path/to/dev-data \
	--opencode-executable /opt/opencode-2.0.19/bin/opencode \
	--engine-config /private/extraction-engine.json \
	--credential-env DEV_OPENCODE_PROVIDER_TOKEN --model deployment/vision
```

`preprocess dev serve` accepts the same development engine options plus the
ordinary listener and workspace options. The launcher starts ordinary HTTP
`opencode serve` in private HOME and XDG roots under `.preprocessing/dev-engine`.
It forwards only the named credential variables. It refuses launch when any
`FLY_*` environment variable is present. The default idle shutdown is 60 seconds;
`--idle-seconds 0` stops immediately after an operation, and `--keep-alive` retains
the engine until launcher exit. Both modes stop their owned engine on exit.

Missing connection configuration fails startup. An unavailable configured engine
blocks new jobs and leaves dependency state visible through capabilities.
Status, existing results, cancellation, and publication repair remain available.
During an admitted PDF job, failed model operations can still produce a partial
result when native evidence is usable. No usable extraction product means failure.

Runtime deployment requires one runtime-owned standalone HTTP server shared by
chat and extraction. The separate runtime must deliver the descriptor, supervise
the service, settle abandoned extraction sessions, and filter those sessions from
chat history. That runtime integration is not implemented in this repository.

## Set extraction limits

Pass `--profile /path/to/profile.json` to `process` or `serve`. The file is a
trusted-administrator configuration, not a per-request override. For example:

```json
{
	"id": "small-documents",
	"limits": {"pages": 100, "rows": 100000},
	"options": {"figureDescriptions": false},
	"models": {"vision": "deployment/vision"},
	"retry": {"attempts": 12, "noProgress": 4}
}
```

Omitted fields merge with defaults. Accepted top-level fields are `id`, `limits`,
`options`, `models`, and `retry`. Unknown fields, duplicate JSON keys, and
credential or environment fields are rejected. The file may contain at most
65,536 bytes. The profile ID accepts 1–128 ASCII letters, digits, dots,
underscores, or hyphens. Model values use `provider/model` identifiers.

Limits are positive integers up to Python's `sys.maxsize`. Administrators can
raise or lower them. A configured limit is enforced, not advisory.

| Limit | Default | Scope |
| --- | ---: | --- |
| `inputBytes` | 268,435,456 | Source snapshot bytes. |
| `outputBytes` | 536,870,912 | Accepted product bytes and per-file size; staged totals are also checked before model calls and sampled every 50 ms during parsing. |
| `expandedBytes` | 536,870,912 | Sum of workbook ZIP member sizes. |
| `pages` | 1,000 | Physical PDF pages. |
| `rows` | 2,000,000 | Delimited rows, workbook rows, or PDF table rows per document. |
| `columns` | 16,384 | Columns in a table row. |
| `pixels` | 16,000,000 | Pixels per PDF render and total image pixels per model operation. |
| `queueDepth` | 100 | Nonterminal jobs admitted by the owner. |
| `requestBytes` | 65,536 | HTTP and MCP request body. |
| `responseBytes` | 262,144 | Typed-row response and accumulated model text; not a cap on full downloads or manifests. |
| `textBytes` | 32,768 | Text-read chunk and model task text. |
| `indexBytes` | 32,768 | Navigation and MCP result-summary budget. |
| `jobSeconds` | 3,600 | Processing timeout and worker CPU soft limit; local I/O and engine settlement complete before ownership is released. |
| `modelSeconds` | 180 | Each model prompt attempt. |
| `workerMemoryBytes` | 2,147,483,648 | Parser address-space limit, excluding the owner and model engine. |
| `orphanGraceSeconds` | 86,400 | Startup grace before removing unpublished staging/sealed outputs of fenced terminal jobs. |
| `modelImages` | 4 | Images in one model operation. |
| `modelImageBytes` | 8,388,608 | Total image-file bytes before base64 in one operation. |

`modelImages` and `modelImageBytes` use engine defaults when absent from the
resolved profile. Job admission requires free space of at least
`inputBytes + 2 × outputBytes`: 1,342,177,280 bytes with the defaults.
This check does not reserve disk space or limit total retained storage.
The worker CPU hard limit is `jobSeconds + 1`. The HTTP engine also applies a
16 MiB request/response limit, a 30-second HTTP request deadline, and a 10-second
cleanup-attempt deadline. Cancellation can wait up to 131 seconds for an active
operation with those defaults. Remote CLI responses are limited to 16 MiB
with a 30-second network timeout.

Options are `encoding` (default `utf-8-sig`), an optional one-character
`delimiter`, and boolean `figureDescriptions` (default `true`). The default
delimiter is comma for CSV and tab for TSV. Disabling descriptions does not
disable figure OCR. `figureDetector` accepts `minDimensionPoints` (default 16;
0 disables size filtering), `groupOverlaps` (default true), and `captionPage`
(default true). Caption pages subsume embedded fragments; otherwise overlapping
images are grouped before size filtering. Detection evidence records exclusions.

The profile retains `retry.attempts` (default 24), `retry.noProgress` (default 8),
and optional `retry.backoffSeconds`. The CLI validates these fields, but the current
HTTP adapter makes one prompt attempt and does not use retry counts or backoff.
`retryAmbiguous` must be false. An intentional repeat requires a new job.
One client prompt does not bound hidden provider requests or establish remote
cancellation, deletion, refunds, or zero charges.

## Repair shared publication and inspect status

After stopping the owner, run:

```sh
uv run preprocess reconcile --data-root /path/to/data --job JOB_ID
uv run preprocess status --data-root /path/to/data --job JOB_ID
```

Both commands open the root as an exclusive admin-only owner. They need no engine
descriptor or model. They preserve unsettled model bindings for supervised recovery.
They fail with `OWNER_ACTIVE` if another owner holds the lock. Reconciliation
copies and validates the sealed result without rerunning extraction. It prints
the publication object and exits 0 only when its state is `ready`.

To inspect a running owner, use `status --url`, with `--token-env` if needed.
Omit `--job` to list recent jobs. `reconcile --url` sends
`POST /preprocessing/jobs/{job_id}/reconcile` and never opens the local root.
The HTTP adapter exposes that route. HTTP errors never trigger a local fallback.

After the server owner supplies fencing or replacement evidence, reconcile engine
ownership through the running service:

```sh
curl --fail-with-body -X POST http://127.0.0.1:8765/preprocessing/engine/reconcile
```

Supply the service bearer header when authentication is enabled. This operation
never resends a prompt. Interrupted extractions require an explicit new job.

## Supported extraction and extension points

- **CSV and TSV:** preserve decoded text values, leading zeros, decimal spelling,
  empty fields, irregular row lengths, and physical line locations. Source
  snapshots retain original quoting and line endings.
- **XLSX and XLSM:** read worksheet cells, hidden-sheet metadata, stored numeric
  strings, date representation, formula expressions, and cached formula values.
  The service does not execute formulas or macros, refresh external links, or
  extract workbook charts and pictures. Legacy XLS is unsupported.
- **PDF:** retain physical-page text, word boxes, native tables, and image
  evidence. OCR transcriptions are separate from figure interpretations.
  Discovery uses embedded-image rectangles and Figure/Fig. caption pages.
  It can miss uncaptioned vector figures or include logos and scanned pages.
  Caption-page bounds are whole-page evidence, not precise segmentation.

To add a format, implement `FormatAdapter.extract(snapshot, profile, output_dir,
context)` and register its extensions in `preprocessing/formats/__init__.py`.
Return coverage, stages, products, tables, figures, findings, and provenance.
The coordinator hashes and validates products before publication. Parsers request
model operations through `ExtractionContext.model`; they do not receive provider
credentials or write the job database. `PDFAdapter` accepts a replacement
`FigureDetector`. Engine integrations implement `bind`, `ready`, `recover`,
`start`, `cancel`, and `close`. `Service` binds durable operation persistence
before recovery or model dispatch.

Runtime upload receipts, project-proxy wiring, shared-engine ownership integration,
R2 synchronization, and billing reconciliation remain pending. Snapshots and
published results are retained for the data-root lifetime. Startup reclaims only
unpublished staging/sealed outputs after exclusive ownership and the grace period.
Failed snapshot copies are removed immediately when possible; unreferenced copies
left by a crash are reclaimed after the same grace period. Referenced snapshots
are retained.
Interrupted jobs require an explicit new submission for another extraction.
Cancellation state reports local settlement; remote provider outcomes may be unknown.

## Run the tests

```sh
uv run pytest tests/preprocessing -q
uv run ruff check preprocessing/cli.py tests/preprocessing/test_cli.py
```

Tests generate synthetic inputs in temporary directories. The default suite does
not need provider credentials. Optional real-engine tests and the explicit live
probe are described in the engine reference.

The legacy extraction and assessment trees, their four report-analysis skills,
and their launch scripts are retired. The shipped command is `preprocess`; the
wheel contains `preprocessing`. See the [import log](docs/import-log.md) for
pinned upstream and helper provenance.
