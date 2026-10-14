"""Opt-in 2.0.19 HTTP probes; completed turns use only a local fake provider."""
import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
import secrets
import socket
import sqlite3
import uuid

import pytest

from preprocessing.engine import SYSTEM, OpenCodeServerEngine, _Operation
from preprocessing.engine_evidence import (
    deny_all,
    operation_marker,
    validate_session_ownership,
    validate_session_selection,
)
from preprocessing.engine_version import cli_version
from preprocessing.errors import ServiceError


@pytest.mark.skipif(not os.environ.get('PREPROCESSING_TEST_OPENCODE'),
                    reason='explicit pinned executable required; no inference')
async def test_pinned_http_image_admission_owned_cleanup_and_chat_preservation(tmp_path):
    executable = os.environ['PREPROCESSING_TEST_OPENCODE']
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'NO_COLOR': '1'}
    for key, name in [('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
                      ('XDG_CACHE_HOME', 'cache'), ('XDG_STATE_HOME', 'state'),
                      ('XDG_RUNTIME_DIR', 'runtime'), ('TMPDIR', 'tmp')]:
        path = tmp_path / name
        path.mkdir()
        env[key] = str(path)
    directory = tmp_path / 'workspace'
    directory.mkdir()
    empty = tmp_path / 'models.json'
    empty.write_text('{}')
    deny = [{'action': '*', 'resource': '*', 'effect': 'deny'}]
    config = {'default_agent': 'preprocessing', 'permissions': deny,
        'agents': {'preprocessing': {'mode': 'primary', 'hidden': False, 'system': SYSTEM,
                                    'steps': 1, 'permissions': deny}},
        'mcp': {'servers': {}}, 'plugins': ['-opencode.agent', '-opencode.plan'], 'skills': [],
        'warming': False, 'snapshots': False, 'compaction': {'auto': False}, 'update': 'disable'}
    password = secrets.token_urlsafe(32)
    env.update({'OPENCODE_CONFIG_CONTENT': json.dumps(config), 'OPENCODE_PASSWORD': password,
        'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_CONFIG_PROJECT_DISABLE': '1',
        'OPENCODE_MODELS_PATH': str(empty), 'OPENCODE_DISABLE_MODELS_FETCH': '1',
        'OPENCODE_CONFIG_DIR': env['XDG_CONFIG_HOME']})
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    version_process = await asyncio.create_subprocess_exec(executable, '--version', env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    output, _ = await asyncio.wait_for(version_process.communicate(), 10)
    actual_version = cli_version(output)
    assert version_process.returncode == 0 and actual_version is not None
    process = await asyncio.create_subprocess_exec(executable, 'serve', '--hostname', '127.0.0.1',
        '--port', str(port), cwd=directory, env=env,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    engine = OpenCodeServerEngine({'serverUrl': f'http://127.0.0.1:{port}', 'password': password,
        'version': actual_version, 'engineInstanceId': 'real-test', 'mode': 'development',
        'owner': 'test', 'directory': str(directory), 'contextVerified': True,
        'recovery': {'managed': False, 'extractionExcluded': True,
                     'fencedGenerations': [], 'deadEngineInstances': []}}, input_roots=[tmp_path])
    records = {}

    async def persist(oid, jid, patch):
        records.setdefault(oid, {'id': oid, 'jobId': jid}).update(patch)

    engine.bind(persist)
    chat = 'ses_' + secrets.token_hex(16)
    try:
        async with asyncio.timeout(20):
            while True:
                assert process.returncode is None
                try:
                    info = await engine._http('GET', '/api/info', envelope=False)
                    assert info['version'] == actual_version
                    break
                except ServiceError:
                    await asyncio.sleep(.05)
        # Empty catalogs do not satisfy readiness, even for native extraction.
        assert (await engine.ready())['ready'] is False
        async with asyncio.timeout(5):
            while True:
                agents = await engine._http('GET', '/api/agent', params={'location[directory]': str(directory)})
                matches = [agent for agent in agents if agent['id'] == 'preprocessing']
                if matches:
                    assert matches[0]['system'] == SYSTEM
                    assert matches[0]['steps'] == 1 and deny_all(matches[0]['permissions'])
                    break
                await asyncio.sleep(.05)
        await engine._http('POST', '/api/session', payload={'id': chat, 'location': {'directory': str(directory)}})
        from PIL import Image
        image = tmp_path / 'image.png'
        Image.new('RGB', (2, 2), 'white').save(image)
        data = image.read_bytes()
        payload = engine._inputs.prompt({'prompt': 'Read this.'}, [{'path': str(image), 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'mediaType': 'image/png'}], {})
        record = {'id': 'real-op', 'jobId': 'real-job', 'generation': 'real-generation',
            'sessionId': 'ses_' + secrets.token_hex(16), 'inputId': 'msg_' + secrets.token_hex(16),
            'engineInstanceId': 'real-test', 'agent': 'preprocessing',
            'model': {'providerID': 'probe', 'id': 'no-inference'},
            'location': {'directory': str(directory)}, 'requestHash': '0' * 64,
            'state': 'prepared', 'settlement': {'settled': False}}
        op = _Operation(record)
        await engine._patch(op, **record)
        engine._known[record['id']] = op
        created = await engine._http('POST', '/api/session', payload={'id': record['sessionId'],
            'agent': record['agent'], 'model': record['model'], 'location': record['location'],
            'permissions': deny, 'metadata': {'preprocessing': operation_marker(record)}})
        validate_session_ownership(created, record)
        op.owned = True
        validate_session_selection(created, record)
        # resume=false is used only by this non-inference protocol test.
        ack = await engine._http('POST', op.path + '/prompt', payload={**payload, 'id': record['inputId'],
            'metadata': {'requestHash': record['requestHash']}, 'resume': False})
        assert ack['id'] == record['inputId'] and ack['sessionID'] == record['sessionId']
        assert ack['payload']['files'][0]['source'] == {'type': 'inline'}
        op.acknowledged = True
        await engine._patch(op, state='admitted', admissionAcknowledged=True, dispatchIntent=True)
        assert (await engine._http('GET', op.path + '/inbox'))[0]['id'] == record['inputId']
        assert await engine._cleanup(op)
        assert records['real-op']['settlement']['sessionClosed'] is True
        assert await engine._http('GET', op.path, missing=True) is None
        assert (await engine._http('GET', '/api/session/' + chat))['id'] == chat
        await engine._http('DELETE', '/api/session/' + chat)
    finally:
        await engine.close()
        if process.returncode is None:
            process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 5)
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.skipif(not os.environ.get('PREPROCESSING_TEST_OPENCODE'),
                    reason='explicit pinned executable required; local fake provider only')
async def test_pinned_completed_text_and_image_turns_with_service_ledger(tmp_path, monkeypatch):
    """Run real turn admission/execution/projection, not a mocked OpenCode transcript."""
    from PIL import Image

    from preprocessing.contracts import default_profile
    from preprocessing.dev import PrivateServer
    from preprocessing.service import Service

    transcription = 'Deterministic local transcription: 42.'
    provider_requests, provider_errors, transcripts, before_deletes = [], [], [], []

    async def provider(reader, writer):
        try:
            async with asyncio.timeout(5):
                raw = await reader.readuntil(b'\r\n\r\n')
                lines = raw.decode('ascii').split('\r\n')
                method, path, _ = lines[0].split()
                headers = dict(line.lower().split(': ', 1) for line in lines[1:] if line)
                size = int(headers['content-length'])
                assert method == 'POST' and path == '/v1/chat/completions'
                assert 0 < size <= 1024 * 1024
                body = json.loads(await reader.readexactly(size))
                provider_requests.append(body)
                assert body['model'] == 'vision' and body['stream'] is True
                assert headers['authorization'] == 'bearer local-fake-not-a-real-key'
                base = {'id': 'chatcmpl-local-fixture', 'object': 'chat.completion.chunk',
                        'created': 1, 'model': 'vision'}
                chunks = [
                    {**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]},
                    {**base, 'choices': [{'index': 0, 'delta': {'content': transcription}, 'finish_reason': None}]},
                    {**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
                    {**base, 'choices': [], 'usage': {'prompt_tokens': 17, 'completion_tokens': 4, 'total_tokens': 21}},
                ]
                stream = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
                payload = stream.encode()
                writer.write(('HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                              f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode() + payload)
                await writer.drain()
        except Exception as error:  # noqa: BLE001 - surface local peer errors in the test thread
            provider_errors.append(repr(error))
        finally:
            writer.close()
            await writer.wait_closed()

    fake = await asyncio.start_server(provider, '127.0.0.1', 0)
    port = fake.sockets[0].getsockname()[1]
    monkeypatch.setenv('LOCAL_FAKE_PROVIDER_TOKEN', 'local-fake-not-a-real-key')
    deny = [{'action': '*', 'resource': '*', 'effect': 'deny'}]
    config = {'model': 'fixture/vision', 'default_agent': 'preprocessing', 'permissions': deny,
        'agents': {'preprocessing': {'mode': 'primary', 'hidden': False,
                                    'system': SYSTEM, 'steps': 1, 'permissions': deny}},
        'providers': {'fixture': {'name': 'Local deterministic fake', 'package': '@ai-sdk/openai-compatible',
            'settings': {'baseURL': f'http://127.0.0.1:{port}/v1', 'apiKey': '{env:LOCAL_FAKE_PROVIDER_TOKEN}'},
            'models': {'vision': {'name': 'Fake vision',
                'capabilities': {'tools': False, 'input': ['text', 'image'], 'output': ['text']},
                'limit': {'context': 4096, 'output': 512}}}}},
        'mcp': {'servers': {}}, 'plugins': ['-opencode.agent', '-opencode.plan'], 'skills': [],
        'warming': False, 'snapshots': False, 'compaction': {'auto': False}, 'update': 'disable'}
    private_server = PrivateServer(tmp_path / 'private-engine', os.environ['PREPROCESSING_TEST_OPENCODE'],
                                   config, ['LOCAL_FAKE_PROVIDER_TOKEN'])
    engine = service = None
    try:
        async with asyncio.timeout(45):
            descriptor = await private_server.start()
            engine = OpenCodeServerEngine(descriptor, input_roots=[tmp_path / 'service'],
                                          rpc_seconds=5, cancel_seconds=5)
            profile = default_profile()
            profile['models'] = {'vision': 'fixture/vision'}
            profile['limits']['modelSeconds'] = 8
            async with asyncio.timeout(10):
                while not (await engine.ready(profile))['ready']:
                    await asyncio.sleep(.05)
            service = Service(tmp_path / 'service', tmp_path, profile=profile, engine=engine)
            await service.start()
            # Exercise the actual worker-to-service model boundary directly. Native
            # parsing is unrelated to this protocol probe; keep its scheduler idle.
            service.scheduler.cancel()
            await asyncio.gather(service.scheduler, return_exceptions=True)
            service.scheduler = None
            (tmp_path / 'source.csv').write_text('value\n42\n')
            job, _ = await service.submit(str(uuid.uuid4()), {
                'source': {'kind': 'workspace-file', 'path': 'source.csv'}})
            job['state'] = 'running'
            service.store.save(job)
            staging = service.private / 'jobs' / job['id'] / 'staging'
            staging.mkdir(parents=True)
            image = staging / 'image.png'
            Image.new('RGB', (3, 3), 'white').save(image)
            chat = 'ses_' + secrets.token_hex(16)
            chat_path = '/api/session/' + chat
            await engine._http('POST', '/api/session', payload={'id': chat, 'title': 'Unrelated chat',
                'agent': 'preprocessing', 'model': {'providerID': 'fixture', 'id': 'vision'},
                'location': {'directory': descriptor['directory']}})
            await engine._http('POST', chat_path + '/prompt', payload={
                'id': 'msg_' + secrets.token_hex(16), 'text': 'Unrelated parked chat input.', 'resume': False})
            chat_before = await engine._http('GET', chat_path)
            chat_inbox_before = await engine._http('GET', chat_path + '/inbox')
            chat_messages_before = await engine._http('GET', chat_path + '/message', envelope=False)

            async def observe_delete(request):
                if request.method != 'DELETE' or '/inbox/' in request.url.path or request.url.path == chat_path:
                    return
                sid = request.url.path.rsplit('/', 1)[-1]
                operations = [op for op in service.store.operations(job['id']) if op.get('sessionId') == sid]
                assert len(operations) == 1
                op = operations[0]
                with contextlib.closing(sqlite3.connect(service.private / 'state.sqlite')) as ledger:
                    persisted = json.loads(ledger.execute('SELECT body FROM operations WHERE id=?', (op['id'],)).fetchone()[0])
                assert persisted['evidence'] == op['evidence']
                # This hook inspects, but never intercepts, the real DELETE request.
                assert op['evidence']['text'] == transcription
                assert op['evidence']['usage']['tokens']['input'] == 17
                assert op['evidence']['usage']['tokens']['output'] == 4
                assert op['evidence']['outcome'] == 'succeeded'
                projection = json.loads((service.private / 'engine-lease.json').read_text())
                assert any(entry['sessionId'] == sid for entry in projection['entries'])
                raw = await engine._http('GET', request.url.path + '/message',
                                         params={'limit': 200, 'order': 'asc'}, envelope=False)
                # A nonempty short page still has a next anchor at this pin.
                assert 0 < len(raw['data']) < 200 and raw['cursor']['next']
                transcripts.append(raw['data'])
                before_deletes.append(op['id'])

            engine._client.event_hooks['request'].append(observe_delete)
            for refs in ([], [{'path': str(image), 'mediaType': 'image/png'}]):
                response = await service._model(job['id'], staging, {'operation': {
                    'kind': 'ocr', 'sourceUnit': 1, 'prompt': 'Transcribe the supplied document: 42.'}, 'inputRefs': refs})
                assert not provider_errors, provider_errors
                assert 'result' in response, response
                result = response['result']
                assert result['text'] == transcription
                assert result['requestedModel'] == result['returnedModel'] == 'fixture/vision'
                assert await engine._http('GET', '/api/session/' + result['sessionId'], missing=True) is None
            operations = service.store.operations(job['id'])
            assert len(operations) == len(before_deletes) == len(transcripts) == 2
            for op, transcript in zip(operations, transcripts, strict=True):
                assert op['settlement']['settled'] and op['settlement']['sessionClosed']
                assert op['evidence']['inputId'] == op['inputId']
                user = next(message for message in transcript if message['type'] == 'user')
                assistant = next(message for message in transcript if message['type'] == 'assistant')
                assert user['id'] == op['inputId'] and user['metadata']['requestHash'] == op['requestHash']
                assert assistant['finish'] == 'stop' and assistant['time']['completed'] is not None
                assert transcript[-1]['type'] == 'idle' and transcript[-1]['outcome'] == 'succeeded'
            extraction_requests = [body for body in provider_requests
                if 'Transcribe the supplied document: 42.' in json.dumps(body['messages'])]
            assert len(extraction_requests) == 2
            image_parts = [part for body in extraction_requests for message in body['messages']
                if message['role'] == 'user' and isinstance(message['content'], list)
                for part in message['content'] if part['type'] == 'image_url']
            assert len(image_parts) == 1 and image_parts[0]['image_url']['url'].startswith('data:image/')
            with Image.open(io.BytesIO(base64.b64decode(image_parts[0]['image_url']['url'].split(',', 1)[1]))) as received:
                assert received.size == (3, 3)
            assert len(service.get(job['id'])['usage']['observations']) == 2
            assert json.loads((service.private / 'engine-lease.json').read_text())['entries'] == []
            assert await engine._http('GET', chat_path) == chat_before
            assert await engine._http('GET', chat_path + '/inbox') == chat_inbox_before
            assert await engine._http('GET', chat_path + '/message', envelope=False) == chat_messages_before
            await engine._http('DELETE', chat_path)
            job = service.get(job['id'])
            job['state'] = 'succeeded'
            service.store.save(job)
    finally:
        try:
            if engine is not None:
                engine._client.event_hooks['request'].clear()
            if service is not None:
                await service.close()
            elif engine is not None:
                await engine.close()
        finally:
            await private_server.close()
            fake.close()
            await fake.wait_closed()
