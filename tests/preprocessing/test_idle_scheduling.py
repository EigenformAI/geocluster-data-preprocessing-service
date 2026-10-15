"""Restart-safe, never-started checkpoints and activation authority."""
import asyncio
import hashlib
import json
import os
import random
import threading
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from fakes import FakeEngine

from preprocessing.api import create_app
from preprocessing.contracts import default_profile
from preprocessing.errors import ServiceError
from preprocessing.service import Service


async def suspend_scheduler(service):
    service.scheduler.cancel()
    await asyncio.gather(service.scheduler, return_exceptions=True)
    service.scheduler = None


@asynccontextmanager
async def owner(tmp_path, capacity=100):
    (tmp_path / 'input.csv').write_text('id,value\n001,original\n')
    profile = default_profile()
    profile['limits']['queueDepth'] = capacity
    service = Service(tmp_path / 'state', tmp_path, profile, engine=FakeEngine())
    await service.start()
    await suspend_scheduler(service)
    try:
        yield service
    finally:
        await service.close()


async def submit(service):
    return (await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'input.csv'}}))[0]


def park_request(job, checkpoint=None):
    checkpoint = checkpoint or 'checkpoint_' + uuid4().hex
    return {'requestId': str(uuid4()), 'expectedRevision': job['scheduling']['revision'],
            'activationRevision': job['scheduling']['activationRevision'], 'checkpointId': checkpoint,
            'block': {'reason': 'dependency-unavailable', 'episodeId': str(uuid4()), 'phase': 'before-extraction',
                      'failedAttempts': 2, 'automaticRetriesRemaining': 1,
                      'nextEligibleAt': '2026-10-14T14:00:00+00:00', 'unblock': 'due', 'checkpointId': checkpoint}}


def activate_request(job):
    return {'requestId': str(uuid4()), 'expectedRevision': job['scheduling']['revision'],
            'activationRevision': job['scheduling']['activationRevision'] + 1,
            'checkpointId': job['scheduling']['block']['checkpointId']}


async def test_queued_acceptance_captures_exact_input_and_survives_restart(tmp_path):
    async with owner(tmp_path) as service:
        job = await submit(service)
        assert job['source'] is not None, 'durable queued acceptance must already retain exact bytes'
        original = (tmp_path / 'input.csv').read_bytes()
        assert job['source']['sha256'] == hashlib.sha256(original).hexdigest()
        (tmp_path / 'input.csv').write_text('replacement bytes')
        root = service.root
    engine = FakeEngine()
    engine.available = False
    reopened = Service(root, tmp_path, engine=engine)
    await reopened.start()
    try:
        saved = reopened.get(job['id'])
        assert saved == job
        engine.available = True
        reopened.wake.set()
        async with asyncio.timeout(10):
            while reopened.get(job['id'])['state'] == 'queued' or reopened.active:
                await asyncio.sleep(.01)
        assert reopened.get(job['id'])['state'] == 'succeeded'
        assert reopened.get_result(job['id'])['source']['sha256'] == hashlib.sha256(original).hexdigest()
    finally:
        await reopened.close()


async def test_capture_failure_never_accepts_queued_job(tmp_path):
    async with owner(tmp_path) as service:
        (tmp_path / 'input.csv').unlink()
        with pytest.raises(ServiceError) as failure:
            await submit(service)
        assert failure.value.code == 'SOURCE_NOT_FOUND'
        assert service.list_jobs()['jobs'] == []
        assert service.store.active_count() == 0


