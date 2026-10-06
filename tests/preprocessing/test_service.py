import asyncio
import hashlib
import io
import json
import os
import random
import sqlite3
import stat
import sys
import time
import uuid

import pytest
from fakes import FakeEngine

from preprocessing.errors import ServiceError
from preprocessing.service import Service


async def terminal(service, job):
    async with asyncio.timeout(20):
        while ((current := service.get(job['id']))['state'] in {'queued', 'running', 'cancelling'} or service.active == job['id']):
            await asyncio.sleep(.02)
    return current


@pytest.mark.asyncio
async def test_missing_engine_requires_explicit_admin_mode(tmp_path):
    service = Service(tmp_path / 'state', tmp_path)
    with pytest.raises(ServiceError) as error:
        await service.start()
    assert error.value.code == 'ENGINE_CONFIGURATION_REQUIRED'
    admin = Service(tmp_path / 'state', tmp_path, admin_only=True)
    await admin.start()
    try:
        assert admin.capabilities()['dependency']['state'] == 'unconfigured'
        with pytest.raises(ServiceError) as error:
            await admin.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        assert error.value.status == 503
    finally:
        await admin.close()


@pytest.mark.asyncio
async def test_native_admission_rechecks_dependency_and_preserves_results(tmp_path):
    (tmp_path / 'data.csv').write_text('001,2\n')
    engine = FakeEngine()
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        body = {'source': {'kind': 'workspace-file', 'path': 'data.csv'}}
        job, _ = await service.submit(str(uuid.uuid4()), body)
        done = await terminal(service, job)
        manifest = service.get_result(done['id'])
        engine.available = False
        with pytest.raises(ServiceError) as error:
            await service.submit(str(uuid.uuid4()), body)
        assert error.value.status == 503
        assert service.get_result(done['id']) == manifest
        assert service.capabilities()['dependency']['state'] == 'unavailable'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_unknown_operation_recovery_and_safe_unblock(tmp_path):
    engine = FakeEngine()
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
    await service.cancel(job['id'])
    service.store.operation({'id': 'op_unknown', 'jobId': job['id'], 'state': 'outcome_unknown',
                             'sessionId': 'ses_old', 'settlement': {'settled': False},
                             'evidence': {'text': 'retained'}})
    await service.close()
    assert json.loads((service.private / 'engine-lease.json').read_text())['entries'][0]['sessionId'] == 'ses_old'
    engine = FakeEngine()
    engine.recovery_settled = False
    engine.descriptor['recovery'] = {'fencedGenerations': [service.generation], 'deadEngineInstances': []}
    reopened = Service(tmp_path / 'state', tmp_path, engine=engine)
    await reopened.start()
    try:
        assert engine.recovered[0]['id'] == 'op_unknown'
        assert reopened.settlement_unknown
        engine.recovery_settled = True
        engine.available = False
        assert not (await reopened.reconcile_engine())['settled']
        with pytest.raises(ServiceError):
            await reopened.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.csv'}})
        engine.available = True
        assert (await reopened.reconcile_engine())['settled']
        saved = reopened.store.operations(job['id'])[0]
        assert saved['settlement']['settled']
        assert saved['evidence'] == {'text': 'retained'}
        assert reopened.get(job['id'])['state'] == 'cancelled'
        await reopened.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.csv'}})
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_configured_unavailable_start_keeps_status_access(tmp_path):
    engine = FakeEngine()
    engine.available = False
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        assert service.list_jobs()['jobs'] == []
        assert service.capabilities()['dependency']['state'] == 'unavailable'
        with pytest.raises(ServiceError):
            await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.csv'}})
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_empty_ledger_outage_reconnects_without_recovery(tmp_path):
    class Engine(FakeEngine):
        async def recover(self, operations):
            assert list(operations) == []
            raise OSError('server unavailable')

    engine = Engine()
    engine.available = False
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        assert not service.settlement_unknown
        engine.available = True
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.csv'}})
        assert job['state'] == 'queued'
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal_state', [False, True])
async def test_inactive_cancel_recovers_persisted_unknown_instead_of_memory(tmp_path, terminal_state):
    engine = FakeEngine()
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        if terminal_state:
            await service.cancel(job['id'])
        service.store.operation({'id': 'op_inactive', 'jobId': job['id'], 'generation': service.generation,
                                 'state': 'outcome_unknown', 'sessionId': 'ses_candidate', 'inputId': 'msg_candidate',
                                 'settlement': {'settled': False}})
        engine.recovery_settled = False
        assert (await service.cancel(job['id']))['state'] == 'cancelling'
        assert engine.recovered[0]['id'] == 'op_inactive'
        assert not service.store.get_operation('op_inactive')['settlement']['settled']
        engine.recovery_settled = True
        assert (await service.cancel(job['id']))['state'] == 'cancelled'
    finally:
        await service.close()


