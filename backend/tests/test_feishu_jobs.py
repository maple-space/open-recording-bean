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


class BatchDevices(Devices):
    def __init__(self):
        super().__init__()
        self.desired = True
        self.device_id = 'bean-a'
        self.session_id = 'session-a'
        self.ids = ['123', '124', '125']
        self.downloaded = []
        self.failures = set()
        self.release.set()

    def snapshot(self, kind='feishu'):
        return dict(connected=self.connected, device_id=self.device_id,
                    session_id=self.session_id, desired_connected=self.desired,
                    files=[dict(original_id=ident, title=ident, size_bytes=160) for ident in self.ids])

    async def download(self, kind, original_id, work_dir, progress=None):
        self.downloaded.append(original_id)
        if progress:
            await progress(dict(message='读取中', progress=37, stage='download', raw_audio=b'private'))
        if original_id in self.failures:
            raise ValueError('完整性冲突')
        return await super().download(kind, original_id, work_dir, progress)


async def submit_batch(app, client, **overrides):
    body = dict(action='download', device_id='bean-a', ids=['123', '124', '125'])
    response = await client.post('/api/devices/feishu/actions', json={**body, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['direct', 'backup'])
@pytest.mark.parametrize('stop', ['desired', 'generation'])
async def test_delete_batch_stops_when_connection_intent_changes(tmp_path, mode, stop):
    class IntentChanges(BatchDevices):
        generation = 1

        def snapshot(self, kind='feishu'):
            return dict(super().snapshot(kind), connection_generation=self.generation)

        async def delete(self, ident):
            await super().delete(ident)
            if stop == 'desired':
                self.desired = False
            else:
                # The transport/session can look unchanged when a new intent is published.
                self.generation += 1

    devices = IntentChanges()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client, action='delete', mode=mode,
            phrase='确认从设备永久删除' if mode == 'direct' else '确认备份本地后删除')
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        assert devices.deleted == ['123']
        job = app.state.jobs.get(submitted['id'])
        assert job['status'] == 'failed'
        assert '已删除 1 条' in job['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['download', 'delete', 'start', 'pause'])
async def test_action_submission_rejects_disconnected_intent_on_open_transport(tmp_path, action):
    class SnapshotIntent(Devices):
        def snapshot(self, kind='feishu'):
            return dict(super().snapshot(kind), desired_connected=False)

    devices = SnapshotIntent()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/devices/feishu/actions', json=dict(
            action=action, device_id='bean-a', ids=['123'], mode='direct', phrase='确认从设备永久删除'))
        try:
            assert response.status_code == 400, response.text
            assert not app.state.jobs.tasks
            assert not devices.deleted
        finally:
            await app.state.jobs.shutdown()


@pytest.mark.asyncio
async def test_batch_continues_after_one_bad_file_and_persists_retry_request(tmp_path):
    devices = BatchDevices()
    devices.failures.add('124')
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client)
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        job = app.state.jobs.get(submitted['id'])
        assert devices.downloaded == ['123', '124', '125']
        assert job['status'] == 'failed'
        assert job['retry_request'] == dict(device_id='bean-a', ids=devices.ids)
        assert job['completed_ids'] == ['123', '125']
        assert job['failed_items'] == [dict(original_id='124', error='完整性冲突')]
        assert job['result']['count'] == 2
        assert job['result']['failed_items'] == job['failed_items']
        assert len(app.state.storage.records()) == 2
        assert all(not record['transcript'] for record in app.state.storage.records())
        assert not devices.deleted


@pytest.mark.asyncio
async def test_resume_after_restart_skips_verified_archives_and_retries_failed_files(tmp_path):
    devices = BatchDevices()
    devices.failures.add('124')
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client)
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
    devices.failures.clear()
    devices.downloaded.clear()
    restarted = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://127.0.0.1') as client:
        response = await client.post(f'/api/jobs/{submitted["id"]}/resume')
        assert response.status_code == 200, response.text
        resumed = response.json()
        assert resumed['resumed_from'] == submitted['id']
        await asyncio.gather(*list(restarted.state.jobs.tasks.values()))
        job = restarted.state.jobs.get(resumed['id'])
        assert job['status'] == 'completed', job
        assert devices.downloaded == ['124']
        assert job['completed_count'] == job['total'] == 3
        assert job['result']['skipped_ids'] == ['123', '125']


