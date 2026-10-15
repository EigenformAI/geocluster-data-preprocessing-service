# Data preprocessing service

Extract PDF, CSV, TSV, XLSX, and XLSM files into immutable document results.
Each result includes a source snapshot, extraction evidence, coverage, and
navigation files. Use the CLI for individual files or HTTP and MCP for queued jobs.

## Get started

Use Linux, Python 3.11 or later, and [uv](https://docs.astral.sh/uv/).
Processing requires a compatible OpenCode HTTP server, a private connection
descriptor supplied by its owner, and an explicitly selected model. This applies
to CSV, TSV, and workbooks too, although their extraction sends no model prompts.
PDF tables use image confirmation by default, including native-text tables.
Known-tested builds are `2.0.19`, `2.0.19-geocluster.1`, and
`2.0.19-geocluster.2`. Unfamiliar or missing version metadata is advisory.
Contradictory valid versions and failed context or ownership checks block work.

To run the service with a dedicated engine, follow the
[standalone container guide](deployment/standalone/README.md).
To connect to an existing engine, see [engine integration](docs/engine.md).

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

Typed JSONL contains selected table rows. PDF candidate evidence retains original
readings, and structure and decision files explain selection. Agreement records
consistency, not accuracy.
CSV exports and previews are convenience views. Check coverage and findings before
using a partial result. Results retain their source and profile hashes; a later
job does not overwrite earlier evidence.
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

See [resume and recovery](docs/api.md) to continue a sealed result or repair
publication.

## Configure extraction

Pass `--profile /path/to/profile.json` to `process` or `serve` to change limits,
models, or extraction options. Omitted fields use defaults. Profiles apply to the
whole service. PDF submissions can override `tableRecovery` extraction fields.

```json
{
	"id": "small-documents",
	"limits": {"pages": 100, "rows": 100000},
	"options": {"figureDescriptions": false}
}
```

Continue to supply `--model` when using this example. Profile defaults are in
[`preprocessing/contracts.py`](preprocessing/contracts.py); PDF recovery policies
are in [`preprocessing/table_recovery.py`](preprocessing/table_recovery.py).

PDF table recovery defaults to `geometry: auto` and `vision: confirm-all`.
`process` accepts `--table-geometry`, `--table-vision`, and
`--table-force-pages 2,5` overrides.
Table vision `off` leaves ordinary page and figure OCR enabled.
`figureDescriptions: false` disables interpretation, not transcription.
`maxAdditionalPasses` limits optional dispute rereads, not initial confirmation.
Its spending is cumulative across compatible resumes: raising the total from
one spent pass to two grants one more pass. Each resumed job has a fresh work
window and table-call counter. See [resume extraction](docs/api.md#resume-extraction).

## Supported formats

- **CSV and TSV:** preserve decoded values, leading zeros, decimal spelling,
  empty fields, irregular rows, and physical line locations.
- **XLSX and XLSM:** preserve worksheet cells, hidden-sheet metadata, formulas,
  and cached values. Formulas and macros are not executed. Charts, pictures,
  and legacy XLS files are unsupported.
- **PDF:** retain page text, word boxes, original table candidates, selected
  table rows, and image evidence. Geometry recovery and independent image reads
  record unresolved differences instead of silently correcting values.
  OCR transcription is separate from figure interpretation. Figure discovery
  uses embedded images and Figure/Fig. captions; it can miss uncaptioned vector
  figures or include logos and scanned pages.

## Develop locally

Run checks from the repository root:

```sh
uv sync
uv run pytest tests/preprocessing -q
uv run ruff check preprocessing scripts tests/preprocessing
```

The default suite uses synthetic inputs and needs no provider credentials.
To include native-engine tests, set `PREPROCESSING_TEST_OPENCODE` to the absolute
path of the pinned OpenCode executable.

For an isolated local engine, adapt
[`deployment/examples/extraction-engine.json`](deployment/examples/extraction-engine.json)
to your provider, export its named credential variable, then run:

```sh
uv run preprocess dev process /path/to/report.pdf --output /path/to/dev-data \
	--opencode-executable /opt/opencode-2.0.19/bin/opencode \
	--engine-config /private/extraction-engine.json \
	--credential-env DEV_OPENCODE_PROVIDER_TOKEN --model deployment/vision
```

Use `preprocess dev serve --help` for a local HTTP and MCP owner.
The launcher uses isolated state and forwards only named credentials.
Read [source provenance](docs/provenance.md) for upstream attribution.

## License

This project is licensed under the [Apache License, Version 2.0](LICENSE).
