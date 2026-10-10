# OpenCode engine reference

The service connects to the native OpenCode **2.0.19** HTTP API. Production
`process` and `serve` never install, launch, or restart OpenCode. Both require a
private descriptor and explicit model selection, including for native formats.
CSV, TSV, and workbook extraction check readiness without creating sessions or
sending prompts. PDF tables use independent image confirmation by default,
including tables extracted from native text. Ordinary page and figure OCR are
separate from table vision.

## Deployment modes

| Mode | Server owner |
| --- | --- |
| `runtime` | External runtime supervisor; the server may be shared with chat. |
| `standalone` | Dedicated supervisor running `preprocess-engine-owner`. |
| `development` | Isolated `preprocess dev` launcher. |

For a packaged dedicated engine, use the
[standalone container](../deployment/standalone/README.md).
For an isolated local engine, use the [development guide](development.md).
The Python wheel contains the service and engine owner, not the OpenCode binary
or container supervisor.

An unavailable engine blocks new jobs. Existing status, results, cancellation,
and publication repair remain available. Failed model work can leave a partial
PDF result if usable native evidence exists.

## Model completion windows

The PDF soft work window is `options.tableRecovery.workSeconds`. The coordinator
starts its monotonic deadline before snapshot acquisition and resume copies.
All PDF model work shares this window, including ordinary OCR and figure
interpretation. Table-specific call and reread caps are separate. See
[PDF table recovery](profiles.md#pdf-table-recovery) for policy resolution and budgets.

Before dispatch, the coordinator checks remaining soft and hard time against
the engine's preparation and finish allowances. With native adapter defaults,
dispatch requires more than 240.05 seconds of soft time and 281.05 seconds of hard
time. A shorter window cannot admit model work, even if inference would be fast.
These conservative allowances do not promise an inference duration. Their
calculation belongs to `OpenCodeServerEngine` in
[`preprocessing/engine.py`](../preprocessing/engine.py).

The coordinator caps each PDF operation's completion window by `modelSeconds`
and the remaining soft time. The window includes readiness and session
preparation. It does not change the persisted profile or appear in the OpenCode
prompt payload. Clients cannot override this local window in a submission.

In-flight preparation requests and cleanup can outlast the work window.
When the work-limited window expires and cleanup settles, the adapter reports
`WORK_BUDGET_EXHAUSTED`. Unfinished PDF units remain pending for a sealed partial
result when usable evidence exists. An operation that settles after the overall
PDF cutoff is not accepted as a completed extraction unit. Unsettled cleanup
reports `PROVIDER_OUTCOME_UNKNOWN` and prevents sealing. A local deadline and
session cleanup do not establish remote provider cancellation or zero charges.

## Connection descriptor

The CLI reads `--engine-descriptor PATH`, falling back to
`PREPROCESSING_ENGINE_DESCRIPTOR`. The JSON file is limited to 4,194,304 bytes.
It contains server credentials and must remain private. It is separate from the
credential-free extraction profile.

This example shows the shape; the owner must supply real identity and recovery
evidence:

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

| Field | Contract |
| --- | --- |
| `serverUrl` | Absolute origin without credentials, query, fragment, or non-root path. HTTP is allowed only on loopback; remote connections require HTTPS. |
| `username` | Nonempty Basic-auth username; defaults to `opencode`. |
| `password` | Nonempty server password. |
| `version` | Exactly `2.0.19`; readiness also checks `/api/info`. |
| `engineInstanceId` | Owner-issued identity, fresh for every server boot. |
| `mode` | `runtime`, `standalone`, or `development`. |
| `owner` | Nonempty server-owner identity. |
| `directory` | Canonical absolute extraction directory. |
| `contextVerified` | Must be true; the owner verifies effective instructions and plugins. |
| `recovery.managed` | Whether the engine manages automatic recovery. |
| `recovery.extractionExcluded` | Required boolean; must be true when recovery is managed. |
| `recovery.fencedGenerations` | Service generations whose writers the owner has stopped or revoked. |
| `recovery.deadEngineInstances` | Old engine instances the owner has confirmed dead. |
| `recovery.cleanupReceipts` | Full-binding evidence and settlement receipts for operations already deleted; defaults to an empty list. |

The CLI rereads the atomically published descriptor before readiness, dispatch,
and recovery. Active operations retain their connection snapshot. Missing or
invalid descriptor updates block admission.

## Extraction configuration

[The example configuration](../deployment/examples/extraction-engine.json)
declares `deployment/vision` with a placeholder provider URL and
`{env:DEV_OPENCODE_PROVIDER_TOKEN}` credentials. Replace the endpoint, model ID,
capabilities, and context/output limits with your provider's values.

Readiness checks the `preprocessing` primary agent, its one-step policy, exact
system prompt, deny-all permissions, and selected models. The example configuration
contains the required prompt, which is checked against `SYSTEM` in
[`preprocessing/engine.py`](../preprocessing/engine.py).

Models must accept text input and produce text output; `vision` also requires
image input. Readiness does not send a provider request, so it does not verify
credential validity or inference success.

Standalone and development configurations permit only the extraction agent,
explicit providers and models, empty MCP and skill configuration, disabled
snapshots, warming, compaction, and updates, and the built-in plugin-removal
directives. Credentials use explicitly named environment variables forwarded
with repeated `--credential-env` arguments. These launchers do not import a
personal auth store or load `.env`. Their HOME, XDG roots, and workspace are
isolated; project discovery and model fetching are disabled.

The owner additionally loads the dedicated `preprocessing-managed-usage` plugin
bundled in the Python package. Caller-supplied plugin paths remain rejected.
Missing bundled hook files prevent launch. The hook preserves the configured
provider URL, selected model, and named credential.

## Managed model attribution and denial

For managed deployment, the configured provider endpoint is the project's
backend model proxy or its machine relay. Account and project authority come
from the proxy purpose credential. The processor does not grant model allowance
or compute supplier prices.

Every native HTTP model attempt carries a fresh UUID in
`x-geocluster-request-id`. `x-geocluster-work-id` is the job ID from the owner's
structured session metadata. `x-geocluster-conversation-id` is that extraction
operation's native root session ID, not a browser chat ID. The agent and kind
headers are both `preprocessing`. Attribution never reads prompt text or uses a
process-wide current-job environment variable.

HTTP 402, 403, and 409 stop automatic model retries. Known managed denial codes
also stop when a response uses another status. HTTP 503 with a known denial code
or `error.retryable:false` stops; ordinary supplier 503 and capacity 429 retain
at most three retries. Authentication failure does not change credentials,
models, or funding. A safe `[geocluster:<code>]` message marker preserves denial
identity when OpenCode serializes away other provider error fields.

The service retains the denial code in job status and reports `retryable:false`,
`retry:"none"`, a status, and a recovery action. Once a job receives a managed
denial, later model operations for that job do not dispatch. PDF work retains
completed products and marks remaining work pending, with a sealed partial
result when usable evidence exists and local cleanup settles. Restored access
does not replay the accepted job. A deliberate new submission or supported
explicit partial-result resume has a new job ID.

These hooks do not qualify image exposure. The managed backend currently admits
qualified text and function requests only; image extraction requires separately
qualified image bounds and backend admission support. An image-capable catalog
entry is not spend authorization. Local fake-provider image tests establish
transport behavior, not managed image eligibility or supplier cancellation.

## Standalone supervision

Pre-create private descriptor and configuration parent directories. Run the
owner under a supervisor with the pinned absolute executable path:

```sh
uv run preprocess-engine-owner \
	--state-dir /private/engine-owner \
	--projection /srv/preprocessing/.preprocessing/engine-lease.json \
	--descriptor /private/engine.json \
	--opencode-executable /opt/opencode-2.0.19/bin/opencode \
	--engine-config /private/extraction-engine.json \
	--credential-env DEV_OPENCODE_PROVIDER_TOKEN
```

After the owner publishes the descriptor, start the service under its supervisor:

```sh
uv run preprocess serve \
	--data-root /srv/preprocessing --workspace /srv/workspace \
	--engine-descriptor /private/engine.json --model deployment/vision
```

The owner watches the service's durable ownership projection, fences previous
writers, checks prior engine death, and captures evidence before deleting owned
sessions. Preserve both owner and service state across restarts.

`--evidence-bytes` defaults to 262,144 bytes for serialized evidence in each
cleanup receipt. The cap includes JSON metadata, not just model text. Raising
the service's `responseBytes` does not raise this cap. Oversized evidence can prevent cleanup
and keep admission blocked. The container supervisor does not forward this
option; a custom owner invocation is required to change it.

The systemd examples for the [service](../deployment/examples/preprocessing.service)
and [engine owner](../deployment/examples/preprocessing-engine-owner.service)
need installation paths, provider configuration, credentials, and storage
adapted to the host. They are not installed automatically.

## Shared-engine ownership and recovery

An external shared-engine owner must provide the descriptor, supervise the
service, isolate the extraction agent, exclude extraction from automatic turn
recovery, and filter extraction sessions from chat history. These responsibilities
are outside the service. Python environment isolation cannot change an
already-running server's global instructions or plugins.

Managed shared-engine deployment also requires the owner to load the bundled
job-aware usage hook or provide equivalent attribution and denial handling.
Only standalone and development `PrivateServer` launchers add the bundle
automatically.

The service publishes `.preprocessing/engine-lease.json`. Its lease renews every
10 seconds and expires after 30 seconds by default. Expiry alone does not prove
writer death. Recovery on a surviving engine requires owner-confirmed fencing;
replacement recovery requires confirmed old-engine death and extraction exclusion.
An absent session alone does not settle a possibly late request.

Unknown outcomes retain operation bindings and block conflicting admission.
Owner cleanup receipts identify the operation, job, generation, engine instance,
session, input, and request hash. Recovery records captured text, usage, and
settlement without replaying prompts. Use
[`POST /preprocessing/engine/reconcile`](api.md#job-state-and-recovery) after
the owner supplies this evidence.

Cancellation touches only owned extraction sessions. Local settlement and
history deletion do not establish provider cancellation, data deletion, refunds,
or zero charges. Available usage is not a complete provider billing ledger.
