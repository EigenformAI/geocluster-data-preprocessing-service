"""Pinned v2 HTTP peer, including acknowledgement-loss and persistence barriers."""
import asyncio
import base64
import copy
import hashlib

import httpx
import pytest
from engine_peer import DENY, MODEL, Peer, default_profile

from preprocessing.engine import OpenCodeServerEngine, _Operation
from preprocessing.engine_evidence import operation_marker
from preprocessing.errors import ServiceError


@pytest.fixture
def make(tmp_path):
    def factory(fault='', bind=True, descriptor_loader=None):
        peer = Peer(tmp_path, fault)
        descriptor = {'serverUrl': 'http://localhost:1234', 'password': 'private',
            'version': '2.0.19', 'engineInstanceId': 'engine-1', 'mode': 'standalone',
            'owner': 'test-owner', 'directory': str(tmp_path), 'contextVerified': True,
            'recovery': {'managed': False, 'extractionExcluded': True,
                         'fencedGenerations': ['generation-old'], 'deadEngineInstances': []}}
        client = httpx.AsyncClient(transport=httpx.MockTransport(peer.handle))
        engine = OpenCodeServerEngine(descriptor, input_roots=[tmp_path], rpc_seconds=.2,
                                      cancel_seconds=.2, client=client,
                                      descriptor_loader=descriptor_loader)
        if bind:
            engine.bind(peer.persist)
        return engine, peer

    return factory


async def start(engine, profile=None, refs=None, job='job'):
    return await engine.start('op-' + job, job, 'generation-1', profile or default_profile(),
                              refs or [], {'prompt': 'Read this.'})


async def test_persistence_before_create_dispatch_and_evidence_before_delete(make):
    engine, peer = make()
    result = await start(engine)
    assert result['text'] == 'transcribed 42'
    assert result['returnedModel'] == 'opencode/space-bunny-free'
    assert result['usage']['tokens']['input'] == 17
    assert [p['state'] for p in peer.patches] == [
        'prepared', 'prepared', 'dispatch_intent', 'admitted', 'evidence', 'cleanup_pending', 'settled']
    assert peer.patches[0]['mutationAttempted'] is False
    assert peer.patches[1]['mutationAttempted'] is True
    prepared = peer.patches[0]
    assert prepared['sessionId'].startswith('ses_') and prepared['inputId'].startswith('msg_')
    assert prepared['settlement']['settled'] is False
    assert peer.patches[-1]['settlement']['sessionClosed'] is True
    assert not peer.sessions
    await engine.close()


@pytest.mark.parametrize('fault', ['create-lost', 'prompt-lost'])
async def test_lost_ack_readback_never_replays_mutations(make, fault):
    engine, peer = make(fault)
    assert (await start(engine))['text'] == 'transcribed 42'
    assert len([c for c in peer.calls if c[:2] == ('POST', '/api/session')]) == 1
    assert len([c for c in peer.calls if c[1].endswith('/prompt')]) == 1
    await engine.close()


async def test_lost_create_absence_retains_binding_and_blocks_next(make):
    engine, peer = make('create-absent')
    with pytest.raises(ServiceError) as error:
        await start(engine)
    assert error.value.settlement['settled'] is False
    with pytest.raises(ServiceError):
        await start(engine, job='next')
    assert len([c for c in peer.calls if c[:2] == ('POST', '/api/session')]) == 1
    assert (await engine.cancel('job'))['settled'] is False
    await engine.close()


async def test_selection_mismatch_never_prompts(make):
    engine, peer = make('selection')
    with pytest.raises(ServiceError) as error:
        await start(engine)
    assert error.value.code == 'PROVIDER_ISOLATION_FAILED'
    assert not peer.prompted.is_set()
    assert error.value.settlement['settled'] is True
    assert peer.sessions == {}
    await engine.close()


