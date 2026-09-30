"""One local job owner for CLI, HTTP, and MCP; parsers live in subprocesses."""
import asyncio
import base64
import errno
import fcntl
import hashlib
import json
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
from preprocessing.store import TERMINAL, Store, encode


def now():
    return datetime.now(UTC).isoformat()


def canonical(body):
    if not isinstance(body, dict) or set(body) - {'source', 'profileId', 'correlation', 'rerunOf'}:
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
    return {'source': source, 'profileId': profile_id,
            'correlation': {'uploadRecordId': correlation.get('uploadRecordId'), 'uploadBatchId': correlation.get('uploadBatchId')},
            'rerunOf': body.get('rerunOf')}


class Service:
    def __init__(self, root: Path, workspace: Path, profile=None, engine=None):
        self.root, self.workspace = Path(root).resolve(), Path(workspace).resolve()
        self.private = self.root / '.preprocessing'
        self.profile = json.loads(encode(profile or default_profile()))
        self.engine = engine
        self.generation = uuid.uuid4().hex
        self.internal_sessions = set()
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

    async def start(self):
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
        if self.engine is not None and hasattr(self.engine, 'owner_fd'):
            self.engine.owner_fd = self.owner.fileno()
            self.engine.on_session = self._record_session
        build_hash = hashlib.sha256()
        for path in sorted(Path(__file__).parent.rglob('*.py')):
            build_hash.update(path.relative_to(Path(__file__).parent).as_posix().encode())
            build_hash.update(path.read_bytes())
        self.profile['build'] = {'upstreamCommit': '2c8a05864ce26b49ec88a78bfd0772e97e949958', 'sourceDigest': build_hash.hexdigest()}
        self.profile['profileDigest'] = hashlib.sha256(encode(self.profile).encode()).hexdigest()
        # Replacement ownership is fenced by the inherited lock in every parser.
        # Engine uncertainty stays explicit; borrowed recovery requires broker settlement.
        for _, job in self.store.all():
            if job['state'] not in TERMINAL:
                unsettled = [item for item in self.store.operations(job['id']) if item['state'] == 'admitted']
                if unsettled and self.engine is not None and not hasattr(self.engine, 'owner_fd'):
                    settlement = await self.engine.cancel(job['id'])
                    if not settlement.get('settled'):
                        self.settlement_unknown = True
                        job['state'] = 'cancelling'
                        self.store.save(job)
                        continue
                for operation in self.store.operations(job['id']):
                    if operation['state'] == 'admitted':
                        operation['state'] = 'outcome_unknown'
                        self.store.operation(operation)
                job.update(state='interrupted', finishedAt=now(), error=ServiceError('RUNTIME_INTERRUPTED', 'Previous owner stopped before completion.', retry='new-job').as_dict(job['id']))
                self.store.save(job)
            if job.get('resultId') and job.get('documents', {}).get('state') != 'ready':
                await self.reconcile_documents(job['id'])
        for _, job in self.store.all():
            for operation in self.store.operations(job['id']):
                self.internal_sessions.update(operation.get('sessionIds', []))
                if operation.get('sessionId'):
                    self.internal_sessions.add(operation['sessionId'])
        await self._catalog()
        if not self.settlement_unknown:
            eligible = [job['id'] for _, job in self.store.all() if job['state'] in TERMINAL and not job.get('resultId')]
            retained = {job['source']['snapshotId'] for _, job in self.store.all() if job.get('source')}
            await asyncio.to_thread(self._reclaim_orphans, eligible, retained)
        self.scheduler = asyncio.create_task(self._schedule())

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
        async with self.lock:
            previous, saved = self.store.lookup(key)
            if previous:
                if saved != request:
                    raise ServiceError('IDEMPOTENCY_CONFLICT', 'Submission key already has a different request.', 409)
                return previous, True
            if self.stopping or self.settlement_unknown:
                raise ServiceError('UNAVAILABLE', 'Service is stopping.', 503, 'check-status')
            if requested['profileId'] not in {'default', self.profile['id']}:
                raise ServiceError('UNKNOWN_PROFILE', 'Profile is not configured.', 422)
            if requested['source']['kind'] == 'upload':
                raise ServiceError('UNSUPPORTED_OPTION', 'Accepted-upload receipts require runtime upload-owner integration.', 422)
            if self.store.active_count() >= self.profile['limits']['queueDepth']:
                raise ServiceError('QUEUE_FULL', 'The processing queue is full.', 429, 'check-status')
            if requested['rerunOf']:
                self.get(requested['rerunOf'])
            if shutil.disk_usage(self.root).free < self.profile['limits']['inputBytes'] + 2 * self.profile['limits']['outputBytes']:
                raise ServiceError('RESOURCE_LIMIT', 'Insufficient free space for a snapshot and two result copies.', 503, 'operator-action')
            job = {'schemaVersion': 1, 'id': 'job_' + uuid.uuid4().hex, 'projectId': self.store.scope,
                   'submissionKey': key, 'correlation': requested['correlation'], 'revision': 1, 'state': 'queued',
                   'createdAt': now(), 'startedAt': None, 'finishedAt': None, 'requestedSource': requested['source'],
                   'source': None, 'profile': self.profile, 'rerunOf': requested['rerunOf'], 'stage': None,
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
            self.active = queued['id']
            try:
                async with asyncio.timeout(self.profile['limits']['jobSeconds']):
                    await self._run(self.active)
            except Exception as exc:  # noqa: BLE001 -- job boundary sanitizes parser failures
                if self.engine and self._needs_settlement(self.active):
                    settlement = await self.engine.cancel(self.active)
                    self.settlement_unknown = not settlement.get('settled')
                await self._stop_worker()
                for operation in self.store.operations(self.active):
                    if operation['state'] == 'admitted':
                        operation.update(state='outcome_unknown', finishedAt=now())
                        self.store.operation(operation)
                job = self.get(self.active)
                if job['state'] not in TERMINAL and job['state'] != 'cancelling':
                    error = exc if isinstance(exc, ServiceError) else ServiceError('RESOURCE_LIMIT' if isinstance(exc, TimeoutError) else 'EXTRACTION_FAILED', 'Processing could not finish within its configured limits.' if isinstance(exc, TimeoutError) else 'Processing failed.', retry='new-job')
                    job.update(state='cancelling' if self.settlement_unknown else 'failed', finishedAt=None if self.settlement_unknown else now(), error=error.as_dict(job['id']))
                    self.store.save(job)
            finally:
                while self.get(self.active)['state'] == 'cancelling' and not self.stopping and not self.settlement_unknown:
                    await asyncio.sleep(.02)
                self.active = None

    def _snapshot(self, job, retained=None):
        request = job['requestedSource']
        if request['kind'] == 'snapshot':
            if retained is None:
                raise ServiceError('SOURCE_NOT_FOUND', 'Snapshot not found.')
            path = self.private / 'sources' / retained['snapshotId'] / 'source'
            if digest_file(path) != retained['sha256']:
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
                    if total > self.profile['limits']['inputBytes']:
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
        self.store.save(job)  # exact bytes/config commit precedes worker/provider work
        self.event('stage.finished', jobId=job_id, stage='snapshot', completedUnits=1)
        directory = self.private / 'jobs' / job_id
        staging = directory / 'staging'
        staging.mkdir(parents=True)
        environment = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PREPROCESSING_PARENT_PID': str(os.getpid()),
                       'PYTHONPATH': str(Path(__file__).parent.parent)}
        async with self.lock:
            if self.get(job_id)['state'] != 'running':
                return
            self.process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'preprocessing.worker',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=environment, start_new_session=True, pass_fds=(self.owner.fileno(),), limit=2 * 1024 * 1024)
        request = {'snapshot': {**snapshot, 'path': str(self.private / 'sources' / snapshot['snapshotId'] / 'source')},
                   'profile': job['profile'], 'output': str(staging)}
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
        await process.wait()
        self.process = None
        job = self.get(job_id)
        if job['state'] != 'running':
            return
        if process.returncode or not completed:
            raise ServiceError('EXTRACTION_FAILED', 'Parser exited without a complete result.')
        extraction = json.loads((staging / 'extraction.json').read_text())
        for stage in extraction['stages']:
            self.event('stage.finished', jobId=job_id, stage=stage['id'], state=stage['state'], coverage=stage.get('coverage'))
        (staging / 'extraction.json').unlink()
        self.finalize_task = asyncio.create_task(asyncio.to_thread(make_manifest, job, extraction, staging))
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
            self.store.save(job)
        await self.reconcile_documents(job_id)
        self.event('job.state_changed', jobId=job_id, state=job['state'])

    async def _model(self, job_id, staging, message):
        operation = {'id': 'operation_' + uuid.uuid4().hex, 'jobId': job_id, 'generation': self.generation,
                     'state': 'admitted', 'createdAt': now(), 'kind': message['operation'].get('kind'),
                     'sourceUnit': message['operation'].get('sourceUnit')}
        async with self.lock:
            job = self.get(job_id)
            if job['state'] != 'running':
                return {'error': ServiceError('RUNTIME_INTERRUPTED', 'Job is no longer running.').as_dict()}
            for ref in message['inputRefs']:
                path = Path(ref['path'])
                relative = path.relative_to(staging).as_posix()
                with open_regular(staging, relative):
                    pass
            self.store.operation(operation)
            self.event('operation.admitted', jobId=job_id, operationId=operation['id'], kind=operation['kind'])
        try:
            if self.engine is None:
                raise ServiceError('PROVIDER_UNAVAILABLE', 'No model engine is configured.', retry='operator-action')
            result = await self.engine.start(operation['id'], job_id, self.generation, job['profile'], message['inputRefs'], message['operation'])
            operation = next(item for item in self.store.operations(job_id) if item['id'] == operation['id'])
            operation.update(state='completed', finishedAt=now(), sessionId=result.get('sessionId'),
                             requestedModel=result.get('requestedModel'), returnedModel=result.get('returnedModel'), usage=result.get('usage'), attempts=result.get('attempts'))
            if result.get('sessionId'):
                self.internal_sessions.add(result['sessionId'])
            self.store.operation(operation)
            self.event('operation.settled', jobId=job_id, operationId=operation['id'], state=operation['state'], sessionId=operation.get('sessionId'))
            current = self.get(job_id)
            if result.get('usage') is not None:
                current['usage'] = {'observations': [{'operationId': item['id'], 'usage': item['usage']} for item in self.store.operations(job_id) if item.get('usage') is not None]}
                current['usageStatus'] = 'incomplete'
                self.store.save(current)
            return {'result': result}
        except ServiceError as exc:
            operation = next(item for item in self.store.operations(job_id) if item['id'] == operation['id'])
            operation.update(state='outcome_unknown' if exc.code == 'PROVIDER_OUTCOME_UNKNOWN' else 'failed',
                             finishedAt=now(), error=exc.as_dict(operation['id']),
                             sessionId=getattr(exc, 'session_id', None), settlement=getattr(exc, 'settlement', None), evidence=getattr(exc, 'evidence', None))
            self.store.operation(operation)
            self.event('operation.settled', jobId=job_id, operationId=operation['id'], state=operation['state'], code=exc.code)
            if operation.get('settlement') and not operation['settlement'].get('settled'):
                self.settlement_unknown = True
                current = self.get(job_id)
                current.update(state='cancelling', error=ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'Local engine settlement is pending.', retry='operator-action').as_dict(job_id))
                self.store.save(current)
                raise
            return {'error': exc.as_dict(operation['id'])}

    async def _record_session(self, operation_id, job_id, session_id):
        async with self.lock:
            operation = next(item for item in self.store.operations(job_id) if item['id'] == operation_id)
            operation['sessionId'] = session_id
            operation.setdefault('sessionIds', []).append(session_id)
            self.store.operation(operation)
            self.internal_sessions.add(session_id)
            if self.get(job_id)['state'] != 'running':
                raise ServiceError('RUNTIME_INTERRUPTED', 'Job stopped before model dispatch.')

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
        return any(item['state'] == 'admitted' or (item.get('settlement') and not item['settlement'].get('settled'))
                   for item in self.store.operations(job_id))

    async def cancel(self, job_id):
        async with self.lock:
            job = self.get(job_id)
            if job['state'] in TERMINAL:
                return job
            job['state'] = 'cancelling'
            self.store.save(job)
        if self.active == job_id:
            needs_settlement = self._needs_settlement(job_id)
            await self._stop_worker()
            settlement = await self.engine.cancel(job_id) if self.engine and needs_settlement else {'settled': True}
            if not settlement.get('settled'):
                self.settlement_unknown = True
                return self.get(job_id)
            if self.snapshot_task:
                await asyncio.gather(self.snapshot_task, return_exceptions=True)
            if self.finalize_task:
                await asyncio.gather(self.finalize_task, return_exceptions=True)
        async with self.lock:
            job = self.get(job_id)
            if job['state'] == 'cancelling':
                for operation in self.store.operations(job_id):
                    if operation['state'] == 'admitted':
                        operation.update(state='outcome_unknown', finishedAt=now())
                        self.store.operation(operation)
                job.update(state='cancelled', finishedAt=now(), stage=None)
                self.store.save(job)
            return job

    def get_result(self, job_id):
        job = self.get(job_id)
        if job['state'] not in {'succeeded', 'partial'} or not job.get('resultId'):
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
        return {'schemaVersion': 1, 'extensions': enabled_extensions(), 'profiles': [self.profile],
                'limits': self.profile['limits'], 'engineAvailable': self.engine is not None,
                'cancellationScope': 'local worker and configured engine operations; remote charges may remain unknown',
                'uploadPolicy': 'explicit-only; upload receipt integration unavailable',
                'deferred': ['upload-receipts', 'runtime-broker-wiring', 'r2-sync', 'billing', 'xls'],
                'droppedTelemetryEvents': self.dropped_events}

    async def close(self):
        self.stopping = True
        self.wake.set()
        if self.active:
            await self.cancel(self.active)
        if self.scheduler:
            try:
                await asyncio.wait_for(self.scheduler, 10)
            except TimeoutError:
                self.scheduler.cancel()
                await asyncio.gather(self.scheduler, return_exceptions=True)
        if self.engine:
            await self.engine.close()
        if self.store:
            self.store.close()
        if self.owner:
            self.owner.close()
