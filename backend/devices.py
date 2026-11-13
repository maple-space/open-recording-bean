"""The recorder, as the rest of the service sees it.

Bluetooth is loaded lazily: constructing the manager never scans for hardware.
Calls are serialised by the application's job runner.
"""
from __future__ import annotations

KINDS = ('feishu',)


class DeviceError(RuntimeError):
    """A device operation failed; the text is safe to show on the local page."""


def _load_ble():
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        raise DeviceError('蓝牙依赖 bleak 未安装，请使用本地服务的 Python 环境。') from None
    return BleakClient, BleakScanner


class DeviceManager:
    """One recorder kind today. The kind stays in every call so that the API paths
    (`/api/devices/feishu`) and the stored records keep saying which hardware they mean."""

    def __init__(self):
        from .feishu import FeishuDevice
        self.feishu = FeishuDevice()

    def _device(self, kind):
        if kind not in KINDS:
            raise DeviceError('不支持的设备类型；本地蓝牙服务只支持录音豆（soundcore Work / 飞书录音豆，D3200）。')
        return self.feishu

    def snapshot(self, kind):
        return self._device(kind).snapshot()

    async def scan(self, kind):
        return await self._device(kind).scan()

    async def connect(self, kind, address=None):
        return await self._device(kind).connect(address)

    async def disconnect(self, kind):
        return await self._device(kind).disconnect()

    async def refresh(self, kind):
        return await self._device(kind).refresh()

    async def download(self, kind, original_id, output_dir, progress=None):
        return await self._device(kind).download(original_id, output_dir, progress)
