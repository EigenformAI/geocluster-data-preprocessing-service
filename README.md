# Data preprocessing service

Extract PDF, CSV, TSV, XLSX, and XLSM files into immutable document results.
Each result includes a source snapshot, extraction evidence, coverage, and
navigation files. Use the CLI for individual files or HTTP and MCP for queued jobs.

## Get started

Use Linux, Python 3.11 or later, and [uv](https://docs.astral.sh/uv/).
Processing requires an OpenCode **2.0.19** HTTP server, a private connection
descriptor supplied by its owner, and an explicitly selected model. This applies
to native formats too, although native extraction sends no model prompts.

To run the service with a dedicated engine, follow the
[standalone container guide](deployment/standalone/README.md).
To connect to an existing engine, see the [engine reference](docs/engine.md).

Run these commands from the checkout:

```sh
uv sync
uv run preprocess process /path/to/input.csv --output /path/to/data \
	--engine-descriptor /private/engine.json --model deployment/vision
```

Replace `deployment/vision` with a `provider/model` configured on your engine.
Each invocation creates a new job and prints a JSON summary to stdout. Events
go to stderr. Exit status is `0` for success, `2` for a partial result, and `1`
for a failed, cancelled, or interrupted job. Argument syntax errors return `2`.

## Read results

Open `documents/index.md` under the output directory to find published results.
Each `documents/<result-id>` generation contains an `AGENTS.md` guide, an index,
a manifest, and products with SHA-256 hashes.

The CLI summary includes:

- `manifestPath`: the sealed manifest, available even if shared publication fails.
- `documentsPath`: the published generation, or null until publication succeeds.
- `documents.state`: publication status, separate from the extraction outcome.

Typed JSONL contains table evidence. CSV exports and previews are convenience
views. Check coverage and findings before using a partial result. Results retain
their source and profile hashes; a later job does not overwrite earlier evidence.
Snapshots and published results remain for the data-root lifetime.

## Serve HTTP and MCP

```sh
uv run preprocess serve \
	--data-root /path/to/data --workspace /path/to/inputs \
	--engine-descriptor /private/engine.json --model deployment/vision
```

The default address is `http://127.0.0.1:8765`. Only one process can own a data
root. File paths submitted to the server are relative to `--workspace`.

Submit a file, then replace `JOB_ID` with the returned ID:

```sh
curl --fail-with-body http://127.0.0.1:8765/preprocessing/jobs \
	-H 'Content-Type: application/json' \
	-H 'Idempotency-Key: 3548179d-27ee-48f8-9430-e58d5f0c6fe3' \
	-d '{"source":{"kind":"workspace-file","path":"input.csv"}}'
uv run preprocess status --url http://127.0.0.1:8765 --job JOB_ID
```

Reuse the UUID with the identical request to retrieve the same job. Use a new
UUID for a new extraction.

For MCP, configure your client's Streamable HTTP transport with
`http://127.0.0.1:8765/mcp`. Submit with `documents_submit`, poll
`documents_get_job`, and read results with `documents_get_result`,
`documents_read_table`, or `documents_read_text`.

To require authentication, export a token and add `--token-env PREPROCESSING_TOKEN`
to `serve`. HTTP and MCP clients use `Authorization: Bearer <token>`.
Non-loopback listeners require authentication. Tokens must contain printable
ASCII characters without spaces. The service does not load `.env` files.

See the [HTTP and MCP reference](docs/api.md) for routes, pagination, and recovery.

## Configure extraction

Pass `--profile /path/to/profile.json` to `process` or `serve` to change limits,
models, or extraction options. Omitted fields use defaults. Profiles apply to the
whole service, not individual requests.

```json
{
	"id": "small-documents",
	"limits": {"pages": 100, "rows": 100000},
	"options": {"figureDescriptions": false}
}
```

Continue to supply `--model` when using this example. See the
[profile reference](docs/profiles.md) for supported fields and limits.

## Supported formats

- **CSV and TSV:** preserve decoded values, leading zeros, decimal spelling,
  empty fields, irregular rows, and physical line locations.
- **XLSX and XLSM:** preserve worksheet cells, hidden-sheet metadata, formulas,
  and cached values. Formulas and macros are not executed. Charts, pictures,
  and legacy XLS files are unsupported.
- **PDF:** retain page text, word boxes, native tables, and image evidence.
  OCR transcription is separate from figure interpretation. Figure discovery
  uses embedded images and Figure/Fig. captions; it can miss uncaptioned vector
  figures or include logos and scanned pages.

For local development, tests, and format extensions, see the
[development guide](docs/development.md).
