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


@pytest.mark.parametrize('invalid_info', [None, b'short', bytes(51)], ids=['wrong-serial', 'short-info', 'empty-serial'])
async def test_recovery_rejects_each_neighbour_once_across_rounds(invalid_info):
    candidates = {f'{i}282ee65-5f36-4c2a-b93f-e93e77b24b2f': 'other12345678901' for i in range(4)}
    Peer, Scanner = rotation_backend(candidates)
    class Candidate(Peer):
        async def write_gatt_char(self, uuid, data, **kwargs):
            if invalid_info is not None and self.address in candidates and data[5:7] == b'\x01\x01':
                self.commands.append((1, 1))
                self.callback(None, packet(1, 1, invalid_info))
                return
            await super().write_gatt_char(uuid, data, **kwargs)
    device = FeishuDevice(ble_loader=lambda: (Candidate, Scanner), reconnect_delays=(60,))
    await device.connect(ADDRESS)
    Scanner.stale = True
    try:
        for _ in range(3):
            async with device.lock:
                with pytest.raises(TransferInterrupted):
                    await device._open(ADDRESS, SERIAL)
        probes = [client for client in Peer.instances if client.address in candidates]
        assert [client.address for client in probes] == list(candidates)
        assert all(client.commands == [(1, 1)] and not client.is_connected for client in probes)
        assert device._rejected_recovery_addresses == set(candidates)
        assert device.state['device_id'] == SERIAL and device.desired
    finally:
        await device.disconnect()


async def test_recovery_reaches_fourth_candidate_by_second_round():
    neighbours = {f'{i}282ee65-5f36-4c2a-b93f-e93e77b24b2f': 'other12345678901' for i in range(3)}
    new = '9282ee65-5f36-4c2a-b93f-e93e77b24b2f'
    Peer, Scanner = rotation_backend({**neighbours, new: SERIAL})
    device = FeishuDevice(ble_loader=lambda: (Peer, Scanner), reconnect_delays=(60,))
    await device.connect(ADDRESS)
    Scanner.stale = True
    try:
        async with device.lock:
            with pytest.raises(TransferInterrupted):
                await device._open(ADDRESS, SERIAL)
            assert [client.address for client in Peer.instances[1:]] == list(neighbours)
            await device._open(ADDRESS, SERIAL)
        assert [client.address for client in Peer.instances[1:]] == [*neighbours, new]
        assert device.snapshot()['connected'] and device.address == new
        assert device.state['device_id'] == SERIAL
        assert device.client.commands == [(1, 1), (0x1b, 0x0e)]
        assert len(Scanner.discovered) == 2
    finally:
        await device.disconnect()


@pytest.mark.parametrize('same_address', [False, True])
async def test_user_connect_clears_rejected_recovery_addresses(same_address):
    new = '9282ee65-5f36-4c2a-b93f-e93e77b24b2f'
    candidates = {OTHER: 'other12345678901', new: SERIAL}
    Peer, Scanner = rotation_backend(candidates)
    device = FeishuDevice(ble_loader=lambda: (Peer, Scanner), reconnect_delays=(60,))
    await device.connect(ADDRESS)
    Scanner.stale = True
    try:
        async with device.lock:
            await device._open(ADDRESS, SERIAL)
        assert device._rejected_recovery_addresses == {OTHER}
        Scanner.stale = False
        await device.connect(new if same_address else ADDRESS)
        assert device.snapshot()['connected']
        candidates[OTHER] = SERIAL  # The previously rejected address must be eligible in this new intent.
        Scanner.stale = True
        async with device.lock:
            await device._open(device.address, SERIAL)
        assert device.address == OTHER and device.snapshot()['connected']
        assert len([client for client in Peer.instances if client.address == OTHER]) == 2
        assert device._rejected_recovery_addresses == set()
    finally:
        await device.disconnect()


async def test_user_disconnect_clears_rejected_recovery_addresses():
    candidates = {OTHER: 'other12345678901'}
    Peer, Scanner = rotation_backend(candidates)
    device = FeishuDevice(ble_loader=lambda: (Peer, Scanner), reconnect_delays=(60,))
    await device.connect(ADDRESS)
    Scanner.stale = True
    try:
        async with device.lock:
            with pytest.raises(TransferInterrupted):
                await device._open(ADDRESS, SERIAL)
        assert device._rejected_recovery_addresses == {OTHER}
        await device.disconnect()
        assert device._rejected_recovery_addresses == set()
        assert not device.desired
        Scanner.stale = False
        await device.connect(ADDRESS)
        candidates[OTHER] = SERIAL
        Scanner.stale = True
        async with device.lock:
            await device._open(ADDRESS, SERIAL)
        assert device.address == OTHER and device.snapshot()['connected']
        assert len([client for client in Peer.instances if client.address == OTHER]) == 2
    finally:
        await device.disconnect()