async def test_park_releases_capacity_duplicate_receipts_survive_restart_and_cancel_retains_source(tmp_path):
    async with owner(tmp_path, capacity=1) as service:
        job = await submit(service)
        request = park_request(job)
        parked = await service.park(job['id'], request)
        assert parked['state'] == 'queued'
        assert parked['scheduling']['class'] == 'parked'
        assert parked['scheduling']['block'] == request['block']
        assert parked['receipt']['quiescent'] is True
        assert parked['receipt']['source'] == job['source']
        assert service.store.active_count() == 0
        assert service.store.next_queued() is None
        assert await service.park(job['id'], request) == parked
        other = await submit(service)
        assert service.store.next_queued()['id'] == other['id']
        activation = activate_request(parked)
        full = await service.activate(job['id'], activation)
        assert full['scheduling'] == parked['scheduling']
        assert full['receipt']['outcome'] == 'capacity-unavailable'
        service.scheduler = asyncio.create_task(service._schedule())
        async with asyncio.timeout(10):
            while service.get(other['id'])['state'] in {'queued', 'running'} or service.active:
                await asyncio.sleep(.01)
        assert service.get(other['id'])['state'] == 'succeeded'
        assert service.get(job['id'])['scheduling']['class'] == 'parked'
        await suspend_scheduler(service)
        # A denied request is a durable receipt; a new capacity event uses a new UUID.
        assert await service.activate(job['id'], activation) == full
        activation['requestId'] = str(uuid4())
        activated = await service.activate(job['id'], activation)
        assert activated['scheduling']['class'] == 'runnable'
        assert activated['scheduling']['block'] == request['block']
        assert activated['scheduling']['activationRevision'] == activation['activationRevision']
        assert await service.park(job['id'], request) == parked
        request2 = park_request(activated)
        parked2 = await service.park(job['id'], request2)
        assert await service.activate(job['id'], activation) == activated
        root = service.root
        source = service.private / 'sources' / job['source']['snapshotId'] / 'source'
        old = time.time() - 10 * 86400
        os.utime(source.parent, (old, old))
        (tmp_path / 'input.csv').unlink()
    reopened = Service(root, tmp_path, engine=FakeEngine())
    await reopened.start()
    try:
        assert reopened.get(job['id'])['scheduling'] == parked2['scheduling']
        assert await reopened.park(job['id'], request) == parked
        assert await reopened.activate(job['id'], activation) == activated
        cancelled = await reopened.cancel(job['id'])
        assert cancelled['state'] == 'cancelled'
        assert cancelled['scheduling']['class'] == 'terminal'
        assert source.read_bytes() == b'id,value\n001,original\n'
    finally:
        await reopened.close()


async def test_stale_mismatched_and_uncertain_controls_cannot_create_effects(tmp_path):
    async with owner(tmp_path) as service:
        job = await submit(service)
        request = park_request(job)
        service.store.operation({'id': 'uncertain', 'jobId': job['id'], 'state': 'outcome_unknown',
                                 'settlement': {'settled': False}})
        with pytest.raises(ServiceError) as failure:
            await service.park(job['id'], request)
        assert failure.value.code == 'PARK_UNSAFE'
        operation = service.store.get_operation('uncertain')
        operation['settlement'] = {'settled': True}
        service.store.operation(operation)
        # Settlement proves the old effect ended, not a supported continuation cursor.
        with pytest.raises(ServiceError) as failure:
            await service.park(job['id'], park_request(service.get(job['id'])))
        assert failure.value.code == 'PARK_UNSAFE'
        await service.cancel(job['id'])
        job = await submit(service)
        parked = await service.park(job['id'], park_request(service.get(job['id'])))
        for patch in ({'expectedRevision': 0}, {'activationRevision': 1}, {'checkpointId': 'wrong'}):
            with pytest.raises(ServiceError) as failure:
                await service.activate(job['id'], {**activate_request(parked), **patch})
            assert failure.value.status == 409
        assert service.get(job['id'])['scheduling'] == parked['scheduling']
        changed = {**request, 'checkpointId': 'different', 'block': {**request['block'], 'checkpointId': 'different'}}
        # Successful request UUIDs cannot be rebound, even after subsequent transitions.
        successful = park_request(await submit(service))
        await service.park(service.store.next_queued()['id'], successful)
        with pytest.raises(ServiceError) as failure:
            await service.park(job['id'], {**changed, 'requestId': successful['requestId']})
        assert failure.value.code == 'IDEMPOTENCY_CONFLICT'