async def test_observed_input_survives_malformed_evidence_and_empty_cleanup(make, monkeypatch):
    engine, peer = make('prompt-lost')
    transcript = peer.transcript
    reads = 0

    def evidence(sid):
        nonlocal reads
        reads += 1
        return [transcript(sid)[0], {'type': 'assistant', 'content': None}] if reads == 1 else []

    monkeypatch.setattr(peer, 'transcript', evidence)
    with pytest.raises(ServiceError) as error:
        await start(engine)
    assert (error.value.code, error.value.status, error.value.retry) == (
        'PROVIDER_ISOLATION_FAILED', 503, 'operator-action')
    assert error.value.settlement['settled'] is True
    assert peer.sessions == {}
    assert len([c for c in peer.calls if c[1].endswith('/prompt')]) == 1
    await engine.close()


async def test_idle_marker_without_completed_assistant_is_not_success(make):
    engine, peer = make('idle-only')
    profile = default_profile()
    profile['limits']['modelSeconds'] = .03
    with pytest.raises(ServiceError) as error:
        await start(engine, profile)
    assert error.value.code == 'PROVIDER_OUTCOME_UNKNOWN'
    assert len([c for c in peer.calls if c[1].endswith('/prompt')]) == 1
    await engine.close()


async def test_cancel_only_owned_input_and_session_preserves_other_chat(make):
    engine, peer = make('hang')
    peer.sessions['ses_chat'] = {'id': 'ses_chat', 'text': 'keep me'}
    task = asyncio.create_task(start(engine))
    await peer.prompted.wait()
    settlement = await engine.cancel('job')
    assert settlement['settled'] is True and settlement['sessionClosed'] is True
    with pytest.raises(ServiceError) as error:
        await task
    assert error.value.code == 'RUNTIME_INTERRUPTED'
    assert peer.sessions == {'ses_chat': {'id': 'ses_chat', 'text': 'keep me'}}
    assert any('/inbox/msg_' in c[1] for c in peer.calls)
    await engine.close()


@pytest.mark.parametrize('fault', ['', 'evidence-persistence'])
async def test_persistence_absent_or_failed_prevents_unsafe_mutations(make, fault):
    engine, peer = make(fault, bind=bool(fault))
    with pytest.raises(ServiceError):
        await start(engine)
    if not fault:
        assert not peer.calls
    else:
        assert not any(c[0] == 'DELETE' for c in peer.calls)
        assert peer.sessions
    await engine.close()


async def test_unowned_child_prevents_recursive_delete(make):
    engine, peer = make('child')
    with pytest.raises(ServiceError):
        await start(engine)
    assert not any(c[0] == 'DELETE' and '/inbox/' not in c[1] for c in peer.calls)
    await engine.close()


async def test_readiness_checks_all_roles_and_image_capability_without_prompt(make):
    engine, peer = make()
    assert (await engine.ready(default_profile()))['ready'] is True
    profile = default_profile()
    profile['models']['text'] = 'missing/model'
    assert (await engine.ready(profile))['ready'] is False
    assert not any(c[0] != 'GET' for c in peer.calls)
    assert all(c[3]['location[directory]'] == peer.directory for c in peer.calls if c[1] in ('/api/model', '/api/agent'))
    await engine.close()


async def test_image_hash_and_size_verified_before_network(make, tmp_path):
    from PIL import Image
    image = tmp_path / 'image.png'
    Image.new('RGB', (3, 3), 'white').save(image)
    data = image.read_bytes()
    ref = {'path': str(image), 'mediaType': 'image/png', 'bytes': len(data),
           'sha256': hashlib.sha256(data).hexdigest()}
    for bad in ({'bytes': len(data) + 1}, {'sha256': '0' * 64}, {'path': str(tmp_path / '../bad.png')}):
        engine, peer = make()
        with pytest.raises(ServiceError) as error:
            await start(engine, refs=[{**ref, **bad}])
        assert error.value.code == 'INPUT_LIMIT' and not peer.calls
        await engine.close()
    engine, peer = make()
    await start(engine, refs=[ref])
    prompt = next(c[2] for c in peer.calls if c[1].endswith('/prompt'))
    assert prompt['files'][0]['uri'].startswith('data:image/png;base64,')
    await engine.close()


async def test_recovery_requires_fencing_and_never_replays(make):
    engine, peer = make('create-absent')
    with pytest.raises(ServiceError):
        await start(engine)
    record = peer.records['op-job']
    report = await engine.recover([record])
    assert report['settled'] is False and 'fenc' in report['reason'].lower()
    assert len([c for c in peer.calls if c[0] == 'POST']) == 1
    await engine.close()


