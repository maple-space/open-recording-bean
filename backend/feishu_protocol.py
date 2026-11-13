"""D3200 wire, crypto and audio primitives; no Bluetooth and no logging here."""
import hmac
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SERVICE = '020cf5da-0000-1000-8000-00805f9b34fb'
WRITE = '00007777-0000-1000-8000-00805f9b34fb'
NOTIFY = '00008888-0000-1000-8000-00805f9b34fb'
BYTES_PER_MS = 8         # one 160-byte Opus frame every 20 ms


def command(kind, ident, payload=b''):
    raw = bytes([8, 238, 0, 0, 0, kind, ident]) + struct.pack('<H', 10 + len(payload)) + payload
    return raw + bytes([sum(raw) & 255])


class Packets:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= 10:
            start = self.buffer.find(b'\x09\xff\x00\x00')
            if start < 0:
                self.buffer = self.buffer[-3:]
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < 10:
                break
            size = int.from_bytes(self.buffer[7:9], 'little')
            if size < 10:
                del self.buffer[:1]
                continue
            if len(self.buffer) < size:
                break
            raw = bytes(self.buffer[:size])
            del self.buffer[:size]
            if sum(raw[:-1]) & 255 == raw[-1]:
                result.append(raw)
        return result


def battery(value):
    value &= 127
    return (value + 1) * 10 if value <= 9 else min(value, 100)


def parse_info(p):
    if len(p) < 32:
        raise ValueError('设备信息不完整')
    ascii_text = lambda b: bytes(b).split(b'\0')[0].decode('ascii', errors='replace').strip()
    result = dict(battery=battery(p[1]), charging=p[2] == 1, firmwareVersion=ascii_text(p[3:8]),
                  serialNumber=ascii_text(p[8:24]).lower(),
                  totalMemoryKb=int.from_bytes(p[24:28], 'little'), freeMemoryKb=int.from_bytes(p[28:32], 'little'))
    if len(p) > 32:
        result['boxCharging'] = p[32] == 1
    if len(p) > 38:
        result['boxBattery'] = battery(p[38])
    if len(p) > 50:
        result['recordStatus'] = p[50] if p[50] in (0, 1, 2) else 0
    if not result['serialNumber']:
        raise ValueError('设备没有返回有效序列号')
    return result


def parse_directory(p):
    if len(p) < 2:
        raise ValueError('设备录音目录不完整')
    count = int.from_bytes(p[:2], 'little')
    if len(p) < 2 + count * 12 + 8:
        raise ValueError('设备录音目录不完整')
    rows = []
    for offset in range(2, 2 + count * 12, 12):
        # Start time, end time, and the length of the audio in milliseconds. Measured on firmware
        # 04.92: a full six-hour file reports 21 600 000 here, while the recorder's free space
        # drops by 28.8 MB per hour. It stores 160 bytes for every 20 ms, eight bytes per millisecond.
        ident, _end, length = struct.unpack_from('<III', p, offset)
        rows.append(dict(original_id=str(ident), title=f'录音 {ident}', recorded_at=ident,
                         size_bytes=length * BYTES_PER_MS, duration_ms=length))
    return rows, count


def decrypt(data, key, counter):
    ctx = Cipher(algorithms.AES(key), modes.CTR(counter)).decryptor()
    return ctx.update(data) + ctx.finalize()


class DeviceCrypto:
    def __init__(self):
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.session_key = None

    def public_key(self):
        return self.private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def handshake(self, p):
        if len(p) < 97:
            raise ValueError('ECDH 握手不完整')
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p[:65])
        shared = self.private.exchange(ec.ECDH(), peer)
        if not hmac.compare_digest(shared, p[65:97]):
            raise ValueError('ECDH 握手校验失败')
        self.session_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b'\x01\x02\x03', info=b'\x01\x02\x03').derive(shared)

    def file_secret(self, p, expected_id):
        if self.session_key is None or len(p) < 87:
            raise ValueError('录音文件头不完整')
        ident, size = struct.unpack_from('<II', p)
        if ident != expected_id:
            raise ValueError('返回的录音文件标识不匹配')
        if p[86]:
            raise ValueError(f'设备拒绝导出（错误码 {p[86]}）')
        plain = decrypt(p[24:70], self.session_key, p[70:86])
        if not hmac.compare_digest(plain[:14], b'soundcored3200'):
            raise ValueError('录音文件密钥校验失败')
        return plain[14:46], p[8:20], size


def _ogg_page(packets, sequence, granule, flags=0):
    segments = bytes(len(p) for p in packets)  # D3200 fixed frames <= 160 bytes.
    header = b'OggS' + bytes([0, flags]) + struct.pack('<QIII', granule, 0x41524b52, sequence, 0) + bytes([len(segments)]) + segments
    page = bytearray(header + b''.join(packets))
    crc = 0
    for byte in page:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04c11db7 if crc & 0x80000000 else 0)) & 0xffffffff
    struct.pack_into('<I', page, 22, crc)
    return page


def mux_ogg(frames):
    if not frames:
        raise ValueError('录音数据为空')
    head = b'OpusHead' + struct.pack('<BBHIhB', 1, 1, 3840, 48000, 0, 0)
    vendor = b'D3200 Local'
    tags = b'OpusTags' + struct.pack('<I', len(vendor)) + vendor + bytes(4)
    pages = [_ogg_page([head], 0, 0, 2), _ogg_page([tags], 1, 0)]
    for start in range(0, len(frames), 50):
        batch = [frame.rstrip(b'\0') or b'\0' for frame in frames[start:start + 50]]
        end = start + len(batch)
        pages.append(_ogg_page(batch, start // 50 + 2, end * 960, 4 if end == len(frames) else 0))
    return b''.join(pages)