@pytest.mark.asyncio
async def test_same_device_new_session_between_files_keeps_downloading(tmp_path):
    class Reconnecting(BatchDevices):
        async def download(self, *args, **kwargs):
            result = await super().download(*args, **kwargs)
            self.session_id = 'session-reconnected'
            return result
    devices = Reconnecting()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client)
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        assert app.state.jobs.get(submitted['id'])['status'] == 'completed'
        assert devices.downloaded == devices.ids


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['restore', 'cancel', 'disconnect', 'switch'])
async def test_batch_waits_between_files_but_stops_on_user_intent(tmp_path, outcome):
    class Disconnecting(BatchDevices):
        async def download(self, *args, **kwargs):
            result = await super().download(*args, **kwargs)
            if len(self.downloaded) == 1:
                self.connected = False
            return result
    devices = Disconnecting()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client)
        jid = submitted['id']
        async def waiting():
            while app.state.jobs.get(jid).get('stage') != 'reconnecting':
                if app.state.jobs.get(jid)['status'] in ('failed', 'completed'):
                    pytest.fail(f'batch terminated before waiting: {app.state.jobs.get(jid)}')
                await asyncio.sleep(.01)
        await asyncio.wait_for(waiting(), 2)
        assert devices.downloaded == ['123']
        assert app.state.jobs.get(jid)['completed_ids'] == ['123']
        if outcome == 'cancel':
            await app.state.jobs.cancel(jid)
        elif outcome == 'disconnect':
            devices.desired = False
        else:
            devices.device_id = 'bean-b' if outcome == 'switch' else 'bean-a'
            devices.session_id = 'session-new'
            devices.connected = True
        await asyncio.wait_for(asyncio.gather(*list(app.state.jobs.tasks.values())), 3)
        job = app.state.jobs.get(jid)
        assert job['status'] == ('completed' if outcome == 'restore' else 'cancelled' if outcome == 'cancel' else 'failed')
        assert devices.downloaded == (devices.ids if outcome == 'restore' else ['123'])
        assert not devices.deleted


@pytest.mark.asyncio
async def test_corrupt_existing_archive_is_not_skipped_or_overwritten(tmp_path):
    devices = BatchDevices()
    app = create_app(tmp_path, devices=devices)
    source = tmp_path / 'original.ogg'
    source.write_bytes(b'OggS original')
    saved = app.state.storage.import_audio(source, device_type='feishu', device_id='bean-a', original_id='123')
    audio = app.state.storage.audio_path(saved['id'])
    audio.write_bytes(b'OggS')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client)
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        job = app.state.jobs.get(submitted['id'])
        assert job['status'] == 'failed'
        assert devices.downloaded == ['124', '125']
        assert job['failed_items'][0]['original_id'] == '123'
        assert audio.read_bytes() == b'OggS'


@pytest.mark.asyncio
@pytest.mark.parametrize(('kind', 'status'), [('device_delete', 'failed'), ('record_start', 'failed'),
    ('record_pause', 'cancelled'), ('download', 'completed'), ('download', 'running')])
async def test_resume_never_replays_other_actions_or_live_or_completed_jobs(tmp_path, kind, status):
    devices = BatchDevices()
    app = create_app(tmp_path, devices=devices)
    app.state.storage.save_job(dict(id='old', type=kind, status=status, created_at='', device_type='feishu',
        retry_request=dict(device_id='bean-a', ids=['123'])))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/jobs/old/resume')
        assert response.status_code == 409, response.text
        assert not devices.downloaded
        assert not devices.deleted


@pytest.mark.asyncio
async def test_backup_delete_still_rejects_same_device_new_session(tmp_path):
    class NewSession(BatchDevices):
        async def download(self, *args, **kwargs):
            result = await super().download(*args, **kwargs)
            self.session_id = 'changed'
            return result
    devices = NewSession()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        job = await submit_batch(app, client, action='delete', mode='backup', phrase='确认备份本地后删除')
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        assert app.state.jobs.get(job['id'])['status'] == 'failed'
        assert not devices.deleted


@pytest.mark.asyncio
async def test_cancelled_sync_can_resume_and_duplicate_resume_cannot_queue(tmp_path):
    class PausesOnSecond(BatchDevices):
        def __init__(self):
            super().__init__()
            self.second_entered = asyncio.Event()
            self.second_release = asyncio.Event()

        async def download(self, kind, ident, work_dir, progress=None):
            if ident == '124':
                self.second_entered.set()
                await self.second_release.wait()
            return await super().download(kind, ident, work_dir, progress)
    devices = PausesOnSecond()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        original = await submit_batch(app, client)
        await asyncio.wait_for(devices.second_entered.wait(), 2)
        await app.state.jobs.cancel(original['id'])
        assert app.state.jobs.get(original['id'])['completed_ids'] == ['123']
        response = await client.post(f'/api/jobs/{original["id"]}/resume')
        assert response.status_code == 200, response.text
        duplicate = await client.post(f'/api/jobs/{original["id"]}/resume')
        assert duplicate.status_code == 409
        devices.second_release.set()
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        assert app.state.jobs.get(response.json()['id'])['status'] == 'completed'
        assert devices.downloaded == devices.ids


@pytest.mark.asyncio
async def test_service_recovery_keeps_request_and_progress_for_resume(tmp_path):
    devices = BatchDevices()
    app = create_app(tmp_path, devices=devices)
    app.state.storage.save_job(dict(id='interrupted', type='download', status='running',
        device_type='feishu', created_at='', completed_ids=['123'], completed_count=1,
        retry_request=dict(device_id='bean-a', ids=devices.ids)))
    restarted = create_app(tmp_path, devices=devices)
    recovered = restarted.state.jobs.get('interrupted')
    assert recovered['status'] == 'failed'
    assert recovered['completed_ids'] == ['123']
    assert recovered['retry_request']['ids'] == devices.ids
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://127.0.0.1') as client:
        resumed = await client.post('/api/jobs/interrupted/resume')
        assert resumed.status_code == 200, resumed.text
        await asyncio.gather(*list(restarted.state.jobs.tasks.values()))
        assert restarted.state.jobs.get(resumed.json()['id'])['status'] == 'completed'