async def test_replaced_engine_requires_confirmed_death_and_exclusion(make):
    engine, peer = make('create-absent')
    with pytest.raises(ServiceError):
        await start(engine)
    record = peer.records['op-job']
    record['engineInstanceId'] = 'engine-old'
    assert not (await engine.recover([record]))['settled']
    engine.descriptor['recovery']['deadEngineInstances'].append('engine-old')
    report = await engine.recover([record])
    assert report['settled'] is True
    assert report['operations'][0]['settlement']['remoteOutcome'] == 'unknown'
    assert not any(c[1].endswith('/prompt') for c in peer.calls)
    await engine.close()


async def test_prepare_commit_is_a_real_async_barrier(make):
    engine, peer = make()
    entered, committed = asyncio.Event(), asyncio.Event()

    async def persist(oid, jid, patch):
        if patch['state'] == 'prepared':
            entered.set()
            await committed.wait()
        await peer.persist(oid, jid, patch)

    engine.bind(persist)
    task = asyncio.create_task(start(engine))
    await entered.wait()
    assert not any(c[0] != 'GET' for c in peer.calls)
    committed.set()
    assert (await task)['text'] == 'transcribed 42'
    await engine.close()


async def test_prepare_persistence_failure_has_no_mutation(make):
    engine, peer = make()

    async def persist(*args):
        raise OSError('SQLite unavailable')

    engine.bind(persist)
    with pytest.raises(ServiceError) as error:
        await start(engine)
    assert error.value.settlement['settled'] is False
    assert engine._known['op-job'].record['mutationAttempted'] is False
    assert not any(c[0] != 'GET' for c in peer.calls)
    await engine.close()


async def test_vision_readiness_requires_image_capability(make):
    engine, peer = make('no-image')
    assert not (await engine.ready(default_profile()))['ready']
    assert not peer.prompted.is_set()
    await engine.close()


async def test_later_permission_allow_override_is_not_deny_all(make):
    engine, peer = make('permission-override')
    assert not (await engine.ready(default_profile()))['ready']
    assert not peer.prompted.is_set()
    await engine.close()


async def test_fenced_recovery_preserves_terminal_evidence_then_cleans(make):
    engine, peer = make('unsettled')
    profile = default_profile()
    profile['limits']['modelSeconds'] = .02
    with pytest.raises(ServiceError) as error:
        await start(engine, profile)
    assert not error.value.settlement['settled']
    # Start attempts cleanup once: a lost interrupt ack does not trigger retry.
    assert len([c for c in peer.calls if c[1].endswith('/interrupt')]) == 1
    record = copy.deepcopy(peer.records['op-job'])
    peer.fault = ''
    engine.descriptor['recovery']['fencedGenerations'].append(record['generation'])
    report = await engine.recover([record])
    assert report['settled']
    assert report['operations'][0]['evidence']['text'] == 'transcribed 42'
    assert report['operations'][0]['settlement']['sessionClosed']
    assert len([c for c in peer.calls if c[1].endswith('/prompt')]) == 1
    await engine.close()


async def test_invalid_recovery_binding_blocks_fresh_admission(make):
    engine, peer = make()
    report = await engine.recover([{'id': 'broken', 'settlement': {'settled': False}}])
    assert not report['settled']
    assert not (await engine.ready(default_profile()))['ready']
    with pytest.raises(ServiceError):
        await start(engine)
    assert not any(c[0] != 'GET' for c in peer.calls)
    await engine.close()


async def test_output_limit_retains_session_instead_of_truncating_success(make):
    engine, peer = make()
    profile = default_profile()
    profile['limits']['responseBytes'] = 3
    with pytest.raises(ServiceError) as error:
        await start(engine, profile)
    assert error.value.code == 'OUTPUT_INVALID'
    assert not error.value.settlement['settled']
    assert peer.sessions and not any(c[0] == 'DELETE' for c in peer.calls)
    await engine.close()


