"""Reservation probes use import/transcribe requests; no record is trashed."""
import asyncio

import httpx
import pytest

from backend.app import create_app
from backend.tests.test_feishu_jobs import BatchDevices, submit_batch


class PausesOnThird(BatchDevices):
    def __init__(self):
        super().__init__()
        self.ids.append('126')
        self.third_entered, self.third_release = asyncio.Event(), asyncio.Event()
        self.delete_entered, self.delete_release = asyncio.Event(), asyncio.Event()

    async def download(self, kind, ident, work_dir, progress=None):
        if ident == '125':
            self.third_entered.set()
            await self.third_release.wait()
        return await super().download(kind, ident, work_dir, progress)

    async def delete(self, ident):
        self.delete_entered.set()
        await self.delete_release.wait()
        await super().delete(ident)


async def import_probe(client, ident):
    return await client.post('/api/records/import',
        data=dict(device_type='feishu', device_id='bean-a', original_id=ident),
        files={'file': ('probe.ogg', b'OggS' + bytes(100), 'audio/ogg')})


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_id', [None, '124'], ids=['completed', 'failed'])
async def test_download_releases_processed_records_but_reserves_current_and_future(tmp_path, monkeypatch, failed_id):
    from backend import workflows

    async def fake_transcribe(store, record, *args, **kwargs):
        return {'record_id': record['id']}

    monkeypatch.setattr(workflows, 'process_record', fake_transcribe)
    devices = PausesOnThird()
    if failed_id:
        devices.failures.add(failed_id)
    app = create_app(tmp_path, devices=devices)
    store = app.state.storage
    original = tmp_path / 'already-archived.ogg'
    original.write_bytes(b'OggS' + bytes(100))
    store.import_audio(original, device_type='feishu', device_id='bean-a', original_id='123')
    record_ids = [store.record_id_for('feishu', 'bean-a', ident) for ident in devices.ids]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client, ids=devices.ids)
        try:
            await asyncio.wait_for(devices.third_entered.wait(), 2)
            # 123 was already archived; 124 has now either completed or failed.
            for ident in (['124', '123'] if failed_id else ['123', '124']):
                imported = await import_probe(client, ident)
                assert imported.status_code == 200, (ident, imported.text)
                transcribe = await client.post('/api/jobs', json=dict(type='transcribe', record_id=imported.json()['id']))
                assert transcribe.status_code == 200, (ident, transcribe.text)
            for ident in ['125', '126']:
                assert (await import_probe(client, ident)).status_code == 409
            job = app.state.jobs.get(submitted['id'])
            assert job['status'] == 'running'
            assert job['record_ids'] == record_ids[2:]
            assert job['retry_request']['ids'] == devices.ids
            assert job['completed_ids'] == (['123'] if failed_id else ['123', '124'])
            assert [item['original_id'] for item in job['failed_items']] == ([failed_id] if failed_id else [])
        finally:
            devices.third_release.set()
            await asyncio.wait_for(asyncio.gather(*list(app.state.jobs.tasks.values())), 3)
            await app.state.jobs.shutdown()
        job = app.state.jobs.get(submitted['id'])
        assert job['status'] == ('failed' if failed_id else 'completed')
        assert job['record_ids'] == []
        assert job['retry_request']['ids'] == devices.ids


@pytest.mark.asyncio
async def test_backup_delete_keeps_whole_batch_reserved_through_backup_and_deletion(tmp_path):
    devices = PausesOnThird()
    app = create_app(tmp_path, devices=devices)
    record_ids = [app.state.storage.record_id_for('feishu', 'bean-a', ident) for ident in devices.ids]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client, action='delete', ids=devices.ids,
            mode='backup', phrase='确认备份本地后删除')
        try:
            await asyncio.wait_for(devices.third_entered.wait(), 2)
            for ident in devices.ids:
                assert (await import_probe(client, ident)).status_code == 409
            assert app.state.jobs.get(submitted['id'])['record_ids'] == record_ids
            devices.third_release.set()
            await asyncio.wait_for(devices.delete_entered.wait(), 2)
            for ident in devices.ids:
                assert (await import_probe(client, ident)).status_code == 409
            assert app.state.jobs.get(submitted['id'])['record_ids'] == record_ids
        finally:
            devices.third_release.set()
            devices.delete_release.set()
            await asyncio.wait_for(asyncio.gather(*list(app.state.jobs.tasks.values())), 3)
            await app.state.jobs.shutdown()
        assert app.state.jobs.get(submitted['id'])['status'] == 'completed'
        assert devices.deleted == devices.ids
