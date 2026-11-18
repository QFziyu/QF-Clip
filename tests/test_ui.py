"""Streamlit workflow smoke tests; no API, Whisper model or video needed."""

import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from storage.database import Database

STREAMLIT_AVAILABLE = importlib.util.find_spec("streamlit") is not None
APP = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "legacy_ui.py"


@unittest.skipUnless(STREAMLIT_AVAILABLE, "Streamlit is not installed")
class ReviewUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "ui.sqlite3"
        self.source_path = Path(self.temp.name) / "测试解说.mp4"
        self.env = patch.dict(os.environ, {"DATABASE_PATH": str(self.database_path)})
        self.env.start()
        self.worker = patch("core.jobs.ensure_worker", return_value=False)
        self.worker.start()

    def tearDown(self):
        self.worker.stop()
        self.env.stop()
        self.temp.cleanup()

    def seed(self, source_exists=True):
        if source_exists:
            self.source_path.touch()
        with Database(self.database_path) as db:
            db.upsert_project({"id": "ui-project", "name": "测试解说.mp4", "source": str(self.source_path),
                               "duration": 180, "status": "ready"})
            db.upsert_clips("ui-project", [{"id": f"ui-clip-{i}", "start": 10 + 40 * i, "end": 35 + 40 * i,
                                           "title": f"候选 {i + 1}", "category": "tactical_analysis", "score": 75,
                                           "reason": "测试观点完整", "description": "模拟测试候选", "tags": ["曼城"]}
                                          for i in range(3)])

    def app(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_file(str(APP), default_timeout=15).run()

    @staticmethod
    def button(app, label):
        return next(item for item in app.button if item.label == label)

    def assertHealthy(self, app):
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertEqual(len(app.error), 0, str(app.error))

    def test_empty_projects_and_stats_are_actionable(self):
        app = self.app()
        self.assertHealthy(app)
        self.assertEqual(app.title[0].value, "项目")
        self.assertTrue(any("支持 MP4" in item.value for item in app.info))
        app.sidebar.radio[0].set_value("数据统计").run()
        self.assertHealthy(app)
        self.assertTrue(any("第一场录像" in item.value for item in app.info))

    def test_three_review_decisions_persist_and_auto_advance(self):
        self.seed()
        app = self.app()
        self.assertHealthy(app)
        app.sidebar.radio[0].set_value("审批工作台").run()
        self.assertHealthy(app)
        app.text_input[0].input("经过人工修正的标题")
        self.button(app, "通过").click().run()
        self.assertHealthy(app)
        self.assertEqual(app.text_input[0].value, "候选 2")
        self.button(app, "拒绝").click().run()
        self.assertHealthy(app)
        self.assertEqual(app.text_input[0].value, "候选 3")
        self.button(app, "通过").click().run()
        self.assertHealthy(app)
        with Database(self.database_path) as db:
            clips = db.list_clips("ui-project")
            self.assertEqual([c["status"] for c in clips], ["approved", "rejected", "approved"])
            self.assertEqual(clips[0]["title"], "经过人工修正的标题")
        app.sidebar.radio[0].set_value("数据统计").run()
        self.assertHealthy(app)
        self.assertEqual(len(app.dataframe), 1)
        restarted = self.app()
        self.assertHealthy(restarted)
        self.assertTrue(any(metric.label == "已通过" and metric.value == "2" for metric in restarted.metric))

    def test_invalid_range_shows_error_and_keeps_review_pending(self):
        self.seed()
        app = self.app()
        app.sidebar.radio[0].set_value("审批工作台").run()
        app.number_input[0].set_value(100.0)
        self.button(app, "通过").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("无法保存" in item.value for item in app.error))
        with Database(self.database_path) as db:
            self.assertEqual(db.get_clip("ui-clip-0")["status"], "pending")
            self.assertEqual(db.get_clip("ui-clip-0")["start"], 10)

    def test_import_submits_background_job_without_claiming_completion(self):
        app = self.app()
        app.text_input[0].input("D:/videos/test.mp4")
        with patch("core.jobs.submit_job", return_value={"id": "job12345", "status": "queued"}) as submit:
            self.button(app, "开始处理").click().run()
        self.assertHealthy(app)
        self.assertEqual(submit.call_args.args[0], "process")
        self.assertTrue(any("任务已加入队列" in item.value for item in app.success))
        self.assertFalse(any("处理完成" in item.value for item in app.success))

    def test_export_queues_only_approved_work(self):
        self.seed()
        with Database(self.database_path) as db:
            db.review_clip("ui-clip-0", "approved")
        app = self.app()
        with patch("core.jobs.submit_job", return_value={"id": "job12345", "status": "queued"}) as submit:
            self.button(app, "导出已通过的切片").click().run()
        self.assertHealthy(app)
        self.assertTrue(any("导出任务已加入队列" in item.value for item in app.success))
        self.assertEqual(submit.call_args.args[0], "export")
        payload = submit.call_args.args[1]
        self.assertEqual(payload["project_id"], "ui-project")
        self.assertEqual(payload["orientations"], ["horizontal"])
        self.assertEqual(payload["export_options"]["subtitle"]["preset"], "classic")
        self.assertFalse(payload["export_options"]["intro"]["enabled"])

    def test_export_style_and_intro_preferences_persist_and_queue_snapshot(self):
        self.seed()
        with Database(self.database_path) as db:
            db.review_clip("ui-clip-0", "approved")
        intro = Path(self.temp.name) / "我的片头.mp4"
        intro.write_bytes(b"intro fixture")
        app = self.app()
        next(item for item in app.selectbox if item.label == "字幕样式").set_value("gold").run()
        self.assertHealthy(app)
        next(item for item in app.color_picker if item.label == "文字颜色").set_value("#FF8811")
        next(item for item in app.number_input if item.label == "横屏字号").set_value(56)
        next(item for item in app.checkbox if item.label == "给本批视频添加片头").check().run()
        next(item for item in app.text_input if item.label == "片头文件路径").input(str(intro))
        self.button(app, "保存导出设置").click().run()
        self.assertHealthy(app)
        with Database(self.database_path) as db:
            saved = db.get_export_options("ui-project")
        self.assertEqual(saved["subtitle"]["primary_color"], "#FF8811")
        self.assertEqual(saved["subtitle"]["font_size"], 56)
        self.assertEqual(saved["intro"], {"enabled": True, "path": str(intro.resolve())})
        restarted = self.app()
        self.assertHealthy(restarted)
        self.assertEqual(next(item for item in restarted.selectbox if item.label == "字幕样式").value, "gold")
        self.assertEqual(next(item for item in restarted.color_picker if item.label == "文字颜色").value, "#FF8811")
        with patch("core.jobs.submit_job", return_value={"id": "job12345", "status": "queued"}) as submit:
            self.button(restarted, "导出已通过的切片").click().run()
        self.assertHealthy(restarted)
        self.assertEqual(submit.call_args.args[1]["export_options"], saved)

    def test_missing_intro_prevents_queue_and_preserves_saved_options(self):
        self.seed()
        with Database(self.database_path) as db:
            db.review_clip("ui-clip-0", "approved")
        app = self.app()
        next(item for item in app.checkbox if item.label == "给本批视频添加片头").check().run()
        next(item for item in app.text_input if item.label == "片头文件路径").input(str(Path(self.temp.name) / "missing.mp4")).run()
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertTrue(self.button(app, "导出已通过的切片").disabled)
        self.assertTrue(any("片头视频" in item.value for item in app.warning))
        with patch("core.jobs.submit_job") as submit:
            self.button(app, "保存导出设置").click().run()
            submit.assert_not_called()
        self.assertTrue(any("无法保存" in item.value for item in app.error))
        with Database(self.database_path) as db:
            self.assertEqual(db.get_export_options("ui-project"), {})

    def test_each_art_subtitle_preset_changes_controls_without_ui_errors(self):
        self.seed()
        from core.subtitle_styles import SUBTITLE_PRESETS
        app = self.app()
        for preset_id, preset in SUBTITLE_PRESETS.items():
            next(item for item in app.selectbox if item.label == "字幕样式").set_value(preset_id).run()
            self.assertHealthy(app)
            self.assertEqual(next(item for item in app.color_picker if item.label == "文字颜色").value,
                             preset["primary_color"])
            self.assertTrue(any("进球了" in item.value for item in app.markdown))
        self.button(app, "保存导出设置").click().run()
        self.assertHealthy(app)
        with Database(self.database_path) as db:
            self.assertEqual(db.get_export_options("ui-project")["subtitle"]["border_style"], 3)

    def test_uploaded_intro_is_streamed_and_deduplicated_under_output_assets(self):
        from ui.app import _save_intro_upload

        class Upload(io.BytesIO):
            def read(self, size=-1):
                self.asserted_sizes.append(size)
                return super().read(size)

        content = b"video fixture" * 100000
        upload = Upload(content)
        upload.name = "../../我的片头.mp4"
        upload.size = len(content)
        upload.asserted_sizes = []
        cfg = {"output_dir": self.temp.name}
        asset = Path(_save_intro_upload(upload, cfg))
        self.assertEqual(asset.read_bytes(), content)
        self.assertEqual(asset.parent, Path(self.temp.name) / "assets" / "intros")
        self.assertEqual(_save_intro_upload(upload, cfg), str(asset))
        self.assertEqual(len(list(asset.parent.iterdir())), 1)
        self.assertTrue(all(size == 1024 * 1024 for size in upload.asserted_sizes))
        upload.size = 201 * 1024 * 1024
        with self.assertRaisesRegex(ValueError, "200 MB"):
            _save_intro_upload(upload, cfg)

    def test_interrupted_intro_upload_cleans_only_its_temp_and_can_retry(self):
        from ui.app import _save_intro_upload

        class InterruptedUpload(io.BytesIO):
            passes = 0
            interrupt = True

            def seek(self, offset, whence=0):
                if offset == 0 and whence == 0:
                    self.passes += 1
                return super().seek(offset, whence)

            def read(self, size=-1):
                if self.interrupt and self.passes == 2 and self.tell() > 0:
                    raise OSError("upload interrupted")
                return super().read(size)

        content = b"intro fixture" * 100000
        upload = InterruptedUpload(content)
        upload.name, upload.size = "片头.mp4", len(content)
        cfg = {"output_dir": self.temp.name}
        directory = Path(self.temp.name) / "assets" / "intros"
        directory.mkdir(parents=True)
        other_upload = directory / ".intro-another-session.tmp"
        other_upload.write_bytes(b"another upload in progress")
        with self.assertRaisesRegex(OSError, "upload interrupted"):
            _save_intro_upload(upload, cfg)
        self.assertEqual(list(directory.iterdir()), [other_upload])
        upload.interrupt = False
        asset = Path(_save_intro_upload(upload, cfg))
        self.assertEqual(asset.read_bytes(), content)
        self.assertTrue(other_upload.is_file())
        # A leftover target from an older, non-atomic uploader is not reused.
        asset.write_bytes(b"truncated")
        self.assertEqual(_save_intro_upload(upload, cfg), str(asset))
        self.assertEqual(asset.read_bytes(), content)

    def test_missing_source_disables_rendering_but_keeps_review_editable(self):
        self.seed(source_exists=False)
        with Database(self.database_path) as db:
            db.review_clip("ui-clip-0", "approved")
        app = self.app()
        self.assertHealthy(app)
        self.assertTrue(any("原片已移除" in item.value for item in app.info))
        self.assertTrue(self.button(app, "导出已通过的切片").disabled)
        app.sidebar.radio[0].set_value("审批工作台").run()
        self.assertHealthy(app)
        self.assertTrue(self.button(app, "生成 / 刷新此条预览").disabled)
        self.assertTrue(any("无法重建" in item.value for item in app.info))
        app.text_input[0].input("原片清理后仍可修改标题")
        with patch("core.jobs.submit_job") as submit:
            self.button(app, "通过").click().run()
            submit.assert_not_called()
        self.assertHealthy(app)
        with Database(self.database_path) as db:
            self.assertEqual(db.get_clip("ui-clip-1")["title"], "原片清理后仍可修改标题")

    def test_missing_preview_plays_matching_export_and_reads_its_subtitles(self):
        self.seed(source_exists=False)
        video = Path(self.temp.name) / "已修改的标题.mp4"
        video.write_bytes(b"export fixture")
        subtitle = "1\n00:00:00,000 --> 00:00:03,000\n成品字幕\n"
        video.with_name("subtitles.srt").write_text(subtitle, encoding="utf-8")
        with Database(self.database_path) as db:
            db.review_clip("ui-clip-0", "approved")
            db.set_preview("ui-clip-0", Path(self.temp.name) / "missing-preview.mp4")
            db.set_export("ui-clip-0", "horizontal", video)
        app = self.app()
        app.sidebar.radio[0].set_value("审批工作台").run()
        next(r for r in app.radio if r.label == "筛选状态").set_value("approved").run()
        self.assertHealthy(app)
        self.assertEqual(len(app.get("video")), 1)
        self.assertTrue(any("当前审批版本一致" in item.value for item in app.caption))
        self.assertEqual(app.text_area[0].value, subtitle)
        self.assertTrue(self.button(app, "生成 / 刷新此条预览").disabled)

    def test_stale_export_is_not_played_after_time_range_edit(self):
        self.seed(source_exists=False)
        video = Path(self.temp.name) / "历史成品.mp4"
        video.write_bytes(b"stale export fixture")
        with Database(self.database_path) as db:
            db.review_clip("ui-clip-0", "approved")
            db.set_export("ui-clip-0", "horizontal", video)
            db.review_clip("ui-clip-0", "approved", start=11)
        app = self.app()
        app.sidebar.radio[0].set_value("审批工作台").run()
        next(r for r in app.radio if r.label == "筛选状态").set_value("approved").run()
        self.assertHealthy(app)
        self.assertEqual(len(app.get("video")), 0)
        self.assertTrue(any("无法重建" in item.value for item in app.info))
        self.assertTrue(video.is_file())

    def test_existing_preview_remains_playable_without_source(self):
        self.seed(source_exists=False)
        preview = Path(self.temp.name) / "preview.mp4"
        preview.write_bytes(b"preview fixture")
        with Database(self.database_path) as db:
            db.set_preview("ui-clip-0", preview)
        app = self.app()
        app.sidebar.radio[0].set_value("审批工作台").run()
        self.assertHealthy(app)
        self.assertEqual(len(app.get("video")), 1)
        self.assertFalse(any("无法重建" in item.value for item in app.info))

    def test_mock_import_requires_and_forwards_transcript(self):
        app = self.app()
        app.text_input[0].input('"D:/中文/测试.mp4"')
        next(item for item in app.checkbox if item.label == "模拟模式（仅测试流程）").check()
        with patch("core.jobs.submit_job", return_value={"id": "job12345", "status": "queued"}) as process:
            self.button(app, "开始处理").click().run()
            self.assertTrue(any("模拟模式需要已有转录 JSON" in item.value for item in app.error))
            process.assert_not_called()
            app.text_input[1].input('"D:/中文/转录.json"')
            self.button(app, "开始处理").click().run()
            self.assertHealthy(app)
            self.assertEqual(process.call_args.args[0], "process")
            self.assertEqual(process.call_args.args[1]["path"], "D:/中文/测试.mp4")
            self.assertEqual(process.call_args.args[1]["transcript_path"], "D:/中文/转录.json")
            self.assertTrue(process.call_args.args[1]["mock"])

    def test_queue_partial_failure_shows_result_and_retry(self):
        from core.jobs import submit_job, run_worker
        from core.config import load_config
        cfg = load_config()
        job = submit_job("export", {"project_id": "ui-project"}, cfg)
        result = {"exports": [{"video": "completed.mp4"}], "errors": [{"error": "FFmpeg fixture error"}]}
        with patch("core.jobs._execute", return_value=result):
            run_worker(cfg)
        app = self.app()
        app.sidebar.radio[0].set_value("任务队列").run()
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertTrue(any("失败项" in item.value for item in app.error))
        self.assertEqual(len(app.success), 0)
        self.assertTrue(any(button.label == "重试此任务" for button in app.button))
        self.assertTrue(any("任务" in code.value for code in app.code))
        self.button(app, "重试此任务").click().run()
        self.assertHealthy(app)
        from core.jobs import get_job
        self.assertEqual(get_job(job["id"], cfg)["status"], "queued")

    def test_empty_queue_and_missing_video_are_actionable(self):
        app = self.app()
        app.sidebar.radio[0].set_value("任务队列").run()
        self.assertHealthy(app)
        self.assertTrue(any("暂无任务" in item.value for item in app.info))
        app.sidebar.radio[0].set_value("项目").run()
        app.text_input[0].input("D:/file-that-does-not-exist.mp4")
        self.button(app, "开始处理").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("找不到文件" in item.value for item in app.error))


if __name__ == "__main__":
    unittest.main()
