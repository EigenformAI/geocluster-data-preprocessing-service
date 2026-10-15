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
The isolated launcher accepts bare CLI output or an `opencode v` prefix and
records the observed HTTP version, otherwise the CLI version, otherwise `null`.
Known-tested versions are advisory. Missing metadata and untested builds produce
compatibility warnings. A failed CLI version probe is reaped before supervised
server startup. Invalid HTTP responses remain protocol failures.

This example shows the owner-supplied connection and recovery evidence:

```json
{
	"serverUrl": "http://127.0.0.1:4096",
	"username": "opencode",
	"password": "OWNER_SUPPLIED_SECRET",
	"version": "2.0.19",
	"engineInstanceId": "OWNER_SUPPLIED_BOOT_ID",
	"mode": "runtime",
	"owner": "project-runtime",
	"directory": "/workspace/preprocessing-extraction",
	"contextVerified": true,
	"recovery": {
		"managed": false,
		"extractionExcluded": true,
		"fencedGenerations": [],
		"deadEngineInstances": [],
		"cleanupReceipts": []
	}
}
```

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

Selection preferences never grant permission. A candidate requires fresh backend
acceptance, qualified image support, trusted declarations, and one enabled exact
engine entry. Text-only declarations are valid but cannot win image selection.
Ranking uses the preferred reference, the configured fallback order, then exact
model IDs in sorted order. Selection does not send a model prompt.

These options apply to `process`, `serve`, development commands, and local `resume`:

| Option | Default and meaning |
| --- | --- |
| `--allow-model-fallback true\|false` | `true`; `false` restricts selection to the preferred model. |
| `--fallback-models JSON` | `[]`; ordered exact `proxy/<model-id>[#variant]` references, deduplicated in order. |
| `--model-policy-max-age-ms INT` | `120000`; positive safe-integer milliseconds. |
| `--min-context-tokens INT` | Optional additional admission input floor; omitted means no operator floor. |
| `--min-output-tokens INT` | Optional additional output-capacity floor; omitted means no operator floor. |

Explicit floors must be positive integers at most `2147483647`. Omitting them
allows candidate-specific bounds from qualified evidence and the extraction
profile. The equivalent runtime settings are `PREPROCESSING_MIN_CONTEXT_TOKENS`
and `PREPROCESSING_MIN_OUTPUT_TOKENS`; the processor receives a CLI flag only
when the runtime operator supplies a floor.

For each new candidate, selection applies these checks:

```text
derivedInputBound = qualified image_input.tokens_per_image * profile.limits.modelImages
                    + profile.limits.requestBytes
minContextTokens = max(derivedInputBound, optional operator input floor)
minOutputTokens = installed engine declaration limit.output
declared context >= minContextTokens + minOutputTokens
backend max_input_tokens >= minContextTokens
backend max_output_tokens >= minOutputTokens
minOutputTokens >= optional operator output floor
```

The default extraction profile explicitly bounds `modelImages` to `4` and
`requestBytes` to `65536`. Both values participate in admission. The request-byte
allowance adds conservative capacity headroom. It is not a tokenizer measurement
or a byte-to-token conversion. This policy intentionally permits default selection
using advisory bounds instead of requiring operator-provided token budgets.
The extractor still does not specify an output-token request. Selection uses the
installed output declaration without inventing or changing a generation budget.
Backend `default_output_tokens` must be valid and within `max_output_tokens`;
it can differ from the installed declaration's output value.

For example, a qualified image cost of `131072`, four images, and `65536` request
bytes produce an input bound of `589824`. A declared output of `32000` needs total
context headroom of `621824`. Declared context of `1050000`, backend maximum input
of `922000`, and backend maximum output of at least `32000` satisfy these checks.

Missing or invalid request bounds, declarations, or backend capability and limit
evidence make the candidate ineligible. Image checks also require PNG support and
usable image-count, byte, and pixel limits. The conservative bound can reject a
candidate even when a smaller operation would fit. Passing selection does not
guarantee that every operation fits, reserve usage, or bypass backend admission.
Actual request failures retain the selected model without hidden rotation.

A variant requires both backend reasoning-effort evidence and an engine variant
with the same exact ID. An explicit preferred variant disables implicit default
alternatives. Any fallback must then be separately listed in `--fallback-models`.
The processor never strips or copies a variant to another model.

