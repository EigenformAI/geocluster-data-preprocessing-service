import asyncio
import hashlib
import random
from uuid import uuid4

import httpx
import pytest
from fakes import FakeEngine

from preprocessing.api import create_app
from preprocessing.errors import ServiceError
from preprocessing.service import Service

BODY = {'source': {'kind': 'workspace-file', 'path': 'source.csv'}}


async def idle(service):
    while not service.stopping:
        await asyncio.sleep(.01)


async def open_service(tmp_path, monkeypatch, root='state'):
    service = Service(tmp_path / root, tmp_path, engine=FakeEngine())
    monkeypatch.setattr(service, '_schedule', lambda: idle(service))
    await service.start()
    return service


async def test_capability_scope_persists_but_generation_and_replacement_scope_rotate(tmp_path, monkeypatch):
    first = await open_service(tmp_path, monkeypatch)
    try:
        capabilities = first.capabilities()
        scope, generation = capabilities['storeScope'], capabilities['serviceGeneration']
        assert scope and generation
    finally:
        await first.close()
    second = await open_service(tmp_path, monkeypatch)
    replacement = await open_service(tmp_path, monkeypatch, 'replacement')
    try:
        assert second.capabilities()['storeScope'] == scope
        assert second.capabilities()['serviceGeneration'] != generation
        assert replacement.capabilities()['storeScope'] != scope
    finally:
        await second.close()
        await replacement.close()


async def test_scope_header_fences_all_mutations_before_effects_and_auth_precedes_scope(tmp_path, monkeypatch):
    service = await open_service(tmp_path, monkeypatch)
    app = create_app(service, token='secret')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            assert (await client.get('/preprocessing/capabilities')).status_code == 401
            client.headers['Authorization'] = 'Bearer secret'
            caps = (await client.get('/preprocessing/capabilities')).json()
            for header in (None, 'scope_replaced'):
                headers = {'Idempotency-Key': str(uuid4())}
                if header is not None:
                    headers['X-Processing-Store-Scope'] = header
                for path in ('/preprocessing/jobs', '/preprocessing/engine/reconcile',
                             '/preprocessing/jobs/absent/cancel', '/preprocessing/jobs/absent/reconcile',
                             '/preprocessing/submissions/' + str(uuid4()) + '/cancel'):
                    response = await client.post(path, headers=headers, json=BODY)
                    assert response.status_code == 409, response.text
                    assert response.json()['error']['code'] == 'PROCESSING_STORE_CHANGED'
            assert service.list_jobs()['jobs'] == []
            client.headers['X-Processing-Store-Scope'] = caps['storeScope']
            response = await client.post('/preprocessing/jobs', headers={'Idempotency-Key': str(uuid4())}, json=BODY)
            assert response.status_code == 202, response.text
    finally:
        await service.close()


async def test_absent_key_cancellation_is_durable_binds_intent_and_blocks_replay(tmp_path, monkeypatch):
    service = await open_service(tmp_path, monkeypatch)
    key = str(uuid4())
    try:
        assert await service.cancel_submission(key, BODY) == {'cancelled': True, 'job': None}
        assert await service.cancel_submission(key.upper(), BODY) == {'cancelled': True, 'job': None}
        with pytest.raises(ServiceError) as error:
            await service.cancel_submission(key, {'source': {'kind': 'workspace-file', 'path': 'other.csv'}})
        assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    finally:
        await service.close()
    service = await open_service(tmp_path, monkeypatch)
    try:
        with pytest.raises(ServiceError) as error:
            await service.submit(key, BODY)
        assert error.value.code == 'SUBMISSION_CANCELLED'
        with pytest.raises(ServiceError) as error:
            await service.submit(key, {'source': {'kind': 'workspace-file', 'path': 'other.csv'}})
        assert error.value.code == 'IDEMPOTENCY_CONFLICT'
        assert service.lookup(key) is None
        # This is a key fence, not source-based deduplication.
        assert (await service.submit(str(uuid4()), BODY))[0]['state'] == 'queued'
    finally:
        await service.close()


