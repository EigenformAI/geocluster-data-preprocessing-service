import asyncio
import copy
import json
import random
import uuid
from datetime import UTC, datetime

import pytest
from fakes import FakeEngine
from test_model_policy import (
    derived_policy,
    evidence,
    luna_evidence,
    make_policy,
    profile,
)

from preprocessing.errors import ServiceError
from preprocessing.service import Service
from preprocessing.store import Store


class CatalogEngine(FakeEngine):
    def __init__(self, catalog):
        super().__init__()
        self.catalog = catalog
        self.reads = []
        self.dispatched = []

    async def ready(self, selected=None):
        self.reads.append(copy.deepcopy(selected))
        await asyncio.sleep(random.uniform(.001, .008))
        return {'ready': self.available, 'models': copy.deepcopy(self.catalog),
                'observedAt': datetime.now(UTC).isoformat(),
                'compatibility': {'classification': 'unknown', 'cliVersion': None,
                                  'httpVersion': None, 'descriptorVersion': None,
                                  'warnings': ['http_version_missing']}}


async def setup(tmp_path):
    policy, declarations, catalog = evidence()
    engine = CatalogEngine(catalog)
    selection = make_policy(policy, declarations)
    service = Service(tmp_path / 'state', tmp_path, profile=profile(), engine=engine,
                      model_policy=selection, observation_max_age_ms=30)
    await service.start()
    service.scheduler.cancel()
    await asyncio.gather(service.scheduler, return_exceptions=True)
    service.scheduler = None
    return service, engine, policy


async def test_duplicate_first_pinning_and_restart_preserve_choice(tmp_path):
    service, engine, policy = await setup(tmp_path)
    body = {'source': {'kind': 'workspace-file', 'path': 'source.csv'}}
    key = str(uuid.uuid4())
    try:
        jobs = await asyncio.gather(*(service.submit(key, body) for _ in range(12)))
        job = jobs[0][0]
        assert len({j['id'] for j, _ in jobs}) == 1
        assert sum(not duplicate for _, duplicate in jobs) == 1
        assert job['profile']['models'] == {'vision': 'proxy/a'}
        assert job['modelSelection']['selectionReason'] == 'preferred'
        digest = job['profile']['profileDigest']
        policy['allowed_model_ids'] = ['b', 'c']
        engine.available = False
        repeated, duplicate = await service.submit(key, body)
        assert duplicate and repeated['profile']['profileDigest'] == digest
        engine.available = True
        second, _ = await service.submit(str(uuid.uuid4()), body)
        assert second['profile']['models'] == {'vision': 'proxy/b'}
        assert second['profile']['profileDigest'] != digest
        assert service.profile['models'] == {'vision': 'proxy/a'}
        assert len(service.list_jobs()['jobs']) == 2
    finally:
        await service.close()
    reopened = Service(tmp_path / 'state', tmp_path, profile=profile(), engine=engine,
                       model_policy=service.model_policy)
    await reopened.start()
    try:
        assert reopened.get(job['id'])['profile']['profileDigest'] == digest
        assert reopened.get(job['id'])['state'] == 'queued'
        assert (await reopened.submit(key, body))[1] is True
    finally:
        await reopened.close()


async def test_held_oldest_does_not_starve_and_stale_policy_does_not_hold(tmp_path, monkeypatch):
    service, _engine, policy = await setup(tmp_path)
    body = {'source': {'kind': 'workspace-file', 'path': 'source.csv'}}
    try:
        first, _ = await service.submit(str(uuid.uuid4()), body)
        policy['allowed_model_ids'] = ['b']
        second, _ = await service.submit(str(uuid.uuid4()), body)
        assert (await service._next_eligible_queued())['id'] == second['id']
        assert service.get(first['id'])['hold']['reason'] == 'model_not_accepted'
        assert service.get(first['id'])['state'] == 'queued'
        dispatched = asyncio.Event()
        async def run(job_id):
            assert job_id == second['id']
            job = service.get(job_id)
            job['state'] = 'succeeded'
            service.store.save(job)
            dispatched.set()
        monkeypatch.setattr(service, '_run', run)
        service.scheduler = asyncio.create_task(service._schedule())
        await asyncio.wait_for(dispatched.wait(), 1)
        service.scheduler.cancel()
        await asyncio.gather(service.scheduler, return_exceptions=True)
        service.scheduler = None
        assert service.get(first['id'])['state'] == 'queued'
        policy['fetched_at_monotonic_ms'] = 0
        service.model_policy.clock_ms = lambda: 120001
        assert (await service._next_eligible_queued())['id'] == first['id']
        assert service.get(first['id']).get('hold') is None
        caps = await service.refresh_capabilities()
        assert caps['dependency']['ready'] is False
        assert caps['dependency']['executionReady'] is True
        assert caps['dependency']['selectionReason'] == 'policy_stale'
        with pytest.raises(ServiceError):
            await service.submit(str(uuid.uuid4()), body)
        assert (await service.submit(first['submissionKey'], body))[1]
        assert (await service.cancel(first['id']))['state'] == 'cancelled'
    finally:
        await service.close()


