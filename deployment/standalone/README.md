# Run the standalone container

This optional image runs the preprocessing service and a dedicated OpenCode
2.0.19 HTTP engine. It exposes authenticated HTTP and Streamable HTTP MCP on
port 8765. Use Linux, a Docker Engine with working pidfd support, and Docker
Compose. The image runs as UID/GID 10001.

## Configure extraction

1. Copy `deployment/examples/extraction-engine.json` to an absolute path outside
   the checkout.
2. Replace the placeholder provider endpoint and `vision` model declaration with
   your provider's actual model ID and capabilities. Set the top-level `model`
   to that explicit `provider/model`. Retain the controlled extraction agent,
   deny-all permissions, and disabled tools, plugins, skills, and recovery behavior.
3. Keep `settings.apiKey` as `{env:DEV_OPENCODE_PROVIDER_TOKEN}`. Supply the value
   separately through that environment variable. To use another variable, change
   the interpolation and set `PREPROCESSING_CREDENTIAL_ENV` to its name.
4. Make the configuration and input files readable by UID 10001. The configuration
   contains credential-variable names, not secret values; a mode-0644 file is
   sufficient. The input directory is mounted read-only.
5. Set the absolute configuration and workspace paths and select the same model:

```bash
export PREPROCESSING_ENGINE_CONFIG=/absolute/path/extraction-engine.json
export PREPROCESSING_WORKSPACE=/absolute/path/inputs
export PREPROCESSING_MODEL=deployment/vision
read -r -s -p 'Provider credential: ' DEV_OPENCODE_PROVIDER_TOKEN
export DEV_OPENCODE_PROVIDER_TOKEN
read -r -s -p 'Service bearer token: ' PREPROCESSING_TOKEN
export PREPROCESSING_TOKEN
```

The service token must be nonempty printable ASCII without spaces. To use an
explicit environment file, pass `--env-file /private/standalone.env` to Compose.
Keep that credential file private. The build context excludes environment files,
credentials, source documents, and private service state.

For OpenAI API-backed Luna, declare `openai/gpt-6-luna`, package
`@opencode/ai/providers/openai/responses`, base URL `https://api.openai.com/v1`,
and text/image input with text output. Supply an OpenAI **API key** through the
named variable. A ChatGPT subscription login is a separate account workflow;
this isolated launcher does not import your personal OpenCode or Codex login.
Free Zen models also need authentication accepted by Zen. The live catalog
listing a free model does not establish anonymous access.

## Start and process files

Run these commands from the repository root:

```bash
docker compose -f deployment/standalone/compose.yaml --profile standalone up --build -d
docker compose -f deployment/standalone/compose.yaml --profile standalone ps
```

The owner publishes a private descriptor after engine readiness and recovery.
The supervisor then starts the service. Health changes to `healthy` when the
authenticated capabilities response reports `engineAvailable: true`. Compose
restarts an unexpectedly exited pair; health status alone does not cause restart.

Submit a workspace-relative path with a fresh UUID:

```bash
curl --fail-with-body http://127.0.0.1:8765/preprocessing/jobs \
	-H "Authorization: Bearer $PREPROCESSING_TOKEN" \
	-H 'Content-Type: application/json' \
	-H "Idempotency-Key: $(python -c 'import uuid; print(uuid.uuid4())')" \
	-d '{"source":{"kind":"workspace-file","path":"input.csv"}}'
docker compose -f deployment/standalone/compose.yaml --profile standalone exec standalone \
	preprocess status --url http://127.0.0.1:8765 --token-env PREPROCESSING_TOKEN --job JOB_ID
```

Read the returned job ID, then replace `JOB_ID` in the status command. A ready
result has `documents.state: ready`. Download the manifest and its listed
products through the job's HTTP routes. Configure MCP clients with
`http://127.0.0.1:8765/mcp` and the same bearer header. The host publishes only
the preprocessing port on loopback; the engine listener is private.

Set `PREPROCESSING_PORT` to change the host port. The container always uses 8765.
For multiple deployments, supply a distinct Compose `--project-name` so each
deployment has separate engine and service volumes.

## Stop, restart, and retain evidence

```bash
docker compose -f deployment/standalone/compose.yaml --profile standalone stop
docker compose -f deployment/standalone/compose.yaml --profile standalone start
docker compose -f deployment/standalone/compose.yaml --profile standalone logs standalone
```

The supervisor stops the service first, waits up to 180 seconds, then stops the
engine owner with a 30-second allowance. The container has a four-minute stop
grace. Uvicorn can re-raise the requested SIGTERM after application teardown;
that expected exit is accepted. A timeout forces the child to exit and records
`shutdown timed out; forcing exit, recovery required`. Job settlement remains
defined by persisted job and operation evidence.

After a forced or unexpected stop, restart uses the retained ownership records
and engine process identity. It never replays extraction prompts. Interrupted
jobs require a new explicit submission. Inspect container logs and
`docker inspect --format '{{.State.ExitCode}}' CONTAINER_ID` for shutdown failure;
`docker compose stop` does not propagate the container's exit code.

The supervisor allows 300 seconds for fresh descriptor publication. Operators
can override `--startup-seconds`, `--shutdown-seconds`, and
`--owner-shutdown-seconds` by replacing the Compose command. Keep
`stop_grace_period` longer than both shutdown allowances combined, plus time
for escalation and reaping. Slow or unresolved recovery may need a longer
startup allowance.

The baked health check uses container port 8765 and `PREPROCESSING_TOKEN`.
If you change `--port` or `--token-env`, also replace the Compose `healthcheck.test`
command with `python /opt/preprocessing/supervise.py health` and the matching flags.
Changing only `PREPROCESSING_PORT` changes the host mapping and needs no health
check override.

Compose retains `service-data` and `engine-data` named volumes. The first includes
SQLite, snapshots, sealed results, publication state, and `documents`. The second
includes engine history, process ownership evidence, and private logs. The
descriptor lives in a mode-0700 tmpfs and is regenerated at startup. Preserve
the same canonical mount paths when restoring volumes. `down` retains named
volumes; `down --volumes` deletes retained jobs and engine recovery evidence.

## Build or reuse the lean service

Build the standalone image explicitly:

```bash
docker build -f deployment/standalone/Dockerfile -t preprocessing-standalone:local .
```

The final image contains the production wheel, its locked dependencies, the
pinned native engine binary, and the deployment-only supervisor. Build stages
contain Node/npm and uv; the final image contains neither. Linux amd64 uses the
baseline binary. Arm64 binary selection is wired into the build but needs an
arm64 runner for execution verification.

When upgrading OpenCode, update the version in the Dockerfile, supervisor
descriptor gate, and Python engine/launcher constants together. An incompatible
descriptor remains blocked rather than admitting another engine version.

The ordinary `uv build --wheel` artifact contains only the `preprocessing`
package. Installing that wheel does not install OpenCode, Node, Docker, or the
standalone supervisor. Runtime integration can install the wheel and connect
`preprocess serve` to its own shared engine descriptor. To opt out, omit this
Dockerfile and the `standalone` Compose profile from your deployment.
