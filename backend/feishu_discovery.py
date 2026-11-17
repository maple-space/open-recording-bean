"""Resolve an explicit macOS peripheral UUID without requiring advertisements.

Apple's retrievePeripheralsWithIdentifiers_ API is public. Passing its result to
Bleak requires the private CoreBluetooth delegate and BLEDevice.details layout.
That adapter is isolated here and feature-detected: unsupported Bleak versions
retain normal discovery. Imports are lazy so non-macOS services remain usable.
No scan or connection is started by this lookup.
"""
from sys import platform
from uuid import UUID

from .async_wait import wait_with_timeout


def _load_macos_backend():
    try:
        from Foundation import NSUUID
        from bleak.backends.corebluetooth.CentralManagerDelegate import CentralManagerDelegate
        from bleak.backends.device import BLEDevice
    except (ImportError, AttributeError):
        return None
    if not callable(getattr(CentralManagerDelegate, 'wait_until_ready', None)):
        return None
    return CentralManagerDelegate, NSUUID, BLEDevice


async def retrieve_known_macos_device(address):
    if platform != 'darwin' or not isinstance(address, str):
        return None
    try:
        identifier = str(UUID(address)).upper()
    except ValueError:
        return None
    backend = _load_macos_backend()
    if backend is None:
        return None
    manager_type, uuid_type, device_type = backend
    manager = manager_type()
    await wait_with_timeout(manager.wait_until_ready(), 10)
    central = getattr(manager, 'central_manager', None)
    retrieve = getattr(central, 'retrievePeripheralsWithIdentifiers_', None)
    if not callable(retrieve):
        return None
    native_id = uuid_type.alloc().initWithUUIDString_(identifier)
    if native_id is None:
        return None
    for peripheral in retrieve([native_id]):
        actual = str(peripheral.identifier().UUIDString())
        if actual.upper() == identifier:
            return device_type(actual, peripheral.name(), (peripheral, manager))
    return None
