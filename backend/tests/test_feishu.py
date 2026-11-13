import asyncio
import struct
from types import SimpleNamespace

import pytest


def packet(kind, ident, payload=b'', status=1):
    raw = bytes([9, 255, 0, 0, status, kind, ident]) + struct.pack('<H', len(payload) + 10) + payload
    return raw + bytes([sum(raw) & 255])


def test_framing_and_info():
    from backend.feishu_protocol import Packets, command, parse_info, parse_directory
    parser = Packets()
    raw = packet(1, 1, b'hello')
    assert parser.feed(b'x' * 15 + raw[:2]) == []
    assert parser.feed(raw[2:]) == [raw]
    assert command(0x18, 0x82, b'\x01').hex() == '08ee00000018820b00019c'
    info = bytearray(51)
    info[1] = 9
    info[3:8] = b'04.92'
    info[8:24] = b'ABC1234567890123'
    info[50] = 2
    result = parse_info(info)
    assert result['serialNumber'] == 'abc1234567890123'
    assert result['recordStatus'] == 2
    assert result['battery'] == 100
    with pytest.raises(ValueError, match='目录'):
        parse_directory(struct.pack('<HIII', 1, 1, 2, 160))


def test_the_file_list_gives_the_length_of_the_audio_and_the_room_it_takes_on_the_recorder():
    from backend.feishu_protocol import parse_directory
    six_hours = 6 * 3600 * 1000
    started = 1763000000
    # Nine hours passed between start and end, because the recording was paused for three of them.
    rows, count = parse_directory(struct.pack('<HIII', 1, started, started + 9 * 3600, six_hours) + bytes(8))
    assert count == 1 and rows[0]['original_id'] == str(started)
    assert rows[0]['duration_ms'] == six_hours
    assert rows[0]['size_bytes'] == 172_800_000        # 160 bytes for every 20 ms: 28.8 MB an hour


class FakeBLE:
    instances = []
    def __init__(self, device, *, disconnected_callback, **kwargs):
        self.address = device.address
        self.is_connected = False
        self.disconnected_callback = disconnected_callback
        self.commands = []
        self.instances.append(self)

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.disconnected_callback(self)

    async def start_notify(self, uuid, callback):
        self.callback = callback

    async def write_gatt_char(self, uuid, data, **kwargs):
        kind, ident = data[5:7]
        self.commands.append((kind, ident))
        if (kind, ident) == (1, 1):
            info = bytearray(51)
            info[1] = 9
            info[3:8] = b'04.92'
            info[8:24] = b'ABC1234567890123'
            self.callback(None, packet(kind, ident, info))
        elif (kind, ident) == (0x1b, 0x0e):
            self.callback(None, packet(kind, ident, bytes(10)))


class FakeScanner:
    @staticmethod
    async def find_device_by_address(address, **kwargs):
        return SimpleNamespace(address=address, name='soundcore Work')


def test_persistent_connection_and_reconnect_only_queries():
    from backend.feishu import FeishuDevice
    async def scenario():
        FakeBLE.instances.clear()
        device = FeishuDevice(ble_loader=lambda: (FakeBLE, FakeScanner), reconnect_delays=(.01,))
        await device.connect('mac-uuid')
        for _ in range(4):
            assert device.snapshot()['connected']
            await asyncio.sleep(.01)
        assert len(FakeBLE.instances) == 1
        await device.connect('mac-uuid')
        assert len(FakeBLE.instances) == 1
        await FakeBLE.instances[0].disconnect()  # Unexpected hardware link loss.
        for _ in range(50):
            if len(FakeBLE.instances) == 2 and device.snapshot()['connected']:
                break
            await asyncio.sleep(.01)
        assert len(FakeBLE.instances) == 2
        assert device.snapshot()['device_id'] == 'abc1234567890123'
        assert set(FakeBLE.instances[1].commands) <= {(1, 1), (0x1b, 0x0e)}
        await device.disconnect()
        await asyncio.sleep(.04)
        assert not device.snapshot()['connected']
        assert not device.snapshot()['desired_connected']
        assert len(FakeBLE.instances) == 2
    asyncio.run(scenario())


class AudioBLE(FakeBLE):
    sequences = [0, 1, 2]
    corrupt_handshake = False
    omit_done = False

    async def write_gatt_char(self, uuid, data, **kwargs):
        from backend.feishu_protocol import decrypt
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        kind, ident = data[5:7]
        if (kind, ident) == (0x1b, 0x0e):
            self.callback(None, packet(kind, ident, struct.pack('<HIII', 1, 123, 124, 480) + bytes(8)))
        elif (kind, ident) == (0x2e, 1):
            private = ec.generate_private_key(ec.SECP256R1())
            shared = private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), data[9:-1]))
            self.session_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b'\x01\x02\x03', info=b'\x01\x02\x03').derive(shared)
            public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            self.callback(None, packet(kind, ident, public + (bytes(32) if self.corrupt_handshake else shared)))
        elif (kind, ident) == (0x1a, 7):
            key, nonce, counter = bytes(range(32)), bytes(range(16)), bytes(16)
            payload = struct.pack('<II', 123, 480) + nonce + decrypt(b'soundcored3200' + key, self.session_key, counter) + counter + b'\0'
            self.callback(None, packet(kind, ident, payload))
            for seq in self.sequences:
                plain = b'\xf8\xff\xfe' + bytes(157)
                encrypted = decrypt(plain, key, nonce[:12] + (seq * 10).to_bytes(4, 'big'))
                self.callback(None, packet(0x1a, 8, struct.pack('<I', seq) + b'\0' + encrypted + b'\0'))
            if not self.omit_done:
                self.callback(None, packet(0x1a, 0x0a))
        else:
            await super().write_gatt_char(uuid, data, **kwargs)


