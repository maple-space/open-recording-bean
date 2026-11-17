import asyncio

import pytest

from backend.jobs import JobManager
from backend.storage import Storage


@pytest.mark.asyncio
async def test_jobs_complete_fail_and_cancel(tmp_path):
    manager = JobManager(Storage(tmp_path / 'data'))
    async def good(progress):
        await progress('正在测试')
        return {'ok': True}
    job = manager.submit('scan', good)
    await manager.tasks[job['id']]
    assert manager.get(job['id'])['result'] == {'ok': True}
    async def bad(progress):
        raise RuntimeError('设备断开')
    failed = manager.submit('scan', bad)
    await manager.tasks[failed['id']]
    assert manager.get(failed['id'])['status'] == 'failed'
    async def slow(progress):
        await asyncio.sleep(60)
    pending = manager.submit('scan', slow)
    await asyncio.sleep(0)
    await manager.cancel(pending['id'])
    assert manager.get(pending['id'])['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_job_metadata_contains_no_private_prompt(tmp_path):
    manager = JobManager(Storage(tmp_path / 'data'))
    async def work(progress):
        return {'text': 'done'}
    job = manager.submit('summarize', work, {'record_id': 'id', 'provider': 'codex'})
    assert job['provider'] == 'codex'
    assert 'transcript' not in job
    await manager.tasks[job['id']]


@pytest.mark.asyncio
async def test_batch_progress_survives_cancellation_without_saving_raw_payloads(tmp_path):
    storage = Storage(tmp_path / 'data')
    manager = JobManager(storage)
    entered = asyncio.Event()
    async def work(progress):
        await progress(dict(message='第二条', stage='download', progress=37, total=3,
            current_index=2, current_id='124', file_progress=37, completed_count=1,
            completed_ids=['123'], failed_items=[], raw_audio=b'private', transcript='private'))
        entered.set()
        await asyncio.Event().wait()
    job = manager.submit('download', work, dict(retry_request=dict(device_id='bean-a', ids=['123', '124', '125'])))
    await entered.wait()
    await manager.cancel(job['id'])
    recovered = JobManager(Storage(storage.root)).get(job['id'])
    assert recovered['completed_ids'] == ['123']
    assert recovered['current_index'] == 2
    assert recovered['file_progress'] == 37
    assert 'raw_audio' not in recovered and 'transcript' not in recovered
    assert recovered['retry_request'] == job['retry_request']


@pytest.mark.asyncio
async def test_progress_replaces_reserved_records_without_changing_retry_request(tmp_path):
    manager = JobManager(Storage(tmp_path / 'data'))
    entered = asyncio.Event()
    retry_request = dict(device_id='bean-a', ids=['123', '124'])

    async def work(progress):
        await progress(dict(record_ids=['record-124'], completed_ids=['123'],
            retry_request={'ids': []}, transcript='private'))
        entered.set()
        await asyncio.Event().wait()

    submitted = manager.submit('download', work, dict(
        record_ids=['record-123', 'record-124'], retry_request=retry_request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        persisted = manager.get(submitted['id'])
        assert persisted['record_ids'] == ['record-124']
        assert persisted['retry_request'] == retry_request
        assert 'transcript' not in persisted
    finally:
        await manager.shutdown()
