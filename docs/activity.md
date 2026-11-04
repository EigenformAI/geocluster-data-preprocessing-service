# Processor activity API

`GET /preprocessing/activity` returns a version 1 inventory for the current
processor owner. It uses the listener's bearer authentication and requires no
store-scope header. It accepts no query parameters. `HEAD` has the same status
and headers without a body; unsupported methods return 405. Responses have
`Cache-Control: no-store`.

Observation does not refresh engine readiness, renew leases, enqueue work,
reconcile ownership or publication, cancel jobs, or wake the scheduler.
Capabilities embeds the same inventory in `activity`; capability discovery
itself can refresh readiness and job holds.

## Identity and completeness

- `contractVersion`: `1`.
- `incarnation`: the service generation string, stable until the owner restarts.
- `storeScope`: the retained store identity. A restart preserves it; replacement
  of the store changes it. A report describes its current scope and incarnation,
  not a previous owner's inventory.
- `revision`: a nonnegative safe integer, monotonic within an incarnation.
  Successful job, operation, and cancellation-fence commits advance the evidence
  used by the next observation. Changed activity facts also advance the revision.
  Unchanged observations retain their revision. Heartbeat timestamps do not
  advance it. Revision is an observation identity, not a compare-and-swap or
  suspension fence; it need not record unobserved intermediate local-task states.
- `complete`: whether the inventory has established ownership and settlement
  authority. Administrative mode, startup, shutdown, missing ownership projection,
  predecessor uncertainty, expired leases, and unknown settlement make it false.
- `settlementUnknown`: true whenever that authority is uncertain.

An authoritative empty inventory requires `complete === true`,
`settlementUnknown === false`, and zero `runnable`, `settling`, and `parked`.
Unavailable reports and authentication failures are not empty inventories.

## Counts and capacity

All numeric fields are nonnegative JavaScript-safe integers, at most
`9007199254740991`. Counts describe conservative blockers, not distinct jobs:
one running job can contribute runnable work and several settling effects.

- `runnable`: queued and running jobs. Model-held and dependency-blocked queued
  jobs remain runnable; they are not parked.
- `settling`: cancelling jobs, unpublished results, unsettled ledger operations,
  active worker ownership, snapshot and finalization tasks, worker processes,
  pending admissions, engine operations and observations, stop tasks, held
  publication, recovery, and cleanup barriers, and uncertain ownership.
  A result with `documents.state` other than `ready` remains settling, including
  failed publication. Catalog publication remains settling after the document
  field first becomes ready.
- `parked`: `0`. This service does not implement park or activate, and does not
  advertise a scheduling contract.
- `terminal`: jobs in succeeded, partial, failed, cancelled, or interrupted
  states. A terminal job can still have settling effects.
- `activeJobId`: the locally owned worker job ID, or null. Ownership can outlast
  the job's terminal-state commit.
- `activeJobs`: nonterminal jobs; `queuedJobs`: queued jobs; `parkedJobs`: `0`.
- `pendingCaptures`: the owned snapshot task count, currently zero or one.
- `pendingAdmissions`: submissions awaiting completion, including readiness and
  resume validation before job insertion.
- `unsettledOperations`: unsettled ledger records, including terminal-job records.
- `predecessorPending`: whether a prior owner still needs fencing evidence.
- `runnableCapacity`: `{limit, occupied, available}`. `limit` is queue depth,
  `occupied` is the number of nonterminal jobs, and `available` is
  `max(0, limit - occupied)`, matching the queue-slot admission check. Snapshot
  tasks already belong to counted jobs. Slots do not guarantee admission:
  engine context, model policy, ownership, disk space, and source validation
  have separate checks.
- `retainedInputs`: `{count, bytes, parkedBytes}`. Count and bytes deduplicate
  committed `source.snapshotId` references across all retained jobs, including
  terminal jobs. They exclude orphan files and inputs not yet committed to a
  job. `parkedBytes` is `0`.

## Reasons and errors

`reasons` contains sorted diagnostic codes for present blockers:
`job-settlement-pending`, `publication-pending`, `operation-settlement-pending`,
`input-capture-pending`, `active-finalization-pending`, `active-worker-pending`,
`worker-process-pending`, `stop-pending`, `engine-operation-pending`,
`admission-pending`, `engine-observation-pending`, `cleanup-pending`,
`recovery-pending`, `ownership-unknown`, `admin-only`, `service-stopping`,
`scheduling-blocked`, `dependency-unavailable`, and `capacity-disabled`.
When capabilities has no open store, its incomplete activity contains
`store-unavailable` and no invented work counts. If an inventory cannot be
constructed, capabilities returns incomplete activity with `activity-unavailable`
while preserving dependency diagnostics and ownership recovery.

Authentication failures return 401 before diagnostics. Missing or unreadable
stores, invalid retained-input facts, unsafe numbers, and revision exhaustion
return 503 `UNAVAILABLE`, not rounded or zero counts. Unexpected failures use
the listener's sanitized error response. A successful inventory has fixed-size
fields and reasons; it contains no job list or document content. Observation
cost grows with retained job and operation history.