def test_audio_crypto_out_of_order_duplicates_and_ogg(tmp_path):
    from backend.feishu import FeishuDevice
    async def scenario():
        class Audio(AudioBLE):
            sequences = [2, 0, 2, 1]
        device = FeishuDevice(ble_loader=lambda: (Audio, FakeScanner))
        await device.connect('mac-uuid')
        result = await device.download('123', tmp_path)
        assert result['duration_ms'] == 60
        data = result['path'].read_bytes()
        assert data.startswith(b'OggS') and b'OpusHead' in data
        assert device.snapshot()['connected']
        await device.disconnect()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['gap', 'handshake', 'timeout', 'disconnected'])
def test_failed_audio_never_creates_archive(tmp_path, failure):
    from backend.feishu import FeishuDevice
    async def scenario():
        class Audio(AudioBLE):
            sequences = [0, 2] if failure == 'gap' else [0, 1, 2]
            corrupt_handshake = failure == 'handshake'
            omit_done = failure in ('timeout', 'disconnected')
        device = FeishuDevice(ble_loader=lambda: (Audio, FakeScanner))
        await device.connect('mac-uuid')
        if failure == 'disconnected':
            async def drop():
                await asyncio.sleep(.01)
                await device.client.disconnect()
            dropping = asyncio.create_task(drop())
        with pytest.raises(ValueError):
            await device.download('123', tmp_path, idle_timeout=.04)
        if failure == 'disconnected':
            await dropping
        assert list(tmp_path.iterdir()) == []
        await device.disconnect()
    asyncio.run(scenario())


def test_reconnect_rejects_changed_serial_and_stops_retrying():
    from backend.feishu import FeishuDevice
    async def scenario():
        class WrongIdentity(FakeBLE):
            async def write_gatt_char(self, uuid, data, **kwargs):
                if len(FakeBLE.instances) > 1 and data[5:7] == b'\x01\x01':
                    info = bytearray(51)
                    info[8:24] = b'DIFFERENT1234567'
                    self.callback(None, packet(1, 1, info))
                else:
                    await super().write_gatt_char(uuid, data, **kwargs)
        FakeBLE.instances.clear()
        device = FeishuDevice(ble_loader=lambda: (WrongIdentity, FakeScanner), reconnect_delays=(.01,))
        await device.connect('mac-uuid')
        await device.client.disconnect()
        await asyncio.sleep(.08)
        assert not device.snapshot()['connected']
        assert not device.snapshot()['desired_connected']
        assert '序列号' in device.snapshot()['error']
        assert len(FakeBLE.instances) == 2
        await device.disconnect()
    asyncio.run(scenario())


def test_new_audio_extends_idle_deadline_but_duplicates_do_not(tmp_path):
    from backend.feishu import FeishuDevice
    async def scenario(duplicate):
        class SlowAudio(AudioBLE):
            def callback_with_delay(self, _, raw):
                kind, ident = raw[5:7]
                if (kind, ident) == (0x1a, 8):
                    seq = int.from_bytes(raw[9:13], 'little')
                    asyncio.get_running_loop().call_later(.025 * (seq + 1), self.real_callback, None, raw)
                    if duplicate:
                        for delay in (.04, .06, .08, .1):
                            asyncio.get_running_loop().call_later(delay, self.real_callback, None, raw)
                elif (kind, ident) == (0x1a, 0x0a):
                    asyncio.get_running_loop().call_later(.1, self.real_callback, None, raw)
                else:
                    self.real_callback(None, raw)

            async def start_notify(self, uuid, callback):
                self.real_callback = callback
                self.callback = self.callback_with_delay
        SlowAudio.sequences = [0] if duplicate else [0, 1, 2]
        SlowAudio.omit_done = duplicate
        device = FeishuDevice(ble_loader=lambda: (SlowAudio, FakeScanner))
        await device.connect('mac-uuid')
        try:
            if duplicate:
                with pytest.raises(ValueError, match='60 分钟'):
                    await device.download('123', tmp_path, idle_timeout=.045)
            else:
                result = await device.download('123', tmp_path, idle_timeout=.045)
                assert result['duration_ms'] == 60
        finally:
            await device.disconnect()
        await asyncio.sleep(.11)  # Drain scheduled fake notifications.
    asyncio.run(scenario(False))
    asyncio.run(scenario(True))