async def predecessor_fixture(tmp_path):
    engine = FakeEngine()
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
    await service.cancel(job['id'])
    service.store.operation({'id': 'op_predecessor', 'jobId': job['id'], 'generation': service.generation,
                             'state': 'outcome_unknown', 'sessionId': 'ses_old', 'inputId': 'msg_old',
                             'engineInstanceId': 'engine_test', 'requestHash': 'a' * 64,
                             'agent': 'preprocessing', 'model': {'providerID': 'p', 'id': 'm'},
                             'location': {'directory': '/test/extraction'}, 'dispatchIntent': True,
                             'admissionAcknowledged': False, 'settlement': {'settled': False}})
    await service.close()
    return service.private, service.generation


@pytest.mark.asyncio
async def test_admin_preserves_pending_lease_and_generation(tmp_path):
    private, _ = await predecessor_fixture(tmp_path)
    original = (private / 'engine-lease.json').read_bytes()
    with sqlite3.connect(private / 'state.sqlite') as db:
        generation = db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0]
    admin = Service(tmp_path / 'state', tmp_path, admin_only=True, lease_interval=.02, lease_seconds=.2)
    await admin.start()
    try:
        await asyncio.sleep(.05)
        assert admin.heartbeat_task is None
        assert (private / 'engine-lease.json').read_bytes() == original
        with sqlite3.connect(private / 'state.sqlite') as db:
            assert db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0] == generation
        assert admin.list_jobs()['jobs']
    finally:
        await admin.close()
    assert (private / 'engine-lease.json').read_bytes() == original


@pytest.mark.asyncio
async def test_predecessor_lease_retained_until_owner_attests_fencing(tmp_path):
    private, generation = await predecessor_fixture(tmp_path)
    original = (private / 'engine-lease.json').read_bytes()

    class Engine(FakeEngine):
        def refresh_descriptor(self):
            if getattr(self, 'owner_fenced', False):
                self.descriptor['recovery'] = {'fencedGenerations': [generation], 'deadEngineInstances': []}

    engine = Engine()
    service = Service(tmp_path / 'state', tmp_path, engine=engine, lease_interval=.02, lease_seconds=.2)
    await service.start()
    try:
        await asyncio.sleep(.05)
        assert service.heartbeat_task is None
        assert (private / 'engine-lease.json').read_bytes() == original
        assert engine.recovered == []
        with sqlite3.connect(private / 'state.sqlite') as db:
            assert json.loads(db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0])['id'] == generation
        assert service.capabilities()['dependency']['state'] == 'blocked'
        engine.owner_fenced = True
        assert (await service.reconcile_engine())['settled']
        projection = json.loads((private / 'engine-lease.json').read_text())
        assert projection['generation'] == service.generation
        with sqlite3.connect(private / 'state.sqlite') as db:
            assert json.loads(db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0])['id'] == service.generation
        assert projection['writer']['bootId']
        assert service.heartbeat_task is not None
        assert service.store.get_operation('op_predecessor')['settlement']['settled']
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_dead_engine_requires_dead_writer_or_different_boot(tmp_path):
    private, _ = await predecessor_fixture(tmp_path)
    engine = FakeEngine()
    engine.descriptor['recovery'] = {'fencedGenerations': [], 'deadEngineInstances': ['engine_test']}
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        assert service.heartbeat_task is None  # Same PID/starttime and boot is still alive.
        assert not (await service.reconcile_engine())['settled']
    finally:
        await service.close()
    projection = json.loads((private / 'engine-lease.json').read_text())
    projection['writer']['bootId'] = str(uuid.uuid4())  # Persisted identity from a previous boot.
    (private / 'engine-lease.json').write_text(json.dumps(projection))
    reopened = Service(tmp_path / 'state', tmp_path, engine=engine)
    await reopened.start()
    try:
        assert not reopened.settlement_unknown
        assert reopened.heartbeat_task is not None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_dependency_status_retains_public_identity_and_constant_reasons(tmp_path):
    class Engine(FakeEngine):
        async def ready(self, profile=None):
            return {'ready': False, 'reason': 'password=private-token', 'serverUrl': 'http://private'}

    engine = Engine()
    engine.descriptor.update(mode='standalone', owner='supervisor', version='2.0.19',
                             password='private-token', serverUrl='http://private')
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        capabilities = service.capabilities()
        dependency = capabilities['dependency']
        assert {key: dependency[key] for key in ('mode', 'owner', 'version', 'engineInstanceId')} == {
            'mode': 'standalone', 'owner': 'supervisor', 'version': '2.0.19', 'engineInstanceId': 'engine_test'}
        assert dependency['lastRecovery']['settled'] is True
        assert dependency['lastRecovery']['ready'] is False
        assert capabilities['activity']['queuedJobs'] == 0
        assert capabilities['activity']['unsettledOperations'] == 0
        assert 'private' not in json.dumps(capabilities)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_result_remains_readable_during_unknown_cancellation(tmp_path):
    (tmp_path / 'data.csv').write_text('001,2\n')
    engine = FakeEngine()
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        job = await terminal(service, job)
        manifest = service.get_result(job['id'])
        service.store.operation({'id': 'op_result', 'jobId': job['id'], 'generation': service.generation,
                                 'sessionId': 'ses_pending', 'inputId': 'msg_pending',
                                 'state': 'outcome_unknown', 'settlement': {'settled': False}})
        engine.recovery_settled = False
        assert (await service.cancel(job['id']))['state'] == 'cancelling'
        assert service.get_result(job['id']) == manifest
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_active_cancel_aggregate_cannot_settle_record_never_started_here(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        service.store.operation({'id': 'op_unregistered', 'jobId': job['id'], 'generation': service.generation,
                                 'sessionId': 'ses_pending', 'inputId': 'msg_pending',
                                 'state': 'outcome_unknown', 'settlement': {'settled': False}})
        service.active = job['id']
        assert (await service.cancel(job['id']))['state'] == 'cancelling'
        assert not service.store.get_operation('op_unregistered')['settlement']['settled']
    finally:
        service.active = None
        await service.close()


@pytest.mark.asyncio
async def test_inactive_recovery_does_not_race_another_active_job(tmp_path):
    engine = FakeEngine()
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        old, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'old.pdf'}})
        active, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'active.pdf'}})
        service.store.operation({'id': 'old-op', 'jobId': old['id'], 'generation': service.generation,
                                 'sessionId': 'ses_old', 'inputId': 'msg_old', 'state': 'outcome_unknown',
                                 'settlement': {'settled': False}})
        current = service.get(active['id'])
        current['state'] = 'running'
        service.store.save(current)
        service.active = active['id']
        assert (await service.cancel(old['id']))['state'] == 'cancelling'
        assert engine.recovered == []
        assert service.get(active['id'])['state'] == 'running'
        assert service.settlement_unknown
    finally:
        service.active = None
        await service.close()


