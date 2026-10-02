# Preprocessing through the OpenCode 2.0 HTTP API

Status: Approach A service-side implementation completed on 2026-10-01. Runtime-owned standalone HTTP was selected for chat and extraction. External runtime integration and live OCR operating-envelope validation remain pending. Source snapshot: 2026-09-30.

## Problem Statement

The preprocessing service needs one model execution path: OpenCode 2.0 through its native HTTP API. Runtime deployment must share the runtime-owned engine rather than launch another server or call Mistral or Anthropic directly. The service must also support standalone production deployment outside the runtime, connecting to a dedicated, externally supervised OpenCode server.

Local development needs an isolated OpenCode server without inheriting the developer's global configuration, instructions, credentials, or sessions. That server is a development dependency, not a production fallback.

The runtime memory target is less than **500,000,000 bytes of incremental peak RAM**. This includes preprocessing processes, added runtime supervision and cleanup memory, and the extra memory preprocessing adds to the shared OpenCode engine. The engine's pre-existing runtime footprint is reported separately. Standalone production and local development report their full deployment footprint, including the dedicated engine. Low idle memory matters more than shaving a small amount from cold startup. Disk usage needs to remain reasonable, but disk minimization is not the primary objective.

## Context

### Current service

The inspected service commit is `75b736d852b8840baba9c4aa965925eed801dcd4`.

- `pyproject.toml:18–33` packages only `preprocessing/` and registers `preprocess`.
- Historical `pipeline/` still contains direct Mistral OCR and figure-description calls, and Anthropic table-reading calls. Historical `analysis/` contains an older OpenCode HTTP client and additional report-analysis tooling. Neither tree ships in the current wheel.
- `preprocessing/service.py` owns the queue, SQLite records, source snapshots, subprocess parsing, immutable results, publication, and cancellation admission.
- `preprocessing/engine.py` currently provides an owned ACP engine and a thin borrowed-engine interface. The selected direct HTTP integration is not implemented.
- The owned engine starts `opencode acp`, which starts a private HTTP server. It therefore adds both an ACP process and a server, plus its guardian.
- The CLI currently permits no-engine processing. Missing OCR becomes a partial result where native evidence is usable. That configuration behavior changes: an OpenCode connection becomes a deployment prerequisite.
- CSV, TSV, XLSX, XLSM, and native PDF processing remain deterministic format operations. Requiring an engine connection does not mean sending those operations to a model.

The runtime keeps one service owner and one parser at a time. It already separates authoritative sealed results from their shared `documents/` publication. These boundaries remain useful.

### Runtime integration target

The inspected integration branch is `EigenformAI/geocluster-ai-ide-BE:feat/cline-cli-migration` at `8585645b1d48a7a91901221ac5c879e1dba09cf3`.

Executable code still uses Cline/API 2. The approved OpenCode plan pins 2.0.19 and proposes one shared ACP connection, with its private OpenCode server serving every conversation. This is a planned dependency, not evidence that the server is currently deployed. [R1, R2]

This design selects direct HTTP rather than borrowing that ACP connection through an extraction broker. The runtime must deliberately expose a supported HTTP connection to its single engine. The required endpoint handoff or server-ownership change is integration work, not a capability already supplied by the approved ACP plan.

The deployment has one Fly Machine and persistent workspace per project. Runtime configuration normally selects `/workspace/<PROJECT_ID>`. An ordinary Python service can run beside the gateway in that Machine.

**Packaging processes into the runtime image does not require Docker-in-Docker.** Fly uses a Docker image to build the Machine filesystem. The existing entrypoint or gateway can supervise an additional process without a Docker daemon, socket, privileged nested container, or second VM. Fly's `[processes]` groups are different: each group runs in separate Machines. [R3, R4]

### OpenCode version matters

This design initially pins **2.0.19**, not any executable named `opencode`. The ambient executable on the inspected development machine reports 1.18.32.

The 2.0.19 standalone server exposes `/api/session`, asynchronous prompt admission, `{data: ...}` response envelopes, and `/openapi.json`. Older examples using `/session/:id/message`, `parts`, or `modelID` do not specify this version's HTTP contract. [R5, R6]

`opencode acp` starts its own private server. Running another `acp` is not a way to attach to the existing runtime server. The private URL and password are not published by the ACP protocol as a supported third-party discovery mechanism. [R7]

## Goals

1. Retire the legacy execution paths and direct provider integrations. All model-assisted extraction uses OpenCode.
2. Share the runtime-owned engine without adding a second OpenCode server in runtime deployment.
3. Use one HTTP extraction adapter for runtime deployment, standalone production, and isolated local development, with explicit configuration and server ownership in each environment.
4. Preserve exact source snapshots, typed artifacts, provenance, partial coverage, and publication repair.
5. Preserve job-scoped cancellation and prevent preprocessing from interrupting unrelated conversations.
6. Measure incremental runtime peak memory, service idle memory, full standalone production footprint, and full development footprint separately.
7. Prefer one parser, bounded payloads, short extraction sessions, and reusable connections over additional resident infrastructure.
8. Make performance budgets and configurable limits explicit. Administrators can increase workload limits and accept a larger memory footprint.

## Non-Goals

- A second OpenCode server alongside the runtime-owned engine, direct Mistral or Anthropic SDK clients, or a no-engine production fallback.
- A production preprocessing-owned server launcher or an extraction broker over ACP. Standalone production uses an externally supervised dedicated HTTP server.
- Docker-in-Docker or running a Docker daemon inside the project Machine.
- A distributed extraction queue or a dedicated worker fleet.
- Retaining historical report-analysis commands, PEACE bootstrap tooling, or their output compatibility.
- Running inference locally on a GPU. OpenCode continues to use deployment-configured remote models.
- Proving the entire IDE, all conversations, and the existing engine fit in 500 MB.
- Guaranteeing provider-side cancellation, exactly-once charges, or deletion of remote model data.

## Approaches Considered

### APPROACH A: Direct client of an explicitly exposed server (Selected; Minimal Viable)

Summary: a Python `OpenCodeServerEngine` implements the engine interface through the pinned HTTP API. The server owner deliberately supplies the private URL, authentication, and engine identity. The same adapter connects to the shared runtime engine, an externally supervised standalone production server, or the isolated development server.

Complexity: M. Risk: Medium.

Pros:

- One adapter for runtime deployment, standalone production, and development, without a broker process or a second operation store.
- No ACP process for the development server.
- The service's existing SQLite ledger can own operation-to-session bindings. No second job queue is needed.

