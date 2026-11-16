"""The agents' window: what it reads from the data folder, and that it speaks MCP when started the way an agent starts it."""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend.live import TranscriptLog
from backend.mcp_server import Saved
from backend.storage import Storage

PROJECT = Path(__file__).resolve().parents[2]


def moment(text):
    return datetime.strptime(text, '%Y-%m-%d %H:%M:%S').timestamp()


@pytest.fixture
def folder(tmp_path):
    """Two sessions of realtime text (the second runs past midnight) and two recordings, one of them with text."""
    log = TranscriptLog(tmp_path / 'live-transcripts')
    log.begin('soundcore Work', 'SenseVoice Small', moment('2026-11-13 09:30:00'))
    log.write(moment('2026-11-13 09:30:05'), '我们先对一下发布计划。', 1)
    log.write(moment('2026-11-13 09:30:12'), '接口周三可以联调完。', 2)
    log.write(moment('2026-11-13 09:31:40'), '好的。', 1)
    log.end()
    log.begin('soundcore Work', 'SenseVoice Small', moment('2026-11-13 23:59:50'))
    log.write(moment('2026-11-13 23:59:55'), '夜里再补一句 Release 计划。', 1)
    log.write(moment('2026-11-14 00:00:03'), '过了零点的一句。', None)
    log.end()
    store = Storage(tmp_path)
    source = tmp_path / 'a.wav'
    for number, (title, when, transcript, summary) in enumerate([
            ('周会录音', '2026-11-13T02:00:00+00:00', '第一行文字\n这里提到发布计划和回归测试。', '一句话总结'),
            ('没转写的录音', '2026-11-12T10:00:00', '', '')]):
        source.write_bytes(b'RIFF' + bytes(40))
        record = store.import_audio(source, device_type='feishu' if number == 0 else 'local', device_id='x', original_id=str(number),
                                    title=title, recorded_at=when, duration_ms=90000)
        store.patch_record(record['id'], {'transcript': transcript, 'summary': summary})
    return tmp_path


def test_a_day_is_read_in_the_order_it_was_said_and_each_session_is_marked(folder):
    saved = Saved(folder)
    day = saved.read_text('2026-11-13')
    assert (day['total'], day['returned'], day['next_offset']) == (4, 4, None)
    assert day['text'].splitlines() == [
        '—— 以下来自 2026-11-13 09:30 开始的一次记录，说话人重新编号 ——',
        '[09:30:05] 说话人1：我们先对一下发布计划。', '[09:30:12] 说话人2：接口周三可以联调完。', '[09:31:40] 说话人1：好的。',
        '—— 以下来自 2026-11-13 23:59 开始的一次记录，说话人重新编号 ——',
        '[23:59:55] 说话人1：夜里再补一句 Release 计划。']
    after_midnight = saved.read_text('2026-11-14')                  # the same session, on the next day
    assert after_midnight['total'] == 1 and after_midnight['text'].endswith('[00:00:03] 过了零点的一句。')
    part = saved.read_text('2026-11-13', start_time='09:30:10', end_time='09:32')
    assert part['total'] == 2 and '联调' in part['text'] and '发布计划' not in part['text']
    assert saved.read_text('2026-11-01') == dict(date='2026-11-01', start_time='00:00:00', end_time='23:59:59', total=0, offset=0,
                                                  returned=0, next_offset=None, text='',
                                                  note='这段时间没有实时输入的文字记录。可以用 list_days 看哪些天有内容。')


def test_a_long_day_is_read_page_by_page(folder):
    saved = Saved(folder)
    first = saved.read_text('2026-11-13', limit=3)
    assert (first['returned'], first['next_offset']) == (3, 3)
    rest = saved.read_text('2026-11-13', offset=first['next_offset'], limit=3)
    assert (rest['returned'], rest['next_offset']) == (1, None) and '夜里' in rest['text']
    assert saved.read_text('2026-11-13', limit=10 ** 9)['returned'] == 4      # a huge limit is cut down, not refused


def test_the_days_with_text_are_listed_newest_first(folder):
    days = Saved(folder).days()
    assert [day['date'] for day in days] == ['2026-11-14', '2026-11-13']
    assert days[1] == dict(date='2026-11-13', sentences=4, characters=sum(map(len, (
        '我们先对一下发布计划。', '接口周三可以联调完。', '好的。', '夜里再补一句 Release 计划。'))),
        first_time='09:30:05', last_time='23:59:55', sessions=2, recordings_with_text=1)
    assert days[0]['sentences'] == 1 and days[0]['sessions'] == 1 and days[0]['recordings_with_text'] == 0


def test_searching_covers_realtime_text_and_recordings_and_needs_every_word(folder):
    saved = Saved(folder)
    found = saved.search('发布计划')
    assert found['total'] == 2 and [hit['source'] for hit in found['matches']] == ['实时文字', '录音']      # most recent first
    assert found['matches'][0] == dict(source='实时文字', date='2026-11-13', time='09:30:05', speaker=1,
                                       text='我们先对一下发布计划。', record='2026-11-13_09-30-00.txt')
    assert found['matches'][1]['title'] == '周会录音' and '回归测试' in found['matches'][1]['text']
    assert saved.search('release 计划')['total'] == 1                 # both words, whatever their case
    assert saved.search('发布计划 联调')['total'] == 0                # not in the same sentence
    assert saved.search('发布计划', start_date='2026-11-14')['total'] == 0
    with pytest.raises(ValueError, match='要找的词'):
        saved.search('   ')


