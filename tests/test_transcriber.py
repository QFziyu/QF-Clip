"""ASR recovery/timeline checks with fake Whisper iterators; no model download."""
import copy
from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from core.config import DEFAULTS
from core.transcriber import DeviceUnavailable, resolve_device, transcribe_audio, validate_transcript


@dataclass
class Word:
    start: float
    end: float
    word: str
    probability: float = 0.9


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list = field(default_factory=list)


class FakeWhisper:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        result = self.results.pop(0)
        # faster-whisper performs work while consuming a lazy iterator. Failures
        # must be tested there rather than only at transcribe() invocation.
        def iterate():
            if isinstance(result, Exception):
                raise result
            yield from result
        return iterate(), SimpleNamespace(language="zh")


class TranscriberTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fcf-asr-")
        self.root = Path(self.temporary.name)
        self.wav = self.root / "中文 音频.wav"
        with wave.open(str(self.wav), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            for _ in range(20):
                output.writeframes(b"\0\0" * 16000)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg["terms"] = ["曼城", "瓜迪奥拉"]
        self.cfg["asr"].update(chunk_seconds=10, overlap_seconds=2)
        self.meta = {"duration": 20, "source": "D:/素材/真实 文件.mp4", "selected_audio": 1,
                     "timeline_origin": 5, "source_signature": {"sample_sha256": "a"}}
        self.first = [Segment(0.5, 10.1, "我觉得曼城", [Word(0.5, 1.5, "我觉得"), Word(9.3, 10.1, "曼城")])]
        self.second = [Segment(1.3, 3.2, "曼城换人", [Word(1.3, 2.1, "曼城"), Word(2.2, 3.2, "换人")])]

    def tearDown(self):
        self.temporary.cleanup()

    def test_overlap_words_have_absolute_times_without_duplicate_speech(self):
        model = FakeWhisper([self.first, self.second])
        with patch("core.transcriber._make_model", return_value=(model, "cpu", "int8")):
            result = transcribe_audio(self.wav, self.meta, self.cfg, self.root)
        text = "".join(s["text"] for s in result["segments"])
        self.assertEqual(text, "我觉得曼城换人")
        self.assertAlmostEqual(result["segments"][1]["words"][0]["start"], 10.2)
        self.assertEqual(result["timeline_origin"], 5)
        self.assertEqual(len(model.calls), 2)
        self.assertTrue(model.calls[0][1]["word_timestamps"])
        self.assertTrue(model.calls[0][1]["vad_filter"])
        self.assertIn("瓜迪奥拉", model.calls[0][1]["initial_prompt"])
        checkpoints = list((self.root / "asr").glob("*/chunk_*.json"))
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(json.loads(checkpoints[0].read_text(encoding="utf-8"))["segments"][0]["text"], "我觉得曼城")

    def test_interrupted_second_chunk_reuses_first_checkpoint_and_final_cache(self):
        broken = FakeWhisper([self.first, RuntimeError("network file interruption")])
        with patch("core.transcriber._make_model", return_value=(broken, "cpu", "int8")):
            with self.assertRaisesRegex(RuntimeError, "2/2"):
                transcribe_audio(self.wav, self.meta, self.cfg, self.root)
        self.assertFalse((self.root / "transcript.json").exists())
        saved = list((self.root / "asr").glob("*/chunk_0000.json"))[0]
        unchanged = saved.read_bytes(), saved.stat().st_mtime_ns
        repaired = FakeWhisper([self.second])
        with patch("core.transcriber._make_model", return_value=(repaired, "cpu", "int8")):
            expected = transcribe_audio(self.wav, self.meta, self.cfg, self.root)
        self.assertEqual(len(repaired.calls), 1)
        self.assertEqual((saved.read_bytes(), saved.stat().st_mtime_ns), unchanged)
        with patch("core.transcriber._make_model", side_effect=AssertionError("cached ASR must not load a model")):
            actual = transcribe_audio(self.wav, self.meta, self.cfg, self.root)
        self.assertEqual(actual, expected)

    def test_source_signature_change_invalidates_same_path_duration_cache(self):
        first = FakeWhisper([self.first, self.second])
        with patch("core.transcriber._make_model", return_value=(first, "cpu", "int8")):
            old = transcribe_audio(self.wav, self.meta, self.cfg, self.root)
        changed = {**self.meta, "source_signature": {"sample_sha256": "b"}}
        second = FakeWhisper([self.first, self.second])
        with patch("core.transcriber._make_model", return_value=(second, "cpu", "int8")):
            new = transcribe_audio(self.wav, changed, self.cfg, self.root)
        self.assertNotEqual(old["cache_key"], new["cache_key"])
        self.assertEqual(len(second.calls), 2)

    def test_cuda_requires_opt_in_to_cpu_and_adapts_compute_precision(self):
        ctr = SimpleNamespace(get_cuda_device_count=lambda: 0,
                              get_supported_compute_types=lambda device: {"int8", "float32"})
        with patch.dict("sys.modules", {"ctranslate2": ctr}), \
                patch("core.transcriber.prepare_cuda_runtime", return_value={"usable": True}):
            with self.assertRaises(DeviceUnavailable):
                resolve_device(self.cfg["asr"], allow_cpu=False)
            self.assertEqual(resolve_device(self.cfg["asr"], allow_cpu=True), ("cpu", "int8"))

    def test_lazy_cuda_failure_restarts_only_current_chunk_on_cpu(self):
        gpu = FakeWhisper([self.first, RuntimeError("CUDA out of memory")])
        cpu = FakeWhisper([self.second])
        factory = unittest.mock.Mock(return_value=cpu)
        with patch("core.transcriber._make_model", return_value=(gpu, "cuda", "int8_float16")), \
                patch.dict("sys.modules", {"faster_whisper": SimpleNamespace(WhisperModel=factory)}):
            result = transcribe_audio(self.wav, self.meta, self.cfg, self.root, allow_cpu=True)
        self.assertEqual(len(gpu.calls), 2)
        self.assertEqual(len(cpu.calls), 1)
        self.assertEqual("".join(s["text"] for s in result["segments"]), "我觉得曼城换人")
        self.assertEqual(factory.call_args.kwargs["device"], "cpu")
        self.assertEqual(result["runtime"]["device"], "cpu")

    def test_imported_transcript_preserves_raw_and_never_loads_whisper(self):
        source = self.root / "人工转录.json"
        source.write_text(json.dumps({"segments": [{"start": 1, "end": 4, "text": "曼城配合"}]}), encoding="utf-8")
        with patch("core.transcriber._make_model", side_effect=AssertionError("import does not need ASR")):
            result = transcribe_audio(self.wav, self.meta, self.cfg, self.root, transcript_path=source)
        self.assertEqual(result["provenance"], "imported")
        self.assertTrue((self.root / "transcript.imported.raw.json").is_file())

    def test_rounding_tolerance_never_creates_reversed_or_out_of_video_times(self):
        for segment in [
            {"start": 20.01, "end": 20.03, "text": "已超时"},
            {"start": 19, "end": 20.03, "text": "结尾", "words": [{"start": 19.8, "end": 20.03, "word": "结尾"}]},
            {"start": 19, "end": 20, "text": "结尾", "words": [{"start": 20.01, "end": 20.02, "word": "结尾"}]},
        ]:
            with self.subTest(segment=segment):
                try:
                    result = validate_transcript({"segments": [segment]}, 20)
                except ValueError:
                    continue
                for sentence in result["segments"]:
                    self.assertTrue(0 <= sentence["start"] < sentence["end"] <= 20)
                    for word in sentence["words"]:
                        self.assertTrue(sentence["start"] <= word["start"] <= word["end"] <= sentence["end"])

    def test_corrupt_final_cache_rebuilds_from_chunks_without_loading_model(self):
        model = FakeWhisper([self.first, self.second])
        with patch("core.transcriber._make_model", return_value=(model, "cpu", "int8")):
            original = transcribe_audio(self.wav, self.meta, self.cfg, self.root)
        (self.root / "transcript.json").write_text("{broken", encoding="utf-8")
        with patch("core.transcriber._make_model", side_effect=AssertionError("chunks suffice")):
            recovered = transcribe_audio(self.wav, self.meta, self.cfg, self.root)
        self.assertEqual(recovered, original)
        self.assertEqual(len(list(self.root.glob("transcript.json.corrupt-*"))), 1)

    def test_corrupt_chunk_retranscribes_only_that_chunk(self):
        model = FakeWhisper([self.first, self.second])
        with patch("core.transcriber._make_model", return_value=(model, "cpu", "int8")):
            original = transcribe_audio(self.wav, self.meta, self.cfg, self.root)
        (self.root / "transcript.json").write_text("{}", encoding="utf-8")
        broken = next((self.root / "asr").glob("*/chunk_0000.json"))
        broken.write_text('{"start":0,"end":12,"segments":[{"start":0,"end":1,"text":"bad","words":[null]}]}', encoding="utf-8")
        repair = FakeWhisper([self.first])
        with patch("core.transcriber._make_model", return_value=(repair, "cpu", "int8")):
            recovered = transcribe_audio(self.wav, self.meta, self.cfg, self.root)
        self.assertEqual(recovered, original)
        self.assertEqual(len(repair.calls), 1)
        self.assertEqual(len(list(broken.parent.glob("chunk_0000.json.corrupt-*"))), 1)


if __name__ == "__main__":
    unittest.main()
