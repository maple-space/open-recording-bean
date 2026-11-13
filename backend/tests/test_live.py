"""Realtime input against a scripted 04.92 recorder.

The scripted replies mirror bytes captured from the real device on 2026-11-13:
two-byte answers to 18 82, a live header whose size is FFFFFFFF, a realtime
transfer that starts at the live edge, stays open across pause and resume for the
life of the connection, and refuses a second request with error 4.
"""
import asyncio
import functools
import os
import struct
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from backend.app import create_app
from backend.feishu import FeishuDevice
from backend.feishu_live import stream_live
from backend.feishu_protocol import decrypt
from backend.live import KEPT_SEGMENTS, LiveInput, TranscriptLog
from backend.realtime_asr import RealtimeASR
from backend.speakers import Voices


def packet(kind, ident, payload=b'', status=1):
    raw = bytes([9, 255, 0, 0, status, kind, ident]) + struct.pack('<H', len(payload) + 10) + payload
    return raw + bytes([sum(raw) & 255])


class World:
    """What survives a BLE reconnect: the recorder keeps its recording state when the link drops."""

    def __init__(self, frames=None):
        self.record = 0
        self.file_id = 0
        self.sequence = 0                       # frames recorded so far in the current file (the live edge)
        self.frames = frames                    # real Opus frames, or None for synthetic payloads
        self.clients = []
        self.file_key = os.urandom(32)
        self.nonce = os.urandom(16)
        self.tick = 0.002
        self.drops_before_closing = 1           # how many times the link must drop before a paused file is closed

    def frame(self, sequence):
        if self.frames:
            return self.frames[sequence % len(self.frames)].ljust(160, b'\0')
        return bytes([sequence & 255]) * 159 + b'\x01'


def press(world, state):
    """Recording control, whether by command or by the button on the recorder."""
    if state == 1 and world.record == 0:
        world.file_id = world.file_id + 1000 if world.file_id else 1794514851
        world.sequence = 0
    world.record = state