Cons:

- The runtime's proposed ACP architecture does not currently expose a supported HTTP connection descriptor. That must become an explicit runtime interface.
- OpenCode HTTP authentication is server-wide. The trusted service receives broader engine authority than extraction alone needs.
- The service must implement asynchronous prompt completion, scoped cancellation, ownership recovery, and version-specific response handling.

Reuses: current job ledger, extraction validation, input limits, and OpenCode's native HTTP API.

Decision: selected to simplify all three deployment modes and keep operation ownership in the existing service ledger. Runtime integration must provide an explicitly exposed server or an equivalent supported private HTTP connection contract. Never discover an ACP-private password through process inspection.

### APPROACH B: Borrow through the runtime's existing engine owner (Not selected)

Summary: an extraction adapter inside the existing runtime gateway would borrow its ACP connection. Python would connect to a narrow internal broker API. Development and standalone production would use a separate HTTP adapter with the same extraction behavior.

Complexity: M. Risk: Medium.

Pros:

- Fits the approved ACP migration without changing how the runtime owns OpenCode.
- Adds no engine and no broker daemon. The broker is a module in the already-running gateway.
- Centralizes extraction-session ownership, callback routing, model selection, and cancellation alongside interactive-session handling.
- The gateway can account for extraction capacity and keep the Machine active independently of the browser.

Cons:

- Requires a small durable operation registry and an explicit internal protocol.
- Two transports need a shared behavioral conformance suite: runtime ACP and standalone/development HTTP.
- Production integration depends on the runtime exposing this capability. The current borrowed-engine placeholder is not sufficient.

Reuses: `EnginePort`, the gateway's single engine reader, session broker, process supervisor, build identity, and activity reporting.

Decision: not selected. Existing ACP routing and narrower service credentials are useful, but a broker, an additional binding registry, and separate extraction transports add complexity across deployment modes. The runtime HTTP exposure work is accepted as part of A; Phase 0 verifies that contract rather than reopening the transport choice.

### APPROACH C: A preprocessing-owned server in the same Machine

Summary: have preprocessing launch and own an isolated OpenCode server beside Python. This is useful for development, but duplicates the runtime engine and adds process ownership to the production service. A dedicated server supplied by the standalone deployment's external supervisor is Approach A, not this owned-launcher alternative.

Complexity: S initially, M with lifecycle handling. Risk: Medium.

Pros:

- Stronger control over configuration and straightforward process cleanup.
- Independent upgrades and model-session state.
- Ordinary subprocess supervision; no nested Docker is necessary.

Cons:

- Duplicates engine baseline memory when deployed alongside the runtime engine. Prior owned-ACP PDF observations exceeded 500 MB sampled process-tree PSS.
- Continues the unwanted production dependency and process ownership.
- A second engine can retain memory after its last extraction unless deliberately stopped.

Reuses: existing process guardian and private HOME/XDG setup.

Decision: preprocessing-owned launch is development only, using standalone HTTP rather than the owned ACP/server pair. Standalone production uses A with an externally supervised dedicated server.

### APPROACH D: Separate container or separate Fly Machine

Summary: isolate preprocessing into a sibling container or another Machine, communicating over private HTTP.

Complexity: L. Risk: Medium to High.

Pros:

- Independent resource controls and dependency packaging.
- A separate Machine can process work independently of IDE shutdown.

Cons:

- A separate Machine cannot share the project volume as network storage. Inputs and results need an explicit transfer contract. [R8]
- A sibling container still shares the Machine's memory budget and complicates networking, paths, and provisioning. Its isolation does not make model payloads smaller.
- Docker-in-Docker would add an unnecessary daemon, image management, and nested lifecycle. It solves no requirement here.

Decision: ordinary processes in the existing image for the first integration. Reconsider separate compute only after workload evidence shows co-location is unsuitable. Sibling containers and Fly process groups are not synonyms for that arrangement.

## Decision

**Select Approach A: direct native HTTP access.** `OpenCodeServerEngine` is the single extraction adapter for runtime deployment, standalone production, and local development. The service's existing SQLite ledger owns operation/session bindings, dispatch, and terminal evidence. The server owner supplies connection identity and supervision; runtime integration adds projection-based cleanup and history filtering.

This choice accepts deliberate runtime HTTP exposure and broad server credentials in exchange for one transport, caller-chosen durable IDs, and no extraction broker or additional operation registry. Approach B remains an evaluated alternative, not an implementation path or automatic fallback. This decision supersedes earlier broker-oriented runtime integration work lists; refresh those implementation references when replacing the current ACP code.

### 1. One logical engine dependency, with ownership outside production preprocessing

Production `serve` and `process` require a configured engine adapter. Missing connection configuration is a startup error. A configured but unavailable engine makes preprocessing not ready and new admission return an actionable unavailable error. Status, existing results, cancellation, and publication repair remain reachable where the service can safely open its state.

The service never launches OpenCode, restarts the shared server, installs a CLI, reads developer credentials, or calls a provider API in production. It does not silently switch models, transports, or execution ownership.

The server owner controls provider configuration, credentials and refresh, the model catalog, server version, and engine lifetime. In runtime deployment this is the runtime owner; in standalone production it is the deployment operator and supervisor. The service supplies a credential-free profile selecting the desired model role. Model IDs are OpenCode provider/model references, not hard-coded Claude or Mistral choices.

Native extraction checks the engine dependency but does not create an extraction session or prompt. This deliberately blocks new CSV/workbook jobs during engine outages too: the deployment has one required dependency contract. Status and already-published results remain available. Genuine model failures during an admitted PDF job can still produce partial evidence. A missing deployment dependency is not an intentional native-only mode.

### 2. Keep the extraction interface; make preparation and recovery explicit

Keep `start`, `cancel`, and `close` as the parser-facing abstraction. Add explicit dependency preparation and recovery at the service boundary. Do not use the current `owner_fd` attribute as a test for whether persistence callbacks are required.

Proposed shape, not implemented code:

```python
class ExtractionEngine:
	async def ready(self) -> EngineCapabilities: ...
	async def recover(self, persisted_operations) -> RecoveryReport: ...
	async def start(self, operation_id, job_id, generation,
	                profile, input_refs, operation) -> ModelResult: ...
	async def cancel(self, job_id) -> Settlement: ...
	async def close(self) -> None: ...
```

