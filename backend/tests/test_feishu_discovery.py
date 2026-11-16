"""CoreBluetooth lookup is tested through injected objects, never the radio."""
import asyncio
import importlib
from types import SimpleNamespace

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from backend.feishu import FeishuDevice
from backend.feishu_errors import DeviceIdentityError, TransferInterrupted
from backend.tests.test_feishu import FakeBLE, FakeScanner
from backend.tests.test_feishu import packet


ADDRESS = '1b8df212-1437-40ec-b9c2-4c915d89fa81'
OTHER = '2082ee65-5f36-4c2a-b93f-e93e77b24b2f'
SERIAL = 'abc1234567890123'


def helper():
    return importlib.import_module('backend.feishu_discovery')


def fake_backend(monkeypatch, peripheral_ids=(ADDRESS,)):
    module = helper()
    calls, instances = [], []
    class UUID:
        @classmethod
        def alloc(cls):
            return cls()
        def initWithUUIDString_(self, value):
            self.value = value
            return self
    peripherals = [SimpleNamespace(identifier=lambda ident=ident: SimpleNamespace(UUIDString=lambda: ident.upper()),
                                   name=lambda: 'soundcore Work') for ident in peripheral_ids]
    class Central:
        def retrievePeripheralsWithIdentifiers_(self, identifiers):
            calls.append(('retrieve', [ident.value for ident in identifiers]))
            return peripherals
    class Manager:
        def __init__(self):
            self.central_manager = Central()
            instances.append(self)
        async def wait_until_ready(self):
            calls.append(('ready',))
    monkeypatch.setattr(module, 'platform', 'darwin')
    monkeypatch.setattr(module, '_load_macos_backend', lambda: (Manager, UUID, BLEDevice))
    return module, calls, instances, peripherals


async def test_known_peripheral_uses_public_lookup_and_exact_uuid(monkeypatch):
    module, calls, managers, peripherals = fake_backend(monkeypatch, (OTHER, ADDRESS))
    device = await module.retrieve_known_macos_device(ADDRESS)
    assert calls == [('ready',), ('retrieve', [ADDRESS.upper()])]
    assert isinstance(device, BLEDevice)
    assert device.address.lower() == ADDRESS and device.name == 'soundcore Work'
    assert device.details == (peripherals[1], managers[0])


@pytest.mark.parametrize('peripherals', [(), (OTHER,)])
async def test_known_lookup_never_invents_a_missing_peripheral(monkeypatch, peripherals):
    module, calls, managers, _ = fake_backend(monkeypatch, peripherals)
    assert await module.retrieve_known_macos_device(ADDRESS) is None
    assert calls[0] == ('ready',)


@pytest.mark.parametrize('system,address', [('linux', ADDRESS), ('win32', ADDRESS), ('darwin', 'AA:BB:CC:DD:EE:FF'),
                                           ('darwin', ''), ('darwin', 'not-a-uuid')])
async def test_unsupported_platform_or_invalid_uuid_does_not_load_apple_backend(monkeypatch, system, address):
    module = helper()
    monkeypatch.setattr(module, 'platform', system)
    def forbidden():
        pytest.fail('Apple APIs must not load for this request')
    monkeypatch.setattr(module, '_load_macos_backend', forbidden)
    assert await module.retrieve_known_macos_device(address) is None


async def test_unavailable_private_bleak_surface_keeps_normal_discovery(monkeypatch):
    module = helper()
    monkeypatch.setattr(module, 'platform', 'darwin')
    monkeypatch.setattr(module, '_load_macos_backend', lambda: None)
    assert await module.retrieve_known_macos_device(ADDRESS) is None


def real_path_with_fake_backend(monkeypatch):
    class Peer(FakeBLE):
        instances = []
    class MissingScanner(FakeScanner):
        calls = 0
        @staticmethod
        async def find_device_by_address(*args, **kwargs):
            MissingScanner.calls += 1
            return None
    monkeypatch.setattr('backend.feishu.platform', 'darwin')
    monkeypatch.setattr('backend.devices._load_ble', lambda: (Peer, MissingScanner))
    return FeishuDevice(reconnect_delays=(60,)), Peer, MissingScanner


