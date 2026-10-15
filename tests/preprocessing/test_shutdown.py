"""Stop intent, ownership barriers, and durable queue recovery without cloud calls."""
import asyncio
import json
import random
import sqlite3
import threading
from uuid import uuid4

import httpx
import pytest
from engine_peer import Peer, default_profile
from fakes import FakeEngine
from test_liveness import scanned_pdf
from test_service import terminal

from preprocessing.engine import OpenCodeServerEngine
from preprocessing.errors import ServiceError
from preprocessing.service import Service


def persisted(service, job_id):
    with sqlite3.connect(service.private / 'state.sqlite') as db:
        return json.loads(db.execute('SELECT body FROM jobs WHERE id=?', (job_id,)).fetchone()[0])


async def wait_until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(.002)


async def peer_service(tmp_path):
    peer = Peer(tmp_path, 'hang')
    descriptor = {'serverUrl': 'http://localhost:1234', 'password': 'private',
                  'version': '2.0.19', 'engineInstanceId': 'engine-1', 'mode': 'standalone',
                  'owner': 'test-owner', 'directory': str(tmp_path), 'contextVerified': True,
                  'recovery': {'managed': False, 'extractionExcluded': True,
                               'fencedGenerations': [], 'deadEngineInstances': []}}
    engine = OpenCodeServerEngine(descriptor, input_roots=(tmp_path,), rpc_seconds=.1, cancel_seconds=.1,
                                  client=httpx.AsyncClient(transport=httpx.MockTransport(peer.handle)))
    service = Service(tmp_path / 'state', tmp_path, default_profile(), engine=engine,
                      lease_interval=.02, lease_seconds=2)
    await service.start()

    async def record(oid, jid, patch):
        await service._record_operation(oid, jid, patch)
        await peer.persist(oid, jid, patch)

    engine.bind(record)
    return service, peer


@pytest.mark.parametrize('explicit', [False, True])
async def test_real_active_scanned_job_stop_persists_intent_before_cleanup(tmp_path, monkeypatch, explicit):
    scanned_pdf(tmp_path / 'scan.pdf', 1)
    service, peer = await peer_service(tmp_path)
    job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'scan.pdf'}})
    await asyncio.wait_for(peer.prompted.wait(), 10)
    running = service.get(job['id'])
    assert service.process is not None and running['startedAt'] is not None
    observations = []
    stop_worker = service._stop_worker

    async def observe_stop():
        observations.append(persisted(service, job['id']))
        await stop_worker()

    monkeypatch.setattr(service, '_stop_worker', observe_stop)
    try:
        if explicit:
            stopped = await service.cancel(job['id'])
            await service.close()
        else:
            await service.close()
            stopped = persisted(service, job['id'])
        assert observations
        assert all(bool(item.get('cancelRequestedAt')) == explicit for item in observations)
        assert all(bool(item.get('interruptedAt')) == (not explicit) for item in observations)
        assert stopped['state'] == ('cancelled' if explicit else 'interrupted')
        assert stopped['finishedAt'] is not None and stopped['stage'] is None
        assert stopped['progress'] == running['progress'] and stopped['startedAt'] == running['startedAt']
        assert stopped.get('resultId') is None
        assert peer.sessions == {}
        assert any(path.endswith('/interrupt') for _, path, _, _ in peer.calls)
        assert any(method == 'DELETE' for method, _, _, _ in peer.calls)
        reopened = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
        await reopened.start()
        try:
            await asyncio.sleep(.03)
            assert reopened.get(job['id'])['state'] == stopped['state']
            assert reopened.active is None  # Interrupted extraction is never automatically rerun.
        finally:
            await reopened.close()
    finally:
        await service.close()


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('cleanup_raises', [False, True])
async def test_unknown_stop_blocks_queued_csv_until_fenced_recovery(tmp_path, explicit, cleanup_raises):
    scanned_pdf(tmp_path / 'scan.pdf', 1)
    (tmp_path / 'queued.csv').write_text('001,2\n')
    admitted, release = asyncio.Event(), asyncio.Event()

    class Engine(FakeEngine):
        async def start(self, oid, jid, *args):
            await self.on_operation(oid, jid, {'state': 'admitted', 'sessionId': 'ses_pending',
                                             'inputId': 'msg_pending', 'settlement': {'settled': False}})
            admitted.set()
            await release.wait()
            error = ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'test cleanup is unknown')
            error.session_id = 'ses_pending'
            error.settlement = {'settled': False}
            raise error

        async def cancel(self, job_id):
            release.set()
            if cleanup_raises:
                raise OSError('cleanup transport unavailable')
            return {'settled': False}

    service = Service(tmp_path / 'state', tmp_path, engine=Engine())
    await service.start()
    reopened = None
    try:
        active, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'scan.pdf'}})
        await asyncio.wait_for(admitted.wait(), 10)
        body = {'source': {'kind': 'workspace-file', 'path': 'queued.csv'}}
        queued = [(await service.submit(str(uuid4()), body))[0] for _ in range(2)]
        if explicit:
            stopped = await service.cancel(active['id'])
            assert stopped['state'] == 'cancelling' and stopped['finishedAt'] is None
        await service.close()
        stopped = persisted(service, active['id'])
        assert stopped['state'] == 'cancelling' and stopped['finishedAt'] is None
        assert bool(stopped.get('cancelRequestedAt')) == explicit
        assert bool(stopped.get('interruptedAt')) == (not explicit)
        marker = stopped.get('cancelRequestedAt') if explicit else stopped['interruptedAt']

        engine = FakeEngine()
        engine.recovery_settled = False
        reopened = Service(tmp_path / 'state', tmp_path, engine=engine)
        await reopened.start()
        await asyncio.sleep(.03)
        assert engine.recovered == []  # No cleanup before predecessor fencing.
        assert reopened.get(active['id'])['state'] == 'cancelling'
        assert [reopened.get(job['id'])['queueAhead'] for job in queued] == [1, 2]
        assert all(reopened.get(job['id']) == job for job in queued)
        engine.descriptor['recovery'] = {'fencedGenerations': [service.generation]}
        assert not (await reopened.reconcile_engine())['settled']
        assert engine.recovered and reopened.active is None
        assert all(reopened.get(job['id'])['startedAt'] is None for job in queued)
        engine.recovery_settled = True
        assert (await reopened.reconcile_engine())['settled']
        recovered = reopened.get(active['id'])
        assert recovered['state'] == ('cancelled' if explicit else 'interrupted')
        assert recovered['finishedAt'] is not None
        assert (recovered.get('cancelRequestedAt') if explicit else recovered['interruptedAt']) == marker
        assert [reopened.get(job['id'])['queueAhead'] for job in queued] == [0, 1]
        assert [(await terminal(reopened, job))['state'] for job in queued] == ['succeeded', 'succeeded']
        assert reopened.get(queued[0]['id'])['startedAt'] < reopened.get(queued[1]['id'])['startedAt']
    finally:
        release.set()
        await service.close()
        if reopened:
            await reopened.close()


