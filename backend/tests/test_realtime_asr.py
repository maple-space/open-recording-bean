import asyncio
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.realtime_asr import RealtimeASR, RealtimeASRError, SAMPLE_RATE, UtteranceSegmenter


class NoDevices:
    def snapshot(self, kind):
        return {'connected': False, 'files': [], 'identity_loaded': False}

    async def disconnect(self, kind):
        return self.snapshot(kind)


def tone(seconds, amplitude=0.2, frequency=220):
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)


def room(seconds, amplitude=0.002, seed=1):
    return (amplitude * np.random.default_rng(seed).standard_normal(int(seconds * SAMPLE_RATE))).astype(np.float32)


def pcm(samples):
    return (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()


# ---- segmentation -------------------------------------------------------- #
def test_utterance_ends_after_a_pause_and_keeps_a_little_context():
    segmenter = UtteranceSegmenter()
    found = segmenter.feed(np.concatenate([room(1.0), tone(1.5), room(1.2, seed=2)]))
    assert len(found) == 1
    utterance = found[0]
    assert 0.6 <= utterance.start <= 0.8          # 0.3 s of pre-roll before the first word
    assert 2.6 <= utterance.end <= 2.8            # 0.2 s of tail after the last word
    assert len(utterance.samples) == round((utterance.end - utterance.start) * SAMPLE_RATE)
    assert segmenter.in_progress() is None


def test_clicks_and_very_short_noises_are_not_speech():
    segmenter = UtteranceSegmenter()
    signal = np.concatenate([room(1.0), tone(0.04), room(1.0, seed=2), tone(0.1), room(1.5, seed=3)])
    assert segmenter.feed(signal) == []
    assert segmenter.flush() == []


def test_two_sentences_are_returned_separately_and_in_order():
    segmenter = UtteranceSegmenter()
    found = segmenter.feed(np.concatenate([room(0.5), tone(1.0), room(1.0, seed=2), tone(1.2), room(1.0, seed=3)]))
    assert len(found) == 2
    assert found[0].end <= found[1].start
    assert 0.9 <= found[0].end - found[0].start <= 1.7
    assert 1.1 <= found[1].end - found[1].start <= 1.9


def test_chunk_boundaries_do_not_change_the_result():
    signal = np.concatenate([room(0.7), tone(1.3), room(0.9, seed=2), tone(0.8), room(1.1, seed=3)])
    whole = UtteranceSegmenter().feed(signal)
    pieces, segmenter, offset = [], UtteranceSegmenter(), 0
    for size in [1, 37, 320, 333, 4000, 15, 16000, 640, 7] * 40:
        if offset >= len(signal):
            break
        pieces.extend(segmenter.feed(signal[offset:offset + size]))
        offset += size
    assert [(round(u.start, 2), round(u.end, 2)) for u in pieces] == [(round(u.start, 2), round(u.end, 2)) for u in whole]


def test_nonstop_speech_is_cut_at_the_quietest_recent_moment():
    segmenter = UtteranceSegmenter(max_utterance_s=6.0)
    speech = np.concatenate([tone(4.5), tone(0.1, amplitude=0.03), tone(4.0)])
    found = segmenter.feed(np.concatenate([room(0.5), speech]))
    assert len(found) == 1
    assert abs(found[0].end - (0.5 + 4.55)) <= 0.15   # the cut lands inside the brief dip
    rest = segmenter.in_progress()
    assert rest is not None and abs(rest[0] - found[0].end) < 0.03
    assert len(segmenter.flush()) == 1


def test_long_talk_settles_at_a_breath_while_a_short_phrase_still_waits_for_a_real_pause():
    # Three 2.5 s clauses with 0.3 s breaths between them, then a real pause.
    signal = np.concatenate([room(1.0), tone(2.5), room(0.3, seed=2), tone(2.5), room(0.3, seed=3), tone(2.5), room(1.2, seed=4)])

    def spans(segmenter):
        found = []
        for offset in range(0, len(signal), 1600):
            found += segmenter.feed(signal[offset:offset + 1600])
        return [(round(item.start, 1), round(item.end, 1)) for item in found + segmenter.flush()]

    assert len(spans(UtteranceSegmenter())) == 1                       # without the rule: one 8.6 s block
    soft = spans(UtteranceSegmenter(soft_limit_s=4.0))
    assert len(soft) == 2
    # The first breath comes after 2.5 s of speech, under the limit: not a sentence end.
    # The second comes after 5.3 s: the text can settle there instead of 3 s later.
    assert 6.0 <= soft[0][1] <= 6.7 and soft[1][0] <= soft[0][1] + 0.1 and soft[1][1] >= 9.0


def test_noise_floor_adapts_to_a_louder_room():
    segmenter = UtteranceSegmenter()
    found = segmenter.feed(np.concatenate([room(3.0, amplitude=0.02), tone(1.0, amplitude=0.4), room(2.0, amplitude=0.02, seed=2)]))
    after_calibration = [u for u in found if u.end > 2.0]
    assert len(after_calibration) == 1
    assert 2.5 <= after_calibration[0].start <= 3.0 and after_calibration[0].end <= 4.5
    assert segmenter.in_progress() is None       # steady noise is not held open as speech


def test_flush_returns_the_open_utterance():
    segmenter = UtteranceSegmenter()
    assert segmenter.feed(np.concatenate([room(0.5), tone(1.0)])) == []
    assert segmenter.in_progress() is not None
    assert len(segmenter.flush()) == 1
    assert segmenter.in_progress() is None


# ---- session manager with the real worker process and a fake engine ------- #
async def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError('condition not reached in time')


async def push_stream(asr, session_id, samples, chunk_seconds=0.1):
    step = int(chunk_seconds * SAMPLE_RATE)
    for offset in range(0, len(samples), step):
        await asr.push(session_id, pcm(samples[offset:offset + step]))


async def test_session_gives_a_draft_while_speaking_and_final_text_after_the_pause():
    asr = RealtimeASR(fake_engine=True)
    try:
        session = await asr.start(language='zh')
        assert session['segments'] == [] and session['model'] == 'fake'
        await push_stream(asr, session['id'], np.concatenate([room(0.5), tone(2.0)]))
        draft = await wait_for(lambda: asr.snapshot(session['id'])['partial'])
        assert draft['text'].startswith('草稿')
        await push_stream(asr, session['id'], room(1.2, seed=2))
        state = await wait_for(lambda: asr.snapshot(session['id']) if asr.snapshot(session['id'])['segment_count'] else None)
        assert state['segments'][0]['text'].startswith('语音')
        assert state['segments'][0]['index'] == 0 and 0 < state['segments'][0]['start'] < state['segments'][0]['end']
        assert state['partial'] is None
        assert asr.snapshot(session['id'], cursor=1)['segments'] == []
        final = await asr.finish(session['id'])
        assert final['closed'] and final['text'] == state['segments'][0]['text']
        assert final['received_seconds'] == pytest.approx(3.7, abs=0.05)
    finally:
        await asr.close()


async def test_finish_transcribes_speech_that_had_no_trailing_pause():
    asr = RealtimeASR(fake_engine=True)
    try:
        session = await asr.start()
        await push_stream(asr, session['id'], np.concatenate([room(0.4), tone(1.5)]))
        final = await asr.finish(session['id'])
        assert final['segment_count'] == 1 and final['error'] is None
    finally:
        await asr.close()


async def test_a_new_session_replaces_the_old_one_and_bad_input_is_rejected():
    asr = RealtimeASR(fake_engine=True)
    try:
        first = await asr.start()
        second = await asr.start(language='auto')
        assert first['id'] != second['id']
        with pytest.raises(KeyError):
            await asr.push(first['id'], pcm(room(0.1)))
        with pytest.raises(RealtimeASRError):
            await asr.push(second['id'], b'\x00\x01\x02')
        with pytest.raises(RealtimeASRError):
            await asr.start(language='klingon')
        assert asr.status()['active_session'] == second['id']   # a rejected request leaves the running session alone
    finally:
        await asr.close()


async def test_idle_session_is_closed_automatically():
    asr = RealtimeASR(fake_engine=True, idle_timeout=0.3)
    try:
        session = await asr.start()
        await wait_for(lambda: asr._session.closed, timeout=5)
        assert '长时间没有收到音频' in asr._session.error
        with pytest.raises(RealtimeASRError):
            await asr.push(session['id'], pcm(room(0.1)))
    finally:
        await asr.close()


def test_status_lists_engines_and_explains_what_is_missing(monkeypatch):
    import backend.asr as asr_module
    import backend.realtime_asr as live
    monkeypatch.setattr(asr_module, 'asr_status', lambda: {'installed': False, 'models': [], 'message': '未安装本地转写依赖。', 'downloads_automatically': False})
    monkeypatch.setattr(live, '_sensevoice_files', lambda: None)
    status = RealtimeASR().status('small')
    assert status['ready'] is False and status['default_model'] is None and '不会自动下载' in status['message']
    assert [row['id'] for row in status['engines']] == ['sensevoice-small', 'small', 'base', 'turbo']
    assert all(not row['available'] and row['missing'] for row in status['engines'])


def test_fastest_available_engine_is_the_default_and_an_explicit_choice_wins(monkeypatch, tmp_path):
    import backend.asr as asr_module
    import backend.realtime_asr as live
    monkeypatch.setattr(asr_module, 'asr_status', lambda: {'installed': True, 'models': ['small'], 'message': 'ok', 'downloads_automatically': False})
    monkeypatch.setattr(asr_module, '_cached_model_path', lambda model: tmp_path if model == 'small' else None)
    monkeypatch.setattr(live, '_sensevoice_files', lambda: None)
    assert live.choose_model(None) == 'small'
    assert live.choose_model('turbo') == 'small'            # not cached: fall back instead of failing
    (tmp_path / 'model.int8.onnx').write_bytes(b'x'); (tmp_path / 'tokens.txt').write_text('a 0')
    monkeypatch.setattr(live, '_sensevoice_files', lambda: (tmp_path / 'model.int8.onnx', tmp_path / 'tokens.txt'))
    monkeypatch.setattr(live.importlib.util, 'find_spec', lambda name: object())
    assert live.choose_model(None) == 'sensevoice-small'
    assert live.choose_model('small') == 'small'
    spec = live._engine_spec('sensevoice-small')
    assert spec['engine'] == 'sensevoice' and spec['model'].endswith('model.int8.onnx')
    # Timing measured on real-speed speech: drafts from the first half second, sentences settle at a breath.
    assert (spec['draft_min'], spec['draft_gap'], spec['soft_limit'], spec['soft_silence']) == (0.5, 0.4, 4.0, 0.2)


# ---- HTTP API ------------------------------------------------------------- #
def test_realtime_api_round_trip_and_limits(tmp_path):
    app = create_app(tmp_path, devices=NoDevices(), realtime=RealtimeASR(fake_engine=True))
    with TestClient(app, base_url='http://127.0.0.1') as client:
        status = client.get('/api/realtime/status').json()
        assert status['ready'] is True and status['languages'] == ['zh', 'en', 'auto']
        assert client.post('/api/realtime/sessions', json={'language': 'fr'}).status_code == 422
        assert client.post('/api/realtime/sessions', json={'language': 'zh'}, headers={'Origin': 'https://example.com'}).status_code == 403
        session = client.post('/api/realtime/sessions', json={'language': 'zh'}).json()
        url = f"/api/realtime/sessions/{session['id']}"
        signal = np.concatenate([room(0.5), tone(1.5), room(1.2, seed=2)])
        for offset in range(0, len(signal), 8000):
            response = client.post(url + '/audio', content=pcm(signal[offset:offset + 8000]), headers={'Content-Type': 'application/octet-stream'})
            assert response.status_code == 200, response.text
        deadline = time.monotonic() + 10
        state = client.get(url).json()
        while not state['segment_count'] and time.monotonic() < deadline:
            time.sleep(0.05)
            state = client.get(url).json()
        assert state['segments'][0]['text'].startswith('语音')
        assert client.get(url, params={'cursor': 1}).json()['segments'] == []
        assert client.post(url + '/audio', content=b'\x01').status_code == 400
        assert client.post(url + '/audio', content=b'\0' * (4 * 1024 * 1024 + 2)).status_code == 413
        assert client.post('/api/realtime/sessions/unknown/audio', content=b'\0\0').status_code == 404
        finished = client.post(url + '/finish').json()
        assert finished['closed'] and finished['text'].startswith('语音')
        assert client.post(url + '/audio', content=b'\0\0').status_code == 400
        assert client.get('/live').status_code in {200, 503}


@pytest.mark.asyncio
async def test_voiceprints_are_made_only_when_asked_for_and_handed_over_once():
    live = RealtimeASR(fake_engine=True)
    tone = (np.sin(2 * np.pi * 440 * np.arange(16000 * 2) / 16000) * 9000).astype('<i2').tobytes()
    quiet = np.zeros(16000 * 2, '<i2').tobytes()
    try:
        assert live.status()['speakers'] == dict(available=True, model=None)
        plain = await live.start(None, 'zh')
        assert plain['speakers'] is False
        done = await live.finish((await live.push(plain['id'], tone + quiet))['id'], voices=True)
        assert done['segments'] and all(row['voice'] is None for row in done['segments'])      # not asked for: none made
        session = await live.start(None, 'zh', speakers=True)
        assert session['speakers'] is True
        await live.push(session['id'], tone + quiet)
        for _ in range(200):
            if live.snapshot(session['id'])['segment_count']:
                break
            await asyncio.sleep(0.02)
        public = live.snapshot(session['id'])
        assert public['segment_count'] == 1 and 'voice' not in public['segments'][0]            # never part of the ordinary answer
        taken = live.snapshot(session['id'], voices=True)
        assert taken['segments'][0]['voice'] == [1.0, 0.0, 0.0]
        assert live.snapshot(session['id'], voices=True)['segments'][0]['voice'] is None       # handed over once, not kept
    finally:
        await live.close()


def test_the_speaker_model_is_any_onnx_file_in_its_folder(monkeypatch, tmp_path):
    import backend.realtime_asr as module
    monkeypatch.setenv('RECORDING_SPEAKER_DIR', str(tmp_path / 'speaker'))
    monkeypatch.setattr(module.importlib.util, 'find_spec', lambda name: object())
    assert module._speaker_model_file() is None and RealtimeASR().status()['speakers'] == dict(available=False, model=None)
    (tmp_path / 'speaker').mkdir()
    (tmp_path / 'speaker' / 'notes.txt').write_text('not a model')
    assert module._speaker_model_file() is None
    (tmp_path / 'speaker' / '3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx').write_bytes(b'onnx')
    assert module._speaker_model_file().name == '3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx'
    monkeypatch.setattr(module.importlib.util, 'find_spec', lambda name: None)                   # sherpa-onnx is not installed
    assert module._speaker_model_file() is None
