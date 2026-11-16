from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


class NoDevices:
    def snapshot(self, kind):
        return {'connected': False, 'files': []}
    async def disconnect(self, kind):
        return self.snapshot(kind)


def test_local_import_edit_playback_and_remote_origin_rejected(tmp_path):
    app = create_app(tmp_path, devices=NoDevices())
    with TestClient(app, base_url='http://127.0.0.1') as client:
        result = client.post('/api/records/import', files={'file': ('voice.wav', b'RIFF' + b'\0' * 40, 'audio/wav')}, data={'device_type': 'local'})
        assert result.status_code == 200, result.text
        record = result.json()
        assert client.get(record['audio_url']).content.startswith(b'RIFF')
        response = client.patch('/api/records/' + record['id'], json={'transcript': '本地修订'})
        assert response.json()['transcript'] == '本地修订'
        assert client.get('/api/records').json()[0]['id'] == record['id']
        assert client.patch('/api/settings', json={'provider': 'claude'}, headers={'Origin': 'https://example.com'}).status_code == 403
        assert client.get('/api/health', headers={'Host': 'evil.test'}).status_code == 403
        assert client.get('/api/records/missing/audio').status_code in {400, 404}


def test_reject_unknown_modes_empty_audio_and_invalid_job(tmp_path):
    with TestClient(create_app(tmp_path, devices=NoDevices()), base_url='http://127.0.0.1') as client:
        assert client.patch('/api/settings', json={'provider': 'shell'}).status_code == 400
        assert client.post('/api/records/import', files={'file': ('a.wav', b'', 'audio/wav')}, data={'device_type': 'local'}).status_code == 400
        assert client.post('/api/jobs', json={'type': 'execute', 'command': 'x'}).status_code == 422
        assert client.post('/api/jobs', json={'type': 'summarize', 'record_id': 'missing'}).status_code == 404
        assert client.get('/api/devices/other').status_code == 404                 # one kind of hardware: the recorder
        assert client.post('/api/jobs', json={'type': 'scan', 'device_type': 'other'}).status_code == 422
        assert client.post('/api/records/import', files={'file': ('a.wav', b'RIFF' + bytes(40), 'audio/wav')},
                           data={'device_type': 'other'}).status_code == 400


def test_api_mode_not_silently_routed_to_cli(tmp_path):
    with TestClient(create_app(tmp_path, devices=NoDevices()), base_url='http://127.0.0.1') as client:
        result = client.post('/api/records/import', files={'file': ('a.wav', b'RIFF' + b'\0' * 40)}, data={'device_type': 'local'})
        record_id = result.json()['id']
        client.patch('/api/records/' + record_id, json={'transcript': 'some text'})
        response = client.post('/api/jobs', json={'type': 'summarize', 'record_id': record_id})
        assert response.status_code == 400
        assert 'API' in response.json()['detail']


def test_manual_summary_survives_delayed_model_result(tmp_path, monkeypatch):
    import asyncio
    import time
    from backend import providers
    async def slow_summary(*args, **kwargs):
        await asyncio.sleep(.2)
        return '模型生成的总结'
    monkeypatch.setattr(providers, 'summarize', slow_summary)
    with TestClient(create_app(tmp_path, devices=NoDevices()), base_url='http://127.0.0.1') as client:
        response = client.post('/api/records/import', files={'file': ('a.wav', b'RIFF' + b'\0' * 40)})
        rid = response.json()['id']
        client.patch('/api/records/' + rid, json={'transcript': '会议内容'})
        client.patch('/api/settings', json={'provider': 'codex'})
        jid = client.post('/api/jobs', json={'type': 'summarize', 'record_id': rid}).json()['id']
        client.patch('/api/records/' + rid, json={'summary': '人工已修改'})
        for _ in range(50):
            job = client.get('/api/jobs/' + jid).json()
            if job['status'] == 'completed':
                break
            time.sleep(.02)
        assert job['status'] == 'completed'
        assert job['result']['record']['summary'] == '人工已修改'
        assert job['result']['record']['generated_summary'] == '模型生成的总结'


def test_api_generated_results_apply_when_original_fields_are_unchanged(tmp_path):
    with TestClient(create_app(tmp_path, devices=NoDevices()), base_url='http://127.0.0.1') as client:
        record = client.post('/api/records/import', files={'file': ('a.wav', b'RIFF' + b'\0' * 40)}).json()
        path = '/api/records/' + record['id']
        result = client.post(path + '/generated-result', json={
            'field': 'transcript', 'text': 'API 识别原文',
            'baseline_transcript': '', 'baseline_summary': '',
        })
        assert result.status_code == 200, result.text
        saved = result.json()
        assert saved['applied'] is True
        assert saved['record']['transcript'] == saved['record']['generated_transcript'] == 'API 识别原文'
        assert saved['record']['transcript_provider'] == 'api'
        result = client.post(path + '/generated-result', json={
            'field': 'summary', 'text': 'API 整理结果',
            'baseline_transcript': 'API 识别原文', 'baseline_summary': '',
        })
        saved = result.json()
        assert saved['applied'] is True
        assert saved['record']['summary'] == saved['record']['generated_summary'] == 'API 整理结果'
        assert saved['record']['summary_source_transcript'] == 'API 识别原文'
        assert saved['record']['summary_provider'] == 'api'


