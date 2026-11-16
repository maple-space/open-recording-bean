"""Optional local ASR in a cancellable, offline worker process: Fun-ASR-Nano (GGUF) or faster-whisper."""
from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import sys
from typing import Callable

if __name__ != "__main__":
    from .providers import ProviderError, _notify, _run_jsonl, _timeout_from_environment


class ASRError(RuntimeError):
    pass


PROJECT = Path(__file__).resolve().parents[1]
SENSEVOICE_ID = "sensevoice-small"
SENSEVOICE_NAME = "SenseVoiceSmall"     # the model's own name, which is also the name of its repository
SENSEVOICE_MISSING = ("未找到 SenseVoice Small 的本地文件（model.int8.onnx 和 tokens.txt）。在项目文件夹运行 ./start.sh models "
                      "会下载到 .local/asr/sensevoice/。也可以在“模型与设置”里改用其他本机模型。")
NANO_ID = "funasr-nano"
NANO_MISSING = ("未找到 Fun-ASR-Nano 的本地文件。需要官方运行程序 llama-funasr-cli，以及 funasr-encoder-f16.gguf 和 "
                "qwen3-0.6b-q4km.gguf，放在 .local/asr/funasr-nano-gguf/（或用 RECORDING_FUNASR_NANO_DIR 指定目录）。"
                "也可以在“模型与设置”里改用已缓存的 Whisper 模型。本应用不会自动下载。")


def _is_nano(model: str) -> bool:
    return str(model).strip().lower() in {NANO_ID, "fun-asr-nano", "nano"}


def _is_sensevoice(model: str) -> bool:
    return str(model).strip().lower() in {SENSEVOICE_ID, "sensevoice", "sense-voice"}


def _asr_home() -> Path:
    return Path(os.environ.get("RECORDING_ASR_DIR") or PROJECT / ".local" / "asr").expanduser()


def _sensevoice_files() -> dict | None:
    """The model realtime text uses (./start.sh models puts it in place); it also reads whole recordings."""
    directory = Path(os.environ.get("RECORDING_SENSEVOICE_DIR") or _asr_home() / "sensevoice").expanduser()
    model = next((directory / name for name in ("model.int8.onnx", "model.onnx") if (directory / name).is_file()), None)
    tokens = directory / "tokens.txt"
    if model is None or not tokens.is_file():
        return None
    return {"model": str(model), "tokens": str(tokens)}


def _sensevoice_runs() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("sherpa_onnx", "av", "numpy"))


def _nano_files() -> dict | None:
    """The official llama.cpp runtime and the GGUF pair it needs, if all are present."""
    directory = Path(os.environ.get("RECORDING_FUNASR_NANO_DIR") or _asr_home() / "funasr-nano-gguf").expanduser()
    binary = next((directory / name for name in ("llama-funasr-cli", "llama-funasr-cli.exe")
                   if (directory / name).is_file() and os.access(directory / name, os.X_OK)), None)
    encoder = directory / "funasr-encoder-f16.gguf"
    llm = next((directory / name for name in ("qwen3-0.6b-q4km.gguf", "qwen3-0.6b-q5km.gguf", "qwen3-0.6b-q8_0.gguf")
                if (directory / name).is_file()), None)
    if binary is None or llm is None or not encoder.is_file():
        return None
    return {"binary": str(binary), "encoder": str(encoder), "llm": str(llm)}


def _cache_root() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    if os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return Path(os.environ["HUGGINGFACE_HUB_CACHE"]).expanduser()
    hf_home = Path(os.environ.get("HF_HOME") or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "huggingface")
    return hf_home.expanduser() / "hub"


