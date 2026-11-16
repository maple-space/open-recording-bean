import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import httpx
import pytest

from backend.app import create_app
from backend.storage import Storage
from backend.tests.test_app import NoDevices


IDENTITY = {'device_type': 'feishu', 'device_id': 'card-a', 'original_id': 'file-1'}


async def import_audio(client, content=b'RIFF original audio'):
    return await client.post('/api/records/import', data=IDENTITY,
                             files={'file': ('recording.wav', content, 'audio/wav')})


@pytest.mark.asyncio
async def test_import_rejects_same_identity_while_trash_is_in_flight(tmp_path, monkeypatch):
    from backend import storage
    entered, release = threading.Event(), threading.Event()

    def delayed_test_trash(command, **kwargs):
        entered.set()
        assert release.wait(5), 'test did not release fake Trash'
        Path(command[1]).rename(tmp_path / 'retained-trash')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(storage.subprocess, 'run', delayed_test_trash)
    app = create_app(tmp_path / 'data', devices=NoDevices())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        record = (await import_audio(client)).json()
        task = asyncio.create_task(client.post(f"/api/records/{record['id']}/trash"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            response = await import_audio(client, b'RIFF new audio')
            assert response.status_code == 409
        finally:
            release.set()
            await task
            await app.state.jobs.shutdown()
        assert (await client.get('/api/records')).json() == []
        restored = await import_audio(client, b'RIFF new audio')
        assert restored.status_code == 200
        assert restored.json()['id'] == record['id']
        assert (await client.get(restored.json()['audio_url'])).content == b'RIFF new audio'
        assert len((await client.get('/api/records')).json()) == 1
        assert len(list((tmp_path / 'retained-trash').glob('*.wav'))) == 1


@pytest.mark.asyncio
async def test_import_rechecks_reservation_after_upload_await(tmp_path, monkeypatch):
    from backend import storage
    from starlette.datastructures import UploadFile
    uploaded, finish_upload = asyncio.Event(), asyncio.Event()
    trash_entered, finish_trash = threading.Event(), threading.Event()
    original_close = UploadFile.close

    async def delayed_close(file):
        if file.filename == 'delayed.wav' and not uploaded.is_set():
            uploaded.set()
            await finish_upload.wait()
        await original_close(file)

    def delayed_test_trash(command, **kwargs):
        trash_entered.set()
        assert finish_trash.wait(5)
        Path(command[1]).rename(tmp_path / 'retained-trash')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(UploadFile, 'close', delayed_close)
    monkeypatch.setattr(storage.subprocess, 'run', delayed_test_trash)
    app = create_app(tmp_path / 'data', devices=NoDevices())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            record = (await import_audio(client)).json()
            uploading = asyncio.create_task(client.post('/api/records/import', data=IDENTITY,
                    files={'file': ('delayed.wav', b'RIFF replacement audio', 'audio/wav')}))
            trash_task = None
            try:
                await asyncio.wait_for(uploaded.wait(), 2)
                trash_task = asyncio.create_task(client.post(f"/api/records/{record['id']}/trash"))
                assert await asyncio.to_thread(trash_entered.wait, 2)
                finish_upload.set()
                assert (await uploading).status_code == 409
            finally:
                finish_upload.set()
                finish_trash.set()
                await uploading
                if trash_task:
                    await trash_task
    assert app.state.storage.records() == []
    assert len(list((tmp_path / 'retained-trash').glob('*.wav'))) == 1


@pytest.mark.asyncio
async def test_cancelled_http_trash_stays_reserved_until_worker_finishes(tmp_path, monkeypatch):
    from backend import storage
    entered, release = threading.Event(), threading.Event()

    def delayed_test_trash(command, **kwargs):
        entered.set()
        assert release.wait(5)
        Path(command[1]).rename(tmp_path / 'retained-trash')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(storage.subprocess, 'run', delayed_test_trash)
    app = create_app(tmp_path / 'data', devices=NoDevices())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            record = (await import_audio(client)).json()
            task = asyncio.create_task(client.post(f"/api/records/{record['id']}/trash"))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert (await import_audio(client, b'RIFF new audio')).status_code == 409
            finally:
                release.set()
    assert app.state.storage.records() == []
    assert len(list((tmp_path / 'retained-trash').glob('*.wav'))) == 1


class DownloadDevices(NoDevices):
    def __init__(self):
        self.device_id = 'card-a'
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.download_calls = 0

    def snapshot(self, kind):
        return {'connected': True, 'device_id': self.device_id, 'files': []}

    async def download(self, kind, original_id, directory, progress=None):
        self.download_calls += 1
        self.entered.set()
        await self.release.wait()
        path = directory / 'download.wav'
        path.write_bytes(b'RIFF downloaded audio')
        return {'path': path, 'device_type': kind, 'device_id': self.device_id,
                'original_id': original_id, 'title': 'download'}


async def finish_job(app, job):
    task = app.state.jobs.tasks.get(job['id'])
    if task:
        await asyncio.wait_for(asyncio.shield(task), 2)
    return app.state.jobs.get(job['id'])


@pytest.mark.asyncio
async def test_download_binds_record_identity_and_blocks_trash_and_reimport(tmp_path, monkeypatch):
    def unexpected_trash(*args, **kwargs):
        raise AssertionError('download must block Trash before any filesystem action')

    monkeypatch.setattr(Storage, 'trash_record', unexpected_trash)
    devices = DownloadDevices()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        record = (await import_audio(client)).json()
        response = await client.post('/api/jobs', json={'type': 'download', 'device_type': 'feishu', 'original_id': 'file-1'})
        assert response.status_code == 200
        job = response.json()
        try:
            await asyncio.wait_for(devices.entered.wait(), 2)
            assert job.get('record_id') == record['id']
            assert (await client.post(f"/api/records/{record['id']}/trash")).status_code == 409
            assert (await import_audio(client)).status_code == 409
            duplicate = await client.post('/api/jobs', json={'type': 'download', 'device_type': 'feishu', 'original_id': 'file-1'})
            assert duplicate.status_code == 409
        finally:
            devices.release.set()
            await finish_job(app, job)
            await app.state.jobs.shutdown()
        assert app.state.jobs.get(job['id'])['status'] == 'completed'
        assert (await client.get(record['audio_url'])).content == b'RIFF downloaded audio'


@pytest.mark.asyncio
async def test_download_refuses_device_changed_while_queued(tmp_path):
    class SwitchingDevices(DownloadDevices):
        def __init__(self):
            super().__init__()
            self.switch_started, self.switch_release = asyncio.Event(), asyncio.Event()

        async def disconnect(self, kind):
            self.switch_started.set()
            await self.switch_release.wait()
            self.device_id = 'card-b'  # Another recorder holds the link when the queue moves on.
            return self.snapshot(kind)

    devices = SwitchingDevices()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        switching = asyncio.create_task(client.post('/api/devices/feishu/disconnect'))
        await asyncio.wait_for(devices.switch_started.wait(), 2)
        job = (await client.post('/api/jobs', json={'type': 'download', 'device_type': 'feishu', 'original_id': 'file-1'})).json()
        devices.switch_release.set()
        devices.release.set()
        assert (await switching).status_code == 200
        finished = await finish_job(app, job)
        assert finished['status'] == 'failed'
        assert '设备' in finished['error']
        assert devices.download_calls == 0
        assert (await client.get('/api/records')).json() == []
        await app.state.jobs.shutdown()


@pytest.mark.asyncio
async def test_the_recorder_takes_one_job_at_a_time(tmp_path):
    devices = DownloadDevices()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        job = (await client.post('/api/jobs', json={'type': 'download', 'device_type': 'feishu', 'original_id': 'file-1'})).json()
        try:
            await asyncio.wait_for(devices.entered.wait(), 2)
            for body in ({'type': 'refresh', 'device_type': 'feishu'},
                         {'type': 'download', 'device_type': 'feishu', 'original_id': 'file-2'}):
                refused = await client.post('/api/jobs', json=body)
                assert refused.status_code == 409 and '录音豆正在执行后台任务' in refused.json()['detail']
        finally:
            devices.release.set()
            await finish_job(app, job)
            await app.state.jobs.shutdown()
        assert devices.download_calls == 1


@pytest.mark.asyncio
async def test_download_rejects_mismatched_archive_identity(tmp_path):
    class WrongIdentityDevices(DownloadDevices):
        async def download(self, *args, **kwargs):
            result = await super().download(*args, **kwargs)
            result['device_id'] = 'different-card'
            return result

    devices = WrongIdentityDevices()
    devices.release.set()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        job = (await client.post('/api/jobs', json={'type': 'download', 'device_type': 'feishu', 'original_id': 'file-1'})).json()
        finished = await finish_job(app, job)
        assert finished['status'] == 'failed'
        assert (await client.get('/api/records')).json() == []
        await app.state.jobs.shutdown()