async def test_cancel_recovery_wakes_queued_successor_without_other_requests(tmp_path):
    (tmp_path / 'queued.csv').write_text('001,2\n')
    engine = FakeEngine()
    engine.recovery_settled = False
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        old, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
        queued, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'queued.csv'}})
        service.store.operation({'id': 'inactive-op', 'jobId': old['id'], 'state': 'outcome_unknown',
                                 'sessionId': 'ses_inactive', 'settlement': {'settled': False}})
        assert (await service.cancel(old['id']))['state'] == 'cancelling'
        assert service.settlement_unknown and service.active is None
        assert service.get(queued['id'])['startedAt'] is None
        await asyncio.sleep(0)  # Let the scheduler park on unresolved settlement.
        engine.recovery_settled = True
        assert (await service.cancel(old['id']))['state'] == 'cancelled'
        assert not service.settlement_unknown
        # Only status reads follow: no submit/reconcile can provide another wake.
        async with asyncio.timeout(3):
            done = await terminal(service, queued)
        assert done['state'] == 'succeeded'
    finally:
        await service.close()


@pytest.mark.parametrize('phase', ['snapshot', 'finalize'])
@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('settled', [False, True])
async def test_stop_waits_for_local_threads_even_when_cleanup_unknown(tmp_path, monkeypatch, phase, explicit, settled):
    (tmp_path / 'data.csv').write_text('001,2\n')
    entered, release = threading.Event(), threading.Event()

    class Engine(FakeEngine):
        async def cancel(self, job_id):
            return {'settled': settled}

    service = Service(tmp_path / 'state', tmp_path, engine=Engine(), lease_interval=.02, lease_seconds=2)
    await service.start()
    original = getattr(service, '_' + phase)

    def block(*args):
        entered.set()
        assert release.wait(10), 'local thread was not released'
        return original(*args)

    monkeypatch.setattr(service, '_' + phase, block)
    stopping = None
    try:
        job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        await wait_until(entered.is_set)
        service.store.operation({'id': 'local-op', 'jobId': job['id'], 'state': 'admitted',
                                 'sessionId': 'ses_local', 'settlement': {'settled': False}})
        service.engine_operations.add('local-op')
        heartbeat = json.loads((service.private / 'engine-lease.json').read_text())['heartbeat']
        stopping = asyncio.create_task(service.cancel(job['id']) if explicit else service.close())
        await asyncio.sleep(.06)
        assert not stopping.done(), 'stop returned while a local thread could still write'
        live = persisted(service, job['id'])
        assert live['state'] == 'cancelling' and live['finishedAt'] is None
        assert bool(live.get('cancelRequestedAt')) == explicit
        assert bool(live.get('interruptedAt')) == (not explicit)
        assert json.loads((service.private / 'engine-lease.json').read_text())['heartbeat'] > heartbeat
        contender = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
        with pytest.raises(ServiceError) as error:
            await contender.start()
        assert error.value.code == 'OWNER_ACTIVE'
        release.set()
        await asyncio.wait_for(stopping, 10)
        done = persisted(service, job['id'])
        assert done['state'] == (('cancelled' if explicit else 'interrupted') if settled else 'cancelling')
        assert (done['finishedAt'] is not None) == settled
        assert done.get('resultId') is None
        if phase == 'snapshot':
            assert done['source'] is None  # No retained snapshot was committed before interruption.
        if explicit:
            await wait_until(lambda: service.active is None)
    finally:
        release.set()
        if stopping:
            await asyncio.gather(stopping, return_exceptions=True)
        await service.close()