@pytest.mark.parametrize(('field', 'manual_patch'), [
    ('transcript', {'transcript': '另一个页面修订的逐字稿'}),
    ('summary', {'summary': '另一个页面修订的总结'}),
    ('summary', {'transcript': '总结生成时已修订来源'}),
    ('summary', {'summary_template': 'memo'}),
    ('summary', {'custom_prompt': '新的整理要求'}),
])
def test_api_generated_results_preserve_concurrent_manual_edits(tmp_path, field, manual_patch):
    with TestClient(create_app(tmp_path, devices=NoDevices()), base_url='http://127.0.0.1') as client:
        record = client.post('/api/records/import', files={'file': ('a.wav', b'RIFF' + b'\0' * 40)}).json()
        path = '/api/records/' + record['id']
        original = {'transcript': '原始逐字稿', 'summary': '原始总结'}
        client.patch(path, json=original)
        # The browser starts its API request from this baseline, then a second
        # page saves a manual edit before the delayed result arrives.
        client.patch(path, json=manual_patch)
        result = client.post(path + '/generated-result', json={
            'field': field, 'text': '延迟返回的 API 生成结果',
            'baseline_transcript': original['transcript'], 'baseline_summary': original['summary'],
            'baseline_summary_template': 'meeting', 'baseline_custom_prompt': '',
        })
        assert result.status_code == 200, result.text
        saved = result.json()
        assert saved['applied'] is False
        assert saved['notice']
        assert saved['record'][field] == manual_patch.get(field, original[field])
        assert saved['record']['generated_' + field] == '延迟返回的 API 生成结果'
        for key, value in manual_patch.items():
            assert saved['record'][key] == value
        if field == 'summary':
            assert saved['record']['summary_source_transcript'] == original['transcript']


def test_api_generated_result_rejects_missing_baselines_and_private_fields(tmp_path):
    with TestClient(create_app(tmp_path, devices=NoDevices()), base_url='http://127.0.0.1') as client:
        record = client.post('/api/records/import', files={'file': ('a.wav', b'RIFF' + b'\0' * 40)}).json()
        path = '/api/records/' + record['id'] + '/generated-result'
        assert client.post(path, json={'field': 'transcript', 'text': 'result'}).status_code == 422
        payload = {'field': 'transcript', 'text': 'result', 'baseline_transcript': '', 'baseline_summary': ''}
        assert client.post(path, json={**payload, 'api_key': 'not-a-real-key'}).status_code == 422
        assert client.post(path, json={**payload, 'field': 'title'}).status_code == 422
        assert client.post(path, json={**payload, 'text': '   '}).status_code == 400
        assert client.post(path, json=payload, headers={'Origin': 'https://example.com'}).status_code == 403
        saved = client.get('/api/records').json()[0]
        assert saved['transcript'] == ''
        assert 'api_key' not in saved


def test_health_names_the_configured_speech_model_in_full_even_when_it_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv('RECORDING_FUNASR_NANO_DIR', str(tmp_path / 'no-models-here'))
    monkeypatch.setenv('HF_HUB_CACHE', str(tmp_path / 'empty-cache'))
    app = create_app(tmp_path, devices=NoDevices())
    with TestClient(app, base_url='http://127.0.0.1') as client:
        asr = client.get('/api/health').json()['asr']
        assert asr['models'] == [] and list(asr['names']) == ['sensevoice-small']     # the default setting
        assert asr['names']['sensevoice-small'] == dict(id='sensevoice-small', name='SenseVoiceSmall')
        assert client.patch('/api/settings', json={'asr_model': 'funasr-nano'}).status_code == 200
        assert client.get('/api/health').json()['asr']['names'] == {'funasr-nano': dict(id='funasr-nano', name='Fun-ASR-Nano-GGUF')}
        assert client.patch('/api/settings', json={'asr_model': 'medium'}).status_code == 200
        names = client.get('/api/health').json()['asr']['names']
        assert names['medium']['name'] == 'faster-whisper-medium' and 'funasr-nano' not in names


def test_the_service_tells_the_page_how_an_agent_starts_the_read_only_window(tmp_path, monkeypatch):
    import backend.app as module
    with TestClient(create_app(tmp_path, devices=NoDevices()), base_url='http://127.0.0.1') as client:
        told = client.get('/api/mcp').json()
        assert told['command'] == str(module.PROJECT / 'start.sh') and told['args'] == ['mcp']
        assert told['env'] == {'RECORDING_DATA_DIR': str(tmp_path.resolve())}       # the data is not in the usual place
        assert isinstance(told['installed'], bool)
        assert client.get('/api/mcp', headers={'Host': 'evil.test'}).status_code == 403
    monkeypatch.setattr(module, 'PROJECT', tmp_path)                                 # data in the project's own folder
    with TestClient(create_app(tmp_path / '.local' / 'data', devices=NoDevices()), base_url='http://127.0.0.1') as client:
        assert client.get('/api/mcp').json()['env'] == {}
