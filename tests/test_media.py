"""Unit checks plus real FFmpeg tests when FFMPEG/FFPROBE (or PATH) are available."""
import array
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from core.audio_extract import extract_audio
from core.clip_renderer import render_clip
from core.subtitle import parse_srt, transcript_cues, validate_srt, write_subtitles
from core.video_input import discover_videos, probe_video, run_media


class SubtitleTests(unittest.TestCase):
    def test_word_timestamps_clip_excludes_outside_text(self):
        transcript = {"segments": [{"start": 10, "end": 20, "text": "前面进球后面", "words": [
            {"start": 10, "end": 12, "word": "前面"}, {"start": 13, "end": 15, "word": "进球"},
            {"start": 18, "end": 20, "word": "后面"}]}]}
        cues = transcript_cues(transcript, {"start": 12, "end": 17})
        self.assertEqual(cues, [{"start": 1.0, "end": 3.0, "text": "进球"}])

    def test_chinese_split_and_relative_times(self):
        transcript = {"segments": [{"start": 4, "end": 24, "text": "曼城这一次的进攻配合非常精彩！教练换人的时机十分关键。"}]}
        cues = transcript_cues(transcript, {"start": 9, "end": 19}, max_chars=8)
        self.assertTrue(cues)
        self.assertTrue(all(0 <= c["start"] < c["end"] <= 10 for c in cues))
        self.assertTrue(all(len(line) <= 8 for c in cues for line in c["text"].splitlines()))

    def test_reject_invalid_manual_subtitles(self):
        for srt in ["1\n00:00:02,000 --> 00:00:01,000\na", "1\n00:00:00,000 --> 00:00:08,000\na", "1\n00:60:00,000 --> 01:01:00,000\na"]:
            with self.subTest(srt=srt), self.assertRaises(ValueError):
                validate_srt(srt, 5)
        self.assertTrue(validate_srt("1\n00:00:00,100 --> 00:00:04,900\n中文", 5))

    def test_edited_text_round_trip_and_ass_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original = "1\n00:00:00,100 --> 00:00:01,200\n{\\pos(0,0)}曼城"
            result = write_subtitles({}, {"start": 10, "end": 12}, base / "字幕.srt", base / "字幕.ass", {}, edited_srt=original)
            srt = Path(result["srt"]).read_text(encoding="utf-8-sig")
            self.assertEqual(parse_srt(srt, 2)[0]["text"], "{\\pos(0,0)}曼城")
            self.assertNotIn("{\\pos", Path(result["ass"]).read_text(encoding="utf-8"))
            self.assertEqual(result["cue_count"], 1)

    def test_discover_local_files_and_recursive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "第一 场.MP4").touch()
            (root / "ignore.txt").touch()
            (root / "nested").mkdir()
            (root / "nested" / "第二场.mkv").touch()
            self.assertEqual(len(discover_videos(root)), 1)
            self.assertEqual(len(discover_videos(root, recursive=True)), 2)

    def test_render_rejects_invalid_timestamps_before_process(self):
        for start, end in [(1, 1), (-1, 2), (0, math.inf), (0, 11)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                render_clip({"duration": 10}, {"start": start, "end": end}, "unused.mp4", {})

    def test_nvenc_runtime_failure_retries_software_encoder(self):
        attempts = []

        def fake_media(command, **kwargs):
            encoder = command[command.index("-c:v") + 1]
            attempts.append(encoder)
            if encoder == "h264_nvenc":
                raise RuntimeError("NVENC driver unavailable")
            Path(command[-1]).write_bytes(b"simulated encoded video")

        with tempfile.TemporaryDirectory() as directory, patch("core.clip_renderer.run_media", side_effect=fake_media), self.assertLogs("core.clip_renderer", level="WARNING"):
            result = render_clip({"source": "source.mp4", "duration": 10, "selected_audio": 1}, {"start": 1, "end": 5},
                                 Path(directory) / "fallback.mp4", {"render": {"encoder": "h264_nvenc"}})
            self.assertEqual(attempts, ["h264_nvenc", "libx264"])
            self.assertEqual(json.loads(result.with_suffix(".timeline.json").read_text())["encoder"], "libx264")


FFMPEG = os.environ.get("FCF_TEST_FFMPEG") or os.environ.get("FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("FCF_TEST_FFPROBE") or os.environ.get("FFPROBE") or shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not available; set FCF_TEST_FFMPEG and FCF_TEST_FFPROBE")
class RealMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="fcf-中文 media-")
        cls.root = Path(cls.temporary.name)
        cls.cfg = {"ffmpeg": FFMPEG, "ffprobe": FFPROBE,
                   "render": {"encoder": "libx264", "horizontal": [160, 90], "vertical": [90, 160], "preview": [160, 90], "fps": 10},
                   "subtitle": {"font_size": 14, "margin_v": 12, "max_chars": 8}}
        cls.source = cls.root / "中文 多音轨.mkv"
        # Video PTS 5..13; audio track 0 starts at 5, selected track 1 at 7.
        run_media([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=8",
                   "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=16000:duration=8", "-itsoffset", "2", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=6",
                   "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", "-ac:a:1", "2", "-output_ts_offset", "5", str(cls.source)])
        cls.meta = probe_video(cls.source, cls.cfg, audio_track=1)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @staticmethod
    def _wave_samples(path):
        with wave.open(str(path), "rb") as stream:
            return stream.getframerate(), array.array("h", stream.readframes(stream.getnframes()))

    def test_probe_preserves_offset_and_selected_audio(self):
        self.assertAlmostEqual(self.meta["timeline_origin"], 5, places=2)
        self.assertAlmostEqual(self.meta["duration"], 8, places=2)
        self.assertEqual(self.meta["selected_audio"], 2)
        self.assertEqual(self.meta["audio_streams"][1]["channels"], 2)
        self.assertAlmostEqual(self.meta["audio_streams"][1]["offset_from_video"], 2, places=2)
        with self.assertRaises(ValueError):
            probe_video(self.source, self.cfg, audio_track=2)

    def test_audio_extract_fills_delayed_track_and_preserves_length(self):
        output = extract_audio(self.meta, self.root / "识别 音频.wav", self.cfg)
        rate, samples = self._wave_samples(output)
        self.assertEqual(rate, 16000)
        self.assertAlmostEqual(len(samples) / rate, 8, places=2)
        first_sound = next(i for i, value in enumerate(samples) if abs(value) > 100) / rate
        self.assertAlmostEqual(first_sound, 2, delta=0.02)

    def test_accurate_seek_actual_content_and_both_layouts(self):
        clip = {"start": 3.2, "end": 6.5}
        for orientation in ("horizontal", "vertical"):
            with self.subTest(orientation=orientation):
                output = render_clip(self.meta, clip, self.root / f"{orientation} 预览.mp4", self.cfg, orientation=orientation, preview=True)
                result = probe_video(output, self.cfg)
                self.assertAlmostEqual(result["duration"], 3.3, delta=0.11)
                expected = (160, 90) if orientation == "horizontal" else (90, 160)
                self.assertEqual((result["width"], result["height"]), expected)
                mapping = json.loads(output.with_suffix(".timeline.json").read_text(encoding="utf-8"))
                self.assertAlmostEqual(mapping["source_pts_start"], 8.2)
                # Decode the entire result: existence alone is not playback verification.
                run_media([FFMPEG, "-v", "error", "-i", str(output), "-f", "null", "-"])
        # The first rendered frame must correspond to source video time 3.2s.
        common = [FFMPEG, "-v", "error"]
        expected = subprocess.check_output(common + ["-copyts", "-i", str(self.source), "-vf", "trim=start=8.2", "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "-"])
        actual = subprocess.check_output(common + ["-i", str(self.root / "horizontal 预览.mp4"), "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "-"])
        self.assertEqual(len(expected), len(actual))
        self.assertLess(sum(abs(a - b) for a, b in zip(expected, actual)) / len(expected), 5.0)

    def test_clip_audio_gap_and_hard_subtitles_in_chinese_path(self):
        clip = {"start": 1, "end": 4}
        subtitle = write_subtitles({"segments": [{"start": 2, "end": 3.5, "text": "曼城攻势，精彩！"}]}, clip,
                                  self.root / "中文 文字.srt", self.root / "中文 文字.ass", self.cfg)
        output = render_clip(self.meta, clip, self.root / "硬字幕 成品.mp4", self.cfg, subtitle_path=subtitle["ass"])
        rendered_meta = probe_video(output, self.cfg)
        self.assertAlmostEqual(rendered_meta["duration"], 3, delta=0.11)
        audio = extract_audio(rendered_meta, self.root / "rendered.wav", self.cfg)
        rate, samples = self._wave_samples(audio)
        first_sound = next(i for i, value in enumerate(samples) if abs(value) > 100) / rate
        self.assertAlmostEqual(first_sound, 1, delta=0.07)

    def test_no_audio_raises_actionable_error(self):
        silent = self.root / "无音轨.mp4"
        run_media([FFMPEG, "-v", "error", "-y", "-i", str(self.source), "-map", "0:v:0", "-c:v", "copy", "-an", str(silent)])
        with self.assertRaisesRegex(ValueError, "没有音轨"):
            probe_video(silent, self.cfg)


if __name__ == "__main__":
    unittest.main()
