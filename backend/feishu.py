"""Service-owned Feishu BLE session. No browser lifetimes or automatic action replay."""
import asyncio
import copy
import struct
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from sys import platform

from .async_wait import wait_with_timeout
from .feishu_errors import DeviceIdentityError, TransferInterrupted
from .feishu_protocol import SERVICE, WRITE, NOTIFY, Packets, DeviceCrypto, command, parse_info, parse_directory, decrypt, mux_ogg


def _transport_error(error):
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, OSError)):
        return True
    # Preserve lazy BLE loading for services and tests that never use hardware.
    try:
        from bleak.exc import BleakError
    except ImportError:
        return False
    return isinstance(error, BleakError)


class FeishuDevice:
    def __init__(self, ble_loader=None, reconnect_delays=(2, 5, 10, 30, 60), frame_cache=None,
                 idle_probe_interval=30):
        self.ble_loader = ble_loader
        self.frame_cache = frame_cache
        self.delays = reconnect_delays
        self.lock = asyncio.Lock()
        self._operation_generation = None
        self.client = None
        self.desired = False
        self.address = None
        self.supervisor = None
        self.pending = {}
        self.transfer = None
        self.exclusive = None  # Reason text while realtime input owns the link.
        self.idle_probe_interval = idle_probe_interval
        self._last_activity = time.monotonic()
        self._closing_client = self._lost_client = None
        self._known_peripheral = None  # One identity-verified macOS BLEDevice for this service instance.
        self._rejected_recovery_addresses = set()  # Address-only exclusions for the current connection intent.
        self.state = dict(connected=False, device_id='', device_name='', files=[], info={},
                          connection_status='disconnected', connection_generation=0,
                          error=None, recordStatus=0, export=None,
                          connection_diagnostics=dict(counts={}, last_event=None, recent_events=[]))

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

    def _connection_event(self, event, reason):
        diagnostics = self.state['connection_diagnostics']
        counts = diagnostics['counts']
        counts[event] = counts.get(event, 0) + 1
        entry = dict(event=event, reason=reason, at=datetime.now(timezone.utc).isoformat())
        diagnostics['last_event'] = entry
        diagnostics['recent_events'] = (diagnostics['recent_events'] + [entry])[-20:]

    def _connection_intent(self):
        return (self.state['connection_generation'], self.desired, self.address, self.state['device_id'])

    def _check_connection_intent(self, expected):
        if self._connection_intent() != expected:
            raise DeviceIdentityError('连接操作已变化，原连接请求停止。')

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
        if client is not self.client or client is self._lost_client:
            return
        self._lost_client = client
        intentional = client is self._closing_client
        if not intentional:
            self._connection_event('unexpected_disconnect', 'transport_lost')
        self.state.update(connected=False, connection_status='reconnecting' if self.desired else 'disconnected',
                          error='蓝牙连接已中断；正在等待原设备重新可用。' if self.desired and not intentional else None)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(TransferInterrupted('录音豆连接中断，本次操作未完成。'))
        if self.transfer is not None:
            self.transfer.put_nowait(None)

    def _receive(self, source, data):
        if source is not self.client:
            return
        for raw in self.parser.feed(data):
            self._last_activity = time.monotonic()
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
            raise TransferInterrupted('录音豆尚未连接，请等待重连或搜索设备。')

        async def send():
            # The bounded wait schedules the coroutine separately: recheck at the actual
            # send boundary, after any stop intent published while it was queued.
            if self._operation_generation is not None and (
                    not self.desired or self.state['connection_generation'] != self._operation_generation):
                raise DeviceIdentityError('连接操作已变化，本次操作停止。')
            await self.client.write_gatt_char(WRITE, command(kind, ident, payload), response=False)

        try:
            await wait_with_timeout(send(), 10)
        except Exception as error:
            if not _transport_error(error):
                raise
            raise TransferInterrupted('蓝牙指令发送失败或超时，请等待连接恢复。') from error
        self._last_activity = time.monotonic()

    async def _exchange(self, kind, ident, payload=b''):
        future = asyncio.get_running_loop().create_future()
        self.pending[kind, ident] = future
        try:
            await self._write(kind, ident, payload)
            return await wait_with_timeout(future, 10)
        except (asyncio.TimeoutError, TimeoutError) as error:
            raise TransferInterrupted('录音豆回应超时，请等待连接恢复。') from error
        finally:
            self.pending.pop((kind, ident), None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume an early disconnect if the write also failed.

    async def _close_client(self, reason='operation_cleanup'):
        client = self.client
        self.state['connected'] = False
        if client:
            self._closing_client = client
            if client.is_connected:
                self._connection_event('intentional_disconnect', reason)
            try:
                await wait_with_timeout(client.disconnect(), 10)
                if client.is_connected:
                    raise TransferInterrupted('蓝牙连接尚未释放，请重试断开。')
            except Exception as error:
                self._connection_event('close_failed', type(error).__name__)
                if not _transport_error(error):
                    raise
                raise TransferInterrupted('蓝牙连接释放失败或超时，请等待连接恢复。') from error
            else:
                self._closing_client = None
        self.client = None

    async def _connect_identity(self, device, expected_sn, intent, client_type):
        """Only identify a candidate; no directory, download, or recorder action."""
        self._check_connection_intent(intent)
        self.parser = Packets()
        self.crypto = DeviceCrypto()
        self.client = client_type(device, timeout=20, disconnected_callback=self._lost)
        client = self.client
        try:
            await client.connect()
            self._check_connection_intent(intent)
            await client.start_notify(NOTIFY, lambda _, data: self._receive(client, data))
            self._check_connection_intent(intent)
        except Exception as error:
            self._known_peripheral = None
            if not _transport_error(error):
                raise
            raise TransferInterrupted('暂时无法连接原录音豆，请检查设备及手机占用。') from error
        raw = await self._exchange(1, 1)
        self._check_connection_intent(intent)
        try:
            info = parse_info(raw[9:-1])
        except ValueError as error:
            raise DeviceIdentityError(str(error)) from error
        if expected_sn and info['serialNumber'] != expected_sn:
            self._known_peripheral = None
            raise DeviceIdentityError('重连设备的序列号不匹配，已停止自动连接。')
        return info

    async def _open(self, address, expected_sn=''):
        intent = self._connection_intent()
        if expected_sn and self.state['device_id'] and (
                not self.desired or self.address != address or self.state['device_id'] != expected_sn):
            raise DeviceIdentityError('连接目标已变化，原连接请求停止。')
        await self._close_client()
        self._check_connection_intent(intent)
        client_type, scanner = self._ble()
        system_lookup = platform == 'darwin' and self.ble_loader is None

        async def advertised_device():
            try:
                device = await scanner.find_device_by_address(address, timeout=12)
            except Exception as error:
                if not _transport_error(error):
                    raise
                raise TransferInterrupted('暂时无法搜索原录音豆，请检查蓝牙连接。') from error
            self._check_connection_intent(intent)
            return device

        async def original_device():
            cached = self._known_peripheral
            if (system_lookup and cached and expected_sn
                    and cached[0] == address.lower() and cached[2] == expected_sn):
                return cached[1], True
            device = await advertised_device()
            if device is None and system_lookup:
                try:
                    from .feishu_discovery import retrieve_known_macos_device
                    device = await retrieve_known_macos_device(address)
                except Exception:
                    # The optional private Bleak adapter can change independently
                    # of normal discovery. Only this path tolerates such errors.
                    self._known_peripheral = None
                    self._check_connection_intent(intent)
                    return await advertised_device(), False
                self._check_connection_intent(intent)
                return device, True
            return device, False

        async def identify(device):
            if device is None:
                raise TransferInterrupted('未发现原录音豆，请唤醒设备并断开手机 App。')
            return await self._connect_identity(device, expected_sn, intent, client_type)

        try:
            device, from_system = await original_device()
            try:
                info = await identify(device)
            except Exception as error:
                if from_system:
                    self._known_peripheral = None
                if not from_system or isinstance(error, DeviceIdentityError) or device is None:
                    raise
                self._check_connection_intent(intent)
                await self._close_client(reason='known_peripheral_fallback')
                self._check_connection_intent(intent)
                device = await advertised_device()
                info = await identify(device)
        except TransferInterrupted:
            # A macOS peripheral UUID can rotate. Only an already-bound serial
            # authorizes probing a few recorder advertisements for that identity.
            if not expected_sn or not callable(getattr(scanner, 'discover', None)):
                raise
            self._check_connection_intent(intent)
            await self._close_client(reason='address_recovery')
            self._check_connection_intent(intent)
            try:
                found = await scanner.discover(timeout=15, return_adv=True)
            except Exception as error:
                if not _transport_error(error):
                    raise
                raise TransferInterrupted('重新搜索原录音豆失败，请等待设备重新可用。') from error
            self._check_connection_intent(intent)
            tried, seen, matched = 0, {address.lower()}, False
            for candidate, advertisement in found.values():
                name = candidate.name or advertisement.local_name or ''
                services = {service.lower() for service in (advertisement.service_uuids or [])}
                if (candidate.address.lower() in seen
                        or candidate.address.lower() in self._rejected_recovery_addresses
                        or (SERVICE not in services and name.lower() != 'soundcore work')):
                    continue
                seen.add(candidate.address.lower())
                tried += 1
                try:
                    info = await self._connect_identity(candidate, expected_sn, intent, client_type)
                except (TransferInterrupted, DeviceIdentityError) as error:
                    self._check_connection_intent(intent)  # An expired intent must never advance to another candidate.
                    if isinstance(error, DeviceIdentityError):
                        self._rejected_recovery_addresses.add(candidate.address.lower())
                    await self._close_client(reason='candidate_rejected')
                    self._check_connection_intent(intent)
                else:
                    device, matched = candidate, True
                    break
                if tried >= 3:
                    break
            if not matched:
                raise TransferInterrupted('尚未找到原序列号的录音豆，请唤醒设备并等待重连。')
        await self._list()
        self._check_connection_intent(intent)
        self.state.update(info=info, device_id=info['serialNumber'], device_name=device.name or 'soundcore Work',
                          recordStatus=info.get('recordStatus', 0))
        self.state.update(connected=True, connection_status='connected', error=None, session_id=uuid.uuid4().hex)
        if expected_sn:
            self.address = device.address
            if device.address.lower() != address.lower():
                self._connection_event('address_recovered', 'serial_verified')
        if system_lookup:
            self._known_peripheral = (device.address.lower(), device, info['serialNumber'])
        self._connection_event('connected', 'reconnect' if expected_sn else 'connect')

    async def connect(self, address):
        if not address:
            raise ValueError('请先搜索并选择要连接的录音豆。')
        self._rejected_recovery_addresses.clear()
        if self.snapshot()['connected'] and self.address == address:
            return self.snapshot()
        self.state['connection_generation'] += 1
        generation = self.state['connection_generation'] + 1  # disconnect() advances it once more
        await self.disconnect()
        if self.state['connection_generation'] != generation:
            raise DeviceIdentityError('连接操作已变化，原连接请求停止。')
        async with self.lock:
            if self.state['connection_generation'] != generation:
                raise DeviceIdentityError('连接操作已变化，原连接请求停止。')
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

    def _idle_probe_due(self):
        return (self.transfer is None and not self.exclusive
                and time.monotonic() - self._last_activity >= self.idle_probe_interval)

    async def _maintain(self):
        attempt = 0
        while self.desired:
            await asyncio.sleep(self.delays[min(attempt, len(self.delays) - 1)])
            if self.snapshot()['connected']:
                attempt = 0
                if self.lock.locked() or not self._idle_probe_due():
                    continue
            async with self.lock:
                if not self.desired:
                    return
                if self.snapshot()['connected']:
                    attempt = 0
                    if not self._idle_probe_due():
                        continue  # an operation restored or used the link while we waited
                    self._connection_event('idle_probe', 'idle_info_check')
                    try:
                        info = parse_info((await self._exchange(1, 1))[9:-1])
                        if info['serialNumber'] != self.state['device_id']:
                            raise DeviceIdentityError('设备身份已变化，已停止自动连接。')
                        self.state.update(info=info, recordStatus=info.get('recordStatus', self.state['recordStatus']))
                    except Exception as error:
                        self._connection_event('idle_probe_failed', type(error).__name__)
                        if isinstance(error, DeviceIdentityError):
                            self.desired = False
                        with suppress(Exception):
                            await self._close_client(reason='idle_probe_failed')
                        self.state.update(connection_status='reconnecting' if self.desired else 'disconnected',
                                          error=str(error) if isinstance(error, ValueError) else '录音豆连接检查失败，正在恢复。')
                    continue
                self.state['connection_status'] = 'reconnecting'
                self._connection_event('reconnect_attempt', 'transport_recovery')
                try:
                    await self._open(self.address, self.state['device_id'])
                    self._connection_event('reconnect_succeeded', 'transport_recovery')
                    attempt = 0
                except Exception as error:
                    with suppress(Exception):
                        await self._close_client()
                    if isinstance(error, DeviceIdentityError):
                        self.desired = False
                    self._connection_event('reconnect_failed', type(error).__name__)
                    self.state.update(connection_status='reconnecting' if self.desired else 'disconnected',
                                      error=str(error) if isinstance(error, ValueError) else '暂时无法连接原录音豆，请检查设备及手机占用。')
                    attempt += 1

    def request_disconnect(self):
        """Publish stop intent synchronously, before callers wait for job locks."""
        self.state['connection_generation'] += 1
        self.desired = False
        self._rejected_recovery_addresses.clear()

    async def disconnect(self):
        self.request_disconnect()
        if self.supervisor:
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)
            self.supervisor = None
        async with self.lock:
            await self._close_client(reason='disconnect_requested')
            self.state.update(connection_status='disconnected', error=None)
        return self.snapshot()

    @asynccontextmanager
    async def operation(self):
        if self.exclusive:  # Fail fast; never queue a download or delete behind hours of live audio.
            raise ValueError(self.exclusive)
        async with self.lock:
            if not self.desired or not self.snapshot()['connected']:
                raise ValueError('录音豆未连接，请等待重连后重新发起操作。')
            self._operation_generation = self.state['connection_generation']
            try:
                yield
            except BaseException as error:
                # Flush ambiguous notifications; the supervisor may reconnect,
                # but this operation is failed and is NEVER replayed.
                if isinstance(error, DeviceIdentityError):
                    self.desired = False
                with suppress(Exception):
                    await self._close_client()
                self.state['connection_status'] = 'reconnecting' if self.desired else 'disconnected'
                raise
            finally:
                self._operation_generation = None

    async def _list(self):
        intent = self._connection_intent()
        collected = {}
        for page in range(50):
            raw = await self._exchange(0x1b, 0x0e, struct.pack('<H', page))
            self._check_connection_intent(intent)
            rows, count = parse_directory(raw[9:-1])
            if page == 0:
                tail = 9 + 2 + count * 12
                self.state['active_file_id'] = int.from_bytes(raw[tail:tail + 4], 'little')
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
                raise DeviceIdentityError('设备身份已变化')
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
            generation = self.state['connection_generation']

            def stopped():
                return not self.desired or self.state['connection_generation'] != generation

            if not any(row['original_id'] == str(original_id) for row in self.state['files']):
                raise ValueError('设备目录中没有此录音，请刷新确认。')
            raw = await self._exchange(0x1a, 0x10, struct.pack('<I', int(original_id)))
            if raw[4] & 15 != 1:
                raise ValueError(f'录音豆拒绝删除（状态码 {raw[4] & 15}）')
            # The already-sent delete was acknowledged. Preserve that result,
            # but a stop request forbids issuing its follow-up directory query.
            if stopped():
                return self.snapshot()
            await asyncio.sleep(.35)
            if stopped():
                return self.snapshot()
            try:
                rows = await self._list()
            except Exception:
                if stopped():
                    return self.snapshot()
                raise ValueError('设备已回应删除成功，但目录核验失败；不会重发删除，请刷新确认。') from None
            if any(row['original_id'] == str(original_id) for row in rows):
                raise ValueError('设备已回应删除成功，但录音仍在目录中；请稍后刷新确认。')
            return self.snapshot()

    async def download(self, original_id, output_dir, progress=None, *, idle_timeout=3600):
        if self.frame_cache is not None:
            from .feishu_incremental import download_incremental
            return await download_incremental(self, original_id, output_dir, progress, min(idle_timeout, 30))
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
                        raw = await wait_with_timeout(queue.get(), max(0, remaining))
                    except (asyncio.TimeoutError, TimeoutError):
                        raise TransferInterrupted('文件读取超时：持续未收到新的音频数据，请检查蓝牙并唤醒录音豆。') from None
                    if raw is None:
                        raise TransferInterrupted('蓝牙已断开，下载中止；请等待连接恢复。')
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
