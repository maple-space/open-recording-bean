"""Exercise HTTP disconnect with the real connection owner and simulated GATT."""
import asyncio
import struct

import httpx
import pytest

from backend.app import create_app
from backend.devices import DeviceManager
from backend.feishu import FeishuDevice
from backend.tests.test_feishu import FakeBLE, FakeScanner, packet
from backend.tests.test_feishu_incremental import IDENT, SERIAL, Radio


@pytest.fixture(autouse=True)
def forbid_hardware(monkeypatch):
    def forbidden():
        pytest.fail('physical Bluetooth is forbidden in endpoint tests')
    monkeypatch.setattr('backend.devices._load_ble', forbidden)


async def until(predicate, timeout=1):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


@pytest.mark.asyncio
async def test_http_disconnect_during_delete_stops_after_acknowledged_item(tmp_path):
    remaining = [101, 102, 103, 104]
    entered, release = asyncio.Event(), asyncio.Event()
    deleted, commands = [], []

    class Peer(FakeBLE):
        instances = []

        async def write_gatt_char(self, uuid, data, **kwargs):
            kind, ident = data[5:7]
            commands.append((kind, ident))
            if (kind, ident) == (0x1b, 0x0e):
                rows = b''.join(struct.pack('<III', item, item + 10, 480) for item in remaining)
                self.callback(None, packet(kind, ident, struct.pack('<H', len(remaining)) + rows + bytes(8)))
            elif (kind, ident) == (0x1a, 0x10):
                target = int.from_bytes(data[9:13], 'little')
                deleted.append(target)
                entered.set()
                await release.wait()
                remaining.remove(target)
                self.callback(None, packet(kind, ident))
            else:
                await super().write_gatt_char(uuid, data, **kwargs)

    device = FeishuDevice(ble_loader=lambda: (Peer, FakeScanner), reconnect_delays=(60,))
    await device.connect('simulation')
    devices = DeviceManager()
    devices.feishu = device
    app = create_app(tmp_path / 'data', devices=devices)
    request = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            submitted = await client.post('/api/devices/feishu/actions', json=dict(
                action='delete', device_id=SERIAL, ids=['101', '102', '103', '104'], mode='direct',
                phrase='确认从设备永久删除'))
            assert submitted.status_code == 200, submitted.text
            await asyncio.wait_for(entered.wait(), 1)
            request = asyncio.create_task(client.post('/api/devices/feishu/disconnect'))
            await until(lambda: not device.desired)
            assert not request.done()  # The first delete is already in flight.
            sent_before_stop = list(commands)
            release.set()
            response = await asyncio.wait_for(request, 3)
            assert response.status_code == 200, response.text
            assert not response.json()['connected']
            await asyncio.gather(*list(app.state.jobs.tasks.values()))
            assert deleted == [101]
            assert remaining == [102, 103, 104]
            assert commands == sent_before_stop  # No directory refresh after stop, either.
            job = app.state.jobs.get(submitted.json()['id'])
            assert job['status'] == 'failed'
            assert '已删除 1 条' in job['error']
            assert len(Peer.instances) == 1 and device.supervisor is None
    finally:
        release.set()
        if request and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        await app.state.jobs.shutdown()
        await device.disconnect()


@pytest.mark.asyncio
async def test_operation_waiting_for_lock_rejects_published_disconnect():
    device = FeishuDevice(ble_loader=lambda: (FakeBLE, FakeScanner), reconnect_delays=(60,))
    await device.connect('simulation')
    peer = device.client
    commands = list(peer.commands)
    await device.lock.acquire()
    refresh = asyncio.create_task(device.refresh())
    try:
        await asyncio.sleep(0)  # Queue refresh before publishing stop intent.
        device.request_disconnect()
        assert device.snapshot()['connected']
        device.lock.release()
        with pytest.raises(ValueError, match='连接'):
            await refresh
        assert peer.commands == commands
    finally:
        if device.lock.locked():
            device.lock.release()
        await asyncio.gather(refresh, return_exceptions=True)
        await device.disconnect()


@pytest.mark.asyncio
async def test_disconnect_during_refresh_blocks_followup_directory_command():
    entered, release = asyncio.Event(), asyncio.Event()
    hold = False

    class Peer(FakeBLE):
        async def write_gatt_char(self, uuid, data, **kwargs):
            if hold and data[5:7] == b'\x01\x01':
                entered.set()
                await release.wait()
            await super().write_gatt_char(uuid, data, **kwargs)

    device = FeishuDevice(ble_loader=lambda: (Peer, FakeScanner), reconnect_delays=(60,))
    await device.connect('simulation')
    peer = device.client
    before = list(peer.commands)
    hold = True
    refresh = asyncio.create_task(device.refresh())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        device.request_disconnect()
        release.set()
        with pytest.raises(ValueError, match='连接'):
            await refresh
        assert peer.commands == before + [(1, 1)]
    finally:
        release.set()
        await asyncio.gather(refresh, return_exceptions=True)
        await device.disconnect()