@pytest.mark.asyncio
async def test_projection_failure_prevents_mutation_and_further_admission(tmp_path, monkeypatch):
    mutations = []

    class Engine(FakeEngine):
        async def start(self, operation_id, job_id, *args):
            await self.on_operation(operation_id, job_id, {
                'state': 'prepared', 'sessionId': 'ses_candidate', 'inputId': 'msg_candidate',
                'settlement': {'settled': False}})
            mutations.append('HTTP create')

    service = Service(tmp_path / 'state', tmp_path, engine=Engine())
    await service.start()
    original = service._write_projection

    def fail_projection():
        if any(item['state'] == 'prepared' for item in service.store.iter_operations()):
            raise OSError('projection fsync failed')
        original()

    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        job['state'] = 'running'
        service.store.save(job)
        monkeypatch.setattr(service, '_write_projection', fail_projection)
        with pytest.raises((OSError, ServiceError)):
            await service._model(job['id'], tmp_path, {'operation': {'kind': 'ocr'}, 'inputRefs': []})
        assert mutations == []
        assert service.store.operations(job['id'])[0]['sessionId'] == 'ses_candidate'
        with pytest.raises(ServiceError) as error:
            await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        assert error.value.status == 503
    finally:
        monkeypatch.setattr(service, '_write_projection', original)
        await service.close()


