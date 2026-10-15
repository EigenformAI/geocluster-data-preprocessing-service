"""One local job owner for CLI, HTTP, and MCP; parsers live in subprocesses."""
import asyncio
import base64
import copy
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from preprocessing.contracts import default_profile
from preprocessing.engine import validate_selection
from preprocessing.engine_version import VersionMismatch, compatibility, valid_version
from preprocessing.errors import ServiceError
from preprocessing.files import (
    atomic_json,
    digest_file,
    fsync_directory,
    open_regular,
    relative_parts,
)
from preprocessing.results import make_manifest, validate_generation
from preprocessing.resume_evidence import (
    prepare_resume,
    validate_parent,
    validate_resume_output,
)
from preprocessing.store import TERMINAL, Store, encode, unsettled
from preprocessing.table_recovery import (
    RECOVERY_EVENT_FIELDS,
    effective_profile,
    validate_override,
)


def now():
    return datetime.now(UTC).isoformat()


STAGE_LABELS = {'snapshot': 'Copying source', 'extract': 'Extracting content',
                'resolve-tables': 'Resolving tables', 'build-index': 'Building index',
                'seal': 'Preparing results'}


def current_stage(stage_id, completed, total, *, pdf=False):
    return {'id': stage_id,
            'label': 'Inspecting pages' if pdf and stage_id == 'extract' else STAGE_LABELS[stage_id],
            'unit': 'page' if pdf and stage_id == 'extract' else 'document',
            'completedUnits': completed, 'totalUnits': total}


def canonical(body):
    if not isinstance(body, dict) or set(body) - {'source', 'profileId', 'correlation', 'rerunOf', 'resumeOf', 'tableRecovery'}:
        raise ServiceError('INVALID_REQUEST', 'Unknown or malformed submission fields.')
    source = body.get('source')
    if not isinstance(source, dict):
        raise ServiceError('INVALID_REQUEST', 'A source is required.')
    shapes = {'workspace-file': ({'kind', 'path'}, {'expectedSha256'}), 'snapshot': ({'kind', 'snapshotId'}, set()),
              'upload': ({'kind', 'receiptId'}, set())}
    if source.get('kind') not in shapes:
        raise ServiceError('INVALID_REQUEST', 'Source kind is invalid.')
    required, optional = shapes[source['kind']]
    if not required <= source.keys() or source.keys() - required - optional or any(not isinstance(value, str) or not value for value in source.values()):
        raise ServiceError('INVALID_REQUEST', 'Source fields are invalid.')
    if 'expectedSha256' in source and not re.fullmatch('[0-9a-f]{64}', source['expectedSha256']):
        raise ServiceError('INVALID_REQUEST', 'Expected SHA-256 must be lowercase hexadecimal.')
    if 'path' in source:
        relative_parts(source['path'])
    correlation = body.get('correlation')
    if correlation is None:
        correlation = {}
    if not isinstance(correlation, dict) or set(correlation) - {'uploadRecordId', 'uploadBatchId'} or any(value is not None and not isinstance(value, str) for value in correlation.values()):
        raise ServiceError('INVALID_REQUEST', 'Correlation fields are invalid.')
    profile_id = body.get('profileId', 'default')
    if not isinstance(profile_id, str) or not profile_id or (body.get('rerunOf') is not None and not isinstance(body['rerunOf'], str)):
        raise ServiceError('INVALID_REQUEST', 'Profile or rerun identifier is invalid.')
    for field in ('rerunOf', 'resumeOf'):
        if body.get(field) is not None and (not isinstance(body[field], str) or not body[field]):
            raise ServiceError('INVALID_REQUEST', 'Parent job identifier is invalid.')
    if body.get('rerunOf') is not None and body.get('resumeOf') is not None:
        raise ServiceError('INVALID_REQUEST', 'resumeOf and rerunOf are mutually exclusive.')
    override = validate_override(body['tableRecovery']) if 'tableRecovery' in body else None
    return {'source': source, 'profileId': profile_id,
            'correlation': {'uploadRecordId': correlation.get('uploadRecordId'), 'uploadBatchId': correlation.get('uploadBatchId')},
            'rerunOf': body.get('rerunOf'), 'resumeOf': body.get('resumeOf'), 'tableRecovery': override}


