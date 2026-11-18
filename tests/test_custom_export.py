"""End-to-end acceptance for batch subtitle styling and optional opening videos.

Every media file is an explicitly synthetic temporary fixture; no user project,
model, network endpoint, or existing export is accessed by these tests.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.config import DEFAULTS
from core.export_options import normalize_export_options
from core import jobs, pipeline
from core.intro import probe_intro
from core.subtitle import parse_srt, transcript_cues
from core.utils import atomic_json, read_json
from core.video_input import run_media
from storage.database import Database

FFMPEG = os.environ.get("FCF_TEST_FFMPEG") or os.environ.get("FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("FCF_TEST_FFPROBE") or os.environ.get("FFPROBE") or shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/FFprobe not available; set FCF_TEST_FFMPEG and FCF_TEST_FFPROBE")
class CustomExportAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory(prefix="fcf-自定义导出 fixtures-")
        cls.media_root = Path(cls.media.name)
        cls.source = cls.media_root / "synthetic 中文 直播.mp4"
        cls.intro = cls.media_root / "用户自选 无声片头.mkv"
        cls.other_intro = cls.media_root / "另一个片头.mp4"
        common = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        run_media(common + ["-f", "lavfi", "-i", "color=c=0x141414:size=320x180:rate=10:duration=8",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=8",
                            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(cls.source)])
        run_media(common + ["-f", "lavfi", "-i", "color=c=red:size=160x100:rate=25:duration=1.24",
                            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-output_ts_offset", "5", str(cls.intro)])
        run_media(common + ["-f", "lavfi", "-i", "color=c=blue:size=160x100:rate=10:duration=0.6",
                            "-an", "-c:v", "libx264", "-preset", "ultrafast", str(cls.other_intro)])

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fcf-custom-export-")
        self.root = Path(self.temporary.name)
        self.transcript = self.root / "人工构造 fixture 转录.json"
        self.fixture = {"provenance": "synthetic-fixture-not-real-ASR", "segments": [
            {"start": 0.5, "end": 2.5, "text": "曼城进球了！"},
            {"start": 4.5, "end": 6.5, "text": "这次换人非常关键。"},
        ]}
        atomic_json(self.transcript, self.fixture)
        prompt = self.root / "fixture-prompt.md"
        prompt.write_text("Synthetic fixture; only select quoted transcript evidence.", encoding="utf-8")
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(ffmpeg=FFMPEG, ffprobe=FFPROBE, cache_dir=str(self.root / "cache"),
                        output_dir=str(self.root / "output"), database=str(self.root / "fixture.sqlite3"),
                        prompt_path=str(prompt), terms=[])
        self.cfg["selection"].update(mode="semantic", audio_events=False, min_seconds=1,
                                     max_seconds=3, context_before=0, context_after=0)
        self.cfg["render"].update(encoder="libx264", horizontal=[320, 180], vertical=[180, 320],
                                  preview=[320, 180], fps=10, bitrate="500k", burn_subtitles=True)
        self.cfg["openai"]["api_key"] = "SYNTHETIC_SECRET_MUST_NOT_ENTER_QUEUE"
        with patch("core.clip_selector.LLMClient.complete", side_effect=AssertionError("mock must never call API")):
            imported = pipeline.process_video(self.source, self.cfg, mock=True, transcript_path=self.transcript, skip_render=True)
        self.project_id = imported["project_id"]
        with Database(self.cfg["database"]) as db:
            candidates = db.list_clips(self.project_id)
            self.assertEqual(len(candidates), 2)
            self.clips = []
            for index, (clip, segment) in enumerate(zip(candidates, self.fixture["segments"]), 1):
                self.clips.append(db.review_clip(clip["id"], "approved", title=f"人工修改标题 {index}",
                                                  start=segment["start"], end=segment["end"]))
        self.options = normalize_export_options({
            "subtitle": {"preset": "classic", "primary_color": "#22FF55", "font_size": 22,
                         "vertical_font_size": 24, "outline": 1.5, "shadow": 0,
                         "margin_v": 16, "vertical_margin_v": 28},
            "burn_subtitles": True, "intro": {"enabled": True, "path": str(self.intro)},
        })

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def _frame(self, path, seconds):
        return subprocess.check_output([FFMPEG, "-v", "error", "-ss", str(seconds), "-i", str(path),
                                        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])

    @staticmethod
    def _green_pixels(frame):
        return sum(frame[index] < 120 and frame[index + 1] > 150 and frame[index + 2] < 150
                   for index in range(0, len(frame), 3))

    def _assert_preserved(self, hashes):
        for path, digest in hashes.items():
            self.assertTrue(Path(path).is_file(), path)
            self.assertEqual(self._digest(path), digest, path)

    def test_batch_styles_intro_timeline_reuse_and_new_packages(self):
        result = pipeline.export_project(self.project_id, self.cfg, orientations=("horizontal", "vertical"),
                                         export_options=self.options)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["exports"]), 4)
        baseline_hashes = {item["video"]: self._digest(item["video"]) for item in result["exports"]}
        by_id = {clip["id"]: clip for clip in self.clips}
        for item in result["exports"]:
            with self.subTest(clip=item["clip_id"], orientation=item["orientation"]):
                video = Path(item["video"])
                clip = by_id[item["clip_id"]]
                self.assertEqual(video.stem, clip["title"])
                meta = probe_intro(video, self.cfg)
                offset = item["intro"]["duration"]
                self.assertAlmostEqual(offset, 1.3, places=6)
                self.assertAlmostEqual(meta["duration"], clip["end"] - clip["start"] + offset, delta=0.01)
                self.assertAlmostEqual(item["duration"], meta["duration"], delta=0.01)
                expected = (320, 180) if item["orientation"] == "horizontal" else (180, 320)
                self.assertEqual((meta["width"], meta["height"]), expected)
                relative = transcript_cues(self.fixture, clip)[0]["start"]
                cues = parse_srt(Path(item["subtitles"]["srt"]).read_text(encoding="utf-8-sig"), meta["duration"])
                self.assertAlmostEqual(cues[0]["start"], relative + offset, places=3)
                ass = Path(item["subtitles"]["ass"]).read_text(encoding="utf-8")
                self.assertIn("&H0055FF22", ass)
                first_ass = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
                self.assertEqual(first_ass.split(",")[1], "0:00:01.30")
                timeline = read_json(video.with_suffix(".timeline.json"))
                self.assertAlmostEqual(timeline["content_start"], offset)
                self.assertAlmostEqual(timeline["subtitle_offset"], offset)
                self.assertEqual([part["kind"] for part in timeline["segments"]], ["intro", "content"])
                self.assertAlmostEqual(timeline["segments"][0]["output_end"], offset)
                self.assertAlmostEqual(timeline["segments"][1]["output_start"], offset)
                self.assertAlmostEqual(timeline["segments"][1]["source_start"], clip["start"])
                self.assertAlmostEqual(timeline["segments"][1]["source_end"], clip["end"])
                # Full decoding verifies a playable export, not just file presence.
                run_media([FFMPEG, "-v", "error", "-i", str(video), "-f", "null", "-"])
        representative = result["exports"][0]
        self.assertLess(self._green_pixels(self._frame(representative["video"], 0.5)), 10)
        self.assertGreater(self._green_pixels(self._frame(representative["video"], 2.0)), 30)
        with patch("core.pipeline._render_final_export", side_effect=AssertionError("identical export must reuse video")):
            repeated = pipeline.export_project(self.project_id, self.cfg, orientations=("horizontal", "vertical"),
                                               export_options=self.options)
        self.assertEqual(repeated["errors"], [])
        self.assertEqual({item["video"] for item in repeated["exports"]}, set(baseline_hashes))
        self._assert_preserved(baseline_hashes)

        changes = []
        styled = copy.deepcopy(self.options)
        styled["subtitle"]["primary_color"] = "#FFD34E"
        changes.append(styled)
        other_intro = copy.deepcopy(self.options)
        other_intro["intro"]["path"] = str(self.other_intro)
        changes.append(other_intro)
        no_intro = copy.deepcopy(self.options)
        no_intro["intro"]["enabled"] = False
        changes.append(no_intro)
        used_paths = set(baseline_hashes)
        for options in changes:
            with self.subTest(changed=options):
                changed = pipeline.export_project(self.project_id, self.cfg, export_options=options)
                self.assertEqual(changed["errors"], [])
                self.assertEqual(len(changed["exports"]), 2)
                paths = {item["video"] for item in changed["exports"]}
                self.assertFalse(paths & used_paths)
                used_paths.update(paths)
                for item in changed["exports"]:
                    meta = probe_intro(item["video"], self.cfg)
                    expected_offset = (0 if not options["intro"]["enabled"] else
                                       0.6 if options["intro"]["path"] == str(self.other_intro) else 1.3)
                    self.assertAlmostEqual(item["subtitle_offset"], expected_offset, places=6)
                    self.assertAlmostEqual(meta["duration"], 2 + expected_offset, delta=0.01)
                    run_media([FFMPEG, "-v", "error", "-i", item["video"], "-f", "null", "-"])
                self._assert_preserved(baseline_hashes)

    def test_preferences_restart_and_queue_snapshot_exclude_credentials(self):
        with Database(self.cfg["database"]) as db:
            db.set_export_options(self.project_id, self.options)
        with Database(self.cfg["database"]) as reopened:
            self.assertEqual(reopened.get_export_options(self.project_id), self.options)
        with patch("core.jobs.ensure_worker"):
            queued = jobs.submit_job("export", {"project_id": self.project_id,
                                     "orientations": ["horizontal", "vertical"], "export_options": self.options}, self.cfg)
        original_snapshot = copy.deepcopy(queued["payload"]["export_options"])
        self.options["subtitle"]["primary_color"] = "#FF0000"
        self.options["intro"]["enabled"] = False
        with Database(self.cfg["database"]) as db:
            db.set_export_options(self.project_id, self.options)
        persisted = jobs.get_job(queued["id"], self.cfg)
        self.assertEqual(persisted["payload"]["export_options"], original_snapshot)
        self.assertNotIn(self.cfg["openai"]["api_key"], json.dumps(persisted, ensure_ascii=False))
        with Database(self.cfg["database"]) as db:
            raw = db.connection.execute("SELECT payload_json, paths_json FROM jobs WHERE id=?", (queued["id"],)).fetchone()
        self.assertNotIn(self.cfg["openai"]["api_key"], "".join(raw))
        with patch("core.jobs.ensure_worker"), self.assertRaises(ValueError):
            jobs.submit_job("export", {"project_id": self.project_id,
                            "export_options": {"api_key": self.cfg["openai"]["api_key"]}}, self.cfg)


if __name__ == "__main__":
    unittest.main()