def _model_repository(model: str) -> str:
    if "/" in model:
        return model
    special = {
        "large": "Systran/faster-whisper-large-v3", "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    }
    return special.get(model, f"Systran/faster-whisper-{model}")


def describe_model(model: str) -> dict:
    """The full name of a local speech model: the name of the model repository it comes from,
    for example Fun-ASR-Nano-GGUF or faster-whisper-small. A model directory keeps what was typed."""
    text = str(model or "").strip()
    if not text or _is_sensevoice(text):
        return {"id": SENSEVOICE_ID, "name": SENSEVOICE_NAME}
    if _is_nano(text):
        return {"id": NANO_ID, "name": "Fun-ASR-Nano-GGUF"}
    if text.startswith(("/", "~", ".")) or "\\" in text or Path(text).expanduser().is_dir():
        return {"id": text, "name": text}
    return {"id": text, "name": _model_repository(text).rsplit("/", 1)[-1]}


def _complete_model(directory: Path) -> bool:
    return all((directory / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json")) and (
        (directory / "vocabulary.txt").is_file() or (directory / "vocabulary.json").is_file())


def _cached_model_path(model: str) -> Path | None:
    explicit = Path(model).expanduser()
    if explicit.is_dir():
        return explicit.resolve() if _complete_model(explicit) else None
    repo = _model_repository(model)
    repo_dir = _cache_root() / ("models--" + repo.replace("/", "--"))
    revision = repo_dir / "refs/main"
    candidates = []
    if revision.is_file():
        try:
            commit = revision.read_text(encoding="utf-8").strip()
            if commit and all(char in "0123456789abcdef" for char in commit):
                candidates.append(repo_dir / "snapshots" / commit)
        except OSError:
            pass
    snapshots = repo_dir / "snapshots"
    if snapshots.is_dir():
        candidates.extend(sorted(snapshots.iterdir(), key=lambda path: path.name, reverse=True))
    return next((path for path in candidates if _complete_model(path)), None)


def asr_status() -> dict:
    installed = importlib.util.find_spec("faster_whisper") is not None
    models = []
    cache = _cache_root()
    if cache.is_dir():
        for repo in sorted(cache.glob("models--*")):
            name = repo.name.removeprefix("models--").replace("--", "/")
            if "whisper" not in name.lower():
                continue
            if _cached_model_path(name):
                prefix = "Systran/faster-whisper-"
                models.append(name.removeprefix(prefix) if name.startswith(prefix) else name)
    nano = _nano_files() is not None
    sensevoice = _sensevoice_files() is not None and _sensevoice_runs()
    if nano:
        models.insert(0, NANO_ID)
    if sensevoice:
        models.insert(0, SENSEVOICE_ID)
    if sensevoice:
        message = "本地转写可用：SenseVoice Small。模型只从本机文件加载，不自动下载。"
    elif nano:
        message = "本地转写可用：Fun-ASR-Nano。模型只从本机文件加载，不自动下载。"
    elif installed:
        message = "本地转写可用，模型仅从已有缓存加载。"
    else:
        message = ("没有可用的本地语音模型。在项目文件夹运行 ./start.sh models 下载 SenseVoice Small；"
                   "想用 Whisper 模型，请在项目根目录使用 .venv 执行：python -m pip install -r backend/requirements-asr.txt")
    # "installed" keeps meaning faster-whisper; "available" is what the settings page shows.
    return {"installed": installed, "nano": nano, "sensevoice": sensevoice, "available": sensevoice or nano or installed,
            "models": models, "names": {model: describe_model(model) for model in models},
            "default_model": SENSEVOICE_ID if sensevoice else NANO_ID if nano else "small", "message": message,
            "downloads_automatically": False}


def _validate_result(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("text"), str) or not isinstance(value.get("segments"), list):
        raise ASRError("本地转写返回了无效结果。")
    try:
        duration = float(value["duration"])
        if not math.isfinite(duration) or duration < 0:
            raise ValueError()
        segments = []
        for segment in value["segments"]:
            start, end = float(segment["start"]), float(segment["end"])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start or not isinstance(segment["text"], str):
                raise ValueError()
            segments.append({"start": start, "end": end, "text": segment["text"]})
        language = value["language"]
        if not isinstance(language, str):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as error:
        raise ASRError("本地转写返回了无效时间轴。") from error
    if not value["text"].strip():
        raise ASRError("没有识别到语音，请检查录音是否有声音。")
    return {"text": value["text"].strip(), "segments": segments, "language": language, "duration": duration}


async def transcribe(path: Path, model: str = "small", progress: Callable | None = None) -> dict:
    path = Path(path).resolve()
    if not path.is_file():
        raise ASRError("录音文件不存在，请重新导入。")
    if _is_sensevoice(model):
        files = _sensevoice_files()
        if files is None:
            raise ASRError(SENSEVOICE_MISSING)
        if not _sensevoice_runs():
            raise ASRError("未安装 SenseVoice 的运行组件。请在项目文件夹运行 ./start.sh，会自动安装。")
        return await _run_worker(["--sensevoice-worker", str(path), json.dumps(files)], path, progress, "正在准备 SenseVoice Small")
    if _is_nano(model):
        files = _nano_files()
        if files is None:
            raise ASRError(NANO_MISSING)
        if importlib.util.find_spec("av") is None or importlib.util.find_spec("numpy") is None:
            raise ASRError("未安装音频解码依赖。请在项目根目录使用 .venv 执行：python -m pip install -r backend/requirements-asr.txt")
        return await _run_worker(["--nano-worker", str(path), json.dumps(files)], path, progress, "正在准备 Fun-ASR-Nano")
    if importlib.util.find_spec("faster_whisper") is None:
        raise ASRError("未安装 faster-whisper。请在项目根目录使用 .venv 执行：python -m pip install -r backend/requirements-asr.txt")
    model_path = _cached_model_path(model)
    if model_path is None:
        repo = _model_repository(model)
        raise ASRError(f"未找到完整的本地模型缓存（{model}）。请先手动下载 {repo}，或将 ASR 模型设置为已下载的 CTranslate2 模型目录；本应用不会自动下载。")
    return await _run_worker(["--worker", str(path), str(model_path)], path, progress, "正在加载本地语音模型")


async def _run_worker(arguments: list, path: Path, progress: Callable | None, first_message: str) -> dict:
    result = None
    worker_error = None

    async def receive(event):
        nonlocal result, worker_error
        if event.get("type") == "result":
            result = event.get("result")
        elif event.get("type") == "error":
            worker_error = str(event.get("message") or "本地转写失败")[:600]
        elif event.get("type") == "progress":
            await _notify(progress, {**event, "stage": "transcribing"})

    env = dict(os.environ)
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", PYTHONUNBUFFERED="1")
    await _notify(progress, {"stage": "transcribing", "message": first_message, "progress": 0.0})
    try:
        await _run_jsonl([sys.executable, "-I", str(Path(__file__).resolve()), *arguments],
                         stdin="", cwd=path.parent, env=env, on_event=receive,
                         timeout=_timeout_from_environment("RECORDING_ASR_TIMEOUT", 7200), max_output_bytes=32 * 1024 * 1024)
    except ProviderError as error:
        raise ASRError(worker_error or str(error)) from error
    if worker_error:
        raise ASRError(worker_error)
    return _validate_result(result)


def _transcribe_in_worker(path: Path, model_path: Path, emit: Callable) -> dict:
    from faster_whisper import WhisperModel

    engine = WhisperModel(str(model_path), device="cpu", compute_type="int8", local_files_only=True,
                          cpu_threads=min(4, os.cpu_count() or 1))
    iterator, info = engine.transcribe(str(path), beam_size=1, vad_filter=True,
                                      vad_parameters={"min_silence_duration_ms": 500})
    duration = float(info.duration)
    segments = []
    for segment in iterator:
        text = segment.text.strip()
        if text:
            segments.append({"start": float(segment.start), "end": float(segment.end), "text": text})
        emit({"type": "progress", "message": "正在转写", "progress": min(.99, float(segment.end) / duration) if duration > 0 else 0.0})
    return {"text": "\n".join(segment["text"] for segment in segments), "segments": segments,
            "language": info.language, "duration": duration}


# ---- Fun-ASR-Nano through the official llama.cpp runtime ------------------------------------- #
# The runtime is a one-shot command: one audio window in, its text out, about a second of model
# loading each time. Its own long-audio mode cuts every 30 s wherever that falls, which garbles a
# word at each cut, so the recording is split here at pauses and each piece is recognised whole.

NANO_MAX_SECONDS = 28.0        # the model's window is 30 s
NANO_TARGET_SECONDS = 18.0     # prefer pieces about this long: fewer loads, still cut at a pause
FRAME = 320                    # 20 ms at 16 kHz


def _decode_mono_16k(path: Path):
    """Any container PyAV reads (Ogg/Opus from the recorders, WAV, MP3, M4A...) as int16 samples."""
    import av
    import numpy as np

    parts = []
    with av.open(str(path)) as container:
        stream = next((item for item in container.streams if item.type == "audio"), None)
        if stream is None:
            raise ValueError("no audio stream")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().reshape(-1).copy())
        for out in resampler.resample(None):
            parts.append(out.to_ndarray().reshape(-1).copy())
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)