def recorder(world):
    class Recorder:
        def __init__(self, device, *, disconnected_callback, **kwargs):
            self.is_connected = False
            self.lost = disconnected_callback
            self.commands = []
            self.requests = []
            self.session_key = None
            self.feeder = None
            self.open_file = None                   # the file whose realtime transfer is open on this connection
            world.clients.append(self)

        async def connect(self):
            self.is_connected = True

        async def disconnect(self):
            self.drop()

        def drop(self):
            """The link goes away: out of range, or closed from this side."""
            if self.is_connected:
                self.is_connected = False
                self.stop_feed()
                if world.record == 2:               # a paused recording is finalised once the link is gone,
                    world.drops_before_closing -= 1     # though not always the first time
                    if world.drops_before_closing <= 0:
                        world.record = 0
                self.lost(self)

        async def start_notify(self, uuid, callback):
            self.callback = callback

        def send(self, kind, ident, payload=b''):
            if self.is_connected:
                self.callback(None, packet(kind, ident, payload))

        def stop_feed(self):
            if self.feeder:
                self.feeder.cancel()
                self.feeder = None

        async def write_gatt_char(self, uuid, data, **kwargs):
            kind, ident, payload = data[5], data[6], bytes(data[9:-1])
            self.commands.append((kind, ident, payload))
            if (kind, ident) == (1, 1):
                info = bytearray(51)
                info[1] = 9
                info[3:8] = b'04.92'
                info[8:24] = b'ABC1234567890123'
                info[50] = world.record
                self.send(kind, ident, bytes(info))
            elif (kind, ident) == (0x1b, 0x0e):
                live = struct.pack('<II', world.file_id, 1) if world.record else bytes(8)
                self.send(kind, ident, struct.pack('<H', 0) + live)
            elif (kind, ident) == (0x2e, 1):
                private = ec.generate_private_key(ec.SECP256R1())
                shared = private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), payload))
                self.session_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b'\x01\x02\x03', info=b'\x01\x02\x03').derive(shared)
                public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
                self.send(kind, ident, public + shared)
            elif (kind, ident) == (0x18, 0x82):
                press(world, payload[0])
                self.send(kind, ident, bytes([payload[0], data[-1]]))     # state + echo of the command checksum
            elif (kind, ident) == (0x1a, 7):
                offset, file_id, flag = struct.unpack('<IIB', payload)
                self.requests.append((offset, file_id, flag))
                session_nonce = os.urandom(16)
                sealed = decrypt(b'soundcored3200' + world.file_key, self.session_key, session_nonce)
                refused = self.open_file == file_id and self.feeder is not None       # already being transferred
                self.send(0x1a, 7, struct.pack('<II', file_id, 0xffffffff) + world.nonce + sealed + session_nonce + (b'\x04' if refused else b'\0'))
                if not refused:
                    self.stop_feed()
                    self.open_file = file_id
                    self.feeder = asyncio.ensure_future(self.feed(file_id, offset // 160 if offset else world.sequence))

        async def feed(self, file_id, sequence):
            while self.is_connected and world.file_id == file_id and world.record != 0:
                if world.record == 2:                                   # paused: the transfer stays open and waits
                    await asyncio.sleep(world.tick)
                    continue
                sequence = max(sequence, world.sequence)                # always the live edge
                body = b''
                for _ in range(3):                                      # the recorder sends three slices per packet
                    world.sequence = max(world.sequence, sequence + 1)
                    sealed = decrypt(world.frame(sequence), world.file_key, world.nonce[:12] + (sequence * 10).to_bytes(4, 'big'))
                    body += struct.pack('<I', sequence) + b'\0' + sealed + b'\0'
                    sequence += 1
                self.send(0x1a, 0x12, body)
                await asyncio.sleep(world.tick)

    return Recorder


class Scanner:
    @staticmethod
    async def find_device_by_address(address, **kwargs):
        return SimpleNamespace(address=address, name='soundcore Work')


def device_for(world):
    return FeishuDevice(ble_loader=lambda: (recorder(world), Scanner), reconnect_delays=(.01,))


async def connected(world):
    device = device_for(world)
    await device.connect('AA')
    return device


async def until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, 'condition was not reached in time'
        await asyncio.sleep(0.005)


def live_stream(device, frames, stop, stats, resume=None, **options):
    options.setdefault('probe_interval', .05)
    return asyncio.create_task(stream_live(device, lambda sequence, data: frames.append(sequence), stop, stats, resume, **options))


# ---- the stream itself ------------------------------------------------------ #

@pytest.mark.asyncio
async def test_stream_starts_recording_owns_the_link_and_pauses_on_stop():
    world = World()
    device = await connected(world)
    frames, stats, stop = [], {}, asyncio.Event()
    task = live_stream(device, frames, stop, stats)
    await until(lambda: len(frames) >= 30)
    assert world.record == 1 and world.clients[-1].requests == [(0, world.file_id, 1)]
    assert device.snapshot()['live'] == {'status': 'streaming', 'file_id': world.file_id}
    with pytest.raises(ValueError, match='实时输入正在进行'):
        await device.refresh()                              # refused at once, not queued behind hours of audio
    assert device.snapshot()['connected']                   # and the refusal did not cost the link
    stop.set()
    first = world.clients[-1]
    assert await asyncio.wait_for(task, 2) == 'stopped'
    assert first.commands[-1] == (0x18, 0x82, b'\x02')     # paused, never left recording
    # The transfer cannot be closed by command, so the link is dropped: the recorder then
    # closes the transfer and finalises the recording, and the supervisor reconnects.
    assert not first.is_connected and world.record == 0
    assert device.exclusive is None and 'live' not in device.snapshot()
    assert frames == list(range(frames[0], frames[0] + len(frames))) and stats['lost_frames'] == 0
    await until(lambda: device.snapshot()['connected'])
    await device.refresh()                                  # ordinary operations work again
    await device.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('announced', [True, False])
async def test_recording_ended_on_the_recorder_ends_the_stream_without_a_pause_command(announced):
    world = World()
    device = await connected(world)
    frames, stats, stop = [], {}, asyncio.Event()
    task = live_stream(device, frames, stop, stats)
    await until(lambda: len(frames) >= 30)
    press(world, 0)
    if announced:
        world.clients[-1].send(0x1a, 0x0a)
    assert await asyncio.wait_for(task, 2) == 'device_stopped'
    assert (0x18, 0x82, b'\x02') not in world.clients[-1].commands
    assert device.snapshot()['connected'] and device.exclusive is None
    await device.disconnect()


@pytest.mark.asyncio
async def test_pause_on_the_recorder_waits_without_timing_out_and_resumes_where_it_stopped():
    world = World()
    device = await connected(world)
    frames, stats, stop = [], {}, asyncio.Event()
    task = live_stream(device, frames, stop, stats, idle_timeout=.1)
    await until(lambda: len(frames) >= 30)
    press(world, 2)
    await until(lambda: stats['paused'])
    assert device.snapshot()['live']['status'] == 'paused'
    await asyncio.sleep(.4)                                 # four idle timeouts: a pause is not a failure
    assert not task.done()
    heard = len(frames)
    press(world, 1)
    await until(lambda: len(frames) >= heard + 30)
    # The open transfer simply continues. Asking again would be refused with error 4.
    assert world.clients[-1].requests == [(0, world.file_id, 1)]
    assert frames == list(range(frames[0], frames[0] + len(frames))) and stats['paused'] is False
    assert device.snapshot()['live']['status'] == 'streaming'
    stop.set()
    assert await asyncio.wait_for(task, 2) == 'stopped'
    await device.disconnect()


@pytest.mark.asyncio
async def test_dropped_link_fails_the_stream_and_the_next_one_resumes_after_the_reconnect():
    world = World()
    device = await connected(world)
    frames, resume, stop = [], {}, asyncio.Event()
    task = live_stream(device, frames, stop, {}, resume)
    await until(lambda: len(frames) >= 30)
    world.clients[-1].drop()
    with pytest.raises(ValueError, match='蓝牙连接已中断'):
        await asyncio.wait_for(task, 2)
    assert device.exclusive is None
    await until(lambda: device.snapshot()['connected'])    # the session supervisor reconnects by itself
    assert world.record == 1                                # the recorder kept recording meanwhile
    last = frames[-1]
    task = live_stream(device, frames, stop, {}, resume)
    await until(lambda: len(frames) >= 80)
    assert world.clients[-1].requests[0] == (last * 160, world.file_id, 1)
    assert frames == sorted(set(frames))
    stop.set()
    await asyncio.wait_for(task, 2)
    await device.disconnect()


@pytest.mark.asyncio
async def test_stream_follows_the_recorder_to_a_new_file_without_a_gap():
    # Measured on 04.92: after six hours the recorder opens a new file, says so with 1A 06,
    # and simply stops sending the old one. There is no 1A 0A.
    world = World()
    device = await connected(world)
    frames, stats, stop = [], {}, asyncio.Event()
    files = []
    task = asyncio.create_task(stream_live(device, lambda sequence, data: (frames.append(sequence), files.append(stats['file_id'])),
                                           stop, stats, probe_interval=5, idle_timeout=30))
    await until(lambda: len(frames) >= 30)
    client, first = world.clients[-1], world.file_id
    client.stop_feed()
    world.file_id, world.sequence = first + 21600, 0
    client.send(0x1a, 6, struct.pack('<I', world.file_id) + bytes(5))
    await until(lambda: len(client.requests) == 2, 2)        # well before any idle timeout could notice
    assert client.requests[1] == (0, first + 21600, 1)
    await until(lambda: files and files[-1] == first + 21600 and files.count(first + 21600) >= 30)
    assert stats['stalls'] == 0 and stats['file_id'] == first + 21600
    client.send(0x1a, 6, struct.pack('<I', world.file_id) + bytes(5))    # the same file announced again changes nothing
    await asyncio.sleep(.05)
    assert len(client.requests) == 2
    stop.set()
    assert await asyncio.wait_for(task, 2) == 'stopped'
    await device.disconnect()


@pytest.mark.asyncio
async def test_disconnecting_the_recorder_ends_the_stream_instead_of_hanging():
    world = World()
    device = await connected(world)
    frames, stop = [], asyncio.Event()
    task = live_stream(device, frames, stop, {})
    await until(lambda: len(frames) >= 30)
    leaving = asyncio.create_task(device.disconnect())
    assert await asyncio.wait_for(task, 2) == 'disconnect_requested'
    await asyncio.wait_for(leaving, 2)
    assert (0x18, 0x82, b'\x02') in world.clients[-1].commands and world.record == 0 and not device.snapshot()['connected']


@pytest.mark.asyncio
async def test_a_second_request_on_the_same_connection_is_refused_like_the_real_recorder():
    # Seen on the device: switching off without dropping the link left the transfer open, and the
    # next start was answered with "error 4". The stream must surface that, not hang or misread audio.
    world = World()
    device = await connected(world)
    frames, stop = [], asyncio.Event()
    task = live_stream(device, frames, stop, {})
    await until(lambda: len(frames) >= 30)
    client = world.clients[-1]
    await client.write_gatt_char(None, bytes(5) + bytes([0x1a, 7, 19, 0]) + struct.pack('<IIB', 0, world.file_id, 1) + b'\0')
    with pytest.raises(ValueError, match='错误码 4'):
        await asyncio.wait_for(task, 2)
    assert not client.is_connected and device.exclusive is None        # the link is reset, which closes the transfer
    await device.disconnect()


@pytest.mark.asyncio
async def test_record_control_accepts_the_two_byte_reply_of_the_real_recorder():
    world = World()
    device = await connected(world)
    assert (await device.record('start'))['recordStatus'] == 1
    assert (await device.record('pause'))['recordStatus'] == 2
    assert device.snapshot()['connected']                   # an exact-match check used to drop the link here
    await device.disconnect()


# ---- the switch --------------------------------------------------------------- #

@pytest.fixture(scope='module')
def opus():
    """Six seconds of 0.5 s tone / 1 s silence as 20 ms Opus frames, the format the recorder sends."""
    return opus_frames(tone=0.5, period=1.5, seconds=6)


def opus_frames(tone, period, seconds):
    av = pytest.importorskip('av')
    np = pytest.importorskip('numpy')
    try:
        encoder = av.CodecContext.create('libopus', 'w')
    except Exception:
        pytest.skip('this PyAV build has no Opus encoder')
    encoder.sample_rate, encoder.layout, encoder.format, encoder.bit_rate = 16000, 'mono', 's16', 32000
    encoder.options = {'frame_duration': '20', 'application': 'voip'}
    encoder.open()
    t = np.arange(int(16000 * seconds)) / 16000
    pcm = (np.sin(2 * np.pi * 440 * t) * 8000 * (t % period < tone)).astype('<i2')
    frames = []
    for start in range(0, len(pcm), 320):
        frame = av.AudioFrame.from_ndarray(pcm[start:start + 320].reshape(1, -1), format='s16', layout='mono')
        frame.sample_rate, frame.pts = 16000, start
        frames += [bytes(item) for item in encoder.encode(frame)]
    assert frames and max(map(len, frames)) <= 160
    return frames


class Bridge:
    rate = 48000
    opened = []

    def __init__(self, name):
        if name == 'missing':
            raise ValueError('没有找到名为“missing”的输出设备。请刷新设备列表后重新选择。')
        self.name, self.samples, self.closed, self.active = name, 0, False, True
        Bridge.opened.append(self)

    def start(self):
        pass

    def push(self, mono):
        self.samples += len(mono)

    def stats(self):
        return dict(name=self.name, sample_rate=self.rate, queued_ms=0, underflows=0, dropped_seconds=0.0,
                    played_seconds=round(self.samples / self.rate, 1), peak=0.0, active=self.active)

    def close(self):
        self.closed = True


def switch(device, realtime, settle_seconds=0, transcripts=None):
    return LiveInput(device, realtime, bridge_factory=Bridge, retry_delays=(.01,), settle_seconds=settle_seconds,
                     stream=functools.partial(stream_live, probe_interval=.05), transcripts=transcripts)


@pytest.mark.asyncio
async def test_switch_sends_audio_to_the_sound_card_and_text_to_the_page(opus):
    world = World(opus)
    device = await connected(world)
    realtime = RealtimeASR(fake_engine=True)
    live = switch(device, realtime)
    try:
        state = await live.start('虚拟声卡', True, None, 'zh')
        assert state['enabled'] and state['options'] == dict(output='虚拟声卡', transcribe=True, model='fake', language='zh', speakers=True)
        await until(lambda: live.snapshot()['status'] == 'streaming')
        await until(lambda: Bridge.opened[-1].samples > 48000 and live.snapshot()['transcript']['next_cursor'] >= 2, 20)
        view = live.snapshot(0)
        assert view['device']['record_status'] == 1 and view['audio']['lost_frames'] == 0 and view['audio']['decode_errors'] == 0
        assert view['output']['name'] == '虚拟声卡' and view['output']['active']
        assert all(row['text'].startswith('语音') for row in view['transcript']['segments'])
        assert live.snapshot(1)['transcript']['segments'][0]['index'] == 1       # a cursor returns only what is new
        with pytest.raises(ValueError, match='已经开启'):
            await live.start('虚拟声卡', False)
        state = await live.stop()
        assert state['status'] == 'off' and not state['enabled'] and world.record == 0 and Bridge.opened[-1].closed
        assert state['transcript']['next_cursor'] >= view['transcript']['next_cursor']   # the text stays readable
        assert device.exclusive is None
        # Switched on again straight away: a fresh link, a new recording, no refusal and nothing lost.
        again = await live.start('虚拟声卡', False)
        assert again['enabled']
        await until(lambda: live.snapshot()['status'] == 'streaming')
        assert live.snapshot()['audio']['retries'] == 0 and live.snapshot()['audio']['lost_frames'] == 0
        assert len(world.clients) == 2 and world.clients[-1].requests == [(0, world.file_id, 1)]
    finally:
        await live.stop()
        await realtime.close()
        await device.disconnect()


@pytest.mark.asyncio
async def test_page_text_can_be_turned_on_and_off_while_the_sound_keeps_flowing(opus):
    world = World(opus)
    device = await connected(world)
    realtime = RealtimeASR(fake_engine=True)
    live = switch(device, realtime)
    try:
        with pytest.raises(ValueError, match='没有开启'):
            await live.set_transcribe(True)
        await live.start('虚拟声卡', False)
        await until(lambda: live.snapshot()['status'] == 'streaming')
        assert live.snapshot()['transcript']['on'] is False
        bridge = Bridge.opened[-1]
        state = await live.set_transcribe(True)
        assert state['transcript']['on'] and state['options']['transcribe'] and state['options']['model'] == 'fake'
        await until(lambda: live.snapshot()['transcript']['next_cursor'] >= 2, 20)
        heard, played = live.snapshot()['transcript']['next_cursor'], bridge.samples
        state = await live.set_transcribe(False)
        assert state['transcript']['on'] is False and state['options']['transcribe'] is False
        assert len(live.snapshot(0)['transcript']['segments']) >= heard            # what was said stays on the page
        await until(lambda: bridge.samples > played + 48000)                         # the sound never stopped
        assert live.snapshot()['status'] == 'streaming' and live.snapshot()['audio']['retries'] == 0 and not bridge.closed
        assert len(world.clients[-1].requests) == 1
    finally:
        await live.stop()
        await realtime.close()
        await device.disconnect()


# ---- the text of each session, kept in its own file ------------------------- #

def moment(text):
    return time.mktime(time.strptime(text, '%Y-%m-%d %H:%M:%S'))


def test_every_session_gets_its_own_file_and_none_is_ever_written_over(tmp_path):
    log = TranscriptLog(tmp_path / 'live-transcripts')
    began = moment('2026-11-13 20:33:22')
    log.begin('soundcore Work', 'sensevoice-small', began)
    assert log.sessions() == [] and log.current()['file'] is None        # nothing said yet: no empty file
    log.write(began + 5, '第一次开启说的第一句。')
    log.write(began + 9, '第一次开启说的第二句。')
    first = tmp_path / 'live-transcripts' / '2026-11-13_20-33-22.txt'
    assert log.current() == dict(started=began, open=True, saving=True, file=first.name, sentences=2, characters=22, error=None)
    log.end()
    kept = first.read_text(encoding='utf-8')
    assert kept.startswith('实时输入文字记录\n开始：2026-11-13 20:33:22\n设备：soundcore Work\n模型：sensevoice-small\n\n—— 2026-11-13 ——\n')
    assert '[20:33:27] 第一次开启说的第一句。\n[20:33:31] 第一次开启说的第二句。\n' in kept and '共 2 句，22 字' in kept
    # Switched on again within the same second: a second file, never the first one again.
    log.begin('soundcore Work', 'sensevoice-small', began + 0.4)
    log.write(began + 1, '第二次开启。')
    second = tmp_path / 'live-transcripts' / '2026-11-13_20-33-22-2.txt'
    assert second.is_file() and first.read_text(encoding='utf-8') == kept
    assert '第二次开启。' in second.read_text(encoding='utf-8') and '第一次' not in second.read_text(encoding='utf-8')
    rows = log.sessions()
    assert [(row['name'], row['active'], row['sentences'], row['characters']) for row in rows] == [
        (second.name, True, 1, 6), (first.name, False, 2, 22)]
    assert rows[1]['started'] == began and rows[1]['bytes'] == first.stat().st_size
    log.write(began + 2, '还在第二次。')
    assert log.sessions()[0]['sentences'] == 2 and first.read_text(encoding='utf-8') == kept
    log.end()
    assert [row['active'] for row in log.sessions()] == [False, False]
    assert log.current()['file'] == second.name and not log.current()['open']    # still pointed at after it ends
    log.write(began + 3, '关闭以后到的句子不算。')
    assert log.sessions()[0]['sentences'] == 2


def test_the_file_keeps_every_sentence_of_a_session_that_runs_for_days(tmp_path):
    log = TranscriptLog(tmp_path)
    began = moment('2026-11-13 23:59:50')
    log.begin('', None, began)
    total = KEPT_SEGMENTS * 3
    for number in range(total):                       # four days, far more than the page keeps
        log.write(began + number * 23, f'第 {number} 句\n带换行')
    log.end()
    lines = (tmp_path / '2026-11-13_23-59-50.txt').read_text(encoding='utf-8').splitlines()
    said = [line for line in lines if line.startswith('[')]
    assert len(said) == total and said[0] == '[23:59:50] 第 0 句 带换行' and said[-1].endswith(f'第 {total - 1} 句 带换行')
    days = [line for line in lines if line.startswith('——')]
    assert days[:2] == ['—— 2026-11-13 ——', '—— 2026-11-14 ——'] and len(days) == 5 and days == sorted(days)
    assert lines[2:4] == ['设备：录音豆', '模型：本机模型']
    assert log.sessions()[0]['sentences'] == total and log.current()['sentences'] == total


def test_a_file_that_cannot_be_written_is_reported_and_never_stops_the_text(tmp_path):
    blocked = tmp_path / 'not-a-folder'
    blocked.write_text('x')
    log = TranscriptLog(blocked)
    log.begin('', '', moment('2026-11-13 08:00:00'))
    log.write(moment('2026-11-13 08:00:05'), '照常显示。')
    state = log.current()
    assert state['sentences'] == 1 and state['file'] is None and '没有写进文件' in state['error']
    assert log.sessions() == []
    with pytest.raises(KeyError):
        log.file('2026-11-13_08-00-00.txt')
    off = TranscriptLog(None)                          # no folder configured: counts only
    off.begin('', '', 0)
    off.write(5, '一句')
    assert off.current()['saving'] is False and off.current()['sentences'] == 1 and off.sessions() == []


def test_only_transcript_files_can_be_listed_or_opened(tmp_path):
    (tmp_path / 'secret.txt').write_text('不是文字记录')
    (tmp_path / '2026-11-13_20-33-22.txt.bak').write_text('也不是')
    (tmp_path / '2026-11-12_09-00-00.txt').write_text('实时输入文字记录\n\n—— 2026-11-12 ——\n[09:00:03] 昨天的一句。\n', encoding='utf-8')
    log = TranscriptLog(tmp_path)
    assert [(row['name'], row['sentences'], row['characters']) for row in log.sessions()] == [('2026-11-12_09-00-00.txt', 1, 6)]
    assert log.file('2026-11-12_09-00-00.txt') == tmp_path / '2026-11-12_09-00-00.txt'
    for name in ('secret.txt', '../2026-11-12_09-00-00.txt', '2026-11-13_20-33-22.txt.bak', '2026-11-13_00-00-00.txt', ''):
        with pytest.raises(KeyError):
            log.file(name)


def test_a_record_can_be_read_back_by_the_hour_and_searched(tmp_path):
    log = TranscriptLog(tmp_path)
    began = moment('2026-11-13 22:58:00')
    log.begin('soundcore Work', 'sensevoice-small', began)
    said = []
    for number in range(200):                    # one sentence a minute, 22:58 on the 13th to 02:17 on the 14th
        said.append(f'第{number}句，Bluetooth 抓包 analysis。' if number % 50 == 0 else f'第{number}句，讨论发布计划。')
        log.write(began + number * 60, said[-1])
    name = log.current()['file']
    view = log.outline(name)
    assert view['active'] and view['ended'] is None and view['started'] == '2026-11-13 22:58:00'
    assert view['sentences'] == 200 and view['characters'] == sum(map(len, said))
    assert view['device'] == 'soundcore Work' and view['model'] == 'sensevoice-small'
    assert [(hour['day'], hour['hour'], hour['first'], hour['sentences']) for hour in view['hours']] == [
        ('2026-11-13', 22, 0, 2), ('2026-11-13', 23, 2, 60), ('2026-11-14', 0, 62, 60), ('2026-11-14', 1, 122, 60), ('2026-11-14', 2, 182, 18)]
    assert sum(hour['characters'] for hour in view['hours']) == view['characters']
    midnight = view['hours'][2]
    page = log.lines(name, midnight['first'], midnight['sentences'])
    assert page['total'] == 200 and len(page['lines']) == 60
    assert page['lines'][0] == dict(index=62, day='2026-11-14', time='00:00:00', text=said[62], speaker=None)
    assert page['lines'][-1]['index'] == 121 and page['lines'][-1]['time'] == '00:59:00'
    assert log.lines(name, 195, 50)['lines'][-1]['index'] == 199 and log.lines(name, 500, 10)['lines'] == []
    found = log.search(name, 'bluetooth 抓包')                      # every word, in any case
    assert found['total'] == 4 and [match['index'] for match in found['matches']] == [0, 50, 100, 150]
    assert found['terms'] == ['bluetooth', '抓包']
    assert found['matches'][2] == dict(index=100, day='2026-11-14', time='00:38:00', text=said[100], speaker=None)
    few = log.search(name, '发布', limit=3)
    assert few['total'] == 196 and [match['index'] for match in few['matches']] == [1, 2, 3]
    assert log.search(name, '没有说过的词') == dict(query='没有说过的词', terms=['没有说过的词'], total=0, matches=[])
    with pytest.raises(ValueError, match='请输入'):
        log.search(name, '   ')
    # What is appended after a read is there on the next read; a finished record says when it ended.
    log.write(began + 200 * 60, '最后补的一句。')
    assert log.outline(name)['sentences'] == 201 and log.outline(name)['hours'][-1]['sentences'] == 19
    log.end()
    done = log.outline(name)
    assert not done['active'] and done['ended'] is not None and done['seconds'] is not None
    for reader in (log.outline, log.lines, lambda missing: log.search(missing, '发布')):
        with pytest.raises(KeyError):
            reader('2026-11-13_00-00-00.txt')


def test_a_record_written_by_hand_or_cut_short_is_still_readable(tmp_path):
    (tmp_path / '2026-11-12_09-00-00.txt').write_text('[09:00:03] 没有文件头的一句。\n随手加的一行备注\n[09:00:09] 第二句。\n', encoding='utf-8')
    view = TranscriptLog(tmp_path).outline('2026-11-12_09-00-00.txt')
    assert view['started'] == '2026-11-12 09:00:00' and view['ended'] == '2026-11-12 09:00:09' and view['seconds'] == 9
    assert view['sentences'] == 2 and view['hours'] == [dict(day='2026-11-12', hour=9, first=0, sentences=2, characters=13)]


# ---- who is speaking ---------------------------------------------------------- #

def voice(*parts):
    """A made-up voiceprint: the same direction is the same voice."""
    return [float(value) for value in parts]


def test_a_sentence_is_told_by_its_voice_when_it_can_be_and_is_otherwise_whoever_spoke_last():
    book = Voices()
    anna, ben, nobody = voice(1, 0, 0, 0), voice(0, 1, 0, 0), voice(0, 0, 0, 1)
    assert book.hear(None, 0.6) == 1 and book.hear(nobody, 1.2) == 1 and len(book) == 0    # before anyone is told: speaker 1
    assert book.hear(anna, 4.0) == 1 and book.hear(ben, 5.0) == 2 and len(book) == 2       # full sentences: by voice
    assert book.hear(voice(0.95, 0.2, 0.1, 0), 3.0) == 1                # a known voice again, a little different each time
    assert book.hear(ben, 1.2) == 2                                     # a short sentence in Ben's voice: Ben, although Anna spoke last
    assert book.hear(nobody, 1.2) == 2                                  # short and like nobody: whoever spoke last
    assert book.hear(None, 0.4) == 2 and book.hear(anna, 0.5) == 2      # too short for the voice to be looked at: whoever spoke last
    assert book.hear(anna, 1.0) == 1                                    # short, but clearly her voice
    assert book.hear(nobody, 6.0, may_open=False) == 1 and len(book) == 2   # hardly any words: never a new speaker
    assert book.hear(nobody, 3.0) == 3 and len(book) == 3               # a full sentence in a third voice: a third speaker
    for nothing in ([], voice(0, 0, 0, 0), [float('nan'), 1.0, 0.0, 0.0]):
        assert book.hear(nothing, 5.0) == 3                             # an unusable voiceprint: whoever spoke last


def test_a_voice_is_shaped_by_its_full_sentences_only():
    book = Voices()
    assert book.hear(voice(1, 0), 5.0) == 1
    for _ in range(20):                                        # many short, slightly off sentences change nothing
        assert book.hear(voice(0.75, 0.66), 1.0) == 1
    assert book.hear(voice(0.5, 0.87), 5.0) == 2               # 0.50 to the first voice: a different person
    assert book.hear(voice(0.6, 0.8), 5.0) == 2                # close to the second voice


def test_a_noise_is_not_a_sentence_and_a_word_or_two_goes_to_whoever_spoke_last():
    live = LiveInput(None, None)
    asr = dict(cursor=0, speakers=True)
    knock, person, other = voice(1, 0, 0), voice(0, 1, 0), voice(0, 0, 1)
    live._absorb(asr, dict(segments=[
        dict(text='。', start=0, end=3, voice=knock),                   # a knock the model wrote as a bare full stop: dropped
        dict(text=' …… ', start=3, end=6, voice=knock),
        dict(text='嗯。', start=6, end=9, voice=knock),                  # one word before anyone has spoken: speaker 1
        dict(text='喂喂喂，现在开始识别说话人了是吧？', start=9, end=14, voice=person),
        dict(text='OK', start=14, end=17, voice=other),                 # two letters in another voice: too little, so whoever spoke last
        dict(text='这是另一个人说的一整句话。', start=17, end=21, voice=other),
        dict(text='嗯。', start=21, end=22, voice=person),               # a short word in the first voice: told by its voice
    ], segment_count=7))
    assert [(row['text'], row['speaker']) for row in live.segments] == [
        ('嗯。', 1), ('喂喂喂，现在开始识别说话人了是吧？', 1), ('OK', 1), ('这是另一个人说的一整句话。', 2), ('嗯。', 1)]
    assert len(live.voices) == 2 and live.next_index == 5 and asr['cursor'] == 7


def test_speaker_numbers_are_written_in_front_of_the_words_and_read_back_apart_from_them(tmp_path):
    log = TranscriptLog(tmp_path)
    began = moment('2026-11-13 23:10:00')
    log.begin('soundcore Work', 'sensevoice-small', began)
    log.write(began + 5, '我们先看一下进度。', 1)
    log.write(began + 9, '好的，没问题。', 2)
    log.write(began + 12, '嗯。')                                 # a line without a number, as older records have them
    log.write(began + 20, '那就这么定了。', 1)
    assert log.current()['characters'] == 9 + 7 + 2 + 7          # the words only, not the labels
    name = log.current()['file']
    text = (tmp_path / name).read_text(encoding='utf-8')
    assert '[23:10:05] 说话人1：我们先看一下进度。\n[23:10:09] 说话人2：好的，没问题。\n[23:10:12] 嗯。\n[23:10:20] 说话人1：那就这么定了。\n' in text
    view = log.outline(name)
    assert view['speakers'] == 2 and view['sentences'] == 4 and view['characters'] == 25
    rows = log.lines(name, 0, 10)['lines']
    assert [(row['speaker'], row['text']) for row in rows] == [(1, '我们先看一下进度。'), (2, '好的，没问题。'), (None, '嗯。'), (1, '那就这么定了。')]
    found = log.search(name, '没问题')                              # the label is not part of what is searched
    assert found['total'] == 1 and found['matches'][0]['speaker'] == 2 and found['matches'][0]['text'] == '好的，没问题。'
    assert log.search(name, '说话人')['total'] == 0
    log.end()
    assert log.sessions()[0]['characters'] == 25 and log.sessions()[0]['sentences'] == 4


@pytest.fixture(scope='module')
def long_opus():
    """Three-second tones with a pause between them: sentences long enough to carry a voice."""
    return opus_frames(tone=3.0, period=4.2, seconds=8.4)


@pytest.mark.asyncio
async def test_realtime_text_gets_speaker_numbers_that_survive_a_model_restart_and_restart_with_a_new_record(long_opus, tmp_path):
    world = World(long_opus)
    device = await connected(world)
    realtime = RealtimeASR(fake_engine=True)
    live = switch(device, realtime, transcripts=tmp_path)
    try:
        state = await live.start('虚拟声卡', True, None, 'zh')
        assert state['options']['speakers'] and state['transcript']['speakers'] == dict(on=True, count=0)
        await until(lambda: live.snapshot()['transcript']['next_cursor'] >= 2, 20)
        view = live.snapshot(0)['transcript']
        assert view['speakers'] == dict(on=True, count=1)                 # the stand-in model hears one voice
        assert all(row['speaker'] == 1 for row in view['segments'])
        path = tmp_path / view['session']['file']
        assert all('] 说话人1：语音' in line for line in path.read_text(encoding='utf-8').splitlines() if line.startswith('['))
        # Sentences in other voices, as a real model would send them; then the model restarts after a silence.
        anna, ben = voice(0, 1, 0), voice(0, 0, 1)
        live._absorb(live.asr, dict(segments=[dict(text='这是第二个人。', start=0, end=4, voice=anna), dict(text='这是第三个人。', start=4, end=9, voice=ben)],
                                    segment_count=live.asr['cursor']))
        live._asr_lost(live.asr, False, '')
        await until(lambda: live.asr and live.asr['id'], 20)              # woken again by new audio
        live._absorb(live.asr, dict(segments=[dict(text='第二个人又说话了。', start=0, end=5, voice=anna)], segment_count=live.asr['cursor']))
        rows = {row['text']: row['speaker'] for row in live.snapshot(0)['transcript']['segments']}
        assert (rows['这是第二个人。'], rows['这是第三个人。'], rows['第二个人又说话了。']) == (2, 3, 2)
        assert live.snapshot()['transcript']['speakers']['count'] == 3
        await live.stop()
        assert '] 说话人2：第二个人又说话了。' in path.read_text(encoding='utf-8')
        # A new record numbers its speakers from 1 again; without the option there are no numbers at all.
        again = await live.start('虚拟声卡', True, None, 'zh', speakers=False)
        assert again['options']['speakers'] is False and again['transcript']['speakers'] == dict(on=False, count=0)
        await until(lambda: live.snapshot()['transcript']['next_cursor'] > again['transcript']['next_cursor'], 20)
        assert all(row['speaker'] is None for row in live.snapshot(0)['transcript']['segments'])
        await live.stop()
        other = tmp_path / live.snapshot()['transcript']['session']['file']
        assert other != path and '说话人' not in other.read_text(encoding='utf-8')
    finally:
        await live.stop()
        await realtime.close()
        await device.disconnect()


@pytest.mark.asyncio
async def test_each_time_it_is_switched_on_the_text_goes_to_a_new_file_and_the_page_starts_empty(opus, tmp_path):
    world = World(opus)
    device = await connected(world)
    realtime = RealtimeASR(fake_engine=True)
    live = switch(device, realtime, transcripts=tmp_path / 'live-transcripts')
    said = lambda path: [line for line in path.read_text(encoding='utf-8').splitlines() if line.startswith('[')]
    try:
        first = await live.start('虚拟声卡', True, None, 'zh')
        assert first['transcript']['session'] == dict(started=first['since'], open=True, saving=True, file=None,
                                                      sentences=0, characters=0, error=None)
        await until(lambda: live.snapshot()['transcript']['next_cursor'] >= 2, 20)
        name = live.snapshot()['transcript']['session']['file']
        path = tmp_path / 'live-transcripts' / name
        assert len(said(path)) >= 2                                       # written while it is still on
        live.clear_transcript()                                           # empties the page only
        assert live.snapshot(0)['transcript']['segments'] == [] and len(said(path)) >= 2
        ended = await live.stop()
        total = ended['transcript']['session']['sentences']
        assert total >= 2 and len(said(path)) == total and not ended['transcript']['session']['open']
        assert f'共 {total} 句' in path.read_text(encoding='utf-8')
        whole = path.read_text(encoding='utf-8')
        # On again: a new record. The page starts empty and the first file is not touched.
        again = await live.start('虚拟声卡', True, None, 'zh')
        assert again['transcript']['segments'] == [] and again['transcript']['session']['started'] != first['since']
        assert again['transcript']['session']['sentences'] == 0 and again['transcript']['next_cursor'] >= total
        await until(lambda: live.snapshot()['transcript']['session']['file'] not in (None, name), 20)
        await live.stop()
        other = tmp_path / 'live-transcripts' / live.snapshot()['transcript']['session']['file']
        assert other != path and path.read_text(encoding='utf-8') == whole and said(other)
        assert sorted(row['name'] for row in live.log.sessions()) == sorted([path.name, other.name])
    finally:
        await live.stop()
        await realtime.close()
        await device.disconnect()


@pytest.mark.asyncio
async def test_the_page_keeps_the_newest_sentences_and_the_file_keeps_them_all(tmp_path):
    world = World()
    device = await connected(world)
    realtime = RealtimeASR(fake_engine=True)
    live = switch(device, realtime, transcripts=tmp_path)
    try:
        await live.start('虚拟声卡', True, None, 'zh')
        total = KEPT_SEGMENTS + 700
        live._absorb(live.asr, dict(segments=[dict(text=f'第 {number} 句') for number in range(total)], segment_count=live.asr['cursor']))
        view = live.snapshot(0)['transcript']
        assert view['kept'] == KEPT_SEGMENTS and len(view['segments']) == KEPT_SEGMENTS
        assert view['segments'][-1]['text'] == f'第 {total - 1} 句' and view['session']['sentences'] >= total
        lines = (tmp_path / view['session']['file']).read_text(encoding='utf-8').splitlines()
        assert sum(1 for line in lines if line.endswith('第 0 句')) == 1 and sum(1 for line in lines if line.startswith('[')) >= total
    finally:
        await live.stop()
        await realtime.close()
        await device.disconnect()


@pytest.mark.asyncio
async def test_switch_survives_a_dropped_link_and_turns_off_when_recording_ends_on_the_recorder(opus):
    world = World(opus)
    device = await connected(world)
    realtime = RealtimeASR(fake_engine=True)
    live = switch(device, realtime)
    try:
        await live.start('虚拟声卡', False)
        await until(lambda: live.snapshot()['status'] == 'streaming')
        world.clients[-1].drop()
        await until(lambda: live.snapshot()['audio']['retries'] >= 1)
        assert live.snapshot()['enabled'] and '中断' in live.snapshot()['error']
        await until(lambda: live.snapshot()['status'] == 'streaming')            # back on without anyone asking
        assert live.snapshot()['error'] is None
        press(world, 0)
        await until(lambda: not live.snapshot()['enabled'])
        await until(lambda: Bridge.opened[-1].closed)
        assert '结束录音' in live.snapshot()['notice'] and live.snapshot()['status'] == 'off'
        assert (0x18, 0x82, b'\x02') not in world.clients[-1].commands
    finally:
        await live.stop()
        await realtime.close()
        await device.disconnect()


@pytest.mark.asyncio
async def test_switching_off_keeps_the_link_down_until_the_recording_is_closed_unless_switched_on_again():
    world = World()
    device = await connected(world)
    realtime = RealtimeASR(fake_engine=True)
    live = switch(device, realtime, settle_seconds=.4)
    try:
        await live.start('虚拟声卡', False)
        await until(lambda: live.snapshot()['status'] == 'streaming')
        await live.stop()
        await asyncio.sleep(.2)
        assert not device.snapshot()['connected'] and len(world.clients) == 1     # no reconnect yet: the file is closing
        assert live.snapshot()['device']['connection_status'] == 'reconnecting'
        await until(lambda: device.snapshot()['connected'], 3)                    # then the supervisor brings it back
        await live.start('虚拟声卡', False)
        await until(lambda: live.snapshot()['status'] == 'streaming')
        await live.stop()
        await asyncio.sleep(.05)
        await live.start('虚拟声卡', False)                                        # at once: does not wait out the hold
        await until(lambda: live.snapshot()['status'] == 'streaming', 3)
        assert live.snapshot()['audio']['retries'] == 0
    finally:
        await live.stop()
        await live._end_settling()
        await realtime.close()
        await device.disconnect()


@pytest.mark.asyncio
async def test_switching_off_drops_the_link_again_when_the_recording_is_still_paused_after_reconnecting():
    world = World()
    world.drops_before_closing = 2
    device = await connected(world)
    realtime = RealtimeASR(fake_engine=True)
    live = switch(device, realtime, settle_seconds=.1)
    try:
        await live.start('虚拟声卡', False)
        await until(lambda: live.snapshot()['status'] == 'streaming')
        await live.stop()
        await until(lambda: live.settling.done(), 6)
        assert world.record == 0 and len(world.clients) == 3          # two drops, then a link that stays up
        assert device.snapshot()['connected'] and device.snapshot()['recordStatus'] == 0
        await live.start('虚拟声卡', False)                            # the next session reports nothing lost
        await until(lambda: live.snapshot()['status'] == 'streaming')
        assert live.snapshot()['audio']['lost_frames'] == 0 and live.snapshot()['audio']['retries'] == 0
    finally:
        await live.stop()
        await live._end_settling()
        await realtime.close()
        await device.disconnect()


@pytest.mark.asyncio
async def test_switch_refuses_requests_it_cannot_serve_before_touching_the_recorder():
    world = World()
    device = device_for(world)
    realtime = RealtimeASR(fake_engine=True)
    live = switch(device, realtime)
    with pytest.raises(ValueError, match='录音豆未连接'):
        await live.start('虚拟声卡', False)
    await device.connect('AA')
    with pytest.raises(ValueError, match='至少选择一个去向'):
        await live.start(None, False)
    with pytest.raises(ValueError, match='没有找到'):
        await live.start('missing', False)
    assert not live.snapshot()['enabled'] and world.record == 0
    assert all(command[:2] != (0x18, 0x82) for command in world.clients[-1].commands)
    await realtime.close()
    await device.disconnect()


# ---- HTTP API ------------------------------------------------------------------ #

class Devices:
    def __init__(self, feishu):
        self.feishu = feishu

    def snapshot(self, kind):
        return self.feishu.snapshot() if kind == 'feishu' else dict(connected=False, files=[])

    async def refresh(self, kind):
        return await self.feishu.refresh()

    async def disconnect(self, kind):
        if kind == 'feishu':
            await self.feishu.disconnect()


@pytest.mark.asyncio
async def test_api_switch_and_other_recorder_requests_fail_fast_while_it_is_on(tmp_path, monkeypatch):
    world = World()
    device = await connected(world)
    realtime = RealtimeASR(fake_engine=True)
    app = create_app(tmp_path, devices=Devices(device), realtime=realtime)
    app.state.live.bridge_factory = Bridge
    app.state.live.settle_seconds = 0
    app.state.live.stream = functools.partial(stream_live, probe_interval=.05)
    monkeypatch.setattr('backend.live.list_outputs', lambda: dict(available=True, suggested='BlackHole 2ch', message='已找到虚拟声卡。', devices=[
        dict(name='BlackHole 2ch', channels=2, sample_rate=48000, virtual=True, loopback=True, system_default=False)]))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            idle = (await client.get('/api/live')).json()
            assert idle['available'] and idle['status'] == 'off' and idle['device']['connected']
            assert (await client.get('/api/live/outputs')).json()['suggested'] == 'BlackHole 2ch'
            refused = await client.post('/api/live/start', json=dict(transcribe=False))
            assert refused.status_code == 400 and '至少选择一个去向' in refused.json()['detail']
            started = await client.post('/api/live/start', json=dict(output='BlackHole 2ch', transcribe=False))
            assert started.status_code == 200 and started.json()['enabled']
            await until(lambda: app.state.live.snapshot()['status'] == 'streaming')
            job = (await client.post('/api/jobs', json=dict(type='refresh', device_type='feishu'))).json()
            await asyncio.gather(*list(app.state.jobs.tasks.values()), return_exceptions=True)
            failed = (await client.get('/api/jobs/' + job['id'])).json()
            assert failed['status'] == 'failed' and '实时输入正在进行' in failed['error']
            assert (await client.get('/api/live')).json()['status'] == 'streaming'  # undisturbed by the refusal
            assert (await client.get('/api/devices/feishu')).json()['live']['status'] == 'streaming'
            texted = await client.post('/api/live/transcribe', json=dict(on=True))
            assert texted.status_code == 200 and texted.json()['transcript']['on']
            assert (await client.post('/api/live/transcribe', json=dict(on=False))).json()['transcript']['on'] is False
            live = app.state.live
            live._absorb(live.asr or dict(cursor=0), dict(segments=[dict(text='写进文件的一句。')], segment_count=0))
            stopped = (await client.post('/api/live/stop')).json()
            assert stopped['status'] == 'off' and world.record == 0
            listed = (await client.get('/api/live/transcripts')).json()
            assert listed['directory'] == str(tmp_path.resolve() / 'live-transcripts') and len(listed['sessions']) == 1
            name = listed['sessions'][0]['name']
            assert name == stopped['transcript']['session']['file'] and listed['sessions'][0]['sentences'] == 1
            opened = await client.get('/api/live/transcripts/' + name)
            assert opened.status_code == 200 and '写进文件的一句。' in opened.text
            assert opened.headers['content-type'].startswith('text/plain') and 'inline' in opened.headers['content-disposition']
            for wrong in ('secret.txt', '2026-01-01_00-00-00.txt', '..%2Fcatalog.sqlite3'):
                assert (await client.get('/api/live/transcripts/' + wrong)).status_code == 404
            # The reader: totals and hours first, then the sentences of one hour, and search.
            view = (await client.get(f'/api/live/transcripts/{name}/outline')).json()
            assert view['sentences'] == 1 and view['hours'][0]['first'] == 0 and not view['active']
            page = (await client.get(f'/api/live/transcripts/{name}/lines', params=dict(first=0, count=10))).json()
            assert [row['text'] for row in page['lines']] == ['写进文件的一句。']
            found = (await client.get(f'/api/live/transcripts/{name}/search', params=dict(q='文件'))).json()
            assert found['total'] == 1 and found['matches'][0]['index'] == 0
            assert (await client.get(f'/api/live/transcripts/{name}/search', params=dict(q=''))).status_code == 422
            assert (await client.get(f'/api/live/transcripts/{name}/lines', params=dict(count=5000))).status_code == 422
            shown = []
            live.reveal = shown.append
            assert (await client.post(f'/api/live/transcripts/{name}/reveal')).json() == dict(ok=True)
            assert shown == [tmp_path.resolve() / 'live-transcripts' / name]
            for part in ('outline', 'lines', 'search?q=x'):
                assert (await client.get(f'/api/live/transcripts/2026-01-01_00-00-00.txt/{part}')).status_code == 404
            assert (await client.post('/api/live/transcripts/2026-01-01_00-00-00.txt/reveal')).status_code == 404 and len(shown) == 1
            assert (await client.get(f'/live/records/{name}')).status_code in {200, 503}       # the reader page itself
            assert (await client.get('/live/records')).status_code in {200, 503}
            assert (await client.get('/live/records/a/b')).status_code == 404
    finally:
        await app.state.live.stop()
        await realtime.close()
        await device.disconnect()
