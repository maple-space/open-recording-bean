"""Realtime input mode: recorder -> this service -> virtual sound card and/or live text.

One switch, owned by the local service. While it is on, the recorder records
and streams; every frame is decoded once and sent to the chosen places:
- an output device (a virtual sound card), where an input method such as
  Doubao or Typeless reads it as a microphone;
- the local speech model, whose text the page shows.
It stays on across Bluetooth reconnects until it is switched off here, the
recording is ended on the recorder, or the recorder is disconnected.
Nothing leaves this computer and no audio is written to disk here. The text of each
session is kept in its own file, appended sentence by sentence and never overwritten.
"""
import asyncio
import os
import re
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .audio_bridge import OutputBridge, list_outputs
from .feishu_live import stream_live
from .speakers import Voices

try:  # optional: PyAV decodes the recorder's Opus frames
    import av
    import numpy as np
except Exception:  # pragma: no cover - missing optional dependency
    av = np = None

MISSING = '未安装实时输入依赖。请在项目根目录使用 .venv 执行：python -m pip install -r backend/requirements-live.txt'
OPUS_HEAD = b'OpusHead' + struct.pack('<BBHIhB', 1, 1, 0, 16000, 0, 0)
PCM_LIMIT = 16000 * 2 * 30          # at most 30 s of speech waits for the model
KEPT_SEGMENTS = 5000                # what the page can scroll back through; the file keeps everything
TRANSCRIPT_NAME = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(-\d+)?\.txt$')
DAY_LINE = re.compile(r'^—— (\d{4}-\d{2}-\d{2}) ——$')
SAID_LINE = re.compile(r'^\[(\d{2}):\d{2}:\d{2}\] ')
SPEAKER = re.compile(r'^说话人(\d{1,4})：')          # written in front of a sentence when its speaker could be told
WORDS = re.compile(r'[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]')    # letters, digits, Chinese, Japanese, Korean
NEW_SPEAKER_WORDS = 4                               # fewer than this is a word or a noise: not judged by its voice
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


