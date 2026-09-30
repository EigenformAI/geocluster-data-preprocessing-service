import pytest

from preprocessing.store import Store, encode


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'state.sqlite')
    try:
        yield value
    finally:
        value.close()


def add_job(store, number, **changes):
    job = {'id': f'job_{number}', 'submissionKey': f'key_{number}', 'revision': 1,
           'state': 'queued', 'source': None, 'correlation': {'uploadBatchId': None},
           'profile': {'profileDigest': 'profile-digest', 'build': {'sourceDigest': 'build-digest'}},
           'finishedAt': None, 'rerunOf': None, 'documents': None}
    job.update(changes)
    store.insert(job, encode({'source': number}))
    return job


def test_empty_store(store):
    assert store.active_count() == 0
    assert store.next_queued() is None
    assert store.max_sequence() == 0
    assert store.page(0, 1, 10) == []
    assert store.snapshot('missing') is None
    assert list(store.ready_summaries()) == []
    assert list(store.all()) == []


def test_admission_and_scheduling_follow_current_state_and_insertion_order(store):
    for number, state in enumerate(['succeeded', 'partial', 'failed', 'cancelled', 'interrupted']):
        add_job(store, number, state=state)
    older = add_job(store, 50, createdAt='2026-09-30T12:00:00Z')
    newer = add_job(store, 20, createdAt='2026-09-29T12:00:00Z')
    running = add_job(store, 70, state='running')
    add_job(store, 80, state='cancelling')
    assert store.active_count() == 4
    assert store.next_queued() == older
    older['state'] = 'cancelled'
    store.save(older)
    assert store.active_count() == 3
    assert store.next_queued() == newer
    newer['state'] = 'running'
    store.save(newer)
    assert store.next_queued() is None
    running['state'] = 'succeeded'
    store.save(running)
    assert store.active_count() == 2


def test_pages_keep_sequence_ceiling_across_new_jobs_and_updates(store):
    jobs = [add_job(store, number) for number in [40, 10, 30, 20, 50]]
    ceiling = store.max_sequence()
    first = store.page(ceiling, ceiling + 1, 2)
    assert [job['id'] for _, job in first] == ['job_50', 'job_20']
    add_job(store, 60)
    jobs[0]['state'] = 'running'
    store.save(jobs[0])
    assert store.max_sequence() == ceiling + 1
    second = store.page(ceiling, first[-1][0], 2)
    third = store.page(ceiling, second[-1][0], 2)
    assert [job['id'] for _, job in first + second + third] == [
        'job_50', 'job_20', 'job_30', 'job_10', 'job_40']
    assert third[0][1]['state'] == 'running'
    assert store.page(ceiling, third[-1][0], 2) == []
    assert store.page(ceiling, ceiling + 100, 20) == first + second + third
    history = store.all()
    assert next(history)[1]['id'] == 'job_60'
    assert list(history) == first + second + third


def test_page_filters_are_combined_before_limit_and_match_literal_values(store):
    source = {'snapshotId': "source_' OR 1=1 --", 'sha256': 'abc'}
    batch = {'uploadBatchId': "batch_' OR 1=1 --"}
    first = add_job(store, 1, source=source, correlation=batch)
    second = add_job(store, 2, source=source, correlation=batch)
    add_job(store, 3, source=source, correlation=batch, state='failed')
    add_job(store, 4, source=source)
    add_job(store, 5, correlation=batch)
    ceiling = store.max_sequence()
    filters = {'state': 'queued', 'snapshot_id': source['snapshotId'], 'upload_batch_id': batch['uploadBatchId']}
    page = store.page(ceiling, ceiling + 1, 1, **filters)
    assert [job for _, job in page] == [second]
    assert [job for _, job in store.page(ceiling, page[-1][0], 1, **filters)] == [first]
    assert len(store.page(ceiling, ceiling + 1, 10, snapshot_id=source['snapshotId'])) == 4
    assert len(store.page(ceiling, ceiling + 1, 10, upload_batch_id=batch['uploadBatchId'])) == 4
    assert [job['id'] for _, job in store.page(ceiling, ceiling + 1, 10, state='failed')] == ['job_3']
    assert store.page(ceiling, ceiling + 1, 10, state="queued' OR 1=1 --") == []


def test_page_limit_never_allows_unbounded_negative_limit(store):
    add_job(store, 1)
    assert store.page(1, 2, 0) == []
    with pytest.raises(ValueError):
        store.page(1, 2, -1)


def test_snapshot_returns_latest_retained_source_not_requested_source(store):
    source = {'snapshotId': "source_' OR 1=1 --", 'sha256': 'abc', 'filename': 'input.csv'}
    add_job(store, 1, source=source)
    latest = {**source, 'filename': 'renamed.csv'}
    add_job(store, 2, source=latest)
    add_job(store, 3, requestedSource={'kind': 'snapshot', 'snapshotId': source['snapshotId']})
    add_job(store, 4, source={'snapshotId': 'unrelated'})
    assert store.snapshot(source['snapshotId']) == latest
    assert store.snapshot('missing') is None


def test_ready_summaries_order_metadata_without_model_trace(store):
    source = {'snapshotId': 'source_1', 'sha256': 'abc'}
    for number, result, finished in [(1, 'result_z', '2026-09-28T12:00:00Z'),
                                     (2, 'result_b', '2026-09-29T12:00:00Z'),
                                     (3, 'result_a', '2026-09-29T12:00:00Z')]:
        add_job(store, number, state='partial', source=source, resultId=result, finishedAt=finished,
                documents={'state': 'ready', 'path': 'documents/' + result}, rerunOf='job_original',
                coverage={'completed': 2, 'total': 3},
                stages=[{'modelTrace': 'private-model-trace' * 1000}],
                products=[{'content': 'private-product-content' * 1000}])
    for number, documents in [(4, None), (5, {'state': 'pending'}), (6, {'state': 'failed'})]:
        add_job(store, number, state='succeeded', resultId=f'result_{number}', documents=documents)
    summaries = store.ready_summaries()
    newest = next(summaries)
    assert newest['resultId'] == 'result_b'
    assert newest['jobId'] == 'job_2'
    assert newest['source'] == source
    assert newest['profile'] == 'profile-digest'
    assert newest['build'] == {'sourceDigest': 'build-digest'}
    assert newest['finishedAt'] == '2026-09-29T12:00:00Z'
    assert newest['outcome'] == 'partial'
    assert newest['rerunOf'] == 'job_original'
    assert newest['coverage'] == {'completed': 2, 'total': 3}
    assert newest['path'] == 'result_b/index.md'
    assert 'private-model-trace' not in encode(newest)
    assert 'private-product-content' not in encode(newest)
    assert [item['resultId'] for item in summaries] == ['result_a', 'result_z']