@pytest.mark.asyncio
async def test_download_progress_is_visible_during_current_file(tmp_path):
    devices = BatchDevices()
    devices.release.clear()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client)
        await asyncio.wait_for(devices.entered.wait(), 2)
        job = app.state.jobs.get(submitted['id'])
        assert (job['current_index'], job['total'], job['completed_count'], job['file_progress']) == (1, 3, 0, 37)
        assert 'raw_audio' not in job
        await app.state.jobs.cancel(job['id'])


@pytest.mark.asyncio
async def test_old_batch_cannot_continue_after_user_reconnects_same_device(tmp_path):
    class UserReconnects(BatchDevices):
        generation = 1

        def snapshot(self, kind='feishu'):
            return dict(super().snapshot(kind), connection_generation=self.generation)

        async def download(self, *args, **kwargs):
            result = await super().download(*args, **kwargs)
            # The disconnect and user connect both occurred during an await;
            # desired and the device ID look the same again at the next check.
            self.generation += 2
            self.session_id = 'user-reconnected'
            return result
    devices = UserReconnects()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client)
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        assert app.state.jobs.get(submitted['id'])['status'] == 'failed'
        assert devices.downloaded == ['123']
        assert app.state.storage.records() == []


@pytest.mark.asyncio
async def test_download_with_wrong_device_type_is_not_imported(tmp_path):
    class WrongType(BatchDevices):
        async def download(self, kind, ident, work_dir, progress=None):
            result = await super().download(kind, ident, work_dir, progress)
            if ident == '123':
                result['device_type'] = 'local'
            return result
    devices = WrongType()
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        submitted = await submit_batch(app, client)
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        records = app.state.storage.records()
        assert all(record['device_type'] == 'feishu' for record in records)
        assert {record['original_id'] for record in records} == {'124', '125'}
        assert app.state.jobs.get(submitted['id'])['status'] == 'failed'


@pytest.mark.asyncio
async def test_resume_skips_missing_catalog_ids_preserves_request_and_reserves_only_present(tmp_path):
    devices = BatchDevices()
    devices.ids = ['124']
    devices.release.clear()
    app = create_app(tmp_path, devices=devices)
    request = dict(device_id='bean-a', ids=['123', '124', '125'])
    app.state.storage.save_job(dict(id='old', type='download', status='failed', created_at='',
        device_type='feishu', retry_request=request))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/jobs/old/resume')
        assert response.status_code == 200, response.text
        resumed = response.json()
        try:
            await asyncio.wait_for(devices.entered.wait(), 2)
            assert resumed['retry_request'] == request
            assert resumed['record_ids'] == [app.state.storage.record_id_for('feishu', 'bean-a', '124')]
        finally:
            devices.release.set()
            await asyncio.gather(*list(app.state.jobs.tasks.values()))
        job = app.state.jobs.get(resumed['id'])
        assert job['status'] == 'completed', job
        assert job['retry_request'] == request
        assert job['result']['missing_ids'] == ['123', '125']
        assert job['result']['missing_count'] == 2
        assert job['result']['completed_ids'] == ['124']
        assert job['result']['count'] == job['result']['total'] == 1
        assert devices.downloaded == ['124']
        assert not devices.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize('wrong_device', [False, True])
async def test_resume_all_missing_reports_explicitly_after_identity_validation(tmp_path, wrong_device):
    devices = BatchDevices()
    devices.ids = []
    if wrong_device:
        devices.device_id = 'other'
    app = create_app(tmp_path, devices=devices)
    app.state.storage.save_job(dict(id='old', type='download', status='cancelled', created_at='',
        device_type='feishu', retry_request=dict(device_id='bean-a', ids=['123', '124'])))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/jobs/old/resume')
        assert response.status_code == 400
        assert ('身份已变化' if wrong_device else '原任务的 2 条录音均已不在设备目录中') in response.json()['detail']
        assert not app.state.jobs.tasks
        assert not devices.downloaded


@pytest.mark.asyncio
@pytest.mark.parametrize('action,mode', [('download', 'backup'), ('delete', 'backup'), ('delete', 'direct')])
async def test_new_actions_still_reject_missing_catalog_ids(tmp_path, action, mode):
    devices = BatchDevices()
    devices.ids = ['123']
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/devices/feishu/actions', json=dict(action=action,
            device_id='bean-a', ids=['123', '124'], mode=mode,
            phrase='确认备份本地后删除' if mode == 'backup' else '确认从设备永久删除'))
        assert response.status_code == 400
        assert '设备目录中没有所选录音' in response.json()['detail']
        assert not devices.downloaded and not devices.deleted