@pytest.mark.asyncio
async def test_queued_execution_waits_for_dependency_without_losing_data(tmp_path):
    (tmp_path / 'data.csv').write_text('001,2\n')
    engine = FakeEngine()
    service = Service(tmp_path / 'state', tmp_path, engine=engine, lease_interval=.02, lease_seconds=.2)
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        engine.available = False
        await asyncio.sleep(.08)
        assert service.get(job['id'])['state'] == 'queued'
        assert service.get(job['id'])['source'] is None
        projection = json.loads((service.private / 'engine-lease.json').read_text())
        assert projection['expiresAt'] > time.time()
        engine.available = True
        assert (await terminal(service, job))['state'] == 'succeeded'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_expired_writer_lease_cannot_be_renewed_or_reconciled_open(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine(),
                      lease_interval=.02, lease_seconds=.1)
    await service.start()
    try:
        expiry = json.loads((service.private / 'engine-lease.json').read_text())['expiresAt']
        time.sleep(.15)  # noqa: ASYNC251 -- deliberately pause the writer and its heartbeat
        await asyncio.sleep(.025)
        assert json.loads((service.private / 'engine-lease.json').read_text())['expiresAt'] == expiry
        assert not (await service.reconcile_engine())['settled']
        with pytest.raises(ServiceError):
            await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.csv'}})
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failure_does_not_replace_durable_evidence_with_stale_exception(tmp_path):
    class Engine(FakeEngine):
        async def start(self, operation_id, job_id, *args):
            await self.on_operation(operation_id, job_id, {
                'state': 'prepared', 'sessionId': 'ses_evidence', 'inputId': 'msg_evidence'})
            await self.on_operation(operation_id, job_id, {
                'state': 'cleanup_pending', 'evidence': {'text': 'captured', 'usage': {'tokens': 9}}})
            error = ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'cleanup remains pending')
            error.evidence = {'text': '', 'usage': None}
            error.settlement = {'settled': False}
            raise error

    service = Service(tmp_path / 'state', tmp_path, engine=Engine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        job['state'] = 'running'
        service.store.save(job)
        with pytest.raises(ServiceError):
            await service._model(job['id'], tmp_path, {'operation': {'kind': 'ocr'}, 'inputRefs': []})
        saved = service.store.operations(job['id'])[0]
        assert saved['evidence'] == {'text': 'captured', 'usage': {'tokens': 9}}
        assert saved['state'] == 'cleanup_pending'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_snapshot_replay_and_full_typed_publication(tmp_path):
    source = tmp_path / 'input.csv'
    source.write_text('id,value\n001,1.000000000000000001\n002,\n')
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        key = str(uuid.uuid4())
        body = {'source': {'kind': 'workspace-file', 'path': 'input.csv'}}
        async def submit():
            await asyncio.sleep(random.random() / 100)
            return await service.submit(key, body)
        responses = await asyncio.gather(*(submit() for _ in range(40)))
        assert len({item[0]['id'] for item in responses}) == 1
        assert sum(not replay for _, replay in responses) == 1
        job = await terminal(service, responses[0][0])
        assert job['state'] == 'succeeded', job
        manifest = service.get_result(job['id'])
        assert manifest['source']['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
        rows = service.read_table(job['id'], manifest['tables'][0]['id'])
        assert rows['rows'][1]['cells'][0]['value'] == '001'
        assert rows['rows'][1]['cells'][1]['value'] == '1.000000000000000001'
        assert job['documents']['state'] == 'ready'
        assert (tmp_path / 'state' / job['documents']['path'] / 'AGENTS.md').is_file()
        for product in manifest['products']:
            path, _ = service.product(job['id'], product['id'])
            assert hashlib.sha256(path.read_bytes()).hexdigest() == product['sha256']
        source.write_text('changed')
        replay, found = await service.submit(key, body)
        assert found and replay['id'] == job['id']
        with pytest.raises(ServiceError, match='different'):
            await service.submit(key, {'source': {'kind': 'workspace-file', 'path': 'other.csv'}})
        rerun, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'snapshot', 'snapshotId': job['source']['snapshotId']}, 'rerunOf': job['id']})
        assert (await terminal(service, rerun))['state'] == 'succeeded'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_hash_mismatch_and_symlink_are_nonbillable(tmp_path):
    (tmp_path / 'data.csv').write_text('001,2\n')
    (tmp_path / 'link.csv').symlink_to(tmp_path / 'data.csv')
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        for src, code in [({'kind': 'workspace-file', 'path': 'data.csv', 'expectedSha256': '0' * 64}, 'SOURCE_CHANGED'),
                          ({'kind': 'workspace-file', 'path': 'link.csv'}, 'SOURCE_READ_FAILED')]:
            job, _ = await service.submit(str(uuid.uuid4()), {'source': src})
            done = await terminal(service, job)
            assert done['state'] == 'failed'
            assert done['error']['code'] == code
            assert service.store.operations(job['id']) == []
            assert list((service.private / 'sources').iterdir()) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_owner_lock_restart_and_cancel(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    other = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    with pytest.raises(ServiceError, match='owner'):
        await other.start()
    job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
    cancelled = await service.cancel(job['id'])
    assert cancelled['state'] == 'cancelled'
    assert service.lookup(job['submissionKey'])['id'] == job['id']
    await service.close()
    await other.start()
    assert other.get(job['id'])['state'] == 'cancelled'
    await other.close()


@pytest.mark.asyncio
async def test_request_schema_and_oversized_row(tmp_path):
    (tmp_path / 'large.csv').write_text('x' * 10000 + '\n')
    from preprocessing.contracts import default_profile
    profile = default_profile()
    profile['limits']['responseBytes'] = 2048
    service = Service(tmp_path / 'state', tmp_path, profile, engine=FakeEngine())
    await service.start()
    try:
        with pytest.raises(ServiceError):
            await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'large.csv'}, 'stages': []})
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'large.csv'}})
        job = await terminal(service, job)
        assert job['state'] == 'succeeded', job
        table = service.get_result(job['id'])['tables'][0]
        with pytest.raises(ServiceError) as err:
            service.read_table(job['id'], table['id'])
        assert err.value.code == 'ROW_TOO_LARGE'
        assert table['downloads'][0]['productId'] in err.value.message
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancellation_publication_jitter_keeps_next_job_alive(tmp_path):
    (tmp_path / 'data.csv').write_text('001,2\n' * 100)
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        for _ in range(15):
            first, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
            second, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
            await asyncio.sleep(random.uniform(0, .10))
            cancelled = await service.cancel(first['id'])
            assert cancelled['state'] in {'cancelled', 'succeeded'}
            assert (await terminal(service, second))['state'] == 'succeeded'
            if cancelled['state'] == 'cancelled':
                with pytest.raises(ServiceError) as error:
                    service.get_result(first['id'])
                assert error.value.code == 'RESULT_NOT_AVAILABLE'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_shared_publication_failure_reconciles_without_extraction(tmp_path, monkeypatch):
    import preprocessing.service as module
    (tmp_path / 'data.csv').write_text('001,2\n')
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    original = module.shutil.copytree
    monkeypatch.setattr(module.shutil, 'copytree', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        job = await terminal(service, job)
        while service.active:
            await asyncio.sleep(.02)
        job = service.get(job['id'])
        assert job['state'] == 'succeeded' and job['documents']['state'] == 'failed'
        manifest = service.get_result(job['id'])
        monkeypatch.setattr(module.shutil, 'copytree', original)
        assert (await service.reconcile_documents(job['id']))['state'] == 'ready'
        assert service.get_result(job['id']) == manifest
        assert service.store.operations(job['id']) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_lost_publication_commit_is_not_visible_after_restart(tmp_path, monkeypatch):
    (tmp_path / 'data.csv').write_text('001,2\n')
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    original = service.store.save

    def lose_commit(job):
        if job.get('resultId'):
            raise OSError('lost owner before result commit')
        return original(job)

    monkeypatch.setattr(service.store, 'save', lose_commit)
    job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
    done = await terminal(service, job)
    assert done['state'] == 'failed'
    assert (service.private / 'jobs' / job['id'] / 'sealed').exists()
    with pytest.raises(ServiceError):
        service.get_result(job['id'])
    await service.close()
    reopened = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await reopened.start()
    assert reopened.get(job['id'])['resultUrl'] is None
    assert not list((tmp_path / 'state' / 'documents').glob('result_*'))
    await reopened.close()


@pytest.mark.asyncio
async def test_history_cursor_excludes_new_admissions_and_checks_filters(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        jobs = [(await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': f'{i}.csv'}}))[0] for i in range(5)]
        page = service.list_jobs(limit=2)
        newer, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'new.csv'}})
        found = list(page['jobs'])
        while page['nextCursor']:
            page = service.list_jobs(limit=2, cursor=page['nextCursor'])
            found.extend(page['jobs'])
        assert {item['id'] for item in found} == {item['id'] for item in jobs}
        assert newer['id'] not in {item['id'] for item in found}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_active_model_session_is_persisted_before_dispatch(tmp_path, monkeypatch):
    fsyncs = []
    original_fsync = os.fsync

    def observe_fsync(fd):
        original_fsync(fd)
        fsyncs.append(os.fstat(fd).st_mode)

    monkeypatch.setattr(os, 'fsync', observe_fsync)

    class Engine(FakeEngine):
        async def start(self, operation_id, job_id, generation, profile, refs, operation):
            saved = service.store.get_operation(operation_id)
            assert saved['state'] == 'preparing'
            before = len(fsyncs)
            await self.on_operation(operation_id, job_id, {
                'state': 'prepared', 'sessionId': 'session_test', 'inputId': 'msg_test',
                'engineInstanceId': 'engine_test', 'agent': 'preprocessing', 'model': {'providerID': 'p', 'id': 'm'},
                'location': {'directory': '/test/extraction'}, 'requestHash': 'a' * 64,
                'settlement': {'settled': False}, 'prompt': 'private', 'credentials': 'secret'})
            assert any(stat.S_ISREG(mode) for mode in fsyncs[before:])
            assert any(stat.S_ISDIR(mode) for mode in fsyncs[before:])
            with sqlite3.connect(service.private / 'state.sqlite') as db:
                committed = json.loads(db.execute('SELECT body FROM operations WHERE id=?', (operation_id,)).fetchone()[0])
            assert committed['sessionId'] == 'session_test'
            saved = service.store.operations(job_id)[0]
            assert saved['sessionId'] == 'session_test'
            assert 'session_test' in service.internal_sessions
            projection_path = service.private / 'engine-lease.json'
            projection = json.loads(projection_path.read_text())
            assert projection['entries'][0]['sessionId'] == 'session_test'
            assert projection['entries'][0]['inputId'] == 'msg_test'
            assert projection['entries'][0]['requestHash'] == 'a' * 64
            assert projection['entries'][0]['agent'] == 'preprocessing'
            assert projection['entries'][0]['model'] == {'providerID': 'p', 'id': 'm'}
            assert projection['entries'][0]['location'] == {'directory': '/test/extraction'}
            assert projection['entries'][0]['dispatchIntent'] is False
            assert projection['entries'][0]['admissionAcknowledged'] is False
            assert projection['writer']['pid'] == os.getpid()
            assert projection['writer']['starttime']
            assert projection['writer']['bootId']
            assert projection_path.stat().st_mode & 0o777 == 0o600
            assert 'private' not in projection_path.read_text()
            assert 'secret' not in projection_path.read_text()
            assert refs[0]['bytes'] == 5
            assert refs[0]['sha256'] == hashlib.sha256(b'image').hexdigest()
            await self.on_operation(operation_id, job_id, {'state': 'dispatch_intent'})
            assert json.loads(projection_path.read_text())['entries'][0]['dispatchIntent'] is True
            await self.on_operation(operation_id, job_id, {'state': 'admitted'})
            assert json.loads(projection_path.read_text())['entries'][0]['admissionAcknowledged'] is True
            await self.on_operation(operation_id, job_id, {'state': 'completed', 'evidence': {'text': 'transcription', 'usage': {'tokens': 7}}})
            assert service.store.get_operation(operation_id)['evidence']['text'] == 'transcription'
            # Model evidence is committed before a simulated HTTP DELETE.
            await self.on_operation(operation_id, job_id, {'state': 'cleanup_pending', 'cleanupState': 'pending'})
            await self.on_operation(operation_id, job_id, {'state': 'completed', 'cleanupState': 'deleted', 'settlement': {'settled': True}})
            return {'text': 'transcription', 'usage': None, 'sessionId': 'session_test'}
    engine = Engine()
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        job['state'] = 'running'
        service.store.save(job)
        (tmp_path / 'image.png').write_bytes(b'image')
        reply = await service._model(job['id'], tmp_path, {'operation': {'kind': 'figure-ocr'}, 'inputRefs': [{'path': str(tmp_path / 'image.png'), 'mediaType': 'image/png'}]})
        assert reply['result']['text'] == 'transcription'
        assert service.store.operations(job['id'])[0]['evidence'] == {'text': 'transcription', 'usage': {'tokens': 7}}
        assert json.loads((service.private / 'engine-lease.json').read_text())['entries'] == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unsettled_engine_cannot_publish_or_admit_more_work(tmp_path):
    class Engine(FakeEngine):
        async def start(self, *args):
            exc = ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'unknown')
            exc.settlement = {'settled': False}
            raise exc
        async def cancel(self, job):
            return {'settled': False}
    service = Service(tmp_path / 'state', tmp_path, engine=Engine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        job['state'] = 'running'
        service.store.save(job)
        with pytest.raises(ServiceError):
            await service._model(job['id'], tmp_path, {'operation': {'kind': 'ocr'}, 'inputRefs': []})
        assert service.get(job['id'])['state'] == 'cancelling'
        with pytest.raises(ServiceError) as error:
            await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        assert error.value.status == 503
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_wide_table_empty_page_obeys_byte_budget(tmp_path):
    from preprocessing.contracts import default_profile
    (tmp_path / 'wide.csv').write_text(','.join('v' for _ in range(100)) + '\n')
    profile = default_profile()
    profile['limits']['responseBytes'] = 2048
    service = Service(tmp_path / 'state', tmp_path, profile, engine=FakeEngine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'wide.csv'}})
        job = await terminal(service, job)
        table = service.get_result(job['id'])['tables'][0]
        with pytest.raises(ServiceError) as error:
            service.read_table(job['id'], table['id'], offset=table['rows'])
        assert error.value.code == 'RESPONSE_TOO_LARGE'
        assert table['downloads'][0]['productId'] in error.value.message
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_poisoned_engine_does_not_block_native_cancel(tmp_path):
    class Engine(FakeEngine):
        async def cancel(self, job_id):
            return {'settled': False}
    (tmp_path / 'data.csv').write_text('001,2\n' * 10000)
    service = Service(tmp_path / 'state', tmp_path, engine=Engine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        async with asyncio.timeout(5):
            while not service.process:
                await asyncio.sleep(.001)
        assert (await service.cancel(job['id']))['state'] == 'cancelled'
        assert service.process is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_publication_does_not_block_event_loop(tmp_path, monkeypatch):
    import threading

    import preprocessing.service as module
    (tmp_path / 'data.csv').write_text('001,2\n' * 100)
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    copying = threading.Event()
    responsive = threading.Event()
    original = module.shutil.copytree

    def slow_copy(*args, **kwargs):
        copying.set()
        assert responsive.wait(2), 'publication blocked concurrent status and cancellation'
        return original(*args, **kwargs)

    monkeypatch.setattr(module.shutil, 'copytree', slow_copy)
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        async with asyncio.timeout(5):
            while not copying.is_set():
                await asyncio.sleep(.001)
        assert service.get(job['id'])['state'] == 'succeeded'
        assert (await service.cancel(job['id']))['state'] == 'succeeded'
        responsive.set()
        while service.active:
            await asyncio.sleep(.01)
        assert service.get(job['id'])['documents']['state'] == 'ready'
    finally:
        responsive.set()
        await service.close()


@pytest.mark.asyncio
async def test_staged_output_budget_stops_parser_before_full_export(tmp_path):
    from preprocessing.contracts import default_profile
    rows = 100000
    (tmp_path / 'data.csv').write_text('001,2\n' * rows)
    profile = default_profile()
    profile['limits']['outputBytes'] = 20000
    service = Service(tmp_path / 'state', tmp_path, profile, engine=FakeEngine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        job = await terminal(service, job)
        assert job['state'] == 'failed'
        assert job['error']['code'] == 'RESOURCE_LIMIT'
        assert not job.get('resultId')
        exports = list((service.private / 'jobs' / job['id'] / 'staging').rglob('*.jsonl'))
        assert exports
        assert sum(1 for _ in exports[0].open()) < rows
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_owner_death_fences_worker_and_interrupts_job(tmp_path):
    (tmp_path / 'data.csv').write_text('001,2\n' * 500000)
    code = '''
import asyncio, sys, uuid
from pathlib import Path
from preprocessing.service import Service
sys.path.insert(0, str(Path.cwd() / 'tests/preprocessing'))
from fakes import FakeEngine
async def main():
    root = Path(sys.argv[1])
    service = Service(root / 'state', root, engine=FakeEngine())
    await service.start()
    job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
    while service.process is None:
        await asyncio.sleep(.001)
    (root / 'job-id').write_text(job['id'])
    await asyncio.sleep(60)
asyncio.run(main())
'''
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', code, str(tmp_path), stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(10):
            while not (tmp_path / 'job-id').exists():
                await asyncio.sleep(.005)
        child.kill()
        await child.wait()
        service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
        async with asyncio.timeout(10):
            while True:
                try:
                    await service.start()
                    break
                except ServiceError as exc:
                    assert exc.code == 'OWNER_ACTIVE'
                    await asyncio.sleep(.02)
        try:
            job = service.get((tmp_path / 'job-id').read_text())
            assert job['state'] == 'interrupted'
            assert job['resultUrl'] is None
            assert not list((tmp_path / 'state/documents').glob('result_*'))
        finally:
            await service.close()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@pytest.mark.asyncio
async def test_fenced_orphans_reclaimed_after_grace_only(tmp_path):
    from preprocessing.contracts import default_profile
    profile = default_profile()
    profile['limits']['orphanGraceSeconds'] = 1
    service = Service(tmp_path / 'state', tmp_path, profile, engine=FakeEngine())
    await service.start()
    job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
    await service.cancel(job['id'])
    orphan = service.private / 'jobs' / job['id'] / 'sealed'
    orphan.mkdir(parents=True)
    (orphan / 'unfinished').write_text('unpublished')
    fresh = orphan.parent / 'staging'
    fresh.mkdir()
    retained = service.private / 'sources/source_retained'
    abandoned = service.private / 'sources/source_abandoned'
    recent = service.private / 'sources/source_recent'
    for directory in (retained, abandoned, recent):
        directory.mkdir()
        (directory / 'source').write_text('preserve exact bytes')
    cancelled = service.get(job['id'])
    cancelled['source'] = {'snapshotId': retained.name}
    service.store.save(cancelled)
    for directory in (retained, abandoned):
        os.utime(directory, (time.time() - 20, time.time() - 20))
    os.utime(orphan, (time.time() - 20, time.time() - 20))
    await service.close()
    reopened = Service(tmp_path / 'state', tmp_path, profile, engine=FakeEngine())
    await reopened.start()
    try:
        assert not orphan.exists()
        assert fresh.exists()
        assert (retained / 'source').read_text() == 'preserve exact bytes'
        assert not abandoned.exists()
        assert recent.exists()
        assert reopened.get(job['id'])['state'] == 'cancelled'
    finally:
        await reopened.close()


def test_manifest_rejects_tampering_and_unlisted_navigation(tmp_path):
    from preprocessing.results import validate_generation
    (tmp_path / 'index.md').write_text('[escape](../secret)')
    item = {'id': 'p', 'path': 'index.md', 'kind': 'navigation-index',
            'bytes': (tmp_path / 'index.md').stat().st_size,
            'sha256': hashlib.sha256((tmp_path / 'index.md').read_bytes()).hexdigest()}
    manifest = {'products': [item], 'figures': []}
    with pytest.raises(ServiceError, match='unlisted'):
        validate_generation(tmp_path, manifest)
    item['sha256'] = '0' * 64
    with pytest.raises(ServiceError, match='bytes'):
        validate_generation(tmp_path, manifest)


def test_worker_metadata_is_not_charged_against_product_budget(tmp_path):
    from preprocessing.worker import Context
    (tmp_path / 'data.txt').write_bytes(b'x' * 99)
    (tmp_path / 'extraction.json').write_bytes(b'x' * 2000)
    Context(tmp_path, 100).check_output()


def test_worker_ipc_honors_raised_response_budget(tmp_path, monkeypatch, capsys):
    from preprocessing.worker import Context
    text = 'x' * (3 * 1024 * 1024)
    reply = (json.dumps({'result': {'text': text}}) + '\n').encode()
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(reply)))
    with pytest.raises(ServiceError) as error:
        Context(tmp_path, 100).model({'kind': 'ocr'}, [])
    assert error.value.code == 'RESOURCE_LIMIT'
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(reply)))
    assert Context(tmp_path, 100, response_bytes=4 * 1024 * 1024).model({'kind': 'ocr'}, [])['text'] == text
    capsys.readouterr()


