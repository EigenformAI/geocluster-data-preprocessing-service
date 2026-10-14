"""Observable processing progress, FIFO position, and interruption metadata."""
import asyncio
import json
import threading
from datetime import datetime
from uuid import uuid4

import pytest
from fakes import FakeEngine
from test_service import terminal

from preprocessing.errors import ServiceError
from preprocessing.service import Service


def scanned_pdf(path, pages):
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    image = Image.new('RGB', (90, 120), 'white')
    ImageDraw.Draw(image).text((5, 5), 'Scanned text', fill='black')
    pdf = canvas.Canvas(str(path), pagesize=(90, 120))
    for _ in range(pages):
        pdf.drawImage(ImageReader(image), 0, 0, 90, 120)
        pdf.showPage()
    pdf.save()
    image.close()


async def test_eighty_scanned_pages_are_live_before_sealing_and_retained(tmp_path, monkeypatch):
    scanned_pdf(tmp_path / 'scan.pdf', 80)
    admitted, release = asyncio.Event(), asyncio.Event()
    sealing, finish = threading.Event(), threading.Event()
    samples, phases = [], []

    class Engine(FakeEngine):
        async def start(self, operation_id, job_id, generation, profile, refs, operation):
            if operation['kind'] == 'page-ocr':
                samples.append(service.get(job_id))
                if operation['sourceUnit']['page'] == 1:
                    admitted.set()
                    await release.wait()
            await self.on_operation(operation_id, job_id, {'state': 'completed', 'settlement': {'settled': True}})
            return {'text': json.dumps({'text': 'Scanned text', 'tables': []})
                    if operation['kind'] == 'page-ocr' else 'Scanned text'}

    service = Service(tmp_path / 'state', tmp_path, engine=Engine())
    await service.start()
    original = service._finalize

    def pause_sealing(*args):
        sealing.set()
        assert finish.wait(20), 'test did not release finalization'
        return original(*args)

    original_save = service.store.save

    def observe(job):
        if job.get('stage'):
            phases.append(job['stage']['id'])
        return original_save(job)

    monkeypatch.setattr(service, '_finalize', pause_sealing)
    monkeypatch.setattr(service.store, 'save', observe)
    try:
        job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'scan.pdf'}})
        async with asyncio.timeout(10):
            await admitted.wait()
        running = service.get(job['id'])
        assert running['state'] == 'running'
        assert running['interruptedAt'] is None
        assert running['progress']['completedUnits'] == 0
        assert running['progress']['totalUnits'] == 80
        assert running['progress']['unit'] == 'page'
        assert running['finishedAt'] is None
        started = running['startedAt']
        release.set()
        async with asyncio.timeout(90):
            while not sealing.is_set():
                await asyncio.sleep(.005)
        live = service.get(job['id'])
        assert live['state'] == 'running' and live.get('resultId') is None
        assert live['stage']['id'] == 'seal'
        assert live['progress']['completedUnits'] == live['progress']['totalUnits'] == 80
        assert live['progress']['label'] == 'Inspecting pages'
        assert [sample['progress']['completedUnits'] for sample in samples] == list(range(80))
        assert all(sample['progress']['totalUnits'] == 80 for sample in samples)
        assert {'snapshot', 'extract', 'resolve-tables', 'build-index', 'seal'} <= set(phases)
        finish.set()
        done = await terminal(service, job)
        assert done['state'] == 'succeeded', done
        assert done['stage'] is None and done['progress'] == live['progress']
        assert done['startedAt'] == started
        assert datetime.fromisoformat(done['finishedAt']) >= datetime.fromisoformat(started)
        assert done['queueAhead'] is None
        await service._record_progress(job['id'], {'id': 'extract', 'completedUnits': 79, 'totalUnits': 80})
        assert service.get(job['id']) == done
    finally:
        release.set()
        finish.set()
        await service.close()


