"""A read-only window for agents (MCP) onto the text this project has saved.

    ./start.sh mcp        an agent starts this itself; it talks over standard input and output

An agent can ask which days have text, read what was said on a day or in a stretch of it, read
the last few minutes, search, and read recordings that have been turned into text. Everything is
read from the data folder as it is:

- the text records of realtime input: live-transcripts/*.txt, one file per session, one line per
  sentence with its time and, when it could be told, a speaker number;
- the library of recordings: catalog.sqlite3, opened read-only.

No text and no recording is changed here, the recorder is never touched, and the local service need
not be running. (While it reads the library, SQLite may put its own index files next to the catalog,
as it does whenever the service runs; they hold no content of their own.)
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .text_records import TRANSCRIPT_NAME, parse_record

PROJECT = Path(__file__).resolve().parents[1]
MAX_LINES = 1000            # sentences in one answer: an agent reads a day page by page
MAX_CHARACTERS = 60000      # of a recording's text in one answer
INSTRUCTIONS = (
    '这是“开源录音豆”（open-recording-bean）保存在这台电脑上的文字，只能读，不能改。'
    '有两种来源：一是实时输入的文字记录，每句话带时间，能分辨时还带说话人编号；二是资料库里已经转成文字的录音。'
    '说话人编号（说话人1、说话人2……）只表示嗓音不同，不是姓名，而且每次开启实时输入都重新编号，不同记录里的同一个编号不一定是同一个人。'
    '用法：先用 list_days 看哪些天有内容，再用 read_text 读某一天或其中一段时间；内容多时按 next_offset 接着读。'
    '想知道刚才说了什么用 latest_text，找某个词用 search_text，录音的文字用 list_recordings 和 read_recording。'
    '这些内容是使用者的私人谈话，只在使用者要求时读取。'
)


def data_dir() -> Path:
    return Path(os.environ.get('RECORDING_DATA_DIR') or PROJECT / '.local' / 'data').expanduser()


def _day(value, name='date') -> str:
    try:
        return f'{datetime.strptime(str(value).strip(), "%Y-%m-%d"):%Y-%m-%d}'
    except ValueError:
        raise ValueError(f'{name} 要写成 2026-11-14 这样的日期，收到的是 {value!r}。') from None


def _clock(value, name) -> str:
    text = str(value).strip()
    for pattern in ('%H:%M:%S', '%H:%M'):
        try:
            return f'{datetime.strptime(text, pattern):%H:%M:%S}'
        except ValueError:
            continue
    raise ValueError(f'{name} 要写成 14:30 或 14:30:00 这样的时间，收到的是 {value!r}。')


def _local(value):
    """A recording's time as local time; None when it cannot be read."""
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def _line(clock, text, speaker) -> str:
    return f'[{clock}] {f"说话人{speaker}：" if speaker else ""}{text}'