`close()` settles owned requests and releases the connection. Delete owned sessions only after evidence capture and confirmed settlement; retain unsettled bindings in the ledger/projection for the server owner's cleanup hook. It never stops an externally owned server, whether shared by the runtime or dedicated to standalone production. Only the development launcher owns and stops its server process.

`ready()` verifies the pinned engine version, exact extraction agent, configured model and image capability, server-owner identity, and actual extraction readiness. Managed recovery without verified extraction-session exclusion is not ready for extraction. A successful server health check or `engineAvailable: true` alone is insufficient. The server's catalog can initialize after its HTTP listener becomes ready. [R9]

Use one canonical extraction working directory per project. Do not create an OpenCode location per PDF, page, or spelling of the same directory. This avoids repeated configuration runtimes and catalog initialization.

### 3. One HTTP adapter across deployment modes

| Deployment | Server owner | HTTP connection and configuration |
| --- | --- | --- |
| Runtime production | Runtime supervisor; one engine shared with interactive chat. | Runtime supplies the supported private connection descriptor, extraction context, cleanup hook, and history filter. |
| Standalone production | External deployment supervisor; one dedicated OpenCode server. | Operator supplies the same connection contract, controlled extraction configuration, and persistent service/engine state. No product gateway or browser is required. |
| Local development | Explicit development launcher; one isolated server. | Launcher supplies the same connection contract and controlled HOME/XDG roots, credentials, and lifetime. |

Standalone production runs ordinary `opencode serve` under a supervisor such as systemd or container process management, with the pinned version and explicit provider/model configuration. Use private configuration and data roots rather than a developer's personal OpenCode state. The operator provides a private endpoint and authentication; same-host deployments can bind to loopback. Remote engine connections require authenticated, protected transport. Provider credentials remain server-side. [R5, R10]

Standalone supervision includes a small `StandaloneEngineOwner` wrapper around the dedicated server, run under the chosen process supervisor. This proposed component supplies a fresh `engineInstanceId` for each server boot, confirms old-process death on replacement, watches the service-generation lease, and performs projection-based fencing and cleanup. Plain systemd or container restart policies do not implement these OpenCode-specific duties. The wrapper has no extraction queue or operation-binding registry; it consumes the service's durable ownership projection and gates replacement service admission on reconciliation. Restarting Python alone does not settle surviving engine work. Owner generation/cleanup state and the service ledger survive restarts. A dedicated engine does not need product chat-history filtering, but still needs owned-session cleanup and no automatic replay.

#### What extraction actually needs from history

One model attempt uses one fresh session and one bounded prompt. An attempt needs enough retained state to distinguish admission, execution, terminal output, and uncertain settlement. It does not need to appear as a conversation in the product.

| Feature | Needed? | Minimal implementation |
| --- | --- | --- |
| Operation/session ownership | Yes | Durable IDs and scope before prompting; cancel/delete only owned sessions. |
| Temporary engine transcript | Yes | Keep through terminal output and usage capture; then delete. |
| Lost-response reconciliation | Yes | Inspect known session/input IDs; never resend a prompt because its acknowledgement was lost. |
| Abandoned-session cleanup | Yes | Server owner fences the lost service generation and settles only its owned work. |
| Hide extraction from chat history | Runtime deployment only | Runtime classifies IDs from the service ownership projection; metadata is supplementary. |
| Browser replay and specialist cards | No | Extraction uses job polling and artifacts, not conversation replay. |
| Growing conversation history | No | A new page never loads an earlier page's session. |
| Automatic turn continuation or compaction | No | Interrupted jobs require explicit reruns; one-step extraction does not need compaction. |
| Usage/billing evidence | Available evidence only | Save returned usage without treating it as a complete provider billing ledger. |

#### Direct HTTP with one authoritative service ledger

`OpenCodeServerEngine` uses the same pinned HTTP transport in every deployment mode. The service's existing SQLite store owns the entire operation lifecycle. The server owner supplies a connection descriptor and an abandonment hook; the runtime also supplies history filtering. Neither acquires another job state machine.

**Connection descriptor.** The server owner deliberately supplies private `serverUrl`, authentication, pinned version, a fresh `engineInstanceId` on each server boot, and verified recovery behavior. Authentication is delivered privately, not in source profiles, browser responses, or general startup logs. The adapter is a trusted client of the engine: raw Basic auth grants server-wide authority. Constant scoped routes, known IDs, and the clean parser environment reduce accidental cross-chat operations; these controls are not a same-user OS sandbox.

**Support gap in the approved runtime.** Unmodified ACP internally chooses its child URL and a fresh password, overriding an inherited password. There is no supported `acp --server`, `--attach`, or password-export option at this pin. Direct access therefore requires one of these deliberate changes:

1. Add a supported owner-to-runtime connection-descriptor handoff for the existing ACP child, with upstream/fork maintenance and a regression probe; or
2. Have the runtime own a standalone HTTP server and use its documented HTTP gateway path for chat as well. This changes the approved ACP migration, but keeps one engine and removes ACP process overhead.

Neither option is a matter of setting `OPENCODE_SERVER_URL` on Python alone. Process/environment scraping and launching a second engine are not connection strategies. Phase 0 must establish one of these supported exposure mechanisms for the selected HTTP path. Failure to establish it blocks runtime integration rather than selecting B automatically. [R7, R12]

**Input and credential boundary.** Inputs are bounded references to service-staged files, not arbitrary URLs. The service opens approved regular files without symlink traversal and verifies size and hash before converting them to engine image blocks. Parsers receive no engine or provider credentials. Public APIs accept no raw server URL, engine session ID, global configuration, or caller-selected working directory. Server credentials and provider keys never enter artifact profiles.

**Allocate identity before the first mutation.** The v2 HTTP API accepts client-chosen session and message IDs. Allocate fresh cryptographically random `ses_...` and `msg_...` IDs and commit their operation association, expected agent/model/location, request hash, and prepared state to SQLite before sending create. Then durably publish the cleanup projection described below. Only after both commits succeed may session creation occur.

Core session creation returns an existing session on duplicate ID rather than overwriting it. Verify every returned binding before use; a colliding ID with different metadata or policy is an error. Lost create responses can be read back by known ID. Retrying a read is safe; no automatic prompt retry is permitted. Prompt IDs deduplicate durable input, but a duplicate prompt call can still wake execution, so that API behavior is not an exactly-once provider guarantee. [R12, R13]