async def test_bounded_probe_cancellation_and_old_timestamps_cannot_claim_ready(tmp_path):
    service, engine, _policy = await setup(tmp_path)
    service.observation_seconds = .04
    began = asyncio.Event()
    release = asyncio.Event()
    async def hung(selected=None):
        began.set()
        await release.wait()
        return {'ready': True, 'models': engine.catalog, 'observedAt': '2000-01-01T00:00:00+00:00'}
    engine.ready = hung
    try:
        first = asyncio.create_task(service.refresh_capabilities())
        await began.wait()
        second = asyncio.create_task(service.refresh_capabilities())
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        bounded = await asyncio.wait_for(second, .2)
        assert bounded['dependency']['ready'] is False
        assert bounded['dependency']['executionReady'] is False
        release.set()
        old = (await service.refresh_capabilities())['dependency']
        assert old['observedAt'] == '2000-01-01T00:00:00+00:00'
        assert old['ready'] is False and old['executionReady'] is False
    finally:
        await service.close()


async def test_pinned_requirements_survive_replacement_selection_configuration(tmp_path):
    service, _engine, _policy = await setup(tmp_path)
    try:
        job, _ = await service.submit(str(uuid.uuid4()), {'source': {'kind': 'workspace-file', 'path': 'source.csv'}})
        service.model_policy.min_context_tokens = None
        service.model_policy.min_output_tokens = None
        assert (await service._next_eligible_queued())['id'] == job['id']
        assert service.get(job['id'])['profile']['modelRequirements'] == {
            'derivedInputBound': 81920, 'minContextTokens': 81920, 'minOutputTokens': 1024,
            'operatorMinContextTokens': 32768, 'operatorMinOutputTokens': 512}
        service.model_policy.min_context_tokens = 1000000
        service.model_policy.min_output_tokens = 1000000
        assert (await service._next_eligible_queued())['id'] == job['id']
        assert service.capabilities()['dependency']['ready'] is False
    finally:
        await service.close()


async def test_default_luna_admission_persists_derived_requirements_and_policy_updates_hold_without_reselection(tmp_path):
    policy, declarations, catalog = luna_evidence()
    name = 'openai/gpt-6-luna'
    requested = profile('proxy/' + name)
    selection = derived_policy(policy, declarations)
    engine = CatalogEngine(catalog)
    service = Service(tmp_path / 'state', tmp_path, profile=requested, engine=engine, model_policy=selection)
    await service.start()
    service.scheduler.cancel()
    await asyncio.gather(service.scheduler, return_exceptions=True)
    service.scheduler = None
    body = {'source': {'kind': 'workspace-file', 'path': 'source.csv'}}
    key = str(uuid.uuid4())
    try:
        capabilities = await service.refresh_capabilities()
        assert capabilities['dependency']['ready'] is True
        assert capabilities['dependency']['selectedModel'] == 'proxy/' + name
        first, duplicate = await service.submit(key, body)
        assert duplicate is False
        persisted = service.get(first['id'])
        assert persisted['profile']['modelRequirements'] == {
            'derivedInputBound': 589824, 'minContextTokens': 589824, 'minOutputTokens': 32000,
            'operatorMinContextTokens': None, 'operatorMinOutputTokens': None}
        original = copy.deepcopy(persisted['profile'])
        policy['model_capabilities'][name]['image_input']['tokens_per_image'] = 524288
        assert (await service._next_eligible_queued())['id'] == first['id']
        assert service.capabilities()['dependency']['ready'] is False
        assert service.capabilities()['dependency']['executionReady'] is True
        replay, duplicate = await service.submit(key, body)
        assert duplicate and replay['profile'] == original
        with pytest.raises(ServiceError):
            await service.submit(str(uuid.uuid4()), body)
        policy['model_limits'][name]['max_input_tokens'] = 589823
        assert await service._next_eligible_queued() is None
        held = service.get(first['id'])
        assert held['hold']['reason'] == 'model_token_limits_insufficient'
        assert held['profile'] == original
    finally:
        await service.close()
    # Read a new SQLite connection to prove the resolved bounds and profile digest
    # survived coordinator shutdown, rather than only inspecting returned objects.
    store = Store(service.private / 'state.sqlite')
    try:
        assert store.get(first['id'])['profile'] == original
    finally:
        store.close()


