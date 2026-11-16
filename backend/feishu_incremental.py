"""Verified closed-file gap reads, using 166-byte wire offsets (160-byte audio).

Realtime never seeks using this offset rule. A closed read is open-ended on the
recorder: we stop a non-tail range by releasing GATT, then reconnect under the
same operation lock. No start, pause, or delete command is issued here.
"""
import asyncio
import struct
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from .feishu_cache import WIRE, SourceWorker, signature, check_cancelled
from .feishu_protocol import decrypt, parse_info
from .feishu_errors import DeviceIdentityError, TransferInterrupted


async def check_idle(device, serial):
    """A light, read-only probe; never page through the directory during audio."""
    check_cancelled()
    if not device.desired:
        raise ValueError('连接已停止，增量同步取消')
    info = parse_info((await device._exchange(1, 1))[9:-1])
    if info['serialNumber'] != serial:
        raise DeviceIdentityError('设备身份已变化，未归档')
    if info.get('recordStatus') != 0:
        raise ValueError('设备仍在录音或状态未知，请结束录音后再保存')
    return info


def slices(raw):
    payload = raw[9:-1]
    if not payload or len(payload) % WIRE:
        raise ValueError('音频通知不是完整的 166 字节传输帧')
    for at in range(0, len(payload), WIRE):
        yield int.from_bytes(payload[at:at + 4], 'little'), payload[at + 5:at + 165]


async def closed_item(device, serial, ident):
    await check_idle(device, serial)
    rows = await device._list()
    item = next((r for r in rows if r['original_id'] == str(ident)), None)
    if item is None or device.state.get('active_file_id'):
        raise ValueError('设备录音尚未闭合或不在目录中')
    return item


