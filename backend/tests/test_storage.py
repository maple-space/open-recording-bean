from pathlib import Path

import pytest

from backend.storage import Storage


def audio(tmp_path):
    path = tmp_path / 'sample.wav'
    path.write_bytes(b'RIFF' + b'\0' * 40)
    return path


def test_device_namespaces_and_edits_survive_reimport(tmp_path):
    store = Storage(tmp_path / 'data')
    source = audio(tmp_path)
    first = store.import_audio(source, device_type='feishu', device_id='one', original_id='1')
    other = store.import_audio(source, device_type='local', device_id='one', original_id='1')
    third = store.import_audio(source, device_type='feishu', device_id='two', original_id='1')
    assert len({first['id'], other['id'], third['id']}) == 3
    store.patch_record(first['id'], {'title': '人工名称', 'transcript': '人工改稿'})
    again = store.import_audio(source, device_type='feishu', device_id='one', original_id='1')
    assert again['title'] == '人工名称'
    assert again['transcript'] == '人工改稿'
    assert 'audio_file' not in again
    assert Storage(tmp_path / 'data').audio_path(first['id']).read_bytes() == source.read_bytes()


def test_paths_and_patch_are_constrained(tmp_path):
    store = Storage(tmp_path / 'data')
    record = store.import_audio(audio(tmp_path), device_type='local', original_id='../../escape')
    assert store.audio_path(record['id']).is_relative_to(store.root)
    with pytest.raises((KeyError, ValueError)):
        store.audio_path('../../escape')
    with pytest.raises(ValueError):
        store.patch_record(record['id'], {'audio_file': '/etc/passwd'})
    with pytest.raises(ValueError):
        store.update_settings({'provider': 'shell'})
    with pytest.raises(ValueError):
        store.update_settings({'api_key': 'not-saved'})


def test_job_recovery_and_raw_transcript_preservation(tmp_path):
    store = Storage(tmp_path / 'data')
    rec = store.import_audio(audio(tmp_path), device_type='local', original_id='a')
    store.patch_record(rec['id'], {'raw_transcript': '原稿', 'segments': [{'start': 0, 'end': 1, 'text': '原稿'}]}, internal=True)
    store.patch_record(rec['id'], {'transcript': '人工修订'})
    assert store.get_record(rec['id'])['raw_transcript'] == '原稿'
    store.save_job({'id': 'job-one', 'status': 'running', 'created_at': '2026-10-29', 'type': 'scan'})
    store.recover_jobs()
    assert store.get_job('job-one')['status'] == 'failed'
    assert '重启' in store.get_job('job-one')['error']


def test_settings_and_exports_do_not_expose_internal_paths(tmp_path):
    store = Storage(tmp_path / 'data')
    assert store.update_settings({'provider': 'claude'})['provider'] == 'claude'
    assert Storage(tmp_path / 'data').settings()['provider'] == 'claude'
    rec = store.import_audio(audio(tmp_path), device_type='local', original_id='a')
    import io, zipfile, json
    data = store.bundle(rec['id'])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        metadata = json.loads(archive.read('metadata.json'))
        assert 'audio_file' not in metadata
        assert 'audio.wav' in archive.namelist()


def test_restart_recovers_jobs_older_than_display_limit(tmp_path):
    store = Storage(tmp_path / 'data')
    store.save_job({'id': 'old', 'status': 'running', 'created_at': '2025-01-01', 'type': 'scan'})
    for index in range(101):
        store.save_job({'id': f'new-{index}', 'status': 'completed', 'created_at': '2026-01-01', 'type': 'scan'})
    store.recover_jobs()
    assert store.get_job('old')['status'] == 'failed'


def test_nonfinite_duration_cannot_poison_catalog(tmp_path):
    store = Storage(tmp_path / 'data')
    for duration in [float('inf'), float('nan'), -1]:
        with pytest.raises(ValueError):
            store.import_audio(audio(tmp_path), device_type='local', original_id='invalid', duration_ms=duration)
    assert store.records() == []