**Dispatch and completion.** Read back agent/model selection, commit dispatch intent, send exactly one prompt, and record admission if acknowledged. Poll bounded session-specific messages or logs, correlate the known input ID with its terminal turn, validate the output, then commit output/usage evidence. A timeout or disconnect leaves an explicit unknown outcome. The HTTP client and any proxy have mutation retries disabled.

**Cleanup projection.** Maintain one bounded manifest under a server-owner-approved private path, such as `.preprocessing/engine-lease.json`. It contains service scope/generation, engine instance, heartbeat/expiry, canonical extraction location, candidate session/input IDs, and cleanup state. It contains no prompts, images, or credentials. It is an atomic, fsynced projection of SQLite for the deployment supervisor and, in runtime deployment, the history filter. It is not a second queue. Keep the active operation's prepared, active, or cleanup-pending entry and its owned attempts; historical terminal tombstones stay in SQLite.

Publishing before create closes the lost-create-ack discovery gap: even a session created after the service dies has a pre-recorded candidate ID. A supervisor startup rescan recovers outstanding entries. The history filter also reads the current projection at request time or follows atomic projection changes; a startup-only snapshot is insufficient. The runtime excludes those IDs from the interactive conversation list and does not load/resume them as chats. Treat an engine-side internal marker as a consistency check, not as independent authority.

**Service loss.** The deployment supervisor watches the known manifest. On owner death or expired generation it first fences that supervised service's ability to send more requests, then cancels pending input and interrupts its candidate sessions. A heartbeat expiry alone does not prove a paused writer has stopped: terminate or otherwise revoke only the preprocessing writer, never the shared engine. Use process identity rather than a reusable PID when termination is needed.

Already-sent requests can still be in flight. One 404, an interrupt acknowledgement, or an absent input is not sufficient to declare permanent settlement. Retain cleanup-pending IDs and retry owned inspection/cleanup until terminal evidence and writer fencing establish local settlement, or an engine replacement with confirmed old-process death settles the old execution. The API has no permanent session tombstone fencing late mutations. The server owner's cleanup hook therefore owns this uncertainty after service death; a new service generation does not automatically replay or discard it.

**History and memory.** After evidence capture and writer settlement, delete only the owned internal session, confirm cleanup before dispatching the next model attempt, and retain the service tombstone. HTTP delete interrupts/settles local work and removes some per-session transport state and durable history. It does not guarantee immediate RSS return or location-catalog eviction. Use one preprocessing-exclusive location to bound catalog growth. [R12, R14]

If location retention is material, the pin has a targeted debug-eviction endpoint, `DELETE /api/debug/location?location[directory]=...`. Consider it only at quiescence for an extraction-exclusive location, not per page or for a chat location. It does not provide permanent disposal or an exactly-once fence, and it does not clear an external ACP catalog cache. Benchmark before making debug eviction a maintained dependency. [R14]

**Resulting runtime work.** The selected path needs supported endpoint handoff, preprocessing supervision, candidate-session cleanup, and history filtering. The service owns session state and transport details. The runtime has no extraction broker or second operation-binding registry.

**Capacity and chat continuity.** Allow one extraction prompt in flight and enforce bounded HTTP payload and image limits. Separate HTTP connections remove shared ACP-frame contention for extraction, but inference and engine resources remain shared with chat. Report extraction activity to the runtime for capacity and Machine-lifetime accounting. Measure added interactive latency against paired controls; if degradation is unacceptable, first lower the extraction image payload/pixel envelope or concurrency.

### 4. Extraction sessions are short and separate from chat

Install a `preprocessing` primary agent in each deployment's engine configuration. It has the extraction system prompt, one-step intent, and deny-all permissions. Each attempt starts a fresh session with the selected model. No conversation history, specialist tree, skills, or per-job MCP servers are attached.

Deny tool execution before dispatch. Reject unexpected tool or child-session activity rather than approving it. Select and verify the extraction agent and model before prompting. Restrict runtime plugins that alter chat prompts or run folders to their intended agents; do not globally disable those plugins on the shared engine.

A clean working directory and deny-all rules do not remove a running server's global instructions or plugins. Production requires an owner-configured extraction context. Changing the Python client's HOME cannot isolate an already-running server. The effective extraction context must be inspected and exercised in runtime and standalone connection probes. [R10]

Use bounded operation status and owned-session reads instead of forwarding global engine events. Save the output and available usage evidence before deleting the completed internal session. Deletion applies only to proven extraction-owned sessions and their owned descendants. Internal session cleanup must not delete a user's conversation.

No session history is needed to extract the next page. Dispose of completed internal sessions after evidence capture, and bound any in-memory result cache in the service. This limits retained context, while acknowledging that engine allocators and location caches may retain memory.

### 5. Cancellation and restart behavior

For an externally owned engine, coordinator death no longer kills provider execution through a local guardian. The server owner must settle abandoned operations through the projection-based cleanup hook. Add a renewable service-generation lease with proposed defaults of a 10-second renewal interval and 30-second expiry. On expiry, fence new execution for that generation and cancel its owned sessions. These are configurable operational defaults, not a provider cancellation guarantee.

Cancel through the HTTP adapter's service-ledger bindings, never through all-session enumeration or a global engine interrupt. Stop dispatch, remove owned pending inputs where applicable, interrupt owned execution, and confirm local settlement. A returned interrupt acknowledgement is not by itself proof that pending work or provider charges have ended.

The supervisor admits one live service generation per ownership scope. Expiry is recorded durably and late renewals cannot revive dispatch. The supervised writer must be stopped/revoked before its projection is cleaned; heartbeat expiry is an observation, not a server-side permission fence.

Recovery must inspect both `admitted` operations and `outcome_unknown` operations whose settlement is false. The current service startup path checks only `admitted` records. Fix that integration gap before enabling the HTTP adapter in any deployment mode.

If settlement cannot be confirmed, retain a visible cancelling/unknown state and block conflicting admission. A successful later `recover()` can reopen admission. Do not claim settlement because a new adapter has no in-memory record of the old request.

`recover()` has these specific outcomes:

- Same surviving engine instance: inspect the known session/input and terminal evidence. If it completed, preserve that evidence and finish local cleanup. If it is active, interrupt only owned work. An absent or ambiguous record stays cleanup-pending until in-flight writer responsibility is resolved.
- Changed engine instance: require the server owner's confirmation that the old process is dead and that extraction cannot auto-resume. Settle the old local execution as abandoned, keep remote usage/outcome unknown, and remove any owned durable inputs/history before enabling new work.
- Managed recovery without extraction exclusion, unknown engine identity, or unfenced writer: remain not ready/blocked and expose the recovery reason. Do not redispatch or silently declare settlement.

