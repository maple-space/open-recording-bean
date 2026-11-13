"""Realtime audio from the service-owned recorder session.

Puts the recorder into the state the phone app uses for live transcription:
recording on (18 82 01), then a realtime transfer of the file being recorded
(1A 07 with the realtime flag). Encrypted Opus frames then arrive continuously
as BLE notifications until the caller asks to stop.

Measured on firmware 04.92 (2026-11-13):
- a realtime transfer starts at the live edge, not at the start of the file;
- the header of a live file reports its size as 0xFFFFFFFF;
- the offset field means "bytes already transferred" (160 per frame);
- 18 82 is answered with two bytes, the new state first;
- a paused recording is finalised by the recorder soon after BLE disconnects;
- after exactly six hours the recorder starts a new file and announces it with 1A 06;
  it does not send 1A 0A for the old one;
- a realtime transfer stays open for the life of the BLE connection: pausing the recording
  stops the slices without a 1A 0A, resuming continues them with the next sequence number
  and without a new request;
- asking again while that transfer is open is refused with error 4 in the header.

Only uses what FeishuDevice already exposes (lock, request helper, transfer
queue, session crypto), so the connection logic stays in one place.
"""
import asyncio
import struct
import time
from contextlib import suppress

from .feishu_protocol import decrypt, parse_info

IDLE_TIMEOUT = 15.0          # seconds without audio while the recorder says it is recording
INFO_INTERVAL = 60.0         # battery / storage / recording state while audio flows
PROBE_INTERVAL = 2.0         # the same query while no audio arrives
EXCLUSIVE = '实时输入正在进行。请先在“实时输入”页面关闭它，再刷新、下载或删除录音。'


async def _info(device):
    info = parse_info((await device._exchange(1, 1))[9:-1])
    device.state.update(info=info, recordStatus=info.get('recordStatus', device.state.get('recordStatus', 0)))
    return info


async def _live_file_id(device, attempts: int = 6) -> int:
    """The recorder appends an 8-byte descriptor of the file being recorded to its directory."""
    for attempt in range(attempts):
        payload = (await device._exchange(0x1b, 0x0e, b'\x00\x00'))[9:-1]
        if len(payload) >= 2:
            offset = 2 + 12 * int.from_bytes(payload[:2], 'little')
            if len(payload) >= offset + 8:
                candidate = int.from_bytes(payload[offset:offset + 4], 'little')
                if candidate:
                    return candidate
        await asyncio.sleep(0.5 if attempt < 2 else 1.0)
    raise ValueError('录音豆没有报告正在录制的文件，无法开始实时传输。请确认设备指示灯已亮起后重试。')


