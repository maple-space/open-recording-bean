"""Durable batches: page closure does not cancel a confirmed device operation."""
import asyncio
import hashlib
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class FeishuAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['download', 'delete', 'start', 'pause']
    device_id: str = Field(min_length=1, max_length=100)
    ids: list[str] = Field(default_factory=list, max_length=500)
    mode: Literal['backup', 'direct'] = 'backup'
    phrase: str = Field(default='', max_length=100)


def register_feishu_actions(app, store, manager, devices, lock, record_busy):
    def validate_connection(device_id, session_id):
        state = devices.snapshot('feishu')
        if not state.get('connected') or state.get('device_id') != device_id or state.get('session_id') != session_id:
            raise ValueError('设备连接已变化，本次操作停止；请确认设备后重新发起。')
        return state

    def fingerprint(record_id, device_id, original_id):
        record = store.get_record(record_id)
        if record['device_type'] != 'feishu' or record['device_id'] != device_id or record['original_id'] != original_id:
            raise ValueError('备份身份不匹配，未删除设备录音。')
        path = store.audio_path(record_id)
        if path.stat().st_size <= 0 or path.stat().st_size != record['size_bytes']:
            raise ValueError('本机备份不完整，未删除设备录音。')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        return digest.digest()

    @app.post('/api/devices/feishu/actions')
    async def submit(body: FeishuAction):
        if body.action in ('download', 'delete'):
            if not body.ids or len(set(body.ids)) != len(body.ids) or any(not n.isascii() or not n.isdecimal() or str(int(n)) != n or not 0 < int(n) <= 0xffffffff for n in body.ids):
                raise ValueError('请选择有效且不重复的设备录音。')
        if body.action == 'delete':
            required = '确认备份本地后删除' if body.mode == 'backup' else '确认从设备永久删除'
            if body.phrase != required:
                raise ValueError(f'请输入“{required}”确认。')
        state = devices.snapshot('feishu')
        session_id = state.get('session_id')
        validate_connection(body.device_id, session_id)
        file_ids = {row['original_id'] for row in state.get('files', [])}
        if any(n not in file_ids for n in body.ids):
            raise ValueError('设备目录中没有所选录音，请刷新列表。')
        record_ids = [store.record_id_for('feishu', body.device_id, n) for n in body.ids]
        if any(record_busy(rid) for rid in record_ids):
            raise HTTPException(409, '所选录音还有进行中的任务，请先完成或取消。')
        # Serialize intent too, so a second tab cannot queue a second recording
        # or delete while this device is still processing the first request.
        if any(manager.get(jid).get('device_type') == 'feishu' for jid, task in list(manager.tasks.items()) if not task.done()):
            raise HTTPException(409, '录音豆正在执行后台任务，请稍后再操作。')
        work_dir = store.root / 'tasks' / uuid.uuid4().hex

        async def archive(ident, rid, progress):
            validate_connection(body.device_id, session_id)
            try:
                record = store.get_record(rid)
            except KeyError:
                record = None
            if record:
                await asyncio.to_thread(fingerprint, rid, body.device_id, ident)
                return record
            data = await devices.download('feishu', ident, work_dir, progress=progress)
            validate_connection(body.device_id, session_id)
            if data['device_id'] != body.device_id or str(data['original_id']) != ident:
                raise ValueError('下载结果标识不匹配，未归档或删除。')
            return store.import_audio(data['path'], move=True, **{k: data[k] for k in (
                'device_type', 'device_id', 'original_id', 'title', 'recorded_at', 'duration_ms') if k in data})

        async def work(progress):
            count, records, digests = 0, [], []
            async with lock:
                validate_connection(body.device_id, session_id)
                work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                if body.action in ('start', 'pause'):
                    return await devices.feishu.record(body.action)
                try:
                    if body.action == 'download' or body.mode == 'backup':
                        for index, (ident, rid) in enumerate(zip(body.ids, record_ids)):
                            await progress(f'备份并核对录音 {index + 1}/{len(body.ids)}')
                            records.append(await archive(ident, rid, progress))
                            digests.append(await asyncio.to_thread(fingerprint, rid, body.device_id, ident))
                    if body.action == 'delete':
                        for index, (ident, rid) in enumerate(zip(body.ids, record_ids)):
                            if body.mode == 'backup':
                                digest = await asyncio.to_thread(fingerprint, rid, body.device_id, ident)
                                if digest != digests[index]:
                                    raise ValueError('本机备份已变化，停止删除。')
                            validate_connection(body.device_id, session_id)
                            await progress(f'删除设备录音 {index + 1}/{len(body.ids)}')
                            await devices.feishu.delete(ident)
                            count += 1
                    return dict(count=count if body.action == 'delete' else len(records),
                                records=records, record=records[-1] if records else None)
                except Exception as error:
                    raise ValueError(f'{"已删除 " + str(count) + " 条；" if count else ""}{error}') from None

        return manager.submit('device_delete' if body.action == 'delete' else 'record_' + body.action if body.action in ('start', 'pause') else 'download',
                              work, dict(device_type='feishu', device_id=body.device_id, record_ids=record_ids))