@pytest.mark.asyncio
async def test_disconnect_before_scheduled_gatt_write_prevents_command(monkeypatch):
    device = FeishuDevice(ble_loader=lambda: (FakeBLE, FakeScanner), reconnect_delays=(60,))
    await device.connect('simulation')
    peer = device.client
    before = list(peer.commands)
    original_wait = asyncio.wait_for
    stop_next_write = True

    async def publish_stop_before_send(awaitable, timeout):
        nonlocal stop_next_write
        if stop_next_write:
            stop_next_write = False
            device.request_disconnect()
        return await original_wait(awaitable, timeout)

    monkeypatch.setattr('backend.feishu.asyncio.wait_for', publish_stop_before_send)
    try:
        with pytest.raises(ValueError, match='连接'):
            await device.refresh()
        assert peer.commands == before
        await device.connect('simulation')
        await device.refresh()  # The stopped operation must not poison a new connection.
        assert device.snapshot()['connected']
    finally:
        await device.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_job', [False, True])
async def test_http_disconnect_publishes_intent_before_batch_releases_lock(tmp_path, cancel_job):
    radio = Radio(count=3000)
    radio.tick = .02
    device = await radio.device(tmp_path / 'cache')
    devices = DeviceManager()
    devices.feishu = device
    app = create_app(tmp_path / 'data', devices=devices)
    request = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            submitted = await client.post('/api/devices/feishu/actions', json=dict(
                action='download', device_id=SERIAL, ids=[str(IDENT)]))
            assert submitted.status_code == 200, submitted.text
            jid = submitted.json()['id']
            await until(lambda: radio.sent >= 9 * 166)
            generation = device.state['connection_generation']
            request = asyncio.create_task(client.post('/api/devices/feishu/disconnect'))
            await until(lambda: not device.desired)
            assert device.state['connection_generation'] > generation
            if cancel_job:
                cancelled = await client.post(f'/api/jobs/{jid}/cancel')
                assert cancelled.status_code == 200
            response = await asyncio.wait_for(request, 2)
            assert response.status_code == 200, response.text
            assert not response.json()['connected']
            await asyncio.gather(*list(app.state.jobs.tasks.values()))
            job = app.state.jobs.get(jid)
            assert job['status'] in ('failed', 'cancelled')
            assert job['retry_request'] == dict(device_id=SERIAL, ids=[str(IDENT)])
            assert not app.state.storage.records()
            assert len(radio.requests) == len(radio.clients) == 1
            assert device.supervisor is None
            with device.frame_cache.source(SERIAL, IDENT) as source:
                assert source.count() >= 9
    finally:
        if request and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        await app.state.jobs.shutdown()
        await device.disconnect()


@pytest.mark.asyncio
async def test_cancelled_http_disconnect_keeps_stop_intent_while_gatt_lock_is_held(tmp_path):
    radio = Radio(count=12)
    device = await radio.device(tmp_path / 'cache')
    devices = DeviceManager()
    devices.feishu = device
    app = create_app(tmp_path / 'data', devices=devices)
    request = None
    await device.lock.acquire()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            generation = device.state['connection_generation']
            request = asyncio.create_task(client.post('/api/devices/feishu/disconnect'))
            await until(lambda: not device.desired and device.supervisor is None)
            assert device.state['connection_generation'] > generation
            assert not request.done()  # The actual close still respects the GATT lock.
            assert device.client.is_connected
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            assert not device.desired
            assert len(radio.clients) == 1
    finally:
        if request and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        device.lock.release()
        await device.disconnect()


@pytest.mark.asyncio
async def test_cancelling_disconnect_while_batch_lock_is_held_does_not_clear_stop_intent(tmp_path):
    radio = Radio(count=12)
    device = await radio.device(tmp_path / 'cache')
    devices = DeviceManager()
    devices.feishu = device
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_download(*args, **kwargs):
        async with device.operation():
            entered.set()
            await release.wait()
            raise ValueError('下载已停止')

    devices.download = held_download
    app = create_app(tmp_path / 'data', devices=devices)
    request = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            submitted = await client.post('/api/devices/feishu/actions', json=dict(
                action='download', device_id=SERIAL, ids=[str(IDENT)]))
            assert submitted.status_code == 200
            await asyncio.wait_for(entered.wait(), 1)
            generation = device.state['connection_generation']
            request = asyncio.create_task(client.post('/api/devices/feishu/disconnect'))
            await until(lambda: not device.desired)
            assert not request.done()
            assert device.state['connection_generation'] > generation
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            assert not device.desired
            release.set()
            await asyncio.gather(*list(app.state.jobs.tasks.values()))
            assert app.state.jobs.get(submitted.json()['id'])['status'] == 'failed'
            assert not device.desired and len(radio.clients) == 1
    finally:
        if request and not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        release.set()
        await app.state.jobs.shutdown()
        await device.disconnect()