async def test_real_loader_falls_back_then_reuses_verified_peripheral_without_scan(monkeypatch):
    module = helper()
    device, Peer, Scanner = real_path_with_fake_backend(monkeypatch)
    lookups = []
    known = BLEDevice(ADDRESS, 'soundcore Work', ('mock-peripheral', 'mock-manager'))
    async def retrieve(address):
        lookups.append(address)
        return known
    monkeypatch.setattr(module, 'retrieve_known_macos_device', retrieve)
    try:
        await device.connect(ADDRESS)
        assert lookups == [ADDRESS] and Scanner.calls == 1
        async with device.lock:
            await device._close_client(reason='range_complete')
            await device._open(ADDRESS, SERIAL)
        assert len(Peer.instances) == 2
        assert lookups == [ADDRESS] and Scanner.calls == 1
    finally:
        await device.disconnect()


async def test_injected_ble_loader_never_uses_system_peripheral_lookup(monkeypatch):
    module = helper()
    monkeypatch.setattr('backend.feishu.platform', 'darwin')
    async def forbidden(address):
        pytest.fail('injected BLE transport must not call system lookup')
    monkeypatch.setattr(module, 'retrieve_known_macos_device', forbidden)
    class MissingScanner:
        @staticmethod
        async def find_device_by_address(*args, **kwargs):
            return None
    device = FeishuDevice(ble_loader=lambda: (FakeBLE, MissingScanner))
    with pytest.raises(TransferInterrupted):
        await device.connect(ADDRESS)


