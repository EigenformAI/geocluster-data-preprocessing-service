"""Explicit pinned standalone-engine check; local fake managed proxy, text requests only."""
import asyncio
import json
import os
import uuid

import pytest
from test_dev import config as extraction_config

from preprocessing.contracts import default_profile
from preprocessing.dev import PrivateServer
from preprocessing.engine import OpenCodeServerEngine
from preprocessing.service import Service


@pytest.mark.skipif(not os.environ.get('PREPROCESSING_TEST_OPENCODE'),
                    reason='explicit pinned executable required; local fake managed proxy only')
async def test_native_job_headers_fresh_attempt_ids_and_one_attempt_denials(tmp_path, monkeypatch):
    calls, failures = [], []
    outcome = {'status': 200, 'code': None, 'capacity_once': False}

    async def proxy(reader, writer):
        try:
            async with asyncio.timeout(5):
                raw = await reader.readuntil(b'\r\n\r\n')
                lines = raw.decode('ascii').split('\r\n')
                headers = dict(line.lower().split(': ', 1) for line in lines[1:] if line)
                body = json.loads(await reader.readexactly(int(headers['content-length'])))
                calls.append({'headers': headers, 'body': body})
                assert headers['authorization'] == 'bearer purpose-proxy-fixture'
                assert body['model'] == 'openai/gpt-6-luna'
                transient = outcome['capacity_once']
                status = outcome.get('transient_status', 429) if transient else outcome['status']
                if transient:
                    outcome['capacity_once'] = False
                if status != 200:
                    payload = json.dumps({'error': {'code': outcome['code'], 'message': 'private $5 detail',
                                                  **({'retryable': False} if outcome['code'] or status == 503 and not transient else {})}}).encode()
                    writer.write((f'HTTP/1.1 {status} Denied\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode() + payload)
                else:
                    base = {'id': 'local-completion', 'object': 'chat.completion.chunk', 'created': 1,
                            'model': 'openai/gpt-6-luna'}
                    chunks = [{**base, 'choices': [{'index': 0, 'delta': {'content': 'Completed local text.'}, 'finish_reason': None}]},
                              {**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
                    payload = (''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n').encode()
                    writer.write((f'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode() + payload)
                await writer.drain()
        except Exception as error:  # noqa: BLE001 -- bring local peer errors into the test
            failures.append(repr(error))
        finally:
            writer.close()
            await writer.wait_closed()

    fake = await asyncio.start_server(proxy, '127.0.0.1', 0)
    monkeypatch.setenv('PREPROCESSING_PROXY_TOKEN', 'purpose-proxy-fixture')
    config = extraction_config()
    config['model'] = 'proxy/openai/gpt-6-luna'
    config['providers'] = {'proxy': {'name': 'Managed local proxy fixture', 'package': '@opencode/ai/providers/openrouter',
        'settings': {'baseURL': f'http://127.0.0.1:{fake.sockets[0].getsockname()[1]}/v1', 'apiKey': '{env:PREPROCESSING_PROXY_TOKEN}'},
        'models': {'openai/gpt-6-luna': {'name': 'GPT-6 Luna', 'capabilities': {'tools': False, 'input': ['text', 'image'], 'output': ['text']},
                                      'limit': {'context': 4096, 'output': 512}}}}}
    server = PrivateServer(tmp_path / 'engine', os.environ['PREPROCESSING_TEST_OPENCODE'], config, ['PREPROCESSING_PROXY_TOKEN'])
    engine = service = None
    try:
        async with asyncio.timeout(60):
            descriptor = await server.start(mode='standalone')
            assert descriptor['version'] == '2.0.19' and descriptor['mode'] == 'standalone'
            engine = OpenCodeServerEngine(descriptor, input_roots=[tmp_path / 'state'], rpc_seconds=3, cancel_seconds=3)
            profile = default_profile()
            profile['models']['vision'] = config['model']
            profile['limits']['modelSeconds'] = 5
            while not (await engine.ready(profile))['ready']:
                await asyncio.sleep(.05)
            service = Service(tmp_path / 'state', tmp_path, profile=profile, engine=engine)
            await service.start()
            service.scheduler.cancel()
            await asyncio.gather(service.scheduler, return_exceptions=True)
            service.scheduler = None
            (tmp_path / 'source.csv').write_text('value\n42\n')

            async def job():
                value, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'source.csv'}})
                value['state'] = 'running'
                service.store.save(value)
                staging = service.private / 'jobs' / value['id'] / 'staging'
                staging.mkdir(parents=True)
                return value, staging

            message = {'operation': {'kind': 'transcription', 'sourceUnit': 1, 'prompt': 'Read the supplied text.'}, 'inputRefs': []}
            good, staging = await job()
            first = await service._model(good['id'], staging, message)
            assert 'result' in first, first
            header = calls[0]['headers']
            assert header.get('x-geocluster-work-id') == good['id']
            assert header.get('x-geocluster-kind') == 'preprocessing'
            assert header.get('x-geocluster-agent') == 'preprocessing'
            assert header.get('x-geocluster-conversation-id') == first['result']['sessionId']
            uuid.UUID(header['x-geocluster-request-id'])
            for status, code, expected in [(402, 'usage_limit_reached', 'usage_limit_reached'),
                                          (403, 'model_not_allowed', 'model_not_allowed'),
                                          (403, 'funding_ended', 'funding_ended'),
                                          (409, 'request_already_admitted', 'request_already_admitted'),
                                          (503, 'usage_unavailable', 'usage_unavailable'),
                                          (402, None, 'usage_limit_reached'), (403, None, 'model_not_allowed'),
                                          (409, None, 'request_already_admitted'), (503, None, 'usage_unavailable'),
                                          (401, None, 'usage_unavailable')]:
                outcome.update(status=status, code=code)
                current, staging = await job()
                mark = len(calls)
                denied = await service._model(current['id'], staging, message)
                assert denied['error']['code'] == expected, denied
                assert denied['error']['retryable'] is False
                assert '$' not in json.dumps(denied)
                assert len(calls) == mark + 1, f'native retry after {code}'
                assert calls[-1]['headers']['x-geocluster-work-id'] == current['id']
                await service._model(current['id'], staging, message)
                assert len(calls) == mark + 1, 'same job cannot start a new supplier attempt after denial'
            for transient_status in (429, 503):
                outcome.update(status=200, code=None, capacity_once=True, transient_status=transient_status)
                restored, staging = await job()
                mark = len(calls)
                response = await service._model(restored['id'], staging, message)
                assert 'result' in response, response
                attempts = calls[mark:]
                assert len(attempts) == 2
                assert all(call['headers']['x-geocluster-work-id'] == restored['id'] for call in attempts)
                assert attempts[0]['headers']['x-geocluster-request-id'] != attempts[1]['headers']['x-geocluster-request-id']
            assert len({call['headers']['x-geocluster-request-id'] for call in calls}) == len(calls)
            assert not failures, failures
    finally:
        if service:
            await service.close()
        elif engine:
            await engine.close()
        await server.close()
        fake.close()
        await fake.wait_closed()
