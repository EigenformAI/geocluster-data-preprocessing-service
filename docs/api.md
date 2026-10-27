# Resume and repair results

All HTTP mutations require `X-Processing-Store-Scope`. The authenticated
`GET /preprocessing/capabilities` response supplies `storeScope` and
`serviceGeneration`. The scope identifies the retained processor store. A normal
restart keeps the scope and changes the generation. A replacement store changes
the scope. Missing, repeated, or mismatched scope headers return HTTP 409 with
`PROCESSING_STORE_CHANGED` before mutation. MCP mutation calls require the same
HTTP header. Reads do not require the scope header.

The remote CLI obtains the current scope before resume or publication repair.
Clients with persisted requests retain the original scope. They must not replace
that scope and replay old intent into a replacement store.

## Resume extraction

Use resume to continue unfinished work in a sealed PDF result. Resume creates a
new generation from the parent's retained source, even if publication failed.
Unsealed interrupted work requires a new extraction instead.

Through a running owner, replace `JOB_ID` with the parent job:

```sh
uv run preprocess resume --url http://127.0.0.1:8765 --job JOB_ID
```

The command prints the admitted child job and returns without waiting. Poll its
new ID with `preprocess status`. For an authenticated owner, add
`--token-env PREPROCESSING_TOKEN` to both commands.

For HTTP or MCP submission, use the parent's exact snapshot reference, its job
ID as `resumeOf`, and a fresh submission UUID. Use `rerunOf` instead when you
want a new extraction without reusing completed work.

To resume locally, stop the existing owner first:

```sh
uv run preprocess resume --data-root /path/to/data --job JOB_ID \
	--engine-descriptor /private/engine.json
```

Local resume waits for completion and uses the parent's profile unless you
supply `--profile` or `--model`. Remote resume uses the running owner's profile.

Keep the requested models, parser versions, and extraction policy compatible
with the parent. You can change budgets and resource limits if retained evidence
still fits. Additional-pass spending survives resume; increase
`maxAdditionalPasses` to grant more total dispute passes.

To deliberately repeat completed work on selected pages, add
`--table-force-pages 2,5`. Only a force list explicitly supplied in the new
submission invalidates reusable work. An inherited list does not repeat it.

## Repair publication

Extraction and shared publication have separate outcomes. If a sealed result
has `documents.state` other than `ready`, retry publication through its owner:

```sh
uv run preprocess reconcile --url http://127.0.0.1:8765 --job JOB_ID
```

For an authenticated owner, add `--token-env PREPROCESSING_TOKEN`.
After stopping the owner, you can repair publication locally:

```sh
uv run preprocess reconcile --data-root /path/to/data --job JOB_ID
```

Local repair needs neither an engine descriptor nor a model. It publishes the
sealed result without extracting again.

## Reopen blocked engine admission

First ask the engine owner to supply writer-fencing or confirmed old-engine-death
evidence. See [engine ownership and recovery](engine.md#ownership-and-recovery).
Then send `POST /preprocessing/engine/reconcile` to the running service, with its
expected store-scope header and bearer token if authentication is enabled. This rechecks ownership without
resending extraction prompts. Publication repair cannot settle engine ownership.

## Cancel an uncertain submission

Send `POST /preprocessing/submissions/{submissionKey}/cancel` with the expected
store-scope header, the service credential, and `Content-Type: application/json`.
The body contains the original upstream submit request:

```json
{"request":{"source":{"kind":"workspace-file","path":"report.pdf","expectedSha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}}}
```

The response is `{"cancelled":true,"job":null}` when no job was admitted. The
processor durably records the key and normalized intent before responding.
A late same-key submit returns HTTP 409 with `SUBMISSION_CANCELLED`, including
a submit that was waiting for readiness when cancellation won. The record
survives restart. Changed-intent reuse returns `IDEMPOTENCY_CONFLICT`.

If admission won, `job` contains the existing job. The processor records the key
fence and job cancellation intent together before cleanup. A repeated same-intent
submit returns that job, not new work. `cancelled: true` acknowledges the durable
admission decision, not supplier settlement. A `cancelling` job still requires
status observation or engine reconciliation. Remote charges can remain unknown.