async def test_close_keeps_owner_and_callbacks_live_through_engine_close(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Engine(FakeEngine):
        async def close(self):
            entered.set()
            await release.wait()
            await self.on_operation('close-op', job['id'], {'settlement': {'settled': True}})

    service = Service(tmp_path / 'state', tmp_path, engine=Engine(), lease_interval=.02, lease_seconds=2)
    await service.start()
    job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
    await service.cancel(job['id'])
    service.store.operation({'id': 'close-op', 'jobId': job['id'], 'state': 'outcome_unknown',
                             'sessionId': 'ses_close', 'settlement': {'settled': False}})
    heartbeat = json.loads((service.private / 'engine-lease.json').read_text())['heartbeat']
    closing = asyncio.create_task(service.close())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(.06)
        assert json.loads((service.private / 'engine-lease.json').read_text())['heartbeat'] > heartbeat
        contender = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
        with pytest.raises(ServiceError, match='owner'):
            await contender.start()
        release.set()
        await asyncio.wait_for(closing, 10)
        projection = json.loads((service.private / 'engine-lease.json').read_text())
        assert projection['entries'] == []
        await contender.start()
        await contender.close()
    finally:
        release.set()
        await closing


@pytest.mark.parametrize('failure', ['projection', 'store'])
async def test_late_stop_failure_still_releases_owner_and_heartbeat(tmp_path, monkeypatch, failure):
    entered, release = asyncio.Event(), asyncio.Event()

    class Engine(FakeEngine):
        async def close(self):
            entered.set()
            await release.wait()

    engine = Engine()
    engine.recovery_settled = False
    service = Service(tmp_path / 'state', tmp_path, engine=engine, lease_interval=.02, lease_seconds=2)
    await service.start()
    store, owner = service.store, service.owner
    closing = cancelling = None
    try:
        pending, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'pending.csv'}})
        late, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'late.csv'}})
        store.operation({'id': 'pending-op', 'jobId': pending['id'], 'state': 'outcome_unknown',
                         'sessionId': 'ses_pending', 'settlement': {'settled': False}})
        assert (await service.cancel(pending['id']))['state'] == 'cancelling'
        closing = asyncio.create_task(service.close())
        await asyncio.wait_for(entered.wait(), 2)
        cancelling = asyncio.create_task(service.cancel(late['id']))
        await wait_until(lambda: persisted(service, late['id']).get('cancelRequestedAt'))
        assert not cancelling.done()
        fault = OSError('late stop persistence failed')
        target, name = (service, '_write_projection') if failure == 'projection' else (store, 'save')
        original = getattr(target, name)

        def fail_after_engine_close(*args):
            if service.engine_closed:
                raise fault
            return original(*args)

        monkeypatch.setattr(target, name, fail_after_engine_close)
        release.set()
        with pytest.raises(OSError, match='late stop persistence failed') as cancel_error:
            await asyncio.wait_for(cancelling, 3)
        assert cancel_error.value is fault
        with pytest.raises(OSError, match='late stop persistence failed'):
            await asyncio.wait_for(closing, 3)
        assert service.heartbeat_task.done(), 'failed late stop leaked the owner heartbeat'
        assert service.store is None and service.owner is None
        assert owner.closed
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            store.get(late['id'])
        contender = Service(tmp_path / 'state', tmp_path, admin_only=True)
        await contender.start()  # Proves the actual flock, not just its field, was released.
        try:
            for job in (pending, late):
                stopped = contender.get(job['id'])
                assert stopped['cancelRequestedAt'] is not None
            unresolved = contender.get(pending['id'])
            assert unresolved['state'] == 'cancelling' and unresolved['finishedAt'] is None
            assert not contender.store.get_operation('pending-op')['settlement']['settled']
            projection = json.loads((service.private / 'engine-lease.json').read_text())
            assert [entry['id'] for entry in projection['entries']] == ['pending-op']
        finally:
            await contender.close()
    finally:
        release.set()
        await asyncio.gather(*(task for task in (closing, cancelling) if task), return_exceptions=True)
        # Clean up the expected red-run leak without hiding the resource assertions.
        if service.heartbeat_task and not service.heartbeat_task.done():
            service.heartbeat_task.cancel()
            await asyncio.gather(service.heartbeat_task, return_exceptions=True)
        if service.store:
            service.store.close()
            service.store = None
        if service.owner:
            service.owner.close()
            service.owner = None


