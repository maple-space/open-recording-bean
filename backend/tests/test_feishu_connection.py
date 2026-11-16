"""Connection recovery tests use an in-memory GATT peer only."""
import asyncio
import struct
import time

import pytest
from bleak.exc import BleakError

from backend.feishu import FeishuDevice
from backend.feishu_errors import DeviceIdentityError, TransferInterrupted
from backend.tests.test_feishu import AudioBLE, FakeBLE, FakeScanner, packet


async def until(predicate, timeout=.5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('simulated connection did not reach expected state')
        await asyncio.sleep(.002)


def device_for(client=FakeBLE, scanner=FakeScanner, **kwargs):
    return FeishuDevice(ble_loader=lambda: (client, scanner), **kwargs)


async def test_disconnect_interrupts_pending_command_with_retryable_type():
    device = device_for()
    await device.connect('simulation')
    try:
        pending = asyncio.create_task(device._exchange(0x55, 1))
        await until(lambda: bool(device.pending))
        await device.client.disconnect()
        with pytest.raises(TransferInterrupted):
            await pending
    finally:
        await device.disconnect()


@pytest.mark.parametrize('error', [BleakError('GATT unavailable'), TimeoutError(), OSError('radio unavailable')])
async def test_gatt_write_transport_failures_are_retryable(error):
    class BrokenWrite(FakeBLE):
        async def write_gatt_char(self, *args, **kwargs):
            raise error
    device = device_for(BrokenWrite)
    device.client = BrokenWrite(type('Peer', (), {'address': 'simulation'})(), disconnected_callback=device._lost)
    device.client.is_connected = True
    with pytest.raises(TransferInterrupted):
        await device._write(1, 1)


async def test_disconnected_write_is_retryable_but_programming_errors_are_not():
    device = device_for()
    with pytest.raises(TransferInterrupted):
        await device._write(1, 1)
    class BrokenWrite(FakeBLE):
        async def write_gatt_char(self, *args, **kwargs):
            raise TypeError('bad application argument')
    device.client = BrokenWrite(type('Peer', (), {'address': 'simulation'})(), disconnected_callback=device._lost)
    device.client.is_connected = True
    with pytest.raises(TypeError, match='application'):
        await device._write(1, 1)


async def test_exchange_timeout_is_retryable_and_clears_pending(monkeypatch):
    device = device_for()
    await device.connect('simulation')
    original = asyncio.wait_for
    async def shorter_wait(awaitable, timeout):
        return await original(awaitable, min(timeout, .01))
    monkeypatch.setattr('backend.feishu.asyncio.wait_for', shorter_wait)
    try:
        with pytest.raises(TransferInterrupted):
            await device._exchange(0x55, 1)
        assert device.pending == {}
    finally:
        await device.disconnect()


async def test_missing_peer_and_connect_transport_error_are_retryable():
    class MissingScanner:
        @staticmethod
        async def find_device_by_address(*args, **kwargs):
            return None
    with pytest.raises(TransferInterrupted):
        await device_for(scanner=MissingScanner)._open('simulation')
    class BrokenConnect(FakeBLE):
        async def connect(self):
            raise BleakError('radio unavailable')
    with pytest.raises(TransferInterrupted):
        await device_for(BrokenConnect).connect('simulation')


async def test_changed_identity_is_not_retryable():
    device = device_for()
    try:
        with pytest.raises(DeviceIdentityError):
            await device._open('simulation', 'different-serial')
    finally:
        await device.disconnect()


async def test_open_does_not_reclassify_invalid_device_data():
    class MalformedInfo(FakeBLE):
        async def write_gatt_char(self, uuid, data, **kwargs):
            self.callback(None, packet(1, 1, b'invalid'))
    device = device_for(MalformedInfo)
    try:
        with pytest.raises(ValueError, match='设备信息不完整') as error:
            await device.connect('simulation')
        assert not isinstance(error.value, TransferInterrupted)
    finally:
        await device.disconnect()


async def test_idle_probe_only_queries_info_after_inactivity():
    device = device_for(reconnect_delays=(.005,), idle_probe_interval=.04)
    await device.connect('simulation')
    try:
        client = device.client
        initial = list(client.commands)
        await asyncio.sleep(.015)
        assert client.commands == initial
        await until(lambda: len(client.commands) > len(initial))
        assert client.commands[len(initial):] == [(1, 1)]
        assert device.client is client
        assert device.snapshot()['connection_diagnostics']['counts']['idle_probe'] == 1
    finally:
        await device.disconnect()


@pytest.mark.parametrize('owner', ['lock', 'transfer', 'exclusive'])
async def test_idle_probe_never_queues_behind_active_transfer_or_operation(owner):
    device = device_for(reconnect_delays=(.005,), idle_probe_interval=.02)
    await device.connect('simulation')
    try:
        client = device.client
        initial = list(client.commands)
        if owner == 'lock':
            await device.lock.acquire()
        elif owner == 'transfer':
            device.transfer = asyncio.Queue()
        else:
            device.exclusive = 'live input'
        await asyncio.sleep(.06)
        assert client.commands == initial
        if owner == 'lock':
            device.lock.release()
        else:
            setattr(device, owner, None)
        await until(lambda: len(client.commands) > len(initial))
        assert client.commands[len(initial):] == [(1, 1)]
    finally:
        if device.lock.locked():
            device.lock.release()
        device.transfer = device.exclusive = None
        await device.disconnect()


async def test_failed_idle_probe_reconnects_without_replaying_an_action():
    class StalePeer(FakeBLE):
        instances = []
        async def write_gatt_char(self, uuid, data, **kwargs):
            if self is self.instances[0] and self.commands.count((1, 1)) == 1 and data[5:7] == b'\x01\x01':
                raise BleakError('stale GATT session')
            await super().write_gatt_char(uuid, data, **kwargs)
    device = device_for(StalePeer, reconnect_delays=(.005,), idle_probe_interval=.02)
    await device.connect('simulation')
    try:
        await until(lambda: len(StalePeer.instances) == 2 and device.snapshot()['connected'])
        assert all(set(client.commands) <= {(1, 1), (0x1b, 0x0e)} for client in StalePeer.instances)
        diagnostics = device.snapshot()['connection_diagnostics']
        assert diagnostics['counts']['idle_probe_failed'] == 1
        assert diagnostics['counts']['reconnect_succeeded'] == 1
        assert diagnostics['counts'].get('unexpected_disconnect', 0) == 0
    finally:
        await device.disconnect()


async def test_diagnostics_distinguish_planned_close_and_ignore_old_callbacks():
    device = device_for(reconnect_delays=(60,))
    await device.connect('simulation')
    try:
        old = device.client
        await device._close_client(reason='range_complete')
        diagnostics = device.snapshot()['connection_diagnostics']
        assert diagnostics['counts']['intentional_disconnect'] == 1
        assert diagnostics['last_event']['reason'] == 'range_complete'
        assert device.state['error'] is None
        await device._open('simulation', device.state['device_id'])
        before = device.snapshot()
        old.disconnected_callback(old)
        old.callback(None, packet(0x18, 0x82, b'\x01'))
        assert device.snapshot() == before
        await device.client.disconnect()
        diagnostics = device.snapshot()['connection_diagnostics']
        assert diagnostics['counts']['unexpected_disconnect'] == 1
        assert diagnostics['last_event']['event'] == 'unexpected_disconnect'
        assert diagnostics['last_event']['at']
        for _ in range(12):
            await device._open('simulation')
            await device._close_client(reason='range_complete')
        assert len(device.snapshot()['connection_diagnostics']['recent_events']) <= 20
    finally:
        await device.disconnect()


async def test_connection_generation_changes_only_with_user_intent():
    class Peer(FakeBLE):
        instances = []
    device = device_for(Peer, reconnect_delays=(.005,))
    initial = device.snapshot().get('connection_generation')
    assert isinstance(initial, int)
    await device.connect('simulation')
    try:
        generation = device.snapshot()['connection_generation']
        assert generation > initial
        async with device.lock:
            await device._close_client(reason='range_complete')
            await device._open('simulation', device.state['device_id'])
        assert device.snapshot()['connection_generation'] == generation
        await device.client.disconnect()
        await until(lambda: device.snapshot()['connected'])
        assert device.snapshot()['connection_generation'] == generation
        await device.disconnect()
        assert device.snapshot()['connection_generation'] > generation
        after_disconnect = device.snapshot()['connection_generation']
        await device.connect('simulation')
        assert device.snapshot()['connection_generation'] > after_disconnect
    finally:
        await device.disconnect()


async def test_failed_close_remains_retryable_and_late_callback_is_still_planned():
    class SlowClose(FakeBLE):
        async def disconnect(self):
            raise BleakError('disconnect did not complete')
    device = device_for(SlowClose)
    await device.connect('simulation')
    old = device.client
    try:
        with pytest.raises(TransferInterrupted):
            await device._close_client(reason='range_complete')
        # Some backends report completion after disconnect() has already failed.
        old.is_connected = False
        old.disconnected_callback(old)
        diagnostics = device.snapshot()['connection_diagnostics']
        assert diagnostics['counts'].get('unexpected_disconnect', 0) == 0
        assert diagnostics['counts']['close_failed'] == 1
    finally:
        async def release():
            old.is_connected = False
            old.disconnected_callback(old)
        old.disconnect = release
        await device.disconnect()


async def test_legacy_read_timeout_is_retryable_and_does_not_claim_fixed_wait(tmp_path):
    class NoTail(AudioBLE):
        omit_done = True
    device = device_for(NoTail)
    await device.connect('simulation')
    try:
        with pytest.raises(TransferInterrupted, match='读取超时') as error:
            await device.download('123', tmp_path, idle_timeout=.01)
        assert '60 分钟' not in str(error.value)
    finally:
        await device.disconnect()


@pytest.mark.parametrize('phase', ['discover', 'connect', 'notify', 'info', 'directory'])
@pytest.mark.parametrize('initial', [False, True])
async def test_user_disconnect_invalidates_inflight_open_at_every_stage(phase, initial):
    entered, release = asyncio.Event(), asyncio.Event()
    active = False
    async def gate(stage):
        if active and stage == phase:
            entered.set()
            await release.wait()
    class Scanner(FakeScanner):
        @staticmethod
        async def find_device_by_address(*args, **kwargs):
            await gate('discover')
            return await FakeScanner.find_device_by_address(*args, **kwargs)
    class Peer(FakeBLE):
        instances = []
        notify_calls = 0
        async def connect(self):
            await gate('connect')
            await super().connect()
        async def start_notify(self, *args):
            self.notify_calls += 1
            await gate('notify')
            await super().start_notify(*args)
        async def write_gatt_char(self, uuid, data, **kwargs):
            if data[5:7] == b'\x01\x01':
                await gate('info')
            if active and data[5:7] == b'\x1b\x0e' and int.from_bytes(data[9:-1], 'little') == 0:
                await gate('directory')
                self.commands.append((0x1b, 0x0e))
                self.callback(None, packet(0x1b, 0x0e, struct.pack('<H', 10) + bytes(120) + bytes(8)))
                return
            await super().write_gatt_char(uuid, data, **kwargs)
    device = device_for(Peer, Scanner, reconnect_delays=(60,))
    if not initial:
        await device.connect('simulation')
    before = len(Peer.instances)
    active = True
    async def recover():
        async with device.operation():
            await device._open(device.address, device.state['device_id'])
    opening = asyncio.create_task(device.connect('simulation') if initial else recover())
    disconnecting = None
    try:
        await asyncio.wait_for(entered.wait(), .5)
        generation = device.state['connection_generation']
        disconnecting = asyncio.create_task(device.disconnect())
        await until(lambda: device.state['connection_generation'] > generation)
        release.set()
        with pytest.raises(DeviceIdentityError, match='连接'):
            await opening
        await disconnecting
        assert not device.snapshot()['connected'] and not device.desired
        if phase == 'discover':
            assert len(Peer.instances) == before
        else:
            peer = Peer.instances[-1]
            assert not peer.is_connected
            if phase == 'connect':
                assert peer.notify_calls == 0
            if phase in ('connect', 'notify'):
                assert peer.commands == []
            elif phase == 'info':
                assert peer.commands == [(1, 1)]
            elif phase == 'directory':
                assert peer.commands == [(1, 1), (0x1b, 0x0e)]
    finally:
        release.set()
        await asyncio.gather(opening, *( [disconnecting] if disconnecting else []), return_exceptions=True)
        await device.disconnect()


async def test_new_disconnect_cancels_initial_connect_waiting_for_lock():
    class Peer(FakeBLE):
        instances = []
    device = device_for(Peer)
    await device.lock.acquire()
    connecting = asyncio.create_task(device.connect('simulation'))
    await until(lambda: device.state['connection_generation'] > 0)
    generation = device.state['connection_generation']
    disconnecting = asyncio.create_task(device.disconnect())
    await until(lambda: device.state['connection_generation'] > generation)
    device.lock.release()
    try:
        with pytest.raises(DeviceIdentityError, match='连接'):
            await connecting
        await disconnecting
        assert not Peer.instances
    finally:
        await asyncio.gather(connecting, disconnecting, return_exceptions=True)
        await device.disconnect()