async def test_late_known_peripheral_does_not_revive_cancelled_connection(monkeypatch):
    module = helper()
    device, Peer, _ = real_path_with_fake_backend(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def retrieve(address):
        entered.set()
        await release.wait()
        return BLEDevice(ADDRESS, 'soundcore Work', ('mock-peripheral', 'mock-manager'))
    monkeypatch.setattr(module, 'retrieve_known_macos_device', retrieve)
    connecting = asyncio.create_task(device.connect(ADDRESS))
    disconnecting = None
    try:
        await asyncio.wait_for(entered.wait(), .5)
        disconnecting = asyncio.create_task(device.disconnect())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(DeviceIdentityError):
            await connecting
        await disconnecting
        assert Peer.instances == []
    finally:
        release.set()
        await asyncio.gather(connecting, *([disconnecting] if disconnecting else []), return_exceptions=True)
        await device.disconnect()


async def test_known_peripheral_still_requires_matching_serial(monkeypatch):
    module = helper()
    device, _, _ = real_path_with_fake_backend(monkeypatch)
    async def retrieve(address):
        return BLEDevice(address, 'soundcore Work', ('mock-peripheral', 'mock-manager'))
    monkeypatch.setattr(module, 'retrieve_known_macos_device', retrieve)
    device.desired, device.address = True, ADDRESS
    device.state['device_id'] = 'different-serial'
    try:
        with pytest.raises(DeviceIdentityError, match='序列号'):
            await device._open(ADDRESS, 'different-serial')
        assert not device.snapshot()['connected']
    finally:
        await device.disconnect()


def rotation_backend(candidate_serials, old_failure='missing', pause=None):
    class Scanner(FakeScanner):
        stale = False
        discovered = []
        @staticmethod
        async def find_device_by_address(address, **kwargs):
            if Scanner.stale and old_failure == 'missing':
                return None
            return SimpleNamespace(address=address, name='soundcore Work')
        @staticmethod
        async def discover(**kwargs):
            Scanner.discovered.append(kwargs)
            if pause:
                await pause('discover')
            advertisement = SimpleNamespace(local_name='soundcore Work', service_uuids=[])
            return {address: (SimpleNamespace(address=address, name='soundcore Work'), advertisement)
                    for address in candidate_serials}
    class Peer(FakeBLE):
        instances = []
        async def connect(self):
            if Scanner.stale and self.address == ADDRESS and old_failure == 'connect':
                raise BleakError('old UUID unreachable')
            await super().connect()
        async def write_gatt_char(self, uuid, data, **kwargs):
            if self.address in candidate_serials and data[5:7] == b'\x01\x01':
                if pause:
                    await pause('info')
                self.commands.append((1, 1))
                info = bytearray(51)
                info[8:24] = candidate_serials[self.address].encode()
                self.callback(None, packet(1, 1, info))
            else:
                await super().write_gatt_char(uuid, data, **kwargs)
    return Peer, Scanner


@pytest.mark.parametrize('failure', ['missing', 'connect'])
async def test_recovery_accepts_new_address_only_after_original_serial_matches(failure):
    new = '3282ee65-5f36-4c2a-b93f-e93e77b24b2f'
    Peer, Scanner = rotation_backend({OTHER: 'other12345678901', new: SERIAL}, failure)
    device = FeishuDevice(ble_loader=lambda: (Peer, Scanner), reconnect_delays=(60,))
    await device.connect(ADDRESS)
    generation = device.state['connection_generation']
    Scanner.stale = True
    try:
        async with device.lock:
            await device._open(ADDRESS, SERIAL)
        assert device.address == new and device.state['device_id'] == SERIAL
        assert device.state['connection_generation'] == generation
        rejected = next(client for client in Peer.instances if client.address == OTHER)
        assert rejected.commands == [(1, 1)] and not rejected.is_connected
        assert device.client.commands == [(1, 1), (0x1b, 0x0e)]
        assert Scanner.discovered == [dict(timeout=15, return_adv=True)]
    finally:
        await device.disconnect()


async def test_first_connect_never_substitutes_an_unselected_advertisement():
    Peer, Scanner = rotation_backend({OTHER: SERIAL})
    Scanner.stale = True
    device = FeishuDevice(ble_loader=lambda: (Peer, Scanner))
    with pytest.raises(TransferInterrupted):
        await device.connect(ADDRESS)
    assert Scanner.discovered == [] and Peer.instances == []


async def test_recovery_probes_at_most_three_other_recorders_without_listing():
    candidates = {f'{i}282ee65-5f36-4c2a-b93f-e93e77b24b2f': 'other12345678901' for i in range(4)}
    Peer, Scanner = rotation_backend(candidates)
    device = FeishuDevice(ble_loader=lambda: (Peer, Scanner), reconnect_delays=(60,))
    await device.connect(ADDRESS)
    Scanner.stale = True
    try:
        async with device.lock:
            with pytest.raises(TransferInterrupted):
                await device._open(ADDRESS, SERIAL)
        assert len(Peer.instances) == 4  # Original connection plus three bounded identity probes.
        assert all(client.commands == [(1, 1)] and not client.is_connected for client in Peer.instances[1:])
        assert device.address == ADDRESS and device.desired
    finally:
        await device.disconnect()


@pytest.mark.parametrize('payload', [b'short', bytes(51)])
async def test_recovery_skips_candidate_with_invalid_info_before_correct_serial(payload):
    new = '3282ee65-5f36-4c2a-b93f-e93e77b24b2f'
    Peer, Scanner = rotation_backend({OTHER: 'other12345678901', new: SERIAL})
    class InvalidCandidate(Peer):
        async def write_gatt_char(self, uuid, data, **kwargs):
            if self.address == OTHER and data[5:7] == b'\x01\x01':
                self.commands.append((1, 1))
                self.callback(None, packet(1, 1, payload))
                return
            await super().write_gatt_char(uuid, data, **kwargs)
    device = FeishuDevice(ble_loader=lambda: (InvalidCandidate, Scanner), reconnect_delays=(60,))
    await device.connect(ADDRESS)
    Scanner.stale = True
    try:
        async with device.lock:
            await device._open(ADDRESS, SERIAL)
        rejected = next(client for client in Peer.instances if client.address == OTHER)
        assert rejected.commands == [(1, 1)] and not rejected.is_connected
        assert device.address == new and device.state['device_id'] == SERIAL
        assert device.client.commands == [(1, 1), (0x1b, 0x0e)]
    finally:
        await device.disconnect()


@pytest.mark.parametrize('phase', ['discover', 'info'])
async def test_disconnected_intent_stops_rediscovery_before_any_further_commands(phase):
    entered, release = asyncio.Event(), asyncio.Event()
    async def pause(stage):
        if phase == stage:
            entered.set()
            await release.wait()
    Peer, Scanner = rotation_backend({OTHER: SERIAL}, pause=pause)
    device = FeishuDevice(ble_loader=lambda: (Peer, Scanner), reconnect_delays=(60,))
    await device.connect(ADDRESS)
    Scanner.stale = True
    async def recover():
        async with device.operation():
            await device._open(ADDRESS, SERIAL)
    recovering = asyncio.create_task(recover())
    disconnecting = None
    try:
        await asyncio.wait_for(entered.wait(), .5)
        disconnecting = asyncio.create_task(device.disconnect())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(DeviceIdentityError):
            await recovering
        await disconnecting
        if phase == 'discover':
            assert len(Peer.instances) == 1
        else:
            assert Peer.instances[-1].commands == [(1, 1)]
        assert device.address == ADDRESS and not device.snapshot()['connected']
    finally:
        release.set()
        await asyncio.gather(recovering, *([disconnecting] if disconnecting else []), return_exceptions=True)
        await device.disconnect()
