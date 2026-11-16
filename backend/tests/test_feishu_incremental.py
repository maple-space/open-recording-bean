"""Offline simulation: no test in this module may load physical Bluetooth."""
import asyncio
import hashlib
import struct
import threading
import time

import httpx
import pytest

from backend.feishu import FeishuDevice
from backend.feishu_cache import FrameCache, CaptureWriter, Source
from backend.feishu_incremental import read_range, slices
from backend.feishu_protocol import decrypt, mux_ogg, parse_info
from backend.tests.test_feishu import AudioBLE, FakeScanner, packet
from backend.tests.test_live import World, connected, live_stream, until, switch, Bridge, opus_frames, press
from backend.realtime_asr import RealtimeASR

SERIAL, IDENT = 'abc1234567890123', 123
SIGNATURE = hashlib.sha256(b'file-key-and-nonce').hexdigest()


@pytest.fixture(autouse=True)
def forbid_hardware(monkeypatch):
    def forbidden():
        pytest.fail('physical Bluetooth is forbidden in offline tests')
    monkeypatch.setattr('backend.devices._load_ble', forbidden)


def frame(seq):
    return seq.to_bytes(4, 'little') * 40


def test_cache_persists_deduplicates_and_isolates_devices(tmp_path):
    cache = FrameCache(tmp_path)
    with cache.source('A', IDENT) as source:
        assert source.put_many([(7, frame(7)), (8, frame(8))], SIGNATURE) == 2
        assert source.put_many([(7, frame(7))], SIGNATURE) == 0
        with pytest.raises(ValueError, match='使用'):
            cache.source('A', IDENT)
    with cache.source('A', IDENT) as source:
        assert source.get(7) == frame(7)
    with cache.source('B', IDENT) as other:
        assert other.get(7) is None


@pytest.mark.parametrize('failure', ['duplicate', 'signature', 'corruption', 'catalog'])
def test_integrity_conflicts_are_sticky_and_preserve_original(tmp_path, failure):
    cache = FrameCache(tmp_path)
    with cache.source('A', IDENT) as source:
        source.put_many([(7, frame(7))], SIGNATURE)
        source.bind(dict(duration_ms=200, ended_at=124))
        with pytest.raises(ValueError):
            if failure == 'duplicate':
                source.put_many([(7, frame(8))], SIGNATURE)
            elif failure == 'signature':
                source.put_many([(8, frame(8))], 'different')
            elif failure == 'corruption':
                source.db.execute('UPDATE frames SET data=? WHERE seq=7', (frame(8),))
                source.db.commit()
                source.get(7)
            else:
                source.bind(dict(duration_ms=220, ended_at=124))
    with pytest.raises(ValueError, match='冲突'):
        cache.source('A', IDENT)


def test_gap_planner_requires_base_and_tail_proof(tmp_path):
    with FrameCache(tmp_path).source('A', IDENT) as source:
        source.put_many([(n, frame(n)) for n in range(20, 100)], SIGNATURE)
        assert source.plan(100) == (0, 23, None)
        source.set_meta('base', 0)
        assert source.plan(100) == (0, 23, 0)
        source.put_many([(n, frame(n)) for n in range(20)], SIGNATURE)
        assert source.plan(100) == (96, 99, 0)
        source.set_meta('tail', 100)
        assert source.plan(100) is None


def test_gap_plan_updates_after_each_commit_and_after_reopen(tmp_path):
    cache = FrameCache(tmp_path)
    known = {n for n in range(1, 501) if n % 17 not in (0, 1)}
    with cache.source(SERIAL, IDENT) as source:
        source.put_many([(n, frame(n)) for n in sorted(known)], SIGNATURE)
        source.set_meta('base', 1)
        for _ in range(60):
            plan = source.plan(500)
            if plan is None:
                break
            first, last, base = plan
            missing = next((n for n in range(1, 501) if n not in known), None)
            assert missing is None or first <= missing <= last
            source.put_many([(n, frame(n)) for n in range(first, last + 1)], SIGNATURE)
            known.update(range(first, last + 1))
            if last == 500:
                source.set_meta('tail', 501)
        assert source.plan(500) is None and len(known) == 500
    with cache.source(SERIAL, IDENT) as source:
        assert source.plan(500) is None and source.get(500) == frame(500)


