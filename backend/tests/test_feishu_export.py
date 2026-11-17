"""Offline export lifecycle checks; never access physical Bluetooth or real archives."""
import asyncio
import threading
from pathlib import Path

import httpx
import pytest

from backend.app import create_app
from backend.devices import DeviceManager
from backend.feishu_cache import FrameCache, SourceWorker
from backend.tests.test_feishu_incremental import IDENT, SERIAL, SIGNATURE, Radio, frame
from backend.tests.test_feishu_jobs import Devices


@pytest.fixture(autouse=True)
def forbid_hardware(monkeypatch):
    def forbidden():
        pytest.fail('physical Bluetooth is forbidden in offline tests')
    monkeypatch.setattr('backend.devices._load_ble', forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize(('action', 'endpoint'), [('download', 'batch'), ('download', 'single'), ('delete', 'batch')])
async def test_link_drop_during_export_archives_download_but_never_deletes(tmp_path, monkeypatch, action, endpoint):
    radio = Radio(count=30)
    device = await radio.device(tmp_path / 'cache')
    devices = DeviceManager()
    devices.feishu = device
    app = create_app(tmp_path / 'data', devices=devices)
    original = SourceWorker.call
    deleted = []

    async def drop_during_export(self, name, *args):
        if name == 'export':
            client = device.client
            client.is_connected = False
            client.disconnected_callback(client)
        return await original(self, name, *args)

    async def delete(ident):
        deleted.append(ident)

    monkeypatch.setattr(SourceWorker, 'call', drop_during_export)
    monkeypatch.setattr(device, 'delete', delete)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            url = '/api/jobs' if endpoint == 'single' else '/api/devices/feishu/actions'
            body = dict(type='download', device_type='feishu', original_id=str(IDENT)) if endpoint == 'single' else dict(
                action=action, device_id=SERIAL, ids=[str(IDENT)], mode='backup', phrase='确认备份本地后删除')
            response = await client.post(url, json=body)
            assert response.status_code == 200, response.text
            await asyncio.gather(*list(app.state.jobs.tasks.values()))
            job = app.state.jobs.get(response.json()['id'])
            assert job['status'] == ('completed' if action == 'download' else 'failed'), job
            assert len(app.state.storage.records()) == (1 if action == 'download' else 0)
            assert deleted == []
            assert list((tmp_path / 'data' / 'tasks').iterdir()) == []
            with device.frame_cache.source(SERIAL, IDENT) as source:
                assert source.count() == 30 and source.plan(30) is None
    finally:
        await app.state.jobs.shutdown()
        await device.disconnect()


@pytest.mark.parametrize('failure', ['write', 'cancel'])
def test_failed_export_trashes_only_its_partial_and_preserves_cache(tmp_path, monkeypatch, failure):
    from backend import feishu_cache
    cache = FrameCache(tmp_path / 'cache')
    cancelled = threading.Event()
    path = tmp_path / 'out.ogg'
    old_partial = tmp_path / 'previous.ogg.part'
    old_partial.write_bytes(b'older unfinished recording')
    original = feishu_cache.iter_ogg

    def interrupted(frames, count):
        yield b'OggS'
        if failure == 'write':
            raise OSError('simulated disk failure')
        cancelled.set()
        yield from original(frames, count)

    monkeypatch.setattr(feishu_cache, 'iter_ogg', interrupted)
    with cache.source(SERIAL, IDENT) as source:
        source.put_many([(n, frame(n)) for n in range(30)], SIGNATURE)
        source.set_meta('base', 0)
        source.set_meta('tail', 30)
        with pytest.raises((OSError, ValueError)):
            source.export(path, 30, cancelled)
        assert not path.exists()
        assert not path.with_suffix('.ogg.part').exists()
        assert old_partial.read_bytes() == b'older unfinished recording'
        assert source.count() == 30 and source.plan(30) is None


def test_export_does_not_trash_a_partial_it_did_not_create(tmp_path):
    path = tmp_path / 'out.ogg'
    partial = path.with_suffix('.ogg.part')
    partial.write_bytes(b'preexisting')
    with FrameCache(tmp_path / 'cache').source(SERIAL, IDENT) as source:
        source.put_many([(n, frame(n)) for n in range(3)], SIGNATURE)
        source.set_meta('base', 0)
        source.set_meta('tail', 3)
        with pytest.raises(FileExistsError):
            source.export(path, 3, threading.Event())
    assert partial.read_bytes() == b'preexisting'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['stop', 'identity', 'generation', 'cancel'])
async def test_intent_change_after_export_trashes_output_and_preserves_cache(tmp_path, monkeypatch, change):
    radio = Radio(count=30)
    device = await radio.device(tmp_path / 'cache')
    original = SourceWorker.call

    async def change_after_export(self, name, *args):
        result = await original(self, name, *args)
        if name == 'export':
            assert Path(result).is_file()
            if change == 'stop':
                device.request_disconnect()
            elif change == 'identity':
                device.state['device_id'] = 'different-recorder'
            elif change == 'generation':
                device.state['connection_generation'] += 1
            else:
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
        return result

    monkeypatch.setattr(SourceWorker, 'call', change_after_export)
    task = asyncio.create_task(device.download(str(IDENT), tmp_path / 'out'))
    try:
        with pytest.raises(asyncio.CancelledError if change == 'cancel' else ValueError):
            await task
        assert list((tmp_path / 'out').iterdir()) == []
        with device.frame_cache.source(SERIAL, IDENT) as source:
            assert source.count() == 30 and source.plan(30) is None
    finally:
        await asyncio.gather(task, return_exceptions=True)
        await device.disconnect()


