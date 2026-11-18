"""Actual FFmpeg acceptance using synthetic media only, never user recordings."""
import copy
import hashlib
import os
import json
import subprocess
import sys
import time
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from core.config import DEFAULTS
from core.editing import create_editing_plan, export_editing_plan, preview_plan_draft
from core.pipeline import process_video
from core.intro import probe_intro
from core.subtitle import parse_srt
from core.utils import atomic_json, read_json
from core.video_input import run_media
from storage.plans import PlanStore

FFMPEG = os.environ.get("FCF_TEST_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("FCF_TEST_FFPROBE") or shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "Requires FFmpeg/FFprobe")
class PlanMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fcf-方案集锦-")
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(ffmpeg=FFMPEG, ffprobe=FFPROBE, database=str(self.root / "db.sqlite3"),
                        cache_dir=str(self.root / "cache"), output_dir=str(self.root / "output"),
                        prompt_path=str(Path(__file__).resolve().parents[1] / "prompts/clip_selection.md"), terms=[])
        self.cfg["selection"].update(context_before=0, context_after=0, mode="semantic", audio_events=False)
        self.cfg["render"].update(encoder="libx264", horizontal=[320, 180], vertical=[180, 320],
                                  preview=[240, 136], fps=10, bitrate="400k", burn_subtitles=True)
        self.cfg["subtitle"].update(font_size=22, margin_v=15)
        self.source = self.root / "合成测试 十二秒.mp4"
        self.intro = self.root / "自选无声片头.mkv"
        base = [FFMPEG, "-v", "error", "-nostdin", "-y"]
        run_media(base + ["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=12", "-f", "lavfi", "-i",
                          "sine=frequency=440:sample_rate=48000:duration=12", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(self.source)])
        run_media(base + ["-f", "lavfi", "-i", "color=c=red:size=160x100:rate=25:duration=1.24", "-an",
                          "-c:v", "libx264", "-preset", "ultrafast", "-output_ts_offset", "5", str(self.intro)])
        self.transcript = self.root / "fixture.json"
        atomic_json(self.transcript, {"segments": [{"start": .3, "end": 2.8, "text": "第一段完整观点。"},
                                                   {"start": 6.3, "end": 9.4, "text": "第二段战术分析。"}]})
        self.options = {"format": "montage", "content": "match", "count": 1, "min_seconds": 1,
                        "target_seconds": 4, "max_seconds": 10,
                        "anchors": [{"period": "first", "match_time": "35:00", "video_time": 0}],
                        "events": [{"period": "first", "match_time": "35:02", "label": "人工指定测试事件", "before": .5, "after": .5}]}
        result = process_video(self.source, self.cfg, mock=True, transcript_path=self.transcript,
                               skip_render=True, editing_options=self.options)
        self.plan_id, self.project_id = result["plan_id"], result["project_id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_batch_montage_intro_captions_timeline_cache_and_old_files(self):
        with PlanStore(self.cfg["database"]) as db:
            plan = db.get_plan(self.plan_id)
            draft = plan["drafts"][0]
            self.assertEqual(db.list_clips(self.project_id), [])
        with self.assertRaises(ValueError):
            export_editing_plan(self.plan_id, self.cfg)
        with PlanStore(self.cfg["database"]) as db:
            plan = db.edit_plan(self.plan_id, 0, {draft["id"]: {"status": "approved", "title": "人工修改 集锦标题",
                "segments": [{"start": .3, "end": 2.8}, {"start": 6.3, "end": 9.4}]}}, {"e1": {"confirmed": True}})
        preview = preview_plan_draft(self.plan_id, draft["id"], self.cfg)
        self.assertAlmostEqual(preview["duration"], 5.6, places=3)
        options = {"subtitle": {"preset": "gold", "font_size": 22, "vertical_font_size": 24},
                   "intro": {"enabled": True, "path": str(self.intro)}}
        result = export_editing_plan(self.plan_id, self.cfg, ("horizontal", "vertical"), options)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["exports"]), 2)
        hashes = {}
        for record in result["exports"]:
            video = Path(record["video"])
            self.assertEqual(video.stem, "人工修改 集锦标题")
            with video.open('rb') as f:hashes[str(video)] = hashlib.file_digest(f,'sha256').hexdigest()
            meta = probe_intro(video, self.cfg)
            self.assertAlmostEqual(meta["duration"], 6.9, places=2)
            timeline = read_json(video.with_suffix(".timeline.json"))
            self.assertEqual(len(timeline["segments"]), 3)
            self.assertAlmostEqual(timeline["segments"][1]["output_start"], 1.3, places=3)
            self.assertAlmostEqual(timeline["segments"][2]["output_start"], 3.8, places=3)
            self.assertEqual(timeline["segments"][2]["source_start"], 6.3)
            cues = parse_srt(Path(record["subtitles"]["srt"]).read_text(encoding="utf-8-sig"), meta["duration"])
            self.assertAlmostEqual(cues[0]["start"], 1.3, places=3)
            second = next(cue for cue in cues if "第二段" in cue["text"])
            self.assertAlmostEqual(second["start"], 3.8, places=3)
            self.assertIn("&H004ED3FF", Path(record["subtitles"]["ass"]).read_text(encoding="utf-8"))
            run_media([FFMPEG, "-v", "error", "-i", str(video), "-f", "null", "-"])
        with patch("core.plan_renderer.render_clip", side_effect=AssertionError("identical export must reuse")):
            again = export_editing_plan(self.plan_id, self.cfg, ("horizontal", "vertical"), options)
        self.assertEqual(again["errors"], [])
        self.assertEqual({x["video"] for x in again["exports"]}, set(hashes))
        with PlanStore(self.cfg["database"]) as db:
            db.edit_plan(self.plan_id, plan["revision"], {draft["id"]: {"title": "新版集锦"}})
        changed = export_editing_plan(self.plan_id, self.cfg, export_options=options)
        self.assertEqual(changed["errors"], [])
        self.assertNotIn(changed["exports"][0]["video"], hashes)
        for path, digest in hashes.items():
            with Path(path).open('rb') as f:self.assertEqual(hashlib.file_digest(f,'sha256').hexdigest(),digest)

    def test_continuous_manual_subtitles_and_no_network_after_import(self):
        options = {"content": "conversation", "format": "continuous", "count": 1,
                   "min_seconds": 1, "target_seconds": 6, "max_seconds": 10}
        with patch("core.transcriber.transcribe_audio", side_effect=AssertionError("reuse ASR")), \
             patch("core.llm_client.LLMClient.complete", side_effect=AssertionError("explicit mock only")):
            created = create_editing_plan(self.project_id, self.cfg, options, mock=True)
        with PlanStore(self.cfg["database"]) as db:
            plan = db.get_plan(created["plan_id"])
            draft = plan["drafts"][0]
            self.assertEqual(len(draft["segments"]), 1)
            db.edit_plan(plan["id"], 0, {draft["id"]: {"status": "approved", "segments": [{"start": 1, "end": 7}],
                         "subtitle_text": "1\n00:00:01,000 --> 00:00:02,000\n人工编辑字幕\n"}})
        result = export_editing_plan(plan["id"], self.cfg, export_options={"burn_subtitles": False})
        self.assertEqual(result["errors"], [])
        record = result["exports"][0]
        cues = parse_srt(Path(record["subtitles"]["srt"]).read_text(encoding="utf-8-sig"), record["duration"])
        self.assertEqual(cues[0]["text"], "人工编辑字幕")
        self.assertEqual(cues[0]["start"], 1)
        run_media([FFMPEG, "-v", "error", "-i", record["video"], "-f", "null", "-"])

    def test_custom_montage_manual_captions_cross_cut_intro_and_render(self):
        created = create_editing_plan(self.project_id, self.cfg, {"content": "custom", "prompt": "完整观点",
            "format": "montage", "count": 1, "min_seconds": 1, "target_seconds": 6, "max_seconds": 10}, mock=True)
        with PlanStore(self.cfg["database"]) as store:
            plan = store.get_plan(created["plan_id"])
            draft = plan["drafts"][0]
            store.edit_plan(plan["id"], 0, {draft["id"]: {"status": "approved", "segments": [
                {"start": .3, "end": 2.8}, {"start": 6.3, "end": 9.4}], "subtitle_text":
                "1\n00:00:00,000 --> 00:00:01,000\n自定义第一句\n\n"
                "2\n00:00:02,000 --> 00:00:03,000\n横跨拼接点\n\n"
                "3\n00:00:04,000 --> 00:00:05,500\n自定义最后一句\n"}})
        options = {"intro": {"enabled": True, "path": str(self.intro)}, "burn_subtitles": True}
        preview = preview_plan_draft(plan["id"], draft["id"], self.cfg)
        self.assertIn("自定义第一句", Path(preview["subtitles"]["srt"]).read_text(encoding="utf-8-sig"))
        result = export_editing_plan(plan["id"], self.cfg, ("horizontal", "vertical"), options)
        self.assertEqual(result["errors"], [])
        for record in result["exports"]:
            text = Path(record["subtitles"]["srt"]).read_text(encoding="utf-8-sig")
            cues = parse_srt(text, record["duration"])
            self.assertEqual(cues[0]["text"], "自定义第一句")
            self.assertAlmostEqual(cues[0]["start"], 1.3, places=3)
            self.assertEqual([c['text'] for c in cues].count("横跨拼接点"), 2)
            self.assertAlmostEqual(cues[-1]["end"], 6.8, places=3)
            self.assertNotIn("第二段战术分析", text)
            self.assertIn("自定义第一句", Path(record["subtitles"]["ass"]).read_text(encoding="utf-8"))
            run_media([FFMPEG, "-v", "error", "-i", record["video"], "-f", "null", "-"])

    def test_cli_detached_worker_plan_review_and_export(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        config_path = self.root / "cli-settings.yaml"
        config_path.write_text(yaml.safe_dump({k: self.cfg[k] for k in ("database", "output_dir", "cache_dir", "render", "selection", "ffmpeg", "ffprobe")}), encoding="utf-8")
        env = {**os.environ, "PYTHONUTF8": "1", "DATABASE_PATH": self.cfg["database"]}

        def cli(*args):
            process = subprocess.run([sys.executable, str(root / "main.py"), "--config", str(config_path), *args],
                                     cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
            self.assertEqual(process.returncode, 0, process.stderr[-2000:])
            return json.loads(process.stdout)

        def completed(job):
            from core.jobs import get_job
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                current = get_job(job["id"], self.cfg)
                if current["status"] in ("completed", "failed", "interrupted"):
                    self.assertEqual(current["status"], "completed", current.get("error"))
                    return current["result"]
                time.sleep(.1)
            self.fail("detached worker did not finish synthetic task")

        job = cli("process", str(self.source), "--mock", "--transcript", str(self.transcript), "--content", "conversation",
                  "--format", "continuous", "--target-count", "1", "--min-seconds", "1", "--target-seconds", "6",
                  "--max-seconds", "10", "--mode", "semantic", "--skip-render", "--background")
        result = completed(job)
        plan = cli("plans", "--plan", result["plan_id"])
        draft_id = plan["drafts"][0]["id"]
        cli("plan-review", "--plan", plan["id"], "--draft", draft_id, "--status", "approved", "--title", "CLI 审批后标题")
        exported = completed(cli("plan-export", "--plan", plan["id"], "--background"))
        self.assertEqual(len(exported["exports"]), 1)
        self.assertEqual(Path(exported["exports"][0]["video"]).stem, "CLI 审批后标题")
        run_media([FFMPEG, "-v", "error", "-i", exported["exports"][0]["video"], "-f", "null", "-"])


if __name__ == "__main__":
    unittest.main()