async def test_hold_persistence_failure_blocks_dispatch_then_scheduler_recovers(tmp_path, monkeypatch):
    service, _engine, policy = await setup(tmp_path)
    service.lease_interval = .02
    body = {'source': {'kind': 'workspace-file', 'path': 'source.csv'}}
    failed, dispatched = asyncio.Event(), asyncio.Event()
    try:
        first, _ = await service.submit(str(uuid.uuid4()), body)
        policy['allowed_model_ids'] = ['b']
        second, _ = await service.submit(str(uuid.uuid4()), body)
        save = service.store.save
        def flaky(job):
            if job.get('hold') and not failed.is_set():
                failed.set()
                raise OSError('temporary storage failure')
            return save(job)
        monkeypatch.setattr(service.store, 'save', flaky)
        async def run(job_id):
            assert job_id == second['id']
            assert service.get(first['id'])['hold']['reason'] == 'model_not_accepted'
            job = service.get(job_id)
            job['state'] = 'succeeded'
            service.store.save(job)
            dispatched.set()
        monkeypatch.setattr(service, '_run', run)
        service.scheduler = asyncio.create_task(service._schedule())
        await asyncio.wait_for(failed.wait(), 1)
        assert not dispatched.is_set()
        assert service.capabilities()['dependency']['ready'] is False
        assert not service.scheduler.done()
        await asyncio.wait_for(dispatched.wait(), 1)
        assert not service.scheduler.done()
    finally:
        await service.close()


@pytest.mark.parametrize('failure', ['negative', 'exception', 'timeout'])
async def test_failed_followup_probe_invalidates_earlier_global_success(tmp_path, failure):
    service, engine, _policy = await setup(tmp_path)
    initial = engine.ready
    service.model_policy = None
    service.profile['models']['vision'] = 'proxy/missing-preference'
    service.observation_seconds = .04
    service.observation_max_age_ms = 5000
    async def followup(selected=None):
        if selected is None:
            return await initial()
        if failure == 'exception':
            raise ServiceError('PROVIDER_UNAVAILABLE', 'context failed')
        if failure == 'timeout':
            await asyncio.sleep(1)
        return {'ready': False, 'observedAt': datetime.now(UTC).isoformat(),
            'compatibility': {'classification': 'inconsistent', 'cliVersion': '2.0.19',
                'httpVersion': '2.0.20', 'descriptorVersion': '2.0.19',
                'warnings': ['version_observations_contradict']}}
    engine.ready = followup
    try:
        dependency = (await service.refresh_capabilities())['dependency']
        assert dependency['executionReady'] is False
        assert dependency['ready'] is False
        if failure == 'negative':
            assert dependency['compatibility']['classification'] == 'inconsistent'
            assert dependency['compatibility']['httpVersion'] == '2.0.20'
    finally:
        await service.close()


async def test_missing_observation_timestamp_is_not_fresh_evidence(tmp_path):
    service, engine, _policy = await setup(tmp_path)
    initial = engine.ready
    async def missing(selected=None):
        observed = await initial(selected)
        observed.pop('observedAt')
        return observed
    engine.ready = missing
    try:
        dependency = (await service.refresh_capabilities())['dependency']
        assert dependency['ready'] is False and dependency['executionReady'] is False
        assert dependency['observedAt'] is None
    finally:
        await service.close()


