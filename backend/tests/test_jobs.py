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