def test_recordings_are_listed_and_read_with_their_text_and_summary(folder):
    saved = Saved(folder)
    rows = saved.recordings()
    assert [(row['title'], row['recorded_at'], row['source'], row['characters'], row['has_summary']) for row in rows] == [
        ('周会录音', f"{datetime.fromisoformat('2026-11-13T02:00:00+00:00').astimezone():%Y-%m-%d %H:%M}", '录音豆', 20, True),
        ('没转写的录音', '2026-11-12 10:00', '导入的音频', 0, False)]
    assert 'transcript' not in rows[0] and rows[0]['minutes'] == 1.5
    whole = saved.read_recording(rows[0]['id'])
    assert whole['transcript'].startswith('第一行文字') and whole['summary'] == '一句话总结' and whole['next_offset'] is None
    part = saved.read_recording(rows[0]['id'], offset=6, limit=5)
    assert (part['returned'], part['next_offset']) == (5, 11) and 'summary' not in part
    assert saved.read_recording(rows[1]['id'])['note'] == '这条录音还没有转成文字。'
    assert [row['title'] for row in saved.recordings(end_date='2026-11-12')] == ['没转写的录音']
    with pytest.raises(ValueError, match='没有这条录音'):
        saved.read_recording('missing')


def test_the_last_minutes_are_read_and_an_unfinished_session_is_reported(tmp_path):
    log = TranscriptLog(tmp_path / 'live-transcripts')
    now = datetime.now()
    log.begin('soundcore Work', 'SenseVoice Small', (now - timedelta(minutes=50)).timestamp())
    log.write((now - timedelta(minutes=40)).timestamp(), '四十分钟前的一句。', 1)
    log.write((now - timedelta(minutes=3)).timestamp(), '三分钟前的一句。', 2)
    saved = Saved(tmp_path)
    recent = saved.latest_text(minutes=10)
    assert recent['returned'] == 1 and '三分钟前' in recent['text'] and '四十分钟前' not in recent['text']
    assert recent['session_open'] is True and recent['last_sentence_at'] == f'{now - timedelta(minutes=3):%Y-%m-%d %H:%M:%S}'
    assert saved.latest_text(minutes=60)['returned'] == 2
    log.end()
    assert Saved(tmp_path).latest_text()['session_open'] is False


def test_no_text_or_recording_is_changed_and_an_empty_or_missing_folder_is_simply_empty(folder, tmp_path):
    def listing(root):          # everything except the index files SQLite keeps beside the catalog while it is open
        return sorted((str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns) for path in root.rglob('*')
                      if path.is_file() and path.name not in ('catalog.sqlite3-shm', 'catalog.sqlite3-wal'))
    before = listing(folder)
    saved = Saved(folder)
    saved.days(), saved.read_text('2026-11-13'), saved.latest_text(), saved.search('计划'), saved.recordings()
    saved.read_recording(saved.recordings()[0]['id'])
    assert listing(folder) == before                                 # not a byte and not a timestamp
    nowhere = Saved(tmp_path / 'never-created')
    assert nowhere.days() == [] and nowhere.recordings() == [] and nowhere.read_text('2026-11-13')['total'] == 0
    assert nowhere.search('计划')['total'] == 0 and nowhere.latest_text()['session_open'] is False
    assert not (tmp_path / 'never-created').exists()
    for wrong in ('2026/11/13', 'today', '13-11-2026'):
        with pytest.raises(ValueError, match='日期'):
            saved.read_text(wrong)
    with pytest.raises(ValueError, match='时间'):
        saved.read_text('2026-11-13', start_time='nine')


@pytest.mark.asyncio
async def test_an_agent_can_start_it_and_use_every_tool(folder):
    pytest.importorskip('mcp')
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    environment = dict(os.environ, RECORDING_DATA_DIR=str(folder), PYTHONPATH=str(PROJECT))
    parameters = StdioServerParameters(command=sys.executable, args=['-m', 'backend.mcp_server'], env=environment, cwd=str(folder))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            hello = await session.initialize()
            assert hello.server_info.name == 'open-recording-bean' and '只能读' in hello.instructions
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert sorted(tools) == ['latest_text', 'list_days', 'list_recordings', 'read_recording', 'read_text', 'search_text']
            assert all(tool.annotations.read_only_hint and not tool.annotations.destructive_hint for tool in tools.values())

            async def call(name, **arguments):
                result = await session.call_tool(name, arguments)
                return result.is_error, json.loads(result.content[0].text) if not result.is_error else result.content[0].text

            failed, day = await call('read_text', date='2026-11-13', limit=2)
            assert not failed and (day['total'], day['returned'], day['next_offset']) == (4, 2, 2)
            failed, found = await call('search_text', query='发布计划')
            assert not failed and found['total'] == 2
            failed, listed = await call('list_recordings')
            assert not failed and [row['title'] for row in listed['recordings']] == ['周会录音', '没转写的录音']
            failed, listed = await call('list_days')
            assert not failed and [day['date'] for day in listed['days']] == ['2026-11-14', '2026-11-13']
            failed, message = await call('read_text', date='yesterday')
            assert failed and '日期' in message                       # the agent is told what was wrong