async def test_park_wins_during_readiness_and_old_selection_cannot_run_new_activation(tmp_path, monkeypatch):
    async with owner(tmp_path) as service:
        job = await submit(service)
        entered, release, ran = asyncio.Event(), asyncio.Event(), asyncio.Event()
        selected = []

        async def ready(profile=None):
            entered.set()
            await release.wait()
            return {'ready': True}

        async def run(job_id, activation_revision):
            selected.append((job_id, activation_revision))
            current = service.get(job_id)
            current.update(state='succeeded', finishedAt='done')
            service.store.save(current)
            ran.set()

        monkeypatch.setattr(service.engine, 'ready', ready)
        monkeypatch.setattr(service, '_run', run)
        service.scheduler = asyncio.create_task(service._schedule())
        await asyncio.wait_for(entered.wait(), 2)
        parked = await service.park(job['id'], park_request(job))
        await service.activate(job['id'], activate_request(parked))
        release.set()
        await asyncio.wait_for(ran.wait(), 2)
        assert selected == [(job['id'], 2)]


@pytest.mark.parametrize('seed', [7, 83, 20261014])
async def test_seeded_park_selection_and_activation_races(tmp_path, monkeypatch, seed):
    rng = random.Random(seed)
    async with owner(tmp_path) as service:
        effects = []
        wins = {'park': 0, 'selection': 0}

        async def jitter_ready(profile=None):
            await asyncio.sleep(rng.uniform(0, .015))
            return {'ready': True}

        async def run(job_id, activation_revision):
            async with service.lock:
                current = service.get(job_id)
                assert current['scheduling']['class'] == 'runnable', (seed, current)
                assert current['scheduling']['activationRevision'] == activation_revision
                assert service.active == job_id
                current.update(state='running', startedAt='claimed')
                service.store.save(current)
                effects.append((job_id, activation_revision))
            await asyncio.sleep(rng.uniform(0, .003))
            current = service.get(job_id)
            current.update(state='succeeded', finishedAt='done')
            service.store.save(current)

        async def delayed(call, delay):
            await asyncio.sleep(delay)
            try:
                return await call()
            except ServiceError as error:
                return error

        monkeypatch.setattr(service, '_run', run)
        monkeypatch.setattr(service.engine, 'ready', jitter_ready)
        for iteration in range(30):
            job = await submit(service)
            request = park_request(job)
            service.scheduler = asyncio.create_task(service._schedule())
            result = await delayed(lambda job=job, request=request: service.park(job['id'], request), rng.uniform(0, .015))
            if isinstance(result, ServiceError):
                wins['selection'] += 1
                assert result.code in {'PARK_UNSAFE', 'STALE_SCHEDULING_REVISION'}, (seed, iteration, result.code)
            else:
                wins['park'] += 1
                assert not any(identity == job['id'] for identity, _ in effects)
                activation = activate_request(result)
                responses = await asyncio.gather(*(delayed(lambda job=job, activation=activation: service.activate(job['id'], activation),
                                                          rng.uniform(0, .003)) for _ in range(4)))
                assert responses.count(responses[0]) == 4
            async with asyncio.timeout(2):
                while service.get(job['id'])['state'] != 'succeeded' or service.active:
                    await asyncio.sleep(.001)
            assert sum(identity == job['id'] for identity, _ in effects) == 1, (seed, iteration, effects)
            await suspend_scheduler(service)
        assert all(wins.values()), (seed, wins)


async def test_authenticated_passive_activity_and_control_wire(tmp_path):
    async with owner(tmp_path) as service:
        job = await submit(service)
        app = create_app(service, token='private-token')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            assert (await client.get('/preprocessing/activity')).status_code == 401
            client.headers['Authorization'] = 'Bearer private-token'
            before = json.dumps(list(service.store.all()), sort_keys=True)
            first = await client.get('/preprocessing/activity')
            assert first.status_code == 200
            assert (await client.get('/preprocessing/activity')).json() == first.json()
            assert first.json()['contractVersion'] == 1
            assert first.json()['runnable'] == 1 and first.json()['parked'] == 0
            assert before == json.dumps(list(service.store.all()), sort_keys=True)
            path = f'/preprocessing/jobs/{job["id"]}'
            parked = await client.post(path + '/park', json=park_request(job))
            assert parked.status_code == 200
            snapshot = (await client.get('/preprocessing/activity')).json()
            assert snapshot['incarnation'] == first.json()['incarnation']
            assert snapshot['revision'] > first.json()['revision']
            assert snapshot['runnable'] == snapshot['settling'] == 0 and snapshot['parked'] == 1
            assert snapshot['retainedInputs']['parkedBytes'] == job['source']['bytes']
            assert 'original' not in json.dumps(snapshot)
            assert (await client.post(path + '/activate', json=activate_request(parked.json()))).status_code == 200
            contract = (await client.get('/preprocessing/capabilities')).json()['scheduling']
            assert contract['contractVersion'] == 1
            assert contract['checkpointContracts'] == ['queued-input-v1']
            assert contract['activeContinuation'] is False