async def test_image_symlink_and_pixel_budget_rejected_before_network(make, tmp_path):
    from PIL import Image
    image = tmp_path / 'bounded.png'
    Image.new('RGB', (30, 30), 'white').save(image)
    link = tmp_path / 'link.png'
    link.symlink_to(image)
    data = image.read_bytes()
    profile = default_profile()
    profile['limits']['pixels'] = 800
    for path in (image, link):
        engine, peer = make()
        with pytest.raises(ServiceError) as error:
            await start(engine, profile, [{'path': str(path), 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest(), 'mediaType': 'image/png'}])
        assert error.value.code == 'INPUT_LIMIT' and not peer.calls
        await engine.close()


def pending_record(engine, oid='op-old', sid='ses_old'):
    return {'id': oid, 'jobId': 'job', 'generation': 'generation-old',
        'engineInstanceId': engine.descriptor['engineInstanceId'], 'sessionId': sid,
        'inputId': 'msg_' + oid.replace('-', '_'), 'requestHash': 'a' * 64,
        'agent': 'preprocessing', 'model': MODEL,
        'location': {'directory': engine.descriptor['directory']},
        'state': 'outcome_unknown', 'dispatchIntent': True, 'settlement': {'settled': False}}


def cleanup_receipt(record):
    return {key: record[key] for key in ('jobId', 'generation', 'engineInstanceId', 'sessionId', 'inputId', 'requestHash')} | {
        'operationId': record['id'],
        'evidence': {'text': 'owner recovered 42', 'usage': {'tokens': {'input': 19}},
                     'outcome': 'succeeded', 'inputId': record['inputId']},
        'settlement': {'settled': True, 'sessionClosed': True, 'remoteOutcome': 'unknown'}}


@pytest.mark.parametrize('observed', [False, True])
async def test_recovered_dispatch_requires_acknowledgement_or_cleanup_time_input(make, observed):
    engine, peer = make()
    record = pending_record(engine)
    session = {'id': record['sessionId'], 'agent': record['agent'], 'model': record['model'],
               'location': record['location'], 'permissions': DENY,
               'metadata': {'preprocessing': operation_marker(record)}}
    if observed:
        session['input'] = {'id': record['inputId'], 'text': 'Read this.',
                            'metadata': {'requestHash': record['requestHash']}}
    peer.sessions[record['sessionId']] = session
    report = await engine.recover([record])
    assert report['settled'] is observed
    assert report['operations'][0]['settlement']['settled'] is observed
    assert not any(c[1].endswith('/prompt') for c in peer.calls)
    if observed:
        assert peer.sessions == {}
        assert report['operations'][0]['evidence']['text'] == 'transcribed 42'
    else:
        assert not any(c[0] != 'GET' for c in peer.calls)
        assert record['sessionId'] in peer.sessions
    await engine.close()


async def test_descriptor_refresh_updates_origin_auth_and_instance(make):
    fresh = None
    engine, peer = make(descriptor_loader=lambda: fresh)
    fresh = copy.deepcopy(engine.descriptor)
    fresh.update(serverUrl='https://new.example:9443', username='owner-new', password='rotated', engineInstanceId='engine-2')
    requests = []
    original = peer.handle

    async def inspect(request):
        requests.append(request)
        return await original(request)

    await engine._client.aclose()
    engine._client = httpx.AsyncClient(transport=httpx.MockTransport(inspect))
    assert (await engine.ready(default_profile()))['engineInstanceId'] == 'engine-2'
    assert (await start(engine))['text'] == 'transcribed 42'
    assert all(str(r.url).startswith('https://new.example:9443/') for r in requests)
    assert all(r.headers['authorization'] == 'Basic ' + base64.b64encode(b'owner-new:rotated').decode() for r in requests)
    assert peer.records['op-job']['engineInstanceId'] == 'engine-2'
    await engine.close()


