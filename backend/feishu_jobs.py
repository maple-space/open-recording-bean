"""Durable batches: page closure does not cancel a confirmed device operation."""
import asyncio
import hashlib
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .jobs import JobFailed
from .task_artifacts import discard_file, discard_empty_directory


class FeishuAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['download', 'delete', 'start', 'pause']
    device_id: str = Field(min_length=1, max_length=100)
    ids: list[str] = Field(default_factory=list, max_length=500)
    mode: Literal['backup', 'direct'] = 'backup'
    phrase: str = Field(default='', max_length=100)


class DownloadStopped(ValueError):
    """Connection intent changed; do not continue to another file."""


def register_feishu_actions(app, store, manager, devices, lock, record_busy):
    def connection_intent(device_id, generation):
        state = devices.snapshot('feishu')
        desired = getattr(devices.feishu, 'desired', state.get('desired_connected'))
        if (state.get('device_id') != device_id or desired is False
                or (generation is not None and state.get('connection_generation') != generation)):
            raise DownloadStopped('设备已断开或身份已变化，本次操作停止；连接原设备后可重新发起。')
        return state, desired

    def validate_connection(device_id, session_id, generation):
        state, _ = connection_intent(device_id, generation)
        if not state.get('connected') or state.get('session_id') != session_id:
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

    async def submit_action(body, *, resumed_from=None):
        if body.action in ('download', 'delete'):
            if not body.ids or len(set(body.ids)) != len(body.ids) or any(not n.isascii() or not n.isdecimal() or str(int(n)) != n or not 0 < int(n) <= 0xffffffff for n in body.ids):
                raise ValueError('请选择有效且不重复的设备录音。')
        if body.action == 'delete':
            required = '确认备份本地后删除' if body.mode == 'backup' else '确认从设备永久删除'
            if body.phrase != required:
                raise ValueError(f'请输入“{required}”确认。')
        state = devices.snapshot('feishu')
        session_id = state.get('session_id')
        connection_generation = state.get('connection_generation')
        validate_connection(body.device_id, session_id, connection_generation)
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

        def download_state():
            current, desired = connection_intent(body.device_id, connection_generation)
            # Older adapters have no reconnect intent, so cannot safely wait.
            if not current.get('connected') and desired is not True:
                raise DownloadStopped('设备连接已中断，同步已停止；连接原设备后可继续同步。')
            return current

        async def wait_for_download_connection(progress):
            announced = False
            while not download_state().get('connected'):
                if not announced:
                    await progress(dict(message='等待原录音豆自动重连，可随时取消', stage='reconnecting'))
                    announced = True
                # The device supervisor owns reconnecting. This task never connects.
                await asyncio.sleep(.25)

        async def archive(ident, rid, progress):
            if body.action == 'download':
                await wait_for_download_connection(progress)
            else:
                validate_connection(body.device_id, session_id, connection_generation)
            try:
                record = store.get_record(rid)
            except KeyError:
                record = None
            if record:
                await asyncio.to_thread(fingerprint, rid, body.device_id, ident)
                return record
            data = await devices.download('feishu', ident, work_dir, progress=progress)
            try:
                if body.action == 'download':
                    download_state()
                else:
                    validate_connection(body.device_id, session_id, connection_generation)
                if data['device_type'] != 'feishu' or data['device_id'] != body.device_id or str(data['original_id']) != ident:
                    raise ValueError('下载结果标识不匹配，未归档或删除。')
                return store.import_audio(data['path'], move=True, **{k: data[k] for k in (
                    'device_type', 'device_id', 'original_id', 'title', 'recorded_at', 'duration_ms') if k in data})
            finally:
                # Only the current task's output is ours to clean up. A moved
                # archive is already absent; unrelated adapter paths are kept.
                await asyncio.to_thread(discard_file, data.get('path'), parent=work_dir)

        async def download_batch(progress):
            records, completed_ids, failed_items, skipped_ids = [], [], [], []
            total = len(body.ids)

            def result():
                return dict(count=len(records), total=total, records=records,
                            record=records[-1] if records else None,
                            completed_ids=completed_ids, failed_items=failed_items,
                            skipped_ids=skipped_ids)

            try:
                for index, (ident, rid) in enumerate(zip(body.ids, record_ids), 1):
                    file_percent = 0

                    async def file_progress(value):
                        nonlocal file_percent
                        details = value if isinstance(value, dict) else dict(message=str(value))
                        if isinstance(details.get('progress'), (int, float)):
                            file_percent = max(0, min(100, int(details['progress'])))
                        text = details.get('message', '同步中')
                        await progress(dict(message=f'同步第 {index}/{total} 条 · 已完成 {len(completed_ids)} 条 · 当前 {file_percent}% · {text}',
                            stage=details.get('stage', 'download'), progress=file_percent,
                            total=total, current_index=index, current_id=ident,
                            file_progress=file_percent, completed_count=len(completed_ids),
                            completed_ids=list(completed_ids), failed_items=list(failed_items)))

                    await file_progress('核对本机归档')
                    download_state()
                    try:
                        try:
                            store.get_record(rid)
                            exists = True
                        except KeyError:
                            exists = False
                        record = await archive(ident, rid, file_progress)
                        await asyncio.to_thread(fingerprint, rid, body.device_id, ident)
                        download_state()
                        records.append(record)
                        completed_ids.append(ident)
                        if exists:
                            skipped_ids.append(ident)
                        await file_progress(dict(message='已核验并归档', progress=100))
                    except DownloadStopped:
                        raise
                    except ValueError as error:
                        # Only download tolerates a deterministic file failure.
                        # A user disconnect/device switch always stops the batch.
                        download_state()
                        failed_items.append(dict(original_id=ident, error=str(error)[:1000]))
                        await file_progress(dict(message=f'此条失败：{error}', stage='file_failed'))
                if failed_items:
                    raise JobFailed(f'已完成 {len(completed_ids)}/{total} 条，{len(failed_items)} 条失败；可继续同步。', result())
                return result()
            except JobFailed:
                raise
            except Exception as error:
                raise JobFailed(f'已完成 {len(completed_ids)}/{total} 条；{error}', result()) from None

        async def run_work(progress):
            count, records, digests = 0, [], []
            async with lock:
                work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                if body.action == 'download':
                    return await download_batch(progress)
                validate_connection(body.device_id, session_id, connection_generation)
                if body.action in ('start', 'pause'):
                    return await devices.feishu.record(body.action)
                try:
                    if body.mode == 'backup':
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
                            validate_connection(body.device_id, session_id, connection_generation)
                            await progress(f'删除设备录音 {index + 1}/{len(body.ids)}')
                            await devices.feishu.delete(ident)
                            count += 1
                            validate_connection(body.device_id, session_id, connection_generation)
                    return dict(count=count if body.action == 'delete' else len(records),
                                records=records, record=records[-1] if records else None)
                except Exception as error:
                    raise ValueError(f'{"已删除 " + str(count) + " 条；" if count else ""}{error}') from None

        async def work(progress):
            try:
                return await run_work(progress)
            finally:
                await asyncio.to_thread(discard_empty_directory, work_dir)

        metadata = dict(device_type='feishu', device_id=body.device_id, record_ids=record_ids)
        if body.action == 'download':
            metadata.update(retry_request=dict(device_id=body.device_id, ids=list(body.ids)),
                            total=len(body.ids), completed_count=0, completed_ids=[], failed_items=[])
            if resumed_from:
                metadata['resumed_from'] = resumed_from
        return manager.submit('device_delete' if body.action == 'delete' else 'record_' + body.action if body.action in ('start', 'pause') else 'download', work, metadata)

    @app.post('/api/devices/feishu/actions')
    async def submit(body: FeishuAction):
        return await submit_action(body)

    @app.post('/api/jobs/{job_id}/resume')
    async def resume(job_id: str):
        job = manager.get(job_id)
        request = job.get('retry_request')
        if (job.get('type') != 'download' or job.get('device_type') != 'feishu'
                or job.get('status') not in ('failed', 'cancelled') or not isinstance(request, dict)):
            raise HTTPException(409, '仅失败或已取消的录音同步任务可以继续。')
        body = FeishuAction(action='download', device_id=request.get('device_id', ''), ids=request.get('ids', []))
        return await submit_action(body, resumed_from=job_id)
