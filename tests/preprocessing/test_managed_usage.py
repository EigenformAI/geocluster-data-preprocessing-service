"""Managed denials retain their meaning and cannot launch further job model work."""
import asyncio
import json
import uuid

import pytest
import test_engine
from test_table_recovery_service import (
    PDF_ROWS,
    TableEngine,
    finished_job,
    native_table_pdf,
    resume_request,
    sealed_tree,
)

from preprocessing.contracts import default_profile
from preprocessing.errors import ServiceError
from preprocessing.service import Service

DENIALS = ["usage_limit_reached", "model_not_allowed", "funding_ended",
           "request_already_admitted", "usage_unavailable"]
make = test_engine.make


@pytest.mark.parametrize('code', DENIALS)
async def test_correlated_engine_denial_is_not_replaced_by_output_invalid(make, code):
    engine, peer = make()
    original = peer.transcript

    def transcript(sid):
        messages = original(sid)
        if len(messages) >= 3:
            messages[1]['content'] = []
            messages[1]['error'] = {'type': 'provider.internal', 'status': 503,
                                    'message': json.dumps({'error': {'code': code, 'retryable': False,
                                                                   'message': 'private $5 supplier detail'}})}
            messages[-1]['outcome'] = 'failed'
        return messages

    peer.transcript = transcript
    try:
        with pytest.raises(ServiceError) as failure:
            await test_engine.start(engine)
        assert failure.value.code == code
        assert failure.value.as_dict()['retryable'] is False
        assert failure.value.retry == 'none'
        assert '$' not in failure.value.message
        assert failure.value.settlement['settled'] is True
        assert len([call for call in peer.calls if call[1].endswith('/prompt')]) == 1
        assert not peer.sessions
    finally:
        await engine.close()


@pytest.mark.parametrize('code', DENIALS)
async def test_pdf_denial_stops_job_rereads_retains_completed_results_and_needs_explicit_new_job(
        tmp_path, monkeypatch, code):
    monkeypatch.setitem(PDF_ROWS, 3, PDF_ROWS[2])
    native_table_pdf(tmp_path / 'tables.pdf')
    profile = default_profile()
    profile['models']['vision'] = 'fixture/vision'

    class DenyingEngine(TableEngine):
        def __init__(self):
            super().__init__()
            self.denied = True
            self.dispatches = []

        async def start(self, oid, jid, generation, profile, refs, operation):
            self.dispatches.append({'jobId': jid, 'page': operation['sourceUnit']['page']})
            if self.denied and len(self.dispatches) >= 2:
                raise ServiceError(code, 'private $5 supplier detail', 503)
            return await super().start(oid, jid, generation, profile, refs, operation)

    engine = DenyingEngine()
    service = Service(tmp_path / 'state', tmp_path, profile=profile, engine=engine)
    await service.start()
    try:
        key = str(uuid.uuid4())
        body = {'source': {'kind': 'workspace-file', 'path': 'tables.pdf'}}
        parent, replayed = await service.submit(key, body)
        assert not replayed
        async with asyncio.timeout(20):
            while service.get(parent['id'])['state'] not in {'succeeded', 'partial', 'failed'} or service.active == parent['id']:
                await asyncio.sleep(.02)
        parent = service.get(parent['id'])
        assert len(engine.dispatches) == 2, 'a denial must stop every later model operation of this job'
        assert parent['state'] == 'partial', parent.get('error')
        assert parent['error']['code'] == code
        assert parent['error']['retryable'] is False
        assert '$' not in json.dumps(parent['error'])
        manifest = service.get_result(parent['id'])
        assert manifest['tables'][0]['confirmation']['state'] == 'agreed'
        assert all(table['confirmation']['state'] == 'pending' for table in manifest['tables'][1:])
        sealed, before = sealed_tree(service, parent)
        assert any(path.endswith('.response.txt') for path in before), 'completed model evidence must stay saved'
        engine.denied = False
        duplicate, replayed = await service.submit(key, body)
        assert replayed and duplicate['id'] == parent['id']
        await asyncio.sleep(.1)
        assert len(engine.dispatches) == 2, 'restoring access cannot automatically replay an accepted job'
        child, _ = await finished_job(service, resume_request(parent, maxModelCalls=10))
        assert child['id'] != parent['id'] and child['state'] == 'succeeded'
        assert all(call['page'] != 1 for call in engine.dispatches if call['jobId'] == child['id'])
        assert sealed_tree(service, parent)[1] == before
        assert sealed.is_dir()
    finally:
        await service.close()
