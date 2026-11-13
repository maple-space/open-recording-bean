"""Service-owned Feishu BLE session. No browser lifetimes or automatic action replay."""
import asyncio
import copy
import struct
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from .feishu_protocol import SERVICE, WRITE, NOTIFY, Packets, DeviceCrypto, command, parse_info, parse_directory, decrypt, mux_ogg


class FeishuDevice:
    def __init__(self, ble_loader=None, reconnect_delays=(2, 5, 10, 30, 60)):
        self.ble_loader = ble_loader
        self.delays = reconnect_delays
        self.lock = asyncio.Lock()
        self.client = None
        self.desired = False
        self.address = None
        self.supervisor = None
        self.pending = {}
        self.transfer = None
        self.exclusive = None  # Reason text while realtime input owns the link.
        self.state = dict(connected=False, device_id='', device_name='', files=[], info={},
                          connection_status='disconnected', error=None, recordStatus=0, export=None)

    def _ble(self):
        if self.ble_loader:
            return self.ble_loader()
        from .devices import _load_ble
        return _load_ble()

    def snapshot(self):
        result = copy.deepcopy(self.state)
        result.update(connected=bool(self.client and self.client.is_connected and self.state['connected']),
                      desired_connected=self.desired, transport='local-service')
        return result

    async def scan(self):
        _, scanner = self._ble()
        found = await scanner.discover(timeout=8, return_adv=True)
        rows = []
        for device, advertisement in found.values():
            name = device.name or advertisement.local_name or ''
            services = {s.lower() for s in (advertisement.service_uuids or [])}
            if SERVICE in services or name.lower() == 'soundcore work':
                rows.append(dict(address=device.address, name=name or '录音豆'))
        return rows

    def _lost(self, client):
        if client is not self.client:
            return
        self.state.update(connected=False, connection_status='reconnecting' if self.desired else 'disconnected',
                          error='蓝牙连接已中断；正在等待原设备重新可用。' if self.desired else None)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ValueError('录音豆连接中断，本次操作未完成；不会自动重发。'))
        if self.transfer is not None:
            self.transfer.put_nowait(None)

    def _receive(self, source, data):
        if source is not self.client:
            return
        for raw in self.parser.feed(data):
            kind, ident = raw[5:7]
            if (kind, ident) == (0x18, 0x82) and len(raw) > 10 and raw[9] in (0, 1, 2):
                self.state['recordStatus'] = raw[9]
            future = self.pending.get((kind, ident))
            if future and not future.done():
                future.set_result(raw)
            # 6 announces the file being recorded; a live stream follows it when the recorder starts a new file.
            if self.transfer is not None and kind in (0x1a, 0x1b) and ident in (6, 7, 8, 0x12, 0x0a):
                self.transfer.put_nowait(raw)

    async def _write(self, kind, ident, payload=b''):
        if not self.client or not self.client.is_connected:
            raise ValueError('录音豆尚未连接，请等待重连或搜索设备。')
        await asyncio.wait_for(self.client.write_gatt_char(WRITE, command(kind, ident, payload), response=False), 10)

    async def _exchange(self, kind, ident, payload=b''):
        future = asyncio.get_running_loop().create_future()
        self.pending[kind, ident] = future
        try:
            await self._write(kind, ident, payload)
            return await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop((kind, ident), None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume an early disconnect if the write also failed.

    async def _close_client(self):
        client = self.client
        self.state['connected'] = False
        if client:
            await asyncio.wait_for(client.disconnect(), 10)
            if client.is_connected:
                raise ValueError('蓝牙连接尚未释放，请重试断开。')
        self.client = None

    async def _open(self, address, expected_sn=''):
        await self._close_client()
        client_type, scanner = self._ble()
        device = await scanner.find_device_by_address(address, timeout=12)
        if device is None:
            raise ValueError('未发现原录音豆，请唤醒设备并断开手机 App。')
        self.parser = Packets()
        self.crypto = DeviceCrypto()
        self.client = client_type(device, timeout=20, disconnected_callback=self._lost)
        client = self.client
        await client.connect()
        await client.start_notify(NOTIFY, lambda _, data: self._receive(client, data))
        raw = await self._exchange(1, 1)
        info = parse_info(raw[9:-1])
        if expected_sn and info['serialNumber'] != expected_sn:
            raise ValueError('重连设备的序列号不匹配，已停止自动连接。')
        self.state.update(info=info, device_id=info['serialNumber'], device_name=device.name or 'soundcore Work',
                          recordStatus=info.get('recordStatus', 0))
        await self._list()
        self.state.update(connected=True, connection_status='connected', error=None, session_id=uuid.uuid4().hex)

    async def connect(self, address):
        if not address:
            raise ValueError('请先搜索并选择要连接的录音豆。')
        if self.snapshot()['connected'] and self.address == address:
            return self.snapshot()
        await self.disconnect()
        async with self.lock:
            self.state.update(connection_status='connecting', files=[], export=None, error=None)
            try:
                await self._open(address)
            except BaseException:
                with suppress(Exception):
                    await self._close_client()
                self.state.update(connection_status='disconnected', error='连接失败，请检查蓝牙权限、设备电量和其他 App 占用。')
                raise
            self.address, self.desired = address, True
            self.supervisor = asyncio.create_task(self._maintain())
            return self.snapshot()

    async def _maintain(self):
        attempt = 0
        while self.desired:
            await asyncio.sleep(self.delays[min(attempt, len(self.delays) - 1)])
            if self.snapshot()['connected']:
                attempt = 0
                continue
            async with self.lock:
                if not self.desired:
                    return
                self.state['connection_status'] = 'reconnecting'
                try:
                    await self._open(self.address, self.state['device_id'])
                    attempt = 0
                except Exception as error:
                    with suppress(Exception):
                        await self._close_client()
                    if '序列号不匹配' in str(error):
                        self.desired = False
                    self.state.update(connection_status='reconnecting' if self.desired else 'disconnected',
                                      error=str(error) if isinstance(error, ValueError) else '暂时无法连接原录音豆，请检查设备及手机占用。')
                    attempt += 1

    async def disconnect(self):
        self.desired = False
        if self.supervisor:
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)
            self.supervisor = None
        async with self.lock:
            await self._close_client()
            self.state.update(connection_status='disconnected', error=None)
        return self.snapshot()

    @asynccontextmanager
    async def operation(self):
        if self.exclusive:  # Fail fast; never queue a download or delete behind hours of live audio.
            raise ValueError(self.exclusive)
        async with self.lock:
            if not self.snapshot()['connected']:
                raise ValueError('录音豆未连接，请等待重连后重新发起操作。')
            try:
                yield
            except BaseException:
                # Flush ambiguous notifications; the supervisor may reconnect,
                # but this operation is failed and is NEVER replayed.
                with suppress(Exception):
                    await self._close_client()
                self.state['connection_status'] = 'reconnecting' if self.desired else 'disconnected'
                raise

    async def _list(self):
        collected = {}
        for page in range(50):
            raw = await self._exchange(0x1b, 0x0e, struct.pack('<H', page))
            rows, count = parse_directory(raw[9:-1])
            collected.update({row['original_id']: row for row in rows})
            if count < 10:
                self.state['files'] = sorted((row for row in collected.values() if row['size_bytes']), key=lambda r: int(r['original_id']), reverse=True)
                return list(collected.values())
        raise ValueError('设备录音目录尚未读取完，不能确认完整列表')

    async def refresh(self):
        async with self.operation():
            raw = await self._exchange(1, 1)
            info = parse_info(raw[9:-1])
            if info['serialNumber'] != self.state['device_id']:
                raise ValueError('设备身份已变化')
            self.state.update(info=info, recordStatus=info.get('recordStatus', self.state['recordStatus']))
            await self._list()
            return self.snapshot()

    async def record(self, action):
        async with self.operation():
            target = {'start': 1, 'pause': 2}[action]
            raw = await self._exchange(0x18, 0x82, bytes([target]))
            if raw[9:10] != bytes([target]):  # 04.92 replies state + one echo byte (019c / 029d).
                raise ValueError('未收到预期录音状态，请刷新设备信息确认。')
            self.state['recordStatus'] = target
            return self.snapshot()

    async def delete(self, original_id):
        async with self.operation():
            if not any(row['original_id'] == str(original_id) for row in self.state['files']):
                raise ValueError('设备目录中没有此录音，请刷新确认。')
            raw = await self._exchange(0x1a, 0x10, struct.pack('<I', int(original_id)))
            if raw[4] & 15 != 1:
                raise ValueError(f'录音豆拒绝删除（状态码 {raw[4] & 15}）')
            await asyncio.sleep(.35)
            try:
                rows = await self._list()
            except Exception:
                raise ValueError('设备已回应删除成功，但目录核验失败；不会重发删除，请刷新确认。') from None
            if any(row['original_id'] == str(original_id) for row in rows):
                raise ValueError('设备已回应删除成功，但录音仍在目录中；请稍后刷新确认。')
            return self.snapshot()

    async def download(self, original_id, output_dir, progress=None, *, idle_timeout=3600):
        async with self.operation():
            item = next((row for row in self.state['files'] if row['original_id'] == str(original_id)), None)
            if item is None:
                raise ValueError('设备目录中没有此文件，请刷新。')
            if not self.crypto.session_key:
                raw = await self._exchange(0x2e, 1, self.crypto.public_key())
                self.crypto.handshake(raw[9:-1])
            queue = self.transfer = asyncio.Queue()
            ident = int(original_id)
            frames, secret = {}, None
            last_new = time.monotonic()
            last_progress = 0
            self.state['export'] = dict(status='downloading', fileId=ident, progress=0)
            try:
                await self._write(0x1a, 7, struct.pack('<IIB', 0, ident, 0))
                while True:
                    remaining = idle_timeout - (time.monotonic() - last_new)
                    try:
                        raw = await asyncio.wait_for(queue.get(), max(0, remaining))
                    except TimeoutError:
                        raise ValueError('文件读取超时：连续 60 分钟未收到新的音频数据，请检查蓝牙并唤醒录音豆。') from None
                    if raw is None:
                        raise ValueError('蓝牙已断开，下载中止；请等待重连后重新下载。')
                    cmd = raw[6]
                    if cmd == 7:
                        secret = self.crypto.file_secret(raw[9:-1], ident)
                    elif cmd in (8, 0x12):
                        if secret is None:
                            raise ValueError('收到音频分片但缺少已验证文件头')
                        for offset in range(9, len(raw) - 165, 166):
                            seq = int.from_bytes(raw[offset:offset + 4], 'little')
                            plain = decrypt(raw[offset + 5:offset + 165], secret[0], secret[1] + (seq * 10).to_bytes(4, 'big'))
                            if seq in frames:
                                if frames[seq] != plain:
                                    raise ValueError('音频分片内容冲突')
                                continue
                            frames[seq] = plain
                            last_new = time.monotonic()
                        received = len(frames) * 160
                        expected = secret[2]
                        percent = min(99, round(received * 100 / expected)) if expected else 0
                        self.state['export'] = dict(status='downloading', fileId=ident, progress=percent, receivedBytes=received, expectedBytes=expected)
                        if progress and time.monotonic() - last_progress >= 1:
                            await progress(dict(message=f'正在读取音频 · {percent}%（{received} 字节）', progress=percent))
                            last_progress = time.monotonic()
                    elif cmd == 0x0a:
                        break
                if not secret or not frames:
                    raise ValueError('设备没有返回完整可解密音频')
                ordered = sorted(frames)
                if ordered[0] not in (0, 1) or ordered != list(range(ordered[0], ordered[-1] + 1)):
                    raise ValueError('音频分片缺失，未写入资料库')
                path = Path(output_dir) / f'{uuid.uuid4().hex}.ogg'
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                audio = await asyncio.to_thread(mux_ogg, [frames[n] for n in ordered])
                with path.open('xb') as stream:
                    stream.write(audio)
                self.state['export'].update(status='complete', progress=100)
                return dict(path=path, device_type='feishu', device_id=self.state['device_id'],
                            original_id=str(original_id), title=item['title'],
                            recorded_at=datetime.fromtimestamp(int(original_id), timezone.utc).isoformat(), duration_ms=len(frames) * 20)
            except BaseException:
                self.state['export']['status'] = 'failed'
                raise
            finally:
                self.transfer = None
