import asyncio
from uuid import uuid4

from test_model_service import CatalogEngine, profile, setup

from preprocessing.service import Service


async def test_inactive_cancel_reports_execution_readiness_when_admission_is_blocked(tmp_path):
    service, _engine, policy = await setup(tmp_path)
    service.observation_max_age_ms = 5000
    try:
        job, _ = await service.submit(str(uuid4()), {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}})
        service.store.operation({'id': 'inactive-op', 'jobId': job['id'], 'state': 'outcome_unknown',
                                 'sessionId': 'ses_inactive', 'settlement': {'settled': False}})
        policy['allowed_model_ids'] = []
        dependency = (await service.refresh_capabilities())['dependency']
        assert dependency['ready'] is False and dependency['executionReady'] is True
        stopped = await service.cancel(job['id'])
        assert stopped['state'] == 'cancelled' and stopped['cancelRequestedAt'] is not None
        dependency = service.capabilities()['dependency']
        assert dependency['ready'] is False and dependency['executionReady'] is True
        assert dependency['lastRecovery']['settled'] is True
        assert dependency['lastRecovery']['ready'] is True
    finally:
        await service.close()


async def test_held_queue_survives_restart_with_positions_and_pinned_profiles(tmp_path):
    service, engine, policy = await setup(tmp_path)
    body = {'source': {'kind': 'workspace-file', 'path': 'absent.csv'}}
    try:
        first, _ = await service.submit(str(uuid4()), body)
        policy['allowed_model_ids'] = ['b']
        second, _ = await service.submit(str(uuid4()), body)
        assert (await service._next_eligible_queued())['id'] == second['id']
        assert service.get(first['id'])['hold']['reason'] == 'model_not_accepted'
        assert [service.get(j['id'])['queueAhead'] for j in (first, second)] == [0, 1]
    finally:
        await service.close()
    reopened = Service(tmp_path / 'state', tmp_path, profile=profile(),
                       engine=CatalogEngine(engine.catalog), model_policy=service.model_policy)
    await reopened.start()
    reopened.scheduler.cancel()
    await asyncio.gather(reopened.scheduler, return_exceptions=True)
    reopened.scheduler = None
    try:
        assert (await reopened._next_eligible_queued())['id'] == second['id']
        for original in (first, second):
            retained = reopened.get(original['id'])
            assert retained['state'] == 'queued' and retained['startedAt'] is None
            assert retained['interruptedAt'] is None and retained['cancelRequestedAt'] is None
            assert retained['profile'] == original['profile']
        replay, duplicate = await reopened.submit(second['submissionKey'], body)
        assert duplicate and replay['queueAhead'] == 1
        assert (await reopened.cancel(first['id']))['state'] == 'cancelled'
        assert reopened.get(second['id'])['queueAhead'] == 0
    finally:
        await reopened.close()


async def test_shutdown_drains_shared_observation_before_releasing_store(tmp_path):
    service, engine, _policy = await setup(tmp_path)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def observe(selected=None):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert service.store is not None and service.owner is not None
            assert service.heartbeat_task is not None and not service.heartbeat_task.done()
            cancelled.set()
    engine.ready = observe
    reader = asyncio.create_task(service.refresh_capabilities())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await service.close()
        assert cancelled.is_set()
        assert service.store is None and service.owner is None
        assert service._observation_task.done()
    finally:
        await asyncio.gather(reader, return_exceptions=True)
        await service.close()
