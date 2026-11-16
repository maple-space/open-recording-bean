"""Failures with different retry rules for the recorder's read-only sync."""


class TransferInterrupted(ValueError):
    """The same source can be read again after recovering the BLE connection."""


class DeviceIdentityError(ValueError):
    """The selected device changed; never retry against a different source."""
