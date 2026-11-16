"""A few words and settings are written down twice: once for the page, once for the service.

Neither side loads them from the other (the page talks to SiliconFlow straight from the browser,
the service talks to a CLI), so nothing but these tests notices when one copy is changed and the
other is not.
"""
import asyncio
import re
from pathlib import Path

import httpx
import pytest

from backend import providers
from backend.app import create_app
from backend.storage import DEFAULT_SETTINGS
from backend.tests.test_feishu_jobs import Devices

PAGE = Path(__file__).resolve().parents[2] / 'src' / 'platform'


def page(name):
    return (PAGE / name).read_text(encoding='utf-8')


def test_a_summary_template_asks_for_the_same_thing_through_the_api_and_through_a_cli():
    source = page('siliconflow.js')
    assert set(providers.TEMPLATES) == {'meeting', 'memo', 'interview'}
    for key, text in providers.TEMPLATES.items():
        assert f"{key}: '{text}'" in source, f'{key}: src/platform/siliconflow.js and backend/providers.py differ'


def test_the_page_starts_from_the_settings_the_service_starts_from():
    written = re.search(r'^export const initialSettings = \{(.*)\}$', page('api.js'), re.MULTILINE)
    assert written, 'initialSettings is no longer one line in src/platform/api.js'
    assert dict(re.findall(r"(\w+): '([^']*)'", written.group(1))) == DEFAULT_SETTINGS


@pytest.mark.asyncio
@pytest.mark.parametrize(('mode', 'constant'), [('backup', 'DEVICE_BACKUP_DELETE_PHRASE'), ('direct', 'DEVICE_DELETE_PHRASE')])
async def test_the_service_accepts_the_words_the_page_asks_for_before_deleting_from_the_recorder(tmp_path, mode, constant):
    phrases = dict(re.findall(r"^export const (DEVICE\w*_PHRASE) = '([^']+)'$", page('deviceDeletion.js'), re.MULTILINE))
    assert set(phrases) == {'DEVICE_BACKUP_DELETE_PHRASE', 'DEVICE_DELETE_PHRASE'}
    devices = Devices()
    devices.release.set()                               # the backup, when one is made, is not held up
    app = create_app(tmp_path, devices=devices)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        body = dict(action='delete', device_id='bean-a', ids=['123'], mode=mode, phrase=phrases[constant])
        response = await client.post('/api/devices/feishu/actions', json=body)
        assert response.status_code == 200, response.text
        await asyncio.gather(*list(app.state.jobs.tasks.values()))
        assert app.state.jobs.get(response.json()['id'])['status'] == 'completed'
        assert devices.deleted == ['123']
        # The words of the other way do not confirm this one.
        other = next(text for name, text in phrases.items() if name != constant)
        assert (await client.post('/api/devices/feishu/actions', json=dict(body, phrase=other))).status_code == 400
