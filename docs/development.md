# Develop the service

Use Linux, Python 3.11 or later, and uv. Run commands from the repository root.

## Run checks

```sh
uv sync
uv run pytest tests/preprocessing -q
uv run ruff check preprocessing scripts tests/preprocessing
```

Tests use synthetic inputs in temporary directories. The default suite needs
no provider credentials. Optional native-engine tests use a deterministic
loopback provider:

```sh
PREPROCESSING_TEST_OPENCODE=/opt/opencode-2.0.19/bin/opencode \
	uv run pytest tests/preprocessing/test_engine_http_real.py -q
```

## Run an isolated engine

Install the pinned OpenCode 2.0.19 executable. Adapt
[the extraction configuration](../deployment/examples/extraction-engine.json)
to your provider and export its named credential variable.

```sh
uv run preprocess dev process /path/to/report.pdf --output /path/to/dev-data \
	--opencode-executable /opt/opencode-2.0.19/bin/opencode \
	--engine-config /private/extraction-engine.json \
	--credential-env DEV_OPENCODE_PROVIDER_TOKEN --model deployment/vision
```

`preprocess dev serve` accepts the same engine options with `--data-root` and
`--workspace` instead of a source and `--output`. State lives under
`ROOT/.preprocessing/dev-engine`; private state directories require mode `0700`.
The launcher forwards only named credentials and refuses launch when any `FLY_*`
variable is present. The idle timeout is 60 seconds. `--idle-seconds 0` stops
after each operation; `--keep-alive` retains the engine until launcher exit.
Both modes stop their owned engine on exit.

## Probe a configured engine

Use a dedicated data root whose ownership projection the server owner watches.
The probe refuses an active service owner and sends real provider requests:

```sh
uv run python scripts/probe_engine.py \
	--engine-descriptor /private/engine.json --data-root /path/to/probe-data \
	--model deployment/vision --seconds 90 --cancel
```

It checks exact text, an image containing `SAMPLE 42` and `AXIS 17`, and optional
cancellation followed by a fresh operation. It prints JSON and retains the
ledger, image, model output, usage, and ownership projection in the reported
root. It creates no published document results. This checks engine integration,
not extraction quality on real documents.

## Add a format

Implement `FormatAdapter.extract(snapshot, profile, output_dir, context)` and
register extensions in `preprocessing/formats/__init__.py`. Return coverage,
stages, products, tables, figures, findings, and provenance as defined by
`Extraction` in `preprocessing/contracts.py`.

The coordinator owns identity, hashing, validation, persistence, and publication.
Request model work through `ExtractionContext.model`; adapters do not receive
provider credentials or write the job database. `PDFAdapter` accepts a replacement
`FigureDetector`.

## Add an engine integration

Implement `EnginePort`: `bind`, `ready`, `recover`, `start`, `cancel`, and `close`.
`Service.start()` binds durable operation persistence before recovery or dispatch.
Persist ownership before remote mutations and preserve uncertain bindings for
recovery. The service serializes parser jobs and model attempts. See the
[engine reference](engine.md) for isolation and settlement requirements.

## Build the wheel

```sh
uv build --wheel
```

The wheel packages `preprocessing` and registers `preprocess` and
`preprocess-engine-owner`. OpenCode and the container supervisor are separate
deployment components.

When upgrading OpenCode, update the pinned version in the engine, launcher,
Dockerfile, and container supervisor together. Update executable paths in
`deployment/standalone/compose.yaml` and
`deployment/examples/preprocessing-engine-owner.service` to match.

[Source provenance](provenance.md) records the upstream extraction lineage.