@pytest.mark.parametrize('stage,error_type', [('connect', BleakError), ('connect', TimeoutError), ('info', TimeoutError)])
async def test_recovery_retries_candidate_after_transport_failure(stage, error_type):
    Peer, Scanner = rotation_backend({OTHER: SERIAL})
    class TransientCandidate(Peer):
        failed = False
        def fail_once(self, current_stage):
            if self.address == OTHER and stage == current_stage and not TransientCandidate.failed:
                TransientCandidate.failed = True
                raise error_type('temporary candidate transport failure')
        async def connect(self):
            self.fail_once('connect')
            await super().connect()
        async def write_gatt_char(self, uuid, data, **kwargs):
            if data[5:7] == b'\x01\x01':
                self.fail_once('info')
            await super().write_gatt_char(uuid, data, **kwargs)
    device = FeishuDevice(ble_loader=lambda: (TransientCandidate, Scanner), reconnect_delays=(60,))
    await device.connect(ADDRESS)
    Scanner.stale = True
    try:
        async with device.lock:
            with pytest.raises(TransferInterrupted):
                await device._open(ADDRESS, SERIAL)
            await device._open(ADDRESS, SERIAL)
        assert len([client for client in Peer.instances if client.address == OTHER]) == 2
        assert device.snapshot()['connected'] and device.address == OTHER
        assert device._rejected_recovery_addresses == set()
    finally:
        await device.disconnect()


async def test_expired_connection_intent_does_not_reject_candidate():
    entered, release = asyncio.Event(), asyncio.Event()
    async def pause(stage):
        if stage == 'info':
            entered.set()
            await release.wait()
    candidates = {OTHER: 'other12345678901'}
    Peer, Scanner = rotation_backend(candidates, pause=pause)
    device = FeishuDevice(ble_loader=lambda: (Peer, Scanner), reconnect_delays=(60,))
    await device.connect(ADDRESS)
    Scanner.stale = True
    async def recover():
        async with device.lock:
            await device._open(ADDRESS, SERIAL)
    recovering = asyncio.create_task(recover())
    try:
        await asyncio.wait_for(entered.wait(), .5)
        device.state['connection_generation'] += 1  # Supersede this attempt without a later cleanup hiding its effects.
        release.set()
        with pytest.raises(DeviceIdentityError, match='连接操作已变化'):
            await recovering
        assert Peer.instances[-1].commands == [(1, 1)]
        assert not device.snapshot()['connected']
        candidates[OTHER] = SERIAL
        async with device.lock:
            await device._open(ADDRESS, SERIAL)
        assert device.address == OTHER and device.snapshot()['connected']
        assert len([client for client in Peer.instances if client.address == OTHER]) == 2
        assert device._rejected_recovery_addresses == set()
    finally:
        release.set()
        await asyncio.gather(recovering, return_exceptions=True)
        await device.disconnect()


async def test_known_lookup_cancelled_at_ready_completion_never_retrieves(monkeypatch):
    module, calls, _, _ = fake_backend(monkeypatch)
    manager_type, _, _ = module._load_macos_backend()
    task = None

    async def ready(self):
        calls.append(('ready',))
        task.cancel()

    monkeypatch.setattr(manager_type, 'wait_until_ready', ready)
    task = asyncio.create_task(module.retrieve_known_macos_device(ADDRESS))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [('ready',)]


def incompatible_known_backend(monkeypatch, failure):
    known = BLEDevice(ADDRESS, 'soundcore Work', ('private-layout', 'manager'))
    advertised = SimpleNamespace(address=ADDRESS, name='soundcore Work')
    state = dict(broken=False, advertised=False, stop=None, lookup_calls=0, scan_calls=0)
    device = None

    def fail(stage):
        if state['broken'] and stage == failure:
            if state['stop'] == 'intent':
                device.request_disconnect()
            if state['stop'] == 'cancel':
                raise asyncio.CancelledError()
            raise TypeError('unsupported private backend layout')

    class Peer(FakeBLE):
        instances = []
        def __init__(self, target, **kwargs):
            self.known = target is known
            if self.known:
                fail('constructor')
            super().__init__(target, **kwargs)
        async def connect(self):
            await super().connect()
            if self.known:
                fail('connect')
        async def start_notify(self, *args):
            if self.known:
                fail('notify')
            await super().start_notify(*args)

    class Scanner:
        @staticmethod
        async def find_device_by_address(address, **kwargs):
            assert address == ADDRESS
            state['scan_calls'] += 1
            return advertised if state['advertised'] else None
        @staticmethod
        async def discover(**kwargs):
            pytest.fail('initial fallback must not probe unselected devices')

    async def lookup(address):
        state['lookup_calls'] += 1
        state['advertised'] = True
        fail('lookup')
        return known

    monkeypatch.setattr('backend.feishu.platform', 'darwin')
    monkeypatch.setattr('backend.devices._load_ble', lambda: (Peer, Scanner))
    monkeypatch.setattr(helper(), 'retrieve_known_macos_device', lookup)
    device = FeishuDevice(reconnect_delays=(60,))
    return device, Peer, Scanner, state, known, advertised


@pytest.mark.parametrize('cached,failure', [(False, 'lookup'), *[
    (cached, failure) for cached in (False, True) for failure in ('constructor', 'connect', 'notify')]])