async def test_submission_activation_header_preserves_identity_on_replay(tmp_path):
    async with owner(tmp_path) as service:
        app = create_app(service, token='private-token')
        body = {'source': {'kind': 'workspace-file', 'path': 'input.csv'}}
        key = str(uuid4())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost',
                                     headers={'Authorization': 'Bearer private-token'}) as client:
            first = await client.post('/preprocessing/jobs', json=body,
                                      headers={'Idempotency-Key': key, 'x-gc-activation-revision': '3'})
            assert first.status_code == 202
            job = first.json()
            assert job['scheduling']['activationRevision'] == 3
            (tmp_path / 'input.csv').write_text('replacement input is not the accepted source')
            for activation in ('1', '4'):
                replay = await client.post('/preprocessing/jobs', json=body,
                                           headers={'Idempotency-Key': key, 'x-gc-activation-revision': activation})
                assert replay.status_code == 200
                assert replay.json() == job
            parked = await service.park(job['id'], park_request(job))
            resumed = await service.activate(job['id'], activate_request(parked))
            assert resumed['scheduling']['activationRevision'] == 4
            assert len(service.list_jobs()['jobs']) == 1
            assert resumed['source'] == job['source']


@pytest.mark.parametrize('activation', ['0', '-1', '1.0', '1e2', ' 3', '3 ', '9007199254740992',
                                         '99999999999999999', '1, 2', ''])
async def test_submission_rejects_invalid_activation_header_before_acceptance(tmp_path, activation):
    async with owner(tmp_path) as service:
        app = create_app(service, token='private-token')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost',
                                     headers={'Authorization': 'Bearer private-token'}) as client:
            response = await client.post('/preprocessing/jobs', json={'source': {'kind': 'workspace-file', 'path': 'input.csv'}},
                                         headers={'Idempotency-Key': str(uuid4()), 'x-gc-activation-revision': activation})
            assert response.status_code == 400
            assert response.json()['error']['code'] == 'INVALID_REQUEST'
            assert service.list_jobs()['jobs'] == []


async def test_submission_rejects_duplicate_activation_headers(tmp_path):
    async with owner(tmp_path) as service:
        app = create_app(service, token='private-token')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost',
                                     headers={'Authorization': 'Bearer private-token'}) as client:
            response = await client.post('/preprocessing/jobs', json={'source': {'kind': 'workspace-file', 'path': 'input.csv'}},
                                         headers=[('Idempotency-Key', str(uuid4())),
                                                  ('x-gc-activation-revision', '2'), ('x-gc-activation-revision', '3')])
            assert response.status_code == 400
            assert response.json()['error']['code'] == 'INVALID_REQUEST'
            assert service.list_jobs()['jobs'] == []


async def test_checkpoint_validation_allows_lease_renewal_and_rechecks_selection(tmp_path, monkeypatch):
    async with owner(tmp_path) as service:
        job = await submit(service)
        entered, release = threading.Event(), threading.Event()
        original = service._validate_capture

        def slow_validation(current):
            entered.set()
            assert release.wait(2)
            original(current)

        monkeypatch.setattr(service, '_validate_capture', slow_validation)
        task = asyncio.create_task(service.park(job['id'], park_request(job)))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            # Disk validation must not starve lease renewals or serialize unrelated effects.
            try:
                snapshot = await asyncio.wait_for(service.activity(), .1)
            except TimeoutError:
                pytest.fail('checkpoint hashing held the admission/heartbeat lock')
            assert snapshot['runnable'] == 1
            async with service.lock:
                service.active = job['id']  # selection wins during validation
            release.set()
            with pytest.raises(ServiceError) as failure:
                await task
            assert failure.value.code == 'PARK_UNSAFE'
            assert service.get(job['id'])['scheduling']['class'] == 'runnable'
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            service.active = None


