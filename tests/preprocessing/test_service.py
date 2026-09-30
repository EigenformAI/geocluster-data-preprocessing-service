import asyncio
import hashlib
import os
import random
import sys
import time
import uuid

import pytest

from preprocessing.errors import ServiceError
from preprocessing.service import Service


async def terminal(service, job):
    async with asyncio.timeout(20):
        while ((current := service.get(job['id']))['state'] in {'queued', 'running', 'cancelling'} or service.active == job['id']):
            await asyncio.sleep(.02)
    return current


@pytest.mark.asyncio
async def test_snapshot_replay_and_full_typed_publication(tmp_path):
    source = tmp_path / 'input.csv'
    source.write_text('id,value\n001,1.000000000000000001\n002,\n')
    service = Service(tmp_path / 'state', tmp_path)
    await service.start()
    try:
        key = str(uuid.uuid4())
        body = {'source': {'kind': 'workspace-file', 'path': 'input.csv'}}
        async def submit():
            await asyncio.sleep(random.random() / 100)
            return await service.submit(key, body)
        responses = await asyncio.gather(*(submit() for _ in range(40)))
        assert len({item[0]['id'] for item in responses}) == 1
        assert sum(not replay for _, replay in responses) == 1
        job = await terminal(service, responses[0][0])
        assert job['state'] == 'succeeded', job
        manifest = service.get_result(job['id'])
        assert manifest['source']['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
        rows = service.read_table(job['id'], manifest['tables'][0]['id'])
        assert rows['rows'][1]['cells'][0]['value'] == '001'
        assert rows['rows'][1]['cells'][1]['value'] == '1.000000000000000001'
        assert job['documents']['state'] == 'ready'
        assert (tmp_path / 'state' / job['documents']['path'] / 'AGENTS.md').is_file()
        for product in manifest['products']:
            path, _ = service.product(job['id'], product['id'])
            assert hashlib.sha256(path.read_bytes()).hexdigest() == product['sha256']
        source.write_text('changed')
        replay, found = await service.submit(key, body)
        assert found and replay['id'] == job['id']
        with pytest.raises(ServiceError, match='different'):
            await service.submit(key, {'source': {'kind': 'workspace-file', 'path': 'other.csv'}})
        rerun, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'snapshot', 'snapshotId': job['source']['snapshotId']}, 'rerunOf': job['id']})
        assert (await terminal(service, rerun))['state'] == 'succeeded'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_hash_mismatch_and_symlink_are_nonbillable(tmp_path):
    (tmp_path / 'data.csv').write_text('001,2\n')
    (tmp_path / 'link.csv').symlink_to(tmp_path / 'data.csv')
    service = Service(tmp_path / 'state', tmp_path)
    await service.start()
    try:
        for src, code in [({'kind': 'workspace-file', 'path': 'data.csv', 'expectedSha256': '0' * 64}, 'SOURCE_CHANGED'),
                          ({'kind': 'workspace-file', 'path': 'link.csv'}, 'SOURCE_READ_FAILED')]:
            job, _ = await service.submit(str(uuid.uuid4()), {'source': src})
            done = await terminal(service, job)
            assert done['state'] == 'failed'
            assert done['error']['code'] == code
            assert service.store.operations(job['id']) == []
            assert list((service.private / 'sources').iterdir()) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_owner_lock_restart_and_cancel(tmp_path):
    service = Service(tmp_path / 'state', tmp_path)
    await service.start()
    other = Service(tmp_path / 'state', tmp_path)
    with pytest.raises(ServiceError, match='owner'):
        await other.start()
    job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
    cancelled = await service.cancel(job['id'])
    assert cancelled['state'] == 'cancelled'
    assert service.lookup(job['submissionKey'])['id'] == job['id']
    await service.close()
    await other.start()
    assert other.get(job['id'])['state'] == 'cancelled'
    await other.close()


