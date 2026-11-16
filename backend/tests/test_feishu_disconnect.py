"""Exercise HTTP disconnect with the real connection owner and simulated GATT."""
import asyncio

import httpx
import pytest

from backend.app import create_app
from backend.devices import DeviceManager
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
