"""Authoritative activity through the private listener and real service ledger."""
import asyncio
import random
import threading
import time
from uuid import uuid4

import pytest
from fakes import FakeEngine
from test_api import client_for
from test_service import terminal
from test_shutdown import wait_until

from preprocessing.errors import ServiceError
from preprocessing.service import Service


async def test_authenticated_activity_route_and_empty_managed_owner(tmp_path, monkeypatch):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    async with client_for(service, token='activity-test') as client:
        original = service.capabilities

        def forbidden_diagnostics():
            raise AssertionError('authentication must precede diagnostics')

        monkeypatch.setattr(service, 'capabilities', forbidden_diagnostics)
        assert (await client.get('/preprocessing/activity')).status_code == 401
        monkeypatch.setattr(service, 'capabilities', original)
        response = await client.get('/preprocessing/activity', headers={'Authorization': 'Bearer activity-test'})
        assert response.status_code == 200, response.text
        report = response.json()
        assert report['contractVersion'] == 1
        assert report['incarnation'] == service.generation
        assert report['complete'] is True and report['settlementUnknown'] is False
        assert (report['runnable'], report['settling'], report['parked']) == (0, 0, 0)
        assert report['activeJobId'] is None
        assert report['pendingCaptures'] == report['unsettledOperations'] == 0
        assert report['predecessorPending'] is False
        limit = service.profile['limits']['queueDepth']
        assert report['runnableCapacity'] == {'limit': limit, 'occupied': 0, 'available': limit}
        assert report['retainedInputs'] == {'count': 0, 'bytes': 0, 'parkedBytes': 0}
        assert report['reasons'] == []
        assert 'scheduling' not in original()  # No park/activate support on this service.
        assert report['revision'] == original()['activity']['revision']
        assert (await client.get('/preprocessing/activity?unexpected=1', headers={'Authorization': 'Bearer activity-test'})).status_code == 400
        assert response.headers['cache-control'] == 'no-store'
        assert (await client.head('/preprocessing/activity', headers={'Authorization': 'Bearer activity-test'})).status_code == 200


async def test_activity_observations_are_passive_and_revision_tracks_capacity(tmp_path, monkeypatch):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        empty = await service.activity()
        # Admission and reads run without yielding to the scheduler between them.
        job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
        job['hold'] = {'reason': 'selected_model_unavailable'}
        service.store.save(job)
        busy = await service.activity()
        assert busy['runnable'] == 1 and busy['parked'] == 0
        assert busy['runnableCapacity']['occupied'] == 1
        assert busy['revision'] > empty['revision']
        service.wake.clear()
        before = service.store.db.total_changes

        async def forbidden(*args):
            raise AssertionError('activity must not perform engine or lifecycle I/O')

        for method in ('_ready', 'reconcile_engine', 'reconcile_documents', 'cancel'):
            monkeypatch.setattr(service, method, forbidden)
        monkeypatch.setattr(service.engine, 'ready', forbidden)
        for _ in range(10):
            assert await service.activity() == busy
        assert service.store.db.total_changes == before
        assert not service.wake.is_set()
        # Restore cancellation for cleanup after checking passive reads.
        monkeypatch.undo()
        await service.cancel(job['id'])
        async with asyncio.timeout(3):
            while (report := await service.activity())['settling']:
                await asyncio.sleep(.001)
        assert report['runnable'] == 0
        assert report['runnableCapacity']['occupied'] == 0
        assert report['revision'] > busy['revision']
    finally:
        await service.close()


