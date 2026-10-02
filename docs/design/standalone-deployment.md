# Opt-in standalone deployment

## Problem Statement

The standalone extraction service already connects to OpenCode 2.0.19 through
an externally owned HTTP server. It needs runnable deployment packaging and
end-to-end acceptance evidence. Runtime deployments must retain the lean Python
wheel and choose their own shared-engine supervision.

## Context

The wheel contains only `preprocessing` and installs no OpenCode, Node, or process
supervisor dependency. `preprocess-engine-owner` owns a dedicated ordinary
`opencode serve` process under a Linux guardian. It publishes admission credentials
after catalog readiness, predecessor fencing, and owned-session cleanup.
`preprocess serve` owns SQLite, snapshots, parsing, and publication.

The descriptor names a loopback endpoint. Writer fencing uses Linux process
identity and pidfds. Both components therefore need the same network and process
namespace and UID. Durable service and engine roots must survive replacements at
the same canonical paths. Engine and descriptor directories are private.

The current systemd examples order the two units, but the service's 30-second stop
timeout is shorter than its documented 131-second model settlement window.
Snapshot and publication I/O can take longer. Timeouts are configurable operational
allowances, not proof of settlement.

## Goals

- Make a standalone deployment an explicit opt-in, outside the Python wheel.
- Install the production wheel and an exactly pinned Linux OpenCode binary in a
  single optional image, with no Node runtime or development dependencies.
- Start the service only after the owner publishes a fresh, valid descriptor.
- Stop the service before the engine owner. Fail visibly if either child exits
  unexpectedly or shutdown requires escalation.
- Preserve both durable roots, use a non-root UID, and expose only authenticated
  preprocessing HTTP and MCP.
- Verify a clean install, the real image, native and model-assisted extraction,
  replacement/recovery, and sampled full-footprint memory.

## Non-Goals

- Implement chat, browser uploads, R2, or a runtime that has not yet migrated.
- Add a second engine or a standalone supervisor to default runtime installations.
- Treat a deterministic provider as evidence of live OCR quality.
- Claim a shared-runtime incremental memory measurement from a dedicated server.

## Approaches Considered

### APPROACH A: Install two systemd units (Minimal Viable)

Summary: adapt the existing owner and service units and install the wheel and
binary separately. Complexity: S. Risk: Low.

Pros:

- Reuses installed system supervision and adds no resident supervisor.
- Leaves binary installation and credential provisioning explicit.

Cons:

- Requires host-specific paths, a service account, and installation steps.
- Unit ordering does not establish descriptor readiness. Independent owner
  replacement can fence a live service writer.

Reuses: current example units and the standalone owner.

### APPROACH B: Optional single image and Compose profile (Selected)

Summary: build an explicitly selected standalone image containing the production
wheel, pinned binary, and a small stdlib pair supervisor. Complexity: M. Risk: Medium.

Pros:

- Supplies repeatable installation, shared namespaces, persistent named volumes,
  health checks, and coordinated lifecycle in one runnable artifact.
- Deployment files and supervision are excluded from the default wheel.
- Docker's opt-in init reaps orphans without adding a Python supervisor package.

Cons:

- Adds one small resident process to standalone deployments.
- Container namespace and default seccomp behavior need actual pidfd/process-loss checks.
- Images and volumes require separate operator lifecycle management.

Reuses: both existing commands, owner readiness and recovery, and Linux guardians.

### APPROACH C: General-purpose supervisor and installation framework (Ideal Architecture)

Summary: add a supervisor package and host/container installer with templated
configuration. Complexity: L. Risk: Medium.

Pros:

- Provides richer restart policies and multiple installation targets.

Cons:

- Adds dependencies and another policy surface for a pair of existing owners.
- Encourages packaging-specific state beyond the authoritative service ledger.

Reuses: existing command interfaces. Deferred because the extra abstraction is not
needed for standalone acceptance.

## Decision

Select B. Keep all packaging and the supervisor under `deployment/standalone/`.
Build explicitly with its Dockerfile, or opt in through the Compose `standalone`
profile. Do not change production dependencies or the wheel's package list.

Use a multi-stage build: obtain the pinned platform binary in a temporary Node
stage, build the wheel in a temporary Python/uv stage, and install both in a slim
Python image. Support Linux amd64 with the baseline binary and Linux arm64 with
its corresponding glibc binary. Arm64 is build-wired, with execution acceptance
requiring an arm64 runner. Copy an explicit allowlist of build inputs. Pre-create
the named-volume mountpoints as UID/GID 10001 with mode 0700.

The stdlib supervisor launches ordinary module commands in separate process groups:

```text
run_pair(owner_command, service_command, descriptor_path, stop_event,
         startup_seconds=300, shutdown_seconds=180, owner_shutdown_seconds=30)
```

Before launch it remembers any prior descriptor's `engineInstanceId`. It uses the
CLI's bounded descriptor read and checks a nonempty changed `engineInstanceId`,
the pinned version, and standalone mode while monitoring owner exit and shutdown.
With no prior descriptor it accepts the first valid publication. The service
performs full descriptor validation; owner publication is the readiness authority.
The supervisor does not infer engine death or write fencing evidence.