Interrupted jobs are not silently converted into fresh successful jobs. Completed model evidence recovered after loss can be retained, but completing or rerunning the interrupted extraction is an explicit decision.

Previous unfinished extraction jobs remain interrupted and require explicit reruns. Reconciliation of sealed-result publication continues independently and does not rerun models.

**Engine-side recovery must also be defined.** OpenCode 2.0.19 managed service startup can resume orphaned turns with at-least-once semantics. Ordinary unregistered standalone servers do not run that sweep, and the target's ACP plan uses a private standalone server. Verify the actual launch mode. If the runtime uses managed recovery, it must exclude or settle extraction sessions before automatic continuation; the public session API has no verified per-session no-recovery switch. A client that avoids replay does not prevent engine-initiated continuation. [R11]

### 6. Isolated standalone server for local development

Add an explicit development command such as `preprocess dev serve` and `preprocess dev process`. These command names are proposed. Normal production commands never spawn a server as a fallback. The dev-only launcher refuses activation inside a Fly Machine.

Install the pinned CLI into a dedicated development prefix. Do not replace the globally installed executable. The launcher starts ordinary `opencode serve`, not `acp`, a TUI, or `serve --service`.

The development launcher supplies its server connection to the same `OpenCodeServerEngine` used in production. Its extraction semantics are:

- Authenticate to `/api/info` and verify version.
- Create `/api/session` with the exact agent, model, location, and permissions.
- Submit `/api/session/:id/prompt`; its response is admission, not output.
- Read owned messages or a session-specific durable log, correlate the admitted input with a terminal turn, and validate completion.
- Interrupt the owned session and cancel undelivered inbox work on cancellation.
- Capture bounded evidence and delete completed internal history.

HTTP interruption omits `resume=true`. Before recursive session deletion, verify that any children are also extraction-owned; an unexpected child causes a visible isolation/cleanup error. Pin these semantics in adapter conformance tests rather than relying on remembered defaults.

The development provider configuration declares the selected provider, its base URL, the exact model IDs/capabilities/context/output limits, and an explicit credential interpolation. An empty bundled catalog disables built-in model discovery; it does not replace these declared provider models. For example, a trusted dev-only configuration can reference `{env:DEV_OPENCODE_PROVIDER_TOKEN}` and the launcher can forward only that named value. A runtime-proxy JWT can be supplied through the same explicit mechanism. No key is copied from a personal auth store, and credentials never enter the artifact profile. The selected `models.vision` is explicit; do not inherit the local free-model default as a runtime choice.

The no-credential, empty-model configuration used by the resource probe is an idle-only experiment. It would not satisfy the product's model-ready dependency check.

Do not use the volatile global `/api/event` stream as the only completion record. Do not treat a generic idle state as a successful extraction. [R6]

Development launch isolation includes:

- A fresh or explicitly selected private HOME, XDG config/data/cache/state/runtime directories, temporary directory, and canonical working directory.
- Controlled PATH, locale, and TLS settings. No wholesale inheritance of `os.environ`.
- Project discovery disabled and an explicit configuration containing only the extraction agent and selected provider configuration.
- An empty bundled model catalog, model fetching disabled, warming disabled, automatic compaction disabled, and updates disabled.
- No ambient `.claude`, `.agents`, OpenCode auth store, shell credentials, or personal plugins. Explicit credentials are supplied separately from the persisted profile.
- Loopback binding, a random authentication secret, private startup logs, bounded readiness waits, and a lifetime tied to the development launcher.

There is no verified `OPENCODE_DISABLE_GLOBAL_CONFIG` flag. Isolate discovery roots instead. The OpenCode database lives beneath `XDG_DATA_HOME`; do not invent an `OPENCODE_DB` setting. [R10]

Start the development server lazily when a command needs to establish the engine dependency. Stop it after all owned work settles and a proposed 60-second idle timeout. A zero timeout can stop it immediately; an explicit keep-alive setting is available for repeated development work. Restarting after idle shutdown performs readiness again and never resumes an uncertain extraction automatically.

The launcher may reuse the Linux process guardian for parent-loss cleanup, but only in development. Its memory is counted in the full development measurement. Production has no preprocessing-owned engine guardian. The parser's existing owner fencing remains.

### 7. Legacy retirement and updated surfaces

The retirement scope is the historical runtime, not just its provider factories. Provider-independent historical analysis is also retired from this service. The current preprocessing API and artifact contract remain the maintained surface.

| Surface | Planned change |
| --- | --- |
| `pipeline/` | Retire the legacy pipeline, including `mistral_client.py`, provider OCR, figure descriptions, Anthropic hybrid-table calls, old OpenCode HTTP client, runner, and provider-specific caches. |
| `analysis/` | Retire legacy analysis execution, stdio MCP wrapper, heuristic/fake fallbacks, older HTTP model client, and PEACE map dispatch. |
| `.claude/skills/` | Retire `report-analysis`, `hie`, `dki`, and `peqa`, all tied to the removed runtime. These files are generic instructions in a Claude discovery location, not a Claude SDK dependency. |
| Legacy scripts and `tests/test_digital_pipeline.py` | Retire launch/probe/bootstrap scripts and tests that exercise the removed trees. Preserve unrelated scripts after checking references. |
| `HOW_TO_USE.md`, `PIPELINE.md`, `OUTPUT_STRUCTURE.md` | Retire or replace stale provider/analysis guides with current service instructions. Preserve historical provenance as history rather than an executable contract. |
| `docs/engine.md`, implementation handoff | Refresh the existing owned/borrowed-ACP implementation reference and broker-oriented handoff as part of the HTTP implementation. Distinguish current code from the selected direct HTTP target; replace broker integration work with descriptor delivery, service-side persistence, and projection-based cleanup/history filtering. |
| `preprocessing/engine.py` | Keep the logical extraction interface and validation; replace owned-ACP execution with `OpenCodeServerEngine`, used in all deployment modes. Keep process ownership in the dev launcher, separate from HTTP result/settlement handling. |
| `preprocessing/cli.py`, `.env.example` | Require explicit HTTP connection configuration for runtime and standalone production; add explicit dev launch configuration; remove production `--opencode-executable` ownership and direct provider-key examples. |
| `preprocessing/service.py`, `store.py` | Add engine readiness, pre-prompt binding, recovery of all unsettled operations, and a safe unblock path. Preserve queue, snapshots, validation, and publication. |
| `preprocessing/api.py` | Report dependency state and configured execution mode, while preserving HTTP/MCP job and artifact routes. Distinguish unavailable configuration from partial extraction coverage. |
| `preprocessing/engine_guardian.py`, engine probes/tests | Reuse process-lifetime handling only for the dev-owned server; replace owned-ACP assumptions with HTTP conformance, shared-engine, standalone-supervision, and isolated-dev tests. |
| `pyproject.toml`, packaging | Preserve the lean production wheel. Add only the small HTTP client dependency if required by `OpenCodeServerEngine`. Pin the external and development CLIs separately from the Python wheel. No Mistral or Anthropic SDK is currently declared here. |
| Runtime gateway and supervisor | Supply the supported HTTP descriptor, projection-based cleanup/history filtering, capacity/activity, extraction agent, and shutdown wiring, with no second engine or extraction broker. |
| Standalone deployment configuration | Document and package external supervision and the small `StandaloneEngineOwner` wrapper for a dedicated pinned HTTP server, controlled provider/agent configuration, persistent state, private connection delivery, generation identity, and projection-based cleanup. |