@pytest.mark.parametrize('phase,reason', [('snapshot', 'input-capture-pending'), ('finalize', 'active-finalization-pending')])
async def test_local_writers_and_disconnected_stop_cannot_report_clear(tmp_path, monkeypatch, phase, reason):
    (tmp_path / 'data.csv').write_text('001,2\n')
    entered, release = threading.Event(), threading.Event()
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    original = getattr(service, '_' + phase)

    def block(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(service, '_' + phase, block)
    cancelling = None
    try:
        job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        await wait_until(entered.is_set)
        writing = await service.activity()
        assert writing['runnable'] > 0 and writing['settling'] > 0
        assert reason in writing['reasons']
        assert writing['pendingCaptures'] == int(phase == 'snapshot')
        cancelling = asyncio.create_task(service.cancel(job['id']))
        await wait_until(lambda: service.get(job['id'])['cancelRequestedAt'])
        cancelling.cancel()  # HTTP disconnect must not release the stop barrier.
        await asyncio.gather(cancelling, return_exceptions=True)
        stopping = await service.activity()
        assert {'stop-pending', 'cleanup-pending', reason} <= set(stopping['reasons'])
        assert stopping['settling'] > 0
        release.set()
        await wait_until(lambda: service.active is None and not service.stop_tasks)
        idle = await service.activity()
        assert idle['complete'] and idle['runnable'] == idle['settling'] == 0
        assert idle['revision'] > writing['revision']
    finally:
        release.set()
        await service.close()


async def test_terminal_ready_result_still_counts_catalog_publication(tmp_path, monkeypatch):
    (tmp_path / 'data.csv').write_text('001,2\n')
    entered, release = threading.Event(), threading.Event()
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    original = service._write_catalog

    def block(entries):
        entered.set()
        assert release.wait(10)
        return original(entries)

    monkeypatch.setattr(service, '_write_catalog', block)
    try:
        job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        await wait_until(entered.is_set)
        assert service.get(job['id'])['documents']['state'] == 'ready'
        report = await service.activity()
        assert report['runnable'] == 0 and report['settling'] > 0
        assert 'publication-pending' in report['reasons']
        release.set()
        await terminal(service, job)
        report = await service.activity()
        assert report['runnable'] == report['settling'] == 0
    finally:
        release.set()
        await service.close()


async def test_closing_owner_and_missing_or_unreadable_store_fail_closed(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Engine(FakeEngine):
        async def close(self):
            entered.set()
            await release.wait()

    service = Service(tmp_path / 'state', tmp_path, engine=Engine())
    with pytest.raises(ServiceError) as missing:
        await service.activity()
    assert missing.value.status == 503
    await service.start()
    closing = asyncio.create_task(service.close())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        report = await service.activity()
        assert report['complete'] is False and report['settling'] > 0
        assert {'cleanup-pending', 'service-stopping'} <= set(report['reasons'])
    finally:
        release.set()
        await closing
    assert service.capabilities()['activity']['complete'] is False
    with pytest.raises(ServiceError) as closed:
        await service.activity()
    assert closed.value.status == 503

    other = Service(tmp_path / 'other', tmp_path, engine=FakeEngine())
    async with client_for(other, token='test') as client:
        other.store.db.execute('DROP TABLE operations')
        response = await client.get('/preprocessing/activity', headers={'Authorization': 'Bearer test'})
        assert response.status_code == 503
        assert 'settling' not in response.json()
        assert 'operations' not in response.text
        # Restore the ledger so ordinary service teardown can run.
        other.store.db.execute('CREATE TABLE operations(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, body TEXT NOT NULL)')


@pytest.mark.parametrize('unsafe', ['bytes', 'revision', 'capacity'])
async def test_http_rejects_unsafe_numbers_instead_of_rounding(tmp_path, unsafe):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    async with client_for(service, token='test') as client:
        if unsafe == 'bytes':
            job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
            job['source'] = {'snapshotId': 'source_test', 'bytes': 2**53}
            service.store.save(job)
        elif unsafe == 'revision':
            service.activity_revision = 2**53 - 1
            service._activity_facts = None
        else:
            service.profile['limits']['queueDepth'] = 2**53
        response = await client.get('/preprocessing/activity', headers={'Authorization': 'Bearer test'})
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'UNAVAILABLE'


async def test_jittered_admission_cancel_and_observation_revisions(tmp_path):
    randomizer = random.Random(20261104)
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    stop, checking = asyncio.Event(), asyncio.Event()
    revisions = []

    async def observe():
        while not stop.is_set():
            report = await service.activity()
            revisions.append(report['revision'])
            if checking.is_set():
                assert report['runnable'] + report['settling'] > 0
            await asyncio.sleep(randomizer.uniform(0, .002))

    observer = asyncio.create_task(observe())
    try:
        for _ in range(30):
            # Model-unavailable queued jobs remain runnable, never parked.
            service.engine.available = True
            job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
            service.engine.available = False
            checking.set()
            await asyncio.sleep(randomizer.uniform(0, .003))
            checking.clear()
            await service.cancel(job['id'])
            await asyncio.sleep(randomizer.uniform(0, .003))
        await wait_until(lambda: service.active is None and not service.stop_tasks)
        final = await service.activity()
        assert final['complete'] and final['runnable'] == final['settling'] == 0
    finally:
        stop.set()
        await observer
        await service.close()
    assert len(revisions) >= 30 and revisions == sorted(revisions)
    assert len(set(revisions)) > 1


async def test_retained_snapshot_deduplication_and_safe_integer_boundary(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    async with client_for(service, token='test') as client:
        for _ in range(2):
            job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
            await service.cancel(job['id'])
            job = service.get(job['id'])
            job['source'] = {'snapshotId': 'shared', 'bytes': 2**53 - 1}
            service.store.save(job)
        response = await client.get('/preprocessing/activity', headers={'Authorization': 'Bearer test'})
        assert response.status_code == 200
        assert response.json()['retainedInputs'] == {'count': 1, 'bytes': 2**53 - 1, 'parkedBytes': 0}


async def test_capacity_exhaustion_and_store_commit_aba_advance_revision(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        service.profile['limits']['queueDepth'] = 1
        job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
        full = await service.activity()
        assert full['runnableCapacity'] == {'limit': 1, 'occupied': 1, 'available': 0}
        # An unobserved store ABA must still change the next evidence identity.
        saved = service.get(job['id'])
        saved['state'] = 'cancelled'
        service.store.save(saved)
        saved['state'] = 'queued'
        service.store.save(saved)
        same_counts = await service.activity()
        assert same_counts['runnableCapacity'] == full['runnableCapacity']
        assert same_counts['revision'] > full['revision']
        await service.cancel(job['id'])
        free = await service.activity()
        assert free['runnableCapacity'] == {'limit': 1, 'occupied': 0, 'available': 1}
        assert free['revision'] > same_counts['revision']
    finally:
        await service.close()


async def test_unavailable_inventory_preserves_capability_diagnostics_and_recovery(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
        await service.cancel(job['id'])
        saved = service.get(job['id'])
        saved['source'] = {'snapshotId': 'retained-without-byte-evidence'}
        service.store.save(saved)
        caps = service.capabilities()
        assert caps['activity']['complete'] is False
        assert caps['activity']['settlementUnknown'] is True
        with pytest.raises(ServiceError) as invalid:
            await service.activity()
        assert invalid.value.status == 503
    finally:
        await service.close()
    reopened = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await reopened.start()  # An unavailable inventory must not disable ownership recovery.
    try:
        assert reopened.capabilities()['activity']['complete'] is False
    finally:
        await reopened.close()


@pytest.mark.parametrize('uncertainty', ['settlement', 'predecessor', 'invalid-predecessor', 'expired',
                                         'unprojected', 'no-deadline', 'closed-owner', 'starting', 'stopping', 'admin'])
async def test_activity_never_certifies_unknown_ownership(tmp_path, uncertainty):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine(), admin_only=uncertainty == 'admin')
    await service.start()
    try:
        if uncertainty == 'settlement':
            service.settlement_unknown = True
        elif uncertainty == 'predecessor':
            service.predecessor = {'entries': []}
        elif uncertainty == 'invalid-predecessor':
            service.predecessor_invalid = True
        elif uncertainty == 'expired':
            service.lease_deadline = time.monotonic() - 1
        elif uncertainty == 'unprojected':
            service.projection_started = False
        elif uncertainty == 'no-deadline':
            service.lease_deadline = None
        elif uncertainty == 'closed-owner':
            service.owner.close()
        elif uncertainty == 'starting':
            service.started = False
        elif uncertainty == 'stopping':
            service.stopping = True
        before = service.store.db.total_changes
        report = await service.activity()
        assert report['complete'] is False and report['settlementUnknown'] is True
        assert report['settling'] > 0 and report['reasons']
        assert service.store.db.total_changes == before
    finally:
        await service.close()


async def test_terminal_operation_and_publication_are_not_idle(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
        await service.cancel(job['id'])
        job = service.get(job['id'])
        job.update(source={'snapshotId': 'source_shared', 'bytes': 12}, resultId='result_test',
                   documents={'state': 'failed'})
        service.store.save(job)
        service.store.operation({'id': 'terminal-op', 'jobId': job['id'], 'state': 'cleanup_pending',
                                 'settlement': {'settled': False}})
        pending = await service.activity()
        assert pending['settling'] > 0 and pending['unsettledOperations'] == 1
        assert {'publication-pending', 'operation-settlement-pending'} <= set(pending['reasons'])
        assert pending['retainedInputs'] == {'count': 1, 'bytes': 12, 'parkedBytes': 0}
        assert pending['runnableCapacity']['occupied'] == 0  # Same capacity rule as admission.
        job['documents'] = {'state': 'ready'}
        service.store.save(job)
        service.store.operation({'id': 'terminal-op', 'jobId': job['id'], 'state': 'settled',
                                 'settlement': {'settled': True}})
        settled = await service.activity()
        assert settled['settling'] == settled['runnable'] == 0
        assert settled['revision'] > pending['revision']
    finally:
        await service.close()


async def test_pending_admission_is_visible_before_job_exists(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Engine(FakeEngine):
        async def ready(self, profile=None):
            if self.block:
                entered.set()
                await release.wait()
            return await super().ready(profile)

    engine = Engine()
    engine.block = False
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    engine.block = True
    submitting = asyncio.create_task(service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        report = await service.activity()
        assert service.store.active_count() == 0
        assert report['settling'] > 0
        assert 'admission-pending' in report['reasons']
        assert service.capabilities()['activity']['settling'] > 0
    finally:
        release.set()
        await submitting
        await service.close()
