import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.storage import Storage
from backend.tests.test_app import NoDevices


def test_trash_retains_audio_versions_and_metadata_together(tmp_path, monkeypatch):
    from backend import storage
    store = Storage(tmp_path / 'data')
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'RIFF' + b'\0' * 40)
    record = store.import_audio(source, device_type='local', original_id='1')
    store.import_audio(source, device_type='local', original_id='1')
    store.patch_record(record['id'], {'transcript': '人工修订', 'summary': '总结'})
    calls = []
    def move_to_test_trash(command, **kwargs):
        calls.append(command)
        Path(command[1]).rename(tmp_path / 'recoverable-trash')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(storage.subprocess, 'run', move_to_test_trash)
    assert store.trash_record(record['id']) == {'id': record['id'], 'trashed': True}
    assert calls[0][0] == '/usr/bin/trash'
    assert len(list((tmp_path / 'recoverable-trash').glob('*.wav'))) == 2
    assert json.loads((tmp_path / 'recoverable-trash/metadata.json').read_text())['transcript'] == '人工修订'
    assert not store.records()
    assert store._read('records', record['id'])['trashed_at']
    import pytest
    with pytest.raises(KeyError):
        store.get_record(record['id'])
    with pytest.raises(KeyError):
        store.patch_record(record['id'], {'summary': 'late update'})


def test_failed_trash_keeps_record_visible(tmp_path, monkeypatch):
    from backend import storage
    store = Storage(tmp_path / 'data')
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'RIFF' + b'\0' * 40)
    record = store.import_audio(source, device_type='local', original_id='1')
    monkeypatch.setattr(storage.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    import pytest
    with pytest.raises(ValueError, match='废纸篓'):
        store.trash_record(record['id'])
    assert store.audio_path(record['id']).is_file()
    assert len(store.records()) == 1


def test_trash_rejects_record_with_active_job(tmp_path, monkeypatch):
    from backend import providers
    async def pending(*args, **kwargs):
        await asyncio.sleep(100)
    monkeypatch.setattr(providers, 'summarize', pending)
    def unexpected_trash(*args, **kwargs):
        raise AssertionError('active jobs must prevent any filesystem move')
    monkeypatch.setattr(Storage, 'trash_record', unexpected_trash)
    with TestClient(create_app(tmp_path, devices=NoDevices()), base_url='http://127.0.0.1') as client:
        record = client.post('/api/records/import', files={'file': ('voice.wav', b'RIFF' + b'\0'*40)}).json()
        client.patch('/api/records/' + record['id'], json={'transcript': 'test'})
        client.patch('/api/settings', json={'provider': 'codex'})
        job = client.post('/api/jobs', json={'type': 'summarize', 'record_id': record['id']}).json()
        response = client.post('/api/records/' + record['id'] + '/trash')
        assert response.status_code == 409
        assert '任务' in response.json()['detail']
        client.post('/api/jobs/' + job['id'] + '/cancel')
