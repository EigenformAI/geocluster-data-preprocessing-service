# Integrate an OpenCode engine

The service requires an OpenCode `2.0.19`, `2.0.19-geocluster.1`, or
`2.0.19-geocluster.2` HTTP server and an explicitly selected
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
suffix when present. The HTTP version must match the descriptor exactly.
The isolated launcher accepts bare CLI output or an `opencode v` prefix and
records the same bare version. Other builds require an explicit update to the
supported versions in `preprocessing/engine_version.py`.

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