@pytest.mark.asyncio
async def test_request_schema_and_oversized_row(tmp_path):
    (tmp_path / 'large.csv').write_text('x' * 10000 + '\n')
    from preprocessing.contracts import default_profile
    profile = default_profile()
    profile['limits']['responseBytes'] = 2048
    service = Service(tmp_path / 'state', tmp_path, profile)
    await service.start()
    try:
        with pytest.raises(ServiceError):
            await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'large.csv'}, 'stages': []})
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'large.csv'}})
        job = await terminal(service, job)
        assert job['state'] == 'succeeded', job
        table = service.get_result(job['id'])['tables'][0]
        with pytest.raises(ServiceError) as err:
            service.read_table(job['id'], table['id'])
        assert err.value.code == 'ROW_TOO_LARGE'
        assert table['downloads'][0]['productId'] in err.value.message
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancellation_publication_jitter_keeps_next_job_alive(tmp_path):
    (tmp_path / 'data.csv').write_text('001,2\n' * 100)
    service = Service(tmp_path / 'state', tmp_path)
    await service.start()
    try:
        for _ in range(15):
            first, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
            second, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
            await asyncio.sleep(random.uniform(0, .10))
            cancelled = await service.cancel(first['id'])
            assert cancelled['state'] in {'cancelled', 'succeeded'}
            assert (await terminal(service, second))['state'] == 'succeeded'
            if cancelled['state'] == 'cancelled':
                with pytest.raises(ServiceError) as error:
                    service.get_result(first['id'])
                assert error.value.code == 'RESULT_NOT_AVAILABLE'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_shared_publication_failure_reconciles_without_extraction(tmp_path, monkeypatch):
    import preprocessing.service as module
    (tmp_path / 'data.csv').write_text('001,2\n')
    service = Service(tmp_path / 'state', tmp_path)
    await service.start()
    original = module.shutil.copytree
    monkeypatch.setattr(module.shutil, 'copytree', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        job = await terminal(service, job)
        while service.active:
            await asyncio.sleep(.02)
        job = service.get(job['id'])
        assert job['state'] == 'succeeded' and job['documents']['state'] == 'failed'
        manifest = service.get_result(job['id'])
        monkeypatch.setattr(module.shutil, 'copytree', original)
        assert (await service.reconcile_documents(job['id']))['state'] == 'ready'
        assert service.get_result(job['id']) == manifest
        assert service.store.operations(job['id']) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_lost_publication_commit_is_not_visible_after_restart(tmp_path, monkeypatch):
    (tmp_path / 'data.csv').write_text('001,2\n')
    service = Service(tmp_path / 'state', tmp_path)
    await service.start()
    original = service.store.save

    def lose_commit(job):
        if job.get('resultId'):
            raise OSError('lost owner before result commit')
        return original(job)

    monkeypatch.setattr(service.store, 'save', lose_commit)
    job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
    done = await terminal(service, job)
    assert done['state'] == 'failed'
    assert (service.private / 'jobs' / job['id'] / 'sealed').exists()
    with pytest.raises(ServiceError):
        service.get_result(job['id'])
    await service.close()
    reopened = Service(tmp_path / 'state', tmp_path)
    await reopened.start()
    assert reopened.get(job['id'])['resultUrl'] is None
    assert not list((tmp_path / 'state' / 'documents').glob('result_*'))
    await reopened.close()


@pytest.mark.asyncio
async def test_history_cursor_excludes_new_admissions_and_checks_filters(tmp_path):
    service = Service(tmp_path / 'state', tmp_path)
    await service.start()
    try:
        jobs = [(await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': f'{i}.csv'}}))[0] for i in range(5)]
        page = service.list_jobs(limit=2)
        newer, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'new.csv'}})
        found = list(page['jobs'])
        while page['nextCursor']:
            page = service.list_jobs(limit=2, cursor=page['nextCursor'])
            found.extend(page['jobs'])
        assert {item['id'] for item in found} == {item['id'] for item in jobs}
        assert newer['id'] not in {item['id'] for item in found}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_active_model_session_is_persisted_before_dispatch(tmp_path):
    class Engine:
        owner_fd = None
        on_session = None
        async def start(self, operation_id, job_id, generation, profile, refs, operation):
            await self.on_session(operation_id, job_id, 'session_test')
            saved = service.store.operations(job_id)[0]
            assert saved['sessionId'] == 'session_test'
            assert 'session_test' in service.internal_sessions
            return {'text': 'transcription', 'usage': None, 'sessionId': 'session_test'}
        async def close(self):
            pass
        async def cancel(self, job_id):
            return {'settled': True}
    engine = Engine()
    service = Service(tmp_path / 'state', tmp_path, engine=engine)
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        job['state'] = 'running'
        service.store.save(job)
        reply = await service._model(job['id'], tmp_path, {'operation': {'kind': 'figure-ocr'}, 'inputRefs': []})
        assert reply['result']['text'] == 'transcription'
        assert service.store.operations(job['id'])[0]['sessionIds'] == ['session_test']
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unsettled_engine_cannot_publish_or_admit_more_work(tmp_path):
    class Engine:
        async def start(self, *args):
            exc = ServiceError('PROVIDER_OUTCOME_UNKNOWN', 'unknown')
            exc.settlement = {'settled': False}
            raise exc
        async def cancel(self, job):
            return {'settled': False}
        async def close(self):
            pass
    service = Service(tmp_path / 'state', tmp_path, engine=Engine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        job['state'] = 'running'
        service.store.save(job)
        with pytest.raises(ServiceError):
            await service._model(job['id'], tmp_path, {'operation': {'kind': 'ocr'}, 'inputRefs': []})
        assert service.get(job['id'])['state'] == 'cancelling'
        with pytest.raises(ServiceError) as error:
            await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        assert error.value.status == 503
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_wide_table_empty_page_obeys_byte_budget(tmp_path):
    from preprocessing.contracts import default_profile
    (tmp_path / 'wide.csv').write_text(','.join('v' for _ in range(100)) + '\n')
    profile = default_profile()
    profile['limits']['responseBytes'] = 2048
    service = Service(tmp_path / 'state', tmp_path, profile)
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'wide.csv'}})
        job = await terminal(service, job)
        table = service.get_result(job['id'])['tables'][0]
        with pytest.raises(ServiceError) as error:
            service.read_table(job['id'], table['id'], offset=table['rows'])
        assert error.value.code == 'RESPONSE_TOO_LARGE'
        assert table['downloads'][0]['productId'] in error.value.message
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_poisoned_engine_does_not_block_native_cancel(tmp_path):
    class Engine:
        async def cancel(self, job_id):
            return {'settled': False}
        async def close(self):
            pass
    (tmp_path / 'data.csv').write_text('001,2\n' * 10000)
    service = Service(tmp_path / 'state', tmp_path, engine=Engine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        async with asyncio.timeout(5):
            while not service.process:
                await asyncio.sleep(.001)
        assert (await service.cancel(job['id']))['state'] == 'cancelled'
        assert service.process is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_publication_does_not_block_event_loop(tmp_path, monkeypatch):
    import threading

    import preprocessing.service as module
    (tmp_path / 'data.csv').write_text('001,2\n' * 100)
    service = Service(tmp_path / 'state', tmp_path)
    await service.start()
    copying = threading.Event()
    responsive = threading.Event()
    original = module.shutil.copytree

    def slow_copy(*args, **kwargs):
        copying.set()
        assert responsive.wait(2), 'publication blocked concurrent status and cancellation'
        return original(*args, **kwargs)

    monkeypatch.setattr(module.shutil, 'copytree', slow_copy)
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        async with asyncio.timeout(5):
            while not copying.is_set():
                await asyncio.sleep(.001)
        assert service.get(job['id'])['state'] == 'succeeded'
        assert (await service.cancel(job['id']))['state'] == 'succeeded'
        responsive.set()
        while service.active:
            await asyncio.sleep(.01)
        assert service.get(job['id'])['documents']['state'] == 'ready'
    finally:
        responsive.set()
        await service.close()


@pytest.mark.asyncio
async def test_staged_output_budget_stops_parser_before_full_export(tmp_path):
    from preprocessing.contracts import default_profile
    rows = 100000
    (tmp_path / 'data.csv').write_text('001,2\n' * rows)
    profile = default_profile()
    profile['limits']['outputBytes'] = 20000
    service = Service(tmp_path / 'state', tmp_path, profile)
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
        job = await terminal(service, job)
        assert job['state'] == 'failed'
        assert job['error']['code'] == 'RESOURCE_LIMIT'
        assert not job.get('resultId')
        exports = list((service.private / 'jobs' / job['id'] / 'staging').rglob('*.jsonl'))
        assert exports
        assert sum(1 for _ in exports[0].open()) < rows
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_owner_death_fences_worker_and_interrupts_job(tmp_path):
    (tmp_path / 'data.csv').write_text('001,2\n' * 500000)
    code = '''
import asyncio, sys, uuid
from pathlib import Path
from preprocessing.service import Service
async def main():
    root = Path(sys.argv[1])
    service = Service(root / 'state', root)
    await service.start()
    job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'data.csv'}})
    while service.process is None:
        await asyncio.sleep(.001)
    (root / 'job-id').write_text(job['id'])
    await asyncio.sleep(60)
asyncio.run(main())
'''
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', code, str(tmp_path), stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(10):
            while not (tmp_path / 'job-id').exists():
                await asyncio.sleep(.005)
        child.kill()
        await child.wait()
        service = Service(tmp_path / 'state', tmp_path)
        async with asyncio.timeout(10):
            while True:
                try:
                    await service.start()
                    break
                except ServiceError as exc:
                    assert exc.code == 'OWNER_ACTIVE'
                    await asyncio.sleep(.02)
        try:
            job = service.get((tmp_path / 'job-id').read_text())
            assert job['state'] == 'interrupted'
            assert job['resultUrl'] is None
            assert not list((tmp_path / 'state/documents').glob('result_*'))
        finally:
            await service.close()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@pytest.mark.asyncio
async def test_fenced_orphans_reclaimed_after_grace_only(tmp_path):
    from preprocessing.contracts import default_profile
    profile = default_profile()
    profile['limits']['orphanGraceSeconds'] = 1
    service = Service(tmp_path / 'state', tmp_path, profile)
    await service.start()
    job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
    await service.cancel(job['id'])
    orphan = service.private / 'jobs' / job['id'] / 'sealed'
    orphan.mkdir(parents=True)
    (orphan / 'unfinished').write_text('unpublished')
    fresh = orphan.parent / 'staging'
    fresh.mkdir()
    retained = service.private / 'sources/source_retained'
    abandoned = service.private / 'sources/source_abandoned'
    recent = service.private / 'sources/source_recent'
    for directory in (retained, abandoned, recent):
        directory.mkdir()
        (directory / 'source').write_text('preserve exact bytes')
    cancelled = service.get(job['id'])
    cancelled['source'] = {'snapshotId': retained.name}
    service.store.save(cancelled)
    for directory in (retained, abandoned):
        os.utime(directory, (time.time() - 20, time.time() - 20))
    os.utime(orphan, (time.time() - 20, time.time() - 20))
    await service.close()
    reopened = Service(tmp_path / 'state', tmp_path, profile)
    await reopened.start()
    try:
        assert not orphan.exists()
        assert fresh.exists()
        assert (retained / 'source').read_text() == 'preserve exact bytes'
        assert not abandoned.exists()
        assert recent.exists()
        assert reopened.get(job['id'])['state'] == 'cancelled'
    finally:
        await reopened.close()


def test_manifest_rejects_tampering_and_unlisted_navigation(tmp_path):
    from preprocessing.results import validate_generation
    (tmp_path / 'index.md').write_text('[escape](../secret)')
    item = {'id': 'p', 'path': 'index.md', 'kind': 'navigation-index',
            'bytes': (tmp_path / 'index.md').stat().st_size,
            'sha256': hashlib.sha256((tmp_path / 'index.md').read_bytes()).hexdigest()}
    manifest = {'products': [item], 'figures': []}
    with pytest.raises(ServiceError, match='unlisted'):
        validate_generation(tmp_path, manifest)
    item['sha256'] = '0' * 64
    with pytest.raises(ServiceError, match='bytes'):
        validate_generation(tmp_path, manifest)


def test_worker_metadata_is_not_charged_against_product_budget(tmp_path):
    from preprocessing.worker import Context
    (tmp_path / 'data.txt').write_bytes(b'x' * 99)
    (tmp_path / 'extraction.json').write_bytes(b'x' * 2000)
    Context(tmp_path, 100).check_output()


def test_catalog_pages_bound_serialized_unicode_bytes(tmp_path):
    from preprocessing.contracts import default_profile
    profile = default_profile()
    profile['limits']['indexBytes'] = 32768
    service = Service(tmp_path, tmp_path, profile)
    (tmp_path / 'documents').mkdir()
    entries = [{'resultId': f'result_{i}', 'jobId': f'job_{i}',
                'source': {'sha256': 'a' * 64, 'origin': {'kind': 'workspace-file', 'path': 'é' * 60 + '.csv'}},
                'coverage': {'unit': 'row', 'total': 5, 'completed': 5, 'failed': 0},
                'profile': 'b' * 64, 'outcome': 'succeeded', 'path': f'result_{i}/index.md'} for i in range(200)]
    service._write_catalog(entries)
    for path in (tmp_path / 'documents').rglob('*'):
        if path.is_file():
            assert path.stat().st_size <= 32768