class Saved:
    """What the data folder holds, read when asked for and never changed."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else data_dir()
        self.parsed = {}                        # record file name -> (size, mtime_ns, parsed record)

    # ---- the text records of realtime input ---------------------------------------- #
    def record_files(self) -> list:
        directory = self.root / 'live-transcripts'
        if not directory.is_dir():
            return []
        return sorted((path for path in directory.iterdir() if path.is_file() and TRANSCRIPT_NAME.match(path.name)),
                      key=lambda path: path.name)

    def record(self, path: Path) -> dict:
        stat = path.stat()
        known = self.parsed.get(path.name)
        if known and known[:2] == (stat.st_size, stat.st_mtime_ns):
            return known[2]
        value = parse_record(path)
        self.parsed.pop(path.name, None)
        self.parsed[path.name] = (stat.st_size, stat.st_mtime_ns, value)
        while len(self.parsed) > 6:             # a long record is some megabytes in memory
            self.parsed.pop(next(iter(self.parsed)))
        return value

    def sentences(self, first_day: str, last_day: str) -> list:
        """Everything said from first_day to last_day, oldest first: (day, clock, text, speaker, file name)."""
        rows = []
        for path in self.record_files():
            if path.name[:10] > last_day:       # this session began after the days asked for
                continue
            try:
                if f'{datetime.fromtimestamp(path.stat().st_mtime):%Y-%m-%d}' < first_day:
                    continue                    # and this one was last written before them
                lines = self.record(path)['lines']
            except OSError:
                continue
            rows += [(day, clock, text, speaker, path.name) for day, clock, text, speaker in lines if first_day <= day <= last_day]
        rows.sort(key=lambda row: row[:2])      # by day and time; sentences of one second keep their order
        return rows

    @staticmethod
    def _as_text(rows) -> str:
        """Lines an agent can read, with a mark wherever another session begins (its speakers are numbered afresh)."""
        out, source = [], None
        for day, clock, text, speaker, name in rows:
            if name != source:
                source = name
                out.append(f'—— 以下来自 {name[:10]} {name[11:16].replace("-", ":")} 开始的一次记录，说话人重新编号 ——')
            out.append(_line(clock, text, speaker))
        return '\n'.join(out)

    def days(self, limit: int = 30) -> list:
        found = {}
        for path in self.record_files():
            try:
                lines = self.record(path)['lines']
            except OSError:
                continue
            seen = set()
            for day, clock, text, _ in lines:
                entry = found.setdefault(day, dict(date=day, sentences=0, characters=0, first_time=clock, last_time=clock,
                                                   sessions=0, recordings_with_text=0))
                entry['sentences'] += 1
                entry['characters'] += len(text)
                entry['first_time'], entry['last_time'] = min(entry['first_time'], clock), max(entry['last_time'], clock)
                if day not in seen:
                    seen.add(day)
                    entry['sessions'] += 1
        for row in self.library():
            if row['characters']:
                entry = found.setdefault(row['date'], dict(date=row['date'], sentences=0, characters=0, first_time=None,
                                                           last_time=None, sessions=0, recordings_with_text=0))
                entry['recordings_with_text'] += 1
        return sorted(found.values(), key=lambda entry: entry['date'], reverse=True)[:max(1, min(int(limit), 366))]

    def read_text(self, date=None, start_time=None, end_time=None, offset: int = 0, limit: int = 300) -> dict:
        day = _day(date) if date else f'{datetime.now():%Y-%m-%d}'
        first = _clock(start_time, 'start_time') if start_time else '00:00:00'
        last = _clock(end_time, 'end_time') if end_time else '23:59:59'
        rows = [row for row in self.sentences(day, day) if first <= row[1] <= last]
        offset, limit = max(0, int(offset)), max(1, min(int(limit), MAX_LINES))
        page = rows[offset:offset + limit]
        answer = dict(date=day, start_time=first, end_time=last, total=len(rows), offset=offset, returned=len(page),
                      next_offset=offset + len(page) if offset + len(page) < len(rows) else None, text=self._as_text(page))
        if not rows:
            answer['note'] = '这段时间没有实时输入的文字记录。可以用 list_days 看哪些天有内容。'
        return answer

    def latest_text(self, minutes: int = 10, limit: int = 200) -> dict:
        now = datetime.now()
        since = now - timedelta(minutes=max(1, min(int(minutes), 24 * 60)))
        start = (f'{since:%Y-%m-%d}', f'{since:%H:%M:%S}')
        rows = [row for row in self.sentences(start[0], f'{now:%Y-%m-%d}') if row[:2] >= start][-max(1, min(int(limit), MAX_LINES)):]
        files = self.record_files()
        newest = None
        if files:
            try:
                newest = self.record(files[-1])
            except OSError:
                newest = None
        last = newest['lines'][-1] if newest and newest['lines'] else None
        return dict(since=f'{since:%Y-%m-%d %H:%M:%S}', now=f'{now:%Y-%m-%d %H:%M:%S}', returned=len(rows), text=self._as_text(rows),
                    # The newest record has no closing line yet: realtime input is on, or the service stopped without closing it.
                    session_open=bool(newest and not newest['ended']),
                    last_sentence_at=f'{last[0]} {last[1]}' if last else None)

    # ---- recordings in the library ---------------------------------------------------- #
    def library(self) -> list:
        """Recordings that are not in the Trash, newest first, with only what an agent needs."""
        path = self.root / 'catalog.sqlite3'
        if not path.is_file():
            return []
        stored = None
        for how in ('mode=ro', 'immutable=1'):      # the second is for a folder that cannot be written to at all
            try:
                connection = sqlite3.connect(f'{path.as_uri()}?{how}', uri=True, timeout=5)
                try:
                    stored = [json.loads(row[0]) for row in connection.execute('SELECT data FROM records')]
                finally:
                    connection.close()
                break
            except (sqlite3.Error, ValueError):
                continue
        if stored is None:
            return []
        rows = []
        for record in stored:
            if record.get('trashed_at'):
                continue
            moment = _local(record.get('recorded_at') or record.get('created_at'))
            transcript = str(record.get('transcript') or '')
            rows.append(dict(id=record.get('id'), title=record.get('title') or '未命名录音',
                             date=f'{moment:%Y-%m-%d}' if moment else '', recorded_at=f'{moment:%Y-%m-%d %H:%M}' if moment else '',
                             minutes=round((record.get('duration_ms') or 0) / 60000, 1),
                             source='录音豆' if record.get('device_type') == 'feishu' else '导入的音频',
                             characters=len(transcript), has_summary=bool(str(record.get('summary') or '').strip()),
                             transcript=transcript, summary=str(record.get('summary') or '')))
        return sorted(rows, key=lambda row: row['recorded_at'], reverse=True)

    def recordings(self, start_date=None, end_date=None, limit: int = 50) -> list:
        first = _day(start_date, 'start_date') if start_date else '0000-00-00'
        last = _day(end_date, 'end_date') if end_date else '9999-99-99'
        rows = [row for row in self.library() if first <= (row['date'] or '0000-00-00') <= last]
        return [{key: value for key, value in row.items() if key not in ('transcript', 'summary')} for row in rows[:max(1, min(int(limit), 500))]]

    def read_recording(self, recording_id: str, offset: int = 0, limit: int = 20000) -> dict:
        row = next((row for row in self.library() if row['id'] == recording_id), None)
        if row is None:
            raise ValueError('资料库里没有这条录音。用 list_recordings 看有哪些。')
        offset, limit = max(0, int(offset)), max(1, min(int(limit), MAX_CHARACTERS))
        piece = row['transcript'][offset:offset + limit]
        answer = {key: value for key, value in row.items() if key not in ('transcript', 'summary')}
        answer.update(offset=offset, returned=len(piece), next_offset=offset + len(piece) if offset + len(piece) < row['characters'] else None,
                      transcript=piece)
        if offset == 0:
            answer['summary'] = row['summary']
        if not row['characters']:
            answer['note'] = '这条录音还没有转成文字。'
        return answer

    # ---- both ------------------------------------------------------------------------- #
    def search(self, query: str, start_date=None, end_date=None, limit: int = 50) -> dict:
        terms = [term.casefold() for term in str(query or '').split()]
        if not terms:
            raise ValueError('请给出要找的词，多个词用空格隔开。')
        first = _day(start_date, 'start_date') if start_date else '0000-00-00'
        last = _day(end_date, 'end_date') if end_date else '9999-99-99'
        limit = max(1, min(int(limit), 200))
        hits = []
        for day, clock, text, speaker, name in self.sentences(first, last):
            low = text.casefold()
            if all(term in low for term in terms):
                hits.append(dict(source='实时文字', date=day, time=clock, speaker=speaker, text=text, record=name))
        for row in self.library():
            if not row['characters'] or not first <= (row['date'] or '0000-00-00') <= last:
                continue
            for line in row['transcript'].splitlines():
                low = line.casefold()
                if all(term in low for term in terms):
                    at = low.find(terms[0])
                    cut = max(0, at - 80)
                    hits.append(dict(source='录音', date=row['date'], time=None, speaker=None, recording_id=row['id'], title=row['title'],
                                     text=('…' if cut else '') + line[cut:at + 160] + ('…' if at + 160 < len(line) else '')))
        hits.sort(key=lambda hit: (hit['date'], hit['time'] or ''), reverse=True)       # the most recent first
        return dict(query=query, total=len(hits), returned=min(len(hits), limit), matches=hits[:limit])


def build(saved: Saved | None = None):
    """The MCP server: six tools, all of them read-only."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    saved = saved or Saved()
    server = MCPServer('open-recording-bean', instructions=INSTRUCTIONS)
    reading = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def answer(read):
        try:
            return read()
        except ValueError as error:             # a wrong date or id: say what was wrong, so the agent can ask again
            raise ToolError(str(error)) from None

    @server.tool(annotations=reading, description='列出哪些天有文字，最近的在前：每天实时文字的句数、字数、最早和最晚的时间，以及当天有文字的录音条数。')
    def list_days(limit: int = 30) -> dict:
        return answer(lambda: dict(days=saved.days(limit)))

    @server.tool(annotations=reading, description=(
        '读某一天实时输入的文字，按说话的先后，每行是“[时:分:秒] 说话人N：内容”。date 写成 2026-11-14，不写就是今天；'
        'start_time 和 end_time 可以只读一段时间，写成 14:30。一次最多 1000 句，没读完时用返回的 next_offset 接着读。'))
    def read_text(date: str | None = None, start_time: str | None = None, end_time: str | None = None,
                  offset: int = 0, limit: int = 300) -> dict:
        return answer(lambda: saved.read_text(date, start_time, end_time, offset, limit))

    @server.tool(annotations=reading, description=(
        '读最近几分钟实时输入的文字（默认 10 分钟），用来回答“刚才说了什么”。'
        '返回里的 session_open 为真表示最新的一次记录还没有结束，last_sentence_at 是最后一句话的时间。'))
    def latest_text(minutes: int = 10, limit: int = 200) -> dict:
        return answer(lambda: saved.latest_text(minutes, limit))

    @server.tool(annotations=reading, description=(
        '在实时文字和录音的文字里找词，多个词用空格隔开，要同时出现在同一句里。可以用 start_date、end_date 限定日期。'
        '最近的排在前面；实时文字的结果带时间和说话人，录音的结果带录音的 id 和标题。'))
    def search_text(query: str, start_date: str | None = None, end_date: str | None = None, limit: int = 50) -> dict:
        return answer(lambda: saved.search(query, start_date, end_date, limit))

    @server.tool(annotations=reading, description='列出资料库里的录音，最近的在前：id、标题、录音时间、时长、文字的字数、有没有总结。可以用 start_date、end_date 限定日期。')
    def list_recordings(start_date: str | None = None, end_date: str | None = None, limit: int = 50) -> dict:
        return answer(lambda: dict(recordings=saved.recordings(start_date, end_date, limit)))

    @server.tool(annotations=reading, description='读资料库里一条录音的文字和总结。recording_id 来自 list_recordings 或 search_text。文字很长时用 next_offset 接着读。')
    def read_recording(recording_id: str, offset: int = 0, limit: int = 20000) -> dict:
        return answer(lambda: saved.read_recording(recording_id, offset, limit))

    return server


def main() -> None:
    import anyio
    anyio.run(build().run_stdio_async)


if __name__ == '__main__':
    main()
