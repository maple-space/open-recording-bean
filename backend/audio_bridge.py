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
    """Jitter-buffered mono-to-stereo playback into one named output device."""

    START_SECONDS = 0.30      # buffer this much before (re)starting playback: BLE delivers audio in bursts
    MAX_SECONDS = 1.50        # never let latency grow beyond this; drop the backlog instead

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
        self._blocks = deque()
        self._offset = 0
        self._queued = 0
        self._playing = False
        self._lock = threading.Lock()
        self.underflows = 0
        self.dropped_seconds = 0.0
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
        block = np.repeat(np.asarray(mono, dtype=np.float32)[:, None], self.channels, axis=1)
        with self._lock:
            if self._queued > self.MAX_SECONDS * self.rate:
                self.dropped_seconds += self._queued / self.rate
                self._blocks.clear()
                self._offset = self._queued = 0
                self._playing = False
            self._blocks.append(block)
            self._queued += len(block)

    def _callback(self, outdata, frames, timing, status):
        outdata.fill(0)
        with self._lock:
            if not self._playing:
                if self._queued < self.START_SECONDS * self.rate:
                    return
                self._playing = True
            written = 0
            while written < frames and self._blocks:
                block = self._blocks[0]
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