class Radio:
    def __init__(self, count=100, base=0):
        self.count, self.base = count, base
        self.requests, self.clients = [], []
        self.recording, self.end = 0, 125
        self.wrong_offset = self.no_end = self.wrong_size = False
        self.drop_at = self.pause = None
        self.sent = 0
        self.batch = 3
        self.active_override = None
        self.order = None
        self.tick = .001

    def audio(self, seq):
        return b'\xf8\xff\xfe' + bytes(157)

    def ble(self):
        world = self

        class BLE(AudioBLE):
            async def connect(self):
                await super().connect()
                self.feeder = None
                world.clients.append(self)

            async def disconnect(self):
                if self.feeder:
                    self.feeder.cancel()
                    await asyncio.gather(self.feeder, return_exceptions=True)
                    self.feeder = None
                await super().disconnect()

            async def write_gatt_char(self, uuid, data, **kwargs):
                kind, command = data[5:7]
                if (kind, command) == (1, 1):
                    info = bytearray(51)
                    info[8:24] = SERIAL.upper().encode()
                    info[50] = world.recording
                    self.callback(None, packet(kind, command, info))
                elif (kind, command) == (0x1b, 0x0e):
                    payload = struct.pack('<HIII', 1, IDENT, world.end, world.count * 20)
                    active = world.active_override if world.active_override is not None else IDENT if world.recording else 0
                    self.callback(None, packet(kind, command, payload + struct.pack('<II', active, 0)))
                elif (kind, command) == (0x1a, 7):
                    offset, ident, live = struct.unpack('<IIB', data[9:-1])
                    assert ident == IDENT and live == 0
                    world.requests.append(offset)
                    key, nonce, counter = bytes(range(32)), bytes(range(16)), bytes(16)
                    header = struct.pack('<II', ident, (world.count + int(world.wrong_size)) * 166) + nonce
                    header += decrypt(b'soundcored3200' + key, self.session_key, counter) + counter + b'\0'
                    self.callback(None, packet(kind, command, header))

                    async def feed():
                        if world.pause:
                            await world.pause.wait()
                        first = world.base + offset // 166 + int(world.wrong_offset)
                        sequences = world.order if world.order is not None else list(range(first, world.base + world.count))
                        for start in range(0, len(sequences), world.batch):
                            body = b''
                            for seq in sequences[start:start + world.batch]:
                                if world.drop_at == seq:
                                    world.drop_at = None
                                    self.is_connected = False
                                    self.disconnected_callback(self)
                                    return
                                encrypted = decrypt(world.audio(seq), key, nonce[:12] + (seq * 10).to_bytes(4, 'big'))
                                body += struct.pack('<I', seq) + b'\0' + encrypted + b'\0'
                            self.callback(None, packet(kind, 0x12, body))
                            world.sent += len(body)
                            await asyncio.sleep(world.tick)
                        if not world.no_end:
                            self.callback(None, packet(kind, 0x0a))
                    self.feeder = asyncio.create_task(feed())
                else:
                    await super().write_gatt_char(uuid, data, **kwargs)
        return BLE

    async def device(self, directory):
        device = FeishuDevice(ble_loader=lambda: (self.ble(), FakeScanner),
                              reconnect_delays=(.001,), frame_cache=FrameCache(directory))
        await device.connect('simulation')
        return device


@pytest.mark.asyncio
async def test_supervisor_rechecks_after_waiting_for_operation_lock(tmp_path):
    radio = Radio()
    device = await radio.device(tmp_path)
    try:
        async with device.lock:
            await device._close_client()
            await asyncio.sleep(.02)
            await device._open(device.address, SERIAL)
            session = device.state['session_id']
        await asyncio.sleep(.03)
        assert len(radio.clients) == 2 and device.state['session_id'] == session
    finally:
        await device.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('base', [0, 1])
