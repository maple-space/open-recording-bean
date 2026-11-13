"""Loopback-only API and production web host for BeanMic."""
from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .jobs import JobManager
from .realtime_asr import MAX_CHUNK_BYTES, RealtimeASR
from .storage import AUDIO_SUFFIXES, Storage

PROJECT = Path(__file__).resolve().parents[1]
MAX_AUDIO = 1024 * 1024 * 1024


class JobRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['scan', 'connect', 'refresh', 'download', 'transcribe', 'summarize', 'process']
    device_type: Literal['feishu'] | None = None
    address: str | None = Field(default=None, max_length=200)
    original_id: str | None = Field(default=None, max_length=300)
    record_id: str | None = Field(default=None, max_length=100)


class GeneratedResultRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: Literal['transcript', 'summary']
    text: str = Field(max_length=2_000_000)
    baseline_transcript: str = Field(max_length=2_000_000)
    baseline_summary: str = Field(max_length=2_000_000)
    baseline_summary_template: Literal['meeting', 'memo', 'interview', 'custom'] = 'meeting'
    baseline_custom_prompt: str = Field(default='', max_length=2_000_000)


class RealtimeStartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str | None = Field(default=None, max_length=160)
    language: Literal['zh', 'en', 'auto'] = 'zh'


def create_app(data_dir: Path | None = None, *, devices=None, realtime=None):
    store = Storage(data_dir or Path(os.getenv('RECORDING_DATA_DIR', str(PROJECT / '.local' / 'data'))))
    realtime = realtime or RealtimeASR()
    if devices is None:
        from .devices import DeviceManager
        devices = DeviceManager()
    manager = JobManager(store)
    device_locks = {'feishu': asyncio.Lock()}
    model_lock = asyncio.Lock()
    asr_lock = asyncio.Lock()
    trashing = set()
    trash_operations = set()

    def record_busy(record_id):
        return record_id in trashing or any(manager.get(job_id).get('record_id') == record_id or record_id in manager.get(job_id).get('record_ids', [])
            for job_id, task in list(manager.tasks.items()) if not task.done())

    @asynccontextmanager
    async def lifespan(app):
        yield
        await live.stop()  # Pauses the recorder if realtime input is still on.
        await live._end_settling()  # The service is leaving; the link drops anyway.
        await realtime.close()
        await manager.shutdown()
        if trash_operations:
            await asyncio.gather(*list(trash_operations), return_exceptions=True)
        for kind in device_locks:
            try:
                await devices.disconnect(kind)
            except Exception:
                pass

    app = FastAPI(title='BeanMic', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.storage = store
    app.state.jobs = manager
    app.state.devices = devices
    app.state.realtime = realtime

    from .feishu_jobs import register_feishu_actions
    register_feishu_actions(app, store, manager, devices, device_locks['feishu'], record_busy)
    from .live import register_live
    live = register_live(app, manager, devices, realtime, store.root / 'live-transcripts')

    @app.middleware('http')
    async def loopback_only(request: Request, call_next):
        host = urlsplit('//' + request.headers.get('host', '')).hostname
        if host not in {'localhost', '127.0.0.1', '::1'}:
            return JSONResponse({'detail': '只允许从本机地址访问'}, status_code=403)
        if request.url.path.startswith('/api'):
            origin = request.headers.get('origin')
            if origin:
                parsed = urlsplit(origin)
                current = request.headers.get('host', '')
                allowed = {current, '127.0.0.1:5173', 'localhost:5173'}
                if parsed.scheme != 'http' or parsed.netloc not in allowed:
                    return JSONResponse({'detail': '拒绝非本地页面访问'}, status_code=403)
            elif request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': '拒绝跨站访问'}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith('/api'):
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.exception_handler(KeyError)
    async def not_found(request, error):
        return JSONResponse({'detail': '记录或任务不存在'}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)

    @app.get('/api/health')
    async def health():
        from .providers import provider_status
        from .asr import asr_status, describe_model
        asr = asr_status()
        configured = str(store.settings().get('asr_model') or '').strip()
        if configured:                                  # also when it is not installed: the page still names it in full
            asr['names'].setdefault(configured, describe_model(configured))
        return {'status': 'ok', 'providers': provider_status(), 'asr': asr,
                'devices': {kind: devices.snapshot(kind) for kind in device_locks}}

    @app.get('/api/settings')
    async def settings():
        return store.settings()

    @app.patch('/api/settings')
    async def settings_update(patch: dict):
        return store.update_settings(patch)

    @app.get('/api/records')
    async def records():
        return store.records()

    @app.post('/api/records/import')
    async def import_record(file: UploadFile = File(...), device_type: str = Form('local'),
                            device_id: str = Form('local'), original_id: str = Form(''),
                            title: str = Form(''), duration_ms: float = Form(0), recorded_at: str = Form('')):
        suffix = Path(file.filename or '').suffix.lower()
        if suffix not in AUDIO_SUFFIXES:
            raise ValueError('不支持的音频格式')
        if device_type not in {'feishu', 'local'}:
            raise ValueError('不支持的设备类型')
        original_id = original_id or uuid.uuid4().hex
        record_id = store.record_id_for(device_type, device_id, original_id)
        if record_busy(record_id):
            raise HTTPException(409, '这条录音正在处理或移入废纸篓，请稍后再导入')
        incoming = store.root / 'incoming'
        incoming.mkdir(exist_ok=True, mode=0o700)
        path = incoming / f'{uuid.uuid4().hex}{suffix}'
        count = 0
        try:
            with path.open('xb') as target:
                while chunk := await file.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_AUDIO:
                        raise HTTPException(413, '单个录音不能超过 1 GB')
                    target.write(chunk)
        finally:
            await file.close()
        if not count:
            raise ValueError('不能导入空音频')
        # Upload reads yield to other requests. Check again immediately before
        # the synchronous archive write, which has no intervening await.
        if record_busy(record_id):
            raise HTTPException(409, '这条录音正在处理或移入废纸篓，请稍后再导入')
        return store.import_audio(path, device_type=device_type, device_id=device_id,
                                  original_id=original_id,
                                  title=title or Path(file.filename or '录音').stem,
                                  recorded_at=recorded_at or None, duration_ms=max(0, duration_ms), move=True)

    @app.patch('/api/records/{record_id}')
    async def patch_record(record_id: str, patch: dict):
        if record_id in trashing:
            raise HTTPException(409, '录音正在移入废纸篓，请稍后重试')
        return store.patch_record(record_id, patch)

    @app.post('/api/records/{record_id}/generated-result')
    async def save_generated_result(record_id: str, body: GeneratedResultRequest):
        if record_id in trashing:
            raise HTTPException(409, '录音正在移入废纸篓，请稍后重试')
        if not body.text.strip():
            raise ValueError('模型未返回有效文字，请重试')
        # Compare and save without yielding, so another local request cannot
        # change the source between this check and the synchronous catalog write.
        current = store.get_record(record_id)
        source_changed = current.get('transcript', '') != body.baseline_transcript
        patch = {'generated_' + body.field: body.text, body.field + '_provider': 'api'}
        if body.field == 'transcript':
            applied = not source_changed
            notice = '转录结果已另存，保留了生成期间手工修订的逐字稿。'
        else:
            applied = not source_changed and current.get('summary', '') == body.baseline_summary \
                and current.get('summary_template', 'meeting') == body.baseline_summary_template \
                and current.get('custom_prompt', '') == body.baseline_custom_prompt
            patch['summary_source_transcript'] = body.baseline_transcript
            notice = '整理结果已另存，保留了生成期间修改的原文、总结和整理要求。'
        if applied:
            patch[body.field] = body.text
        saved = store.patch_record(record_id, patch, internal=True)
        return {'record': saved, 'applied': applied, 'notice': '' if applied else notice}

    @app.post('/api/records/{record_id}/trash')
    async def trash_record(record_id: str):
        store.get_record(record_id)
        if record_busy(record_id):
            raise HTTPException(409, '这条录音还有进行中的任务，请先完成或取消任务')
        trashing.add(record_id)
        operation = asyncio.create_task(asyncio.to_thread(store.trash_record, record_id))
        trash_operations.add(operation)

        def completed(task):
            trashing.discard(record_id)
            trash_operations.discard(task)
            if not task.cancelled():
                task.exception()  # Retrieve errors even if the HTTP caller left.

        operation.add_done_callback(completed)
        # Cancelling an HTTP request cannot stop its already running thread.
        # Keep the reservation until that thread finishes, including its DB write.
        return await asyncio.shield(operation)

    @app.get('/api/records/{record_id}/audio')
    async def get_audio(record_id: str):
        path = store.audio_path(record_id)
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                            filename=store.get_record(record_id)['title'] + path.suffix, content_disposition_type='inline')

    @app.get('/api/records/{record_id}/bundle')
    async def get_bundle(record_id: str):
        return Response(await asyncio.to_thread(store.bundle, record_id), media_type='application/zip',
                        headers={'Content-Disposition': f'attachment; filename="recording-{record_id[:8]}.zip"'})

    def device_kind(kind):
        if kind not in device_locks:
            raise HTTPException(404, '不存在的硬件入口')
        return kind

    @app.get('/api/devices/{kind}')
    async def device_state(kind: str):
        return devices.snapshot(device_kind(kind))

    @app.post('/api/devices/{kind}/disconnect')
    async def disconnect(kind: str):
        kind = device_kind(kind)
        async with device_locks[kind]:
            return await devices.disconnect(kind)

    # ---- live transcription of audio the page decodes from the recorder ----
    def realtime_missing():
        return HTTPException(404, '实时转写会话不存在或已结束，请重新开始')

    @app.get('/api/realtime/status')
    async def realtime_status():
        return realtime.status()

    @app.post('/api/realtime/sessions')
    async def realtime_start(body: RealtimeStartRequest):
        return await realtime.start(body.model, body.language)

    @app.post('/api/realtime/sessions/{session_id}/audio')
    async def realtime_audio(session_id: str, request: Request, cursor: int = 0):
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunks) + len(chunk) > MAX_CHUNK_BYTES:
                raise HTTPException(413, '单次上传的音频过大')
            chunks.extend(chunk)
        try:
            return await realtime.push(session_id, bytes(chunks), cursor)
        except KeyError:
            raise realtime_missing() from None

    @app.get('/api/realtime/sessions/{session_id}')
    async def realtime_snapshot(session_id: str, cursor: int = 0):
        try:
            return realtime.snapshot(session_id, cursor)
        except KeyError:
            raise realtime_missing() from None

    @app.post('/api/realtime/sessions/{session_id}/finish')
    async def realtime_finish(session_id: str, cursor: int = 0):
        try:
            return await realtime.finish(session_id, cursor)
        except KeyError:
            raise realtime_missing() from None

    @app.get('/api/jobs')
    async def jobs():
        return store.jobs()

    @app.get('/api/jobs/{job_id}')
    async def job(job_id: str):
        return manager.get(job_id)

    @app.post('/api/jobs/{job_id}/cancel')
    async def cancel_job(job_id: str):
        return await manager.cancel(job_id)

    @app.post('/api/jobs')
    async def start_job(body: JobRequest):
        settings = store.settings()  # Bind settings and text to this job, not future changes.
        record = None
        job_record_id = None
        download_device_id = None
        if body.type in {'summarize', 'transcribe', 'process'}:
            if not body.record_id:
                raise ValueError('请先选择已归档录音')
            record = store.get_record(body.record_id)
            job_record_id = record['id']
            if record_busy(body.record_id):
                raise HTTPException(409, '这条录音还有进行中的任务，请先完成或取消任务')
            if body.type in {'summarize', 'process'}:
                if settings['provider'] == 'api':
                    raise ValueError('API 模式由浏览器沿用原有 SiliconFlow 请求，请填写 API Key')
                if body.type == 'summarize' and not record.get('transcript', '').strip():
                    raise ValueError('请先转写或粘贴逐字稿')
                if record.get('summary_template') == 'custom' and not record.get('custom_prompt', '').strip():
                    raise ValueError('请填写自定义整理要求')
            elif settings['provider'] == 'api' and settings['asr'] != 'local':
                raise ValueError('API 转写由浏览器发起；本地转写请先切换语音转写设置')
        else:
            device_kind(body.device_type)
            if body.device_type == 'feishu' and any(manager.get(jid).get('device_type') == 'feishu'
                    for jid, task in list(manager.tasks.items()) if not task.done()):
                raise HTTPException(409, '录音豆正在执行后台任务，请稍后再操作')
            if body.type == 'download' and not body.original_id:
                raise ValueError('请先选择设备录音')
            if body.type == 'download':
                state = devices.snapshot(body.device_type)
                if not state.get('connected') or not state.get('device_id'):
                    raise ValueError('请先连接设备，再选择录音下载')
                download_device_id = str(state['device_id'])
                job_record_id = store.record_id_for(body.device_type, download_device_id, body.original_id)
                if record_busy(job_record_id):
                    raise HTTPException(409, '这条录音正在处理或移入废纸篓，请稍后再下载')
        work_dir = store.root / 'tasks' / uuid.uuid4().hex

        async def work(progress):
            work_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            if body.type in {'scan', 'connect', 'refresh', 'download'}:
                await progress('等待设备通道')
                async with device_locks[body.device_type]:
                    await progress('正在与设备通信，请保持设备开机')
                    if body.type == 'scan':
                        return await devices.scan(body.device_type)
                    if body.type == 'connect':
                        return await devices.connect(body.device_type, body.address)
                    if body.type == 'refresh':
                        return await devices.refresh(body.device_type)
                    state = devices.snapshot(body.device_type)
                    if not state.get('connected') or str(state.get('device_id')) != download_device_id:
                        raise ValueError('设备连接已变化，请重新选择设备录音后下载')
                    if job_record_id in trashing:
                        raise ValueError('录音正在移入废纸篓，请稍后再下载')
                    data = await devices.download(body.device_type, body.original_id, work_dir, progress=progress)
                    archive_id = store.record_id_for(data['device_type'], data['device_id'], data['original_id'])
                    if archive_id != job_record_id:
                        raise ValueError('下载结果与提交时的设备录音不一致，未写入资料库，请重新连接设备')
                    if job_record_id in trashing:
                        raise ValueError('录音正在移入废纸篓，下载文件已保留，请稍后重试')
                    saved = store.import_audio(data['path'], move=True, **{key: data[key] for key in (
                        'device_type', 'device_id', 'original_id', 'title', 'recorded_at', 'duration_ms') if key in data})
                    return {'record': saved}
            from .workflows import process_record
            return await process_record(store, record, settings, work_dir, progress,
                mode=body.type, model_lock=model_lock, asr_lock=asr_lock)

        metadata = {key: value for key, value in {'device_type': body.device_type, 'record_id': job_record_id,
                    'device_id': download_device_id,
                    'provider': settings['provider'] if record else None,
                    'model': settings.get(settings['provider'] + '_model', '') if record else None}.items() if value is not None}
        return manager.submit(body.type, work, metadata)

    @app.get('/{requested:path}')
    async def frontend(requested: str):
        requested = requested.rstrip('/')
        if requested == 'api' or requested.startswith('api/'):
            raise HTTPException(404, '接口不存在')
        dist = PROJECT / 'dist'
        candidate = (dist / requested).resolve()
        if candidate.is_relative_to(dist.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        if requested in {'', 'device', 'feishu', 'live', 'library', 'settings'} or re.fullmatch(r'live/records(/[\w.-]+)?', requested):
            if (dist / 'index.html').is_file():
                return FileResponse(dist / 'index.html', headers={'Cache-Control': 'no-cache'})
            return JSONResponse({'detail': '前端尚未构建，请运行 pnpm build 或使用本地开发服务器'}, status_code=503)
        raise HTTPException(404, '页面不存在')

    return app