Discovery uses schema version `1`: `managed_access_enabled`, `allowed_model_ids`,
per-model `model_limits`, per-model `model_capabilities`, and `as_of`.
Limits contain `default_output_tokens`, `max_output_tokens`, and `max_input_tokens`.
Capabilities contain `input`, `output`, `allowed_reasoning_efforts`, and nullable
`image_input`. Qualified image evidence contains `mime_types`, `max_images`,
`max_bytes`, `max_pixels`, `tokens_per_image`, and `detail` (`low`, `high`, or `original`).
The root projection preserves the successful `fetched_at_monotonic_ms` and
`boot_id` from `/proc/sys/kernel/random/boot_id`. Reads and failed upstream
refreshes do not reset age. `as_of` is diagnostic evidence, not the age clock.

Same-key replay returns the stored job before policy or engine checks. New jobs
persist the selected profile, its recomputed digest, and `modelSelection` before
work starts. `profile.modelRequirements` preserves `derivedInputBound`,
`minContextTokens`, `minOutputTokens`, `operatorMinContextTokens`, and
`operatorMinOutputTokens`. Omitted operator floors are stored as `null`.
Replacement configuration and later image-cost observations do not enlarge a
pinned job's requirements. Fresh policy checks the stored requirements against
current acceptance and limits. Reduced limits can hold the job; a changed installed
output default also holds it rather than changing its output requirement.
Backend per-request admission still applies if qualified image cost changes.
Fresh revocation holds queued jobs without changing their profiles.
The scheduler dispatches the oldest eligible queued job. Stale or unavailable
discovery blocks new selection but does not revoke previously accepted jobs.
Running operations retain their pinned model, including after request failures.
Cleanup does not require model-policy availability.

`GET /preprocessing/capabilities` awaits `Service.refresh_capabilities()`.
Concurrent readers share one engine observation, bounded to 10 seconds.
`dependency.observedAt` is the engine observation's UTC timestamp. Cached evidence
older than `observationMaxAgeMs` (5000 by default) reports both readiness values
as false without changing that timestamp. Missing or invalid observation timestamps
cannot establish readiness. Failed follow-up probes invalidate earlier success.
`dependency.ready` describes new selection; `dependency.executionReady` describes
global authenticated context and ownership. Model-policy unavailability can make
the first false while the second stays true. Diagnostics expose `preferredModel`,
`selectedModel`, `eligibleCandidateCount`, `selectionReason`, `policyAgeMs`, and
`jobHolds`. Queued job `hold` includes a reason and its pinned `selectedModel`.
`dependency.compatibility` contains `classification`, nullable `cliVersion`,
`httpVersion`, and `descriptorVersion`, and bounded warning identifiers.
Version contradictions also block normal operation cleanup mutations, retaining
unsettled ownership for reconciliation. Hold-persistence failures block dispatch
and new admission until a bounded scheduler retry succeeds.

Without `--model-policy`, standalone selection uses the explicit profile as before.

## Test an installed package

Install the candidate wheel and `pytest` and `pytest-asyncio` in a separate Python
environment. Run the external protocol tests with the actual engine executable:

```sh
python scripts/test_installed_protocol.py \
	--python /path/to/installed-venv/bin/python \
	--opencode /path/to/actual/opencode
```

The runner uses Python isolated mode and rejects source-masked installations.
The tests assert that processor modules load from the installation environment.
Two eligible local fixture models exercise selection, text and image requests,
durable transcript evidence before deletion, and scoped settlement. The provider
fixture uses no paid inference or live account credentials.

## Ownership and recovery

A shared-engine owner must supervise the service, isolate the extraction agent,
exclude extraction from automatic turn recovery, and filter extraction sessions
from chat history. These are external responsibilities the service cannot prove.

The service publishes `.preprocessing/engine-lease.json` for its owner to watch.
Lease expiry does not prove writer death. Before recovery on a surviving engine,
stop or revoke the previous service generation's writer and record that fencing.
For replacement recovery, confirm old-engine death and extraction exclusion.
An absent session does not settle a potentially late request.

Capture evidence before deleting owned sessions. Cleanup receipts bind the
operation, job, service generation, engine instance, session, input, and request
hash. After publishing owner evidence, [reconcile engine admission](api.md#reopen-blocked-engine-admission).

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
