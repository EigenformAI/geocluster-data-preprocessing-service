"""One local job owner for CLI, HTTP, and MCP; parsers live in subprocesses."""
import asyncio
import base64
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
from preprocessing.errors import ServiceError
from preprocessing.files import (
    atomic_json,
    digest_file,
    fsync_directory,
    open_regular,
    relative_parts,
)
from preprocessing.results import make_manifest, validate_generation
from preprocessing.store import TERMINAL, Store, encode, unsettled
from preprocessing.table_recovery import (
    RECOVERY_EVENT_FIELDS,
    effective_profile,
    semantic_fingerprint,
    validate_override,
)


def now():
    return datetime.now(UTC).isoformat()


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
                 admin_only=False, lease_interval=10, lease_seconds=30):
        self.root, self.workspace = Path(root).resolve(), Path(workspace).resolve()
        self.private = self.root / '.preprocessing'
        self.profile = effective_profile(profile or default_profile())
        self.work_deadlines = {}
        self.hard_deadlines = {}
        self.engine = engine
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
        self.heartbeat_task = None
        self.predecessor = None
        self.predecessor_invalid = False
        self.projection_started = False
        self.last_recovery = None
        self.dependency = {'state': 'unconfigured' if engine is None else 'unavailable',
                           'configured': engine is not None, 'ready': False}

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
            await self.reconcile_engine()
        for _, job in self.store.all():
            if not self.admin_only and job['state'] not in TERMINAL:
                if self._needs_settlement(job['id']):
                    job.update(state='cancelling', error=ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'Local engine settlement is pending.', retry='check-status').as_dict(job['id']))
                    self.store.save(job)
                    continue
                job.update(state='interrupted', finishedAt=now(), error=ServiceError('RUNTIME_INTERRUPTED', 'Previous owner stopped before completion.', retry='new-job').as_dict(job['id']))
                self.store.save(job)
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
                await self.engine.ready(self.profile)
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
        self._check_lease()
        if self.lease_expired:
            return False
        if self.engine is None or self.admin_only:
            self.dependency.update(ready=False, state='unconfigured' if self.engine is None else 'admin-only')
            return False
        try:
            capabilities = await self.engine.ready(self.profile)
            ready = isinstance(capabilities, dict) and capabilities.get('ready') is True
        except Exception:  # noqa: BLE001 -- dependency failures must not leak credentials
            ready = False
        self._check_lease()
        if self.lease_expired:
            return False
        self.dependency = {'configured': True, 'ready': ready,
                           'state': 'ready' if ready else 'unavailable'}
        if not ready:
            self.dependency['reason'] = 'OpenCode extraction readiness failed; check server identity, agent, model and recovery configuration.'
        return ready

    async def reconcile_engine(self):
        """Reconcile durable ownership without replay, then recheck admission readiness."""
        if self.admin_only:
            return {'settled': False, 'operations': [], 'reason': 'Engine reconciliation is unavailable in administrative mode.'}
        async with self.recovery_lock:
            async with self.lock:
                if self.active is not None and self.get(self.active)['state'] == 'running':
                    return {'settled': False, 'operations': [], 'reason': 'An extraction is active.'}
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
            ready = await self._ready()
            async with self.lock:
                self.settlement_unknown = not recovered or self.lease_expired
                if recovered:
                    for _, job in self.store.all():
                        if job['state'] == 'cancelling' and not self._needs_settlement(job['id']):
                            job.update(state='interrupted', finishedAt=now(), stage=None,
                                       error=ServiceError('RUNTIME_INTERRUPTED', 'Interrupted extraction requires an explicit rerun.', retry='new-job').as_dict(job['id']))
                            self.store.save(job)
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
        return self.store.get(job_id)

    def lookup(self, key):
        return self.store.lookup(key.lower())[0]

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
            return previous, True
        if self.stopping or self.admin_only or self.settlement_unknown:
            raise ServiceError('UNAVAILABLE', 'Processing admission is blocked; check dependency status and reconcile engine ownership.', 503, 'check-status')
        # Dependency network I/O must not prevent lease renewal or durable callbacks.
        if not await self._ready():
            raise ServiceError('PROVIDER_UNAVAILABLE', 'OpenCode extraction is unavailable; check the configured server, agent and model.', 503, 'operator-action')
        profile = effective_profile(self.profile, requested['tableRecovery'], code='INVALID_REQUEST')
        resume_parent = None
        if requested['resumeOf']:
            resume_parent = self._resume_parent(requested['resumeOf'], requested['source'])
            await asyncio.to_thread(self._validate_resume_files, resume_parent, profile)
        async with self.lock:
            previous, saved = self.store.lookup(key)
            if previous:
                if saved != request:
                    raise ServiceError('IDEMPOTENCY_CONFLICT', 'Submission key already has a different request.', 409)
                return previous, True
            if self.stopping or self.admin_only or self.settlement_unknown:
                raise ServiceError('UNAVAILABLE', 'Processing admission is blocked; check dependency status and reconcile engine ownership.', 503, 'check-status')
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
                if current_parent['revision'] != resume_parent['revision']:
                    raise ServiceError('RESUME_INVALID', 'Parent changed during resume validation; submit again.')
            if shutil.disk_usage(self.root).free < self.profile['limits']['inputBytes'] + 2 * self.profile['limits']['outputBytes']:
                raise ServiceError('RESOURCE_LIMIT', 'Insufficient free space for a snapshot and two result copies.', 503, 'operator-action')
            job = {'schemaVersion': 1, 'id': 'job_' + uuid.uuid4().hex, 'projectId': self.store.scope,
                   'submissionKey': key, 'correlation': requested['correlation'], 'revision': 1, 'state': 'queued',
                   'createdAt': now(), 'startedAt': None, 'finishedAt': None, 'requestedSource': requested['source'],
                   'source': None, 'profile': profile, 'rerunOf': requested['rerunOf'],
                   'resumeOf': requested['resumeOf'], 'stage': None,
                   'stages': [], 'resultUrl': None, 'documents': None, 'usage': None, 'usageStatus': 'unavailable', 'error': None}
            self.store.insert(job, request)
            self.wake.set()
            self.event('job.state_changed', jobId=job['id'], state='queued')
            return job, False

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
        return {'jobs': [job for _, job in page], 'nextCursor': cursor}

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

    def _validate_resume(self, parent_id, source, profile):
        return self._validate_resume_files(self._resume_parent(parent_id, source), profile)

    def _validate_resume_files(self, parent, profile):
        root = self.private / 'jobs' / parent['id'] / 'sealed'
        try:
            with open_regular(root, 'manifest.json') as handle:
                raw = handle.read()
            if hashlib.sha256(raw).hexdigest() != parent.get('sealedManifestSha256'):
                raise ValueError('manifest digest')
            manifest = json.loads(raw)
            if (manifest['jobId'] != parent['id'] or manifest['resultId'] != parent['resultId']
                    or manifest['source'] != parent['source'] or manifest['pipeline'] != parent['profile']):
                raise ValueError('manifest identity')
            validate_generation(root, manifest)
            with open_regular(self.private / 'sources' / parent['source']['snapshotId'], 'source') as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != parent['source']['sha256']:
                    raise ValueError('source digest')
            plans = [item for item in manifest['products'] if item['kind'] == 'work-plan']
            if len(plans) != 1:
                raise ValueError('missing or ambiguous work plan')
            with open_regular(root, plans[0]['path']) as handle:
                raw_plan = handle.read()
            if len(raw_plan) != plans[0]['bytes'] or hashlib.sha256(raw_plan).hexdigest() != plans[0]['sha256']:
                raise ValueError('work plan changed during validation')
            plan = json.loads(raw_plan)
            fingerprint = semantic_fingerprint(parent['source'], parent['profile'])
            if plan.get('fingerprint') != fingerprint:
                raise ValueError('work plan semantic identity')
            if fingerprint != semantic_fingerprint(parent['source'], profile):
                raise ServiceError('RESUME_INCOMPATIBLE', 'Resume source, build, parser, policy, prompt or requested model settings changed.')
            units = {unit['id']: unit for unit in plan['units']}
            paths = {product['path'] for product in manifest['products']}
            for unit in units.values():
                if (unit['state'] == 'completed'
                        and any(units[dep]['state'] != 'completed' for dep in unit['dependencies'])):
                    raise ValueError('completed unit has unfinished dependencies')
                if unit.get('fingerprint', fingerprint) != fingerprint:
                    raise ValueError('unit semantic identity')
                evidence = unit['evidencePaths']
                if isinstance(evidence, dict):
                    evidence = list(evidence.values())
                if not isinstance(evidence, list) or any(path not in paths for path in evidence):
                    raise ValueError('unit evidence closure')
            return parent, manifest, plan
        except ServiceError as exc:
            if exc.code == 'RESUME_INCOMPATIBLE':
                raise
            raise ServiceError('RESUME_INVALID', 'Parent sealed evidence or work-plan validation failed.') from None
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise ServiceError('RESUME_INVALID', 'Parent sealed evidence or work-plan validation failed.') from None

    def _prepare_resume(self, job, staging, validated=None):
        parent, manifest, plan = validated or self._validate_resume(job['resumeOf'], job['requestedSource'], job['profile'])
        # Copies are independent mutable files; sealed generations are never linked.
        excluded = {'navigation-index', 'table-preview', 'figure-index', 'agent-guide', 'work-plan'}
        products = [item for item in manifest['products'] if item['kind'] not in excluded]
        limits = job['profile']['limits']
        if (sum(table['rows'] for table in manifest['tables']) > limits['rows']
                or any(len(table['columns']) > limits['columns'] for table in manifest['tables'])):
            raise ServiceError('RESOURCE_LIMIT', 'Retained tables exceed the new generation row or column limit.')
        if sum(item['bytes'] for item in products) > job['profile']['limits']['outputBytes']:
            raise ServiceError('RESOURCE_LIMIT', 'Reusable evidence exceeds the new generation output-byte limit.')
        root = self.private / 'jobs' / parent['id'] / 'sealed'
        copied = []
        for item in products:
            target = staging.joinpath(*relative_parts(item['path']))
            target.parent.mkdir(parents=True, exist_ok=True)
            with open_regular(root, item['path']) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
            if target.stat().st_size != item['bytes'] or digest_file(target) != item['sha256']:
                raise ServiceError('RESUME_INVALID', 'Parent evidence changed while copying.')
            # Source locators, render transforms and model/attempt metadata are
            # evidence. Only generation-specific publication identities change.
            copied.append(json.loads(encode({key: value for key, value in item.items()
                                             if key not in {'id', 'contentUrl'}})))
        # Explicit new force pages invalidate their dependent units, not whole jobs.
        force_pages = job['profile'].get('_tableRecoveryOverride', {}).get('forcePages', [])
        invalidated = {unit['id'] for unit in plan['units'] if unit.get('source', {}).get('page') in force_pages}
        changed = True
        while changed:
            more = {unit['id'] for unit in plan['units'] if invalidated.intersection(unit['dependencies'])}
            changed = not more <= invalidated
            invalidated.update(more)
        plan = json.loads(encode(plan))
        completed = []
        for unit in plan['units']:
            if unit['id'] in invalidated:
                unit.update(state='pending', reason='explicit-force-page')
                unit.pop('reusedFromJob', None)
            elif unit['state'] == 'completed':
                unit['reusedFromJob'] = parent['id']
                completed.append(unit)
        extraction = {key: manifest[key] for key in ('coverage', 'stages', 'tables', 'figures', 'findings', 'provenance')}
        extraction = json.loads(encode(extraction))
        extraction['products'] = copied
        for table in extraction['tables']:
            for key in ('rowsUrl', 'downloads'):
                table.pop(key, None)
        for figure in extraction['figures']:
            for key in ('imageProductId', 'ocrProductId', 'interpretationProductId'):
                figure.pop(key, None)
        histories = {}
        completed_tables = {unit.get('regionId') for unit in completed if unit['kind'] == 'table-confirmation'}
        copied_paths = {item['path'] for item in copied}
        for table in extraction['tables']:
            if table.get('source', {}).get('page') in force_pages:
                continue
            path = table.get('evidencePaths', {}).get('decisions')
            if path:
                with open_regular(staging, path) as handle:
                    history = json.load(handle)
                comparisons = history.get('comparisons', [])
                if not isinstance(comparisons, list):
                    raise ServiceError('RESUME_INVALID', 'Parent comparison history is malformed.')
                histories[table['id']] = {
                    'comparisonCount': len(comparisons),
                    'comparisonsDigest': hashlib.sha256(encode(comparisons).encode()).hexdigest(),
                    'evidencePaths': history.get('evidencePaths', []),
                }
            if table['id'] in completed_tables:
                table_paths = [table['typedPath'], table['csvPath'], *table.get('evidencePaths', {}).values()]
                if any(path not in copied_paths for path in table_paths):
                    raise ServiceError('RESUME_INVALID', 'Completed table dependency evidence was not copied.')
        return {'reusedFromJob': parent['id'], 'previousExtraction': extraction,
                'products': copied, 'workPlan': plan, 'completedUnits': completed,
                'forcePages': force_pages, 'invalidatedUnitIds': sorted(invalidated),
                'tableHistories': histories,
                'maxAdditionalPasses': job['profile']['options']['tableRecovery']['maxAdditionalPasses']}

    def _work_remaining(self, job_id):
        deadline = self.work_deadlines.get(job_id)
        return float('inf') if deadline is None else max(0, deadline - time.monotonic())

    def _validate_resume_output(self, extraction, staging, resume):
        try:
            plans = [product for product in extraction['products'] if product['kind'] == 'work-plan']
            if len(plans) != 1:
                raise ValueError('resume requires one work plan')
            with open_regular(staging, plans[0]['path']) as handle:
                plan = json.load(handle)
            if plan['fingerprint'] != resume['workPlan']['fingerprint']:
                raise ValueError('resume semantic identity changed')
            units = {unit['id']: unit for unit in plan['units']}
            products = {item['path']: item for item in extraction['products']}
            old_products = {item['path']: item for item in resume['products']}
            immutable_paths = set()
            completed_tables = set()
            for reused in resume['completedUnits']:
                unit = units[reused['id']]
                if unit['state'] != 'completed' or unit.get('reusedFromJob') != resume['reusedFromJob']:
                    raise ValueError('resume discarded completed work')
                for key in ('kind', 'source', 'fingerprint', 'candidateId', 'regionId', 'passId', 'tile', 'operationKind'):
                    if unit.get(key) != reused.get(key):
                        raise ValueError('reused unit identity changed')
                if not set(reused['dependencies']) <= set(unit['dependencies']):
                    raise ValueError('completed dependency closure was dropped')
                if not set(reused['evidencePaths']) <= set(unit['evidencePaths']):
                    raise ValueError('completed evidence references were dropped')
                for path in reused['evidencePaths']:
                    if path not in products:
                        raise ValueError('reused evidence product was dropped')
                    if old_products[path]['kind'] in {'table-image', 'table-model-response', 'typed-table',
                                                    'table-export', 'table-structure', 'table-recovery-audit'}:
                        immutable_paths.add(path)
                if reused['kind'] == 'table-confirmation' and reused.get('regionId'):
                    completed_tables.add(reused['regionId'])
            invalidated = set(resume.get('invalidatedUnitIds', []))
            for old_unit in resume['workPlan']['units']:
                if old_unit['id'] in invalidated or 'passesSpent' not in old_unit:
                    continue
                spent = units[old_unit['id']].get('passesSpent')
                old_spent = old_unit['passesSpent']
                allowance = max(old_spent, resume.get('maxAdditionalPasses', old_spent))
                if type(spent) is not int or not old_spent <= spent <= allowance:
                    raise ValueError('cumulative dispute allowance changed')
            tables = {table['id']: table for table in extraction['tables']}
            confirmations = {unit['regionId']: unit for unit in units.values()
                             if unit['kind'] == 'table-confirmation' and unit.get('regionId')}
            for old_table in resume['previousExtraction']['tables']:
                if old_table['id'] in completed_tables:
                    table = tables[old_table['id']]
                    if any(table.get(key) != value for key, value in old_table.items()):
                        raise ValueError('completed table selection or references changed')
                history = resume.get('tableHistories', {}).get(old_table['id'])
                if history and old_table['id'] in tables:
                    with open_regular(staging, tables[old_table['id']]['evidencePaths']['decisions']) as handle:
                        decisions = json.load(handle)
                    prefix = decisions.get('comparisons', [])[:history['comparisonCount']]
                    if hashlib.sha256(encode(prefix).encode()).hexdigest() != history['comparisonsDigest']:
                        raise ValueError('prior comparison history was dropped or changed')
                    if not set(history['evidencePaths']) <= set(decisions.get('evidencePaths', [])):
                        raise ValueError('prior decision evidence was dropped')
                    if old_table['id'] in confirmations:
                        spent = decisions.get('passesSpent')
                        if type(spent) is not int or spent != confirmations[old_table['id']]['passesSpent']:
                            raise ValueError('decision audit and work plan disagree on cumulative passes')
            for path in immutable_paths:
                old = old_products[path]
                if any(products[path].get(key) != old.get(key) for key in ('kind', 'source', 'render', 'model')):
                    raise ValueError('reused evidence metadata changed')
                with open_regular(staging, path) as handle:
                    if hashlib.file_digest(handle, 'sha256').hexdigest() != old['sha256']:
                        raise ValueError('reused evidence bytes changed')
        except (OSError, ValueError, TypeError, KeyError, ServiceError, RecursionError):
            raise ServiceError('OUTPUT_INVALID', 'Resumed extraction must retain completed evidence, selection, cumulative passes and comparison history.') from None

    def _finalize(self, job, extraction, staging, resume):
        if resume is not None:
            self._validate_resume_output(extraction, staging, resume)
        return make_manifest(job, extraction, staging)

    def _dispatch_allowance(self):
        engine = getattr(self.engine, 'engine', None) or self.engine
        return getattr(engine, 'dispatch_allowance_seconds', 0)

    def _can_dispatch(self, job_id):
        if job_id not in self.work_deadlines:
            return True
        engine = getattr(self.engine, 'engine', None) or self.engine
        finish = getattr(engine, 'finish_allowance_seconds', 0)
        hard_remaining = self.hard_deadlines.get(job_id, float('inf')) - time.monotonic()
        return (self._work_remaining(job_id) > self._dispatch_allowance()
                and hard_remaining > self._dispatch_allowance() + finish)

    async def _schedule(self):
        while not self.stopping:
            if self.settlement_unknown:
                self.wake.clear()
                await self.wake.wait()
                continue
            queued = self.store.next_queued()
            if queued is None:
                self.wake.clear()
                await self.wake.wait()
                continue
            # An outage after admission does not turn queued source data into a failure.
            ready = await self._ready()
            if not ready or self.settlement_unknown or self.stopping:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), self.lease_interval)
                except TimeoutError:
                    pass
                continue
            self.active = queued['id']
            try:
                async with asyncio.timeout(queued['profile']['limits']['jobSeconds']):
                    await self._run(self.active)
            except Exception as exc:  # noqa: BLE001 -- job boundary sanitizes parser failures
                if self.engine and self._needs_settlement(self.active):
                    try:
                        settlement = await self.engine.cancel(self.active)
                        await self._apply_settlement(self.active, settlement)
                        self.settlement_unknown = not settlement.get('settled')
                    except Exception:  # noqa: BLE001 -- preserve ownership uncertainty on cleanup failure
                        self.settlement_unknown = True
                await self._stop_worker()
                for operation in self.store.operations(self.active):
                    if unsettled(operation):
                        try:
                            await self._record_operation(operation['id'], self.active,
                                                         {'state': 'outcome_unknown', 'finishedAt': now()})
                        except ServiceError:
                            self.settlement_unknown = True
                job = self.get(self.active)
                if job['state'] not in TERMINAL and job['state'] != 'cancelling':
                    error = exc if isinstance(exc, ServiceError) else ServiceError('RESOURCE_LIMIT' if isinstance(exc, TimeoutError) else 'EXTRACTION_FAILED', 'Processing could not finish within its configured limits.' if isinstance(exc, TimeoutError) else 'Processing failed.', retry='new-job')
                    job.update(state='cancelling' if self.settlement_unknown else 'failed', finishedAt=None if self.settlement_unknown else now(), error=error.as_dict(job['id']))
                    self.store.save(job)
            finally:
                while self.get(self.active)['state'] == 'cancelling' and not self.stopping and not self.settlement_unknown:
                    await asyncio.sleep(.02)
                self.work_deadlines.pop(self.active, None)
                self.hard_deadlines.pop(self.active, None)
                self.active = None

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
        job.update(state='running', startedAt=now(), stage={'id': 'snapshot', 'completedUnits': 0, 'totalUnits': 1})
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
        job.update(source=snapshot, stage={'id': 'extract', 'completedUnits': 0, 'totalUnits': None})
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
            validated = await asyncio.to_thread(self._validate_resume_files, parent, job['profile'])
            self.finalize_task = asyncio.create_task(asyncio.to_thread(self._prepare_resume, job, staging, validated))
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
                   'workRemaining': self._work_remaining(job_id) if job_id in self.work_deadlines else None,
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
            engine = getattr(self.engine, 'engine', None) or self.engine
            poll_allowance = getattr(engine, 'rpc_seconds', 0) if patch.get('state') == 'dispatch_intent' else 0
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
        async with self.lock:
            job = self.get(job_id)
            needs_settlement = self._needs_settlement(job_id)
            if job['state'] in TERMINAL and not needs_settlement:
                return job
            job['state'] = 'cancelling'
            if not job.get('resultId'):
                job['finishedAt'] = None
            self.store.save(job)
        if self.admin_only and needs_settlement:
            return self.get(job_id)
        if self.active == job_id:
            await self._stop_worker()
            settlement = await self.engine.cancel(job_id) if self.engine and needs_settlement else {'settled': True}
            await self._apply_settlement(job_id, settlement)
            if not settlement.get('settled'):
                self.settlement_unknown = True
                return self.get(job_id)
            if self.snapshot_task:
                await asyncio.gather(self.snapshot_task, return_exceptions=True)
            if self.finalize_task:
                await asyncio.gather(self.finalize_task, return_exceptions=True)
        elif needs_settlement:
            async with self.recovery_lock:
                if self.active is not None and self.active != job_id:
                    # Do not let recovery reject another job's imminent model start.
                    self.settlement_unknown = True
                    self.dependency.update(state='blocked', ready=False,
                                           reason='Owned cleanup waits for the active job; reconcile engine ownership when idle.')
                    return self.get(job_id)
                if not await self._predecessor_cleared():
                    self.settlement_unknown = True
                    return self.get(job_id)
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
                    return self.get(job_id)
                self._recovery_result(True, report.get('operations', []), self.dependency['ready'])
        async with self.lock:
            job = self.get(job_id)
            if job['state'] == 'cancelling':
                if self._needs_settlement(job_id):
                    self.settlement_unknown = True
                    return job
                self._write_projection()
                job.update(state='cancelled', finishedAt=now(), stage=None)
                self.store.save(job)
            return job

    def get_result(self, job_id):
        job = self.get(job_id)
        # The durable result commit remains authoritative while engine cleanup
        # changes the job's cancellation/recovery state.
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
                job['documents'] = {'state': 'ready', 'path': f'documents/{result_id}'}
                self.store.save(job)
                await self._catalog()
                self.event('documents.published', jobId=job_id, resultId=result_id)
                return job['documents']
            except (OSError, ServiceError):
                if attempt < 2:
                    await asyncio.sleep(.05 * (attempt + 1))
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
        dependency = dict(self.dependency)
        if self.settlement_unknown and dependency['ready']:
            dependency.update(ready=False, state='blocked',
                              reason='Owned engine work has not been confirmed settled; reconcile engine ownership.')
        descriptor = self.engine.descriptor if self.engine is not None else {}
        for key in ('mode', 'owner', 'version', 'engineInstanceId'):
            value = descriptor.get(key)
            if isinstance(value, str) and 0 < len(value) <= 256 and all(ord(char) >= 32 for char in value):
                dependency[key] = value
        dependency['lastRecovery'] = self.last_recovery
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
        try:
            if self.active:
                await self.cancel(self.active)
            if self.scheduler:
                try:
                    await asyncio.wait_for(self.scheduler, 10)
                except TimeoutError:
                    self.scheduler.cancel()
                    await asyncio.gather(self.scheduler, return_exceptions=True)
        finally:
            try:
                if self.engine and not self.admin_only:
                    await self.engine.close()
            finally:
                # Keep renewing while the engine is settling and invoking callbacks.
                if self.heartbeat_task:
                    self.heartbeat_task.cancel()
                    await asyncio.gather(self.heartbeat_task, return_exceptions=True)
                try:
                    if self.store:
                        async with self.lock:
                            self._write_projection()
                finally:
                    if self.store:
                        self.store.close()
                        self.store = None
                    if self.owner:
                        self.owner.close()
                        self.owner = None