async def test_diagnostic_hold_persistence_failure_blocks_admission_until_retry(tmp_path, monkeypatch):
    service, _engine, policy = await setup(tmp_path)
    body = {'source': {'kind': 'workspace-file', 'path': 'source.csv'}}
    try:
        first, _ = await service.submit(str(uuid.uuid4()), body)
        policy['allowed_model_ids'] = ['b']
        save = service.store.save
        def fail_hold(job):
            if job.get('hold'):
                raise OSError('temporary storage failure')
            return save(job)
        monkeypatch.setattr(service.store, 'save', fail_hold)
        with pytest.raises(OSError):
            await service.refresh_capabilities()
        assert service.capabilities()['dependency']['ready'] is False
        with pytest.raises(ServiceError):
            await service.submit(str(uuid.uuid4()), body)
        assert (await service.submit(first['submissionKey'], body))[1] is True
        monkeypatch.setattr(service.store, 'save', save)
        assert (await service.refresh_capabilities())['dependency']['ready'] is True
        second, _ = await service.submit(str(uuid.uuid4()), body)
        assert second['profile']['models']['vision'] == 'proxy/b'
    finally:
        await service.close()


async def test_capabilities_idle_failure_stale_evidence_and_coalesced_readers(tmp_path):
    service, engine, policy = await setup(tmp_path)
    try:
        before = len(engine.reads)
        observations = await asyncio.gather(*(service.refresh_capabilities() for _ in range(32)))
        assert len(engine.reads) == before + 1
        assert all(c['dependency']['executionReady'] is True for c in observations)
        times = {c['dependency']['observedAt'] for c in observations}
        assert len(times) == 1
        assert 'http_version_missing' in observations[0]['dependency']['compatibility']['warnings']
        await asyncio.sleep(.04)
        cached = service.capabilities()['dependency']
        assert cached['ready'] is False and cached['executionReady'] is False
        assert cached['observedAt'] in times
        engine.available = False
        assert (await service.refresh_capabilities())['dependency']['executionReady'] is False
        engine.available = 'false'
        assert (await service.refresh_capabilities())['dependency']['ready'] is False
        engine.available = True
        policy['allowed_model_ids'] = []
        dependency = (await service.refresh_capabilities())['dependency']
        assert dependency['ready'] is False and dependency['executionReady'] is True
        assert dependency['eligibleCandidateCount'] == 0
        assert service.projection_started
    finally:
        await service.close()


async def test_failed_insertion_sends_no_prompt_and_pinned_failure_never_rotates(tmp_path, monkeypatch):
    service, engine, policy = await setup(tmp_path)
    body = {'source': {'kind': 'workspace-file', 'path': 'source.csv'}}
    try:
        insert = service.store.insert
        def fail(*args):
            raise OSError('disk failed')
        monkeypatch.setattr(service.store, 'insert', fail)
        with pytest.raises(OSError):
            await service.submit(str(uuid.uuid4()), body)
        assert service.list_jobs()['jobs'] == []
        monkeypatch.setattr(service.store, 'insert', insert)
        job, _ = await service.submit(str(uuid.uuid4()), body)
        job['state'] = 'running'
        service.store.save(job)
        staging = service.private / 'jobs' / job['id'] / 'staging'
        staging.mkdir(parents=True)
        async def denied(oid, jid, generation, pinned, refs, operation):
            engine.dispatched.append(pinned['models']['vision'])
            raise ServiceError('PROVIDER_UNAVAILABLE', 'Request denied', 503)
        engine.start = denied
        policy['allowed_model_ids'] = ['b', 'c']
        reply = await service._model(job['id'], staging, {'operation': {'kind': 'ocr', 'prompt': 'Read.'}, 'inputRefs': []})
        assert reply['error']['code'] == 'PROVIDER_UNAVAILABLE'
        assert engine.dispatched == ['proxy/a']
        assert service.get(job['id'])['profile']['models']['vision'] == 'proxy/a'
        assert json.loads((service.private / 'engine-lease.json').read_text())['entries'] == []
    finally:
        await service.close()
