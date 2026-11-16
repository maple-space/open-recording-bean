"""Byte-for-byte Ogg compatibility against the previous non-reflected CRC."""
import array
import io
import math
import random
import struct

import pytest

from backend import feishu_protocol as protocol


def old_crc_entry(value):
    crc = value << 24
    for _ in range(8):
        crc = ((crc << 1) ^ (0x04c11db7 if crc & 0x80000000 else 0)) & 0xffffffff
    return crc


OLD_TABLE = tuple(old_crc_entry(value) for value in range(256))


def old_crc(data):
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xffffffff) ^ OLD_TABLE[(crc >> 24) ^ byte]
    return crc


def old_page(packets, sequence, granule, flags=0):
    segments = bytes(len(packet) for packet in packets)
    header = b'OggS' + bytes([0, flags]) + struct.pack('<QIII', granule, 0x41524b52, sequence, 0) + bytes([len(segments)]) + segments
    page = bytearray(header + b''.join(packets))
    struct.pack_into('<I', page, 22, old_crc(page))
    return page


@pytest.mark.parametrize('size', [0, 1, 2, 7, 8, 27, 255, 256, 257, 4096, 8192, 65536])
def test_crc_matches_previous_algorithm_for_varied_lengths(size):
    data = random.Random(0x04c11db7 + size).randbytes(size)
    assert protocol._ogg_crc(data) == old_crc(data)
    assert protocol._ogg_crc(bytearray(data)) == old_crc(data)


@pytest.mark.parametrize('count', [0, 1, 2, 49, 50, 51, 255])
def test_random_pages_are_byte_identical_to_previous_pages(count):
    randomizer = random.Random(1234 + count)
    for _ in range(4):
        packets = [randomizer.randbytes(randomizer.randrange(161)) for _ in range(count)]
        sequence, granule, flags = randomizer.getrandbits(32), randomizer.getrandbits(64), randomizer.randrange(8)
        assert protocol._ogg_page(packets, sequence, granule, flags) == old_page(packets, sequence, granule, flags)


def test_real_opus_decodes_with_identical_pages_and_unchanged_preskip(monkeypatch):
    av = pytest.importorskip('av')
    encoder = av.CodecContext.create('libopus', 'w')
    encoder.sample_rate, encoder.layout, encoder.format, encoder.bit_rate = 16000, 'mono', 's16', 32000
    encoder.options = {'frame_duration': '20', 'application': 'voip'}
    encoder.open()
    packets = []
    for start in range(0, 24000, 320):
        samples = array.array('h', (round(6000 * math.sin(2 * math.pi * 440 * index / 16000))
                                   for index in range(start, start + 320)))
        frame = av.AudioFrame(format='s16', layout='mono', samples=320)
        frame.planes[0].update(samples.tobytes())
        frame.sample_rate, frame.pts = 16000, start
        packets.extend(bytes(packet) for packet in encoder.encode(frame))
    assert packets and max(map(len, packets)) <= 160
    actual = protocol.mux_ogg(packets)
    with monkeypatch.context() as patch:
        patch.setattr(protocol, '_ogg_page', old_page)
        expected = protocol.mux_ogg(packets)
    assert actual == expected
    head = actual.index(b'OpusHead')
    assert struct.unpack_from('<H', actual, head + 10)[0] == 3840
    with av.open(io.BytesIO(actual)) as container:
        decoded = list(container.decode(audio=0))
    assert decoded and sum(frame.samples for frame in decoded) > 16000
    assert all(frame.sample_rate == 48000 for frame in decoded)