async def test_late_post_after_readiness_wait_cannot_cross_cancelled_key(tmp_path, monkeypatch):
    service = await open_service(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    ready = service._ready

    async def delayed():
        entered.set()
        await release.wait()
        return await ready()

    monkeypatch.setattr(service, '_ready', delayed)
    key = str(uuid4())
    submit = asyncio.create_task(service.submit(key, BODY))
    try:
        await entered.wait()
        assert await service.cancel_submission(key, BODY) == {'cancelled': True, 'job': None}
        release.set()
        with pytest.raises(ServiceError) as error:
            await submit
        assert error.value.code == 'SUBMISSION_CANCELLED'
        assert service.lookup(key) is None
    finally:
        release.set()
        await asyncio.gather(submit, return_exceptions=True)
        await service.close()


@pytest.mark.parametrize('seed', [74912, 19, 28315, 561])
async def test_keyed_cancel_submit_jitter_and_restart_preserve_single_admission(tmp_path, monkeypatch, seed):
    service = await open_service(tmp_path, monkeypatch)
    rng = random.Random(seed)
    keys = [str(uuid4()) for _ in range(40)]
    try:
        async def submit(key, delay):
            await asyncio.sleep(delay)
            try:
                return (await service.submit(key, BODY))[0]['id']
            except ServiceError as error:
                assert error.code == 'SUBMISSION_CANCELLED'
                return None

        async def cancel(key, delay):
            await asyncio.sleep(delay)
            return await service.cancel_submission(key, BODY)

        tasks = []
        for key in keys:
            tasks.extend([submit(key, rng.uniform(0, .03)), cancel(key, rng.uniform(0, .03)),
                          submit(key, rng.uniform(0, .03)), cancel(key, rng.uniform(0, .03))])
        results = await asyncio.gather(*tasks)
        for offset, key in enumerate(keys):
            ids = {results[offset * 4 + index] for index in (0, 2)} - {None}
            assert len(ids) <= 1
            job = service.lookup(key)
            assert (job is None and not ids) or (job['id'] in ids and job['state'] == 'cancelled')
        before = {key: service.lookup(key) for key in keys}
    finally:
        await service.close()
    service = await open_service(tmp_path, monkeypatch)
    try:
        for key in keys:
            if before[key] is None:
                with pytest.raises(ServiceError) as error:
                    await service.submit(key, BODY)
                assert error.value.code == 'SUBMISSION_CANCELLED'
            else:
                job, replayed = await service.submit(key, BODY)
                assert replayed and job['id'] == before[key]['id'] and job['state'] == 'cancelled'
    finally:
        await service.close()


async def test_keyed_cancel_http_contract_and_normalized_intent(tmp_path, monkeypatch):
    service = await open_service(tmp_path, monkeypatch)
    app = create_app(service, token='secret')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost', headers={
            'Authorization': 'Bearer secret', 'X-Processing-Store-Scope': service.capabilities()['storeScope'],
        }) as client:
            key = str(uuid4())
            path = '/preprocessing/submissions/' + key + '/cancel'
            for body in ({}, {'request': BODY, 'extra': True}, {'request': None}):
                assert (await client.post(path, json=body)).status_code == 400
            accepted = await client.post('/preprocessing/jobs', headers={'Idempotency-Key': key}, json=BODY)
            assert accepted.status_code == 202
            # Explicit defaults and omitted defaults are the same normalized intent.
            request = {**BODY, 'profileId': 'default', 'correlation': {'uploadBatchId': None, 'uploadRecordId': None},
                       'rerunOf': None, 'resumeOf': None, 'tableRecovery': None}
            stopped = await client.post(path, json={'request': request})
            assert stopped.status_code == 200
            assert stopped.json()['cancelled'] is True
            assert stopped.json()['job']['state'] == 'cancelled'
            assert stopped.json()['job']['id'] == accepted.json()['id']
            request['source'] = {'kind': 'workspace-file', 'path': 'other.csv'}
            conflict = await client.post(path, json={'request': request})
            assert conflict.status_code == 409
            assert conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
            absent = await client.post('/preprocessing/submissions/' + str(uuid4()) + '/cancel', json={'request': BODY})
            assert absent.json() == {'cancelled': True, 'job': None}
    finally:
        await service.close()


async def test_keyed_cancel_retains_unknown_supplier_settlement_and_survives_restart(tmp_path, monkeypatch):
    service = await open_service(tmp_path, monkeypatch)
    key = str(uuid4())
    try:
        job, _ = await service.submit(key, BODY)
        service.store.operation({'id': 'op_uncertain', 'jobId': job['id'], 'generation': service.generation,
                                 'sessionId': 'ses_supplier', 'inputId': 'msg_supplier',
                                 'state': 'outcome_unknown', 'settlement': {'settled': False}})
        service.engine.recovery_settled = False
        response = await service.cancel_submission(key, BODY)
        assert response['cancelled'] and response['job']['state'] == 'cancelling'
        assert service.store.get_operation('op_uncertain')['settlement']['settled'] is False
        assert response['job']['cancelRequestedAt']
        generation = service.generation
    finally:
        await service.close()
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    service.engine.descriptor['recovery'] = {'fencedGenerations': [generation], 'deadEngineInstances': []}
    service.engine.recovery_settled = False
    monkeypatch.setattr(service, '_schedule', lambda: idle(service))
    await service.start()
    try:
        assert service.lookup(key)['state'] == 'cancelling'
        assert (await service.cancel_submission(key, BODY))['job']['state'] == 'cancelling'
        service.engine.recovery_settled = True
        response = await service.cancel_submission(key, BODY)
        assert response['job']['state'] == 'cancelled'
        assert service.store.get_operation('op_uncertain')['settlement']['settled'] is True
    finally:
        await service.close()


async def test_expected_digest_still_binds_path_when_snapshot_happens_after_admission(tmp_path, monkeypatch):
    source = tmp_path / 'source.csv'
    original = b'code,value\n001,1.2300\n'
    source.write_bytes(original)
    service = await open_service(tmp_path, monkeypatch)
    try:
        key = str(uuid4())
        body = {'source': {**BODY['source'], 'expectedSha256': hashlib.sha256(original).hexdigest()}}
        job, _ = await service.submit(key, body)
        replacement = tmp_path / 'replacement.csv'
        replacement.write_bytes(b'code,value\n002,99\n')
        replacement.replace(source)
        replay, replayed = await service.submit(key, body)
        assert replayed and replay['id'] == job['id']
        with pytest.raises(ServiceError) as error:
            await service._run(job['id'])
        assert error.value.code == 'SOURCE_CHANGED'
        assert not list((service.private / 'sources').iterdir())
        assert service.get(job['id'])['source'] is None
        source.write_bytes(original)
        snapshot = service._snapshot(job)
        assert snapshot['sha256'] == hashlib.sha256(original).hexdigest()
        assert (service.private / 'sources' / snapshot['snapshotId'] / 'source').read_bytes() == original
        source.write_bytes(b'newer path bytes')
        retained_job = {**job, 'requestedSource': {'kind': 'snapshot', 'snapshotId': snapshot['snapshotId']}}
        assert service._snapshot(retained_job, snapshot) == snapshot
    finally:
        await service.close()