async def test_incompatible_known_device_falls_back_to_selected_advertisement(monkeypatch, cached, failure):
    device, Peer, _, state, known, advertised = incompatible_known_backend(monkeypatch, failure)
    try:
        if cached:
            await device.connect(ADDRESS)
            assert device._known_peripheral[1] is known
        state['broken'] = True
        if cached:
            async with device.lock:
                await device._open(ADDRESS, SERIAL)
        else:
            await device.connect(ADDRESS)
        assert device.snapshot()['connected']
        assert device.state['device_id'] == SERIAL
        assert device._known_peripheral[1] is advertised
        assert state['scan_calls'] == 2 and state['lookup_calls'] == 1
        assert all(not peer.is_connected for peer in Peer.instances[:-1])
        assert device.client.commands == [(1, 1), (0x1b, 0x0e)]
    finally:
        await device.disconnect()


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('stop', ['intent', 'cancel'])
async def test_known_compatibility_failure_never_falls_back_after_stop(monkeypatch, cached, stop):
    device, Peer, _, state, _, _ = incompatible_known_backend(monkeypatch, 'notify')
    try:
        if cached:
            await device.connect(ADDRESS)
        state.update(broken=True, stop=stop)
        error = DeviceIdentityError if stop == 'intent' else asyncio.CancelledError
        with pytest.raises(error):
            if cached:
                async with device.operation():
                    await device._open(ADDRESS, SERIAL)
            else:
                await device.connect(ADDRESS)
        assert state['scan_calls'] == state['lookup_calls'] == 1
        assert all(not peer.is_connected for peer in Peer.instances)
        assert not device.snapshot()['connected']
    finally:
        await device.disconnect()


async def test_known_lookup_error_after_stop_never_restarts_discovery(monkeypatch):
    device, Peer, _, state, _, _ = incompatible_known_backend(monkeypatch, 'lookup')
    state.update(broken=True, stop='intent')
    try:
        with pytest.raises(DeviceIdentityError):
            await device.connect(ADDRESS)
        assert state['scan_calls'] == 1
        assert not Peer.instances
    finally:
        await device.disconnect()


async def test_known_fallback_does_not_connect_neighbours_on_initial_selection(monkeypatch):
    device, _, Scanner, state, _, _ = incompatible_known_backend(monkeypatch, 'constructor')
    state['broken'] = True
    async def missing(*args, **kwargs):
        state['scan_calls'] += 1
        return None
    monkeypatch.setattr(Scanner, 'find_device_by_address', missing)
    try:
        with pytest.raises(TransferInterrupted):
            await device.connect(ADDRESS)
        assert device._known_peripheral is None
        assert state['scan_calls'] == 2
    finally:
        await device.disconnect()


@pytest.mark.parametrize('injected', [False, True])
async def test_normal_scanned_constructor_programming_errors_remain_visible(monkeypatch, injected):
    class BrokenPeer:
        def __init__(self, *args, **kwargs):
            raise TypeError('ordinary constructor bug')
    monkeypatch.setattr('backend.feishu.platform', 'darwin')
    monkeypatch.setattr('backend.devices._load_ble', lambda: (BrokenPeer, FakeScanner))
    device = FeishuDevice(ble_loader=(lambda: (BrokenPeer, FakeScanner)) if injected else None)
    with pytest.raises(TypeError, match='ordinary constructor bug'):
        await device.connect(ADDRESS)


async def test_known_fallback_still_rejects_wrong_serial_before_listing(monkeypatch):
    device, Peer, _, state, _, _ = incompatible_known_backend(monkeypatch, 'constructor')
    try:
        await device.connect(ADDRESS)
        state['broken'] = True
        write = Peer.write_gatt_char
        async def wrong_info(peer, uuid, data, **kwargs):
            if not peer.known and data[5:7] == b'\x01\x01':
                peer.commands.append((1, 1))
                info = bytearray(51)
                info[8:24] = b'OTHER12345678901'
                peer.callback(None, packet(1, 1, info))
            else:
                await write(peer, uuid, data, **kwargs)
        monkeypatch.setattr(Peer, 'write_gatt_char', wrong_info)
        async with device.lock:
            with pytest.raises(DeviceIdentityError, match='序列号'):
                await device._open(ADDRESS, SERIAL)
        assert device.client.commands == [(1, 1)]
        assert device._known_peripheral is None
        assert not device.snapshot()['connected']
    finally:
        await device.disconnect()


async def test_invalid_known_identity_clears_verified_peripheral_cache(monkeypatch):
    device, Peer, _, _, known, _ = incompatible_known_backend(monkeypatch, 'constructor')
    try:
        await device.connect(ADDRESS)
        assert device._known_peripheral[1] is known
        async def malformed(peer, uuid, data, **kwargs):
            peer.callback(None, packet(1, 1, b'short'))
        monkeypatch.setattr(Peer, 'write_gatt_char', malformed)
        async with device.lock:
            with pytest.raises(DeviceIdentityError):
                await device._open(ADDRESS, SERIAL)
        assert device._known_peripheral is None
    finally:
        await device.disconnect()
