"""Resume acceptance with a simulated radio; never opens physical Bluetooth."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.tests.test_feishu_incremental import Radio, SERIAL, IDENT


@pytest.mark.asyncio
async def test_a_dropped_download_resumes_in_the_same_task_from_durable_frames(tmp_path):
    radio = Radio(count=90)
    radio.drop_at = 45
    device = await radio.device(tmp_path / 'cache')
    session = device.state['session_id']
    try:
        result = await asyncio.wait_for(device.download(str(IDENT), tmp_path / 'out', idle_timeout=.2), 4)
        assert result['path'].exists()
        assert radio.requests[0] == 0 and len(radio.requests) == 2
        assert radio.requests[1] == (45 - 4) * 166
        assert device.snapshot()['connected'] and device.state['session_id'] != session
        assert device.state['export']['retries'] == 1
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_verified_same_recorder_can_resume_after_its_transport_address_changes(tmp_path):
    radio = Radio(count=90)
    radio.drop_at = 45
    device = await radio.device(tmp_path / 'cache')
    original = device._open
    async def verified_reopen(address, serial=''):
        await original(address, serial)
        device.address = 'verified-new-address'
    device._open = verified_reopen
    try:
        data = await asyncio.wait_for(device.download(str(IDENT), tmp_path / 'out', idle_timeout=.2), 4)
        assert data['path'].exists() and data['device_id'] == SERIAL
        assert device.address == 'verified-new-address'
        assert radio.requests == [0, (45 - 4) * 166]
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_a_file_that_repeatedly_drops_without_progress_does_not_block_the_batch(tmp_path, monkeypatch):
    from backend import feishu_incremental as incremental
    from backend.feishu_errors import TransferInterrupted
    radio = Radio(count=30)
    device = await radio.device(tmp_path / 'cache')
    reads = []
    async def bad_read(*args, **kwargs):
        reads.append(1)
        device.client.is_connected = False
        device._lost(device.client)
        raise TransferInterrupted('same damaged file disconnects')
    monkeypatch.setattr(incremental, 'read_range', bad_read)
    try:
        with pytest.raises(ValueError, match='多次重试'):
            await asyncio.wait_for(device.download(str(IDENT), tmp_path / 'out', idle_timeout=.2), 3)
        assert len(reads) == 8
        assert not list((tmp_path / 'out').glob('*.ogg'))
    finally:
        await device.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('error_type', [asyncio.TimeoutError, TimeoutError])
async def test_one_wrapped_info_timeout_does_not_end_healthy_realtime_audio(error_type):
    from backend.feishu_errors import TransferInterrupted
    from backend.tests.test_live import World, connected, live_stream, until
    world = World()
    device = await connected(world)
    frames, stats, stop = [], {}, asyncio.Event()
    original, failures = device._exchange, []
    async def exchange(kind, ident, *args, **kwargs):
        if (kind, ident) == (1, 1) and len(frames) >= 30 and not failures:
            failures.append(len(frames))
            raise TransferInterrupted('query timeout') from error_type()
        return await original(kind, ident, *args, **kwargs)
    device._exchange = exchange
    task = live_stream(device, frames, stop, stats, info_interval=.02)
    try:
        await until(lambda: bool(failures))
        await until(lambda: len(frames) > failures[0] + 30 or task.done())
        assert not task.done()
        stop.set()
        assert await asyncio.wait_for(task, 2) == 'stopped'
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
        await device.disconnect()


@pytest.mark.asyncio
async def test_a_complete_file_keeps_the_connection_for_the_next_file(tmp_path):
    radio = Radio(count=12)
    device = await radio.device(tmp_path / 'cache')
    initial = device.client
    reports = []
    async def progress(value):
        reports.append(value)
    try:
        await device.download(str(IDENT), tmp_path / 'out', progress=progress, idle_timeout=.2)
        assert device.client is initial and len(radio.clients) == 1
        assert device.snapshot()['connected']
        assert reports[-1]['stage'] == 'verifying' and reports[-1]['progress'] == 99
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_unexpected_disconnect_after_eof_cannot_preserve_delete_session(tmp_path):
    radio = Radio(count=12)
    device = await radio.device(tmp_path / 'cache')
    session = device.state['session_id']
    dropped = False
    async def progress(_):
        nonlocal dropped
        if device.state['export']['completedRanges'] and not dropped:
            dropped = True
            await device.client.disconnect()
    try:
        data = await device.download(str(IDENT), tmp_path / 'out', progress=progress, idle_timeout=.2)
        assert data['path'].exists() and dropped
        assert device.state['session_id'] != session
        assert radio.requests == [0]  # a verified cache does not need the audio again
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_healthy_slow_audio_has_no_total_deadline_and_does_not_poll_full_directory(tmp_path, monkeypatch):
    from backend import feishu_incremental as incremental
    radio = Radio(count=30)
    device = await radio.device(tmp_path / 'cache')
    clock = [0.0]
    monkeypatch.setattr(incremental, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    original_slices, original_list = incremental.slices, device._list
    listings = []
    def slow_audio(raw):
        clock[0] += 12
        yield from original_slices(raw)
    async def listing():
        listings.append(clock[0])
        return await original_list()
    monkeypatch.setattr(incremental, 'slices', slow_audio)
    device._list = listing
    try:
        result = await device.download(str(IDENT), tmp_path / 'out', idle_timeout=30)
        assert result['path'].exists() and clock[0] > 60
        assert not any(0 < value < clock[0] for value in listings)
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_cancelling_flushes_valid_frames_smaller_than_the_write_batch(tmp_path):
    radio = Radio(count=300)
    radio.tick = .05
    device = await radio.device(tmp_path / 'cache')
    task = asyncio.create_task(device.download(str(IDENT), tmp_path / 'out', idle_timeout=.5))
    try:
        for _ in range(100):
            if radio.sent >= 9 * 166:
                break
            await asyncio.sleep(.01)
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with device.frame_cache.source(SERIAL, IDENT) as source:
            assert source.count() >= 9
            assert source.meta('base') == '0'
        assert not list((tmp_path / 'out').glob('*.ogg'))
    finally:
        await device.disconnect()


@pytest.mark.asyncio
async def test_an_early_end_with_a_missing_frame_repairs_the_gap_before_archiving(tmp_path, monkeypatch):
    from backend import feishu_incremental as incremental
    radio = Radio(count=30)
    device = await radio.device(tmp_path / 'cache')
    original = incremental.slices
    dropped = False
    def miss_one(raw):
        nonlocal dropped
        for seq, data in original(raw):
            if seq == 15 and not dropped:
                dropped = True
                continue
            yield seq, data
    monkeypatch.setattr(incremental, 'slices', miss_one)
    try:
        data = await asyncio.wait_for(device.download(str(IDENT), tmp_path / 'out', idle_timeout=.2), 4)
        assert data['path'].exists()
        assert radio.requests[0] == 0 and all(offset > 0 for offset in radio.requests[1:])
        with device.frame_cache.source(SERIAL, IDENT) as source:
            assert source.count() == 30 and source.meta('tail') == '30'
    finally:
        await device.disconnect()
