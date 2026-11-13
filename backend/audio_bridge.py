"""Play decoded recorder audio into an output device, normally a virtual sound card.

A virtual sound card (BlackHole, Soundflower) is a loopback: whatever is played
into its output appears on its input, where a dictation app such as Doubao or
Typeless can select it as a microphone. This module only ever *plays*; it never
opens a microphone and never changes the system default devices.
"""
import threading
import time
from collections import deque

try:  # optional: only needed for the "send to an input method" path
    import numpy as np
    import sounddevice as sd
except Exception:  # pragma: no cover - missing optional dependency or PortAudio
    np = None
    sd = None

VIRTUAL_HINTS = ('blackhole', 'soundflower', 'loopback', 'vb-cable', 'virtual', '虚拟')
_active = 0
# PortAudio is not safe to re-scan while a stream is being opened or closed on another
# thread. Every control operation takes this lock; the audio callback never does.
_control = threading.RLock()


def looks_virtual(name: str) -> bool:
    lowered = (name or '').lower()
    return any(hint in lowered for hint in VIRTUAL_HINTS)


def list_outputs() -> dict:
    """Output-capable devices, virtual sound cards first."""
    if sd is None:
        return {'available': False, 'devices': [], 'suggested': None,
                'message': '未安装音频输出依赖。请在项目根目录使用 .venv 执行：python -m pip install sounddevice'}
    with _control:
        if _active == 0:
            # PortAudio caches its device list; re-scan so a newly installed
            # virtual sound card shows up without restarting the service.
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        try:
            default_output = sd.default.device[1]
            rows = []
            for index, device in enumerate(sd.query_devices()):
                if device['max_output_channels'] < 1:
                    continue
                rows.append({'name': device['name'], 'channels': int(device['max_output_channels']),
                             'sample_rate': int(device['default_samplerate']), 'virtual': looks_virtual(device['name']),
                             'loopback': device['max_input_channels'] >= 1, 'system_default': index == default_output})
        except Exception:
            return {'available': False, 'devices': [], 'suggested': None, 'message': '无法读取这台电脑的音频输出设备。'}
    rows.sort(key=lambda row: (not row['virtual'], row['name'].lower()))
    suggested = next((row['name'] for row in rows if row['virtual'] and row['loopback']), None)
    message = '已找到虚拟声卡。' if suggested else '没有找到虚拟声卡。请先按页面下方的说明安装 BlackHole。'
    return {'available': True, 'devices': rows, 'suggested': suggested, 'message': message}