async def test_live_cache_closed_gaps_playable_ogg_and_no_replay(tmp_path, base):
    radio = Radio(base=base)
    device = await radio.device(tmp_path / 'cache')
    signature = hashlib.sha256(bytes(range(32)) + bytes(range(12))).hexdigest()
    with device.frame_cache.source(SERIAL, IDENT) as source:
        source.put_many([(seq, radio.audio(seq)) for seq in range(base + 10, base + 95)
                         if seq not in range(base + 50, base + 53)], signature)
    session = device.state['session_id']
    try:
        data = await device.download(str(IDENT), tmp_path / 'out', idle_timeout=.15)
        assert data['device_id'] == SERIAL and data['duration_ms'] == 2000
        assert radio.requests == [0, 46 * 166, 91 * 166]
        await asyncio.sleep(.02)
        assert device.snapshot()['connected'] and device.state['session_id'] == session
        assert device.state['export']['reusedFrames'] == 82
        assert device.state['export']['receivedBytes'] == radio.sent
        # Queue scheduling can deliver extra notifications before disconnect;
        # exact traffic is asserted above, never inferred from the planned range.
        import av
        with av.open(str(data['path'])) as audio:
            assert sum(f.samples for f in audio.decode(audio=0)) > 0
        before = list(radio.requests)
        again = await device.download(str(IDENT), tmp_path / 'again', idle_timeout=.15)
        assert radio.requests == before
        assert again['path'].read_bytes() == data['path'].read_bytes()
    finally:
        await device.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['wrong_offset', 'no_end', 'wrong_size', 'active', 'unknown'])
async def test_invalid_source_never_exports(tmp_path, failure):
    radio = Radio(count=12)
    if failure in ('active', 'unknown'):
        radio.recording = 1 if failure == 'active' else 255
    else:
        setattr(radio, failure, True)
    device = await radio.device(tmp_path / 'cache')
    try:
        with pytest.raises(ValueError):
            await device.download(str(IDENT), tmp_path / 'out', idle_timeout=.03)
        assert not list((tmp_path / 'out').glob('*.ogg'))
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_disconnection_retains_frames_and_retries_only_the_gap(tmp_path):
    radio = Radio(count=30)
    radio.drop_at = 15
    device = await radio.device(tmp_path / 'cache')
    try:
        await device.download(str(IDENT), tmp_path / 'out', idle_timeout=.1)
        assert len(radio.requests) == 2 and radio.requests[0] == 0
        assert radio.requests[1] == (15 - 4) * 166
        assert device.state['export']['cachedFrames'] == 30
        assert device.state['export']['retries'] == 1
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_slow_commit_does_not_timeout_an_end_packet_already_received(tmp_path, monkeypatch):
    original = Source.put_many
    def slow(self, *args):
        time.sleep(.08)
        return original(self, *args)
    monkeypatch.setattr(Source, 'put_many', slow)
    radio = Radio(count=12)
    device = await radio.device(tmp_path / 'cache')
    try:
        data = await device.download(str(IDENT), tmp_path / 'out', idle_timeout=.03)
        assert data['path'].exists()
    finally:
        await device.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('gap', [False, True])