async def test_active_readiness_uses_pinned_descriptor_then_idle_refreshes(make):
    fresh, loads = None, []

    def loader():
        loads.append(True)
        return fresh

    engine, peer = make('hang', descriptor_loader=loader)
    fresh = copy.deepcopy(engine.descriptor)
    task = asyncio.create_task(start(engine))
    await peer.prompted.wait()
    count = len(loads)
    fresh.update(engineInstanceId='engine-2', password='new-secret')
    readiness = await engine.ready(default_profile())
    assert readiness['ready'] and readiness['engineInstanceId'] == 'engine-1'
    assert len(loads) == count
    await engine.cancel('job')
    with pytest.raises(ServiceError):
        await task
    assert (await engine.ready(default_profile()))['engineInstanceId'] == 'engine-2'
    await engine.close()


async def test_start_refreshes_before_generation_fence_check(make):
    fresh = None
    engine, peer = make(descriptor_loader=lambda: fresh)
    fresh = copy.deepcopy(engine.descriptor)
    fresh['recovery']['fencedGenerations'].append('generation-1')
    with pytest.raises(ServiceError) as error:
        await start(engine)
    assert 'fenced' in error.value.message
    assert not peer.calls
    await engine.close()


@pytest.mark.parametrize('fault', ['bad-version', 'loader-error', 'bad-port'])
async def test_descriptor_refresh_failure_sanitizes_secrets_and_keeps_old_snapshot(make, fault):
    fresh = None

    def loader():
        if fault == 'loader-error':
            raise OSError('secret-credential')
        return fresh

    engine, peer = make(descriptor_loader=loader)
    fresh = copy.deepcopy(engine.descriptor)
    if fault == 'bad-version':
        fresh['version'] = 'secret-credential'
    elif fault == 'bad-port':
        fresh['serverUrl'] = 'https://example.com:secret-credential'
    report = await engine.ready(default_profile())
    assert not report['ready'] and 'secret-credential' not in str(report)
    assert engine.descriptor['engineInstanceId'] == 'engine-1'
    with pytest.raises(ServiceError) as error:
        await start(engine)
    assert 'secret-credential' not in str(error.value)
    assert not peer.calls
    await engine.close()


@pytest.mark.parametrize('url', ['http://example.com', 'http://10.0.0.8:1234', 'http://[::]:1234',
                               'https://example.com:bad', 'https://example.com:70000'])
def test_descriptor_rejects_remote_http_and_invalid_ports(make, url):
    engine, _ = make()
    descriptor = copy.deepcopy(engine.descriptor)
    descriptor['serverUrl'] = url
    with pytest.raises(ServiceError):
        OpenCodeServerEngine(descriptor)


@pytest.mark.parametrize('url', ['http://127.0.0.1:1234', 'http://[::1]:1234', 'http://localhost:1234',
                               'https://engine.example:9443'])
async def test_descriptor_accepts_loopback_http_and_remote_https(make, url):
    engine, _ = make()
    descriptor = copy.deepcopy(engine.descriptor)
    descriptor['serverUrl'] = url
    other = OpenCodeServerEngine(descriptor)
    await other.close()
    await engine.close()


async def test_refresh_receipt_recovers_deleted_session_without_http_and_commits_evidence_first(make):
    fresh = None
    engine, peer = make(descriptor_loader=lambda: fresh)
    record = pending_record(engine)
    fresh = copy.deepcopy(engine.descriptor)
    fresh['recovery']['cleanupReceipts'] = [cleanup_receipt(record)]
    report = await engine.recover([record])
    assert report['settled']
    assert not peer.calls
    assert [p['state'] for p in peer.patches] == ['evidence', 'settled']
    assert report['operations'][0]['evidence']['text'] == 'owner recovered 42'
    assert peer.patches[-1]['settlement']['sessionClosed']
    assert (await engine.ready(default_profile()))['ready']
    await engine.close()


@pytest.mark.parametrize('key', ['operationId', 'jobId', 'generation', 'engineInstanceId',
                                'sessionId', 'inputId', 'requestHash', 'evidence-input',
                                'unfenced', 'unsettled', 'evidence-size'])