async def test_missing_capture_restart_is_visible_and_never_substitutes_source(tmp_path):
    async with owner(tmp_path) as service:
        job = await submit(service)
        parked = await service.park(job['id'], park_request(job))
        source = service.private / 'sources' / job['source']['snapshotId'] / 'source'
        source.unlink()
        (tmp_path / 'input.csv').write_text('new path bytes cannot replace a capture')
        root = service.root
    reopened = Service(root, tmp_path, engine=FakeEngine())
    await reopened.start()
    try:
        current = reopened.get(job['id'])
        assert current['state'] == 'interrupted'
        assert current['error']['code'] == 'CHECKPOINT_INVALID'
        assert current['source'] == job['source']
        with pytest.raises(ServiceError):
            await reopened.activate(job['id'], activate_request(parked))
    finally:
        await reopened.close()


async def test_activity_protects_terminal_publication_and_unknown_effects(tmp_path):
    async with owner(tmp_path) as service:
        job = await submit(service)
        current = service.get(job['id'])
        current.update(state='succeeded', resultId='retained-result', documents={'state': 'failed'})
        service.store.save(current)
        assert (await service.activity())['runnable'] == 1
        current.update(documents={'state': 'ready'})
        service.store.save(current)
        service.active = job['id']  # result commit can precede final catalog I/O
        try:
            assert (await service.activity())['settling'] == 1
        finally:
            service.active = None
        service.store.operation({'id': 'unknown-terminal', 'jobId': job['id'], 'state': 'outcome_unknown',
                                 'settlement': {'settled': False}})
        snapshot = await service.activity()
        assert snapshot['settling'] == 1 and snapshot['unsettledOperations'] == 1
        assert service.get(job['id'])['scheduling']['class'] == 'settling'


async def test_recovered_effect_on_queued_record_never_becomes_automatic_continuation(tmp_path):
    async with owner(tmp_path) as service:
        job = await submit(service)
        service.store.operation({'id': 'crash-effect', 'jobId': job['id'], 'state': 'outcome_unknown',
                                 'settlement': {'settled': False}})
        root = service.root
        generation = service.generation
    engine = FakeEngine()
    engine.available = False
    engine.descriptor['recovery'] = {'fencedGenerations': [generation]}
    reopened = Service(root, tmp_path, engine=engine)
    await reopened.start()
    try:
        assert engine.recovered[0]['id'] == 'crash-effect'
        current = reopened.get(job['id'])
        assert current['state'] == 'interrupted'
        assert current['scheduling']['class'] == 'terminal'
        assert current['error']['code'] == 'RUNTIME_INTERRUPTED'
        assert current['source'] == job['source']
        assert reopened.store.next_queued() is None
    finally:
        await reopened.close()


async def test_terminal_extraction_with_unsettled_effect_still_reserves_capacity(tmp_path):
    async with owner(tmp_path, capacity=1) as service:
        job = await submit(service)
        current = service.get(job['id'])
        current['state'] = 'cancelled'
        service.store.save(current)
        operation = {'id': 'still-resident', 'jobId': job['id'], 'state': 'outcome_unknown',
                     'settlement': {'settled': False}}
        service.store.operation(operation)
        assert (await service.activity())['runnableCapacity']['occupied'] == 1
        with pytest.raises(ServiceError) as failure:
            await submit(service)
        assert failure.value.code == 'QUEUE_FULL'
        operation['settlement'] = {'settled': True}
        service.store.operation(operation)
        assert (await service.activity())['runnableCapacity']['occupied'] == 0
        await submit(service)