@pytest.mark.asyncio
async def test_failed_task_preserves_nonempty_workdir_and_older_task_residue(tmp_path):
    class Interrupted(Devices):
        async def download(self, kind, original_id, work_dir, progress=None):
            (Path(work_dir) / 'unowned-note.txt').write_text('retain this artifact')
            raise ValueError('simulated failure before export')

    app = create_app(tmp_path, devices=Interrupted())
    older = tmp_path / 'tasks' / 'older'
    older.mkdir(parents=True)
    (older / 'previous.ogg.part').write_bytes(b'old audio')
    empty_older = tmp_path / 'tasks' / 'older-empty'
    empty_older.mkdir()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/devices/feishu/actions', json=dict(action='download', device_id='bean-a', ids=['123']))
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        assert app.state.jobs.get(response.json()['id'])['status'] == 'failed'
        assert (older / 'previous.ogg.part').read_bytes() == b'old audio'
        assert empty_older.is_dir()
        assert len(list((tmp_path / 'tasks').glob('*/unowned-note.txt'))) == 1


@pytest.mark.asyncio
async def test_rejected_download_never_trashes_path_outside_its_workdir(tmp_path):
    foreign = tmp_path / 'preserve.ogg'
    foreign.write_bytes(b'OggS existing audio')

    class WrongResult(Devices):
        async def download(self, kind, original_id, work_dir, progress=None):
            return dict(path=foreign, device_id='another', device_type='feishu', original_id=original_id)

    app = create_app(tmp_path / 'data', devices=WrongResult())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/devices/feishu/actions', json=dict(action='download', device_id='bean-a', ids=['123']))
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        assert app.state.jobs.get(response.json()['id'])['status'] == 'failed'
        assert foreign.read_bytes() == b'OggS existing audio'
        assert list((tmp_path / 'data' / 'tasks').iterdir()) == []


@pytest.mark.asyncio
async def test_cancelled_export_job_leaves_no_task_artifacts_and_can_reuse_cache(tmp_path, monkeypatch):
    from backend.feishu_cache import Source
    from backend.tests.test_live import until

    entered = threading.Event()
    original = Source.export

    def paused(self, path, count, cancelled):
        entered.set()
        assert cancelled.wait(3)
        return original(self, path, count, cancelled)

    monkeypatch.setattr(Source, 'export', paused)
    radio = Radio(count=30)
    device = await radio.device(tmp_path / 'cache')
    devices = DeviceManager()
    devices.feishu = device
    app = create_app(tmp_path / 'data', devices=devices)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            response = await client.post('/api/devices/feishu/actions', json=dict(
                action='download', device_id=SERIAL, ids=[str(IDENT)]))
            assert response.status_code == 200, response.text
            await until(entered.is_set)
            await app.state.jobs.cancel(response.json()['id'])
            assert app.state.jobs.get(response.json()['id'])['status'] == 'cancelled'
            assert app.state.storage.records() == []
            assert list((tmp_path / 'data' / 'tasks').iterdir()) == []
            with device.frame_cache.source(SERIAL, IDENT) as source:
                assert source.count() == 30 and source.plan(30) is None
    finally:
        await app.state.jobs.shutdown()
        await device.disconnect()


def test_task_cleanup_failure_preserves_artifacts_without_masking_original_error(tmp_path, monkeypatch, caplog):
    from backend import task_artifacts

    def denied(path):
        raise ValueError('simulated Trash permission denial')

    monkeypatch.setattr(task_artifacts, 'move_to_trash', denied)
    partial = tmp_path / 'new.ogg.part'
    partial.write_bytes(b'partial audio')
    task_artifacts.discard_file(partial)
    assert partial.read_bytes() == b'partial audio'
    assert '已保留' in caplog.text


def test_task_directory_inspection_failure_does_not_replace_job_result(tmp_path, monkeypatch, caplog):
    from backend import task_artifacts
    directory = tmp_path / 'task'
    directory.mkdir()

    def denied(path):
        raise PermissionError('simulated directory permission denial')

    monkeypatch.setattr(Path, 'iterdir', denied)
    task_artifacts.discard_empty_directory(directory)
    assert directory.is_dir()
    assert '已保留' in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_general_device_job_retires_only_its_empty_directory(tmp_path, failure):
    class Scanner(Devices):
        async def scan(self, kind):
            if failure:
                raise ValueError('simulated scan failure')
            return []

    app = create_app(tmp_path, devices=Scanner())
    older = tmp_path / 'tasks' / 'older-empty'
    older.mkdir(parents=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/jobs', json=dict(type='scan', device_type='feishu'))
        assert response.status_code == 200, response.text
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        assert app.state.jobs.get(response.json()['id'])['status'] == ('failed' if failure else 'completed')
        assert list((tmp_path / 'tasks').iterdir()) == [older]


@pytest.mark.asyncio
async def test_single_download_rejected_identity_retires_only_new_output(tmp_path):
    class WrongResult(Devices):
        async def download(self, kind, original_id, work_dir, progress=None):
            self.release.set()
            data = await super().download(kind, original_id, work_dir, progress)
            data['device_id'] = 'another-device'
            return data

    app = create_app(tmp_path, devices=WrongResult())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/jobs', json=dict(type='download', device_type='feishu', original_id='123'))
        assert response.status_code == 200, response.text
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        job = app.state.jobs.get(response.json()['id'])
        assert job['status'] == 'failed' and '不一致' in job['error']
        assert app.state.storage.records() == []
        assert list((tmp_path / 'tasks').iterdir()) == []