def _plan_pieces(samples, *, max_seconds: float = NANO_MAX_SECONDS, target_seconds: float = NANO_TARGET_SECONDS) -> list:
    """(start_sample, end_sample) pieces that contain speech, each at most *max_seconds*, cut at pauses."""
    import numpy as np

    count = len(samples) // FRAME
    if not count:
        return []
    frames = samples[:count * FRAME].astype(np.float32).reshape(count, FRAME) / 32768.0
    rms = np.sqrt((frames ** 2).mean(axis=1))
    # The quietest tenth of the recording is the room; speech is clearly above it.
    threshold = min(max(0.006, float(np.percentile(rms, 10)) * 3.0), 0.08)
    loud = rms >= threshold
    islands, start = [], None                       # runs of speech, bridging gaps under 0.3 s
    quiet = 0
    for index in range(count):
        if loud[index]:
            if start is None:
                start = index
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= 15:
                islands.append((start, index - quiet + 1))
                start, quiet = None, 0
    if start is not None:
        islands.append((start, count - quiet))
    islands = [(a, b) for a, b in islands if b - a >= 12]   # a click is not speech
    max_frames, target_frames, pad = int(max_seconds * 50), int(target_seconds * 50), 10
    pieces = []                                       # [first frame, end frame, pad before, pad after]
    for a, b in islands:
        forced = False
        while b - a > max_frames:                     # nobody paused: cut at the quietest moment in the allowed span
            lo, hi = a + target_frames, a + max_frames
            cut = lo + int(np.argmin(rms[lo:hi]))
            pieces.append([a, cut, not forced, False])  # no padding across a forced cut: it would repeat a syllable
            a, forced = cut, True
        if not forced and pieces and pieces[-1][3] and a - pieces[-1][1] <= 100 and b - pieces[-1][0] <= target_frames:
            pieces[-1][1] = b                         # close to the previous speech and still short: keep together
        else:
            pieces.append([a, b, not forced, True])
    return [(max(0, a - pad * before) * FRAME, min(count, b + pad * after) * FRAME) for a, b, before, after in pieces]


