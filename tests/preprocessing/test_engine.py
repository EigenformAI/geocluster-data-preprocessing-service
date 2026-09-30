"""Behavioral ACP peer; no provider/network access in this suite."""
import asyncio
import json
import os
import random
import sys

import pytest

from preprocessing.contracts import default_profile
from preprocessing.engine import BorrowedEngine, OwnedEngine
from preprocessing.errors import ServiceError

FAKE = r'''
import json, os, sys
if '--version' in sys.argv:
    print('opencode v2.0.19'); sys.exit()
fault = os.environ.get('FAKE_FAULT', '')
log = os.environ['FAKE_LOG']
current = {'mode': 'build', 'model': 'paid/default'}
counter = 0
pending = None
def send(value):
    print(json.dumps(dict(jsonrpc='2.0', **value)), flush=True)
def options():
    return {'configOptions': [{'id': k, 'currentValue': v} for k, v in current.items()]}
for line in sys.stdin:
    request = json.loads(line)
    with open(log, 'a') as f: f.write(json.dumps(request) + '\n')
    if 'method' not in request: continue
    method = request['method']; params = request.get('params', {})
    result = {}
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentInfo': {'version': '2.0.19'},
                  'agentCapabilities': {'promptCapabilities': {'image': True}}}
    elif method == 'session/new':
        if fault == 'new-hang' or (fault == 'empty-new-hang' and counter): continue
        counter += 1
        current = {'mode': 'build', 'model': 'paid/default'}
        result = dict(sessionId='s' + str(counter), **options())
    elif method == 'session/set_config_option':
        if fault != params['configId']: current[params['configId']] = params['value']
        if fault == 'mode-reset' and params['configId'] == 'model': current['mode'] = 'build'
        result = options()
    elif method == 'session/prompt':
        pending = request
        if fault in ('hang', 'cancel-hang'): continue
        if fault == 'permission':
            send({'id': 'permission-1', 'method': 'session/request_permission',
                  'params': {'sessionId': params['sessionId'], 'options': []}})
            continue
        if fault not in ('empty', 'empty-new-hang'):
            send({'method': 'session/update', 'params': {'sessionId': params['sessionId'],
                  'update': {'sessionUpdate': 'agent_message_chunk',
                             'content': {'type': 'text', 'text': 'transcribed 42'}}}})
        result = {'stopReason': 'end_turn', 'usage': {'inputTokens': 17, 'outputTokens': 4}}
        pending = None
    elif method == 'session/cancel':
        if pending and fault != 'cancel-hang':
            send({'id': pending['id'], 'result': {'stopReason': 'cancelled'}})
            pending = None
        continue
    if 'id' in request: send({'id': request['id'], 'result': result})
'''