class OutputBridge:
    """Mono-to-stereo playback into one named output device, kept a fraction of a second behind the recorder.

    The recorder delivers sound in clumps (about a quarter of a second at a time), so a small
    cushion always waits. Sound that arrives late and all at once (the Bluetooth link or this
    computer stalled for a moment) must not stay as delay: whoever is dictating would find the
    input method behind for the rest of the session. Such a backlog is worked off by taking the
    middle out of the pauses in it, which costs no speech. Only what does not fit under
    MAX_SECONDS even then is discarded, oldest first.
    """

    START_SECONDS = 0.30      # buffer this much before (re)starting playback: BLE delivers audio in bursts
    CATCH_UP_SECONDS = 0.60   # more than this waiting is a backlog: shorten the pauses in it...
    KEEP_SECONDS = 0.45       # ...until this much is left, which is where a fresh start settles
    MAX_SECONDS = 1.50        # never let latency grow beyond this; discard the oldest sound instead
    PIECE_SECONDS = 0.02      # pauses are judged in pieces of this length
    PAUSE_PIECES = 8          # this many quiet pieces in a row (160 ms) are a pause, not a gap inside a word
    PAUSE_HEAD = 3            # of a pause, the first pieces (the end of the last word)...
    PAUSE_TAIL = 2            # ...and the last ones (the start of the next word) always stay
    # What counts as quiet follows the rule of the page's own sentence detector (UtteranceSegmenter):
    # three times the room noise, taken as the tenth percentile of the last ten seconds.
    QUIET_FLOOR, QUIET_RATIO, QUIET_CEILING, QUIET_MARGIN = 0.006, 3.0, 0.08, 0.6

    def __init__(self, name: str):
        if sd is None:
            raise ValueError('未安装音频输出依赖 sounddevice，无法输出到虚拟声卡。')
        with _control:
            matches = [(index, device) for index, device in enumerate(sd.query_devices())
                       if device['name'] == name and device['max_output_channels'] >= 1]
            if not matches:
                raise ValueError(f'没有找到名为“{name}”的输出设备。请刷新设备列表后重新选择。')
            self.name = name
            self.index, device = matches[0]
            self.channels = min(2, int(device['max_output_channels']))
            self.rate = 48000
            try:
                sd.check_output_settings(device=self.index, samplerate=self.rate, channels=self.channels, dtype='float32')
            except Exception:
                self.rate = int(device['default_samplerate'])
        self._piece = max(1, round(self.rate * self.PIECE_SECONDS))
        self._blocks = deque()              # (samples, loudness) of each piece that waits to be played
        self._levels = deque(maxlen=500)    # loudness of every piece of the last ten seconds
        self._offset = 0
        self._queued = 0
        self._playing = False
        self._lock = threading.Lock()
        self.underflows = 0
        self.dropped_seconds = 0.0          # sound that was discarded because it did not fit
        self.skipped_seconds = 0.0          # pause that was taken out to catch up
        self.peak = 0.0
        self.played_seconds = 0.0
        self._stream = None

    def start(self):
        global _active
        with _control:
            self._stream = sd.OutputStream(device=self.index, samplerate=self.rate, channels=self.channels,
                                           dtype='float32', callback=self._callback)
            self._stream.start()
            _active += 1

    def push(self, mono):
        """mono: float32 samples at self.rate."""
        if not len(mono):
            return
        samples = np.asarray(mono, dtype=np.float32)
        pieces = []
        for start in range(0, len(samples), self._piece):
            part = samples[start:start + self._piece]
            pieces.append((np.repeat(part[:, None], self.channels, axis=1), float(np.sqrt(np.dot(part, part) / len(part)))))
        with self._lock:
            for piece in pieces:
                self._blocks.append(piece)
                self._levels.append(piece[1])
                self._queued += len(piece[0])
            if self._queued > self.CATCH_UP_SECONDS * self.rate:
                self._shorten_pauses()
            over = self._queued - int(self.MAX_SECONDS * self.rate)
            gone = 0
            while gone < over and len(self._blocks) > 1:        # still too much: the oldest sound goes
                gone += len(self._blocks.popleft()[0]) - self._offset
                self._offset = 0
            self._queued -= gone
            self.dropped_seconds += gone / self.rate

    def _quiet_below(self) -> float:
        if len(self._levels) < 50:
            return self.QUIET_FLOOR * self.QUIET_MARGIN
        noise = sorted(self._levels)[len(self._levels) // 10]
        return min(max(self.QUIET_FLOOR, noise * self.QUIET_RATIO), self.QUIET_CEILING) * self.QUIET_MARGIN

    def _shorten_pauses(self):
        """Take the middle out of the pauses that wait, oldest first, until the backlog is gone."""
        excess = self._queued - int(self.KEEP_SECONDS * self.rate)
        quiet = self._quiet_below()
        waiting = list(self._blocks)
        spare = []                                               # pieces that can go, oldest first
        began = None
        for index in range(1, len(waiting) + 1):                 # the first piece may be half played: it stays
            silent = index < len(waiting) and waiting[index][1] < quiet
            if silent and began is None:
                began = index
            elif not silent and began is not None:
                if index - began >= self.PAUSE_PIECES:
                    spare.extend(range(began + self.PAUSE_HEAD, index - self.PAUSE_TAIL))
                began = None
        gone, out = 0, set()
        for index in spare:
            if gone >= excess:
                break
            out.add(index)
            gone += len(waiting[index][0])
        if out:
            self._blocks = deque(piece for index, piece in enumerate(waiting) if index not in out)
            self._queued -= gone
            self.skipped_seconds += gone / self.rate

    def _callback(self, outdata, frames, timing, status):
        outdata.fill(0)
        with self._lock:
            if not self._playing:
                if self._queued < self.START_SECONDS * self.rate:
                    return
                self._playing = True
            written = 0
            while written < frames and self._blocks:
                block = self._blocks[0][0]
                count = min(frames - written, len(block) - self._offset)
                outdata[written:written + count] = block[self._offset:self._offset + count]
                written += count
                self._offset += count
                self._queued -= count
                if self._offset == len(block):
                    self._blocks.popleft()
                    self._offset = 0
            if written < frames:
                self.underflows += 1
                self._playing = False
            self.played_seconds += written / self.rate
            if written:
                self.peak = max(self.peak, float(np.abs(outdata[:written]).max()))

    @property
    def active(self) -> bool:
        return bool(self._stream and self._stream.active)

    def stats(self) -> dict:
        with self._lock:
            peak, self.peak = self.peak, 0.0
            return {'name': self.name, 'sample_rate': self.rate, 'queued_ms': round(self._queued * 1000 / self.rate),
                    'underflows': self.underflows, 'dropped_seconds': round(self.dropped_seconds, 2),
                    'skipped_seconds': round(self.skipped_seconds, 2),
                    'played_seconds': round(self.played_seconds, 1), 'peak': round(peak, 4),
                    'active': bool(self._stream and self._stream.active)}

    def close(self):
        global _active
        with _control:
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                finally:
                    _active = max(0, _active - 1)
        # Give PortAudio a moment before another stream reuses the device.
        time.sleep(0.05)
