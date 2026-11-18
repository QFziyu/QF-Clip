"""Workflow recovery tests use real SQLite/WAV/files and fake media encoders."""
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from core.config import DEFAULTS, load_config
from core import pipeline
from core.transcriber import validate_transcript
from core.utils import atomic_json, fingerprint, read_json
from storage.database import Database


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fcf-pipeline-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "中文 测试录像.mp4"
        self.source.write_bytes(b"Explicit test fixture: media is mocked, not a playable source.")
        self.transcript = self.root / "fixture-transcript.json"
        self.transcript.write_text(json.dumps({"segments": [
            {"start": 2, "end": 6, "text": "这次换人非常关键。"},
            {"start": 12, "end": 16, "text": "他的跑位拉开了空间。"},
            {"start": 22, "end": 26, "text": "我们再聊聊战术变化。"}]}), encoding="utf-8")
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("Only select quoted, timed source text.", encoding="utf-8")
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(cache_dir=str(self.root / "cache"), output_dir=str(self.root / "output"),
                        database=str(self.root / "review.sqlite3"), prompt_path=str(self.prompt), terms=["曼城"])
        self.cfg["selection"].update(audio_events=False, context_before=1, context_after=1, min_seconds=5)
        self.cfg["render"]["burn_subtitles"] = False
        self.metadata = {"source": str(self.source), "duration": 30.0, "width": 1920, "height": 1080,
                         "selected_audio": 1, "timeline_origin": 0, "fps": 30}
        self.stack = ExitStack()
        self.probe = self.stack.enter_context(patch("core.pipeline.probe_video", side_effect=lambda *a, **k: dict(self.metadata)))
        self.extract = self.stack.enter_context(patch("core.pipeline.extract_audio", side_effect=self._extract))
        self.render = self.stack.enter_context(patch("core.pipeline.render_clip", side_effect=self._render))
        self.select = self.stack.enter_context(patch("core.pipeline.select_clips", wraps=pipeline.select_clips))

    def tearDown(self):
        self.stack.close()
        self.temporary.cleanup()

    def _extract(self, meta, destination, cfg):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            for _ in range(int(meta["duration"])):
                output.writeframes(b"\0\0" * 16000)
        return Path(destination)

    def _render(self, meta, clip, destination, cfg, **kwargs):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"Mock encoded output; not playable media.")
        atomic_json(Path(destination).with_suffix(".timeline.json"),
                    {"output": str(destination), "start": clip["start"], "end": clip["end"]})
        return Path(destination)

    def process(self, **kwargs):
        return pipeline.process_video(self.source, self.cfg, mock=True, transcript_path=self.transcript, **kwargs)

    def clips(self, project_id):
        with Database(self.cfg["database"]) as db:
            return db.list_clips(project_id)

    def test_repeat_run_keeps_human_edits_and_reuses_previews_and_decisions(self):
        first = self.process()
        clips = self.clips(first["project_id"])
        self.assertEqual(len(clips), 3)
        first_renders = self.render.call_count
        with Database(self.cfg["database"]) as db:
            db.review_clip(clips[0]["id"], "approved", title="人工修改后标题")
        second = self.process()
        self.assertEqual(first["project_id"], second["project_id"])
        self.assertEqual(self.render.call_count, first_renders)
        self.assertEqual(self.select.call_count, 1)
        self.assertEqual(self.extract.call_count, 1)
        with Database(self.cfg["database"]) as db:
            edited = db.get_clip(clips[0]["id"])
            self.assertEqual((edited["status"], edited["title"]), ("approved", "人工修改后标题"))
            self.assertEqual(len(db.review_history(clips[0]["id"])), 1)

    def test_selector_failure_preserves_audio_and_transcript_for_retry(self):
        original = pipeline.select_clips._mock_wraps
        self.select.side_effect = RuntimeError("simulated provider outage")
        with self.assertRaisesRegex(RuntimeError, "provider outage"):
            self.process(skip_render=True)
        with Database(self.cfg["database"]) as db:
            failed = db.list_projects()[0]
        project_dir = Path(failed["project_dir"])
        self.assertEqual(failed["status"], "failed")
        self.assertTrue((project_dir / "transcript.json").is_file())
        self.assertFalse((project_dir / "decisions.json").exists())
        wav_state = (project_dir / "audio.wav").stat().st_mtime_ns
        self.select.side_effect = original
        recovered = self.process(skip_render=True)
        self.assertEqual(recovered["status"], "planned")
        self.assertEqual(self.extract.call_count, 1)
        self.assertEqual((project_dir / "audio.wav").stat().st_mtime_ns, wav_state)

    def test_failed_preview_is_retried_without_reencoding_successful_previews(self):
        failed_ids = []
        def sometimes(meta, clip, destination, cfg, **kwargs):
            if not failed_ids:
                failed_ids.append(clip["id"])
                raise RuntimeError("simulated encoder interruption")
            return self._render(meta, clip, destination, cfg, **kwargs)
        self.render.side_effect = sometimes
        first = self.process()
        self.assertEqual(first["status"], "preview_partial")
        self.assertEqual(len(first["errors"]), 1)
        self.render.side_effect = self._render
        second = self.process()
        self.assertEqual(second["status"], "ready")
        self.assertEqual(self.render.call_count, 4)
        self.assertEqual(self.select.call_count, 1)
        self.assertTrue(all(Path(c["preview_path"]).is_file() for c in self.clips(first["project_id"])))

    def test_selection_changes_keep_project_approvals_audio_and_budget_ledger(self):
        first = self.process(skip_render=True)
        clips = self.clips(first["project_id"])
        with Database(self.cfg["database"]) as db:
            db.review_clip(clips[0]["id"], "approved")
        ledger = Path(first["project_dir"]) / "selection" / "api_budget.json"
        ledger.parent.mkdir(exist_ok=True)
        ledger.write_text('{"attempts":[{"cost_usd":0.01,"status":"success"}]}', encoding="utf-8")
        old_ledger = ledger.read_bytes()
        self.cfg["selection"]["context_after"] = 2
        self.cfg["openai"]["model"] = "a-different-model"
        self.prompt.write_text("A new selection style; same source and project budget.", encoding="utf-8")
        second = self.process(skip_render=True)
        self.assertEqual(second["project_id"], first["project_id"])
        self.assertEqual(self.extract.call_count, 1)
        self.assertEqual(self.select.call_count, 2)
        self.assertEqual(ledger.read_bytes(), old_ledger)
        with Database(self.cfg["database"]) as db:
            self.assertEqual(db.get_clip(clips[0]["id"])["status"], "approved")
            self.assertEqual(len(db.list_projects()), 1)

    def test_distinct_viewpoints_with_identical_times_remain_distinct_candidates(self):
        base = {"start": 1, "end": 10, "category": "opinion", "title": "换人观点", "description": "说明",
                "reason": "原文支持", "score": 70, "tags": [], "evidence": ["这次换人非常关键。"]}
        self.select.side_effect = lambda *a, **k: [{**base, "id": "quote-one"},
                                                  {**base, "id": "quote-two", "title": "跑位观点", "evidence": ["他的跑位拉开了空间。"]}]
        result = self.process(skip_render=True)
        stored = self.clips(result["project_id"])
        self.assertEqual(len(stored), 2)
        self.assertEqual({c["title"] for c in stored}, {"换人观点", "跑位观点"})

    def test_export_only_approved_is_idempotent_and_keeps_both_orientations(self):
        project = self.process(skip_render=True)
        clips = self.clips(project["project_id"])
        with Database(self.cfg["database"]) as db:
            db.review_clip(clips[0]["id"], "approved")
            db.review_clip(clips[1]["id"], "rejected")
        first = pipeline.export_project(project["project_id"], self.cfg, ("horizontal", "vertical"))
        second = pipeline.export_project(project["project_id"], self.cfg, ("horizontal", "vertical"))
        self.assertEqual(first, second)
        self.assertEqual(self.render.call_count, 2)
        self.assertEqual(len(first["exports"]), 2)
        self.assertEqual(first["errors"], [])
        self.assertEqual({item["clip_id"] for item in first["exports"]}, {clips[0]["id"]})
        for item in first["exports"]:
            self.assertTrue(Path(item["video"]).is_file())
            self.assertTrue(Path(item["subtitles"]["srt"]).is_file())
            self.assertTrue(Path(item["video"]).with_name("publication.txt").is_file())

    def test_export_rejects_changed_source_before_render(self):
        project = self.process(skip_render=True)
        clip = self.clips(project["project_id"])[0]
        with Database(self.cfg["database"]) as db:
            db.review_clip(clip["id"], "approved")
        self.source.write_bytes(b"A different recording at the same file path.")
        with self.assertRaises(RuntimeError):
            pipeline.export_project(project["project_id"], self.cfg)
        self.assertEqual(self.render.call_count, 0)

    def test_export_uses_reviewed_title_everywhere_and_title_edit_reuses_render(self):
        project = self.process(skip_render=True)
        clip = self.clips(project["project_id"])[0]
        with Database(self.cfg["database"]) as db:
            db.review_clip(clip["id"], "approved", title="人工标题：曼城/换人 ⚽")
        first = pipeline.export_project(project["project_id"], self.cfg)["exports"][0]
        first_video = Path(first["video"])
        self.assertEqual(first_video.name, "人工标题：曼城_换人 ⚽.mp4")
        self.assertEqual(read_json(first_video.with_suffix(".timeline.json"))["output"], str(first_video))
        with Database(self.cfg["database"]) as db:
            db.review_clip(clip["id"], "approved", title="修改后的最终标题")
        second = pipeline.export_project(project["project_id"], self.cfg)["exports"][0]
        video = Path(second["video"])
        self.assertEqual(video.name, "修改后的最终标题.mp4")
        self.assertEqual(self.render.call_count, 1)  # title-only edit copies cached media
        self.assertTrue(first_video.is_file())  # retained publication history
        self.assertEqual(video.read_bytes(), first_video.read_bytes())
        self.assertEqual(read_json(video.with_suffix(".timeline.json"))["output"], str(video))
        self.assertEqual(read_json(video.with_name("metadata.json"))["video"], str(video))
        with Database(self.cfg["database"]) as db:
            current = db.get_clip(clip["id"])["exports"]["horizontal"]
        self.assertEqual(current["path"], str(video))
        self.assertEqual(current["metadata"]["title"], "修改后的最终标题")
        self.assertEqual(pipeline.export_project(project["project_id"], self.cfg)["exports"][0], second)

    def test_legacy_export_is_renamed_without_render_and_timeline_is_updated(self):
        project = self.process(skip_render=True)
        clip = self.clips(project["project_id"])[0]
        with Database(self.cfg["database"]) as db:
            db.review_clip(clip["id"], "approved", title="已审核的比赛观点")
        item = pipeline.export_project(project["project_id"], self.cfg)["exports"][0]
        named = Path(item["video"])
        legacy = named.with_name("video.mp4")
        named.rename(legacy)
        named.with_suffix(".timeline.json").rename(legacy.with_suffix(".timeline.json"))
        atomic_json(legacy.with_suffix(".timeline.json"), {"output": str(legacy), "start": clip["start"]})
        atomic_json(legacy.with_name("metadata.json"), {**item, "video": str(legacy)})
        recovered = pipeline.export_project(project["project_id"], self.cfg)["exports"][0]
        self.assertEqual(self.render.call_count, 1)
        self.assertEqual(recovered["video"], str(named))
        self.assertFalse(legacy.exists())
        self.assertFalse(legacy.with_suffix(".timeline.json").exists())
        self.assertEqual(read_json(named.with_suffix(".timeline.json"))["output"], str(named))

    def test_corrupt_decision_cache_rebuilds_without_resetting_budget(self):
        project = self.process(skip_render=True)
        directory = Path(project["project_dir"])
        ledger = directory / "selection" / "api_budget.json"
        ledger.write_text('{"attempts":[{"status":"success","cost_usd":0.2}]}', encoding="utf-8")
        original_ledger = ledger.read_bytes()
        decision = directory / "decisions.json"
        value = json.loads(decision.read_text(encoding="utf-8"))
        value["clips"][0]["end"] = 500
        decision.write_text(json.dumps(value), encoding="utf-8")
        recovered = self.process(skip_render=True)
        self.assertEqual(recovered["status"], "planned")
        self.assertEqual(self.select.call_count, 2)
        self.assertEqual(ledger.read_bytes(), original_ledger)
        self.assertEqual(len(list(directory.glob("decisions.json.corrupt-*"))), 1)
        self.assertEqual(self.extract.call_count, 1)

    def test_import_revision_preserves_approved_clip_transcript_and_resume_options(self):
        project = self.process(skip_render=True, allow_cpu=True)
        old_clip = self.clips(project["project_id"])[0]
        with Database(self.cfg["database"]) as db:
            db.review_clip(old_clip["id"], "approved", title="人工标题")
            saved_project = db.get_project(project["project_id"])
            options = saved_project["metadata"]["process_options"]
            self.assertEqual(options, {"path": str(self.source), "audio_track": 0, "mock": True,
                                       "transcript_path": str(self.transcript), "allow_cpu": True, "skip_render": True})
        original_text = pipeline.load_clip_transcript(saved_project, old_clip)
        value = json.loads(self.transcript.read_text(encoding="utf-8"))
        value["segments"][0]["text"] = "这一句已经由人工修订。"
        self.transcript.write_text(json.dumps(value), encoding="utf-8")
        updated = self.process(skip_render=True)
        self.assertEqual(updated["project_id"], project["project_id"])
        with Database(self.cfg["database"]) as db:
            saved_project = db.get_project(project["project_id"])
            old_clip = db.get_clip(old_clip["id"])
            self.assertEqual((old_clip["status"], old_clip["title"]), ("approved", "人工标题"))
            self.assertNotEqual(old_clip["transcript_key"], saved_project["metadata"]["transcript_key"])
        self.assertEqual(pipeline.load_clip_transcript(saved_project, old_clip), original_text)
        exported = pipeline.export_project(project["project_id"], self.cfg)
        self.assertEqual(len(exported["exports"]), 1)
        subtitles = Path(exported["exports"][0]["subtitles"]["srt"]).read_text(encoding="utf-8-sig")
        self.assertIn("这次换人非常关键", subtitles)
        self.assertNotIn("这一句已经由人工修订", subtitles)

    def test_audio_only_needs_neither_asr_nor_api(self):
        self.cfg["selection"]["mode"] = "audio"
        self.cfg["openai"]["api_key"] = ""
        self.prompt.unlink()  # Audio mode must not depend on a semantic prompt.
        events = [{"start": 10, "end": 12, "strength": 3, "kind": "energy_rise"}]
        with patch("core.pipeline.transcribe_audio", side_effect=AssertionError("audio-only must skip Whisper")), \
                patch("core.clip_selector.LLMClient.complete", side_effect=AssertionError("audio-only must skip API")), \
                patch("core.pipeline.analyze_audio", return_value=events):
            result = pipeline.process_video(self.source, self.cfg)
        self.assertEqual(result["status"], "ready")
        clips = self.clips(result["project_id"])
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["candidate"]["source"], "audio")
        self.assertIn("待人工确认", clips[0]["title"])
        self.assertEqual(self.render.call_count, 1)

    def test_hybrid_missing_key_fails_after_saving_resumable_transcript(self):
        self.cfg["selection"]["mode"] = "hybrid"
        self.cfg["openai"]["api_key"] = ""
        with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
            pipeline.process_video(self.source, self.cfg, transcript_path=self.transcript, skip_render=True)
        with Database(self.cfg["database"]) as db:
            project = db.list_projects()[0]
        self.assertEqual(project["status"], "failed")
        self.assertTrue((Path(project["project_dir"]) / "transcript.json").exists())
        self.assertEqual(project["metadata"]["process_options"]["transcript_path"], str(self.transcript))

    def test_manifest_excludes_concurrent_revocation_and_invalidates_later_edits(self):
        result = self.process(skip_render=True)
        project_id = result["project_id"]
        clips = self.clips(project_id)
        with Database(self.cfg["database"]) as db:
            db.review_clip(clips[0]["id"], "approved")
            db.review_clip(clips[1]["id"], "approved")
        def render_and_revoke(meta, clip, destination, cfg, **kwargs):
            if clip["id"] == clips[1]["id"]:
                with Database(self.cfg["database"]) as other:
                    other.review_clip(clips[0]["id"], "rejected")
            return self._render(meta, clip, destination, cfg, **kwargs)
        self.render.side_effect = render_and_revoke
        report = pipeline.export_project(project_id, self.cfg)
        self.assertEqual([item["clip_id"] for item in report["exports"]], [clips[1]["id"]])
        self.assertEqual(len(report["errors"]), 1)
        exported_video = Path(report["exports"][0]["video"])
        with Database(self.cfg["database"]) as db:
            self.assertEqual(db.get_clip(clips[0]["id"])["exports"], {})
            db.review_clip(clips[1]["id"], "approved", title="导出后人工修改标题")
        current = json.loads((Path(self.cfg["output_dir"]) / project_id / "export_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(current["exports"], [])
        self.assertEqual(current["invalidated"][0]["clip_id"], clips[1]["id"])
        self.assertTrue(exported_video.is_file())

    def test_real_asr_integer_word_bounds_do_not_look_like_snapshot_corruption(self):
        # Taken verbatim from the real tiny-Whisper acceptance run. Synthetic
        # mocks missed words clamped to integer chunk boundaries at 0/20/60.
        raw = read_json(Path(__file__).parent / "fixtures" / "asr_integer_word_bounds.json")
        key = fingerprint(raw)
        snapshot = self.root / "transcripts" / f"{key}.json"
        atomic_json(snapshot, raw)
        project = {"project_dir": str(self.root), "duration": 75.0, "metadata": {"transcript_key": key}}
        clip = {"transcript_key": key}
        normalized = pipeline.load_clip_transcript(project, clip)
        self.assertEqual(normalized["segments"], validate_transcript(raw, 75.0)["segments"])
        self.assertEqual(fingerprint(normalized), fingerprint(validate_transcript(normalized, 75.0)))
        self.assertEqual(fingerprint(read_json(snapshot)), key)  # validation never rewrites snapshot
        raw["segments"][0]["text"] = "unexpected modification"
        atomic_json(snapshot, raw)
        with self.assertRaisesRegex(ValueError, "快照已损坏"):
            pipeline.load_clip_transcript(project, clip)

    def test_legacy_approval_does_not_inherit_new_transcript_after_old_cache_corruption(self):
        result = self.process(skip_render=True)
        clip = self.clips(result["project_id"])[0]
        old_key = clip["transcript_key"]
        with Database(self.cfg["database"]) as db:
            db.review_clip(clip["id"], "approved")
            # Recreate a pre-snapshot project upgraded by schema migration.
            db.connection.execute("UPDATE clips SET transcript_key=NULL WHERE project_id=?", (result["project_id"],))
            db.connection.commit()
        directory = Path(result["project_dir"])
        (directory / "transcripts" / f"{old_key}.json").unlink()
        (directory / "transcript.json").write_text("{broken", encoding="utf-8")
        changed = read_json(self.transcript)
        changed["segments"][0]["text"] = "人工修订后的全新转录内容。"
        atomic_json(self.transcript, changed)
        self.process(skip_render=True)
        with Database(self.cfg["database"]) as db:
            project = db.get_project(result["project_id"])
            old_clip = db.get_clip(clip["id"])
        self.assertEqual(old_clip["status"], "approved")
        self.assertEqual(old_clip["transcript_key"], old_key)
        self.assertNotEqual(project["metadata"]["transcript_key"], old_key)
        # Fail clearly on the unavailable old snapshot, never export new words
        # under an approval made against different source text.
        with self.assertRaises(FileNotFoundError):
            pipeline.load_clip_transcript(project, old_clip)


class ConfigurationTests(unittest.TestCase):
    def test_environment_precedence_and_project_relative_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "settings.yaml").write_text("openai:\n  model: configured-model\noutput_dir: local-output\n", encoding="utf-8")
            (root / ".env").write_text('OPENAI_MODEL="dotenv-model"\nDEVICE=cpu\n', encoding="utf-8")
            with patch("core.config.ROOT", root), patch.dict(os.environ, {"OPENAI_MODEL": "environment-model", "OUTPUT_DIR": "relative-publish"}, clear=True):
                cfg = load_config()
            self.assertEqual(cfg["openai"]["model"], "environment-model")
            self.assertEqual(cfg["asr"]["device"], "cpu")
            self.assertEqual(Path(cfg["output_dir"]), root / "relative-publish")
            self.assertEqual(Path(cfg["database"]), root / "relative-publish" / "factory.sqlite3")

    def test_invalid_overlap_rejected_before_long_running_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "invalid.yaml"
            config.write_text("asr:\n  chunk_seconds: 10\n  overlap_seconds: 10\n", encoding="utf-8")
            with patch("core.config.ROOT", root), patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "overlap_seconds"):
                    load_config(config)


if __name__ == "__main__":
    unittest.main()