@pytest.fixture
def engine_factory(tmp_path, monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    spawns = []

    async def spawn(*args, **kwargs):
        spawns.append(args)
        # Exercise the real guardian; replace only its owned CLI payload.
        if len(args) > 2 and str(args[2]).endswith('engine_guardian.py'):
            return await real_spawn(*args[:6], sys.executable, '-u', '-c', FAKE, *args[7:], **kwargs)
        return await real_spawn(sys.executable, '-u', '-c', FAKE, *args[1:], **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)

    def make(fault='', **kwargs):
        log = tmp_path / 'wire.jsonl'
        engine = OwnedEngine(tmp_path / 'state', executable='/fake/opencode',
                             input_roots=[tmp_path], rpc_seconds=2, cancel_seconds=.15,
                             environment={'FAKE_FAULT': fault, 'FAKE_LOG': str(log)}, **kwargs)
        return engine, log, spawns

    return make


def messages(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


async def start(engine, job='job', profile=None, refs=None):
    return await engine.start('op-' + job, job, 'generation-1', profile or default_profile(),
                              refs or [], {'kind': 'ocr', 'sourceUnit': 1, 'prompt': 'Read this.'})


async def wait_prompt(log):
    async with asyncio.timeout(3):
        while not any(m.get('method') == 'session/prompt' for m in messages(log)):
            await asyncio.sleep(.005)


@pytest.mark.parametrize('fault', ['mode', 'model', 'mode-reset'])
async def test_selection_fails_closed(engine_factory, fault):
    engine, log, _ = engine_factory(fault)
    try:
        with pytest.raises(ServiceError) as error:
            await start(engine)
        assert error.value.code == 'PROVIDER_UNAVAILABLE'
        methods = [m.get('method') for m in messages(log)]
        assert 'session/prompt' not in methods
        assert 'session/close' in methods
    finally:
        await engine.close()


async def test_shared_lazy_engine_fresh_sessions_and_usage(engine_factory):
    engine, log, spawns = engine_factory()
    assert not spawns
    try:
        # Jittered concurrent admission must still create just one ACP process.
        async def run(index):
            await asyncio.sleep(random.Random(index).random() / 100)
            return await start(engine, str(index))
        results = await asyncio.gather(*(run(i) for i in range(12)))
        assert len({r['sessionId'] for r in results}) == 12
        assert all(r['text'] == 'transcribed 42' for r in results)
        assert all(r['requestedModel'] == default_profile()['models']['vision'] for r in results)
        assert all(r['returnedModel'] is None for r in results)
        assert all(r['usage']['prompt']['inputTokens'] == 17 for r in results)
        assert len([args for args in spawns if 'acp' in args]) == 1
        assert len([m for m in messages(log) if m.get('method') == 'session/close']) == 12
    finally:
        await engine.close()
    assert engine.process.returncode is not None


async def test_cancel_waits_for_prompt_settlement_then_closes(engine_factory):
    engine, log, _ = engine_factory('hang')
    task = asyncio.create_task(start(engine))
    try:
        await wait_prompt(log)
        settlement = await engine.cancel('job')
        assert settlement['settled'] is True
        assert settlement['remoteOutcome'] == 'unknown'
        with pytest.raises(ServiceError) as error:
            await task
        assert error.value.code == 'RUNTIME_INTERRUPTED'
        methods = [m.get('method') for m in messages(log)]
        assert methods.index('session/cancel') < methods.index('session/close')
        cancel = next(m for m in messages(log) if m.get('method') == 'session/cancel')
        assert 'id' not in cancel
    finally:
        await engine.close()


@pytest.mark.parametrize('fault,settled', [('hang', True), ('cancel-hang', False)])
async def test_timeout_never_repeats_and_preserves_uncertainty(engine_factory, fault, settled):
    engine, log, _ = engine_factory(fault)
    profile = default_profile()
    profile['limits']['modelSeconds'] = .05
    try:
        with pytest.raises(ServiceError) as error:
            await start(engine, profile=profile)
        assert error.value.code == 'PROVIDER_OUTCOME_UNKNOWN'
        assert error.value.settlement['settled'] is settled
        assert error.value.session_id == 's1'
        assert len([m for m in messages(log) if m.get('method') == 'session/prompt']) == 1
        if not settled:
            with pytest.raises(ServiceError):
                await start(engine, 'next')
            assert len([m for m in messages(log) if m.get('method') == 'session/prompt']) == 1
    finally:
        await engine.close()


async def test_permission_callback_rejected_while_reader_continues(engine_factory):
    engine, log, _ = engine_factory('permission')
    try:
        with pytest.raises(ServiceError) as error:
            await start(engine)
        assert error.value.code == 'PROVIDER_ISOLATION_FAILED'
        response = next(m for m in messages(log) if m.get('id') == 'permission-1')
        assert response['result']['outcome']['outcome'] == 'cancelled'
    finally:
        await engine.close()


async def test_completed_empty_results_have_finite_no_progress_limit(engine_factory):
    engine, log, _ = engine_factory('empty')
    profile = default_profile()
    profile['retry'] = {'attempts': 5, 'noProgress': 2, 'backoffSeconds': 0}
    try:
        with pytest.raises(ServiceError) as error:
            await start(engine, profile=profile)
        assert error.value.code == 'LOOP_NO_PROGRESS'
        assert len([m for m in messages(log) if m.get('method') == 'session/prompt']) == 2
    finally:
        await engine.close()


async def test_image_outside_approved_roots_rejected_before_spawn(engine_factory, tmp_path):
    engine, _, spawns = engine_factory()
    try:
        with pytest.raises(ServiceError):
            await start(engine, refs=[{'path': str(tmp_path / '../escape.png'), 'mediaType': 'image/png'}])
        assert not spawns
    finally:
        await engine.close()


async def test_borrowed_without_broker_never_spawns(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail('borrowed engine spawned a child')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    engine = BorrowedEngine()
    with pytest.raises(ServiceError) as error:
        await start(engine)
    assert error.value.code == 'PROVIDER_UNAVAILABLE'
    assert (await engine.cancel('job'))['settled'] is True
    await engine.close()


async def test_cancelling_queued_job_does_not_wait_for_other_job(engine_factory):
    engine, log, _ = engine_factory('hang')
    first = asyncio.create_task(start(engine, 'first'))
    second = None
    try:
        await wait_prompt(log)
        second = asyncio.create_task(start(engine, 'queued'))
        await asyncio.sleep(.02)
        settlement = await asyncio.wait_for(engine.cancel('queued'), .5)
        assert settlement['settled'] is True
        assert not first.done()
        with pytest.raises(ServiceError):
            await second
        assert len([m for m in messages(log) if m.get('method') == 'session/new']) == 1
    finally:
        await engine.close()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


async def test_task_cancel_during_session_creation_blocks_unsafe_reuse(engine_factory):
    engine, log, _ = engine_factory('new-hang')
    task = asyncio.create_task(start(engine))
    try:
        async with asyncio.timeout(3):
            while not any(m.get('method') == 'session/new' for m in messages(log)):
                await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await engine.cancel('job'))['settled'] is False
        with pytest.raises(ServiceError) as error:
            await start(engine, 'next')
        assert error.value.code == 'PROVIDER_UNAVAILABLE'
    finally:
        await engine.close()


async def test_output_bound_cancels_instead_of_returning_truncated_success(engine_factory):
    engine, log, _ = engine_factory()
    profile = default_profile()
    profile['limits']['responseBytes'] = 3
    try:
        with pytest.raises(ServiceError) as error:
            await start(engine, profile=profile)
        assert error.value.code == 'OUTPUT_INVALID'
        assert error.value.evidence['text'] == ''
        assert len([m for m in messages(log) if m.get('method') == 'session/prompt']) == 1
    finally:
        await engine.close()


async def test_image_pixels_and_symlinks_rejected_before_spawn(engine_factory, tmp_path):
    from PIL import Image
    image = tmp_path / 'image.png'
    Image.new('RGB', (30, 30), 'white').save(image)
    link = tmp_path / 'link.png'
    link.symlink_to(image)
    engine, _, spawns = engine_factory()
    profile = default_profile()
    profile['limits']['pixels'] = 800
    try:
        for path in (image, link):
            with pytest.raises(ServiceError) as error:
                await start(engine, profile=profile, refs=[{'path': str(path), 'mediaType': 'image/png'}])
            assert error.value.code == 'INPUT_LIMIT'
        assert not spawns
    finally:
        await engine.close()


async def test_lost_retry_session_creation_does_not_reuse_previous_session_evidence(engine_factory):
    engine, _, _ = engine_factory('empty-new-hang')
    profile = default_profile()
    profile['retry'] = {'attempts': 2, 'noProgress': 2, 'backoffSeconds': 0}
    try:
        with pytest.raises(ServiceError) as error:
            await start(engine, profile=profile)
        assert error.value.code == 'PROVIDER_OUTCOME_UNKNOWN'
        assert error.value.session_id is None
        assert error.value.settlement['settled'] is False
        assert error.value.evidence['attempts'][0]['sessionId'] == 's1'
    finally:
        await engine.close()


async def test_session_callback_finishes_before_selection_or_prompt(engine_factory):
    engine, log, _ = engine_factory()
    entered, committed = asyncio.Event(), asyncio.Event()
    seen = []

    async def persist(operation_id, job_id, session_id):
        seen.append((operation_id, job_id, session_id))
        entered.set()
        await committed.wait()

    engine.on_session = persist
    task = asyncio.create_task(start(engine))
    try:
        await asyncio.wait_for(entered.wait(), .5)
        assert seen == [('op-job', 'job', 's1')]
        assert not any(m.get('method') in ('session/set_config_option', 'session/prompt') for m in messages(log))
        committed.set()
        assert (await task)['sessionId'] == 's1'
    finally:
        committed.set()
        await engine.close()
        await asyncio.gather(task, return_exceptions=True)


async def test_session_callback_failure_closes_without_prompt(engine_factory):
    engine, log, _ = engine_factory()

    async def persist(*args):
        raise OSError('database unavailable')

    engine.on_session = persist
    try:
        with pytest.raises(ServiceError) as error:
            await start(engine)
        assert error.value.session_id == 's1'
        assert error.value.settlement['remoteOutcome'] == 'not-dispatched'
        methods = [m.get('method') for m in messages(log)]
        assert 'session/prompt' not in methods
        assert 'session/set_config_option' not in methods
        assert 'session/close' in methods
    finally:
        await engine.close()


@pytest.mark.skipif(not os.environ.get('PREPROCESSING_TEST_OPENCODE'),
                    reason='explicit pinned executable required; catalog-only, no model prompts')
@pytest.mark.parametrize('cold_start', range(3))
async def test_pinned_cold_catalog_exposes_only_trusted_mode(tmp_path, cold_start):
    """Real ACP regression for the cold-start catalog race; no inference requests."""
    await asyncio.sleep(random.Random(cold_start).random() / 100)
    engine = OwnedEngine(tmp_path / 'engine', executable=os.environ['PREPROCESSING_TEST_OPENCODE'],
                         rpc_seconds=30, cancel_seconds=5)
    session_id = None
    try:
        await engine._ensure_started('opencode/space-bunny-free')
        response = await engine._request('session/new', {'cwd': str(engine.cwd), 'mcpServers': []})
        session_id = response['sessionId']
        mode = next(option for option in response['configOptions'] if option['id'] == 'mode')
        assert [option['value'] for option in mode['options']] == ['preprocessing']
        for key, value in (('mode', 'preprocessing'), ('model', 'opencode/space-bunny-free')):
            response = await engine._request('session/set_config_option',
                {'sessionId': session_id, 'configId': key, 'value': value})
            values = {option['id']: option['currentValue'] for option in response['configOptions']}
            assert values[key] == value
        assert values['mode'] == 'preprocessing'
    finally:
        try:
            if session_id:
                await engine._request('session/close', {'sessionId': session_id})
        finally:
            await engine.close()