async def test_closed_frames_accept_duplicates_out_of_order_but_require_every_frame(tmp_path, gap):
    radio = Radio(count=12)
    radio.order = [0, 2, 1, 2] + list(range(3, 12))
    if gap:
        radio.order.remove(6)
    device = await radio.device(tmp_path / 'cache')
    try:
        if gap:
            with pytest.raises(ValueError, match='完整|偏移'):
                await device.download(str(IDENT), tmp_path / 'out', idle_timeout=.2)
            assert not list((tmp_path / 'out').glob('*.ogg'))
        else:
            await device.download(str(IDENT), tmp_path / 'out', idle_timeout=.2)
            assert device.state['export']['receivedBytes'] == len(radio.order) * 166
            assert device.state['export']['cachedFrames'] == 12
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_cold_download_reports_progress_before_transfer_finishes(tmp_path):
    radio = Radio(count=600)
    radio.tick = .01
    device = await radio.device(tmp_path / 'cache')
    notices = []
    async def progress(value):
        notices.append(value['progress'])
    try:
        await device.download(str(IDENT), tmp_path / 'out', progress=progress, idle_timeout=.5)
        assert any(0 < value < 99 for value in notices)
        assert notices == sorted(notices)
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_background_cache_does_not_block_audio_and_flushes_on_stop(tmp_path, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    put = Source.put_many
    def stalled(self, *args):
        entered.set()
        gate.wait(5)
        return put(self, *args)
    monkeypatch.setattr(Source, 'put_many', stalled)
    world = World()
    device = await connected(world)
    device.frame_cache = FrameCache(tmp_path)
    frames, stop, stats = [], asyncio.Event(), {}
    task = live_stream(device, frames, stop, stats)
    try:
        await until(entered.is_set)
        before = len(frames)
        await until(lambda: len(frames) >= before + 30, 1)
    finally:
        gate.set()
        stop.set()
        await task
        await device.disconnect()
    with device.frame_cache.source(SERIAL, world.file_id) as source:
        assert source.sequences() == set(frames)


def test_invalid_audio_packet_rejected():
    with pytest.raises(ValueError):
        list(slices(packet(0x1a, 8, bytes(165))))


def test_unknown_record_state_is_not_idle():
    info = bytearray(51)
    info[8:24] = SERIAL.encode()
    info[50] = 255
    assert parse_info(info)['recordStatus'] is None


@pytest.mark.asyncio
async def test_recording_starts_between_info_and_directory_response(tmp_path):
    radio = Radio()
    radio.active_override = IDENT
    device = await radio.device(tmp_path / 'cache')
    try:
        with pytest.raises(ValueError, match='闭合'):
            await device.download(str(IDENT), tmp_path / 'out', idle_timeout=.1)
        assert radio.requests == []
    finally:
        await device.disconnect()


def test_streaming_ogg_keeps_existing_format_and_crc():
    from backend.feishu_protocol import iter_ogg
    frames = [bytes([i % 251]) * 157 + b'\x01\0\0' for i in range(151)]
    expected = '821021d7cf84eefec4f03e3d6c51455807991e97b76a277669c8a6fe0ac2ce4b'
    assert hashlib.sha256(b''.join(iter_ogg(iter(frames), len(frames)))).hexdigest() == expected


def test_export_checks_every_cached_frame_before_publishing(tmp_path):
    cache = FrameCache(tmp_path / 'cache')
    with cache.source(SERIAL, IDENT) as source:
        source.put_many([(n, frame(n)) for n in range(100)], SIGNATURE)
        source.set_meta('base', 0)
        source.set_meta('tail', 100)
        source.db.execute('UPDATE frames SET data=? WHERE seq=90', (frame(91),))
        source.db.commit()
        with pytest.raises(ValueError, match='校验冲突'):
            source.export(tmp_path / 'out.ogg', 100, threading.Event())
        assert not (tmp_path / 'out.ogg').exists()
    with pytest.raises(ValueError, match='冲突'):
        cache.source(SERIAL, IDENT)


@pytest.mark.asyncio
async def test_task_cancelled_as_radio_write_completes_never_archives(tmp_path):
    radio = Radio(count=12)
    device = await radio.device(tmp_path / 'cache')
    write = device._write
    async def racing_write(kind, ident, payload=b''):
        await write(kind, ident, payload)
        if (kind, ident) == (0x1a, 7):
            ready = asyncio.get_running_loop().create_future()
            ready.set_result(None)
            asyncio.current_task().cancel()
            # Python 3.11 wait_for may return an already-ready result despite
            # cancellation. The transport must check the task's cancellation flag.
            await asyncio.wait_for(ready, 1)
    device._write = racing_write
    try:
        task = asyncio.create_task(device.download(str(IDENT), tmp_path / 'out', idle_timeout=.1))
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list((tmp_path / 'out').glob('*.ogg'))
    finally:
        device._write = write
        await device.disconnect()


@pytest.mark.asyncio
async def test_cancel_during_slow_cache_shutdown_still_releases_radio(tmp_path, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    put = Source.put_many
    def stalled(self, *args):
        entered.set()
        gate.wait(5)
        return put(self, *args)
    monkeypatch.setattr(Source, 'put_many', stalled)
    world = World()
    device = await connected(world)
    device.frame_cache = FrameCache(tmp_path)
    frames, stop = [], asyncio.Event()
    task = live_stream(device, frames, stop, {})
    try:
        await until(entered.is_set)
        stop.set()
        await asyncio.sleep(.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert device.exclusive is None and device.transfer is None
        assert world.record != 1
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)
        await device.disconnect()


@pytest.mark.asyncio
async def test_cancel_during_radio_cleanup_still_finishes_cache_writer(tmp_path):
    world = World()
    device = await connected(world)
    device.frame_cache = FrameCache(tmp_path)
    frames, stop = [], asyncio.Event()
    task = live_stream(device, frames, stop, {})
    entered = asyncio.Event()
    close = device._close_client
    async def stuck_close():
        entered.set()
        await asyncio.Event().wait()
    try:
        await until(lambda: len(frames) >= 15)
        device._close_client = stuck_close
        stop.set()
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        def cache_released():
            try:
                with device.frame_cache.source(SERIAL, world.file_id) as source:
                    return source.count() >= 15
            except ValueError:
                return False
        await until(cache_released, 1)
    finally:
        device._close_client = close
        await device.disconnect()


@pytest.mark.asyncio
async def test_batched_notifications_count_all_arrived_bytes(tmp_path):
    radio = Radio()
    radio.batch = 100
    device = await radio.device(tmp_path / 'cache')
    with device.frame_cache.source(SERIAL, IDENT) as source:
        source.put_many([(n, radio.audio(n)) for n in range(10, 100)],
                        hashlib.sha256(bytes(range(32)) + bytes(range(12))).hexdigest())
    try:
        await device.download(str(IDENT), tmp_path / 'out', idle_timeout=.2)
        assert radio.requests == [0, 96 * 166]
        assert radio.sent == device.state['export']['receivedBytes'] == 104 * 166
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_live_rollover_and_late_frames_are_cached_but_not_replayed_to_asr(tmp_path):
    world = World()
    press(world, 1)
    world.sequence = 20
    device = await connected(world)
    device.frame_cache = FrameCache(tmp_path)
    frames, files, stats, stop = [], [], {}, asyncio.Event()
    from backend.feishu_live import stream_live
    task = asyncio.create_task(stream_live(device, lambda seq, raw: (frames.append(seq), files.append(stats['file_id'])),
                                           stop, stats, probe_interval=5))
    try:
        await until(lambda: len(frames) > 20)
        client, first = world.clients[-1], world.file_id
        encrypted = decrypt(world.frame(10), world.file_key, world.nonce[:12] + (10 * 10).to_bytes(4, 'big'))
        client.send(0x1a, 8, struct.pack('<I', 10) + b'\0' + encrypted + b'\0')
        await until(lambda: stats['duplicate_frames'] == 1)
        client.stop_feed()
        world.file_id, world.sequence = first + 21600, 0
        client.send(0x1a, 6, struct.pack('<I', world.file_id) + bytes(5))
        await until(lambda: files.count(world.file_id) > 20)
        assert 10 not in [n for n, fid in zip(frames, files) if fid == first]
    finally:
        stop.set()
        await task
        await device.disconnect()
    with device.frame_cache.source(SERIAL, first) as source:
        assert source.sequences() == {n for n, fid in zip(frames, files) if fid == first} | {10}
    with device.frame_cache.source(SERIAL, world.file_id) as source:
        assert source.sequences() == {n for n, fid in zip(frames, files) if fid == world.file_id}


@pytest.mark.asyncio
async def test_live_cache_bounded_overflow_is_reported_and_durable_frames_are_exact(tmp_path, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    original = Source.put_many
    def stalled(self, *args):
        entered.set()
        gate.wait(5)
        return original(self, *args)
    monkeypatch.setattr(Source, 'put_many', stalled)
    stats = {}
    cache = FrameCache(tmp_path)
    capture = CaptureWriter(cache, SERIAL, stats, capacity=3)
    capture.put(IDENT, 0, frame(0), SIGNATURE)
    try:
        await until(entered.is_set)
        for seq in range(1, 11):
            capture.put(IDENT, seq, frame(seq), SIGNATURE)
        assert capture.queue.qsize() == 3
        assert stats['cache_dropped_frames'] == 7
    finally:
        gate.set()
        await capture.close()
    with cache.source(SERIAL, IDENT) as source:
        assert source.sequences() == {0, 1, 2, 3}


@pytest.mark.asyncio
async def test_live_local_decoder_asr_and_text_log_still_work_with_cache(tmp_path):
    world = World(opus_frames(.5, 1.5, 6))
    device = await connected(world)
    device.frame_cache = FrameCache(tmp_path / 'cache')
    realtime = RealtimeASR(fake_engine=True)
    live = switch(device, realtime, transcripts=tmp_path / 'text')
    try:
        await live.start('虚拟声卡', True, None, 'zh')
        await until(lambda: live.snapshot()['transcript']['next_cursor'] >= 2, 15)
        assert Bridge.opened[-1].samples > 48000
        assert live.snapshot()['audio']['decode_errors'] == 0
        await live.stop()
        assert list((tmp_path / 'text').glob('*.txt'))
        with device.frame_cache.source(SERIAL, world.file_id) as source:
            assert source.count() > 50
    finally:
        await live.stop()
        await realtime.close()
        await device.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['batch', 'single'])
async def test_download_jobs_archive_once_and_reach_local_asr(tmp_path, monkeypatch, endpoint):
    from backend.app import create_app
    from backend.devices import DeviceManager
    radio = Radio()
    radio.pause = asyncio.Event()
    device = await radio.device(tmp_path / 'cache')
    devices = DeviceManager()
    devices.feishu = device
    app = create_app(tmp_path / 'store', devices=devices)
    calls = []
    async def recognize(path, *, model, progress):
        import av
        with av.open(str(path)) as container:
            assert sum(f.samples for f in container.decode(audio=0)) > 0
        calls.append((path, model))
        return dict(text='本地识别接口测试', segments=[], language='zh', duration=2)
    monkeypatch.setattr('backend.asr.transcribe', recognize)
    url = '/api/devices/feishu/actions' if endpoint == 'batch' else '/api/jobs'
    body = dict(action='download', device_id=SERIAL, ids=[str(IDENT)]) if endpoint == 'batch' else dict(type='download', device_type='feishu', original_id=str(IDENT))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            response = await client.post(url, json=body)
            assert response.status_code == 200, response.text
            await until(lambda: bool(radio.requests))
            assert (await client.post(url, json=body)).status_code == 409
            radio.pause.set()
            await asyncio.gather(*list(app.state.jobs.tasks.values()))
            job = app.state.jobs.get(response.json()['id'])
            assert job['status'] == 'completed', job
            records = (await client.get('/api/records')).json()
            assert len(records) == 1
            saved = records[0]
            assert saved['duration_ms'] == 2000
            before = list(radio.requests)
            again = await client.post(url, json=body)
            assert again.status_code == 200
            await asyncio.gather(*list(app.state.jobs.tasks.values()))
            assert app.state.jobs.get(again.json()['id'])['status'] == 'completed'
            assert radio.requests == before
            assert len((await client.get('/api/records')).json()) == 1
            transcribe = await client.post('/api/jobs', json=dict(type='transcribe', record_id=saved['id']))
            assert transcribe.status_code == 200, transcribe.text
            await asyncio.gather(*list(app.state.jobs.tasks.values()))
            result = app.state.jobs.get(transcribe.json()['id'])
            assert result['status'] == 'completed', result
            assert result['result']['record']['transcript'] == '本地识别接口测试'
            assert calls[0][1] == 'sensevoice-small'
    finally:
        radio.pause.set()
        await app.state.jobs.shutdown()
        await app.state.realtime.close()
        await device.disconnect()


@pytest.mark.asyncio
async def test_cancelled_job_never_archives_or_locks_out_retry(tmp_path):
    from backend.app import create_app
    from backend.devices import DeviceManager
    radio = Radio()
    radio.pause = asyncio.Event()
    device = await radio.device(tmp_path / 'cache')
    devices = DeviceManager()
    devices.feishu = device
    app = create_app(tmp_path / 'store', devices=devices)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
            response = await client.post('/api/devices/feishu/actions', json=dict(action='download', device_id=SERIAL, ids=[str(IDENT)]))
            await until(lambda: bool(radio.requests))
            cancel = await client.post('/api/jobs/' + response.json()['id'] + '/cancel')
            assert cancel.json()['status'] == 'cancelled'
            assert (await client.get('/api/records')).json() == []
            assert device.transfer is None and not device.lock.locked()
            with device.frame_cache.source(SERIAL, IDENT) as source:
                assert source.meta('tail') is None
    finally:
        radio.pause.set()
        await app.state.jobs.shutdown()
        await app.state.realtime.close()
        await device.disconnect()


@pytest.mark.asyncio
async def test_cancel_during_export_releases_cache_lock_without_final_file(tmp_path, monkeypatch):
    entered = threading.Event()
    original = Source.export
    def delayed(self, path, count, cancelled):
        entered.set()
        assert cancelled.wait(3)
        return original(self, path, count, cancelled)
    monkeypatch.setattr(Source, 'export', delayed)
    radio = Radio(count=12)
    device = await radio.device(tmp_path / 'cache')
    task = asyncio.create_task(device.download(str(IDENT), tmp_path / 'out', idle_timeout=.2))
    try:
        await until(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert not list((tmp_path / 'out').glob('*.ogg'))
        with device.frame_cache.source(SERIAL, IDENT) as source:
            assert source.plan(12) is None
    finally:
        await asyncio.gather(task, return_exceptions=True)
        await device.disconnect()
