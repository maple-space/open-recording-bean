"""Live, local speech-to-text for audio the web page decodes from the recorder.

The page posts 16 kHz mono PCM. One long-lived, offline worker process keeps a
cached faster-whisper model loaded, cuts the stream into utterances, and returns
text as soon as each utterance ends (plus a provisional text while it is still
being spoken). Audio is never written to disk and never leaves this machine.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import sys
import time
import uuid
from collections import deque
from contextlib import suppress
from pathlib import Path

try:  # numpy ships with the optional ASR dependencies, not the base server
    import numpy as np
except ImportError:  # pragma: no cover - exercised only without the ASR extras
    np = None

SAMPLE_RATE = 16000
MAX_CHUNK_BYTES = 4 * 1024 * 1024
LANGUAGES = ("zh", "en", "auto")


class RealtimeASRError(ValueError):
    """User-facing failure; the API layer turns it into a 400 response."""


# --------------------------------------------------------------------------- #
# Utterance segmentation (pure numpy, shared by the worker and the tests)
# --------------------------------------------------------------------------- #
class Utterance:
    __slots__ = ("start", "end", "samples")

    def __init__(self, start: float, end: float, samples):
        self.start, self.end, self.samples = start, end, samples


class UtteranceSegmenter:
    """Energy-based endpointing with an adaptive noise floor.

    Whisper itself re-checks every utterance with its Silero VAD, so this only
    has to decide *when a sentence is over*; false starts on noise cost one
    cheap, empty transcription.
    """

    def __init__(self, *, sample_rate: int = SAMPLE_RATE, frame_ms: int = 20, start_frames: int = 3,
                 end_silence_s: float = 0.7, preroll_s: float = 0.3, tail_s: float = 0.2,
                 max_utterance_s: float = 15.0, min_speech_s: float = 0.24,
                 floor: float = 0.006, ratio: float = 3.0, ceiling: float = 0.08,
                 soft_limit_s: float = 0.0, soft_silence_s: float = 0.2):
        self.sample_rate = sample_rate
        self.frame = sample_rate * frame_ms // 1000
        self.frame_s = frame_ms / 1000
        self.start_frames = start_frames
        self.end_silence_frames = max(1, round(end_silence_s / self.frame_s))
        self.tail_frames = max(0, round(tail_s / self.frame_s))
        self.max_frames = max(self.end_silence_frames + 1, round(max_utterance_s / self.frame_s))
        # Someone talking without long pauses still breathes between clauses. Once an utterance
        # is longer than the soft limit, a short pause is enough to end it, so text settles
        # sentence by sentence instead of waiting for the hard limit and a cut inside a word.
        self.soft_frames = round(soft_limit_s / self.frame_s) if soft_limit_s > 0 else 0
        self.soft_silence_frames = max(1, round(soft_silence_s / self.frame_s))
        self.min_speech_frames = max(1, round(min_speech_s / self.frame_s))
        self.floor, self.ratio, self.ceiling = floor, ratio, ceiling
        self._leftover = np.zeros(0, dtype=np.float32)
        self._position = 0                      # frames consumed so far
        self._history = deque(maxlen=max(1, round(preroll_s / self.frame_s)))
        self._noise = deque(maxlen=500)         # RMS of every frame in the last ~10 s
        self._run = []                          # loud frames seen while idle, not yet an utterance
        self._frames = None                     # list of frames while an utterance is open
        self._loud = None                       # per-frame loud flags of the open utterance
        self._rms = None
        self._start_frame = 0
        self._silence = 0

    # -- helpers ---------------------------------------------------------- #
    def threshold(self) -> float:
        # The 10th percentile of recent frame energy tracks the room noise even
        # while someone is talking, because speech always contains short gaps.
        if len(self._noise) < 50:
            return self.floor
        noise = sorted(self._noise)[len(self._noise) // 10]
        return min(max(self.floor, noise * self.ratio), self.ceiling)

    def _open(self, frames):
        self._frames = list(frames)
        self._start_frame = self._position + 1 - len(self._frames)
        self._loud = [False] * (len(self._frames) - len(self._run)) + [True] * len(self._run)
        self._rms = [0.0] * len(self._frames)
        self._silence = 0
        self._run = []
        self._history.clear()

    def _close(self, keep: int):
        """Finish the open utterance using its first *keep* frames; return it or None."""
        frames, loud = self._frames[:keep], self._loud[:keep]
        start = self._start_frame
        rest, rest_loud, rest_rms = self._frames[keep:], self._loud[keep:], self._rms[keep:]
        self._frames = self._loud = self._rms = None
        self._silence = 0
        utterance = None
        if frames and sum(loud) >= self.min_speech_frames:
            utterance = Utterance(start * self.frame_s, (start + len(frames)) * self.frame_s, np.concatenate(frames))
        return utterance, rest, rest_loud, rest_rms, start + keep

    def _frame(self, frame, rms: float, out: list):
        threshold = self.threshold()
        self._noise.append(rms)
        if self._frames is None:
            if rms >= threshold:
                self._run.append(frame)
                if len(self._run) >= self.start_frames:
                    self._open(list(self._history) + self._run)
            else:
                # A short blip was not speech: fold it into the pre-roll.
                for blip in self._run:
                    self._history.append(blip)
                self._run = []
                self._history.append(frame)
            return
        loud = rms >= threshold * 0.6           # hysteresis: stay "in speech" a little longer
        self._frames.append(frame)
        self._loud.append(loud)
        self._rms.append(rms)
        if loud:
            self._silence = 0
        else:
            self._silence += 1
        needed = self.end_silence_frames
        if self.soft_frames and len(self._frames) - self._silence >= self.soft_frames:
            needed = min(needed, self.soft_silence_frames)
        if self._silence >= needed:
            keep = len(self._frames) - self._silence + min(self._silence, self.tail_frames)
            utterance, rest, _, _, _ = self._close(keep)
            if utterance:
                out.append(utterance)
            for quiet in rest[-self._history.maxlen:]:
                self._history.append(quiet)
        elif len(self._frames) >= self.max_frames:
            # Someone is speaking without pausing: cut at the quietest recent frame.
            window = round(3.0 / self.frame_s)
            guard = round(0.2 / self.frame_s)
            lo = max(self.min_speech_frames, len(self._frames) - window)
            hi = max(lo + 1, len(self._frames) - guard)
            cut = lo + int(np.argmin(self._rms[lo:hi])) + 1
            utterance, rest, rest_loud, rest_rms, next_start = self._close(cut)
            if utterance:
                out.append(utterance)
            self._frames, self._loud, self._rms = list(rest), list(rest_loud), list(rest_rms)
            self._start_frame = next_start
            self._silence = 0
            for flag in reversed(self._loud):
                if flag:
                    break
                self._silence += 1

    # -- public API ------------------------------------------------------- #
    def feed(self, samples) -> list:
        """Add float32 samples in [-1, 1]; return the utterances that just ended."""
        out = []
        data = np.concatenate([self._leftover, np.asarray(samples, dtype=np.float32)]) if len(self._leftover) else np.asarray(samples, dtype=np.float32)
        count = len(data) // self.frame
        for index in range(count):
            frame = data[index * self.frame:(index + 1) * self.frame]
            rms = float(math.sqrt(float(np.dot(frame, frame)) / self.frame))
            self._frame(frame, rms, out)
            self._position += 1
        self._leftover = data[count * self.frame:].copy()
        return out

    def in_progress(self):
        """(start_seconds, samples) of the utterance being spoken, or None."""
        if self._frames is None or sum(self._loud) < self.min_speech_frames:
            return None
        return self._start_frame * self.frame_s, np.concatenate(self._frames)

    def flush(self) -> list:
        """End of stream: finish whatever is open."""
        out = []
        if self._frames is not None:
            utterance, *_ = self._close(len(self._frames))
            if utterance:
                out.append(utterance)
        self._run = []
        return out

    @property
    def seconds(self) -> float:
        return self._position * self.frame_s


# --------------------------------------------------------------------------- #
# Worker process
# --------------------------------------------------------------------------- #
class _FakeEngine:
    """Deterministic stand-in used by the tests (no model, no audio content)."""

    def transcribe(self, samples, final: bool) -> str:
        return f"{'语音' if final else '草稿'}{len(samples) / SAMPLE_RATE:.1f}秒"


class _WhisperEngine:
    def __init__(self, model_path: str, language: str):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model_path, device="cpu", compute_type="int8", local_files_only=True,
                                  cpu_threads=min(4, os.cpu_count() or 1))
        self.language = None if language == "auto" else language
        # Nudges Whisper towards Simplified Chinese punctuation and characters.
        self.prompt = "以下是普通话的句子，使用简体中文。" if language == "zh" else None

    def transcribe(self, samples, final: bool) -> str:
        segments, _ = self.model.transcribe(
            samples, language=self.language, beam_size=3 if final else 1, temperature=0.0,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False, initial_prompt=self.prompt, without_timestamps=True)
        return "".join(segment.text for segment in segments).strip()


class _SenseVoiceEngine:
    """Alibaba FunASR SenseVoice Small through sherpa-onnx: non-autoregressive, very fast on CPU."""

    def __init__(self, model: str, tokens: str, language: str):
        import sherpa_onnx

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model, tokens=tokens, num_threads=min(4, os.cpu_count() or 1), use_itn=True,
            language=language, debug=False)

    def transcribe(self, samples, final: bool) -> str:
        stream = self.recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self.recognizer.decode_stream(stream)
        return stream.result.text.strip()


class _Voiceprint:
    """A speaker model through sherpa-onnx: one vector per sentence, close for the same voice."""

    def __init__(self, model: str):
        self.extractor = None
        if model != "fake":                     # the tests' stand-in needs no model
            import sherpa_onnx

            self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model, num_threads=1))

    def of(self, samples):
        if self.extractor is None:
            return [1.0, 0.0, 0.0]
        stream = self.extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            return None
        return [round(float(value), 4) for value in self.extractor.compute(stream)]


def _build_engine(spec: dict, language: str):
    kind = spec.get("engine")
    if kind == "fake":
        return _FakeEngine()
    if kind == "sensevoice":
        return _SenseVoiceEngine(spec["model"], spec["tokens"], language)
    if kind == "whisper":
        return _WhisperEngine(spec["model_path"], language)
    raise ValueError("unknown engine")


def _worker_main(argv: list) -> int:
    import queue
    import threading

    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    if len(argv) != 4 or argv[1] != "--worker" or argv[3] not in LANGUAGES:
        return 2
    try:
        spec = json.loads(argv[2])
        engine = _build_engine(spec, argv[3])
    except Exception as error:
        emit({"type": "error", "message": f"本地语音模型加载失败（{type(error).__name__}）。请检查模型文件和对应的依赖是否完整。"})
        return 1
    voiceprint = None
    if spec.get("speaker_model"):
        try:
            voiceprint = _Voiceprint(spec["speaker_model"])
        except Exception:
            voiceprint = None                   # the text still works; sentences just get no speaker number
    # Drafts cost one extra model call each; slow engines get fewer of them.
    draft_gap = float(spec.get("draft_gap", 0.8))
    draft_min = float(spec.get("draft_min", 1.0))      # seconds of a sentence heard before its first draft

    inbox = queue.Queue()
    stdin = sys.stdin.buffer

    def reader():
        while True:
            head = stdin.read(4)
            if len(head) < 4:
                inbox.put(("eof", b""))
                return
            size = int.from_bytes(head, "little")
            if size == 0:
                inbox.put(("flush", b""))
                continue
            if size > MAX_CHUNK_BYTES or size % 2:
                inbox.put(("eof", b""))
                return
            data = stdin.read(size)
            if len(data) < size:
                inbox.put(("eof", b""))
                return
            inbox.put(("audio", data))

    threading.Thread(target=reader, daemon=True).start()
    segmenter = UtteranceSegmenter(end_silence_s=float(spec.get("end_silence", 0.7)),
                                   max_utterance_s=float(spec.get("max_utterance", 15.0)),
                                   soft_limit_s=float(spec.get("soft_limit", 0.0)),
                                   soft_silence_s=float(spec.get("soft_silence", 0.2)))
    index = 0
    partial_length = 0
    partial_shown = False
    last_status = None

    def status(processed: float):
        nonlocal last_status
        value = (round(segmenter.seconds, 2), round(processed, 2))
        if value != last_status:
            last_status = value
            emit({"type": "status", "received": value[0], "processed": value[1]})

    emit({"type": "ready"})
    while True:
        try:
            kind, data = inbox.get(timeout=0.25)
        except queue.Empty:
            kind, data = "idle", b""
        finals, flushed, done = [], False, False
        while True:  # take everything already queued before spending time on a model call
            if kind == "audio":
                finals.extend(segmenter.feed(np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0))
            elif kind == "flush":
                finals.extend(segmenter.flush())
                flushed = True
            elif kind == "eof":
                finals.extend(segmenter.flush())
                done = True
            if done or inbox.empty():
                break
            kind, data = inbox.get_nowait()
        try:
            for utterance in finals:
                text = engine.transcribe(utterance.samples, True)
                if text:
                    event = {"type": "segment", "index": index, "start": round(utterance.start, 2),
                             "end": round(utterance.end, 2), "text": text}
                    if voiceprint is not None:
                        with suppress(Exception):
                            event["voice"] = voiceprint.of(utterance.samples)
                    emit(event)
                    index += 1
                status(utterance.end)
            if finals:
                partial_length = 0
                if partial_shown:
                    emit({"type": "partial", "text": ""})
                    partial_shown = False
            if flushed:
                emit({"type": "flushed"})
            if done:
                return 0
            current = segmenter.in_progress() if inbox.empty() else None
            if current is not None:
                start, samples = current
                if draft_gap > 0 and len(samples) >= int(draft_min * SAMPLE_RATE) and len(samples) - partial_length >= int(draft_gap * SAMPLE_RATE):
                    partial_length = len(samples)
                    began = time.monotonic()
                    text = engine.transcribe(samples, False)
                    # Never let drafts consume more than about half of the CPU time.
                    draft_gap = max(float(spec.get("draft_gap", 0.8)), 2.0 * (time.monotonic() - began))
                    emit({"type": "partial", "start": round(start, 2), "text": text})
                    partial_shown = bool(text)
            if inbox.empty():
                # Nothing is waiting: everything heard so far has been handled.
                status(segmenter.seconds)
        except Exception as error:
            emit({"type": "error", "message": f"本地转写失败（{type(error).__name__}）。请检查模型文件和对应的依赖是否完整。"})
            return 1


# --------------------------------------------------------------------------- #
# In-server session manager
# --------------------------------------------------------------------------- #
class _Session:
    def __init__(self, session_id: str, model: str, language: str, process, speakers: bool = False):
        self.id, self.model, self.language, self.process = session_id, model, language, process
        self.speakers = speakers            # the worker also sends a voiceprint with every sentence
        self.voices = {}                    # sentence index -> voiceprint, until the caller has taken it
        self.segments = []
        self.partial = None
        self.error = None
        self.received = 0.0          # seconds of audio accepted from the page
        self.heard = 0.0             # seconds the worker has segmented
        self.processed = 0.0         # seconds the worker has finished transcribing
        self.ready = asyncio.Event()
        self.flushed = asyncio.Event()
        self.closed = False
        self.last_activity = time.monotonic()
        self.reader = None
        self.write_lock = asyncio.Lock()

    def snapshot(self, cursor: int = 0, voices: bool = False) -> dict:
        cursor = max(0, min(int(cursor), len(self.segments)))
        segments = self.segments[cursor:]
        if voices:                          # handed over once: a long session must not pile them up
            segments = [dict(row, voice=self.voices.pop(row["index"], None)) for row in segments]
        return {"id": self.id, "model": self.model, "language": self.language, "speakers": self.speakers,
                "segments": segments, "segment_count": len(self.segments),
                "partial": self.partial, "received_seconds": round(self.received, 2),
                "processed_seconds": round(self.processed, 2),
                "lag_seconds": round(max(0.0, self.received - self.processed), 2),
                "error": self.error, "closed": self.closed}


PROJECT = Path(__file__).resolve().parents[1]
SENSEVOICE_ID = "sensevoice-small"


def _asr_home() -> Path:
    return Path(os.environ.get("RECORDING_ASR_DIR") or PROJECT / ".local" / "asr").expanduser()


def _sensevoice_files():
    directory = Path(os.environ.get("RECORDING_SENSEVOICE_DIR") or _asr_home() / "sensevoice").expanduser()
    model = next((directory / name for name in ("model.int8.onnx", "model.onnx") if (directory / name).is_file()), None)
    tokens = directory / "tokens.txt"
    return (model, tokens) if model and tokens.is_file() else None


def _speaker_model_file():
    """The speaker model, if one has been put in place: any .onnx file in .local/asr/speaker/."""
    directory = Path(os.environ.get("RECORDING_SPEAKER_DIR") or _asr_home() / "speaker").expanduser()
    if not directory.is_dir() or importlib.util.find_spec("sherpa_onnx") is None or np is None:
        return None
    return next((path for path in sorted(directory.glob("*.onnx")) if path.is_file()), None)


def engine_catalogue() -> list:
    """Every local engine this build knows, with whether it can run right now."""
    from .asr import _cached_model_path, asr_status
    whisper = asr_status()
    rows = []
    files = _sensevoice_files()
    has_sherpa = importlib.util.find_spec("sherpa_onnx") is not None
    rows.append({
        "id": SENSEVOICE_ID, "engine": "sensevoice", "label": "SenseVoice Small（阿里 FunASR）",
        "note": "速度最快，中文准确率高，自带标点。", "available": bool(files and has_sherpa and np is not None),
        "missing": None if files and has_sherpa else "缺少模型文件（model.int8.onnx、tokens.txt）" if not files else "未安装 sherpa-onnx"})
    for name, label, note in (("small", "Whisper small", "通用模型，速度较慢。"), ("base", "Whisper base", "较快，准确率较低。"),
                              ("turbo", "Whisper large-v3-turbo", "准确率高，CPU 上很慢。")):
        cached = bool(whisper["installed"] and _cached_model_path(name))
        rows.append({"id": name, "engine": "whisper", "label": label, "note": note, "available": cached,
                     "missing": None if cached else "未安装 faster-whisper" if not whisper["installed"] else "模型未缓存"})
    return rows


def choose_model(preferred: str | None) -> str | None:
    """The requested engine if it can run, otherwise the best available fallback."""
    rows = engine_catalogue()
    usable = [row["id"] for row in rows if row["available"]]
    if preferred in usable:
        return preferred
    for candidate in (SENSEVOICE_ID, "small", "base", "turbo"):
        if candidate in usable:
            return candidate
    return None


def _engine_spec(model_id: str) -> dict:
    from .asr import _cached_model_path
    # Once someone has talked for 4 s, a 0.2 s breath ends the sentence, so text settles clause by
    # clause instead of every 15 s. Measured on continuous speech: settling at each pause, and no
    # more cuts inside a word.
    pauses = {"soft_limit": 4.0, "soft_silence": 0.2}
    if model_id == SENSEVOICE_ID:
        model, tokens = _sensevoice_files()
        # SenseVoice is fast enough to re-read the sentence in progress every 0.4 s from its first
        # half second: the screen then trails the voice by about a quarter of a second.
        return {"engine": "sensevoice", "model": str(model), "tokens": str(tokens), "draft_gap": 0.4, "draft_min": 0.5, **pauses}
    return {"engine": "whisper", "model_path": str(_cached_model_path(model_id)), "draft_gap": 1.5, **pauses}


class RealtimeASR:
    """At most one live session; a new one replaces the previous one."""

    def __init__(self, *, fake_engine: bool = False, idle_timeout: float = 180.0, ready_timeout: float = 90.0):
        self._fake = fake_engine
        self._idle_timeout = idle_timeout
        self._ready_timeout = ready_timeout
        self._session = None
        self._lock = asyncio.Lock()
        self._watchdog = None

    def status(self, preferred: str | None = None) -> dict:
        engines = [] if self._fake else engine_catalogue()
        default = "fake" if self._fake else choose_model(preferred)
        if self._fake:
            message = "测试引擎"
        elif not default:
            message = "没有可用的本地语音模型。请准备 SenseVoice 模型文件，或安装 faster-whisper 并缓存模型；本应用不会自动下载。"
        else:
            message = "本地语音模型已就绪，音频不会离开这台电脑。"
        speaker_model = None if self._fake else _speaker_model_file()
        return {"ready": bool(default), "engines": engines, "default_model": default, "languages": list(LANGUAGES),
                "message": message,
                "speakers": {"available": bool(self._fake or speaker_model), "model": speaker_model.stem if speaker_model else None},
                "active_session": self._session.id if self._session and not self._session.closed else None}

    def _get(self, session_id: str) -> _Session:
        session = self._session
        if session is None or session.id != session_id:
            raise KeyError(session_id)
        return session

    async def start(self, model: str | None = None, language: str = "zh", speakers: bool = False) -> dict:
        if language not in LANGUAGES:
            raise RealtimeASRError("不支持的识别语言")
        async with self._lock:
            await self._close_current()
            if self._fake:
                model_name, model_arg = "fake", json.dumps({"engine": "fake", **({"speaker_model": "fake"} if speakers else {})})
            else:
                if np is None:
                    raise RealtimeASRError("未安装本地转写依赖。请在项目根目录使用 .venv 执行：python -m pip install -r backend/requirements-asr.txt")
                model_name = choose_model(model)
                if model_name is None:
                    raise RealtimeASRError("没有可用的本地语音模型。请准备 SenseVoice 模型文件，或安装 faster-whisper 并缓存模型；本应用不会自动下载。")
                spec = _engine_spec(model_name)
                speaker_model = _speaker_model_file() if speakers else None
                if speaker_model:
                    spec["speaker_model"] = str(speaker_model)
                speakers = bool(speaker_model)
                model_arg = json.dumps(spec)
            env = dict(os.environ)
            env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", PYTHONUNBUFFERED="1")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(Path(__file__).resolve()), "--worker", model_arg, language,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=env, start_new_session=True)
            session = _Session(uuid.uuid4().hex, model_name, language, process, speakers)
            session.reader = asyncio.create_task(self._read(session))
            self._session = session
            try:
                await asyncio.wait_for(session.ready.wait(), self._ready_timeout)
            except asyncio.TimeoutError:
                await self._terminate(session)
                raise RealtimeASRError("本地语音模型加载超时，请稍后重试。") from None
            if session.error:
                message = session.error
                await self._terminate(session)
                raise RealtimeASRError(message)
            if self._watchdog is None or self._watchdog.done():
                self._watchdog = asyncio.create_task(self._watch())
            return session.snapshot()

    async def _read(self, session: _Session):
        try:
            while True:
                line = await session.process.stdout.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "ready":
                    session.ready.set()
                elif kind == "segment" and isinstance(event.get("text"), str):
                    voice = event.get("voice")
                    if isinstance(voice, list) and 0 < len(voice) <= 2048:
                        session.voices[len(session.segments)] = voice
                    session.segments.append({"index": len(session.segments), "start": float(event.get("start", 0)),
                                             "end": float(event.get("end", 0)), "text": event["text"][:4000]})
                    session.partial = None
                elif kind == "partial":
                    text = str(event.get("text") or "")[:4000]
                    session.partial = {"start": float(event.get("start", 0)), "text": text} if text else None
                elif kind == "status":
                    session.heard = float(event.get("received", session.heard))
                    session.processed = max(session.processed, float(event.get("processed", session.processed)))
                elif kind == "flushed":
                    session.processed = max(session.processed, session.received)
                    session.flushed.set()
                elif kind == "error":
                    session.error = str(event.get("message") or "本地转写失败")[:600]
        except Exception:
            session.error = session.error or "本地转写进程的输出无法读取。"
        finally:
            if not session.closed and not session.error and session.process.returncode not in (0, None):
                session.error = "本地转写进程意外退出。"
            session.closed = True
            session.partial = None
            session.ready.set()
            session.flushed.set()

    async def push(self, session_id: str, pcm: bytes, cursor: int = 0, voices: bool = False) -> dict:
        session = self._get(session_id)
        if session.closed:
            raise RealtimeASRError(session.error or "实时转写已结束，请重新开始。")
        if len(pcm) % 2 or len(pcm) > MAX_CHUNK_BYTES:
            raise RealtimeASRError("音频数据格式无效（需要 16 kHz 单声道 16 位 PCM）")
        if pcm:
            async with session.write_lock:
                try:
                    session.process.stdin.write(len(pcm).to_bytes(4, "little") + pcm)
                    await asyncio.wait_for(session.process.stdin.drain(), 10)
                except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
                    raise RealtimeASRError(session.error or "本地转写进程已停止，请重新开始。") from None
            session.received += len(pcm) / 2 / SAMPLE_RATE
        session.last_activity = time.monotonic()
        return session.snapshot(cursor, voices)

    def snapshot(self, session_id: str, cursor: int = 0, voices: bool = False) -> dict:
        session = self._get(session_id)
        session.last_activity = time.monotonic()
        return session.snapshot(cursor, voices)

    async def finish(self, session_id: str, cursor: int = 0, voices: bool = False) -> dict:
        session = self._get(session_id)
        if not session.closed:
            async with session.write_lock:
                with suppress(BrokenPipeError, ConnectionResetError):
                    session.process.stdin.write((0).to_bytes(4, "little"))
                    await session.process.stdin.drain()
            # Remaining speech still has to be transcribed; allow for a slow CPU.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(session.flushed.wait(), 60 + 2 * max(0.0, session.received - session.processed))
        await self._terminate(session)
        result = session.snapshot(cursor, voices)
        result["text"] = "\n".join(segment["text"] for segment in session.segments)
        return result

    async def _terminate(self, session: _Session):
        session.closed = True
        process = session.process
        with suppress(Exception):
            process.stdin.close()
        with suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(process.stdin.wait_closed(), 2)
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(Exception):
                    await process.wait()
        if session.reader:
            with suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(session.reader, 2)
        # Release the pipes while the event loop is still running; otherwise the
        # transport is finalised by the garbage collector after the loop closed.
        transport = getattr(process, "_transport", None)
        if transport is not None:
            with suppress(Exception):
                transport.close()
        await asyncio.sleep(0)

    async def _close_current(self):
        if self._session is not None and not self._session.closed:
            await self._terminate(self._session)

    async def _watch(self):
        while True:
            await asyncio.sleep(min(15.0, max(0.05, self._idle_timeout / 4)))
            session = self._session
            if session is None or session.closed:
                return
            if time.monotonic() - session.last_activity > self._idle_timeout:
                session.error = session.error or "长时间没有收到音频，实时转写已自动结束。"
                await self._terminate(session)
                return

    async def close(self):
        async with self._lock:
            await self._close_current()
        if self._watchdog and not self._watchdog.done():
            self._watchdog.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._watchdog


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv))