@pytest.mark.parametrize('phase', ['snapshot', 'finalize'])
async def test_interrupted_retained_snapshot_and_explicit_fresh_source_can_rerun(tmp_path, monkeypatch, phase):
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
    job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
    await wait_until(entered.is_set)
    closing = asyncio.create_task(service.close())
    await wait_until(lambda: service.stopping)
    release.set()
    await closing
    reopened = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await reopened.start()
    try:
        parent = reopened.get(job['id'])
        assert parent['state'] == 'interrupted'
        assert (parent['source'] is None) == (phase == 'snapshot')
        with pytest.raises(ServiceError, match='source is required'):
            await reopened.submit(str(uuid4()), {'rerunOf': parent['id']})
        (tmp_path / 'data.csv').write_text('003,4\n')
        sources = [{'kind': 'workspace-file', 'path': 'data.csv'}]
        if parent['source'] is not None:
            sources.insert(0, {'kind': 'snapshot', 'snapshotId': parent['source']['snapshotId']})
        for source in sources:
            child, _ = await reopened.submit(str(uuid4()), {'source': source, 'rerunOf': parent['id']})
            done = await terminal(reopened, child)
            assert done['state'] == 'succeeded' and done['id'] != parent['id']
            if parent['source'] is not None:
                assert (done['source']['sha256'] == parent['source']['sha256']) == (source['kind'] == 'snapshot')
        assert reopened.get(parent['id']) == parent
    finally:
        await reopened.close()


@pytest.mark.parametrize('seed', [7, 19, 41])
async def test_cancel_shutdown_scheduler_jitter_stress(tmp_path, seed):
    rng = random.Random(seed)
    scanned_pdf(tmp_path / 'scan.pdf', 1)
    (tmp_path / 'queued.csv').write_text('001,2\n')
    for iteration in range(8):
        admitted, release = asyncio.Event(), asyncio.Event()

        class Engine(FakeEngine):
            async def start(self, oid, jid, *args, admitted=admitted, release=release):
                await self.on_operation(oid, jid, {'state': 'admitted', 'sessionId': 'ses_jitter',
                                                 'settlement': {'settled': False}})
                admitted.set()
                await release.wait()
                raise OSError('scheduler cleanup overlaps stop')

            async def cancel(self, job_id, release=release):
                await asyncio.sleep(rng.uniform(0, .01))
                release.set()
                await asyncio.sleep(rng.uniform(0, .01))
                return {'settled': True}

        root = tmp_path / f'{seed}-{iteration}'
        service = Service(root, tmp_path, engine=Engine())
        await service.start()
        active, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'scan.pdf'}})
        await asyncio.wait_for(admitted.wait(), 10)
        queued = [(await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'queued.csv'}}))[0]
                  for _ in range(2)]

        async def cancel(service=service, active=active):
            await asyncio.sleep(rng.uniform(0, .005))
            return await service.cancel(active['id'])

        async def close(service=service):
            await asyncio.sleep(rng.uniform(0, .005))
            await service.close()

        try:
            await asyncio.wait_for(asyncio.gather(cancel(), close(), close()), 10)
            saved = persisted(service, active['id'])
            assert saved['state'] == ('cancelled' if saved.get('cancelRequestedAt') else 'interrupted')
            assert saved['finishedAt'] is not None and saved.get('resultId') is None
            assert service.owner is None and service.process is None
            reopened = Service(root, tmp_path, engine=FakeEngine())
            await reopened.start()
            try:
                assert [(await terminal(reopened, job))['state'] for job in queued] == ['succeeded', 'succeeded']
                assert reopened.get(queued[0]['id'])['startedAt'] < reopened.get(queued[1]['id'])['startedAt']
                assert reopened.get(active['id'])['state'] == saved['state']
            finally:
                await reopened.close()
        finally:
            release.set()
            await service.close()
