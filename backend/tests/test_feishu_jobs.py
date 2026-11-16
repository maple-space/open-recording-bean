import asyncio
from pathlib import Path

import httpx
import pytest

from backend.app import create_app


class Devices:
    def __init__(self):
        self.feishu = self
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.deleted = []
        self.connected = True

    def snapshot(self, kind='feishu'):
        return dict(connected=self.connected, device_id='bean-a', session_id='session-a',
                    files=[dict(original_id='123', title='test', size_bytes=160)])

    async def disconnect(self, kind):
        self.connected = False

    async def download(self, kind, original_id, work_dir, progress=None):
        self.entered.set()
        await self.release.wait()
        path = Path(work_dir) / 'test.ogg'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'OggS' + bytes(100))
        return dict(path=path, device_id='bean-a', device_type='feishu', original_id=original_id,
                    title='test', duration_ms=20)

    async def delete(self, ident):
        self.deleted.append(ident)


@pytest.mark.asyncio
async def test_backup_and_delete_survive_page_client_closing(tmp_path):
    devices = Devices()
    app = create_app(tmp_path, devices=devices)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as client:
        response = await client.post('/api/devices/feishu/actions', json=dict(action='delete',
            device_id='bean-a', ids=['123'], mode='backup', phrase='确认备份本地后删除'))
        assert response.status_code == 200, response.text
        jid = response.json()['id']
        await asyncio.wait_for(devices.entered.wait(), 2)
    # Page/HTTP client is gone, service and job remain.
    assert devices.connected
    assert not devices.deleted
    devices.release.set()
    await asyncio.gather(*list(app.state.jobs.tasks.values()))
    async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as reopened:
        job = (await reopened.get('/api/jobs/' + jid)).json()
        assert job['status'] == 'completed', job
        assert devices.deleted == ['123']
        assert len((await reopened.get('/api/records')).json()) == 1
        assert (await reopened.get('/api/devices/feishu')).json()['connected']


@pytest.mark.asyncio
async def test_confirmation_and_connection_change_never_delete(tmp_path):
    devices = Devices()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        body = dict(action='delete', device_id='bean-a', ids=['123'], mode='backup', phrase='确认从设备永久删除')
        assert (await client.post('/api/devices/feishu/actions', json=body)).status_code == 400
        body['phrase'] = '确认备份本地后删除'
        response = await client.post('/api/devices/feishu/actions', json=body)
        assert response.status_code == 200, response.text
        await devices.entered.wait()
        duplicate = await client.post('/api/devices/feishu/actions', json=body)
        assert duplicate.status_code == 409
        devices.connected = False
        devices.release.set()
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        assert not devices.deleted
        assert app.state.jobs.get(response.json()['id'])['status'] == 'failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('direct', [True, False])
async def test_direct_never_downloads_and_backup_failure_never_deletes(tmp_path, direct):
    class FailingDownload(Devices):
        async def download(self, *args, **kwargs):
            raise ValueError('备份失败')
    devices = FailingDownload()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        body = dict(action='delete', device_id='bean-a', ids=['123'],
                    mode='direct' if direct else 'backup',
                    phrase='确认从设备永久删除' if direct else '确认备份本地后删除')
        response = await client.post('/api/devices/feishu/actions', json=body)
        assert response.status_code == 200
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        job = app.state.jobs.get(response.json()['id'])
        assert job['status'] == ('completed' if direct else 'failed')
        assert devices.deleted == (['123'] if direct else [])


@pytest.mark.asyncio
async def test_batch_backup_reserves_existing_local_audio_against_trash(tmp_path):
    devices = Devices()
    app = create_app(tmp_path, devices=devices)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as client:
        response = await client.post('/api/devices/feishu/actions', json=dict(action='download', device_id='bean-a', ids=['123']))
        assert response.status_code == 200
        await devices.entered.wait()
        rid = app.state.storage.record_id_for('feishu', 'bean-a', '123')
        # Even before import finishes, the identity is reserved against a second writer.
        conflict = await client.post('/api/records/import', data=dict(device_type='feishu', device_id='bean-a', original_id='123'),
                                     files={'file': ('test.wav', b'RIFF' + bytes(40))})
        assert conflict.status_code == 409
        await client.post('/api/jobs/' + response.json()['id'] + '/cancel')
        assert app.state.jobs.get(response.json()['id'])['status'] == 'cancelled'
        assert not devices.deleted