async def test_cleanup_receipt_mismatch_cannot_settle_operation(make, key):
    engine, peer = make()
    record = pending_record(engine)
    receipt = cleanup_receipt(record)
    if key == 'evidence-input':
        receipt['evidence']['inputId'] = 'msg_other'
    elif key == 'unfenced':
        engine.descriptor['recovery']['fencedGenerations'] = []
    elif key == 'unsettled':
        receipt['settlement']['settled'] = False
    elif key == 'evidence-size':
        receipt['evidence']['text'] = 'x' * (256 * 1024 + 1)
    else:
        receipt[key] = 'other'
    engine.descriptor['recovery']['cleanupReceipts'] = [receipt]
    report = await engine.recover([record])
    assert not report['settled']
    assert not any(p.get('settlement', {}).get('settled') for p in peer.patches)
    assert not (await engine.ready(default_profile()))['ready']
    await engine.close()


async def test_receipt_persistence_failure_does_not_drop_binding(make):
    engine, peer = make('evidence-persistence')
    record = pending_record(engine)
    engine.descriptor['recovery']['cleanupReceipts'] = [cleanup_receipt(record)]
    report = await engine.recover([record])
    assert not report['settled'] and record['id'] in engine._known
    assert not any(p['state'] == 'settled' for p in peer.patches)
    assert not peer.calls
    await engine.close()


async def test_prepare_cancellation_persists_not_dispatched_settlement(make):
    engine, peer = make()
    entered, release = asyncio.Event(), asyncio.Event()

    async def persist(oid, jid, patch):
        await peer.persist(oid, jid, patch)
        if patch['state'] == 'prepared':
            entered.set()
            await release.wait()

    engine.bind(persist)
    task = asyncio.create_task(start(engine))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert peer.records['op-job']['settlement']['settled']
    assert peer.records['op-job']['settlement']['remoteOutcome'] == 'not-dispatched'
    assert not any(c[0] != 'GET' for c in peer.calls)
    assert (await engine.ready(default_profile()))['ready']
    await engine.close()


async def test_pre_mutation_persistence_failure_retains_durable_no_mutation_proof(make):
    engine, peer = make()

    async def persist(oid, jid, patch):
        await peer.persist(oid, jid, patch)
        raise OSError('projection failed after SQLite commit')

    engine.bind(persist)
    with pytest.raises(ServiceError):
        await start(engine)
    record = copy.deepcopy(peer.records['op-job'])
    assert record['mutationAttempted'] is False
    assert not any(c[0] != 'GET' for c in peer.calls)
    engine.bind(peer.persist)
    engine.descriptor['recovery']['fencedGenerations'].append(record['generation'])
    assert (await engine.recover([record]))['settled']
    assert peer.records['op-job']['settlement']['remoteOutcome'] == 'not-dispatched'
    await engine.close()


async def test_cancel_aggregates_all_owned_settlements_for_job(make):
    engine, peer = make()
    first = _Operation(pending_record(engine, 'op-unknown', 'ses_absent'))
    second = _Operation(pending_record(engine, 'op-complete', 'ses_complete'))
    second.record['dispatchIntent'] = False
    peer.sessions['ses_complete'] = {'id': 'ses_complete', 'agent': 'preprocessing', 'model': MODEL,
        'location': second.record['location'], 'metadata': {'preprocessing': operation_marker(second.record)},
        'permissions': DENY}
    engine._known = {first.record['id']: first, second.record['id']: second}
    result = await engine.cancel('job')
    assert result['settled'] is False
    assert len(result['operations']) == 2
    assert first.record['id'] in engine._known and second.record['id'] not in engine._known
    await engine.close()


async def test_cancel_without_binding_after_failed_recovery_cannot_claim_settlement(make):
    engine, _ = make()
    assert not (await engine.recover([{'id': 'broken', 'settlement': {'settled': False}}]))['settled']
    assert not (await engine.cancel('job'))['settled']
    await engine.close()


async def test_active_unknown_outcome_blocks_readiness_while_persistence_is_pending(make):
    engine, peer = make('prompt-lost')
    entered, release = asyncio.Event(), asyncio.Event()

    async def persist(oid, jid, patch):
        if patch['state'] == 'outcome_unknown':
            entered.set()
            await release.wait()
        await peer.persist(oid, jid, patch)

    engine.bind(persist)
    task = asyncio.create_task(start(engine))
    await entered.wait()
    assert not (await engine.ready(default_profile()))['ready']
    release.set()
    assert (await task)['text'] == 'transcribed 42'
    await engine.close()