def show_in_finder(path: Path):
    if sys.platform != 'darwin':
        raise ValueError(f'只有在 Mac 上才能在访达中显示。文件位置：{path}')
    try:
        subprocess.run(['/usr/bin/open', '-R', str(path)], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        raise ValueError(f'没能打开访达。文件位置：{path}') from None


class TranscriptLog:
    """One text file per realtime input session: the complete record of what was heard.

    A sentence is appended the moment it settles, so nothing that reached the screen is lost
    when the service stops. A session never writes into another session's file and nothing is
    ever trimmed: the page keeps the newest KEPT_SEGMENTS sentences, the file keeps all of them.
    """

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else None
        self.path = None                    # the file of the current or most recent session
        self.open = False
        self.started = None                 # epoch seconds; also tells the page one session from the next
        self.model = self.device_name = ''
        self.day = None
        self.sentences = self.characters = 0
        self.error = None
        self.counted = {}                   # finished files: name -> (size, mtime_ns, sentences, characters)
        self.parsed = {}                    # records opened in the reader: name -> (size, mtime_ns, record)
        self.reading = threading.Lock()

    def begin(self, device_name, model, started=None):
        self.end()
        self.started = time.time() if started is None else started
        self.path, self.open, self.day = None, True, None
        self.device_name, self.model = device_name or '', model or ''
        self.sentences = self.characters = 0
        self.error = None

    def write(self, at: float, text: str, speaker: int | None = None):
        if not self.open:
            return
        text = text.replace('\r', ' ').replace('\n', ' ')
        self.sentences += 1
        self.characters += len(text)
        if self.directory is None:
            return
        moment = datetime.fromtimestamp(at)
        day = f'{moment:%Y-%m-%d}'
        try:
            if self.path is None:               # created with the first sentence: a silent session leaves no file
                self.path = self._create()
            with self.path.open('a', encoding='utf-8') as sink:
                if day != self.day:             # a session can run for days; the date is written when it changes
                    sink.write(f'\n—— {day} ——\n')
                sink.write(f'[{moment:%H:%M:%S}] {f"说话人{speaker}：" if speaker else ""}{text}\n')
            self.day, self.error = day, None
        except OSError as error:
            self.error = f'文字没有写进文件（{type(error).__name__}）。页面上仍然可以看到和复制。'

    def _create(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        began = datetime.fromtimestamp(self.started)
        for suffix in ('', *(f'-{number}' for number in range(2, 1000))):
            path = self.directory / f'{began:%Y-%m-%d_%H-%M-%S}{suffix}.txt'
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)     # never an existing file
            except FileExistsError:
                continue
            with os.fdopen(descriptor, 'w', encoding='utf-8') as sink:
                sink.write(f'实时输入文字记录\n开始：{began:%Y-%m-%d %H:%M:%S}\n'
                           f'设备：{self.device_name or "录音豆"}\n模型：{self.model or "本机模型"}\n')
            return path
        raise FileExistsError('no free file name')

    def end(self):
        if self.open and self.path is not None:
            with suppress(OSError):
                with self.path.open('a', encoding='utf-8') as sink:
                    sink.write(f'\n结束：{datetime.now():%Y-%m-%d %H:%M:%S}，共 {self.sentences} 句，{self.characters} 字\n')
        self.open = False

    def current(self) -> dict:
        return {'started': self.started, 'open': self.open, 'saving': self.directory is not None,
                'file': self.path.name if self.path else None, 'sentences': self.sentences,
                'characters': self.characters, 'error': self.error}

    def sessions(self) -> list:
        """Every session's file, newest first."""
        rows = []
        if self.directory is None or not self.directory.is_dir():
            return rows
        found = []
        for path in self.directory.iterdir():
            match = TRANSCRIPT_NAME.match(path.name)
            if match and path.is_file():
                found.append(((path.name[:19], int((match.group(1) or '-1')[1:])), path))      # start time, then -2, -3…
        for _, path in sorted(found, reverse=True):
            try:
                stat = path.stat()
                began = datetime.strptime(path.name[:19], '%Y-%m-%d_%H-%M-%S').timestamp()
            except (OSError, ValueError):
                continue
            active = self.open and path == self.path
            sentences, characters = (self.sentences, self.characters) if active else self._count(path, stat)
            rows.append({'name': path.name, 'started': began, 'active': active, 'bytes': stat.st_size,
                         'seconds': max(0, round((time.time() if active else stat.st_mtime) - began)),
                         'sentences': sentences, 'characters': characters})
        return rows

    def _count(self, path, stat):
        known = self.counted.get(path.name)
        if known and known[:2] == (stat.st_size, stat.st_mtime_ns):
            return known[2:]
        sentences = characters = 0
        with suppress(OSError):
            with path.open(encoding='utf-8', errors='replace') as source:
                for line in source:
                    if line.startswith('['):            # "[HH:MM:SS] text", perhaps with "说话人N：" in front of the text
                        sentences += 1
                        characters += len(SPEAKER.sub('', line.rstrip('\n')[11:], count=1))
        self.counted[path.name] = (stat.st_size, stat.st_mtime_ns, sentences, characters)
        return sentences, characters

    def file(self, name: str) -> Path:
        if self.directory is None or not TRANSCRIPT_NAME.match(name) or not (self.directory / name).is_file():
            raise KeyError(name)
        return self.directory / name

    # ---- reading a record back ------------------------------------------------ #
    def read(self, name: str) -> dict:
        path = self.file(name)
        with self.reading:
            stat = path.stat()
            known = self.parsed.get(name)
            if known and known[:2] == (stat.st_size, stat.st_mtime_ns):
                return known[2]
            record = parse_record(path)
            self.parsed.pop(name, None)
            self.parsed[name] = (stat.st_size, stat.st_mtime_ns, record)
            while len(self.parsed) > 2:                 # a long record is some megabytes in memory
                self.parsed.pop(next(iter(self.parsed)))
            return record

    def outline(self, name: str) -> dict:
        """What the reader shows before any text: totals, and each hour with where it begins."""
        record = self.read(name)
        lines = record['lines']
        active = self.open and self.path is not None and self.path.name == name
        started = record['started'] or f'{name[:10]} {name[11:19].replace("-", ":")}'
        ended = None if active else record['ended'] or (f'{lines[-1][0]} {lines[-1][1]}' if lines else started)
        try:
            until = datetime.now() if active else datetime.strptime(ended, STAMP)
            seconds = max(0, round((until - datetime.strptime(started, STAMP)).total_seconds()))
        except ValueError:
            seconds = None
        return {'name': name, 'active': active, 'started': started, 'ended': ended, 'seconds': seconds,
                'device': record['device'], 'model': record['model'], 'sentences': len(lines),
                'characters': record['characters'], 'hours': record['hours'],
                'speakers': len({line[3] for line in lines if line[3]})}

    def lines(self, name: str, first: int = 0, count: int = 1000) -> dict:
        rows = self.read(name)['lines']
        first = max(0, first)
        return {'first': first, 'total': len(rows),
                'lines': [{'index': first + offset, 'day': day, 'time': clock, 'text': text, 'speaker': speaker}
                          for offset, (day, clock, text, speaker) in enumerate(rows[first:first + max(0, count)])]}

    def search(self, name: str, query: str, limit: int = 200) -> dict:
        """Sentences that contain every word of the query, in the order they were said."""
        terms = query.split()
        if not terms:
            raise ValueError('请输入要搜索的词。')
        folded = [term.casefold() for term in terms]
        matches, total = [], 0
        for index, (day, clock, text, speaker) in enumerate(self.read(name)['lines']):
            low = text.casefold()
            if all(term in low for term in folded):
                total += 1
                if len(matches) < limit:
                    matches.append({'index': index, 'day': day, 'time': clock, 'text': text, 'speaker': speaker})
        return {'query': query, 'terms': terms, 'total': total, 'matches': matches}


class LiveStartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    output: str | None = Field(default=None, max_length=200)
    transcribe: bool = True
    model: str | None = Field(default=None, max_length=160)
    language: Literal['zh', 'en', 'auto'] = 'zh'
    speakers: bool = True


class LiveTextRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    on: bool
    model: str | None = Field(default=None, max_length=160)
    language: Literal['zh', 'en', 'auto'] | None = None


class LiveInput:
    def __init__(self, device, realtime, *, bridge_factory=OutputBridge, stream=stream_live,
                 retry_delays=(1, 2, 5, 10, 30), settle_seconds=15.0, transcripts=None):
        self.device, self.realtime = device, realtime
        self.log = TranscriptLog(transcripts)
        self.reveal = show_in_finder
        self.bridge_factory, self.stream, self.delays = bridge_factory, stream, retry_delays
        self.settle_seconds = settle_seconds
        self.settling = None
        self.switch = asyncio.Lock()
        self.enabled = False
        self.task = None
        self.stop_event = asyncio.Event()
        self.phase = 'off'
        self.error = self.notice = self.since = None
        self.options = {}
        self.stats = {}
        self.resume = {}
        self.attempt = 0
        self.totals = self._zero()
        self.levels = deque(maxlen=200)
        self.last_audio_at = None
        self.bridge = None
        self.output_error = None
        self.next_output_check = 0.0
        self.asr = None
        self.pcm = bytearray()
        self.voices = Voices()               # who is who in the current record; numbers only
        self.want_speakers = True
        self.segments = deque(maxlen=KEPT_SEGMENTS)
        self.next_index = 0
        self.partial = None
        self.lag = 0.0
        self.transcript_error = None
        self.codec = self.to_float = self.to_pcm = None

    @staticmethod
    def _zero():
        return dict(frames=0, audio_seconds=0.0, decode_errors=0, lost_frames=0, duplicate_frames=0, stalls=0,
                    retries=0, speech_dropped_seconds=0.0)

    # ---- switch ------------------------------------------------------------ #
    async def start(self, output=None, transcribe=True, model=None, language='zh', speakers=True) -> dict:
        async with self.switch:
            if self.task is not None and not self.task.done():
                raise ValueError('实时输入已经开启。要更换输出设备或模型，请先关闭再开启。')
            if self.device is None:
                raise ValueError('这个服务没有启用录音豆的蓝牙连接。')
            if not output and not transcribe:
                raise ValueError('请至少选择一个去向：输出到虚拟声卡，或在网页上显示文字。')
            if av is None:
                raise ValueError(MISSING)
            if not self.device.snapshot()['connected'] and not self.device.desired:
                raise ValueError('录音豆未连接。请先在“录音豆”页面连接设备，再开启实时输入。')
            await self._end_settling()               # switched on again at once: no need to wait for the file to close
            bridge = asr = None
            if output:
                bridge = self._open_output(output)
            if transcribe:
                try:
                    session = await self.realtime.start(model, language, speakers=bool(speakers))
                except BaseException:
                    if bridge:
                        bridge.close()
                    raise
                asr = dict(id=session['id'], cursor=0, model=session['model'], language=language,
                           speakers=bool(session.get('speakers')), restarted_at=0.0)
            self.bridge, self.asr = bridge, asr
            self.want_speakers = bool(speakers)
            self.voices = Voices()                   # a new record numbers its speakers from 1 again
            self.options = dict(output=output or None, transcribe=bool(transcribe), model=asr and asr['model'], language=language,
                                speakers=bool(asr and asr['speakers']))
            self.enabled, self.phase, self.attempt = True, 'starting', 0
            self.error = self.notice = self.output_error = self.transcript_error = None
            self.partial, self.stats, self.totals = None, {}, self._zero()
            self.pcm.clear()
            self.levels.clear()
            self.last_audio_at = None
            self.since = time.time()
            self.segments.clear()                    # each session is its own record; earlier ones are in their files
            self.log.begin(self.device.snapshot().get('device_name'), asr and asr['model'], self.since)
            self.stop_event = asyncio.Event()
            self.task = asyncio.create_task(self._run())
            return self.snapshot(self.next_index)

    async def stop(self) -> dict:
        async with self.switch:
            task, self.task = self.task, None
            self.enabled = False
            self.stop_event.set()
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), 25)
                except (asyncio.TimeoutError, TimeoutError):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                except Exception:
                    pass
            return self.snapshot(self.next_index)

    async def set_transcribe(self, on: bool, model=None, language=None) -> dict:
        """Turn the page text on or off while the sound keeps flowing."""
        async with self.switch:
            if not self.enabled:
                raise ValueError('实时输入没有开启。请先开启，再选择是否显示文字。')
            if on and not self.asr:
                language = language or self.options.get('language') or 'zh'
                session = await self.realtime.start(model, language, speakers=self.want_speakers)
                self.pcm.clear()
                self.transcript_error = None
                self.asr = dict(id=session['id'], cursor=0, model=session['model'], language=language,
                                speakers=bool(session.get('speakers')), restarted_at=0.0)
                self.options.update(transcribe=True, model=session['model'], language=language, speakers=self.asr['speakers'])
                self.log.model = self.log.model or session['model']
            elif not on and self.asr:
                asr, self.asr = self.asr, None
                self.options.update(transcribe=False, speakers=False)
                if asr['id']:
                    with suppress(Exception):               # keep what was already said
                        self._absorb(asr, await self.realtime.finish(asr['id'], asr['cursor'], voices=True))
                self.pcm.clear()
                self.partial = None
            return self.snapshot(self.next_index)

    def clear_transcript(self) -> dict:
        """Empties the page. The session's file is left as it is."""
        self.segments.clear()
        self.partial = None
        return self.snapshot(self.next_index)

    def _open_output(self, name):
        try:
            bridge = self.bridge_factory(name)
            bridge.start()
            return bridge
        except ValueError:
            raise
        except Exception as error:
            raise ValueError(f'无法打开输出设备“{name}”（{type(error).__name__}）。请确认它仍在系统的声音设备列表里。') from None

    # ---- supervisor --------------------------------------------------------- #
    async def _sleep(self, seconds):
        with suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(self.stop_event.wait(), seconds)

    async def _run(self):
        pump = asyncio.create_task(self._pump())
        try:
            while self.enabled:
                if not self.device.snapshot()['connected'] or self.device.lock.locked():
                    if not self.device.desired:
                        self.notice = '录音豆已断开，实时输入已关闭。'
                        break
                    self.phase = 'waiting_device'
                    await self._sleep(1)
                    continue
                self._new_decoder()
                self.phase = 'starting'
                self.stats = stats = {}
                try:
                    result = await self.stream(self.device, self._frame, self.stop_event, stats, self.resume)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._fold(stats)
                    if not self.enabled:
                        break
                    self.error = str(error) if isinstance(error, ValueError) else '实时音频意外中断，正在自动重试。'
                    self.phase = 'retrying'
                    self.totals['retries'] += 1
                    await self._sleep(self.delays[min(self.attempt, len(self.delays) - 1)])
                    self.attempt += 1
                    continue
                self._fold(stats)
                if result == 'stopped' and self.settle_seconds > 0:
                    self.settling = asyncio.create_task(self._settle())
                if result == 'device_stopped':
                    self.notice = '录音豆上已结束录音，实时输入已关闭。需要时请重新开启。'
                elif result == 'disconnect_requested':
                    self.notice = '录音豆已断开，实时输入已关闭。'
                break
        finally:
            self.enabled = False
            self.phase = 'off'
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            await self._close_outputs()

    async def _settle(self):
        """After switching off, keep the link down long enough for the recorder to close the recording.

        The recorder finalises a paused recording some seconds after BLE disconnects. Holding
        the session lock keeps the supervisor from reconnecting before that, so the recording
        shows up as a finished file instead of staying paused.
        """
        device = self.device
        with suppress(asyncio.CancelledError):
            for _ in range(3):
                async with device.lock:
                    if device.snapshot()['connected']:
                        if device.state.get('recordStatus') != 2:
                            return
                        # Reconnected before the recorder had closed the file (seen on the device:
                        # it does not always manage within the first hold). Drop the link once more.
                        with suppress(Exception):
                            await device._close_client()
                        device.state['connection_status'] = 'reconnecting' if device.desired else 'disconnected'
                    await asyncio.sleep(self.settle_seconds)
                for _ in range(120):                  # the supervisor reconnects; then look at what it read
                    if device.snapshot()['connected'] or not device.desired:
                        break
                    await asyncio.sleep(.25)
                if device.state.get('recordStatus') != 2:
                    return

    async def _end_settling(self):
        task, self.settling = self.settling, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _fold(self, stats):
        for key in ('lost_frames', 'duplicate_frames', 'stalls'):
            self.totals[key] += stats.get(key, 0)
        if stats.get('last_frame_at'):
            self.last_audio_at = stats['last_frame_at']
        self.stats = {}

    # ---- audio --------------------------------------------------------------- #
    def _new_decoder(self):
        self.codec = av.CodecContext.create('opus', 'r')
        self.codec.extradata = OPUS_HEAD
        self.codec.open()
        self.to_float = av.AudioResampler(format='flt', layout='mono', rate=self.bridge.rate if self.bridge else 16000)
        self.to_pcm = av.AudioResampler(format='s16', layout='mono', rate=16000)

    def _frame(self, sequence, data):
        self.totals['frames'] += 1
        self.attempt = 0
        self.error = None
        try:
            for frame in self.codec.decode(av.Packet(data)):
                self.totals['audio_seconds'] += frame.samples / frame.sample_rate
                for out in self.to_float.resample(frame):
                    mono = out.to_ndarray().reshape(-1)
                    if mono.size:
                        self.levels.append((time.monotonic(), float(np.abs(mono).max())))
                        if self.bridge:
                            self.bridge.push(mono)
                if self.asr:
                    for out in self.to_pcm.resample(frame):
                        self.pcm.extend(out.to_ndarray().tobytes())
                    if len(self.pcm) > PCM_LIMIT:               # the model cannot keep up: keep the newest speech
                        extra = len(self.pcm) - PCM_LIMIT
                        del self.pcm[:extra]
                        self.totals['speech_dropped_seconds'] += extra / 32000
        except Exception:
            self.totals['decode_errors'] += 1

    def _watch_output(self):
        """A sound device can disappear (unplugged, driver restarted); reopen it when it returns."""
        now = time.monotonic()
        if not self.bridge or now < self.next_output_check:
            return
        self.next_output_check = now + 10
        if self.bridge.active:
            self.output_error = None
            return
        name = self.options.get('output')
        try:
            self.bridge.close()
            self.bridge = self._open_output(name)
            self.to_float = av.AudioResampler(format='flt', layout='mono', rate=self.bridge.rate)
            self.output_error = None
        except Exception as error:
            self.output_error = str(error) if isinstance(error, ValueError) else f'输出设备“{name}”已停止，正在等待它恢复。'

    # ---- text ------------------------------------------------------------------ #
    async def _pump(self):
        while True:
            await asyncio.sleep(0.1)
            self._watch_output()
            asr = self.asr
            if not asr:
                continue
            chunk = bytes(self.pcm)
            del self.pcm[:len(chunk)]
            try:
                if not asr['id']:                               # dormant after a long silence: wake with new audio
                    if not chunk:
                        continue
                    session = await self.realtime.start(asr['model'], asr['language'], speakers=asr.get('speakers', False))
                    asr.update(id=session['id'], cursor=0)
                if chunk:
                    state = await self.realtime.push(asr['id'], chunk, asr['cursor'], voices=True)
                else:
                    state = self.realtime.snapshot(asr['id'], asr['cursor'], voices=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:                          # replaced, idled out, or the worker stopped
                self._asr_lost(asr, bool(chunk), str(error) if isinstance(error, ValueError) else '')
                continue
            self._absorb(asr, state)
            if state.get('error') or state.get('closed'):
                self._asr_lost(asr, bool(chunk), state.get('error') or '')

    def _asr_lost(self, asr, had_audio, reason):
        now = time.monotonic()
        asr['id'] = None
        self.partial = None
        if had_audio and now - asr['restarted_at'] < 30:        # failing with audio twice in a row: stop trying
            self.asr = None
            self.transcript_error = (reason or '本地语音模型已停止') + ' 实时文字已关闭，声音输出不受影响。'
        asr['restarted_at'] = now

    def _absorb(self, asr, state):
        for segment in state.get('segments') or []:
            text = (segment.get('text') or '').strip()
            words = len(WORDS.findall(text))
            if words:                                       # a noise the model turned into a bare full stop is not a sentence
                at = round(time.time(), 1)
                speaker = None
                if asr.get('speakers'):                     # the same voices keep their numbers across a model restart
                    with suppress(Exception):
                        speaker = self.voices.hear(segment.get('voice'), float(segment.get('end', 0)) - float(segment.get('start', 0)),
                                                   may_open=words >= NEW_SPEAKER_WORDS)
                self.segments.append(dict(index=self.next_index, at=at, text=text, speaker=speaker))
                self.next_index += 1
                self.log.write(at, text, speaker)
        asr['cursor'] = state.get('segment_count', asr['cursor'])
        self.partial = ((state.get('partial') or {}).get('text') or '').strip() or None
        self.lag = state.get('lag_seconds') or 0.0

    async def _close_outputs(self):
        bridge, self.bridge = self.bridge, None
        if bridge:
            with suppress(Exception):
                await asyncio.to_thread(bridge.close)
        asr, self.asr = self.asr, None
        if asr and asr['id']:
            with suppress(Exception):
                chunk = bytes(self.pcm)
                if chunk:
                    self._absorb(asr, await self.realtime.push(asr['id'], chunk, asr['cursor'], voices=True))
                self._absorb(asr, await self.realtime.finish(asr['id'], asr['cursor'], voices=True))
        self.pcm.clear()
        self.partial = None
        self.log.end()

    # ---- view -------------------------------------------------------------------- #
    def _status(self):
        if not self.enabled:
            return 'off'
        if self.stats.get('paused'):
            return 'paused'
        last = self.stats.get('last_frame_at')
        if last and time.monotonic() - last < 3:
            return 'streaming'
        return self.phase

    def snapshot(self, cursor: int = 0) -> dict:
        device = self.device.snapshot() if self.device is not None else {}
        info = device.get('info') or {}
        now = time.monotonic()
        last = self.stats.get('last_frame_at') or self.last_audio_at
        totals = self.totals
        output = None
        if self.bridge:
            output = dict(self.bridge.stats(), error=self.output_error)
        elif self.options.get('output') and self.enabled:
            output = dict(name=self.options['output'], active=False, error=self.output_error)
        return {
            'available': self.device is not None and av is not None,
            'unavailable_reason': None if av is not None else MISSING,
            'enabled': self.enabled, 'status': self._status(), 'error': self.error, 'notice': self.notice,
            'since': self.since, 'options': self.options,
            'device': {'connected': bool(device.get('connected')), 'name': device.get('device_name') or '',
                       'connection_status': device.get('connection_status'), 'battery': info.get('battery'),
                       'charging': info.get('charging'), 'free_kb': info.get('freeMemoryKb'),
                       'total_kb': info.get('totalMemoryKb'), 'record_status': device.get('recordStatus', 0)},
            'audio': {'frames': totals['frames'], 'seconds': round(totals['audio_seconds'], 1),
                      'lost_frames': totals['lost_frames'] + self.stats.get('lost_frames', 0),
                      'decode_errors': totals['decode_errors'], 'retries': totals['retries'],
                      'stalls': totals['stalls'] + self.stats.get('stalls', 0),
                      'level': round(max((peak for at, peak in self.levels if now - at <= 1.0), default=0.0), 4),
                      'seconds_since_audio': None if last is None else round(now - last, 1)},
            'output': output,
            'transcript': {'on': bool(self.asr), 'model': self.options.get('model'), 'language': self.options.get('language'),
                           'segments': [row for row in self.segments if row['index'] >= cursor],
                           'session': self.log.current(), 'kept': KEPT_SEGMENTS,
                           'speakers': {'on': bool(self.asr and self.asr.get('speakers')), 'count': len(self.voices)},
                           'next_cursor': self.next_index, 'partial': self.partial, 'lag_seconds': self.lag,
                           'dropped_seconds': round(totals['speech_dropped_seconds'], 1), 'error': self.transcript_error},
        }


def register_live(app, manager, devices, realtime, transcripts=None):
    """Routes for the realtime input switch. The recorder session itself stays in FeishuDevice."""
    live = LiveInput(getattr(devices, 'feishu', None), realtime, transcripts=transcripts)
    app.state.live = live

    @app.get('/api/live')
    async def live_state(cursor: int = 0):
        return live.snapshot(max(0, cursor))

    @app.get('/api/live/transcripts')
    async def live_transcripts():
        directory = live.log.directory
        return {'directory': str(directory) if directory else None, 'sessions': await asyncio.to_thread(live.log.sessions)}

    @app.get('/api/live/transcripts/{name}')
    async def live_transcript(name: str):
        try:
            path = live.log.file(name)
        except KeyError:
            raise HTTPException(404, '没有这份文字记录') from None
        return FileResponse(path, media_type='text/plain; charset=utf-8', filename=name, content_disposition_type='inline')

    async def reading(call, *arguments):
        try:
            return await asyncio.to_thread(call, *arguments)
        except KeyError:
            raise HTTPException(404, '没有这份文字记录') from None

    @app.get('/api/live/transcripts/{name}/outline')
    async def live_transcript_outline(name: str):
        return await reading(live.log.outline, name)

    @app.get('/api/live/transcripts/{name}/lines')
    async def live_transcript_lines(name: str, first: int = Query(0, ge=0), count: int = Query(1000, ge=1, le=4000)):
        return await reading(live.log.lines, name, first, count)

    @app.get('/api/live/transcripts/{name}/search')
    async def live_transcript_search(name: str, q: str = Query(..., min_length=1, max_length=100),
                                     limit: int = Query(200, ge=1, le=500)):
        return await reading(live.log.search, name, q, limit)

    @app.post('/api/live/transcripts/{name}/reveal')
    async def live_transcript_reveal(name: str):
        await reading(lambda: live.reveal(live.log.file(name)))
        return {'ok': True}

    @app.get('/api/live/outputs')
    async def live_outputs():
        return await asyncio.to_thread(list_outputs)

    @app.post('/api/live/start')
    async def live_start(body: LiveStartRequest):
        if any(manager.get(job_id).get('device_type') == 'feishu' for job_id, task in list(manager.tasks.items()) if not task.done()):
            raise HTTPException(409, '录音豆正在执行后台任务（例如下载），请等它完成后再开启实时输入。')
        return await live.start(body.output, body.transcribe, body.model, body.language, body.speakers)

    @app.post('/api/live/stop')
    async def live_stop():
        return await live.stop()

    @app.post('/api/live/transcribe')
    async def live_text(body: LiveTextRequest):
        return await live.set_transcribe(body.on, body.model, body.language)

    @app.post('/api/live/transcript/clear')
    async def live_clear():
        return live.clear_transcript()

    return live