class Service:
    def __init__(self, root: Path, workspace: Path, profile=None, engine=None, *,
                 admin_only=False, lease_interval=10, lease_seconds=30, model_policy=None,
                 observation_seconds=10, observation_max_age_ms=5000):
        self.root, self.workspace = Path(root).resolve(), Path(workspace).resolve()
        self.private = self.root / '.preprocessing'
        self.profile = effective_profile(profile or default_profile())
        self.work_deadlines = {}
        self.hard_deadlines = {}
        self.engine = engine
        self.model_policy = model_policy
        if (type(observation_seconds) not in (int, float) or not math.isfinite(observation_seconds)
                or observation_seconds <= 0 or type(observation_max_age_ms) is not int or observation_max_age_ms <= 0):
            raise ValueError('Engine observation bounds must be positive.')
        self.observation_seconds, self.observation_max_age_ms = observation_seconds, observation_max_age_ms
        self._observation_task = None
        self._observation = {}
        self._observed_monotonic = None
        self._scheduler_error = None
        self.admin_only = admin_only
        if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0 for value in (lease_interval, lease_seconds))
                or lease_interval >= lease_seconds):
            raise ValueError('Lease renewal must be positive and shorter than expiry.')
        self.lease_interval, self.lease_seconds = lease_interval, lease_seconds
        self.lease_deadline = None
        self.lease_heartbeat = None
        self.lease_expires_at = None
        self.lease_expired = False
        self.generation = uuid.uuid4().hex
        self.internal_sessions = set()
        self.engine_operations = set()
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.stopping = False
        self.process = None
        self.active = None
        self.scheduler = None
        self.owner = None
        self.store = None
        self.dropped_events = 0
        self.snapshot_task = None
        self.settlement_unknown = False
        self.finalize_task = None
        self.publication_lock = asyncio.Lock()
        self.recovery_lock = asyncio.Lock()
        self.cleanup_lock = asyncio.Lock()
        self.stop_tasks = {}
        self.close_task = None
        self.cleanup_closed = False
        self.engine_closed = False
        self.heartbeat_task = None
        self.predecessor = None
        self.predecessor_invalid = False
        self.projection_started = False
        self.last_recovery = None
        self.dependency = {'state': 'unconfigured' if engine is None else 'unavailable',
                           'configured': engine is not None, 'ready': False, 'executionReady': False}

    async def start(self):
        if self.engine is None and not self.admin_only:
            raise ServiceError('ENGINE_CONFIGURATION_REQUIRED', 'Configure an OpenCode server connection before starting processing.', 503, 'operator-action')
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner = (self.private / 'owner.lock').open('a+b')
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.owner.close()
            self.owner = None
            raise ServiceError('OWNER_ACTIVE', 'Another owner is using this state root.', 503, 'operator-action') from None
        for path in (self.private / 'sources', self.private / 'jobs', self.root / 'documents', self.root / '.document-publication'):
            path.mkdir(exist_ok=True)
        self.store = Store(self.private / 'state.sqlite')
        if not self.admin_only:
            self._read_predecessor()
            self.engine.bind(self._record_operation)
        build_hash = hashlib.sha256()
        for path in sorted(Path(__file__).parent.rglob('*.py')):
            build_hash.update(path.relative_to(Path(__file__).parent).as_posix().encode())
            build_hash.update(path.read_bytes())
        self.profile['build'] = {'upstreamCommit': '2c8a05864ce26b49ec88a78bfd0772e97e949958', 'sourceDigest': build_hash.hexdigest()}
        self.profile = effective_profile(self.profile)
        # Replacement ownership is fenced by the inherited lock in every parser.
        # Every surviving operation is reconciled, even on a terminal job.
        if not self.admin_only:
            # Execution stopped with the previous writer, independently of
            # whether its provider operations can be confirmed settled yet.
            for _, job in self.store.all():
                if (job['state'] not in TERMINAL and not self._never_started(job)
                        and not job.get('cancelRequestedAt') and job.get('interruptedAt') is None):
                    job['interruptedAt'] = now()
                    self.store.save(job)
            await self.reconcile_engine()
        for _, job in self.store.all():
            if not self.admin_only and job['state'] not in TERMINAL and not self._never_started(job):
                if self._needs_settlement(job['id']):
                    job.update(state='cancelling', finishedAt=None, stage=None, error=ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'Local engine settlement is pending.', retry='check-status').as_dict(job['id']))
                    self.store.save(job)
                    continue
                self._finish_stop(job)
            if job.get('resultId') and job.get('documents', {}).get('state') != 'ready':
                await self.reconcile_documents(job['id'])
        await self._catalog()
        if not self.admin_only and not self.settlement_unknown:
            eligible = [job['id'] for _, job in self.store.all() if job['state'] in TERMINAL and not job.get('resultId')]
            retained = {job['source']['snapshotId'] for _, job in self.store.all() if job.get('source')}
            await asyncio.to_thread(self._reclaim_orphans, eligible, retained)
        if not self.admin_only:
            self.scheduler = asyncio.create_task(self._schedule())

    def _read_predecessor(self):
        try:
            with open_regular(self.private, 'engine-lease.json') as handle:
                raw = handle.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError('Oversized lease')
            prior = json.loads(raw)
            if not isinstance(prior, dict) or not isinstance(prior.get('entries'), list):
                raise TypeError('Invalid lease')
            if prior['entries']:
                self.predecessor = prior
                self.internal_sessions = {item['sessionId'] for item in prior['entries'] if item.get('sessionId')}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.predecessor_invalid = True

    def _writer_dead(self, writer):
        if not isinstance(writer, dict):
            return False
        boot_id = writer.get('bootId')
        if isinstance(boot_id, str) and boot_id and boot_id != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
            return True
        pid, starttime = writer.get('pid'), writer.get('starttime')
        if type(pid) is not int or pid <= 0 or not isinstance(starttime, str) or not starttime:
            return False
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            return fields[0] == 'Z' or fields[19] != starttime
        except FileNotFoundError:
            return True
        except (OSError, IndexError):
            return False

    async def _predecessor_cleared(self):
        if self.predecessor_invalid:
            return False
        if self.predecessor is None:
            return True
        try:
            refresh = getattr(self.engine, 'refresh_descriptor', None)
            if refresh is not None:
                refresh()
            else:
                # Development can launch its private server here; readiness also
                # refreshes descriptors on adapters without the optional sync hook.
                await self.engine.ready()
            descriptor = self.engine.descriptor
            recovery = descriptor.get('recovery', {})
            prior = self.predecessor
            fenced = bool(prior.get('generation')) and prior['generation'] in recovery.get('fencedGenerations', [])
            dead = recovery.get('deadEngineInstances', [])
            engines = {prior.get('engineInstanceId'),
                       *(item.get('engineInstanceId', prior.get('engineInstanceId')) for item in prior['entries'])}
            replaced = bool(engines) and None not in engines and all(identity in dead for identity in engines)
            if fenced or (replaced and self._writer_dead(prior.get('writer'))):
                self.predecessor = None
                return True
        except Exception:  # noqa: BLE001 -- an unverified predecessor remains protected
            return False
        return False

    def _begin_lease(self):
        if not self.projection_started:
            self.store.metadata('generation', {'id': self.generation, 'startedAt': now(), 'pid': os.getpid()})
            self.projection_started = True
            self._persist_projection()
            self.heartbeat_task = asyncio.create_task(self._heartbeat())

    def _write_projection(self):
        if self.admin_only or not self.projection_started:
            return
        descriptor = self.engine.descriptor if self.engine is not None else {}
        self._check_lease()
        heartbeat = time.time()
        renewal_deadline = time.monotonic() + self.lease_seconds
        expires_at = heartbeat + self.lease_seconds
        if self.lease_expired:
            heartbeat, expires_at = self.lease_heartbeat, self.lease_expires_at
        # Linux stat's comm field may contain spaces or parentheses.
        starttime = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
        fields = ('id', 'jobId', 'generation', 'engineInstanceId', 'sessionId', 'inputId',
                  'sessionIds', 'state', 'cleanupState', 'requestHash', 'agent', 'model',
                  'location', 'dispatchIntent', 'admissionAcknowledged', 'mutationAttempted')
        entries = [{'dispatchIntent': False, 'admissionAcknowledged': False,
                    **{key: item[key] for key in fields if key in item}}
                   for item in self.store.iter_operations(only_unsettled=True)
                   if item.get('sessionId') or item.get('inputId')]
        self.internal_sessions = {session for item in entries
                                  for session in [item.get('sessionId'), *item.get('sessionIds', [])]
                                  if session}
        projection = {'schemaVersion': 1, 'scope': self.store.scope, 'generation': self.generation,
                      'engineInstanceId': descriptor.get('engineInstanceId'),
                      'directory': descriptor.get('directory'), 'heartbeat': heartbeat,
                      'expiresAt': expires_at,
                      'writer': {'pid': os.getpid(), 'starttime': starttime,
                                 'bootId': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}, 'entries': entries}
        # Only the coordinator writes this path; permissions are set before fsync/rename.
        path = self.private / 'engine-lease.json'
        temporary = path.with_name(path.name + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(projection, handle, ensure_ascii=False, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
        if not self.lease_expired:
            self.lease_deadline = renewal_deadline
            self.lease_heartbeat, self.lease_expires_at = heartbeat, expires_at

    def _check_lease(self):
        if not self.lease_expired and self.lease_deadline is not None and time.monotonic() >= self.lease_deadline:
            self.lease_expired = True
            self.settlement_unknown = True
            self.dependency.update(state='blocked', ready=False,
                                   reason='The writer lease expired; start a new supervised service generation.')
            self.store.metadata('generation', {'id': self.generation, 'state': 'expired', 'expiredAt': now()})

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self.lease_interval)
            try:
                async with self.lock:
                    self._write_projection()
            except OSError:
                self.settlement_unknown = True
                self.dependency.update(state='blocked', ready=False, reason='The ownership lease could not be persisted.')

    def _persist_projection(self):
        if self.admin_only or not self.projection_started:
            raise ServiceError('UNAVAILABLE', 'Engine ownership has not been admitted for this writer.', 503, 'check-status')
        try:
            self._write_projection()
        except OSError:
            self.settlement_unknown = True
            self.dependency.update(state='blocked', ready=False, reason='The ownership projection could not be persisted.')
            raise ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'Engine ownership could not be durably projected; admission is blocked.', 503, 'operator-action') from None
        self._check_lease()
        if self.lease_expired:
            raise ServiceError('RUNTIME_INTERRUPTED', 'The writer lease expired; a new supervised generation is required.', 503, 'operator-action')

    async def _ready(self):
        await self._refresh_observation()
        self._update_dependency()
        return self.capabilities()['dependency']['ready']

    async def _refresh_observation(self):
        # Concurrent readers share a bounded observation, not each other's cached
        # timestamps. Cancelling an HTTP reader cannot cancel the shared probe.
        if self._observation_task is None or self._observation_task.done():
            self._observation_task = asyncio.create_task(self._observe_engine())
        await asyncio.shield(self._observation_task)

    async def _observe_engine(self):
        self._check_lease()
        observed = {'ready': False, 'observedAt': None}
        try:
            if self.engine is not None and not self.admin_only and not self.lease_expired:
                async with asyncio.timeout(self.observation_seconds):
                    candidate = await self.engine.ready()
                    if isinstance(candidate, dict):
                        observed = copy.deepcopy(candidate)
                        warmup = getattr(self.engine, 'ready_catalog', None)
                        if (self.model_policy is not None and callable(warmup)
                                and observed.get('ready') is True and not observed.get('models')):
                            catalog = await warmup()
                            observed = copy.deepcopy(catalog) if isinstance(catalog, dict) else {'ready': False}
                        if self.model_policy is None and observed.get('ready') is True and 'models' in observed:
                            try:
                                validate_selection(self.profile, observed['models'])
                            except (ServiceError, TypeError, KeyError, AttributeError):
                                # A development launcher can finish asynchronous
                                # catalog startup here without coupling owner context
                                # readiness to an inference candidate.
                                explicit = await self.engine.ready(self.profile)
                                observed = copy.deepcopy(explicit) if isinstance(explicit, dict) else {'ready': False}
                                if observed.get('ready') is False and observed.get('executionReady') is True:
                                    # A checked model-only failure is admission-only.
                                    # Preserve its global context and actual catalog,
                                    # including negative compatibility observations.
                                    observed['ready'] = True
        except Exception:  # noqa: BLE001 -- dependency failures must not leak credentials
            observed.update(ready=False, reason='OpenCode observation failed; check authenticated engine context and ownership.')
        self._observation = observed
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(observed['observedAt'])).total_seconds()
            self._observed_monotonic = time.monotonic() - max(0, age) if age >= 0 else None
        except (ValueError, TypeError, KeyError):
            self._observed_monotonic = None
        self.wake.set()

    def _selection_error(self, profile):
        # Production engines expose the checked catalog in the global observation.
        # Alternate EnginePort adapters may validate their own explicit profiles.
        if 'models' not in self._observation:
            return None
        try:
            validate_selection(profile, self._observation['models'])
        except (ServiceError, TypeError, AttributeError, KeyError):
            return 'selected_model_unavailable'
        return None

    def _observation_fresh(self):
        return (self._observed_monotonic is not None
                and (time.monotonic() - self._observed_monotonic) * 1000 <= self.observation_max_age_ms)

    def _update_dependency(self):
        self._check_lease()
        observed = self._observation
        execution_ready = observed.get('ready') is True and self._observation_fresh() and not self.lease_expired
        reason = None
        selection = {}
        if self.model_policy is not None:
            selection = self.model_policy.select(self.profile, observed.get('models', []))
            reason = selection['selectionReason'] if selection['selectedModel'] is None else None
        else:
            reason = self._selection_error(self.profile)
        ready = execution_ready and reason is None
        self.dependency = {'configured': True, 'ready': ready,
                           'executionReady': execution_ready, 'state': 'ready' if ready else 'unavailable',
                           'observedAt': observed.get('observedAt'), 'observationMaxAgeMs': self.observation_max_age_ms,
                           **selection}
        for key in ('compatibility', 'version', 'mode', 'engineInstanceId'):
            if key in observed:
                self.dependency[key] = copy.deepcopy(observed[key])
        if self.engine is None or self.admin_only:
            self.dependency.update(configured=self.engine is not None, ready=False, executionReady=False,
                                   state='unconfigured' if self.engine is None else 'admin-only')
        if not ready:
            self.dependency['reason'] = reason if execution_ready else 'OpenCode context observation failed or is stale; check authenticated engine identity, extraction context and ownership.'

    async def refresh_capabilities(self):
        """On-demand bounded engine observation for diagnostic API readers."""
        await self._ready()
        await self._update_job_holds()
        return self.capabilities()

    async def reconcile_engine(self):
        """Reconcile durable ownership without replay, then recheck admission readiness."""
        if self.admin_only:
            return {'settled': False, 'operations': [], 'reason': 'Engine reconciliation is unavailable in administrative mode.'}
        async with self.recovery_lock:
            async with self.lock:
                if self.stopping or self.active is not None or self.stop_tasks:
                    return {'settled': False, 'operations': [], 'reason': 'Extraction or local cleanup is active.'}
                self.settlement_unknown = True
            if not await self._predecessor_cleared():
                self.dependency.update(state='blocked', ready=False,
                                       reason='The server owner must confirm predecessor fencing or old-engine and writer death.')
                return self._recovery_result(False, [], False)
            try:
                self._begin_lease()
            except ServiceError:
                return self._recovery_result(False, [], False)
            operations = []
            recovered = False
            # Preparing has no HTTP candidate yet: callback success is required
            # before create, so this state can be settled without contacting OpenCode.
            for item in self.store.iter_operations(only_unsettled=True):
                if item.get('state') == 'preparing' and not item.get('sessionId') and not item.get('inputId'):
                    await self._record_operation(item['id'], item['jobId'], {
                        'state': 'failed', 'settlement': {'settled': True, 'remoteOutcome': 'not-dispatched'}}, recovering=True)
            pending = any(self.store.iter_operations(only_unsettled=True))
            recovered = not pending
            if pending and self.engine is not None:
                try:
                    report = await self.engine.recover(self.store.iter_operations(only_unsettled=True))
                    if not isinstance(report, dict) or not isinstance(report.get('operations'), list):
                        raise TypeError('Invalid recovery report')
                    for patch in report['operations']:
                        if not isinstance(patch, dict):
                            raise TypeError('Invalid recovery patch')
                        await self._record_operation(patch['id'], patch['jobId'], patch, recovering=True)
                        operations.append(self.store.get_operation(patch['id']))
                    recovered = report.get('settled') is True and not any(self.store.iter_operations(only_unsettled=True))
                except Exception:  # noqa: BLE001 -- a recovery failure keeps status available
                    recovered = False
            await self._ready()
            ready = self.dependency['executionReady']
            async with self.lock:
                self.settlement_unknown = not recovered or self.lease_expired
                if recovered:
                    for _, job in self.store.all():
                        if job['state'] == 'cancelling' and not self._needs_settlement(job['id']):
                            self._finish_stop(job)
                if not recovered and self.engine is not None:
                    self.dependency.update(state='blocked', ready=False, reason='Owned engine work has not been confirmed settled; reconcile engine ownership.')
                try:
                    self._persist_projection()
                except ServiceError:
                    recovered = False
                self.wake.set()
            return self._recovery_result(recovered, operations, ready)

    def _recovery_result(self, recovered, operations, ready):
        result = {'settled': recovered and ready, 'operations': operations}
        if not result['settled']:
            result['reason'] = self.dependency.get('reason', 'Engine processing is not ready.')
        self.last_recovery = {'settled': recovered, 'ready': ready, 'checkedAt': now(),
                              'operationCount': len(operations),
                              **({'reason': result['reason']} if 'reason' in result else {})}
        return result

    def _reclaim_orphans(self, eligible, retained):
        cutoff = time.time() - self.profile['limits'].get('orphanGraceSeconds', 86400)
        for job_id in eligible:
            for name in ('staging', 'sealed'):
                path = self.private / 'jobs' / job_id / name
                if path.exists() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path)
        for path in (self.private / 'sources').iterdir():
            if path.name not in retained and not path.is_symlink() and path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)

    def event(self, name, **data):
        try:
            print(encode({'event': name, 'timestamp': now(), 'generation': self.generation, **data}), file=sys.stderr, flush=True)
        except (OSError, ValueError, TypeError):
            self.dropped_events += 1

    def get(self, job_id):
        return self._job_status(self.store.get(job_id))

    def _job_status(self, job):
        if job is not None:
            job.setdefault('progress', None)
            job.setdefault('interruptedAt', None)
            job.setdefault('cancelRequestedAt', None)
            job['queueAhead'] = self.store.queue_ahead(job['id']) if job['state'] == 'queued' else None
        return job

    def lookup(self, key):
        return self._job_status(self.store.lookup(key.lower())[0])

    async def _record_progress(self, job_id, message):
        """Persist worker observations without granting execution or settlement authority."""
        async with self.lock:
            job = self.get(job_id)
            if job['state'] != 'running':
                return
            invalid = ServiceError('INVALID_PROGRESS', 'Parser progress is invalid; check its stage and nondecreasing integer counts.', retry='operator-action')
            if not isinstance(message, dict) or set(message) != {'id', 'completedUnits', 'totalUnits'}:
                raise invalid
            stage_id, completed, total = message['id'], message['completedUnits'], message['totalUnits']
            if (not isinstance(stage_id, str) or stage_id not in STAGE_LABELS
                    or type(completed) is not int or completed < 0
                    or (total is not None and (type(total) is not int or total < completed))):
                raise invalid
            previous = job.get('stage')
            if previous is not None:
                order = list(STAGE_LABELS)
                if order.index(stage_id) < order.index(previous['id']):
                    raise invalid
                if stage_id == previous['id'] and (completed < previous['completedUnits']
                        or (previous['totalUnits'] is not None and total != previous['totalUnits'])):
                    raise invalid
            pdf = Path((job.get('source') or {}).get('filename', job['requestedSource'].get('path', ''))).suffix.lower() == '.pdf'
            stage = current_stage(stage_id, completed, total, pdf=pdf)
            if pdf and stage_id == 'extract':
                progress = job.get('progress')
                if progress is not None and (completed < progress['completedUnits']
                        or (progress['totalUnits'] is not None and total != progress['totalUnits'])):
                    raise invalid
                job['progress'] = {**stage, 'updatedAt': now()}
            job['stage'] = stage
            self.store.save(job)

    async def submit(self, key, body):
        try:
            if str(uuid.UUID(key)) != key.lower():
                raise ValueError
            key = str(uuid.UUID(key))
        except (ValueError, TypeError, AttributeError):
            raise ServiceError('INVALID_SUBMISSION_KEY', 'A UUID submission key is required.') from None
        requested = canonical(body)
        request = encode(requested)
        previous, saved = self.store.lookup(key)
        if previous:
            if saved != request:
                raise ServiceError('IDEMPOTENCY_CONFLICT', 'Submission key already has a different request.', 409)
            return self._job_status(previous), True
        if self.stopping or self.admin_only or self.settlement_unknown:
            raise ServiceError('UNAVAILABLE', 'Processing admission is blocked; check dependency status and reconcile engine ownership.', 503, 'check-status')
        # Observe outside the insertion lock; duplicate and trusted selection reads
        # are rechecked inside it. No inference or network request occurs there.
        await self._ready()
        resume_parent, resume_digest = None, None
        if requested['resumeOf']:
            preview = effective_profile(self.profile, requested['tableRecovery'], code='INVALID_REQUEST')
            if self.model_policy is not None:
                preview, _selection = self.model_policy.resolve(preview, self._observation.get('models', []))
                if preview is not None:
                    preview = effective_profile(preview, requested['tableRecovery'], code='INVALID_REQUEST')
            if preview is not None:
                resume_parent = self._resume_parent(requested['resumeOf'], requested['source'])
                # Retained PDF hashing can be substantial. Keep it outside the
                # event loop and writer lock, then recheck identity at insertion.
                await asyncio.to_thread(validate_parent, resume_parent, preview,
                    sealed_root=self.private / 'jobs' / resume_parent['id'] / 'sealed',
                    snapshot_root=self.private / 'sources' / resume_parent['source']['snapshotId'])
                resume_digest = preview['profileDigest']
        async with self.lock:
            previous, saved = self.store.lookup(key)
            if previous:
                if saved != request:
                    raise ServiceError('IDEMPOTENCY_CONFLICT', 'Submission key already has a different request.', 409)
                return self._job_status(previous), True
            if self.stopping or self.admin_only or self.settlement_unknown:
                raise ServiceError('UNAVAILABLE', 'Processing admission is blocked; check dependency status and reconcile engine ownership.', 503, 'check-status')
            self._update_dependency()
            if not self.capabilities()['dependency']['executionReady']:
                raise ServiceError('PROVIDER_UNAVAILABLE', 'OpenCode extraction context is unavailable; check engine ownership.', 503, 'check-status')
            profile = effective_profile(self.profile, requested['tableRecovery'], code='INVALID_REQUEST')
            selection = None
            if self.model_policy is not None:
                profile, selection = self.model_policy.resolve(profile, self._observation.get('models', []))
                if profile is None:
                    raise ServiceError('MODEL_POLICY_UNAVAILABLE', 'New model selection is blocked: ' + selection['selectionReason'] + '.', 503, 'check-status')
                profile = effective_profile(profile, requested['tableRecovery'], code='INVALID_REQUEST')
            if self._selection_error(profile):
                raise ServiceError('PROVIDER_UNAVAILABLE', 'The explicit extraction model is unavailable.', 503, 'check-status')
            if requested['profileId'] not in {'default', self.profile['id']}:
                raise ServiceError('UNKNOWN_PROFILE', 'Profile is not configured.', 422)
            if requested['source']['kind'] == 'upload':
                raise ServiceError('UNSUPPORTED_OPTION', 'Accepted-upload receipts require runtime upload-owner integration.', 422)
            if self.store.active_count() >= self.profile['limits']['queueDepth']:
                raise ServiceError('QUEUE_FULL', 'The processing queue is full.', 429, 'check-status')
            if requested['rerunOf']:
                self.get(requested['rerunOf'])
            if requested['resumeOf']:
                current_parent = self._resume_parent(requested['resumeOf'], requested['source'])
                if (resume_parent is None or current_parent['revision'] != resume_parent['revision']
                        or profile['profileDigest'] != resume_digest):
                    raise ServiceError('RESUME_INVALID', 'Parent or selected profile changed during resume validation; submit again.')
            if shutil.disk_usage(self.root).free < self.profile['limits']['inputBytes'] + 2 * self.profile['limits']['outputBytes']:
                raise ServiceError('RESOURCE_LIMIT', 'Insufficient free space for a snapshot and two result copies.', 503, 'operator-action')
            job = {'schemaVersion': 1, 'id': 'job_' + uuid.uuid4().hex, 'projectId': self.store.scope,
                   'submissionKey': key, 'correlation': requested['correlation'], 'revision': 1, 'state': 'queued',
                    'createdAt': now(), 'startedAt': None, 'finishedAt': None, 'interruptedAt': None,
                    'cancelRequestedAt': None, 'requestedSource': requested['source'],
                   'source': None, 'profile': profile, 'rerunOf': requested['rerunOf'],
                   'resumeOf': requested['resumeOf'], 'stage': None, 'progress': None,
                   'stages': [], 'resultUrl': None, 'documents': None, 'usage': None, 'usageStatus': 'unavailable', 'error': None}
            if selection is not None:
                job['modelSelection'] = selection
            self.store.insert(job, request)
            self.wake.set()
            self.event('job.state_changed', jobId=job['id'], state='queued')
            return self.get(job['id']), False

    def list_jobs(self, **query):
        allowed = {'submissionKey', 'cursor', 'limit', 'state', 'snapshotId', 'uploadBatchId'}
        if set(query) - allowed:
            raise ServiceError('INVALID_QUERY', 'Unknown list filter.')
        if query.get('submissionKey') is not None:
            if any(value is not None for key, value in query.items() if key != 'submissionKey'):
                raise ServiceError('INVALID_QUERY', 'Submission lookup cannot be combined with list filters.')
            return {'job': self.lookup(query['submissionKey'])}
        try:
            limit = int(query.get('limit') or 50)
            if not 1 <= limit <= 200:
                raise ValueError
            ceiling, before = (2**63 - 1, 2**63 - 1)
            if query.get('cursor'):
                cursor = json.loads(base64.urlsafe_b64decode(query['cursor']))
                ceiling, before = cursor['ceiling'], cursor['before']
                if cursor['scope'] != self.store.scope:
                    raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ServiceError('INVALID_QUERY', 'Invalid pagination parameters.') from None
        if not query.get('cursor'):
            ceiling = self.store.max_sequence()
        selected = self.store.page(ceiling, before, limit + 1, state=query.get('state'),
                                   snapshot_id=query.get('snapshotId'), upload_batch_id=query.get('uploadBatchId'))
        page = selected[:limit]
        cursor = base64.urlsafe_b64encode(encode({'scope': self.store.scope, 'ceiling': ceiling, 'before': page[-1][0]}).encode()).decode() if len(selected) > limit else None
        return {'jobs': [self._job_status(job) for _, job in page], 'nextCursor': cursor}

    def _resume_parent(self, parent_id, source):
        parent = self.get(parent_id)
        if parent['state'] not in TERMINAL or not parent.get('resultId') or not parent.get('source'):
            raise ServiceError('INVALID_REQUEST', 'Resume requires a terminal job with a sealed PDF result.')
        if (source != {'kind': 'snapshot', 'snapshotId': parent['source']['snapshotId']}
                or Path(parent['source']['filename']).suffix.lower() != '.pdf'):
            raise ServiceError('INVALID_REQUEST', 'Resume must use the parent PDF retained snapshot.')
        if self._needs_settlement(parent_id):
            raise ServiceError('RESUME_INVALID', 'The parent still owns unsettled model work.')
        return parent

    def _work_remaining(self, job_id):
        deadline = self.work_deadlines.get(job_id)
        return float('inf') if deadline is None else max(0, deadline - time.monotonic())

    def _finalize(self, job, extraction, staging, resume):
        if resume is not None:
            # Check retained hashes before manifest creation recalculates them.
            validate_resume_output(extraction, staging, resume)
        return make_manifest(job, extraction, staging)

    def _dispatch_allowance(self):
        return self.engine.dispatch_allowance_seconds if self.engine is not None else 0

    def _can_dispatch(self, job_id):
        if job_id not in self.work_deadlines:
            return True
        finish = self.engine.finish_allowance_seconds if self.engine is not None else 0
        hard_remaining = self.hard_deadlines.get(job_id, float('inf')) - time.monotonic()
        return (self._work_remaining(job_id) > self._dispatch_allowance()
                and hard_remaining > self._dispatch_allowance() + finish)

    async def _schedule(self):
        while not self.stopping:
            if self.settlement_unknown:
                self.wake.clear()
                await self.wake.wait()
                continue
            try:
                queued = await self._next_eligible_queued()
            except Exception:  # noqa: BLE001 -- failed hold persistence must never dispatch or kill scheduling
                self._scheduler_error = 'Queued eligibility evidence could not be persisted; scheduling will retry.'
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), self.lease_interval)
                except TimeoutError:
                    pass
                continue
            if queued is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), self.lease_interval)
                except TimeoutError:
                    pass
                continue
            if self.settlement_unknown or self.stopping:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), self.lease_interval)
                except TimeoutError:
                    pass
                continue
            async with self.lock:
                # Readiness can yield to stop/recovery or a cancellation of this
                # exact queued job. Claim only after rechecking the durable state.
                if self.stopping or self.settlement_unknown or self.get(queued['id'])['state'] != 'queued':
                    continue
                self.active = queued['id']
            try:
                async with asyncio.timeout(queued['profile']['limits']['jobSeconds']):
                    await self._run(self.active)
            except Exception as exc:  # noqa: BLE001 -- job boundary sanitizes parser failures
                async with self.cleanup_lock:
                    await self._cleanup_active(self.active)
                    for operation in self.store.operations(self.active):
                        if unsettled(operation):
                            try:
                                await self._record_operation(operation['id'], self.active,
                                                             {'state': 'outcome_unknown', 'finishedAt': now()})
                            except ServiceError:
                                self.settlement_unknown = True
                    async with self.lock:
                        job = self.get(self.active)
                        if job['state'] not in TERMINAL and job['state'] != 'cancelling':
                            error = exc if isinstance(exc, ServiceError) else ServiceError('RESOURCE_LIMIT' if isinstance(exc, TimeoutError) else 'EXTRACTION_FAILED', 'Processing could not finish within its configured limits.' if isinstance(exc, TimeoutError) else 'Processing failed.', retry='new-job')
                            job.update(state='cancelling' if self.settlement_unknown else 'failed', finishedAt=None if self.settlement_unknown else now(), stage=None, error=error.as_dict(job['id']))
                            self.store.save(job)
            finally:
                # Pin local task/process ownership until stop has crossed every
                # barrier; a late cleanup must never stop the next FIFO worker.
                stopping = self.stop_tasks.get(self.active)
                if stopping:
                    await asyncio.shield(stopping)
                async with self.lock:
                    job = self.get(self.active)
                    if (job['state'] == 'cancelling' and not self.settlement_unknown
                            and not self._needs_settlement(self.active)):
                        self._finish_stop(job)
                self.work_deadlines.pop(self.active, None)
                self.hard_deadlines.pop(self.active, None)
                self.active = None

    def _job_hold(self, job):
        reason = self._selection_error(job['profile'])
        if reason is None and self.model_policy is not None:
            reason = self.model_policy.pinned_hold(job['profile'], self._observation.get('models', []))
        return reason

    async def _update_job_holds(self):
        try:
            async with self.lock:
                # Finish the queue read before persisting diagnostic hold updates.
                queued = list(self.store.queued()) if self.store else ()
                for job in queued:
                    reason = self._job_hold(job)
                    hold = {'reason': reason, 'selectedModel': job['profile']['models'].get('vision')} if reason else None
                    if job.get('hold') != hold:
                        job['hold'] = hold
                        self.store.save(job)
        except Exception:  # Every hold-write caller must retain the admission block.
            self._scheduler_error = 'Queued eligibility evidence could not be persisted; scheduling will retry.'
            raise
        self._scheduler_error = None

    async def _next_eligible_queued(self):
        if not self.store.state_count('queued'):
            self._scheduler_error = None
            return None
        await self._ready()
        await self._update_job_holds()
        self._scheduler_error = None
        if not self.capabilities()['dependency']['executionReady']:
            return None
        # Store order is admission order, and holds never dequeue or rewrite profiles.
        return next((job for job in self.store.queued() if not job.get('hold')), None)

    def _snapshot(self, job, retained=None):
        request = job['requestedSource']
        if request['kind'] == 'snapshot':
            if retained is None:
                raise ServiceError('SOURCE_NOT_FOUND', 'Snapshot not found.')
            with open_regular(self.private / 'sources' / retained['snapshotId'], 'source') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != retained['sha256']:
                    raise ServiceError('SOURCE_CHANGED', 'Retained snapshot no longer matches its hash.')
            return retained
        relative = request['path']
        # Private state is never a source selectable through file APIs.
        source_path = self.workspace / relative
        if source_path.is_relative_to(self.private) or source_path.is_relative_to(self.root / '.document-publication'):
            raise ServiceError('SOURCE_READ_FAILED', 'Private processing files cannot be submitted.')
        snapshot_id = 'source_' + uuid.uuid4().hex
        target = self.private / 'sources' / snapshot_id
        target.mkdir()
        copied = False
        try:
            with open_regular(self.workspace, relative) as source, (target / 'source').open('xb') as output:
                before = os.fstat(source.fileno())
                total = 0
                sha = hashlib.sha256()
                while block := source.read(1024 * 1024):
                    total += len(block)
                    if total > job['profile']['limits']['inputBytes']:
                        raise ServiceError('RESOURCE_LIMIT', 'Source exceeds the input-byte limit.')
                    output.write(block)
                    sha.update(block)
                after = os.fstat(source.fileno())
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ServiceError('SOURCE_CHANGED', 'Source changed during snapshot acquisition.')
                output.flush()
                os.fsync(output.fileno())
            if request.get('expectedSha256') and request['expectedSha256'] != sha.hexdigest():
                raise ServiceError('SOURCE_CHANGED', 'Source differs from the expected SHA-256.')
            fsync_directory(target)
            fsync_directory(target.parent)
            copied = True
            return {'snapshotId': snapshot_id, 'sha256': sha.hexdigest(), 'bytes': total,
                    'filename': Path(relative).name, 'origin': request, 'consistency': 'copied-bytes'}
        except FileNotFoundError:
            raise ServiceError('SOURCE_NOT_FOUND', 'Source file was not found.') from None
        except OSError as exc:
            code = 'RESOURCE_LIMIT' if exc.errno in {errno.ENOSPC, errno.EDQUOT, errno.EFBIG} else 'SOURCE_READ_FAILED'
            raise ServiceError(code, 'Source snapshot could not be acquired.') from None
        finally:
            if not copied:
                shutil.rmtree(target, ignore_errors=True)

    async def _run(self, job_id):
        job = self.get(job_id)
        if job['state'] != 'queued':
            return
        self.work_deadlines[job_id] = time.monotonic() + job['profile']['options']['tableRecovery']['workSeconds']
        self.hard_deadlines[job_id] = time.monotonic() + job['profile']['limits']['jobSeconds']
        job.update(state='running', startedAt=now(), stage=current_stage('snapshot', 0, 1))
        self.store.save(job)
        self.event('job.state_changed', jobId=job_id, state='running')
        retained = None
        if job['requestedSource']['kind'] == 'snapshot':
            retained = self.store.snapshot(job['requestedSource']['snapshotId'])
        self.snapshot_task = asyncio.create_task(asyncio.to_thread(self._snapshot, job, retained))
        try:
            snapshot = await asyncio.shield(self.snapshot_task)
        finally:
            # A running copy is local work too. Do not report cancellation or
            # release ownership while the copy thread can still write bytes.
            await asyncio.gather(self.snapshot_task, return_exceptions=True)
            self.snapshot_task = None
        job = self.get(job_id)
        if job['state'] != 'running':
            return
        pdf = Path(snapshot['filename']).suffix.lower() == '.pdf'
        job.update(source=snapshot, stage=current_stage('extract', 0, None, pdf=pdf))
        if pdf:
            job['progress'] = {**job['stage'], 'updatedAt': now()}
        if Path(snapshot['filename']).suffix.lower() != '.pdf':
            self.work_deadlines.pop(job_id, None)
        self.store.save(job)  # exact bytes/config commit precedes worker/provider work
        self.event('stage.finished', jobId=job_id, stage='snapshot', completedUnits=1)
        directory = self.private / 'jobs' / job_id
        staging = directory / 'staging'
        staging.mkdir(parents=True)
        resume = None
        if job.get('resumeOf'):
            parent = self._resume_parent(job['resumeOf'], job['requestedSource'])
            validated = await asyncio.to_thread(validate_parent, parent, job['profile'],
                                               sealed_root=self.private / 'jobs' / parent['id'] / 'sealed',
                                               snapshot_root=self.private / 'sources' / parent['source']['snapshotId'])
            self.finalize_task = asyncio.create_task(asyncio.to_thread(prepare_resume, validated, staging, profile=job['profile']))
            try:
                resume = await asyncio.shield(self.finalize_task)
            finally:
                await asyncio.gather(self.finalize_task, return_exceptions=True)
                self.finalize_task = None
            atomic_json(directory / 'resume.json', resume)
        environment = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PREPROCESSING_PARENT_PID': str(os.getpid()),
                       'PYTHONPATH': str(Path(__file__).parent.parent)}
        async with self.lock:
            if self.get(job_id)['state'] != 'running':
                return
            self.process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'preprocessing.worker',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=environment, start_new_session=True, pass_fds=(self.owner.fileno(),),
                limit=max(job['profile']['limits']['requestBytes'], job['profile']['limits']['responseBytes']) * 8 + 65536)
        request = {'snapshot': {**snapshot, 'path': str(self.private / 'sources' / snapshot['snapshotId'] / 'source')},
                   'profile': job['profile'], 'output': str(staging),
                   'workDeadline': self.work_deadlines.get(job_id),
                   'resumePath': str(directory / 'resume.json') if resume is not None else None}
        process = self.process
        process.stdin.write((encode(request) + '\n').encode())
        await process.stdin.drain()
        completed = False
        while line := await process.stdout.readline():
            message = json.loads(line)
            if message['type'] == 'model':
                reply = await self._model(job_id, staging, message)
                if process.returncode is None:
                    process.stdin.write((encode(reply) + '\n').encode())
                    await process.stdin.drain()
            elif message['type'] == 'error':
                error = message['error']
                raise ServiceError(error['code'], error['message'])
            elif message['type'] == 'complete':
                completed = True
            elif message['type'] == 'progress':
                await self._record_progress(job_id, message.get('stage'))
            elif message['type'] == 'event' and isinstance(message.get('name'), str) and message['name'].startswith('table.'):
                data = {key: value for key, value in message.get('data', {}).items()
                        if key in RECOVERY_EVENT_FIELDS and type(value) in {str, int, float, bool, type(None)}}
                self.event(message['name'], jobId=job_id, **data)
        await process.wait()
        self.process = None
        job = self.get(job_id)
        if job['state'] != 'running':
            return
        if process.returncode or not completed:
            raise ServiceError('EXTRACTION_FAILED', 'Parser exited without a complete result.')
        if self._needs_settlement(job_id):
            raise ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'Unsettled model ownership prevents sealing.', 503, 'check-status')
        extraction = json.loads((staging / 'extraction.json').read_text())
        for stage in extraction['stages']:
            self.event('stage.finished', jobId=job_id, stage=stage['id'], state=stage['state'], coverage=stage.get('coverage'))
        (staging / 'extraction.json').unlink()
        await self._record_progress(job_id, {'id': 'seal', 'completedUnits': 0, 'totalUnits': 1})
        job = self.get(job_id)
        if job['state'] != 'running':
            return
        self.finalize_task = asyncio.create_task(asyncio.to_thread(self._finalize, job, extraction, staging, resume))
        try:
            manifest = await asyncio.shield(self.finalize_task)
        finally:
            await asyncio.gather(self.finalize_task, return_exceptions=True)
            self.finalize_task = None
        if self.get(job_id)['state'] != 'running':
            return
        sealed = directory / 'sealed'
        os.rename(staging, sealed)
        fsync_directory(directory)
        async with self.lock:
            job = self.get(job_id)
            if job['state'] != 'running':
                return
            job.update(state=manifest['outcome'], finishedAt=now(), stages=manifest['stages'], stage=None, coverage=manifest['coverage'],
                       resultId=manifest['resultId'], resultUrl=f'/preprocessing/jobs/{job_id}/results',
                       documents={'state': 'pending', 'path': None})
            job['sealedManifestSha256'] = digest_file(sealed / 'manifest.json')
            self.store.save(job)
        await self.reconcile_documents(job_id)
        self.event('job.state_changed', jobId=job_id, state=job['state'])

    async def _model(self, job_id, staging, message):
        if not self._can_dispatch(job_id):
            self.event('work.budget_exhausted', jobId=job_id, kind=message['operation'].get('kind'))
            return {'error': ServiceError('WORK_BUDGET_EXHAUSTED', 'The PDF soft work budget cannot admit another model operation.').as_dict()}
        operation = {'id': 'operation_' + uuid.uuid4().hex, 'jobId': job_id, 'generation': self.generation,
                     'state': 'preparing', 'settlement': {'settled': False},
                     'dispatchIntent': False, 'admissionAcknowledged': False,
                     'createdAt': now(), 'kind': message['operation'].get('kind'),
                     'sourceUnit': message['operation'].get('sourceUnit')}
        refs = []
        async with self.lock:
            job = self.get(job_id)
            if job['state'] != 'running':
                return {'error': ServiceError('RUNTIME_INTERRUPTED', 'Job is no longer running.').as_dict()}
            for ref in message['inputRefs']:
                path = Path(ref['path'])
                relative = path.relative_to(staging).as_posix()
                with open_regular(staging, relative) as handle:
                    before = os.fstat(handle.fileno())
                    digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                    after = os.fstat(handle.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise ServiceError('SOURCE_CHANGED', 'Model input changed during hashing.')
                    refs.append({**ref, 'bytes': before.st_size, 'sha256': digest})
            remaining = self._work_remaining(job_id)
            if not self._can_dispatch(job_id):
                return {'error': ServiceError('WORK_BUDGET_EXHAUSTED', 'The PDF soft work budget expired before dispatch.').as_dict()}
            dispatch = {key: value for key, value in message['operation'].items() if key != 'completionSeconds'}
            if job_id in self.work_deadlines:
                dispatch['completionSeconds'] = min(job['profile']['limits']['modelSeconds'], remaining)
                operation['completionSeconds'] = dispatch['completionSeconds']
            self.store.operation(operation)
            self._persist_projection()
        try:
            if self.engine is None:
                raise ServiceError('PROVIDER_UNAVAILABLE', 'No model engine is configured.', retry='operator-action')
            self.engine_operations.add(operation['id'])
            result = await self.engine.start(operation['id'], job_id, self.generation, job['profile'], refs, dispatch)
            operation = self.store.get_operation(operation['id'])
            patch = {key: result[key] for key in ('sessionId', 'requestedModel', 'returnedModel', 'usage', 'attempts')
                     if key in result and result[key] is not None}
            patch['finishedAt'] = now()
            if operation['state'] in {'preparing', 'prepared', 'admitted', 'dispatch_intent'}:
                patch['state'] = 'completed'
            await self._record_operation(operation['id'], job_id, patch)
            operation = self.store.get_operation(operation['id'])
            if unsettled(operation):
                raise ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'Local engine cleanup remains pending.', 503, 'check-status')
            self.event('operation.settled', jobId=job_id, operationId=operation['id'], state=operation['state'], sessionId=operation.get('sessionId'))
            current = self.get(job_id)
            if result.get('usage') is not None:
                current['usage'] = {'observations': [{'operationId': item['id'], 'usage': item['usage']} for item in self.store.operations(job_id) if item.get('usage') is not None]}
                current['usageStatus'] = 'incomplete'
                self.store.save(current)
            if self._work_remaining(job_id) <= 0:
                return {'error': ServiceError('WORK_BUDGET_EXHAUSTED', 'The model operation settled after the PDF work cutoff.').as_dict(operation['id'])}
            return {'result': result}
        except ServiceError as exc:
            operation = self.store.get_operation(operation['id'])
            patch = {'finishedAt': now(), 'error': exc.as_dict(operation['id'])}
            if operation['state'] not in {'completed', 'evidence', 'settled', 'cleanup_pending'}:
                patch['state'] = 'outcome_unknown' if exc.code == 'PROVIDER_OUTCOME_UNKNOWN' else 'failed'
            for key, attribute in (('sessionId', 'session_id'), ('settlement', 'settlement'), ('evidence', 'evidence')):
                value = getattr(exc, attribute, None)
                if value is not None and (key != 'evidence' or not operation.get('evidence')):
                    patch[key] = value
            # Failures before a candidate session is allocated cannot leave server work.
            if not operation.get('sessionId') and exc.code != 'PROVIDER_OUTCOME_UNKNOWN':
                patch.setdefault('settlement', {'settled': True})
            await self._record_operation(operation['id'], job_id, patch)
            operation = self.store.get_operation(operation['id'])
            self.event('operation.settled', jobId=job_id, operationId=operation['id'], state=operation['state'], code=exc.code)
            if unsettled(operation):
                self.settlement_unknown = True
                current = self.get(job_id)
                current.update(state='cancelling', error=ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'Local engine settlement is pending.', retry='operator-action').as_dict(job_id))
                self.store.save(current)
                raise
            if exc.code == 'WORK_BUDGET_EXHAUSTED':
                self.event('work.budget_exhausted', jobId=job_id, operationId=operation['id'])
            return {'error': exc.as_dict(operation['id'])}

    async def _record_operation(self, operation_id, job_id, patch, *, recovering=False):
        """Commit a binding/evidence patch and its cleanup projection before returning."""
        async with self.lock:
            if self.admin_only or not self.projection_started:
                raise ServiceError('UNAVAILABLE', 'This writer is not admitted to update engine ownership.', 503, 'check-status')
            operation = self.store.get_operation(operation_id)
            if operation['jobId'] != job_id or patch.get('id', operation_id) != operation_id or patch.get('jobId', job_id) != job_id:
                raise ServiceError('INVALID_OPERATION', 'Engine operation binding does not match the ledger.')
            for key, value in patch.items():
                if key in {'settlement', 'evidence'} and isinstance(value, dict) and isinstance(operation.get(key), dict):
                    operation[key] = {**operation[key], **value}
                else:
                    operation[key] = value
            if patch.get('state') == 'dispatch_intent':
                operation['dispatchIntent'] = True
            if patch.get('state') == 'admitted':
                operation['admissionAcknowledged'] = True
            self.store.operation(operation)
            if not unsettled(operation):
                self.engine_operations.discard(operation_id)
            self._persist_projection()
            if (not recovering and patch.get('state') in {'prepared', 'dispatch_intent'}
                    and (self.stopping or self.get(job_id)['state'] != 'running')):
                raise ServiceError('RUNTIME_INTERRUPTED', 'Job stopped before model dispatch.')
            poll_allowance = self.engine.dispatch_poll_allowance_seconds if (
                self.engine is not None and patch.get('state') == 'dispatch_intent') else 0
            if (not recovering and patch.get('state') in {'prepared', 'dispatch_intent'}
                    and self._work_remaining(job_id) <= poll_allowance):
                raise ServiceError('WORK_BUDGET_EXHAUSTED', 'The PDF soft work budget expired before model dispatch.')

    async def _stop_worker(self):
        process = self.process
        if process and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await asyncio.wait_for(process.wait(), 2)
            except ProcessLookupError:
                await process.wait()
        self.process = None

    def _needs_settlement(self, job_id):
        return any(unsettled(item) for item in self.store.operations(job_id))

    def _never_started(self, job):
        return (job['state'] == 'queued' and job['startedAt'] is None
                and not job.get('cancelRequestedAt') and not self._needs_settlement(job['id']))

    def _finish_stop(self, job):
        """Choose the durable stop outcome without replacing a committed result."""
        if job['state'] in TERMINAL or self._needs_settlement(job['id']):
            return
        if job.get('cancelRequestedAt'):
            job.update(state='cancelled', error=None)
        else:
            job.update(state='interrupted', interruptedAt=job.get('interruptedAt') or now(),
                       error=ServiceError('RUNTIME_INTERRUPTED', 'Interrupted extraction requires an explicit rerun.', retry='new-job').as_dict(job['id']))
        job.update(finishedAt=now(), stage=None)
        self.store.save(job)

    async def _apply_settlement(self, job_id, report):
        for patch in report.get('operations', []):
            await self._record_operation(patch['id'], job_id, patch, recovering=True)
        if report.get('settled') is True:
            for item in self.store.operations(job_id):
                if unsettled(item) and item['id'] in self.engine_operations:
                    patch = {'settlement': {'settled': True}, 'finishedAt': now()}
                    if item['state'] not in {'completed', 'cleanup_pending'}:
                        patch['state'] = 'outcome_unknown'
                    await self._record_operation(item['id'], job_id, patch, recovering=True)

    async def cancel(self, job_id):
        return await self._request_stop(job_id, explicit=True)

    async def _request_stop(self, job_id, *, explicit):
        async with self.lock:
            if self.cleanup_closed:
                raise ServiceError('UNAVAILABLE', 'The processing owner has closed.', 503, 'check-status')
            job = self.get(job_id)
            needs_settlement = self._needs_settlement(job_id)
            if job['state'] in TERMINAL and not needs_settlement:
                return job
            # Commit user intent before waiting for any local/provider cleanup.
            # Shutdown can overlap this request but never creates that intent.
            if explicit:
                job['cancelRequestedAt'] = job.get('cancelRequestedAt') or now()
            elif job['state'] not in TERMINAL and not job.get('cancelRequestedAt'):
                job['interruptedAt'] = job.get('interruptedAt') or now()
            if not job.get('resultId'):
                job.update(state='cancelling', finishedAt=None, stage=None)
            self.store.save(job)
            if needs_settlement:
                self.settlement_unknown = True
            task = self.stop_tasks.get(job_id)
            if task is None or task.done():
                task = asyncio.create_task(self._stop_job(job_id))
                self.stop_tasks[job_id] = task
                task.add_done_callback(lambda done: self._forget_stop(job_id, done))
        # A disconnected caller cannot abandon the durable stop it requested.
        return await asyncio.shield(task)

    def _forget_stop(self, job_id, task):
        if self.stop_tasks.get(job_id) is task:
            del self.stop_tasks[job_id]

    async def _cleanup_active(self, job_id):
        """Called under cleanup_lock, shared with scheduler failure cleanup."""
        needs_settlement = self._needs_settlement(job_id)
        confirmed = not needs_settlement
        try:
            await self._stop_worker()
            if self.engine and needs_settlement:
                report = await self.engine.cancel(job_id)
                await self._apply_settlement(job_id, report)
                confirmed = report.get('settled') is True and not self._needs_settlement(job_id)
                if confirmed:
                    self.settlement_unknown = self.lease_expired or any(self.store.iter_operations(only_unsettled=True))
                    self.wake.set()
        except Exception:  # noqa: BLE001 -- failed cleanup cannot manufacture settlement
            confirmed = False
        finally:
            # Unknown provider cleanup still has to wait for local filesystem
            # writers before reporting stop or handing ownership to a successor.
            tasks = [task for task in (self.snapshot_task, self.finalize_task) if task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        if not confirmed:
            self.settlement_unknown = True
        return confirmed

    async def _stop_job(self, job_id):
        async with self.cleanup_lock:
            confirmed = await self._settle_stop(job_id)
            async with self.lock:
                job = self.get(job_id)
                if confirmed and not self._needs_settlement(job_id):
                    self._write_projection()
                    self._finish_stop(job)
                elif job['state'] not in TERMINAL:
                    job.update(state='cancelling', finishedAt=None, stage=None,
                               error=ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'Local engine settlement is pending.', retry='check-status').as_dict(job_id))
                    self.store.save(job)
                return self.get(job_id)

    async def _settle_stop(self, job_id):
        needs_settlement = self._needs_settlement(job_id)
        if self.admin_only and needs_settlement:
            return False
        if self.active == job_id:
            return await self._cleanup_active(job_id)
        elif needs_settlement:
            async with self.recovery_lock:
                if self.engine_closed or self.active is not None:
                    # Do not let recovery reject another job's imminent model start.
                    self.settlement_unknown = True
                    self.dependency.update(state='blocked', ready=False,
                                           reason='Owned cleanup waits for the active job; reconcile engine ownership when idle.')
                    return False
                if not await self._predecessor_cleared():
                    self.settlement_unknown = True
                    return False
                try:
                    self._begin_lease()
                    report = await self.engine.recover(item for item in self.store.operations(job_id) if unsettled(item))
                    # Recovery's aggregate flag never settles persisted bindings.
                    # Each record needs its own durable settlement evidence.
                    for patch in report.get('operations', []):
                        await self._record_operation(patch['id'], job_id, patch, recovering=True)
                    confirmed = report.get('settled') is True and not self._needs_settlement(job_id)
                except Exception:  # noqa: BLE001 -- cancellation cannot manufacture settlement
                    confirmed = False
                if not confirmed:
                    self.settlement_unknown = True
                    self.dependency.update(state='blocked', ready=False, reason='Owned engine work has not been confirmed settled; reconcile engine ownership.')
                    self._recovery_result(False, [], False)
                    return False
                self._recovery_result(True, report.get('operations', []), self.dependency['executionReady'])
                self.settlement_unknown = self.lease_expired or any(self.store.iter_operations(only_unsettled=True))
                self.wake.set()
        return True

    def get_result(self, job_id):
        job = self.get(job_id)
        # The durable result commit remains authoritative during engine cleanup.
        if not job.get('resultId'):
            raise ServiceError('RESULT_NOT_AVAILABLE', 'No published result is available.', 409, 'check-status')
        with open_regular(self.private / 'jobs' / job_id / 'sealed', 'manifest.json') as handle:
            return json.load(handle)

    def product(self, job_id, product_id):
        manifest = self.get_result(job_id)
        item = next((item for item in manifest['products'] if item['id'] == product_id), None)
        if item is None:
            raise ServiceError('NOT_FOUND', 'Product not found.', 404)
        root = self.private / 'jobs' / job_id / 'sealed'
        with open_regular(root, item['path']):
            pass
        return root / item['path'], item

    def read_table(self, job_id, table_id, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ServiceError('INVALID_QUERY', 'Rows require offset >= 0 and limit between 1 and 1000.')
        manifest = self.get_result(job_id)
        table = next((item for item in manifest['tables'] if item['id'] == table_id), None)
        if table is None:
            raise ServiceError('NOT_FOUND', 'Table not found.', 404)
        result = {'offset': offset, 'count': 0, 'total': table['rows'], 'columns': table['columns'], 'rows': [], 'nextOffset': None}
        budget = self.profile['limits']['responseBytes']
        if len(encode(result).encode()) > budget - 64:
            raise ServiceError('RESPONSE_TOO_LARGE', 'Column metadata exceeds the response limit. Use complete downloads: ' + encode(table['downloads']), 413)
        path = self.private / 'jobs' / job_id / 'sealed'
        with open_regular(path, table['typedPath']) as handle:
            index = 0
            while True:
                line = handle.readline(budget + 1)
                if not line:
                    break
                if index < offset:
                    while line and not line.endswith(b'\n'):
                        line = handle.readline(budget + 1)
                    index += 1
                    continue
                if len(line) > budget:
                    if not result['rows']:
                        raise ServiceError('ROW_TOO_LARGE', 'Use complete downloads: ' + encode(table['downloads']), 413)
                    break
                row = json.loads(line)
                candidate = {**result, 'rows': result['rows'] + [row], 'count': len(result['rows']) + 1, 'nextOffset': index + 1}
                if len(encode(candidate).encode()) > budget:
                    if not result['rows']:
                        raise ServiceError('ROW_TOO_LARGE', 'Use complete downloads: ' + encode(table['downloads']), 413)
                    break
                result['rows'].append(row)
                index += 1
                if len(result['rows']) >= limit:
                    break
        result['count'] = len(result['rows'])
        result['nextOffset'] = offset + result['count'] if offset + result['count'] < table['rows'] else None
        return result

    def read_text(self, job_id, product_id, offset=0, limit=None):
        limit = self.profile['limits']['textBytes'] if limit is None else limit
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= self.profile['limits']['textBytes']:
            raise ServiceError('INVALID_QUERY', 'Invalid text byte range.')
        path, item = self.product(job_id, product_id)
        if not item['mediaType'].startswith('text/'):
            raise ServiceError('INVALID_PRODUCT', 'This product is not a readable text product.', 422)
        with path.open('rb') as handle:
            handle.seek(offset)
            raw = handle.read(limit)
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            if exc.start == 0:
                raise ServiceError('INVALID_QUERY', 'Offset must be a UTF-8 character boundary.') from None
            raw = raw[:exc.start]
            text = raw.decode('utf-8')
        return {'productId': product_id, 'offset': offset, 'text': text,
                'nextOffset': offset + len(raw) if offset + len(raw) < item['bytes'] else None, 'totalBytes': item['bytes']}

    async def reconcile_documents(self, job_id):
        async with self.publication_lock:
            return await self._reconcile_documents(job_id)

    async def _reconcile_documents(self, job_id):
        manifest = self.get_result(job_id)
        job = self.get(job_id)
        result_id = manifest['resultId']
        source = self.private / 'jobs' / job_id / 'sealed'
        target = self.root / 'documents' / result_id
        pending = self.root / '.document-publication' / (result_id + '.pending')
        def materialize():
            if not target.exists():
                if pending.exists():
                    shutil.rmtree(pending)
                shutil.copytree(source, pending, symlinks=True)
                validate_generation(pending, manifest)
                for path in pending.rglob('*'):
                    if path.is_file():
                        with path.open('rb') as handle:
                            os.fsync(handle.fileno())
                for path in sorted((p for p in pending.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                    fsync_directory(path)
                fsync_directory(pending)
                os.rename(pending, target)
                fsync_directory(target.parent)
            else:
                validate_generation(target, manifest)
        for attempt in range(3):
            try:
                task = asyncio.create_task(asyncio.to_thread(materialize))
                try:
                    await asyncio.shield(task)
                finally:
                    await asyncio.gather(task, return_exceptions=True)
                job = self.get(job_id)
                job['documents'] = {'state': 'ready', 'path': f'documents/{result_id}'}
                self.store.save(job)
                await self._catalog()
                self.event('documents.published', jobId=job_id, resultId=result_id)
                return job['documents']
            except (OSError, ServiceError):
                if attempt < 2:
                    await asyncio.sleep(.05 * (attempt + 1))
        job = self.get(job_id)
        job['documents'] = {'state': 'failed', 'path': None, 'error': 'Shared publication failed; run reconcile after correcting storage.'}
        self.store.save(job)
        return job['documents']

    async def _catalog(self):
        entries = list(self.store.ready_summaries())
        await asyncio.to_thread(self._write_catalog, entries)

    def _write_catalog(self, entries):
        root = self.root / 'documents'
        # Immutable catalog generation: the two root pointers may differ briefly,
        # but each independently references a complete bounded generation.
        catalog = root / ('catalog_' + uuid.uuid4().hex)
        catalog.mkdir()
        chunks = [[]]
        budget = self.profile['limits']['indexBytes']
        def serialized_size(chunk):
            return len(json.dumps({'entries': chunk, 'next': 'page_999999999.json'}, ensure_ascii=False, indent=2).encode())
        for entry in entries:
            if serialized_size([entry]) > budget:
                raise ServiceError('RESOURCE_LIMIT', 'A catalog entry exceeds the index-byte limit.')
            if serialized_size(chunks[-1] + [entry]) > budget and chunks[-1]:
                chunks.append([])
            chunks[-1].append(entry)
        for index, chunk in enumerate(chunks):
            next_page = f'page_{index+1}.json' if index + 1 < len(chunks) else None
            atomic_json(catalog / f'page_{index}.json', {'entries': chunk, 'next': next_page})
            lines = ['# Ready document generations\n'] + [f"- [{entry['resultId']}](../{entry['path']}) — {entry['outcome']}; source `{entry['source']['sha256']}`\n" for entry in chunk]
            if next_page:
                lines.append(f'\n[Continue](page_{index+1}.md)\n')
            if len(''.join(lines).encode()) > budget:
                raise ServiceError('RESOURCE_LIMIT', 'A catalog page exceeds the index-byte limit.')
            (catalog / f'page_{index}.md').write_text(''.join(lines))
        atomic_json(root / 'index.json', {'schemaVersion': 1, 'first': catalog.name + '/page_0.json'})
        temporary = root / 'index.md.tmp'
        temporary.write_text(f'# Documents\n\n[Ready generations]({catalog.name}/page_0.md)\n\nMatch the exact source hash and profile. Report partial coverage. Newer results do not replace earlier evidence.\n')
        os.replace(temporary, root / 'index.md')
        fsync_directory(root)

    def capabilities(self):
        from preprocessing.formats import enabled_extensions
        dependency = copy.deepcopy(self.dependency)
        if self.model_policy is not None:
            selection = self.model_policy.select(self.profile, self._observation.get('models', []))
            dependency.update(selection)
            dependency['ready'] = dependency.get('executionReady') is True and selection['selectedModel'] is not None
        if not self._observation_fresh():
            dependency.update(ready=False, executionReady=False)
        if self.settlement_unknown or self.lease_expired:
            dependency.update(ready=False, executionReady=False, state='blocked',
                              reason='Owned engine work has not been confirmed settled; reconcile engine ownership.')
        if self._scheduler_error is not None:
            dependency.update(ready=False, executionReady=False, state='blocked', reason=self._scheduler_error)
        descriptor = self.engine.descriptor if self.engine is not None else {}
        version = dependency.get('version', descriptor.get('version'))
        dependency['version'] = version if valid_version(version) else None
        raw = dependency.get('compatibility', {})
        try:
            dependency['compatibility'] = compatibility(cli=raw.get('cliVersion'), http=raw.get('httpVersion'),
                descriptor=descriptor.get('version'), warnings=[v for v in raw.get('warnings', [])[:32]
                    if isinstance(v, str) and re.fullmatch('[a-z_]{1,128}', v)])
        except VersionMismatch as error:
            dependency.update(ready=False, executionReady=False)
            dependency['compatibility'] = error.evidence
        except (ValueError, TypeError, AttributeError):
            dependency.update(ready=False, executionReady=False)
            dependency['compatibility'] = {'classification': 'unknown', 'cliVersion': None, 'httpVersion': None,
                                         'descriptorVersion': None, 'warnings': ['version_evidence_invalid']}
        for key in ('mode', 'owner', 'engineInstanceId'):
            value = descriptor.get(key)
            if isinstance(value, str) and 0 < len(value) <= 256 and all(ord(char) >= 32 for char in value):
                dependency[key] = value
        dependency['lastRecovery'] = self.last_recovery
        dependency['jobHolds'] = [{'jobId': job['id'], **job['hold']} for job in self.store.queued() if job.get('hold')] if self.store else []
        return {'schemaVersion': 1, 'extensions': enabled_extensions(), 'profiles': [self.profile],
                'limits': self.profile['limits'], 'engineAvailable': dependency['ready'],
                'dependency': dependency,
                'activity': {'activeJobId': self.active, 'activeJobs': self.store.active_count() if self.store else 0,
                             'queuedJobs': self.store.state_count('queued') if self.store else 0,
                             'unsettledOperations': sum(1 for _ in self.store.iter_operations(only_unsettled=True)) if self.store else 0,
                             'settlementUnknown': self.settlement_unknown,
                             'predecessorPending': self.predecessor is not None or self.predecessor_invalid},
                'executionMode': 'admin-only' if self.admin_only else 'opencode-server',
                'cancellationScope': 'local worker and configured engine operations; remote charges may remain unknown',
                'uploadPolicy': 'explicit-only; upload receipt integration unavailable',
                'deferred': ['upload-receipts', 'r2-sync', 'billing', 'xls'],
                'droppedTelemetryEvents': self.dropped_events}

    async def close(self):
        self.stopping = True
        self.wake.set()
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _drain_stops(self):
        errors = []
        while self.stop_tasks:
            results = await asyncio.gather(*tuple(self.stop_tasks.values()), return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, BaseException))
        if errors:
            raise errors[0]

    async def _close(self):
        try:
            try:
                if self.active:
                    await self._request_stop(self.active, explicit=False)
                await self._drain_stops()
            finally:
                if self.scheduler:
                    try:
                        await asyncio.wait_for(asyncio.shield(self.scheduler), 10)
                    except TimeoutError:
                        self.scheduler.cancel()
                        await asyncio.gather(self.scheduler, return_exceptions=True)
        finally:
            try:
                if self.engine and not self.admin_only:
                    async with self.cleanup_lock, self.recovery_lock:
                        await self.engine.close()
            finally:
                self.engine_closed = True
                async with self.lock:
                    self.cleanup_closed = True
                if self._observation_task and not self._observation_task.done():
                    self._observation_task.cancel()
                    await asyncio.gather(self._observation_task, return_exceptions=True)
                # Requests admitted during engine.close must finish their ledger
                # checks before callbacks, heartbeat, and SQLite are withdrawn.
                try:
                    await self._drain_stops()
                finally:
                    try:
                        # Keep renewing through engine callbacks and every admitted
                        # stop, including stops whose persistence ultimately fails.
                        if self.heartbeat_task:
                            self.heartbeat_task.cancel()
                            await asyncio.gather(self.heartbeat_task, return_exceptions=True)
                    finally:
                        try:
                            if self.store:
                                async with self.lock:
                                    for _, job in self.store.all():
                                        if job['state'] == 'cancelling' and not self._needs_settlement(job['id']):
                                            self._finish_stop(job)
                                    self._write_projection()
                        finally:
                            try:
                                if self.store:
                                    self.store.close()
                            finally:
                                self.store = None
                                if self.owner:
                                    self.owner.close()
                                    self.owner = None
                                self.active = None