Retain `pdfplumber`, `pypdfium2`, Pillow, `defusedxml`, and the existing HTTP/MCP dependencies. The current parser does not require pandas, torch, cv2, Poppler, Mistral, or Anthropic SDKs. Do not remove a transitive dependency solely because a retired file also used it.

Changes described here are not implemented by this document. Existing untracked documentation and retained evaluation evidence are preserved.

### 8. Memory strategy and accounting

The primary target is sampled **incremental aggregate PSS below 500 MB decimal** under a declared default workload. Also report RSS and exclusive-cgroup `memory.current`, `memory.peak`, and memory events. PSS apportions shared mappings; summed RSS can double-count them. Cgroup memory includes costs that process PSS omits, including filesystem cache and kernel allocations.

In a quiet shared-engine experiment:

```text
incremental peak = max over time of
	(preprocessing process-tree PSS
	 + shared runtime/engine PSS during extraction
	 - matched runtime/engine baseline PSS)
```

Measure the Node gateway and supervisor explicitly as part of the shared-runtime term, plus all engine descendants, including an ACP process if the runtime retains one for chat. A controlled baseline uses the same loaded catalogs and configuration, without extraction. If extraction triggers a normally stopped runtime engine, count that cold-start growth too; do not subtract a hypothetical always-running baseline. Standalone production reports the full service, supervisor, and dedicated engine footprint; there is no pre-existing shared engine baseline to subtract.

Concurrent chat needs paired control and treatment runs with identical workloads and multiple repetitions. Subtracting unrelated peaks or measuring a busy shared development cgroup cannot attribute preprocessing cost. Report total Machine memory and chat latency separately.

The 500 MB target is a performance objective, not an unconditional kernel limit for all administrator-selected inputs. Benchmark defaults must have a stated input envelope. Operators can raise limits or concurrency and accept the measured trade-off. Keep actual configured byte, pixel, row, and deadline limits enforced.

Initial memory choices:

1. One parser and one preprocessing model operation at a time. No worker pool or extra engine.
2. No preprocessing ACP client or extraction broker in any deployment mode. Use the same HTTP adapter.
3. Readiness and native jobs do not warm a model or create sessions.
4. Use one canonical OpenCode location and fresh short-lived extraction sessions. Capture evidence, then delete completed internal history.
5. Process PDF pages sequentially; release page/render references promptly. Measure whole-page rendering before crop, which can allocate more than the final image suggests.
6. Keep image count, encoded bytes, total pixels, text, output, and HTTP payload limits explicit. Preserve one encoding path where possible rather than accumulating bytes, base64 strings, and JSON copies across parsing and HTTP submission.
7. Keep the parser subprocess so its memory returns when the job ends. Spawn PDF-specific imports only for PDF jobs.
8. If native-only serving remains about 50 MB idle, optimize imports only after measurement shows a worthwhile reduction. Do not replace the HTTP/MCP stack speculatively.
9. Make parser address-space and aggregate resident-memory metrics distinct. The current 2 GiB `RLIMIT_AS` is not a 500 MB resident-memory guarantee. Do not lower it to 500 MB without testing PDFium and mapped libraries.

Proposed default goals, subject to the real OCR probe:

| Measurement | Goal |
| --- | --- |
| Service idle, excluding existing engine baseline | At most 60 MB PSS initially; reduce further where profiling identifies a material saving. |
| Incremental runtime production peak, declared default workload | Less than 500 MB PSS; include engine, gateway, and supervision growth. |
| Incremental post-job idle | Return close to the matched baseline after cleanup; report 60-second and five-minute retention. |
| Cold engine-dependency readiness | Less than five seconds on the deployment target as an initial objective; model latency measured separately. |
| Full local-dev footprint | Report separately, including owned server and guardian. Prefer less than 500 MB on representative OCR, but do not relabel it as the production incremental measurement. |
| Full standalone production footprint | Report separately, including dedicated server and external supervision. Do not subtract a hypothetical shared engine baseline or claim the runtime incremental target establishes this footprint. |

### 9. Measurements already available

Fresh direct probes used Python 3.14.3 and the pinned OpenCode 2.0.19 binary. All values below are decimal MB. Process identities were sampled at approximately 50 ms intervals. The largest gap in the reported windows was 52.584 ms.

| Probe | Median PSS | Sampled maximum PSS | Sampled maximum RSS |
| --- | ---: | ---: | ---: |
| Current HTTP service idle, 15 seconds | 51.769 MB | 51.769 MB | 63.742 MB |
| HTTP service processing CSV | 59.195 MB | 66.135 MB | 88.601 MB |
| HTTP service processing workbook | 67.882 MB | 67.894 MB | 90.849 MB |
| Standalone server catalog initialization | 212.864 MB | 226.885 MB | 231.109 MB |
| Standalone server warm idle, 40 seconds | 209.814 MB | 213.061 MB | 217.285 MB |
| Service and standalone server idle together | 263.191 MB | 263.358 MB | 279.683 MB |
| Service, CSV worker, and standalone server together | 273.177 MB | 273.374 MB | 300.261 MB |

