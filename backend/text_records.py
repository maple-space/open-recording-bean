"""Reading the text records of realtime input: one file per session, one line per sentence.

    2026-11-14_09-30-00.txt
        开始：2026-11-14 09:30:00
        —— 2026-11-14 ——
        [09:30:05] 说话人1：……
        结束：2026-11-14 10:02:11，共 120 句，2,345 字

Only the reading side lives here, and it needs nothing but the standard library. The page's
reader (backend/live.py) and the agents' window (backend/mcp_server.py) both read a file through
it, so they cannot disagree about what a file says. The files are written in backend/live.py.
"""
import re
from pathlib import Path

TRANSCRIPT_NAME = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?\.txt$')
DAY_LINE = re.compile(r'^—— (\d{4}-\d{2}-\d{2}) ——$')
SAID_LINE = re.compile(r'^\[(\d{2}):\d{2}:\d{2}\] ')
SPEAKER = re.compile(r'^说话人(\d{1,4})：')          # written in front of a sentence when its speaker could be told
STAMP = '%Y-%m-%d %H:%M:%S'


def parse_record(path: Path) -> dict:
    """A record file as the reader needs it: header fields, every sentence, and where each hour begins.

    Days and times are kept exactly as written in the file, so the reader shows what the file says.
    """
    day = path.name[:10]                        # until the first date line: the day in the file's name
    head = {'started': None, 'ended': None, 'device': '', 'model': ''}
    lines, hours, characters = [], [], 0
    with path.open(encoding='utf-8', errors='replace') as source:
        for raw in source:
            line = raw.rstrip('\n')
            said = SAID_LINE.match(line)
            if said:
                hour, text = int(said.group(1)), line[11:]
                tagged = SPEAKER.match(text)
                speaker = int(tagged.group(1)) if tagged else None
                if tagged:
                    text = text[tagged.end():]
                if not hours or (hours[-1]['day'], hours[-1]['hour']) != (day, hour):
                    hours.append({'day': day, 'hour': hour, 'first': len(lines), 'sentences': 0, 'characters': 0})
                hours[-1]['sentences'] += 1
                hours[-1]['characters'] += len(text)
                characters += len(text)
                lines.append((day, line[1:9], text, speaker))
                continue
            date = DAY_LINE.match(line)
            if date:
                day = date.group(1)
            elif line.startswith('结束：'):
                head['ended'] = line[3:22]
            elif not lines:
                for key, label in (('started', '开始：'), ('device', '设备：'), ('model', '模型：')):
                    if line.startswith(label):
                        head[key] = line[len(label):]
    return dict(head, lines=lines, hours=hours, characters=characters)
