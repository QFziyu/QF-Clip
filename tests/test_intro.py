"""Real media coverage for user-selected, optionally silent opening videos."""
import array
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.intro import prepend_intro, probe_intro
from core.video_input import run_media

FFMPEG = os.environ.get("FCF_TEST_FFMPEG") or os.environ.get("FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("FCF_TEST_FFPROBE") or os.environ.get("FFPROBE") or shutil.which("ffprobe")


class IntroValidationTests(unittest.TestCase):
    def test_invalid_orientation_fails_before_any_io(self):
        with self.assertRaisesRegex(ValueError, "orientation"):
            prepend_intro("body.mp4", "intro.mp4", "output.mp4", {}, "square")

    def test_missing_intro_is_actionable(self):
        with self.assertRaisesRegex(FileNotFoundError, "视频不存在"):
            probe_intro("this-opening-video-does-not-exist.mp4", {})

    def test_does_not_overwrite_input(self):
        meta = {"source": str(Path("intro.mp4").resolve()), "duration": 1}
        with patch("core.intro.probe_intro", return_value=meta), self.assertRaisesRegex(ValueError, "不能覆盖"):
            prepend_intro("intro.mp4", "intro.mp4", "intro.mp4", {})


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not available; set FCF_TEST_FFMPEG and FCF_TEST_FFPROBE")
class RealIntroTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="fcf-片头 media-")
        cls.root = Path(cls.temporary.name)
        cls.cfg = {"ffmpeg": FFMPEG, "ffprobe": FFPROBE,
                   "render": {"encoder": "libx264", "horizontal": [160, 90], "vertical": [90, 160], "fps": 10}}
        cls.silent = cls.root / "我自己的 片头 无声.mkv"
        cls.voiced = cls.root / "我自己的 片头 有声.mkv"
        cls.body = cls.root / "已剪辑 主体.mp4"
        common = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        run_media(common + ["-f", "lavfi", "-i", "color=c=red:size=160x100:rate=25:duration=1.24",
                            "-c:v", "libx264", "-preset", "ultrafast", "-an", "-output_ts_offset", "5", str(cls.silent)])
        run_media(common + ["-f", "lavfi", "-i", "color=c=red:size=120x160:rate=15:duration=1.4",
                            "-itsoffset", "0.25", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=1.15",
                            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
                            "-c:a", "pcm_s16le", "-output_ts_offset", "3", str(cls.voiced)])
        run_media(common + ["-f", "lavfi", "-i", "color=c=blue:size=96x64:rate=12:duration=2",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ac", "1",
                            "-output_ts_offset", "2", str(cls.body)])

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def _samples(self, video):
        data = subprocess.check_output([FFMPEG, "-v", "error", "-i", str(video), "-map", "0:a:0",
                                        "-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
        return array.array("h", data)

    def _color(self, video, seconds):
        data = subprocess.check_output([FFMPEG, "-v", "error", "-ss", str(seconds), "-i", str(video),
                                        "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
        self.assertEqual(len(data), 3)
        return data

    def _assert_decodes(self, video):
        run_media([FFMPEG, "-v", "error", "-i", str(video), "-f", "null", "-"])

    def test_probe_accepts_silent_intro_and_original_pts(self):
        meta = probe_intro(self.silent, self.cfg)
        self.assertIsNone(meta["selected_audio"])
        self.assertEqual(meta["audio_streams"], [])
        self.assertAlmostEqual(meta["timeline_origin"], 5, delta=0.002)
        self.assertAlmostEqual(meta["duration"], 1.24, delta=0.002)
        self.assertEqual(meta["signature"]["path"], str(self.silent.resolve()))

    def test_silent_intro_boundary_preserves_order_and_body_sound(self):
        destination = self.root / "中文 无声片头 合成.mp4"
        result = prepend_intro(self.body, self.silent, destination, self.cfg)
        self.assertAlmostEqual(result["intro_duration"], 1.3, places=6)
        self.assertAlmostEqual(result["body_duration"], 2.0, places=6)
        output = probe_intro(destination, self.cfg)
        self.assertAlmostEqual(output["duration"], result["duration"], delta=0.01)
        self.assertEqual((output["width"], output["height"]), (160, 90))
        beginning, after_intro = self._color(destination, 0.2), self._color(destination, 1.5)
        self.assertGreater(beginning[0], beginning[2] + 70)
        self.assertGreater(after_intro[2], after_intro[0] + 70)
        samples = self._samples(destination)
        first_sound = next(index for index, value in enumerate(samples) if abs(value) > 100) / 16000
        self.assertAlmostEqual(first_sound, result["intro_duration"], delta=0.07)
        self.assertLess(max(abs(value) for value in samples[:16000]), 50)
        self._assert_decodes(destination)

    def test_intro_own_delayed_audio_is_preserved_and_vertical_decodes(self):
        destination = self.root / "竖屏 有声 片头.mp4"
        result = prepend_intro(self.body, self.voiced, destination, self.cfg, orientation="vertical")
        output = probe_intro(destination, self.cfg)
        self.assertEqual((output["width"], output["height"]), (90, 160))
        self.assertAlmostEqual(output["duration"], result["duration"], delta=0.01)
        samples = self._samples(destination)
        first_sound = next(index for index, value in enumerate(samples) if abs(value) > 100) / 16000
        self.assertAlmostEqual(first_sound, 0.25, delta=0.06)
        self.assertGreater(max(abs(value) for value in samples[8000:12000]), 500)
        self.assertGreater(max(abs(value) for value in samples[int(result["intro_duration"] * 16000) + 8000:]), 500)
        first, second = self._color(destination, 0.2), self._color(destination, result["intro_duration"] + 0.2)
        self.assertGreater(first[0], first[2] + 70)
        self.assertGreater(second[2], second[0] + 70)
        data = json.loads(run_media([FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(destination)]).stdout)
        audio = next(stream for stream in data["streams"] if stream["codec_type"] == "audio")
        self.assertEqual((int(audio["sample_rate"]), audio["channels"]), (48000, 2))
        self._assert_decodes(destination)

    def test_nvenc_runtime_failure_falls_back_without_touching_sources(self):
        cfg = {**self.cfg, "render": {**self.cfg["render"], "encoder": "h264_nvenc"}}
        original_sizes = [source.stat().st_size for source in (self.body, self.silent)]
        attempts = []
        actual = run_media

        def controlled_media(command, **kwargs):
            if "-c:v" in command:
                encoder = command[command.index("-c:v") + 1]
                attempts.append(encoder)
                if encoder == "h264_nvenc":
                    raise RuntimeError("simulated unavailable NVENC driver")
            return actual(command, **kwargs)

        destination = self.root / "fallback.mp4"
        with patch("core.intro.run_media", side_effect=controlled_media), self.assertLogs("core.intro", level="WARNING"):
            result = prepend_intro(self.body, self.silent, destination, cfg)
        self.assertEqual(attempts, ["h264_nvenc", "libx264"])
        self.assertEqual(result["encoder"], "libx264")
        self.assertEqual(original_sizes, [source.stat().st_size for source in (self.body, self.silent)])
        self._assert_decodes(destination)

    def test_audio_only_file_rejected(self):
        audio = self.root / "only-audio.wav"
        run_media([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1", str(audio)])
        with self.assertRaisesRegex(ValueError, "视频画面"):
            probe_intro(audio, self.cfg)


if __name__ == "__main__":
    unittest.main()