async def stream_live(device, on_frame, stop: asyncio.Event, stats: dict, resume: dict | None = None, *,
                      idle_timeout: float = IDLE_TIMEOUT, info_interval: float = INFO_INTERVAL,
                      probe_interval: float = PROBE_INTERVAL) -> str:
    """Deliver ordered, decrypted Opus frames to on_frame(sequence, data) until told to stop.

    Owns the recorder link for the whole session: other operations fail fast
    with a clear message instead of colliding with the stream. *resume* maps a
    file id to the last sequence delivered and should be kept by the caller
    across calls, so a reconnect continues where the audio stopped.

    Returns 'stopped' (caller asked), 'disconnect_requested' (the user
    disconnected the recorder) or 'device_stopped' (recording was ended on the
    recorder itself). Raises ValueError with a user-facing message on failure.
    """
    resume = {} if resume is None else resume
    stats.update(frames=0, lost_frames=0, duplicate_frames=0, stalls=0, file_id=None, started_at=None,
                 last_frame_at=None, paused=False)
    if device.lock.locked():
        raise ValueError('录音豆正在执行其他操作，请等它完成后再开启实时输入。')
    async with device.lock:
        if not device.snapshot()['connected']:
            raise ValueError('录音豆未连接。请先在“录音豆”页面连接设备。')
        device.exclusive = EXCLUSIVE
        device.state['live'] = {'status': 'starting'}
        deliberate = failed = False
        try:
            if not device.crypto.session_key:
                raw = await device._exchange(0x2e, 1, device.crypto.public_key())
                device.crypto.handshake(raw[9:-1])
            info = await _info(device)
            if info.get('recordStatus') != 1:
                raw = await device._exchange(0x18, 0x82, b'\x01')
                if raw[9:10] != b'\x01':
                    raise ValueError('录音豆没有进入录音状态，请查看设备指示灯后重试。')
                device.state['recordStatus'] = 1
            file_id = queue = secret = None
            idle_since = time.monotonic()

            async def attach():
                """(Re)request the live transfer of whatever file the recorder is writing now."""
                nonlocal file_id, queue, secret, idle_since
                file_id = await _live_file_id(device)
                previous = resume.get(file_id)
                secret = None
                queue = device.transfer = asyncio.Queue()       # anything queued for an older request is dropped
                await device._write(0x1a, 7, struct.pack('<IIB', previous * 160 if previous else 0, file_id, 1))
                idle_since = time.monotonic()
                stats['file_id'] = file_id
                device.state['live'] = {'status': 'streaming', 'file_id': file_id}

            await attach()
            last_check = time.monotonic()
            retried = was_paused = False
            while True:
                if stop.is_set() or not device.desired:
                    deliberate = True
                    return 'stopped' if stop.is_set() else 'disconnect_requested'
                try:
                    raw = await asyncio.wait_for(queue.get(), 0.25)
                except (asyncio.TimeoutError, TimeoutError):
                    raw = b''
                if raw is None:
                    raise ValueError('蓝牙连接已中断，实时音频已停止。')
                now = time.monotonic()
                ended = False
                if raw:
                    ident = raw[6]
                    if ident == 7:
                        if int.from_bytes(raw[9:13], 'little') == file_id:      # a late header of an older file is ignored
                            secret = device.crypto.file_secret(raw[9:-1], file_id)
                    elif ident in (8, 0x12) and secret is not None:
                        offset = 9
                        while len(raw) - offset >= 165:
                            sequence = int.from_bytes(raw[offset:offset + 4], 'little')
                            encrypted = raw[offset + 5:offset + 165]
                            offset += 166
                            last = resume.get(file_id)
                            if last is not None and sequence <= last:
                                stats['duplicate_frames'] += 1
                                continue
                            if last is not None and sequence > last + 1:
                                stats['lost_frames'] += sequence - last - 1
                            resume[file_id] = sequence
                            plain = decrypt(encrypted, secret[0], secret[1] + (sequence * 10).to_bytes(4, 'big'))
                            if stats['started_at'] is None:
                                stats['started_at'] = now
                            stats['frames'] += 1
                            stats['last_frame_at'] = idle_since = now
                            if was_paused:                                  # audio is back after a pause on the recorder
                                device.state['live'] = {'status': 'streaming', 'file_id': file_id}
                            stats['paused'] = retried = was_paused = False
                            on_frame(sequence, plain.rstrip(b'\x00') or b'\x00')
                        if len(resume) > 8:                                     # only recent files can be resumed
                            for stale in sorted(resume)[:-8]:
                                del resume[stale]
                    elif ident == 0x0a:
                        ended = True
                    elif ident == 6 and len(raw) >= 14:
                        announced = int.from_bytes(raw[9:13], 'little')
                        if announced and announced != file_id:              # the recorder moved on: follow it at once
                            await attach()
                            continue
                idle = now - idle_since
                # Ask the recorder how it is doing: rarely while audio flows, often while it does not.
                interval = probe_interval if idle > probe_interval or was_paused else info_interval
                if not ended and now - last_check < interval:
                    continue
                last_check = now
                try:
                    state = (await _info(device)).get('recordStatus')
                except (asyncio.TimeoutError, TimeoutError):
                    if idle > idle_timeout * 2:
                        raise ValueError('录音豆没有回应，实时音频已停止。') from None
                    continue                                                    # one unanswered query does not end a healthy stream
                if state == 0:
                    return 'device_stopped'
                if state == 2:                                                  # paused on the recorder: wait, never time out
                    stats['paused'] = was_paused = True
                    device.state['live'] = {'status': 'paused', 'file_id': file_id}
                    continue
                if was_paused:                                                  # resumed on the recorder: the open transfer
                    was_paused = False                                          # continues by itself; asking again is refused
                    stats['paused'] = False
                    device.state['live'] = {'status': 'streaming', 'file_id': file_id}
                    idle_since = time.monotonic()
                elif ended:                                                     # the recorder closed this transfer
                    await attach()
                elif idle > idle_timeout:
                    if retried:
                        raise ValueError(f'连续 {int(idle_timeout * 2)} 秒没有收到实时音频，已停止。请确认录音豆在附近且正在录音。')
                    retried = True
                    stats['stalls'] += 1
                    await attach()
        except BaseException:
            failed = True
            raise
        finally:
            device.transfer = None
            device.state.pop('live', None)
            device.exclusive = None
            connected = device.client is not None and device.client.is_connected
            if deliberate and connected:
                # Leave the recorder paused rather than recording into its storage forever.
                if device.state.get('recordStatus') == 1:
                    with suppress(Exception):
                        raw = await device._exchange(0x18, 0x82, b'\x02')
                        if raw[9:10] == b'\x02':
                            device.state['recordStatus'] = 2
                # The transfer is still open on the recorder and there is no command to close it.
                # Dropping the link closes it; left open, the next realtime request is refused
                # (error 4) and its slices would mix into a later download. The session supervisor
                # reconnects by itself, and a paused recording is finalised once the link is gone.
                with suppress(Exception):
                    await device._close_client()
                device.state['connection_status'] = 'reconnecting' if device.desired else 'disconnected'
                # A later session starts afresh; the frames between the pause and the dropped
                # link were never wanted, so they must not be counted as lost next time.
                resume.pop(file_id, None)
            elif failed:
                # Same rule as FeishuDevice.operation(): flush an ambiguous link, never replay.
                with suppress(Exception):
                    await device._close_client()
                device.state['connection_status'] = 'reconnecting' if device.desired else 'disconnected'