The CSV had 100,000 data rows and eight columns, approximately 8.57 MB. The workbook had 10,000 data rows and eight columns, approximately 0.416 MB. HTTP jobs took 3.078 and 1.500 seconds, including publication. Direct CLI probes and a second HTTP CSV run also succeeded: five native jobs in total.

Authenticated service readiness took 0.770 seconds. Standalone `/api/info` readiness took 0.392 seconds. Its extraction-agent catalog was polled separately. Listener readiness is not complete extraction readiness.

The standalone server had clean discovery directories, an empty model catalog, and no provider credentials. No model sessions or prompts were created. Combined figures were sampled concurrently, not obtained by adding independent peaks.

**These observations do not establish the production OCR target.** They measure current native behavior and an unloaded standalone engine. They exclude the proposed cleanup/supervision integration, live inference payloads, guardian overhead in the proposed dev launcher, concurrent chats, and non-process memory. Sampling can miss shorter peaks. The measurement environment's shared cgroup cannot provide attributable aggregate peaks.

Retained earlier PDF evaluations with an owned ACP/server pair reached 560,944,128 and 531,181,568 bytes PSS, approximately 561 and 531 MB decimal. The first peak included about 377 MB for the server and 81 MB for ACP. Removing a duplicate engine is therefore material, but subtracting those old process values does not predict shared-server OCR growth.

## Implementation Plan

### Implementation status, 2026-10-01

Phases 1 and 2 are implemented in this repository: legacy retirement, the pinned
HTTP adapter, persistence-first operation bindings, all-unsettled recovery,
predecessor fencing, private renewable projection, isolated development launch,
standalone owner supervision, and authenticated engine reconciliation.

The user selected runtime ownership of one ordinary standalone HTTP server for
both chat and extraction. A supported ACP-child handoff is no longer the intended
runtime exposure mechanism. The separate gateway repository still needs that
ownership change, authenticated preprocessing proxying, projection filtering,
activity accounting, and shutdown wiring. Phase 0 shared-runtime acceptance,
Phase 3, and Phase 4 are not complete.

Pinned-server verification includes real completed text and image turns using a
loopback deterministic provider, alongside non-inference primitives and restart
fault probes. These checks verify wire behavior and local settlement. They do
not establish live OCR quality, paired chat latency, deployed Fly behavior, or the
500 MB incremental memory objective. Current verification and review evidence
are retained in the implementation handoff.

### Phase 0: Prove direct HTTP exposure, lifecycle, and memory budget

1. Establish the selected direct HTTP connection contract against the runtime owner's actual startup path: supported ACP-child endpoint handoff or explicit standalone-server ownership with HTTP chat integration. Confirm engine launch mode/version, identity delivery, supervisor fencing, cleanup projection, and history filtering. Validate the same owner contract with a dedicated externally supervised standalone server. Approach A is selected; this probe chooses its exposure mechanism and verifies feasibility.
2. Run a one-operation extraction probe against the shared runtime engine: text, image, model/agent selection, evidence capture, and scoped cancellation while another conversation continues.
3. Inspect the effective extraction prompt/configuration and verify that runtime chat plugins and local instructions do not alter it.
4. Exercise a service/gateway restart while the engine survives, then a full engine/Machine restart. Repeat service and engine replacement under standalone supervision. Verify known-ID readback and cleanup for an unknown acknowledgement, no unexpected engine-side continuation, and no client replay.
5. Benchmark a representative scanned report, a dense high-resolution page, figure OCR, the existing native fixtures, and a workbook stressing shared strings. Test a declared moderate default report and operator-raised limits separately.
6. Measure quiet-engine delta, paired concurrent-chat delta, cleanup retention, cold readiness, full standalone production footprint, and full-dev footprint. Use at least five fresh runs per scenario. Record hardware, image digest, version, model, profile digest, input hash, concurrency, and sample gaps.

Exit: a verified engine connection contract and evidence for or against the default 500 MB target. If it exceeds the target, report the allocation source and compare bounded pixels/payloads, session cleanup, or less concurrency before adding infrastructure.

### Phase 1: Retire the legacy runtime

Remove the retired trees and their callers as one coherent deletion. Refresh CLI help, environment examples, and service documentation. Preserve useful native-parser behavior through existing tests rather than retaining obsolete execution paths.

No new tests are warranted merely to assert deleted names are absent. Verify imports, packaging contents, maintained tests, and documented commands directly.

Exit: the maintained wheel and documented commands have no callers into the retired runtime; relevant existing tests pass.

### Phase 2: Implement engine connection and lifecycle

Implement `OpenCodeServerEngine` and the projection-based supervisor cleanup hook. Wire readiness and persist operation/session/input identity before HTTP mutation. Extend startup recovery to all unsettled operations and add explicit safe reconciliation/unblocking.

Add the isolated dev launcher, supplying its connection to the same HTTP adapter. Keep preprocessing-owned server launch entirely within that dev path. Provide standalone deployment configuration for the externally supervised server and its owner contract. Reuse validation and result/settlement handling in all three deployment modes.

Exit: HTTP-adapter conformance, lost-ack recovery, scoped cleanup, standalone supervision, dev isolation, and missing-dependency behavior pass with fake faults and the pinned real server's non-inference primitives.

### Phase 3: Integrate the Fly runtime surfaces

Package the lean Python environment into the existing image. Add service supervision and feature/readiness reporting. Preserve native `/preprocessing/*` links through the authenticated gateway.

Wire source-hash-bound submission, private processing paths, service credentials, extraction-session filtering from interactive history, Machine activity, capacity, and shutdown. Shared server credentials and provider-token refresh remain the runtime owner's responsibility.

Count queued, running, cancelling, and finalizing extraction work in Machine activity independently of browser heartbeats. The service's `jobSeconds` already bounds wall time as well as the parser CPU limit. Do not confuse the service-generation lease with the backend's longer Machine-activity lease.

Exit: the built runtime uses one OpenCode engine, preserves source/job/result contracts, and continues work beyond browser idle timeout without exposing internal sessions as chats.

### Phase 4: Validate and document the operating envelope

Run image-level, isolated Fly project, and standalone production scenarios with real OCR. Verify service/gateway/engine loss, lease expiry, wrong model selection, lost responses, publication repair, and chat continuity where applicable. Document runtime, standalone production, and development setup, the measured default profile, and explicit operator overrides.

Roll out only after the runtime connection dependency is present. The existing Cline deployment is not a production substitute for the required OpenCode server.

Exit: live OCR resource/fault probes establish the operating envelope, or explicitly document a revised budget/limit decision before release.

