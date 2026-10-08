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

PDF table recovery has separate geometry and vision policies, plus resumable
evidence. See [PDF table recovery](profiles.md#pdf-table-recovery) for defaults,
budgets, and CLI overrides. Ordinary page and figure OCR are separate from
dedicated table reads.

## Inspect PDF recovery evidence

1. Read the CLI summary's `manifestPath`, including for a partial result.
2. Check `coverage.tableDiscovery`, `coverage.tableConfirmation`, and the
	`table-recovery` and `table-confirmation` stages in the manifest.
3. Follow each table's `evidencePaths` to compare the original candidates,
	selected-row structure, and recovery decisions.
4. Check confirmation basis. Agreement records consistency, not accuracy.
5. Read `work-plan.json` beside the sealed manifest for pending units,
	dependencies, and cumulative `passesSpent`.
6. To resume eligible work through HTTP or MCP, use a new submission UUID and
	the parent's retained snapshot.

The [evidence reference](provenance.md#pdf-table-evidence) describes product
fields. The [resume reference](api.md#resume-extraction) covers compatibility,
explicit force-page invalidation, and local and remote CLI commands.

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

Implement `FormatAdapter` using the `Extraction` and `ExtractionContext` contracts
in [`preprocessing/contracts.py`](../preprocessing/contracts.py). Register the
adapter in [`preprocessing/formats/__init__.py`](../preprocessing/formats/__init__.py).

The coordinator owns identity, hashing, validation, persistence, and publication.
Request model work through `ExtractionContext.model`; adapters do not receive
provider credentials or write the job database. `PDFAdapter` accepts a replacement
`FigureDetector`.

For PDF recovery, use `context.work_remaining()` to check the coordinator's
monotonic work deadline, and `context.resume` for validated reusable evidence.
Emit compact `table.*` events through `context.event` without document cell text.
Keep pure occurrence, grid, comparison, and tile algorithms in
`preprocessing/formats/pdf_tables.py`. Rendering and model effects belong in
`preprocessing/formats/pdf_recovery.py`. Register evidence products before
returning the extraction so sealing can validate their references.

## Add an engine integration

Implement `EnginePort` in [`preprocessing/engine.py`](../preprocessing/engine.py).
`Service.start()` binds durable operation persistence before recovery or dispatch.
The persistence callback must commit both the operation ledger and the ownership
projection before returning. Persist ownership before remote mutations and
preserve uncertain bindings for recovery. The service serializes parser jobs and
model attempts. See the [engine reference](engine.md) for isolation and settlement
requirements.

Honor coordinator-supplied `operation.completionSeconds` in `EnginePort.start`
without changing the profile. Include preparation in that local completion
window, and preserve settlement evidence if the window expires. The coordinator
overwrites worker-supplied completion windows; clients cannot override this
field in a submission.

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