Any spontaneous child exit, including exit 0, returns failure and stops the pair.
A signal during startup stops the owner without launching the service. A signal
after service launch stops the service first, waits for its exit, then stops the
owner. Nonzero child exits and configurable timeout escalation return failure,
except the SIGTERM re-raised by Uvicorn after a requested teardown. A requested
signal exit is a process lifecycle result, not proof of a job's settlement state.
Escalation kills the exact supervised child; worker parent-death protection and
the engine guardian handle descendants. Container init reaps adopted orphans.
Later startup uses existing durable recovery rather than claiming graceful success.
The startup allowance includes owner process checks, listener/catalog initialization,
and recovery; operators can raise it for slow or unresolved cleanup.

Compose enables init and `restart: unless-stopped`. It persists separate service
and engine volumes and uses a private UID/GID-10001 tmpfs for the descriptor at
`/run/preprocessing/engine.json`. It binds the workspace and controlled engine
configuration read-only. The service listens on `0.0.0.0:8765` with mandatory
`--token-env`; the host publishes only `127.0.0.1:<port>:8765`. Credentials are
explicit named environment variables. Same-UID processes and the Docker operator
are trusted with these credentials.

Compose allows four minutes for the default ordered shutdown. Any override must
keep its stop grace longer than the sum of service and owner allowances plus
escalation margin. Forced shutdown is visible in stderr and the container exit
code; `docker compose stop` itself does not report that exit code. Health checks
authenticate to capabilities and require engine readiness. An unhealthy check
does not cause Compose to restart; unexpected child exits do.

The existing isolated owner accepts environment-based API credentials. ChatGPT
subscription login requires an explicit isolated account-provisioning workflow;
it cannot be substituted for an API key or inherited from the developer's HOME.
Attempt only approved Luna or explicitly approved free vision-capable Zen models.
If no usable credential/model is available, retain deterministic protocol evidence
and record the live-provider blocker.

## Implementation Plan

1. Add red-green lifecycle tests under `tests/preprocessing/` for stale/fresh
   descriptor gating, startup/child failure, ordered shutdown, escalation, and
   jitter around competing exit and stop events. Tests deliberately load the
   deployment-only supervisor from its source path.
2. Implement the deployment-only supervisor, health check, Dockerfile, profile,
   allowlisted build context, and standalone instructions.
3. Correct the example service stop allowance and document graceful versus forced
   recovery.
4. Build and install a fresh wheel outside the checkout. Inspect wheel RECORD and
   metadata: only `preprocessing` application code, no deployment scripts or engine
   runtime dependencies. Build the optional image and confirm Node/npm and
   development dependencies are absent. Run actual CLI/HTTP/MCP, extraction,
   crash/replacement, and persistence checks. Seed a stale descriptor in a direct
   supervisor test even though container replacements use fresh tmpfs.
5. Sample aggregate process PSS/RSS across the supervisor, service, worker, owner,
   guardian, and engine. Record full standalone footprint and sample gaps. Run
   approved live OCR if account access permits.
6. Review the complete implementation diff and verification evidence, correct
   accepted findings, and publish the design and acceptance notes.

## Observability

Child logs remain attached to the container. Supervisor lifecycle messages go to
stderr without credentials. Health reports dependency readiness. Durable private
logs, SQLite evidence, ownership projections, and artifacts retain their existing
locations. Container failure is distinct from job extraction/publication status.

## Verification Strategy

Use small meaningful red-green tests for new pair supervision behavior. Real
subprocess fixtures protect observable startup/shutdown ordering and failure
handling. Add jitter around signal/exit races. Existing engine/owner/guardian tests
protect transport and fencing. Direct clean-wheel and container execution verify
the disposable deployment and evaluation workflows. Record decisive commands,
outcomes, memory methods, model provenance, and limitations. Static implementation
review covers process lifetime, credential handling, build isolation, and the lean
default installation.

Run container lifecycle checks with Docker's default seccomp profile. Record the
tested Docker/kernel versions and exercise `pidfd_open`, `pidfd_send_signal`,
`prctl(PR_SET_CHILD_SUBREAPER)`, `prctl(PR_SET_PDEATHSIG)`, and pidfd polling through
actual owner/worker/process-loss behavior. Existing guardian checks reject
unsupported syscalls. No `seccomp=unconfined` or privileged workaround is acceptance
evidence. Existing crash-time temporary-file residue remains private and is not
treated as a new packaging cleanup policy.

## Open Questions

- Live Luna subscription access is not provisioned in the controlled owner. No
  approved API credential is present in the current environment. Verify whether
  the explicitly allowed free Zen vision model can run without a paid account.
- Only the available Linux amd64 Docker target can be executed here. Arm64 build
  wiring requires a corresponding runner for execution evidence.

## References

- [OpenCode v2 providers](https://opencode.ai/v2/docs/providers)
- [OpenCode v2 provider authentication](https://opencode.ai/v2/docs/cli/providers)
- [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [Codex authentication](https://developers.openai.com/codex/auth/)
- [Docker multi-stage builds](https://docs.docker.com/build/building/multi-stage/)
- [Compose profiles](https://docs.docker.com/compose/how-tos/profiles/)