def _nano_text(files: dict, wav_path: Path, seconds: float) -> str:
    import subprocess

    # A decoder that starts repeating itself would otherwise run to its 512-token limit on every piece.
    tokens = min(512, int(seconds * 12) + 32)
    done = subprocess.run([files["binary"], "--enc", files["encoder"], "-m", files["llm"], "-a", str(wav_path), "-n", str(tokens)],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=max(90.0, 30.0 + 10.0 * seconds))
    if done.returncode != 0:
        raise RuntimeError(f"llama-funasr-cli exited with {done.returncode}")
    return " ".join(done.stdout.decode("utf-8", "replace").split())


def _transcribe_nano_in_worker(path: Path, files: dict, emit: Callable) -> dict:
    import tempfile
    import wave

    samples = _decode_mono_16k(path)
    duration = len(samples) / 16000.0
    pieces = _plan_pieces(samples)
    segments = []
    with tempfile.TemporaryDirectory(prefix="recording-nano-") as scratch:
        wav_path = Path(scratch) / "piece.wav"
        for index, (start, end) in enumerate(pieces):
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(samples[start:end].tobytes())
            text = _nano_text(files, wav_path, (end - start) / 16000.0)
            if text:
                segments.append({"start": start / 16000.0, "end": end / 16000.0, "text": text})
            emit({"type": "progress", "message": f"正在转写 {index + 1}/{len(pieces)}",
                  "progress": min(.99, (end / 16000.0) / duration) if duration > 0 else 0.0})
    return {"text": "\n".join(segment["text"] for segment in segments), "segments": segments,
            "language": "", "duration": duration}


def _transcribe_sensevoice_in_worker(path: Path, files: dict, emit: Callable) -> dict:
    """SenseVoice Small through sherpa-onnx, piece by piece: it reads at most about thirty seconds at once."""
    import numpy as np
    import sherpa_onnx

    samples = _decode_mono_16k(path)
    duration = len(samples) / 16000.0
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=files["model"], tokens=files["tokens"], num_threads=min(4, os.cpu_count() or 1), use_itn=True,
        language="auto", debug=False)
    pieces = _plan_pieces(samples)
    segments = []
    for index, (start, end) in enumerate(pieces):
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, samples[start:end].astype(np.float32) / 32768.0)
        recognizer.decode_stream(stream)
        text = stream.result.text.strip()
        if text:
            segments.append({"start": start / 16000.0, "end": end / 16000.0, "text": text})
        emit({"type": "progress", "message": f"正在转写 {index + 1}/{len(pieces)}",
              "progress": min(.99, (end / 16000.0) / duration) if duration > 0 else 0.0})
    return {"text": "\n".join(segment["text"] for segment in segments), "segments": segments,
            "language": "", "duration": duration}


def _worker_main() -> int:
    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)
    workers = {"--worker": (lambda: _transcribe_in_worker(Path(sys.argv[2]), Path(sys.argv[3]), emit),
                            "音频格式、模型缓存完整性及 faster-whisper 依赖"),
               "--nano-worker": (lambda: _transcribe_nano_in_worker(Path(sys.argv[2]), json.loads(sys.argv[3]), emit),
                                 "音频格式和 Fun-ASR-Nano 的运行程序与模型文件"),
               "--sensevoice-worker": (lambda: _transcribe_sensevoice_in_worker(Path(sys.argv[2]), json.loads(sys.argv[3]), emit),
                                       "音频格式和 SenseVoice Small 的模型文件")}
    if len(sys.argv) != 4 or sys.argv[1] not in workers:
        return 2
    work, hint = workers[sys.argv[1]]
    try:
        emit({"type": "result", "result": work()})
        return 0
    except Exception as error:
        # Avoid dumping native-library logs, paths, or inherited environment.
        emit({"type": "error", "message": f"本地转写失败（{type(error).__name__}）。请检查{hint}。"})
        return 1


if __name__ == "__main__":
    raise SystemExit(_worker_main())