async def read_range(device, ident, count, plan, source, timeout, receipt, progress=None):
    first, last, base = plan
    requested = last - first + 1
    offset = 0 if base is None else (first - base) * WIRE
    receipt['lastOffsetBytes'] = offset
    receipt['rangeRequests'] = receipt.get('rangeRequests', 0) + 1
    queue = device.transfer = asyncio.Queue()
    secret, fingerprint = None, None
    seen = bytearray(requested)
    unique = 0
    pending = []
    base_saved = False
    started = last_new = last_probe = time.monotonic()
    last_report = started
    ready_at = None

    async def flush(report=True):
        nonlocal base_saved, last_report
        if pending:
            added = await source.call('put_many', list(pending), fingerprint)
            pending.clear()
            receipt['cachedFrames'] += added
            receipt['progress'] = min(99, receipt['cachedFrames'] * 100 // count)
            if not base_saved:
                await source.call('set_meta', 'base', base)
                base_saved = True
            if report and progress and time.monotonic() - last_report >= 1:
                await report_progress(progress, receipt)
                last_report = time.monotonic()

    async def receive():
        nonlocal secret, fingerprint, unique, base, first, last, last_new, ready_at, last_probe
        await device._write(0x1a, 7, struct.pack('<IIB', offset, ident, 0))
        while True:
            check_cancelled()
            if not device.desired:
                raise ValueError('连接已停止，增量同步取消')
            now = time.monotonic()
            deadline = min(last_new + timeout,
                           ready_at + min(10, timeout) if ready_at is not None else float('inf'),
                           started + 15 if secret is None else float('inf'))
            if now >= deadline and queue.empty():
                raise TransferInterrupted('增量补传暂时没有新音频，已保存部分保留，等待续传')
            try:
                raw = queue.get_nowait() if not queue.empty() else await asyncio.wait_for(queue.get(), min(.25, deadline - now))
            except TimeoutError:
                raw = b''
            if raw is None:
                await flush()
                raise TransferInterrupted('增量补传连接中断，已保存音频保留')
            if raw:
                if len(raw) < 10 or raw[4] & 15 != 1 or raw[5] not in (0x1a, 0x1b):
                    raise ValueError('设备拒绝增量传输或响应无效')
                cmd = raw[6]
                if cmd == 7:
                    if secret is not None:
                        raise ValueError('增量传输收到重复文件头')
                    secret = device.crypto.file_secret(raw[9:-1], ident)
                    if secret[2] != count * WIRE:
                        raise ValueError('闭合文件头长度与设备目录不一致')
                    fingerprint = signature(secret)
                    last_new = time.monotonic()
                elif cmd in (8, 0x12):
                    if secret is None:
                        raise ValueError('音频缺少已验证文件头')
                    batch = list(slices(raw))
                    receipt['receivedBytes'] += len(batch) * WIRE
                    accepted = []
                    for seq, encrypted in batch:
                        if not unique:
                            if base is None:
                                if seq not in (0, 1):
                                    raise ValueError('零偏移未返回起始帧，停止补传')
                                base = seq
                                first, last = seq, seq + last
                            if seq != first:
                                raise ValueError('补传响应偏移与请求不符，停止补传')
                        if not first <= seq <= last:
                            raise ValueError('补传区间未收齐却收到范围外音频')
                        data = decrypt(encrypted, secret[0], secret[1] + (seq * 10).to_bytes(4, 'big'))
                        accepted.append((seq, data))
                        if not seen[seq - first]:
                            seen[seq - first] = 1
                            unique += 1
                            last_new = time.monotonic()
                        if unique == requested and last < base + count - 1:
                            break  # arrived trailing slices count as traffic, never as coverage
                    if unique == requested and last < base + count - 1:
                        # Stop the open-ended radio export before waiting for the disk.
                        # Otherwise a slow fsync can receive much of the source again.
                        await device._close_client(reason='range_complete')
                    pending.extend(accepted)
                    if len(pending) >= 128 or unique == requested:
                        await flush()
                    if unique == requested:
                        if last < base + count - 1:
                            return True  # intentional range boundary, not a lost link
                        ready_at = ready_at or time.monotonic()
                elif cmd == 0x0a:
                    if secret is None or base is None or unique != requested or last != base + count - 1:
                        await flush()
                        raise TransferInterrupted('录音尾部或补传区间未完整验证')
                    await source.call('set_meta', 'tail', base + count)
                    return False  # the device ended this transfer; keep the connection
                elif cmd == 6:
                    # A notification can be late; verify actual state before deciding.
                    await closed_item(device, device.state['device_id'], ident)
                else:
                    raise ValueError('未知补传响应，停止归档')
            if time.monotonic() - last_probe >= 60:
                await check_idle(device, device.state['device_id'])
                last_probe = time.monotonic()
    try:
        return await receive()
    finally:
        # Even cancellation must retain already decrypted and verified frames.
        # A fresh cleanup task can use SourceWorker without inheriting the
        # caller's cancellation flag; __aexit__ drains its queued disk writes.
        if pending:
            cleanup = asyncio.create_task(flush(report=False))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cleanup.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                raise


async def report_progress(progress, receipt):
    message = (f"增量同步 · 复用 {receipt['reusedFrames']} 帧 · "
               f"本次接收 {receipt['receivedBytes']} 字节 · {receipt['progress']}%")
    if receipt.get('status') == 'reconnecting':
        message = f"连接中断，等待自动续传（第 {receipt['retries']} 次）· 已缓存 {receipt['cachedFrames']} 帧"
    elif receipt.get('status') == 'verifying':
        message = '音频已收齐，正在核验并生成本机播放文件'
    await progress(dict(message=message, progress=receipt['progress'], stage=receipt['status']))


async def download_incremental(device, original_id, output_dir, progress=None, idle_timeout=30):
    async with device.operation():
        ident, serial = int(original_id), device.state['device_id']
        session = device.state.get('session_id')
        generation = device.state.get('connection_generation')
        device.state['export'] = receipt = dict(status='downloading', fileId=ident, progress=0,
                                                receivedBytes=0, reusedFrames=0, completedRanges=0,
                                                cachedFrames=0, retries=0)

        def still_selected():
            check_cancelled()
            if not device.desired:
                raise ValueError('连接已停止，增量同步取消')
            if device.state['device_id'] != serial:
                raise DeviceIdentityError('设备身份已变化，增量同步取消')
            if device.state.get('connection_generation') != generation:
                raise DeviceIdentityError('连接操作已变化，原同步任务停止')

        async def release_transfer(close=False):
            try:
                if close:
                    await device._close_client(reason='transfer_recovery')
            finally:
                queue, device.transfer = device.transfer, None
                while queue is not None and not queue.empty():
                    raw = queue.get_nowait()
                    if raw and len(raw) >= 10 and raw[6] in (8, 0x12):
                        receipt['receivedBytes'] += len(raw) - 10

        try:
            async with SourceWorker(device.frame_cache, serial, ident) as source:
                receipt['reusedFrames'] = await source.call('count')
                receipt['cachedFrames'] = receipt['reusedFrames']
                stalled, attempt = 0, 0
                preserve_session = False
                while True:
                    still_selected()
                    before = receipt['cachedFrames']
                    attempted_read = False
                    try:
                        if not device.snapshot()['connected']:
                            await device._open(device.address, serial)
                            # Planned range boundaries retain the operation session.
                            # An unexpected interruption starts a new session so a
                            # backup-and-delete job cannot silently cross it.
                            if preserve_session:
                                device.state['session_id'] = session
                            else:
                                session = device.state.get('session_id')
                            preserve_session = False
                        # Verify the source before each requested range and again after
                        # EOF. Directory traffic never competes with the audio stream.
                        item = await closed_item(device, serial, ident)
                        count = await source.call('bind', item)
                        receipt['expectedBytes'] = count * WIRE
                        receipt['progress'] = min(99, receipt['cachedFrames'] * 100 // count)
                        plan = await source.call('plan', count)
                        if plan is None:
                            break
                        if not device.crypto.session_key:
                            device.crypto.handshake((await device._exchange(0x2e, 1, device.crypto.public_key()))[9:-1])
                            # Recording could start during the key exchange.
                            await source.call('bind', await closed_item(device, serial, ident))
                        receipt['status'] = 'downloading'
                        attempted_read = True
                        try:
                            preserve_session = await read_range(device, ident, count, plan, source, idle_timeout, receipt, progress)
                        except TransferInterrupted:
                            raise  # inspect whether the radio is still available before releasing it
                        except BaseException:
                            with suppress(Exception):
                                await release_transfer(close=True)
                            raise
                        else:
                            # A verified EOF ends the export itself; do not disconnect
                            # an otherwise healthy radio just to start the next file.
                            await release_transfer()
                        receipt['completedRanges'] += 1
                        stalled = attempt = 0
                        if progress:
                            await report_progress(progress, receipt)
                    except TransferInterrupted as error:
                        still_selected()
                        receipt['cachedFrames'] = await source.call('count')
                        progressed = receipt['cachedFrames'] > before
                        # Repeated missing tail / stalled reads on an available device
                        # need attention. An unavailable radio keeps waiting, without
                        # imposing a total time limit on a legitimate long transfer.
                        available = bool(device.client and device.client.is_connected)
                        stalled = 0 if progressed else stalled + int(available or attempted_read)
                        preserve_session = False
                        with suppress(Exception):
                            await release_transfer(close=True)
                        if stalled >= 8:
                            raise ValueError(f'多次重试仍未确认完整音频；已保存部分可继续同步：{error}') from None
                        receipt.update(status='reconnecting', retries=receipt['retries'] + 1,
                                       last_error=str(error))
                        if progress:
                            await report_progress(progress, receipt)
                        delay = device.delays[min(attempt, len(device.delays) - 1)]
                        attempt = 0 if progressed else attempt + 1
                        await asyncio.sleep(delay)
                receipt.update(status='verifying', progress=99)
                if progress:
                    await report_progress(progress, receipt)
                path = Path(output_dir) / f'{uuid.uuid4().hex}.ogg'
                await source.call('export', path, count, source.cancelled)
                still_selected()
                if not device.desired or not device.snapshot()['connected']:
                    raise ValueError('连接已停止，归档取消；已保存音频保留')
                receipt.update(status='complete', progress=100)
                return dict(path=path, device_type='feishu', device_id=serial, original_id=str(ident),
                            title=item['title'], recorded_at=datetime.fromtimestamp(ident, timezone.utc).isoformat(),
                            duration_ms=count * 20)
        except BaseException:
            receipt['status'] = 'failed'
            raise
