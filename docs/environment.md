# Environment Setup

The extraction pipeline runs in this repo. Map analysis depends on the separate
`peace-tool-pool` MCP server, which is managed as an environment requirement.

## Local Workstation Path

This machine uses Nix flakes and direnv:

```bash
direnv allow
uv sync
```

When direnv loads, it:

- Enters the Nix dev shell from `flake.nix`.
- Provides `uv`, `git`, Poppler, `jq`, and `bash`.
- Runs `scripts/sync-peace-tool-pool.sh --print-env`.
- Clones or updates `peace-tool-pool` from `https://github.com/Lexikat-Pte-Ltd/peace-tool-pool.git`.
- Tracks `PEACE_TOOL_POOL_REF=main` by default.
- Exports the `GEOMAP_*` environment expected by `peace-tool-pool-mcp`.

The `peace-tool-pool` checkout defaults to `.cache/peace-tool-pool`, which is
gitignored. Override it in `.env` only if you need a different location:

```bash
PEACE_TOOL_POOL_ROOT=/absolute/path/to/peace-tool-pool
```

## Latest-Main Policy

`peace-tool-pool` is intentionally not pinned. The sync script polls upstream
`main` every time direnv loads and every time the MCP wrapper starts:

```bash
bash scripts/sync-peace-tool-pool.sh
bash scripts/peace-tool-pool-mcp
```

Updates use `git fetch` followed by `git pull --ff-only`. If the local checkout
has local changes or has diverged from upstream, setup fails instead of silently
discarding work. Delete `.cache/peace-tool-pool` or point `PEACE_TOOL_POOL_ROOT`
at a clean checkout to recover.

## MCP Startup

`opencode.json` starts the server through a repo-local wrapper:

```json
"peace-tool-pool": {
  "type": "local",
  "command": ["bash", "scripts/peace-tool-pool-mcp"],
  "cwd": "."
}
```

The wrapper syncs upstream `main`, exports `GEOMAP_*`, then executes:

```bash
uv run --project "$PEACE_TOOL_POOL_ROOT" \
  --extra mcp \
  --extra detectors \
  --extra geo \
  --extra knowledge-local \
  peace-tool-pool-mcp
```

MCP protocol stdout remains clean because sync/status output goes to stderr.

## Non-Nix Users

The current portable fallback is manual host setup:

```bash
# Install uv, git, and Poppler using your OS package manager first.
uv sync
eval "$(bash scripts/sync-peace-tool-pool.sh --print-env)"
bash scripts/peace-tool-pool-mcp
```

This is sufficient for developers who do not use direnv, but it is not a final
onboarding story. The likely cross-user path is a Docker image that installs the
host tools, checks out latest `peace-tool-pool` `main` at container start, mounts
`output/`, and runs `peace-tool-pool-mcp` over stdio.
