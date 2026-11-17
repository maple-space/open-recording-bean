"""Jobs reference the catalog; they never duplicate recording text."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from backend import asr, providers, storage
from backend.app import create_app
from backend.storage import Storage
from backend.tests.test_feishu_jobs import BatchDevices


MARKER = 'TRANSCRIPT-MUST-STAY-IN-CATALOG'
LONG_TEXT = (MARKER + ' 会议发言。') * 700


def seed_record(store, directory, ident, *, transcript=LONG_TEXT):
    source = directory / 'synthetic.wav'
    source.write_bytes(b'RIFF' + bytes(40))
    record = store.import_audio(source, device_type='feishu', device_id='bean-a', original_id=ident)
    return store.patch_record(record['id'], dict(
        transcript=transcript, raw_transcript=LONG_TEXT, generated_transcript=LONG_TEXT,
        segments=[dict(start=0, end=3600, text=LONG_TEXT)], summary=LONG_TEXT,
        generated_summary=LONG_TEXT, summary_source_transcript=LONG_TEXT), internal=True)


def assert_compact(job):
    encoded = json.dumps(job, ensure_ascii=False).encode()
    assert MARKER.encode() not in encoded
    assert len(encoded) < 40_000
    assert 'record' not in job['result'] and 'records' not in job['result']


@pytest.mark.asyncio
@pytest.mark.parametrize('action, mode, fail_last', [
    ('download', 'backup', False), ('download', 'backup', True),
    ('delete', 'backup', False), ('delete', 'direct', False),
])
async def test_38_record_batch_jobs_do_not_duplicate_catalog_text(tmp_path, action, mode, fail_last):
    devices = BatchDevices()
    devices.ids = [str(1794000000 + n * 3600) for n in range(38)]
    app = create_app(tmp_path / 'data', devices=devices)
    store = app.state.storage
    archived = devices.ids[:-1] if fail_last else devices.ids
    record_ids = [seed_record(store, tmp_path, ident)['id'] for ident in archived]
    if fail_last:
        devices.failures.add(devices.ids[-1])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/devices/feishu/actions', json=dict(
            action=action, mode=mode, device_id='bean-a', ids=devices.ids,
            phrase='确认从设备永久删除' if mode == 'direct' else '确认备份本地后删除'))
        assert response.status_code == 200, response.text
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        job_id = response.json()['id']
        job = (await client.get('/api/jobs/' + job_id)).json()
        assert job['status'] == ('failed' if fail_last else 'completed')
        assert_compact(job)
        result = job['result']
        expected = record_ids if mode == 'backup' else []
        assert result['record_ids'] == expected
        assert result['record_id'] == (expected[-1] if expected else None)
        assert result['count'] == (len(archived) if action == 'download' else 38)
        if action == 'download':
            assert result['total'] == 38
            assert result['completed_ids'] == result['skipped_ids'] == archived
            assert len(result['failed_items']) == int(fail_last)
        else:
            assert devices.deleted == devices.ids
        assert_compact(Storage(store.root).get_job(job_id))
        payload = await client.get('/api/jobs')
        assert len(payload.content) < 40_000 and MARKER.encode() not in payload.content
        assert store.get_record(record_ids[0])['transcript'] == LONG_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['transcribe', 'summarize', 'process'])
async def test_text_processing_job_results_only_reference_saved_record(tmp_path, monkeypatch, mode):
    app = create_app(tmp_path / 'data', devices=BatchDevices())
    store = app.state.storage
    record = seed_record(store, tmp_path, '123', transcript=LONG_TEXT if mode == 'summarize' else '')
    store.update_settings(dict(provider='codex', asr='local'))
    monkeypatch.setattr(asr, 'transcribe', AsyncMock(return_value=dict(text=LONG_TEXT)))
    monkeypatch.setattr(providers, 'correct_transcript', AsyncMock(return_value=LONG_TEXT))
    monkeypatch.setattr(providers, 'summarize', AsyncMock(return_value=LONG_TEXT))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/jobs', json=dict(type=mode, record_id=record['id']))
        assert response.status_code == 200, response.text
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        job = store.get_job(response.json()['id'])
        assert job['status'] == 'completed', job
        assert_compact(job)
        assert job['result'] == dict(record_id=record['id'], notice='')
        assert store.get_record(record['id'])['transcript'] == LONG_TEXT
        assert MARKER.encode() not in (await client.get('/api/jobs')).content


@pytest.mark.asyncio
async def test_single_download_job_only_references_record_with_existing_text(tmp_path):
    app = create_app(tmp_path / 'data', devices=BatchDevices())
    record = seed_record(app.state.storage, tmp_path, '123')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        response = await client.post('/api/jobs', json=dict(type='download', device_type='feishu', original_id='123'))
        assert response.status_code == 200, response.text
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        job = app.state.jobs.get(response.json()['id'])
        assert job['status'] == 'completed', job
        assert_compact(job)
        assert job['result'] == dict(record_id=record['id'])
        assert app.state.storage.get_record(record['id'])['transcript'] == LONG_TEXT


@pytest.mark.asyncio
async def test_record_detail_returns_catalog_content_and_404_after_trash(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'data', devices=BatchDevices())
    record = seed_record(app.state.storage, tmp_path, '123')
    # Exercise the actual catalog operation without touching the system Trash.
    monkeypatch.setattr(storage, 'move_to_trash', lambda path: Path(path).rename(tmp_path / 'retained-record'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        path = '/api/records/' + record['id']
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json() == record
        assert (await client.get('/api/records/' + '0' * 32)).status_code == 404
        assert (await client.post(path + '/trash')).status_code == 200
        assert (await client.get(path)).status_code == 404
        assert (tmp_path / 'retained-record' / 'transcript.txt').read_text() == LONG_TEXT


@pytest.mark.asyncio
async def test_legacy_persisted_jobs_still_load_without_migration(tmp_path):
    store = Storage(tmp_path / 'data')
    record = seed_record(store, tmp_path, '123')
    legacy = dict(id='a' * 32, type='download', status='completed', created_at='',
                  result=dict(record=record, records=[record], count=1))
    store.save_job(legacy)
    app = create_app(store.root, devices=BatchDevices())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        assert (await client.get('/api/jobs/' + legacy['id'])).json() == legacy
        assert (await client.get('/api/jobs')).json() == [legacy]
