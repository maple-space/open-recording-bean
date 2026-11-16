import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.storage import Storage
from backend.tests.test_app import NoDevices


def trash_command(tmp_path, monkeypatch, present=True):
    """Stands in for /usr/bin/trash, so the tests do not depend on the macOS they run on."""
    from backend import storage
    assert str(storage.TRASH_COMMAND) == '/usr/bin/trash'
    command = tmp_path / 'trash-command'
    if present:
        command.touch()
    monkeypatch.setattr(storage, 'TRASH_COMMAND', command)
    return str(command)


def test_trash_retains_audio_versions_and_metadata_together(tmp_path, monkeypatch):
    from backend import storage
    expected = trash_command(tmp_path, monkeypatch)
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
    monkeypatch.setattr(storage, '_trash_through_the_system', lambda path: pytest.fail('the trash command is there'))
    assert store.trash_record(record['id']) == {'id': record['id'], 'trashed': True}
    assert calls[0][0] == expected
    assert len(list((tmp_path / 'recoverable-trash').glob('*.wav'))) == 2
    assert json.loads((tmp_path / 'recoverable-trash/metadata.json').read_text())['transcript'] == '人工修订'
    assert not store.records()
    assert store._read('records', record['id'])['trashed_at']
    with pytest.raises(KeyError):
        store.get_record(record['id'])
    with pytest.raises(KeyError):
        store.patch_record(record['id'], {'summary': 'late update'})


def test_failed_trash_keeps_record_visible(tmp_path, monkeypatch):
    from backend import storage
    trash_command(tmp_path, monkeypatch)
    store = Storage(tmp_path / 'data')
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'RIFF' + b'\0' * 40)
    record = store.import_audio(source, device_type='local', original_id='1')
    monkeypatch.setattr(storage.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(ValueError, match='废纸篓'):
        store.trash_record(record['id'])
    assert store.audio_path(record['id']).is_file()
    assert len(store.records()) == 1


def test_a_mac_without_the_trash_command_moves_the_folder_through_the_system_itself(tmp_path, monkeypatch):
    """macOS 11 to 13: /usr/bin/trash came with macOS 14."""
    from backend import storage
    trash_command(tmp_path, monkeypatch, present=False)
    store = Storage(tmp_path / 'data')
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'RIFF' + b'\0' * 40)
    record = store.import_audio(source, device_type='local', original_id='1')
    store.patch_record(record['id'], {'transcript': '人工修订'})
    moved = []
    def move_to_test_trash(path):
        moved.append(Path(path))
        Path(path).rename(tmp_path / 'recoverable-trash')
        return True
    monkeypatch.setattr(storage, '_trash_through_the_system', move_to_test_trash)
    monkeypatch.setattr(storage.subprocess, 'run', lambda *args, **kwargs: pytest.fail('there is no trash command to run'))
    assert store.trash_record(record['id']) == {'id': record['id'], 'trashed': True}
    assert moved == [store.root / 'recordings' / record['id']]
    assert len(list((tmp_path / 'recoverable-trash').glob('*.wav'))) == 1
    assert json.loads((tmp_path / 'recoverable-trash/metadata.json').read_text())['transcript'] == '人工修订'
    assert not store.records() and store._read('records', record['id'])['trashed_at']


@pytest.mark.parametrize('failure', ['unavailable', 'refused', 'left in place'])
def test_without_the_trash_command_a_move_that_fails_says_why_and_keeps_the_record(tmp_path, monkeypatch, failure):
    from backend import storage
    trash_command(tmp_path, monkeypatch, present=False)
    store = Storage(tmp_path / 'data')
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'RIFF' + b'\0' * 40)
    record = store.import_audio(source, device_type='local', original_id='1')
    def through_the_system(path):
        if failure == 'unavailable':
            raise ImportError('No module named Foundation')
        return failure == 'left in place'           # said it moved the folder, but the folder is still there
    monkeypatch.setattr(storage, '_trash_through_the_system', through_the_system)
    with pytest.raises(ValueError, match='macOS 14' if failure == 'unavailable' else '资料库记录已保留') as error:
        store.trash_record(record['id'])
    assert '权限' not in str(error.value)             # the old message blamed permissions for a missing command
    assert store.audio_path(record['id']).is_file()
    assert len(store.records()) == 1 and not store._read('records', record['id']).get('trashed_at')


def test_the_system_interface_reports_a_folder_it_could_not_move(tmp_path):
    """The real Foundation call, on a folder that is not there: nothing reaches the Trash."""
    pytest.importorskip('Foundation')
    from backend import storage
    assert storage._trash_through_the_system(tmp_path / 'not-there') is False


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
