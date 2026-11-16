"""Verified closed-file gap reads, using 166-byte wire offsets (160-byte audio).

Realtime never seeks using this offset rule. A closed read is open-ended on the
recorder: we stop a non-tail range by releasing GATT, then reconnect under the
same operation lock. No start, pause, or delete command is issued here.
"""
import asyncio
import struct
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .feishu_cache import WIRE, SourceWorker, signature, check_cancelled
from .feishu_protocol import decrypt, parse_info


def slices(raw):
    payload = raw[9:-1]
    if not payload or len(payload) % WIRE:
        raise ValueError('音频通知不是完整的 166 字节传输帧')
    for at in range(0, len(payload), WIRE):
        yield int.from_bytes(payload[at:at + 4], 'little'), payload[at + 5:at + 165]


async def closed_item(device, serial, ident):
    check_cancelled()
    if not device.desired:
        raise ValueError('连接已停止，增量同步取消')
    info = parse_info((await device._exchange(1, 1))[9:-1])
    if info['serialNumber'] != serial:
        raise ValueError('设备身份已变化，未归档')
    if info.get('recordStatus') != 0:
        raise ValueError('设备仍在录音或状态未知，请结束录音后再保存')
    rows = await device._list()
    item = next((r for r in rows if r['original_id'] == str(ident)), None)
    if item is None or device.state.get('active_file_id'):
        raise ValueError('设备录音尚未闭合或不在目录中')
    return item


async def read_range(device, ident, count, plan, source, timeout, receipt, progress=None):
    first, last, base = plan
    requested = last - first + 1
    offset = 0 if base is None else (first - base) * WIRE
    queue = device.transfer = asyncio.Queue()
    secret, fingerprint = None, None
    seen = bytearray(requested)
    unique = 0
    pending = []
    base_saved = False
    started = last_new = last_probe = time.monotonic()
    last_report = started
    ready_at = None
    total_timeout = max(30, requested * WIRE / 4096 + 30)

    async def flush():
        nonlocal base_saved, last_report
        if pending:
            added = await source.call('put_many', list(pending), fingerprint)
            pending.clear()
            receipt['cachedFrames'] += added
            receipt['progress'] = min(99, receipt['cachedFrames'] * 100 // count)
            if not base_saved:
                await source.call('set_meta', 'base', base)
                base_saved = True
            if progress and time.monotonic() - last_report >= 1:
                await report_progress(progress, receipt)
                last_report = time.monotonic()

    await device._write(0x1a, 7, struct.pack('<IIB', offset, ident, 0))
    while True:
        check_cancelled()
        if not device.desired:
            raise ValueError('连接已停止，增量同步取消')
        now = time.monotonic()
        deadline = min(last_new + timeout, started + total_timeout,
                       ready_at + min(10, timeout) if ready_at is not None else float('inf'),
                       started + 15 if secret is None else float('inf'))
        if now >= started + total_timeout or (now >= deadline and queue.empty()):
            raise ValueError('增量补传超时，已保存音频保留；未确认完整归档')
        try:
            raw = queue.get_nowait() if not queue.empty() else await asyncio.wait_for(queue.get(), min(.25, deadline - now))
        except TimeoutError:
            raw = b''
        if raw is None:
            await flush()
            raise ValueError('增量补传连接中断，已保存音频保留')
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
                    await device._close_client()
                pending.extend(accepted)
                if len(pending) >= 128 or unique == requested:
                    await flush()
                if unique == requested:
                    if last < base + count - 1:
                        return
                    ready_at = ready_at or time.monotonic()
            elif cmd == 0x0a:
                if secret is None or base is None or unique != requested or last != base + count - 1:
                    await flush()
                    raise ValueError('录音尾部或补传区间未完整验证')
                await source.call('set_meta', 'tail', base + count)
                return
            elif cmd == 6:
                # A notification can be late; verify actual state before deciding.
                await closed_item(device, device.state['device_id'], ident)
            else:
                raise ValueError('未知补传响应，停止归档')
        if time.monotonic() - last_probe >= 2:
            await source.call('bind', await closed_item(device, device.state['device_id'], ident))
            last_probe = time.monotonic()


async def report_progress(progress, receipt):
    await progress(dict(message=f"增量同步 · 复用 {receipt['reusedFrames']} 帧 · 本次接收 {receipt['receivedBytes']} 字节 · {receipt['progress']}%",
                        progress=receipt['progress']))


async def download_incremental(device, original_id, output_dir, progress=None, idle_timeout=30):
    async with device.operation():
        ident, serial = int(original_id), device.state['device_id']
        session, address = device.state.get('session_id'), device.address
        device.state['export'] = receipt = dict(status='downloading', fileId=ident, progress=0,
                                                receivedBytes=0, reusedFrames=0, completedRanges=0)
        try:
            async with SourceWorker(device.frame_cache, serial, ident) as source:
                item = await closed_item(device, serial, ident)
                count = await source.call('bind', item)
                receipt['reusedFrames'] = await source.call('count')
                receipt['cachedFrames'] = receipt['reusedFrames']
                receipt['expectedBytes'] = count * WIRE
                while (plan := await source.call('plan', count)) is not None:
                    if receipt['completedRanges'] >= 4096:
                        raise ValueError('音频缺口过多，本轮停止；已保存音频保留')
                    if not device.client or not device.client.is_connected:
                        if not device.desired:
                            raise ValueError('连接已停止，增量同步取消')
                        await device._open(address, serial)
                        device.state['session_id'] = session
                    await source.call('bind', await closed_item(device, serial, ident))
                    if not device.crypto.session_key:
                        device.crypto.handshake((await device._exchange(0x2e, 1, device.crypto.public_key()))[9:-1])
                    # Hardware can start recording during the key exchange.
                    await source.call('bind', await closed_item(device, serial, ident))
                    try:
                        await read_range(device, ident, count, plan, source, idle_timeout, receipt, progress)
                    finally:
                        # Keep receiving until GATT is actually released, then count queued
                        # notifications too. This measures delivered wire audio, not air time.
                        try:
                            await device._close_client()
                        finally:
                            queue, device.transfer = device.transfer, None
                            while queue is not None and not queue.empty():
                                raw = queue.get_nowait()
                                if raw and raw[6] in (8, 0x12):
                                    receipt['receivedBytes'] += len(raw) - 10
                    receipt['completedRanges'] += 1
                    if progress:
                        await report_progress(progress, receipt)
                # Revalidate the finished source and restore the logical session for the
                # batch job's identity check. Supervisor remains blocked by operation().
                if not device.client or not device.client.is_connected:
                    if not device.desired:
                        raise ValueError('连接已停止，增量同步取消')
                    await device._open(address, serial)
                    device.state['session_id'] = session
                await source.call('bind', await closed_item(device, serial, ident))
                path = Path(output_dir) / f'{uuid.uuid4().hex}.ogg'
                await source.call('export', path, count, source.cancelled)
                check_cancelled()
                if not device.desired or not device.snapshot()['connected']:
                    raise ValueError('连接已停止，归档取消；已保存音频保留')
                receipt.update(status='complete', progress=100)
                return dict(path=path, device_type='feishu', device_id=serial, original_id=str(ident),
                            title=item['title'], recorded_at=datetime.fromtimestamp(ident, timezone.utc).isoformat(),
                            duration_ms=count * 20)
        except BaseException:
            receipt['status'] = 'failed'
            raise
