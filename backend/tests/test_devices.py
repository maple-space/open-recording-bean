import asyncio
from types import SimpleNamespace

import pytest

from backend import devices
from backend.feishu_protocol import SERVICE


def run(awaitable):
    return asyncio.run(awaitable)


def test_constructing_the_manager_does_not_touch_bluetooth(monkeypatch):
    monkeypatch.setattr(devices, '_load_ble', lambda: pytest.fail('Bluetooth must stay lazy'))
    manager = devices.DeviceManager()
    state = manager.snapshot('feishu')
    assert state['connected'] is False and state['files'] == []


def test_a_snapshot_is_a_copy_and_an_unknown_kind_fails():
    manager = devices.DeviceManager()
    manager.snapshot('feishu')['files'].append({'original_id': 'injected'})
    assert manager.snapshot('feishu')['files'] == []
    for kind in ('other', 'unknown'):
        with pytest.raises(devices.DeviceError, match='飞书录音豆'):
            manager.snapshot(kind)
        with pytest.raises(devices.DeviceError):
            run(manager.scan(kind))


def test_a_scan_lists_recorders_only(monkeypatch):
    class Scanner:
        @staticmethod
        async def discover(**kwargs):
            return {
                'by-service': (SimpleNamespace(address='a1', name=None),
                               SimpleNamespace(local_name=None, service_uuids=[SERVICE.upper()])),
                'by-name': (SimpleNamespace(address='a2', name='soundcore Work'),
                            SimpleNamespace(local_name=None, service_uuids=[])),
                'other': (SimpleNamespace(address='x1', name='Headphones'),
                          SimpleNamespace(local_name=None, service_uuids=[])),
            }
    monkeypatch.setattr(devices, '_load_ble', lambda: (None, Scanner))
    found = run(devices.DeviceManager().scan('feishu'))
    assert found == [{'address': 'a1', 'name': '录音豆'}, {'address': 'a2', 'name': 'soundcore Work'}]


def test_every_call_goes_to_the_recorder(tmp_path):
    calls = []

    class Recorder:
        def snapshot(self):
            return {'connected': True}

        async def scan(self):
            calls.append('scan')
            return []

        async def connect(self, address):
            calls.append(('connect', address))
            return self.snapshot()

        async def disconnect(self):
            calls.append('disconnect')
            return self.snapshot()

        async def refresh(self):
            calls.append('refresh')
            return self.snapshot()

        async def download(self, original_id, output_dir, progress):
            calls.append(('download', original_id, output_dir, progress))
            return {'path': output_dir / 'a.ogg'}

    manager = devices.DeviceManager()
    manager.feishu = Recorder()

    async def all_of_them():
        await manager.scan('feishu')
        await manager.connect('feishu', 'a1')
        await manager.refresh('feishu')
        await manager.download('feishu', '7', tmp_path, None)
        await manager.disconnect('feishu')

    run(all_of_them())
    assert calls == ['scan', ('connect', 'a1'), 'refresh', ('download', '7', tmp_path, None), 'disconnect']


def test_a_missing_bluetooth_library_is_an_ordinary_error(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_bleak(name, *args, **kwargs):
        if name == 'bleak':
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_bleak)
    with pytest.raises(devices.DeviceError, match='bleak'):
        devices._load_ble()