async def test_queue_position_uses_durable_fifo_and_drops_terminal_predecessors(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        body = {'source': {'kind': 'workspace-file', 'path': 'absent.pdf'}}
        jobs = [(await service.submit(str(uuid4()), body))[0] for _ in range(5)]
        assert [job['queueAhead'] for job in jobs] == [0, 1, 2, 3, 4]
        for index, job in enumerate(jobs):
            saved = service.store.get(job['id'])
            saved['createdAt'] = f'2000-01-01T00:00:0{4-index}+00:00'
            service.store.save(saved)
        first = service.get(jobs[0]['id'])
        first.update(state='running', startedAt='2000-01-01T00:00:00+00:00')
        service.store.save(first)
        assert service.get(first['id'])['queueAhead'] is None
        assert [service.get(job['id'])['queueAhead'] for job in jobs[1:]] == [1, 2, 3, 4]
        first['state'] = 'cancelling'
        service.store.save(first)
        assert service.get(jobs[1]['id'])['queueAhead'] == 1
        await service.cancel(jobs[0]['id'])
        await service.cancel(jobs[2]['id'])
        assert [service.get(job['id'])['queueAhead'] for job in jobs] == [None, 0, None, 1, 2]
        assert service.lookup(jobs[4]['submissionKey'])['queueAhead'] == 2
        assert (await service.submit(jobs[4]['submissionKey'], body))[0]['queueAhead'] == 2
        assert service.list_jobs(submissionKey=jobs[4]['submissionKey'])['job']['queueAhead'] == 2
        listed = service.list_jobs(limit=1, state='queued')
        assert listed['jobs'][0]['queueAhead'] == 2  # Count outside this filtered page.
        assert service.get(jobs[1]['id'])['startedAt'] is None
        assert service.get(jobs[1]['id'])['finishedAt'] is None
    finally:
        await service.close()


async def test_progress_validation_and_late_messages_cannot_revive_job(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        job.update(state='running', stage=None)
        service.store.save(job)
        await service._record_progress(job['id'], {'id': 'extract', 'completedUnits': 0, 'totalUnits': None})
        assert service.get(job['id'])['progress']['totalUnits'] is None
        message = {'id': 'extract', 'completedUnits': 2, 'totalUnits': 80}
        await service._record_progress(job['id'], message)
        good = service.get(job['id'])
        assert good['progress']['completedUnits'] == 2
        datetime.fromisoformat(good['progress']['updatedAt'])
        for patch in ({'id': 'invented'}, {'id': []}, {'completedUnits': True},
                      {'completedUnits': 2.0}, {'completedUnits': -1}, {'completedUnits': 1},
                      {'completedUnits': 81}, {'totalUnits': True}, {'totalUnits': -1},
                      {'totalUnits': 79}, {'totalUnits': None}, {'updatedAt': 'worker-time'}):
            with pytest.raises(ServiceError) as error:
                await service._record_progress(job['id'], {**message, **patch})
            assert error.value.code == 'INVALID_PROGRESS'
            assert service.get(job['id']) == good
        await service._record_progress(job['id'], {'id': 'build-index', 'completedUnits': 0, 'totalUnits': 1})
        assert service.get(job['id'])['progress'] == good['progress']
        with pytest.raises(ServiceError):
            await service._record_progress(job['id'], message)
        cancelled = await service.cancel(job['id'])
        await service._record_progress(job['id'], {**message, 'completedUnits': 80})
        assert service.get(job['id']) == cancelled
        assert cancelled['stage'] is None and cancelled['progress'] == good['progress']
    finally:
        await service.close()


@pytest.mark.parametrize('pending_settlement', [False, True])
@pytest.mark.parametrize('prior_state', ['running', 'cancelling'])
async def test_restart_clears_current_stage_but_preserves_progress_and_start(tmp_path, pending_settlement, prior_state):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
    progress = {'id': 'extract', 'label': 'Inspecting pages', 'unit': 'page',
                'completedUnits': 12, 'totalUnits': 80, 'updatedAt': '2000-01-01T00:00:12+00:00'}
    job.update(state=prior_state, startedAt='2000-01-01T00:00:00+00:00',
               stage={key: value for key, value in progress.items() if key != 'updatedAt'}, progress=progress)
    service.store.save(job)
    if pending_settlement:
        service.store.operation({'id': 'pending', 'jobId': job['id'], 'generation': service.generation,
                                 'state': 'outcome_unknown', 'sessionId': 'old-session',
                                 'settlement': {'settled': False}})
    await service.close()
    engine = FakeEngine()
    engine.recovery_settled = not pending_settlement
    engine.descriptor['recovery'] = {'fencedGenerations': [service.generation], 'deadEngineInstances': []}
    reopened = Service(tmp_path / 'state', tmp_path, engine=engine)
    await reopened.start()
    try:
        stopped = reopened.get(job['id'])
        marker = stopped['interruptedAt']
        assert marker is not None
        datetime.fromisoformat(marker)
        assert stopped['state'] == ('cancelling' if pending_settlement else 'interrupted')
        assert stopped['stage'] is None
        assert stopped['progress'] == progress
        assert stopped['startedAt'] == job['startedAt']
        assert (stopped['finishedAt'] is None) == pending_settlement
        generation = reopened.generation
        await reopened.close()
        engine = FakeEngine()
        engine.recovery_settled = not pending_settlement
        engine.descriptor['recovery'] = {'fencedGenerations': [generation], 'deadEngineInstances': []}
        reopened = Service(tmp_path / 'state', tmp_path, engine=engine)
        await reopened.start()
        fresh = reopened.get(job['id'])
        assert fresh['interruptedAt'] == marker
        assert fresh['state'] == stopped['state']
        assert fresh['startedAt'] == stopped['startedAt'] and fresh['finishedAt'] == stopped['finishedAt']
        if pending_settlement:
            await reopened._record_progress(job['id'], {'id': 'extract', 'completedUnits': 80, 'totalUnits': 80})
            assert reopened.get(job['id']) == fresh
            engine.recovery_settled = True
            assert (await reopened.reconcile_engine())['settled']
            interrupted = reopened.get(job['id'])
            assert interrupted['state'] == 'interrupted' and interrupted['finishedAt'] is not None
            assert interrupted['stage'] is None and interrupted['progress'] == progress
            assert interrupted['interruptedAt'] == marker
    finally:
        await reopened.close()


async def test_restart_marks_never_started_queued_job(tmp_path):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'queued.pdf'}})
    try:
        assert job['state'] == 'queued' and job['interruptedAt'] is None
    finally:
        await service.close()
    reopened = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await reopened.start()
    try:
        stopped = reopened.get(job['id'])
        assert stopped['state'] == 'interrupted' and stopped['interruptedAt'] is not None
        assert stopped['startedAt'] is None and stopped['finishedAt'] is not None
        assert stopped['progress'] is None
    finally:
        await reopened.close()
