"""Recording workflows with durable intermediate results and edit preservation."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

from . import asr, providers


async def process_record(store, record, settings, work_dir: Path, progress, *,
                         mode='process', model_lock=None, asr_lock=None):
    # A job binds its original input; mutable caller dictionaries must not retarget it.
    record, settings = deepcopy(record), deepcopy(settings)
    if mode not in {'process', 'transcribe', 'summarize'}:
        raise ValueError('不支持的录音处理任务')
    provider = settings.get('provider')
    if provider not in {'api', 'codex', 'claude'}:
        raise ValueError('不支持的处理模式')
    if provider == 'api' and mode != 'transcribe':
        raise ValueError('API 模式由浏览器沿用原有 API 请求，请填写 API Key')
    if provider == 'api' and settings.get('asr') != 'local':
        raise ValueError('API 转写由浏览器发起；本地转写请先切换语音转写设置')
    if mode in {'process', 'summarize'} and record.get('summary_template') == 'custom' and not record.get('custom_prompt', '').strip():
        raise ValueError('请填写自定义整理要求')

    record_id = record['id']
    source = record.get('transcript', '')
    notices = []
    if mode == 'summarize' and not source.strip():
        raise ValueError('请先转写或粘贴逐字稿')

    if mode == 'transcribe' or (mode == 'process' and not source.strip()):
        await providers._notify(progress, {'stage': 'transcribing', 'message': '等待本地语音模型'})
        async with asr_lock if asr_lock is not None else nullcontext():
            recognized = await asr.transcribe(store.audio_path(record_id),
                                             model=settings['asr_model'], progress=progress)
        # Persist recognition before starting the CLI, so CLI failure/cancellation
        # never discards a completed local transcription.
        current = store.get_record(record_id)
        transcript_edited = current.get('transcript', '') != source
        patch = {'raw_transcript': recognized['text'], 'segments': recognized.get('segments', []),
                 'language': recognized.get('language', ''),
                 'duration_ms': recognized.get('duration', 0) * 1000}
        if not transcript_edited:
            patch.update(transcript=recognized['text'], transcript_provider='local')
        saved = store.patch_record(record_id, patch, internal=True)
        expected_transcript = saved.get('transcript', '')

        if provider != 'api':
            await providers._notify(progress, {'stage': 'correcting', 'message': '等待逐字稿校订任务'})
            try:
                async with model_lock if model_lock is not None else nullcontext():
                    corrected = await providers.correct_transcript(
                        provider, recognized['text'], model=settings.get(provider + '_model', ''),
                        work_dir=work_dir, progress=progress)
            except providers.ProviderError as error:
                raise providers.ProviderError(f'语音识别原稿已保存，逐字稿校订失败：{error}') from error
            current = store.get_record(record_id)
            transcript_edited = transcript_edited or current.get('transcript', '') != expected_transcript
            patch = {'generated_transcript': corrected, 'transcript_provider': provider}
            if not transcript_edited:
                patch['transcript'] = corrected
            saved = store.patch_record(record_id, patch, internal=True)
        if transcript_edited:
            notices.append('原始识别及校订结果已另存；保留了处理中手工修订的逐字稿。' if provider != 'api'
                           else '转写已保存为原稿；保留了处理中手工修订的逐字稿。')
        # Prefer a user's newer transcript over a generated one for the next step.
        source = saved.get('transcript', '')
        if mode == 'transcribe':
            return {'record': saved, 'notice': ' '.join(notices)}

    if not source.strip():
        raise ValueError('逐字稿为空，识别成果已保存；请确认逐字稿后再生成总结')
    await providers._notify(progress, {'stage': 'summarizing', 'message': '等待文字整理任务'})
    async with model_lock if model_lock is not None else nullcontext():
        text = await providers.summarize(
            provider, source, template=record.get('summary_template', 'meeting'),
            custom_prompt=record.get('custom_prompt', ''), model=settings.get(provider + '_model', ''),
            work_dir=work_dir, progress=progress)
    current = store.get_record(record_id)
    summary_edited = current.get('summary', '') != record.get('summary', '')
    source_edited = current.get('transcript', '') != source
    patch = {'generated_summary': text, 'summary_provider': provider, 'summary_source_transcript': source}
    if not summary_edited and not source_edited:
        patch['summary'] = text
    else:
        notices.append('生成总结已另存；保留了处理中手工修订的逐字稿和总结。')
    saved = store.patch_record(record_id, patch, internal=True)
    return {'record': saved, 'notice': ' '.join(notices)}