def test_catalog_pages_bound_serialized_unicode_bytes(tmp_path):
    from preprocessing.contracts import default_profile
    profile = default_profile()
    profile['limits']['indexBytes'] = 32768
    service = Service(tmp_path, tmp_path, profile)
    (tmp_path / 'documents').mkdir()
    entries = [{'resultId': f'result_{i}', 'jobId': f'job_{i}',
                'source': {'sha256': 'a' * 64, 'origin': {'kind': 'workspace-file', 'path': 'é' * 60 + '.csv'}},
                'coverage': {'unit': 'row', 'total': 5, 'completed': 5, 'failed': 0},
                'profile': 'b' * 64, 'outcome': 'succeeded', 'path': f'result_{i}/index.md'} for i in range(200)]
    service._write_catalog(entries)
    root = tmp_path / 'documents'
    assert (root / 'index.json').is_file()
    assert (root / 'index.md').is_file()
    pointer = json.loads((root / 'index.json').read_text())
    page_path = root / pointer['first']
    assert page_path.with_suffix('.md').is_file()
    assert f"({page_path.relative_to(root).with_suffix('.md').as_posix()})" in (root / 'index.md').read_text()
    visited, found = set(), []
    while page_path is not None:
        assert page_path not in visited, 'Catalog continuation links must not cycle.'
        visited.add(page_path)
        page = json.loads(page_path.read_text())
        found.extend(page['entries'])
        page_path = page_path.parent / page['next'] if page['next'] is not None else None
    assert len(visited) > 1
    assert len(found) == len(entries)
    assert {entry['resultId']: entry for entry in found} == {entry['resultId']: entry for entry in entries}
    for path in root.rglob('*'):
        if path.is_file():
            assert path.stat().st_size <= 32768