## Observability

Expose authenticated dependency status: HTTP adapter, deployment mode and server owner, engine version/instance, extraction readiness, active operation, queue depth, settlement state, and last recovery result. Do not publish raw engine configuration or credentials.

Keep structured events for preparation, dispatch intent, admission acknowledgement, terminal evidence, cancellation, settlement uncertainty, recovery, and session cleanup. Correlate service scope, job, operation, generation, engine instance, and session without logging document content.

Record cold readiness time, per-operation duration, sample provenance, image bytes/pixels, retained-session count, and process memory. Benchmark reports include peak, idle, and post-job memory. Runtime-supplied usage is evidence, not a complete billing ledger; missing usage remains unknown.

## Verification Strategy

### Verification performed for this design

Decision: primary-source inspection and disposable direct resource probes. No maintained implementation behavior changed, so no new repository tests were added and the application suite was not rerun solely for document edits.

The native and standalone probe verifier reported:

> PASS: all five native jobs succeeded and published; pinned version verified; empty models; no prompt routes; private logs 0600; all owned process identities absent.

No provider/model calls, Docker processes, installations, or production deployments were performed by these probes. Native startup and shutdown were observed; the native service logged `Application shutdown complete.` The measurements and their limitations are reproduced above so this document remains readable without the temporary evidence directory.

### Verification required during implementation

- Run the existing preprocessing suite and packaging/import checks after retirement and interface changes.
- Add red-green tests for missing dependency, delayed readiness, unavailable image model, required persistence before prompt, scoped cancellation, and reconnect without replay. Confirm each test rejects a credible wrong behavior.
- Run HTTP adapter conformance scenarios against a fault-injecting HTTP fake and the pinned real server: prepared does not mean dispatched; admission does not mean success; idle does not mean valid output; cancel/close never stops shared chat or an externally owned standalone server.
- Inject loss between session creation, binding commit, prompt dispatch, acknowledgement, result capture, and cleanup. Include `outcome_unknown` records after restart.
- Use jitter/stress tests for service-generation replacement, lease expiry, concurrent chat/extraction execution, history projection updates, and cancellation.
- Plant harmless sentinel global configuration and instruction files in an isolated test home. Confirm the dev launcher reads only its controlled roots. Do not use actual developer credentials as test data.
- Benchmark actual scanned OCR and post-job memory with attributable sampling and paired baseline runs. No performance claim is accepted solely from the native/empty-catalog measurements.
- Verify the built image runs ordinary supervised processes with no Docker socket or daemon dependency. Confirm the dev-only launcher is absent from the production startup path.
- Exercise standalone production without a gateway or browser: externally supervised engine readiness, service restart while the engine survives, engine replacement identity, owned-session cleanup, and recovery without replay.

## Open Questions

1. Resolved on 2026-10-01: the runtime will own one standalone HTTP server and use HTTP for chat and extraction. The connection probe must still establish endpoint delivery, cleanup, and history filtering before runtime release.
2. What vision model and default report envelope establish the 500 MB acceptance workload? This requires live OCR and quality evidence, not only a catalog declaration.
3. Does the selected runtime launch path use ordinary unregistered `serve` or managed recovery? Verify standalone deployment's launch mode too. Extraction restart semantics depend on the actual owner contract, not the transport label.
4. Can the runtime expose and verify the effective extraction context without globally changing interactive configuration? Per-session selection alone is not global isolation.
5. What cold-readiness latency and post-job retained growth are acceptable on the deployment hardware? Five seconds and the idle goals above are proposed starting objectives.

Confirmed decisions: Approach A and one native HTTP extraction adapter across all deployment modes; runtime production shares one runtime-owned engine; standalone production uses a dedicated externally supervised server; preprocessing-owned launch is dev-only; the runtime memory budget includes incremental shared-server and supervision growth, with full standalone/development footprints reported separately; the legacy runtime is retired; no Docker-in-Docker is introduced.

## References

- R1: [Runtime migration status at the inspected commit](https://github.com/EigenformAI/geocluster-ai-ide-BE/blob/8585645b1d48a7a91901221ac5c879e1dba09cf3/docs/OPENCODE_MIGRATION_STATUS_20260930.md).
- R2: [Approved ACP migration plan, engine ownership and boot](https://github.com/EigenformAI/geocluster-ai-ide-BE/blob/8585645b1d48a7a91901221ac5c879e1dba09cf3/docs/OPENCODE_MIGRATION_PLAN.md).
- R3: [Fly: Docker images as Machine packaging](https://fly.io/docs/blueprints/working-with-docker/).
- R4: [Fly process groups run in separate Machines](https://fly.io/docs/launch/processes).
- R5: [OpenCode v2 standalone server documentation](https://opencode.ai/v2/docs/cli/web/).
- R6: [Pinned 2.0.19 session HTTP protocol](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/protocol/src/groups/session.ts), [message schema](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/schema/src/session-message.ts), and [image conversion](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/cli/src/acp/content.ts).
- R7: [Pinned ACP private-server lifecycle](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/cli/src/services/standalone.ts) and [ACP handler](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/cli/src/commands/handlers/acp.ts).
- R8: [Fly volume ownership and attachment](https://fly.io/docs/reference/volumes).
- R9: [Pinned model-catalog readiness caveat](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/protocol/src/groups/model.ts) and [ACP catalog caching](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/cli/src/acp/service.ts).
- R10: [Pinned configuration discovery](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/core/src/config/discovery.ts), [instruction discovery](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/core/src/instruction-discovery.ts), and [configuration schema](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/schema/src/config.ts).
- R11: [Pinned managed-server recovery and at-least-once contract](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/core/src/session/execution/restart.ts).
- R12: [Pinned session creation and removal](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/core/src/session.ts#L250-L415), [session ID schema](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/schema/src/session-id.ts), and [HTTP create handler](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/server/src/handlers/session.ts#L115-L155).
- R13: [Pinned prompt first-admission and wake semantics](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/core/src/session/session.ts#L145-L200) and [inbox reconciliation](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/core/src/session/inbox.ts#L120-L205).
- R14: [Pinned per-session transport cleanup](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/core/src/session/model-transport.ts), [location cache lifecycle](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/core/src/location-services.ts#L15-L90), and [targeted debug eviction](https://github.com/anomalyco/opencode/blob/1fd016ef32286de9489b7b24f1029f52c49a27b3/packages/protocol/src/groups/debug.ts#L18-L33).
