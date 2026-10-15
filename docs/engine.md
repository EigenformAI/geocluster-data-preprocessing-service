# Integrate an OpenCode engine

The service requires a compatible OpenCode HTTP server and an explicitly selected
model, including for native formats. The production CLI connects to an existing
server. Use the [standalone container](../deployment/standalone/README.md) for a
dedicated supervised engine, or [local development](../README.md#develop-locally)
for an isolated launcher.

## Connect an existing engine

The server owner must supply a private JSON descriptor and publish updates
atomically. Pass its path as `--engine-descriptor` or
`PREPROCESSING_ENGINE_DESCRIPTOR`. The descriptor contains server credentials;
keep it separate from the credential-free extraction profile.

Record the server's full bare version in the descriptor, including the Geocluster
suffix when present. Use `null` when no valid version was observed.
Available valid CLI, HTTP, and descriptor versions must agree exactly.
Known-tested versions are advisory. Missing metadata and untested builds produce
compatibility warnings. Invalid HTTP responses remain protocol failures.

For descriptor fields, see [`preprocessing/engine.py`](../preprocessing/engine.py).
For version classification, see
[`preprocessing/engine_version.py`](../preprocessing/engine_version.py).

Issue a fresh `engineInstanceId` for every server boot. Use a canonical absolute
extraction directory. Set `contextVerified` only after verifying effective
instructions and plugins; Python environment isolation cannot change an
already-running server's global configuration.

For a controlled dedicated engine, adapt
[the extraction configuration](../deployment/examples/extraction-engine.json)
to your provider. Preserve the extraction agent and its deny-all, one-step
policy. Readiness checks configuration and model capabilities. It does not
establish credential validity or successful inference.

Allow time for engine preparation and settlement when choosing a PDF work
window. A short window can refuse model work before inference begins, even if
inference would be fast. An expired local deadline does not establish provider
cancellation.

## Standalone supervision

For installation outside the container, create private configuration and
descriptor parent directories, then run the owner under your supervisor:

```sh
uv run preprocess-engine-owner \
	--state-dir /private/engine-owner \
	--projection /srv/preprocessing/.preprocessing/engine-lease.json \
	--descriptor /private/engine.json \
	--opencode-executable /opt/opencode-2.0.19/bin/opencode \
	--engine-config /private/extraction-engine.json \
	--credential-env DEV_OPENCODE_PROVIDER_TOKEN
```

After the descriptor is published, start `preprocess serve` with the same data
root and descriptor. Adapt the [service](../deployment/examples/preprocessing.service)
and [owner](../deployment/examples/preprocessing-engine-owner.service) systemd
examples to your installation paths, credentials, and storage.

Preserve both owner and service state across restarts. Recovery receipts have
a separate serialized-evidence limit that includes JSON metadata. Raising the
service's `responseBytes` does not raise that limit. Oversized evidence can keep
cleanup and admission blocked. Use the owner's `--evidence-bytes` option to
change it; the container supervisor does not forward that option.

## Managed model selection

`--model-policy PATH` enables backend-authorized selection for new submissions.
`--model-declarations PATH` identifies the trusted private engine configuration.
Both files must be root-owned regular files, at most 64 KiB, under root-owned
directories without group or other write permission. Symlinks are rejected.
The runtime supplies `/run/runtime/preprocessing/model-policy.json` and its
root-owned private `engine.json` configuration. The declarations file contains
`providers.proxy.models`; it is separate from the connection descriptor.

Selection preferences never grant permission. Text-only declarations cannot win
image selection. Selection does not establish successful inference.

Use `--allow-model-fallback false` to restrict selection to the preferred model.
To specify fallback order, pass exact `proxy/<model-id>[#variant]` references as a
JSON array to `--fallback-models`.

Use `--model-policy-max-age-ms` to change the discovery freshness allowance.
To impose additional capacity floors, supply `--min-context-tokens` or
`--min-output-tokens`. The equivalent runtime settings are
`PREPROCESSING_MIN_CONTEXT_TOKENS` and `PREPROCESSING_MIN_OUTPUT_TOKENS`.

The `requestBytes` allowance adds conservative capacity headroom. It is not a
tokenizer measurement or a byte-to-token conversion. This policy permits default
selection using advisory bounds instead of requiring operator-provided token
budgets. Selection does not change the generation budget.
When the installed declaration supplies `limit.input`, the input bound must also
fit that budget, including any operator context floor.

The conservative bound can reject a candidate even when a smaller operation
would fit. Passing selection does not guarantee that every operation fits,
reserve usage, or bypass backend admission.
Actual request failures retain the selected model without hidden rotation.

A variant requires an explicit installed declaration and an engine catalog variant
with the same exact ID. The declaration's configured reasoning effort, not its
variant ID, must be allowed by the backend and match the engine catalog settings.
An explicit preferred variant disables implicit default alternatives. Any fallback
must then be separately listed in `--fallback-models`. A bare fallback reference
uses that model's default variant.
The processor never strips or copies a variant to another model.

Stale or unavailable discovery blocks new selection but does not revoke previously
accepted jobs. Fresh revocation or reduced limits can hold queued jobs. Running
operations retain their pinned model. Cleanup does not require model-policy
availability. Version contradictions still block normal operation cleanup
mutations and require [ownership reconciliation](api.md#reopen-blocked-engine-admission).

For selection rules and policy validation, see
[`preprocessing/model_policy.py`](../preprocessing/model_policy.py).
For readiness observations and job holds, see
[`preprocessing/service.py`](../preprocessing/service.py).
For command options, run `uv run preprocess serve --help` or see
[`preprocessing/cli.py`](../preprocessing/cli.py).

Without `--model-policy`, standalone selection uses the explicit profile.

## Test an installed package

Install the candidate wheel and `pytest` and `pytest-asyncio` in a separate Python
environment. Run the external protocol tests with the actual engine executable:

```sh
python scripts/test_installed_protocol.py \
	--python /path/to/installed-venv/bin/python \
	--opencode /path/to/actual/opencode
```

The provider fixture uses no paid inference or live account credentials.

## Ownership and recovery

A shared-engine owner must supervise the service, isolate the extraction agent,
exclude extraction from automatic turn recovery, and filter extraction sessions
from chat history. These are external responsibilities the service cannot prove.

The service publishes `.preprocessing/engine-lease.json` for its owner to watch.
Lease expiry does not prove writer death. Before recovery on a surviving engine,
stop or revoke the previous service generation's writer and record that fencing.
For replacement recovery, confirm old-engine death and extraction exclusion.
An absent session does not settle a potentially late request.

Capture evidence before deleting owned sessions. After publishing owner evidence,
[reconcile engine admission](api.md#reopen-blocked-engine-admission).

Local settlement and session-history deletion do not establish remote provider
cancellation, data deletion, refunds, or zero charges. Available usage is not a
complete provider billing ledger.

## Probe a configured engine

Use a dedicated data root whose ownership projection the engine owner watches.
The probe sends real provider requests and retains its evidence:

```sh
uv run python scripts/probe_engine.py \
	--engine-descriptor /private/engine.json --data-root /path/to/probe-data \
	--model deployment/vision --seconds 90 --cancel
```

Inspect the printed JSON and retained output for text, image, and cancellation
checks. This tests engine integration, not extraction quality on real documents.
