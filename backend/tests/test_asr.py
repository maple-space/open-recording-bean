import asyncio
import importlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
import wave
from unittest.mock import patch


class ASRTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertTrue((Path(__file__).parents[1] / "asr.py").exists(), "ASR implementation is missing")
        self.a = importlib.import_module("backend.asr")

    def test_missing_dependency_has_install_instruction(self):
        empty = tempfile.mkdtemp()
        with patch.object(self.a.importlib.util, "find_spec", return_value=None), patch.dict("os.environ", {"RECORDING_FUNASR_NANO_DIR": empty}):
            status = self.a.asr_status()
            self.assertFalse(status["installed"])
            self.assertFalse(status["available"])
            self.assertIn("requirements-asr.txt", status["message"])

    # ---- Fun-ASR-Nano (official GGUF runtime) ---------------------------------------------- #
    def nano_directory(self, script=None):
        """A directory laid out like the real one, with a stand-in for the official command."""
        root = Path(tempfile.mkdtemp())
        for name in ("funasr-encoder-f16.gguf", "qwen3-0.6b-q4km.gguf"):
            (root / name).write_bytes(b"gguf")
        binary = root / "llama-funasr-cli"
        binary.write_text(script or "#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        return root

    def test_nano_is_offered_first_only_when_every_file_is_present(self):
        root = self.nano_directory()
        with patch.dict("os.environ", {"RECORDING_FUNASR_NANO_DIR": str(root)}):
            status = self.a.asr_status()
            self.assertTrue(status["nano"] and status["available"])
            self.assertEqual((status["models"][0], status["default_model"]), ("funasr-nano", "funasr-nano"))
            self.assertTrue(self.a._nano_files()["llm"].endswith("qwen3-0.6b-q4km.gguf"))
            (root / "llama-funasr-cli").chmod(0o644)                      # present but not runnable
            self.assertIsNone(self.a._nano_files())
            self.assertNotIn("funasr-nano", self.a.asr_status()["models"])

    # ---- SenseVoice Small (the model realtime text uses) ------------------------------------ #
    def sensevoice_directory(self):
        root = Path(tempfile.mkdtemp())
        (root / "sensevoice").mkdir()
        for name in ("model.int8.onnx", "tokens.txt"):
            (root / "sensevoice" / name).write_bytes(b"model")
        return root

    def test_sensevoice_is_offered_first_when_its_files_and_runtime_are_present(self):
        root = self.sensevoice_directory()
        with patch.dict("os.environ", {"RECORDING_ASR_DIR": str(root)}), patch.object(self.a, "_sensevoice_runs", return_value=True):
            status = self.a.asr_status()
            self.assertTrue(status["sensevoice"] and status["available"])
            self.assertEqual((status["models"][0], status["default_model"]), ("sensevoice-small", "sensevoice-small"))
            self.assertEqual(status["names"]["sensevoice-small"]["name"], "SenseVoiceSmall")
            (root / "sensevoice" / "tokens.txt").rename(root / "sensevoice" / "tokens.bak")
            self.assertNotIn("sensevoice-small", self.a.asr_status()["models"])
        with patch.dict("os.environ", {"RECORDING_ASR_DIR": str(self.sensevoice_directory())}), patch.object(self.a, "_sensevoice_runs", return_value=False):
            self.assertNotIn("sensevoice-small", self.a.asr_status()["models"])      # the files alone are not enough
        self.assertEqual(self.a.describe_model("")["id"], "sensevoice-small")        # nothing chosen: the default model
        self.assertEqual(self.a.describe_model(" SenseVoice ")["id"], "sensevoice-small")

    async def test_sensevoice_says_how_to_get_the_model_when_it_is_missing(self):
        source = Path(tempfile.mkdtemp()) / "a.wav"
        source.write_bytes(b"RIFF")
        with patch.dict("os.environ", {"RECORDING_ASR_DIR": tempfile.mkdtemp()}):
            with self.assertRaises(self.a.ASRError) as caught:
                await self.a.transcribe(source, "sensevoice-small")
        self.assertIn("./start.sh models", str(caught.exception))

    def test_sensevoice_reads_a_recording_piece_by_piece(self):
        try:
            import numpy as np
            import av  # noqa: F401
        except ImportError:
            self.skipTest("numpy and PyAV are needed to decode the sample")
        source = Path(tempfile.mkdtemp()) / "talk.wav"
        t = np.arange(16000 * 40) / 16000
        pcm = (np.sin(2 * np.pi * 300 * t) * 9000 * (t % 20 < 12)).astype("<i2")     # two bursts of twelve seconds
        with wave.open(str(source), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(pcm.tobytes())
        heard = []

        class Stream:
            def accept_waveform(self, rate, samples):
                heard.append((rate, len(samples), str(samples.dtype)))
                self.result = types.SimpleNamespace(text=f" 第{len(heard)}段。 ")

        class Recognizer:
            @staticmethod
            def from_sense_voice(**options):
                Recognizer.options = options
                return Recognizer()

            def create_stream(self):
                return Stream()

            def decode_stream(self, stream):
                pass

        events = []
        with patch.dict(sys.modules, {"sherpa_onnx": types.SimpleNamespace(OfflineRecognizer=Recognizer)}):
            result = self.a._transcribe_sensevoice_in_worker(source, {"model": "m.onnx", "tokens": "t.txt"}, events.append)
        self.assertEqual(Recognizer.options["model"], "m.onnx")
        self.assertEqual(Recognizer.options["language"], "auto")
        self.assertEqual(result["text"], "第1段。\n第2段。")
        self.assertEqual(len(result["segments"]), 2)
        self.assertTrue(all(rate == 16000 and kind == "float32" for rate, _, kind in heard))
        self.assertTrue(all(count <= 16000 * 30 for _, count, _ in heard))           # never more than the model reads at once
        self.assertAlmostEqual(result["duration"], 40.0, places=1)
        self.assertEqual(events[-1]["type"], "progress")

    def test_models_are_called_by_their_full_names_the_repository_names(self):
        root = self.nano_directory()
        with patch.dict("os.environ", {"RECORDING_FUNASR_NANO_DIR": str(root)}):
            self.assertEqual(self.a.describe_model("funasr-nano"), {"id": "funasr-nano", "name": "Fun-ASR-Nano-GGUF"})
            self.assertEqual(self.a.describe_model(" Nano "), {"id": "funasr-nano", "name": "Fun-ASR-Nano-GGUF"})
            self.assertEqual(self.a.asr_status()["names"]["funasr-nano"], {"id": "funasr-nano", "name": "Fun-ASR-Nano-GGUF"})
        self.assertEqual(self.a.describe_model("small")["name"], "faster-whisper-small")
        self.assertEqual(self.a.describe_model("turbo")["name"], "faster-whisper-large-v3-turbo")
        self.assertEqual(self.a.describe_model("large")["name"], "faster-whisper-large-v3")
        self.assertEqual(self.a.describe_model("Systran/faster-whisper-medium")["name"], "faster-whisper-medium")
        self.assertEqual(self.a.describe_model(str(root))["name"], str(root))                # a model directory keeps what was typed
        self.assertEqual(self.a.describe_model("~/models/whisper-ct2")["name"], "~/models/whisper-ct2")

    def test_the_settings_page_and_the_service_use_the_same_full_names(self):
        source = (Path(__file__).parents[2] / "src" / "platform" / "api.js").read_text(encoding="utf-8")
        self.assertIn("return '" + self.a.describe_model("funasr-nano")["name"] + "'", source)
        self.assertIn("return '" + self.a.describe_model("sensevoice-small")["name"] + "'", source)
        self.assertIn("`faster-whisper-${id}`", source)                                   # the page's rule for regular Whisper models
        for model in ("tiny", "base", "small", "medium", "large-v3"):
            self.assertIn(f"'{model}'", source)
            self.assertEqual(self.a.describe_model(model)["name"], f"faster-whisper-{model}")

    async def test_nano_without_its_files_says_what_is_needed_and_starts_nothing(self):
        source = Path(tempfile.mkdtemp()) / "audio.wav"
        source.write_bytes(b"audio")
        with patch.dict("os.environ", {"RECORDING_FUNASR_NANO_DIR": tempfile.mkdtemp()}), patch.object(self.a, "_run_jsonl") as run:
            with self.assertRaisesRegex(self.a.ASRError, "llama-funasr-cli.*qwen3-0.6b-q4km.gguf"):
                await self.a.transcribe(source, "funasr-nano")
            run.assert_not_called()

    async def test_nano_transcription_runs_its_own_worker(self):
        source = Path(tempfile.mkdtemp()) / "audio.ogg"
        source.write_bytes(b"audio")
        result = {"text": "你好。", "segments": [{"start": 0.2, "end": 1.4, "text": "你好。"}], "language": "", "duration": 2.}
        calls, progress = [], []
        async def fake_run(argv, **kwargs):
            calls.append(argv)
            await kwargs["on_event"]({"type": "progress", "progress": .5, "message": "正在转写 1/2"})
            await kwargs["on_event"]({"type": "result", "result": result})
        root = self.nano_directory()
        with patch.dict("os.environ", {"RECORDING_FUNASR_NANO_DIR": str(root)}), patch.object(self.a, "_run_jsonl", side_effect=fake_run):
            self.assertEqual(await self.a.transcribe(source, "Fun-ASR-Nano", progress=progress.append), result)
        self.assertEqual(calls[0][3:5], ["--nano-worker", str(source.resolve())])
        self.assertEqual(self.a.json.loads(calls[0][5])["binary"], str(root / "llama-funasr-cli"))
        self.assertEqual(progress[0]["message"], "正在准备 Fun-ASR-Nano")

    @staticmethod
    def speech(seconds, level=0.2):
        import numpy as np
        t = np.arange(int(16000 * seconds)) / 16000
        return (np.sin(2 * np.pi * 220 * t) * level * 32767).astype(np.int16)

    @staticmethod
    def hush(seconds, seed=1):
        import numpy as np
        return (np.random.default_rng(seed).standard_normal(int(16000 * seconds)) * 0.002 * 32767).astype(np.int16)

    def test_long_recording_is_split_at_pauses_into_pieces_the_model_can_take(self):
        import numpy as np
        # 12 s, a 0.5 s breath, 12 s, a long silence, then 40 s without any pause except a dip at 22 s.
        steady = self.speech(40).astype(np.float32)
        steady[int(21.9 * 16000):int(22.1 * 16000)] *= 0.05
        audio = np.concatenate([self.hush(1), self.speech(12), self.hush(.5, 2), self.speech(12), self.hush(6, 3), steady.astype(np.int16), self.hush(2, 4)])
        pieces = [(round(a / 16000, 1), round(b / 16000, 1)) for a, b in self.a._plan_pieces(audio)]
        self.assertTrue(all(end - start <= 28.5 for start, end in pieces), pieces)
        # Two 12 s stretches do not fit one 18 s piece: cut in the breath, not in a word.
        self.assertEqual(pieces[0], (0.8, 13.2))
        self.assertEqual(pieces[1], (13.3, 25.7))
        # Nobody paused for 40 s: the cut lands on the quietest moment, and neither side repeats audio.
        self.assertEqual(len(pieces), 4)
        self.assertAlmostEqual(pieces[2][1], 31.5 + 22.0, delta=0.2)
        self.assertEqual(pieces[2][1], pieces[3][0])
        self.assertEqual(self.a._plan_pieces(self.hush(5)), [])           # silence is never sent to the model

    def test_nano_worker_recognises_each_piece_whole_and_keeps_its_time(self):
        import numpy as np
        # Stand-in for the official command: says how long its piece was, and is noisy on stderr like the real one.
        script = (f"#!{sys.executable}\nimport sys, wave\n"
                  "args = sys.argv[1:]\nw = wave.open(args[args.index('-a') + 1])\n"
                  "sys.stderr.write('load_tensors: ...\\n' * 2000)\n"
                  "print('  这一段  %.1f 秒，上限 %s。 ' % (w.getnframes() / w.getframerate(), args[args.index('-n') + 1]))\n")
        root = self.nano_directory(script)
        folder = Path(tempfile.mkdtemp())
        source = folder / "meeting.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(np.concatenate([self.hush(1), self.speech(3), self.hush(4, 2), self.speech(2), self.hush(1, 3)]).tobytes())
        events = []
        with patch.dict("os.environ", {"RECORDING_FUNASR_NANO_DIR": str(root)}):
            result = self.a._transcribe_nano_in_worker(source, self.a._nano_files(), events.append)
        self.assertEqual([(round(item["start"], 1), round(item["end"], 1)) for item in result["segments"]], [(0.8, 4.2), (7.8, 10.2)])
        self.assertEqual(result["text"], "这一段 3.4 秒，上限 72。\n这一段 2.4 秒，上限 60。")     # whitespace tidied, one line per piece
        self.assertEqual(result["duration"], 11.0)
        self.assertEqual([event["message"] for event in events], ["正在转写 1/2", "正在转写 2/2"])
        self.assertEqual(self.a._validate_result(result)["language"], "")

    def test_nano_failure_is_reported_not_swallowed(self):
        root = self.nano_directory("#!/bin/sh\nexit 3\n")
        folder = Path(tempfile.mkdtemp())
        piece = folder / "piece.wav"
        with wave.open(str(piece), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16000)
        with patch.dict("os.environ", {"RECORDING_FUNASR_NANO_DIR": str(root)}):
            with self.assertRaisesRegex(RuntimeError, "exited with 3"):
                self.a._nano_text(self.a._nano_files(), piece, 1.0)

    @unittest.skipUnless(importlib.util.find_spec("faster_whisper"), "optional ASR dependencies are not installed")
    def test_installed_decoder_reads_wav_without_model(self):
        # Exercise the real faster-whisper/PyAV boundary, without a model or network.
        from faster_whisper.audio import decode_audio
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 1600)
        audio.seek(0)
        samples = decode_audio(audio, sampling_rate=16000)
        self.assertEqual(samples.shape, (1600,))
        self.assertEqual(str(samples.dtype), "float32")

    def test_cached_model_requires_complete_files(self):
        root = Path(tempfile.mkdtemp())
        snapshot = root / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
        snapshot.mkdir(parents=True)
        with patch.dict("os.environ", {"HF_HUB_CACHE": str(root)}):
            self.assertIsNone(self.a._cached_model_path("small"))
            for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
                (snapshot / name).write_text("data")
            self.assertEqual(self.a._cached_model_path("small"), snapshot)

    async def test_missing_model_does_not_start_worker_or_download(self):
        source = Path(tempfile.mkdtemp()) / "audio.wav"
        source.write_bytes(b"audio")
        with patch.object(self.a, "_cached_model_path", return_value=None), patch.object(self.a.importlib.util, "find_spec", return_value=object()), patch.object(self.a, "_run_jsonl") as run:
            with self.assertRaisesRegex(self.a.ASRError, "缓存|下载"):
                await self.a.transcribe(source, "small")
            run.assert_not_called()

    async def test_transcribe_worker_result_and_progress(self):
        source = Path(tempfile.mkdtemp()) / "audio.wav"
        source.write_bytes(b"audio")
        result = {"text": "你好", "segments": [{"start": 0., "end": 1., "text": "你好"}], "language": "zh", "duration": 1.}
        calls, progress = [], []
        async def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            await kwargs["on_event"]({"type": "progress", "progress": .8, "message": "转写中"})
            await kwargs["on_event"]({"type": "result", "result": result})
        with patch.object(self.a, "_cached_model_path", return_value=source.parent), patch.object(self.a.importlib.util, "find_spec", return_value=object()), patch.object(self.a, "_run_jsonl", side_effect=fake_run):
            self.assertEqual(await self.a.transcribe(source, progress=progress.append), result)
        self.assertEqual(calls[0][0][0], sys.executable)
        self.assertIn("--worker", calls[0][0])
        self.assertTrue(progress)

    def test_worker_uses_local_files_and_preserves_segments(self):
        calls = []
        class FakeModel:
            def __init__(self, model, **kwargs):
                calls.append(kwargs)
            def transcribe(self, path, **kwargs):
                return iter([types.SimpleNamespace(start=.5, end=1.5, text=" 你好 ")]), types.SimpleNamespace(language="zh", duration=2.)
        events = []
        with patch.dict(sys.modules, {"faster_whisper": types.SimpleNamespace(WhisperModel=FakeModel)}):
            result = self.a._transcribe_in_worker(Path("/audio.wav"), Path("/model"), emit=events.append)
        self.assertTrue(calls[0]["local_files_only"])
        self.assertEqual(result["segments"], [{"start": .5, "end": 1.5, "text": "你好"}])
        self.assertEqual(result["text"], "你好")
        self.assertEqual(result["duration"], 2.)

    async def test_cancel_worker_is_not_swallowed(self):
        source = Path(tempfile.mkdtemp()) / "audio.wav"
        source.write_bytes(b"audio")
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()
        with patch.object(self.a, "_cached_model_path", return_value=source.parent), patch.object(self.a.importlib.util, "find_spec", return_value=object()), patch.object(self.a, "_run_jsonl", side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await self.a.transcribe(source)


if __name__ == "__main__":
    unittest.main()
