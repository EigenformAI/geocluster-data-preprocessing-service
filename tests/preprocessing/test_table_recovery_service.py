"""Caller policy, coordinator deadlines, and immutable resume boundaries."""
import asyncio
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path

import pytest
from fakes import FakeEngine
from test_resume_evidence import build_parent_evidence

from preprocessing.contracts import default_profile
from preprocessing.errors import ServiceError
from preprocessing.service import Service, canonical

PDF_ROWS = {1: [['Code', 'Value'], ['001', '1.2300']],
            2: [['Code', 'Value'], ['002', '2.3400']]}


def native_table_pdf(path):
    from reportlab.pdfgen import canvas
    pdf = canvas.Canvas(str(path), pagesize=(300, 400))
    for page, rows in PDF_ROWS.items():
        pdf.drawString(20, 370, f'Native prose page {page}')
        for x in (20, 120, 220):
            pdf.line(x, 250, x, 330)
        for y in (250, 290, 330):
            pdf.line(20, y, 220, y)
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                pdf.drawString(25 + 100 * c, 305 - 40 * r, value)
        pdf.showPage()
    pdf.save()


class TableEngine(FakeEngine):
    """Deterministic source-page transcripts through the real coordinator RPC."""

    def __init__(self, *, dispute=False):
        super().__init__()
        self.calls = []
        self.dispute = dispute

    async def start(self, oid, jid, generation, profile, refs, operation):
        from PIL import Image
        assert len(refs) == 1
        with Image.open(refs[0]['path']) as image:
            assert image.width > 0 and image.height > 0
        raw = Path(refs[0]['path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == refs[0]['sha256']
        assert len(raw) == refs[0]['bytes']
        page, kind = operation['sourceUnit']['page'], operation['kind']
        self.calls.append({'jobId': jid, 'kind': kind, 'page': page})
        if kind == 'table-scout':
            text = json.dumps({'tables': []})
        else:
            assert kind in {'table-confirmation', 'table-reread'}
            rows = json.loads(json.dumps(PDF_ROWS[page]))
            if self.dispute and page == 1:
                rows[1][1] = '-1.2300' if kind == 'table-confirmation' else '3.4500'
            text = json.dumps({'tablePresent': True, 'rowCount': len(rows), 'columnCount': 2,
                               'cells': [{'row': r, 'column': c, 'text': value}
                                         for r, row in enumerate(rows, 1) for c, value in enumerate(row, 1)]})
        await self.on_operation(oid, jid, {'state': 'completed', 'sessionId': 'session_' + oid,
                                         'settlement': {'settled': True}})
        return {'text': text, 'requestedModel': 'fixture/vision', 'returnedModel': 'fixture/vision',
                'operationId': oid, 'usage': {'tokens': {'input': 1, 'output': 1}}}


async def finished_job(service, body):
    job, replayed = await service.submit(str(uuid.uuid4()), body)
    assert not replayed
    async with asyncio.timeout(20):
        while service.get(job['id'])['state'] not in {'succeeded', 'partial', 'failed'} or service.active == job['id']:
            await asyncio.sleep(.02)
    job = service.get(job['id'])
    assert job['state'] != 'failed', job.get('error')
    return job, service.get_result(job['id'])


def resume_request(parent, **overrides):
    return {'source': {'kind': 'snapshot', 'snapshotId': parent['source']['snapshotId']},
            'resumeOf': parent['id'], 'tableRecovery': overrides}


def sealed_tree(service, job):
    root = service.private / 'jobs' / job['id'] / 'sealed'
    return root, {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def assert_self_contained(service, job, manifest):
    from preprocessing.results import validate_generation
    root = service.private / 'jobs' / job['id'] / 'sealed'
    validate_generation(root, manifest)
    products = {p['path']: p for p in manifest['products']}
    assert len(products) == len(manifest['products'])
    for path, product in products.items():
        raw = (root / path).read_bytes()
        assert len(raw) == product['bytes']
        assert hashlib.sha256(raw).hexdigest() == product['sha256']
        assert product['contentUrl'] == f"/preprocessing/jobs/{job['id']}/products/{product['id']}"
    plan = json.loads((root / next(p['path'] for p in products.values() if p['kind'] == 'work-plan')).read_text())
    units = {unit['id']: unit for unit in plan['units']}
    for unit in units.values():
        for path in unit['evidencePaths']:
            assert path in products
            assert (root / path).is_file()
        if unit['state'] == 'completed':
            assert all(units[dep]['state'] == 'completed' for dep in unit['dependencies'])
    for table in manifest['tables']:
        for path in [table['typedPath'], table['csvPath'], *table['evidencePaths'].values()]:
            assert path in products
        rows = [json.loads(line) for line in (root / table['typedPath']).read_text().splitlines()]
        assert [[cell['value'] for cell in row['cells']] for row in rows] == PDF_ROWS[table['source']['page']]
        candidates = [json.loads(line) for line in (root / table['evidencePaths']['candidates']).read_text().splitlines()]
        assert any(c['id'] == table['selectedCandidateId'] for c in candidates)
        for key in ('structure', 'decisions'):
            evidence = json.loads((root / table['evidencePaths'][key]).read_text())
            assert evidence['tableId'] == table['id']
            assert evidence['selectedCandidateId'] == table['selectedCandidateId']
    return root, products, units


@pytest.mark.asyncio
@pytest.mark.parametrize('pending_publication', [False, True])
async def test_worker_pdf_partial_resume_reuses_exact_completed_evidence(tmp_path, monkeypatch, pending_publication):
    native_table_pdf(tmp_path / 'tables.pdf')
    profile = default_profile()
    profile['models']['vision'] = 'fixture/vision'
    engine = TableEngine()
    service = Service(tmp_path / 'state', tmp_path, profile=profile, engine=engine)
    await service.start()
    if pending_publication:
        async def defer_publication(job_id):
            return {'state': 'pending', 'path': None}
        monkeypatch.setattr(service, 'reconcile_documents', defer_publication)
    try:
        parent, first = await finished_job(service, {
            'source': {'kind': 'workspace-file', 'path': 'tables.pdf'}, 'tableRecovery': {'maxModelCalls': 1}})
        assert parent['state'] == 'partial'
        assert [t['confirmation']['state'] for t in first['tables']] == ['agreed', 'pending']
        assert [(c['kind'], c['page']) for c in engine.calls] == [('table-confirmation', 1)]
        assert parent['documents']['state'] == ('pending' if pending_publication else 'ready')
        old_root, before = sealed_tree(service, parent)
        _, old_products, old_units = assert_self_contained(service, parent, first)
        # Resume must render the retained snapshot, even if the workspace changes.
        (tmp_path / 'tables.pdf').write_bytes(b'changed workspace source')
        child, second = await finished_job(service, resume_request(parent, maxModelCalls=4))
        assert child['state'] == 'succeeded'
        assert child['source'] == parent['source']
        assert child['submissionKey'] != parent['submissionKey']
        assert [(c['kind'], c['page']) for c in engine.calls if c['jobId'] == child['id']] == [('table-confirmation', 2)]
        assert [t['confirmation']['state'] for t in second['tables']] == ['agreed', 'agreed']
        assert second['coverage']['completed'] == 2 and second['coverage']['notProcessed'] == 0
        assert second['coverage']['tableConfirmation']['modelCalls'] == 1
        new_root, new_products, new_units = assert_self_contained(service, child, second)
        assert second['tables'][0]['id'] == first['tables'][0]['id']
        assert second['tables'][0]['selectedCandidateId'] == first['tables'][0]['selectedCandidateId']
        paths = {path for unit in old_units.values() if unit['state'] == 'completed' and unit['source']['page'] == 1
                 for path in unit['evidencePaths']}
        assert any(old_products[path]['kind'] == 'table-image' for path in paths)
        assert any(old_products[path]['kind'] == 'table-model-response' for path in paths)
        for path in paths:
            assert (new_root / path).read_bytes() == before[path]
            assert (new_root / path).stat().st_ino != (old_root / path).stat().st_ino
            for field in ('source', 'render', 'model'):
                if field in old_products[path]:
                    assert new_products[path].get(field) == old_products[path][field]
            assert new_products[path]['id'] != old_products[path]['id']
        assert all(new_units[u['id']]['reusedFromJob'] == parent['id'] for u in old_units.values() if u['state'] == 'completed')
        assert sealed_tree(service, parent)[1] == before
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_worker_pdf_force_page_invalidates_only_its_closure(tmp_path):
    native_table_pdf(tmp_path / 'tables.pdf')
    profile = default_profile()
    profile['models']['vision'] = 'fixture/vision'
    engine = TableEngine()
    service = Service(tmp_path / 'state', tmp_path, profile=profile, engine=engine)
    await service.start()
    try:
        parent, first = await finished_job(service, {'source': {'kind': 'workspace-file', 'path': 'tables.pdf'}})
        assert parent['state'] == 'succeeded'
        _, before = sealed_tree(service, parent)
        child, second = await finished_job(service, resume_request(parent, forcePages=[1]))
        assert child['state'] == 'succeeded'
        assert [(c['kind'], c['page']) for c in engine.calls if c['jobId'] == child['id']] == [
            ('table-scout', 1), ('table-confirmation', 1)]
        _, products, units = assert_self_contained(service, child, second)
        old_by_path = {p['path']: p for p in first['products']}
        for path, old_product in old_by_path.items():
            if old_product.get('source', {}).get('page') == 2:
                product = products[path]
                assert (service.private / 'jobs' / child['id'] / 'sealed' / path).read_bytes() == before[path]
                assert product.get('source') == old_product['source']
                assert product.get('render') == old_product.get('render')
                assert product.get('model') == old_product.get('model')
        assert all(u.get('reusedFromJob') == parent['id'] for u in units.values()
                   if u['state'] == 'completed' and u['source']['page'] == 2)
        assert all('reusedFromJob' not in u for u in units.values() if u['source']['page'] == 1)
        # Omission of a newly explicit force override must reuse previously forced work.
        _, child_before = sealed_tree(service, child)
        grandchild, third = await finished_job(service, resume_request(child, maxModelCalls=4))
        assert grandchild['state'] == 'succeeded'
        assert [c for c in engine.calls if c['jobId'] == grandchild['id']] == []
        assert_self_contained(service, grandchild, third)
        assert sealed_tree(service, child)[1] == child_before
        assert sealed_tree(service, parent)[1] == before
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_worker_pdf_resume_preserves_cumulative_dispute_history(tmp_path):
    native_table_pdf(tmp_path / 'tables.pdf')
    profile = default_profile()
    profile['models']['vision'] = 'fixture/vision'
    engine = TableEngine(dispute=True)
    service = Service(tmp_path / 'state', tmp_path, profile=profile, engine=engine)
    await service.start()
    try:
        parent, first = await finished_job(service, {'source': {'kind': 'workspace-file', 'path': 'tables.pdf'}})
        assert parent['state'] == 'partial'
        root, before = sealed_tree(service, parent)
        _, old_products, old_units = assert_self_contained(service, parent, first)
        confirmation_id = first['tables'][0]['id'] + ':confirmation'
        assert old_units[confirmation_id]['passesSpent'] == 1
        assert [(c['kind'], c['page']) for c in engine.calls] == [
            ('table-confirmation', 1), ('table-confirmation', 2), ('table-reread', 1)]
        decision_path = first['tables'][0]['evidencePaths']['decisions']
        history = json.loads((root / decision_path).read_text())
        assert history['comparisons'] and history['passesSpent'] == 1
        child, second = await finished_job(service, resume_request(parent, maxModelCalls=4))
        assert child['state'] == 'partial'
        assert [c for c in engine.calls if c['jobId'] == child['id']] == []
        second_root, second_products, second_units = assert_self_contained(service, child, second)
        assert second_units[confirmation_id]['passesSpent'] == 1
        retained = json.loads((second_root / decision_path).read_text())
        assert retained['comparisons'][:len(history['comparisons'])] == history['comparisons']
        assert retained['passesSpent'] == 1
        response_paths = {p for p in history['evidencePaths'] if old_products[p]['kind'] == 'table-model-response'}
        assert len(response_paths) == 2
        for path in response_paths:
            assert (second_root / path).read_bytes() == before[path]
            assert second_products[path]['model'] == old_products[path]['model']
        _, child_before = sealed_tree(service, child)
        engine.dispute = False
        grandchild, third = await finished_job(service, resume_request(child, maxAdditionalPasses=2))
        assert grandchild['state'] == 'succeeded'
        assert [(c['kind'], c['page']) for c in engine.calls if c['jobId'] == grandchild['id']] == [('table-reread', 1)]
        third_root, _, third_units = assert_self_contained(service, grandchild, third)
        assert third_units[confirmation_id]['passesSpent'] == 2
        final_history = json.loads((third_root / decision_path).read_text())
        assert final_history['comparisons'][:len(retained['comparisons'])] == retained['comparisons']
        assert response_paths <= set(final_history['evidencePaths'])
        assert sealed_tree(service, child)[1] == child_before
        assert sealed_tree(service, parent)[1] == before
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['passes', 'audit-passes', 'history', 'tile-evidence', 'selected-candidate'])
async def test_worker_resume_rejects_lost_history_or_changed_completed_evidence(tmp_path, monkeypatch, corruption):
    import preprocessing.service as service_module

    native_table_pdf(tmp_path / 'tables.pdf')
    profile = default_profile()
    profile['models']['vision'] = 'fixture/vision'
    engine = TableEngine(dispute=True)
    service = Service(tmp_path / 'state', tmp_path, profile=profile, engine=engine)
    await service.start()
    try:
        parent, _ = await finished_job(service, {'source': {'kind': 'workspace-file', 'path': 'tables.pdf'}})
        validate = service_module.validate_resume_output

        def corrupt_output(extraction, staging, resume):
            plan_path = staging / 'work-plan.json'
            plan = json.loads(plan_path.read_text())
            if corruption == 'passes':
                unit = next(u for u in plan['units'] if u['kind'] == 'table-confirmation' and u['source']['page'] == 1)
                unit['passesSpent'] = 0
            elif corruption == 'tile-evidence':
                unit = next(u for u in plan['units'] if u['kind'] == 'table-read-tile' and u['source']['page'] == 1)
                unit['evidencePaths'] = []
            else:
                table = next(t for t in extraction['tables'] if t['source']['page'] == (1 if corruption in {'history', 'audit-passes'} else 2))
                decision_path = staging / table['evidencePaths']['decisions']
                decisions = json.loads(decision_path.read_text())
                if corruption == 'history':
                    decisions['comparisons'] = []
                elif corruption == 'audit-passes':
                    decisions['passesSpent'] = 0
                else:
                    candidates = [json.loads(line) for line in (staging / table['evidencePaths']['candidates']).read_text().splitlines()]
                    alternate = next(c for c in candidates if c['method'] == 'visual')['id']
                    table['selectedCandidateId'] = decisions['selectedCandidateId'] = alternate
                    structure_path = staging / table['evidencePaths']['structure']
                    structure = json.loads(structure_path.read_text())
                    structure['selectedCandidateId'] = alternate
                    structure_path.write_text(json.dumps(structure))
                decision_path.write_text(json.dumps(decisions))
            plan_path.write_text(json.dumps(plan))
            validate(extraction, staging, resume)

        monkeypatch.setattr(service_module, 'validate_resume_output', corrupt_output)
        child, _ = await service.submit(str(uuid.uuid4()), resume_request(parent, maxModelCalls=4))
        async with asyncio.timeout(20):
            while service.get(child['id'])['state'] not in {'succeeded', 'partial', 'failed'} or service.active == child['id']:
                await asyncio.sleep(.02)
        child = service.get(child['id'])
        assert child['state'] == 'failed', 'Corrupted resume history or completed evidence was published.'
        assert child['error']['code'] == 'OUTPUT_INVALID'
        assert not child.get('resultId')
        assert [c for c in engine.calls if c['jobId'] == child['id']] == []
    finally:
        await service.close()


def test_canonical_recovery_identity_and_resume_exclusivity():
    source = {'kind': 'workspace-file', 'path': 'report.pdf'}
    first = canonical({'source': source, 'tableRecovery': {'forcePages': [3, 1, 3], 'renderDpi': 300.0}})
    second = canonical({'source': source, 'tableRecovery': {'forcePages': [1, 3], 'renderDpi': 300}})
    assert first == second
    assert first != canonical({'source': source, 'tableRecovery': {'forcePages': [2]}})
    with pytest.raises(ServiceError) as error:
        canonical({'source': source, 'resumeOf': 'job_a', 'rerunOf': 'job_b'})
    assert error.value.code == 'INVALID_REQUEST'


@pytest.mark.parametrize('override', [
    {'bogus': 1}, {'renderDpi': True}, {'renderDpi': float('nan')},
    {'workSeconds': float('inf')}, {'maxModelCalls': 0},
    {'maxAdditionalPasses': False}, {'forcePages': [0]}, {'forcePages': [True]},
    {'forcePages': '1,2'}, {'geometry': 'sometimes'}, {'vision': 'always'},
    {'renderDpi': 10**400},
])
def test_invalid_overrides(override):
    with pytest.raises(ServiceError) as error:
        canonical({'source': {'kind': 'workspace-file', 'path': 'a.pdf'}, 'tableRecovery': override})
    assert error.value.code == 'INVALID_REQUEST'


@pytest.mark.asyncio
async def test_effective_profile_hash_and_non_pdf_worker_rejection(tmp_path):
    (tmp_path / 'a.csv').write_text('001,2\n')
    profile = default_profile()
    profile['options']['tableRecovery'] = {'renderDpi': 450, 'forcePages': [7]}
    service = Service(tmp_path / 'state', tmp_path, profile=profile, engine=FakeEngine())
    await service.start()
    try:
        key = str(uuid.uuid4())
        body = {'source': {'kind': 'workspace-file', 'path': 'a.csv'},
                'tableRecovery': {'forcePages': [], 'vision': 'off'}}
        job, _ = await service.submit(key, body)
        effective = job['profile']
        assert effective['options']['tableRecovery']['renderDpi'] == 450
        assert effective['options']['tableRecovery']['forcePages'] == []
        assert effective['_tableRecoveryOverride'] == body['tableRecovery']
        assert service.profile['options']['tableRecovery']['forcePages'] == [7]
        unhashed = {k: v for k, v in effective.items() if k != 'profileDigest'}
        from preprocessing.store import encode
        assert effective['profileDigest'] == hashlib.sha256(encode(unhashed).encode()).hexdigest()
        assert effective['profileDigest'] != service.profile['profileDigest']
        assert (await service.submit(key, body))[1]
        with pytest.raises(ServiceError) as error:
            await service.submit(key, {**body, 'tableRecovery': {'vision': 'on-demand'}})
        assert error.value.code == 'IDEMPOTENCY_CONFLICT'
        async with asyncio.timeout(10):
            while service.get(job['id'])['state'] not in {'failed', 'succeeded'}:
                await asyncio.sleep(.02)
        assert service.get(job['id'])['error']['code'] == 'UNSUPPORTED_OPTION'
        plain, _ = await service.submit(str(uuid.uuid4()), {'source': body['source']})
        async with asyncio.timeout(10):
            while service.get(plain['id'])['state'] not in {'failed', 'succeeded'}:
                await asyncio.sleep(.02)
        assert service.get(plain['id'])['state'] == 'succeeded'
    finally:
        await service.close()


def test_direct_service_resolves_and_validates_work_budget(tmp_path):
    profile = default_profile()
    profile['limits']['jobSeconds'] = 100
    service = Service(tmp_path, tmp_path, profile=profile)
    assert service.profile['options']['tableRecovery']['workSeconds'] == 75
    assert service.profile['options']['tableRecovery']['vision'] == 'confirm-all'
    profile['options']['tableRecovery'] = {'workSeconds': 100}
    with pytest.raises(ServiceError) as error:
        Service(tmp_path, tmp_path, profile=profile)
    assert error.value.code == 'INVALID_PROFILE'


def test_semantic_fingerprint_excludes_budgets_and_force_but_not_model(tmp_path):
    from preprocessing.table_recovery import effective_profile, semantic_fingerprint
    source = {'sha256': 'a' * 64}
    original = effective_profile(default_profile())
    enlarged = effective_profile(original, {'workSeconds': 3000, 'maxModelCalls': 99,
                                          'maxAdditionalPasses': 5, 'forcePages': [2]})
    enlarged['limits']['pixels'] *= 2
    assert semantic_fingerprint(source, original) == semantic_fingerprint(source, enlarged)
    enlarged['models']['vision'] = 'other/model'
    assert semantic_fingerprint(source, original) != semantic_fingerprint(source, enlarged)


@pytest.mark.asyncio
async def test_coordinator_deadline_declines_and_caps_dispatch(tmp_path):
    class Engine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def start(self, oid, jid, generation, profile, refs, operation):
            self.calls.append(copy.deepcopy({'profile': profile, 'operation': operation}))
            await self.on_operation(oid, jid, {'state': 'completed', 'settlement': {'settled': True}})
            return {'text': 'ok'}

    profile = default_profile()
    profile['limits']['modelSeconds'] = 37
    engine = Engine()
    service = Service(tmp_path / 'state', tmp_path, profile=profile, engine=engine)
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        job.update(state='running')
        service.store.save(job)
        service.work_deadlines[job['id']] = time.monotonic() - 1
        message = {'operation': {'kind': 'table-confirmation', 'completionSeconds': 999999}, 'inputRefs': []}
        reply = await service._model(job['id'], tmp_path, message)
        assert reply['error']['code'] == 'WORK_BUDGET_EXHAUSTED'
        assert engine.calls == []
        assert service.store.operations(job['id']) == []
        service.work_deadlines[job['id']] = time.monotonic() + 2
        assert (await service._model(job['id'], tmp_path, message))['result']['text'] == 'ok'
        assert 0 < engine.calls[0]['operation']['completionSeconds'] <= 2
        assert engine.calls[0]['profile']['limits']['modelSeconds'] == 37
        assert service.get(job['id'])['profile']['limits']['modelSeconds'] == 37
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_snapshot_elapsed_counts_against_worker_work_remaining(tmp_path, monkeypatch):
    from reportlab.pdfgen import canvas
    pdf = canvas.Canvas(str(tmp_path / 'a.pdf'))
    pdf.drawString(10, 700, 'Plain text page')
    pdf.save()
    profile = default_profile()
    profile['limits']['jobSeconds'] = 10
    service = Service(tmp_path / 'state', tmp_path, profile=profile, engine=FakeEngine())
    await service.start()
    observed = []
    snapshot = service._snapshot

    def slow_snapshot(*args):
        time.sleep(.15)
        return snapshot(*args)

    launch = asyncio.create_subprocess_exec

    async def capture_launch(*args, **kwargs):
        process = await launch(*args, **kwargs)
        write = process.stdin.write

        def capture(data):
            observed.append(json.loads(data))
            write(data)

        process.stdin.write = capture
        return process

    monkeypatch.setattr(service, '_snapshot', slow_snapshot)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', capture_launch)
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        async with asyncio.timeout(10):
            while service.get(job['id'])['state'] not in {'failed', 'succeeded'}:
                await asyncio.sleep(.02)
        assert service.get(job['id'])['state'] == 'succeeded'
        assert observed[0]['workDeadline'] <= time.monotonic() + 7.4
    finally:
        await service.close()


async def sealed_parent(service, tmp_path):
    parent, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
    await service.cancel(parent['id'])
    source_dir = service.private / 'sources' / 'source_fixture'
    parent = service.get(parent['id'])
    directory = service.private / 'jobs' / parent['id'] / 'sealed'
    manifest = build_parent_evidence(parent, directory, source_dir)
    service.store.save(parent)
    return parent, directory, manifest


@pytest.mark.asyncio
@pytest.mark.parametrize('fault, expected', [
    ('source', 'INVALID_REQUEST'), ('active', 'INVALID_REQUEST'), ('unsealed', 'INVALID_REQUEST'),
    ('unsettled', 'RESUME_INVALID'),
])
async def test_resume_rejects_unsafe_or_incompatible_parent(tmp_path, fault, expected):
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        parent, _, _ = await sealed_parent(service, tmp_path)
        source = {'kind': 'snapshot', 'snapshotId': parent['source']['snapshotId']}
        if fault == 'source':
            source['snapshotId'] = 'other_source'
        elif fault == 'active':
            parent['state'] = 'running'
            service.store.save(parent)
        elif fault == 'unsealed':
            parent.pop('resultId')
            service.store.save(parent)
        elif fault == 'unsettled':
            service.store.operation({'id': 'unknown', 'jobId': parent['id'], 'settlement': {'settled': False}})
        with pytest.raises(ServiceError) as error:
            service._resume_parent(parent['id'], source)
        assert error.value.code == expected
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault, expected', [('hang', 'WORK_BUDGET_EXHAUSTED'), ('unsettled', 'PROVIDER_OUTCOME_UNKNOWN')])
async def test_engine_completion_window_requires_settlement(tmp_path, fault, expected):
    import httpx
    from test_engine import Peer

    from preprocessing.engine import OpenCodeServerEngine
    peer = Peer(tmp_path, fault)
    descriptor = {'serverUrl': 'http://localhost:1234', 'password': 'private',
                  'version': '2.0.19', 'engineInstanceId': 'engine-1', 'mode': 'standalone',
                  'owner': 'test-owner', 'directory': str(tmp_path), 'contextVerified': True,
                  'recovery': {'managed': False, 'extractionExcluded': True,
                               'fencedGenerations': [], 'deadEngineInstances': []}}
    engine = OpenCodeServerEngine(descriptor, rpc_seconds=.1, cancel_seconds=.1,
                                  client=httpx.AsyncClient(transport=httpx.MockTransport(peer.handle)))
    engine.bind(peer.persist)
    profile = default_profile()
    profile['models']['vision'] = 'opencode/space-bunny-free'
    try:
        started = time.monotonic()
        with pytest.raises(ServiceError) as error:
            await engine.start('op_soft', 'job_soft', 'generation', profile, [],
                               {'kind': 'table-confirmation', 'prompt': 'Read table', 'completionSeconds': .02})
        assert error.value.code == expected
        assert time.monotonic() - started < 1
        assert error.value.settlement['settled'] is (fault == 'hang')
        assert profile['limits']['modelSeconds'] == 180
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_engine_does_not_dispatch_after_preparation_uses_work_window(tmp_path):
    import httpx
    from test_engine import Peer

    from preprocessing.engine import OpenCodeServerEngine
    peer = Peer(tmp_path)

    async def delayed(request):
        if request.url.path == '/api/info':
            await asyncio.sleep(.04)
        return await peer.handle(request)

    descriptor = {'serverUrl': 'http://localhost:1234', 'password': 'private',
                  'version': '2.0.19', 'engineInstanceId': 'engine-1', 'mode': 'standalone',
                  'owner': 'test-owner', 'directory': str(tmp_path), 'contextVerified': True,
                  'recovery': {'managed': False, 'extractionExcluded': True,
                               'fencedGenerations': [], 'deadEngineInstances': []}}
    engine = OpenCodeServerEngine(descriptor, rpc_seconds=.1, cancel_seconds=.1,
                                  client=httpx.AsyncClient(transport=httpx.MockTransport(delayed)))
    engine.bind(peer.persist)
    profile = default_profile()
    profile['models']['vision'] = 'opencode/space-bunny-free'
    try:
        with pytest.raises(ServiceError) as error:
            await engine.start('op_soft', 'job_soft', 'generation', profile, [],
                               {'kind': 'table-confirmation', 'prompt': 'Read table', 'completionSeconds': .02})
        assert error.value.code == 'WORK_BUDGET_EXHAUSTED'
        assert not any(method == 'POST' for method, *_ in peer.calls)
        assert error.value.settlement['settled']
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_http_and_mcp_recovery_controls_and_resume(tmp_path):
    from test_api import client_for, mcp_session, tool_data, tool_error
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    async with client_for(service) as client:
        parent, _, _ = await sealed_parent(service, tmp_path)
        body = {'source': {'kind': 'snapshot', 'snapshotId': parent['source']['snapshotId']},
                'resumeOf': parent['id'], 'tableRecovery': {'forcePages': [2, 2], 'maxModelCalls': 2}}
        response = await client.post('/preprocessing/jobs', json=body, headers={'Idempotency-Key': str(uuid.uuid4())})
        assert response.status_code == 202
        child = response.json()
        await service.cancel(child['id'])
        assert child['resumeOf'] == parent['id']
        assert child['profile']['options']['tableRecovery']['forcePages'] == [2]
        async with mcp_session(client) as session:
            tools = await session.list_tools()
            schema = next(tool.inputSchema for tool in tools.tools if tool.name == 'documents_submit')
            from jsonschema import Draft202012Validator
            assert Draft202012Validator(schema).is_valid({'submissionKey': str(uuid.uuid4()), **body})
            assert not Draft202012Validator(schema).is_valid({'submissionKey': str(uuid.uuid4()), **body,
                                                             'tableRecovery': {'renderDpi': True}})
            assert not Draft202012Validator(schema).is_valid({'submissionKey': str(uuid.uuid4()), **body,
                                                             'tableRecovery': None})
            result = tool_data(await session.call_tool('documents_submit', {'submissionKey': str(uuid.uuid4()), **body}))
            await service.cancel(result['id'])
            assert result['resumeOf'] == parent['id']
            bad = await session.call_tool('documents_submit', {'submissionKey': str(uuid.uuid4()), **body,
                                                               'tableRecovery': {'bogus': 1}})
            assert tool_error(bad)['code'] == 'INVALID_REQUEST'


def test_cli_profile_flags_and_remote_resume_exact_snapshot(tmp_path, monkeypatch):
    import io

    from preprocessing import cli
    profile_path = tmp_path / 'profile.json'
    profile_path.write_text(json.dumps({'models': {'vision': 'fixture/vision'},
                                       'options': {'tableRecovery': {'renderDpi': 600, 'maxAdditionalPasses': 0}}}))
    profile = cli.load_profile(profile_path)
    assert profile['options']['tableRecovery']['geometry'] == 'auto'
    assert profile['options']['tableRecovery']['renderDpi'] == 600
    for prefix in ([], ['dev']):
        arguments = prefix + ['process', 'report.pdf', '--output', str(tmp_path),
                              '--table-geometry', 'force', '--table-vision', 'on-demand',
                              '--table-force-pages', '3,1,3']
        if prefix:
            arguments += ['--opencode-executable', '/tmp/cli', '--engine-config', '/tmp/config']
        args = cli._parser().parse_args(arguments)
        assert cli._table_override(args) == {'geometry': 'force', 'vision': 'on-demand', 'forcePages': [1, 3]}
    calls = []
    parent = {'id': 'job_old', 'source': {'snapshotId': 'source_exact'}, 'profile': {'id': 'selected'}}

    class Owner:
        def open(self, request, timeout):
            calls.append(request)
            return io.BytesIO(json.dumps(parent if request.method == 'GET' else {'id': 'job_new', 'state': 'queued'}).encode())

    monkeypatch.setattr(cli, 'build_opener', lambda *args: Owner())
    args = cli._parser().parse_args(['resume', '--job', 'job_old', '--url', 'http://localhost:1234',
                                    '--table-force-pages', '2'])
    result, code = cli._remote_admin(args, None)
    assert result['id'] == 'job_new' and code == 0
    assert [request.method for request in calls] == ['GET', 'POST']
    assert json.loads(calls[1].data) == {'source': {'kind': 'snapshot', 'snapshotId': 'source_exact'},
                                       'resumeOf': 'job_old', 'profileId': 'selected', 'tableRecovery': {'forcePages': [2]}}
    assert str(uuid.UUID(calls[1].get_header('Idempotency-key'))) == calls[1].get_header('Idempotency-key')


@pytest.mark.asyncio
async def test_coordinator_cutoff_at_durable_dispatch_barrier_settles_session(tmp_path):
    import httpx
    from test_engine import Peer

    from preprocessing.engine import OpenCodeServerEngine
    peer = Peer(tmp_path)

    async def persist(oid, jid, patch):
        await peer.persist(oid, jid, patch)
        if patch.get('state') == 'dispatch_intent':
            raise ServiceError('WORK_BUDGET_EXHAUSTED', 'Coordinator cutoff after durable intent.')

    descriptor = {'serverUrl': 'http://localhost:1234', 'password': 'private',
                  'version': '2.0.19', 'engineInstanceId': 'engine-1', 'mode': 'standalone',
                  'owner': 'test-owner', 'directory': str(tmp_path), 'contextVerified': True,
                  'recovery': {'managed': False, 'extractionExcluded': True,
                               'fencedGenerations': [], 'deadEngineInstances': []}}
    engine = OpenCodeServerEngine(descriptor, rpc_seconds=.1, cancel_seconds=.1,
                                  client=httpx.AsyncClient(transport=httpx.MockTransport(peer.handle)))
    engine.bind(persist)
    profile = default_profile()
    profile['models']['vision'] = 'opencode/space-bunny-free'
    try:
        with pytest.raises(ServiceError) as error:
            await engine.start('op_barrier', 'job_barrier', 'generation', profile, [],
                               {'kind': 'table-confirmation', 'prompt': 'Read table', 'completionSeconds': 1})
        assert error.value.code == 'WORK_BUDGET_EXHAUSTED'
        assert error.value.settlement['settled']
        assert not peer.prompted.is_set()
        assert peer.sessions == {}
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_confirmation_cancellation_keeps_existing_settlement_boundary(tmp_path):
    import httpx
    from test_engine import Peer

    from preprocessing.engine import OpenCodeServerEngine
    peer = Peer(tmp_path, 'hang')
    descriptor = {'serverUrl': 'http://localhost:1234', 'password': 'private',
                  'version': '2.0.19', 'engineInstanceId': 'engine-1', 'mode': 'standalone',
                  'owner': 'test-owner', 'directory': str(tmp_path), 'contextVerified': True,
                  'recovery': {'managed': False, 'extractionExcluded': True,
                               'fencedGenerations': [], 'deadEngineInstances': []}}
    engine = OpenCodeServerEngine(descriptor, rpc_seconds=.1, cancel_seconds=.1,
                                  client=httpx.AsyncClient(transport=httpx.MockTransport(peer.handle)))
    profile = default_profile()
    profile['models']['vision'] = 'opencode/space-bunny-free'
    service = Service(tmp_path / 'state', tmp_path, profile=profile, engine=engine)
    await service.start()

    async def persist(oid, jid, patch):
        await service._record_operation(oid, jid, patch)
        await peer.persist(oid, jid, patch)

    engine.bind(persist)
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'a.pdf'}})
        job['state'] = 'running'
        service.store.save(job)
        service.active = job['id']
        service.work_deadlines[job['id']] = time.monotonic() + 10
        operation = asyncio.create_task(service._model(job['id'], tmp_path, {
            'operation': {'kind': 'table-confirmation', 'prompt': 'Read table'}, 'inputRefs': []}))
        await asyncio.wait_for(peer.prompted.wait(), 2)
        assert (await service.cancel(job['id']))['state'] == 'cancelled'
        assert (await operation)['error']['code'] == 'RUNTIME_INTERRUPTED'
        assert not service.get(job['id']).get('resultId')
        assert not service._needs_settlement(job['id'])
        assert peer.sessions == {}
    finally:
        service.active = None
        await service.close()


@pytest.mark.asyncio
async def test_worker_validates_force_pages_against_physical_pdf_count(tmp_path):
    from reportlab.pdfgen import canvas
    pdf = canvas.Canvas(str(tmp_path / 'one.pdf'))
    pdf.drawString(10, 700, 'One physical page')
    pdf.save()
    service = Service(tmp_path / 'state', tmp_path, engine=FakeEngine())
    await service.start()
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {
            'source': {'kind': 'workspace-file', 'path': 'one.pdf'}, 'tableRecovery': {'forcePages': [2]}})
        async with asyncio.timeout(10):
            while service.get(job['id'])['state'] not in {'failed', 'succeeded', 'partial'}:
                await asyncio.sleep(.02)
        assert service.get(job['id'])['error']['code'] == 'INVALID_REQUEST'
        assert service.store.operations(job['id']) == []
    finally:
        await service.close()
