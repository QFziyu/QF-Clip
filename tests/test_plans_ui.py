"""New plan UI uses isolated SQLite and synthetic text, no user approval edits."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import load_config
from core.editing import create_editing_plan
from core.utils import atomic_json, fingerprint
from storage.plans import PlanStore


class PlanUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fcf-ui-plan-")
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"DATABASE_PATH": str(self.root / "db.sqlite3")})
        self.env.start()
        self.worker = patch("core.jobs.ensure_worker", return_value=False)
        self.worker.start()
        cfg = load_config()
        cfg['selection'].update(context_before=0, context_after=0)
        source = self.root / "fixture.mp4"
        source.touch()
        transcript = {"segments": [{"start": 10.0, "end": 40.0, "text": "节目里讨论足球新闻和球员表现。"},
                                   {"start": 100.0, "end": 130.0, "text": "第二段讨论教练换人。"}]}
        key = fingerprint(transcript)
        project_dir = self.root / "cache"
        atomic_json(project_dir / "transcripts" / f"{key}.json", transcript)
        with PlanStore(cfg["database"]) as db:
            db.upsert_project({"id": "p", "name": "fixture.mp4", "source": str(source), "duration": 1200,
                               "project_dir": str(project_dir), "status": "ready",
                               "metadata": {"source": str(source), "duration": 1200, "transcript_key": key}})
        created = create_editing_plan("p", cfg, {"content": "conversation", "format": "continuous", "count": 1}, mock=True)
        self.plan_id = created["plan_id"]

    def tearDown(self):
        self.worker.stop()
        self.env.stop()
        self.temp.cleanup()

    def app(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "tests/fixtures/legacy_ui.py"), default_timeout=15).run()
        app.sidebar.radio[0].set_value("剪辑方案").run()
        return app

    @staticmethod
    def item(items, label):
        return next(x for x in items if x.label == label)

    def healthy(self, app):
        self.assertEqual(len(app.exception), 0, str(app.exception))
        self.assertEqual(len(app.error), 0, str(app.error))

    def test_form_modes_change_defaults_and_custom_prompt_validation(self):
        app = self.app()
        self.healthy(app)
        self.item(app.selectbox, "成品形式").set_value("continuous").run()
        self.healthy(app)
        self.assertEqual(self.item(app.number_input, "目标条数").value, 2)
        self.assertEqual(self.item(app.number_input, "每条目标时长（秒）").value, 600)
        self.item(app.selectbox, "内容标准").set_value("custom").run()
        self.assertTrue(self.item(app.button, "生成独立方案").disabled)
        self.item(app.text_area, "自定义筛选提示词").input("重点关注战术观点").run()
        self.assertFalse(self.item(app.button, "生成独立方案").disabled)
        with patch("ui.plans_page.submit_job", return_value={"id": "test-job"}) as submit:
            self.item(app.button, "生成独立方案").click().run()
        self.healthy(app)
        self.assertEqual(submit.call_args.args[1]["options"]["content"], "custom")
        self.assertEqual(submit.call_args.args[1]["options"]["format"], "continuous")

    def test_review_persists_and_export_uses_snapshot(self):
        app = self.app()
        self.healthy(app)
        self.item(app.text_input, "草稿标题").input("修改后的长段标题")
        self.item(app.selectbox, "草稿审批").set_value("approved")
        self.item(app.button, "保存草稿审核").click().run()
        self.healthy(app)
        with PlanStore(self.root / "db.sqlite3") as store:
            draft = store.get_plan(self.plan_id)["drafts"][0]
        self.assertEqual(draft["status"], "approved")
        self.assertEqual(draft["title"], "修改后的长段标题")
        with patch("ui.plans_page.submit_job", return_value={"id": "export-job"}) as submit:
            self.item(app.button, "导出方案成品").click().run()
        self.healthy(app)
        self.assertEqual(submit.call_args.args[0], "plan_export")
        self.assertIn("export_options", submit.call_args.args[1])
        restarted = self.app()
        self.assertEqual(self.item(restarted.text_input, "草稿标题").value, "修改后的长段标题")

    def test_import_can_choose_independent_plan_without_legacy_selection(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "tests/fixtures/legacy_ui.py"), default_timeout=15).run()
        self.item(app.checkbox, "使用独立剪辑方案（比赛 / 对话 / 自定义；短片 / 长段 / 集锦）").check().run()
        self.healthy(app)
        self.item(app.selectbox, "成品形式").set_value("montage").run()
        self.item(app.text_input, "录像路径").input(str(self.root / "fixture.mp4"))
        with patch("core.jobs.submit_job", return_value={"id": "process-job"}) as submit:
            self.item(app.button, "开始处理").click().run()
        self.healthy(app)
        self.assertEqual(submit.call_args.args[1]["editing_options"]["format"], "montage")

    def montage_app(self):
        cfg = load_config()
        result = create_editing_plan("p", cfg, {"content": "custom", "prompt": "重点讨论", "format": "montage",
            "count": 1, "min_seconds": 1, "target_seconds": 60, "max_seconds": 1200}, mock=True)
        self.plan_id = result["plan_id"]
        with PlanStore(cfg["database"]) as store:
            plan = store.get_plan(self.plan_id)
            self.draft_id = plan["drafts"][0]["id"]
            store.edit_plan(plan["id"], plan["revision"], {self.draft_id: {"segments": [
                {"start": 10, "end": 40}, {"start": 100, "end": 130}]}})
        app = self.app()
        self.item(app.selectbox, "选择剪辑方案").set_value(self.plan_id).run()
        return app

    def test_custom_montage_edit_loads_relative_captions_and_persists_after_restart(self):
        from core.subtitle import parse_srt
        app = self.montage_app()
        self.item(app.button, "编辑字幕").click().run()
        self.healthy(app)
        editor = self.item(app.text_area, "字幕（SRT，可直接修改文字和时间）")
        cues = parse_srt(editor.value, 60)
        self.assertEqual(cues[0]["start"], 0)
        self.assertEqual(cues[-1]["start"], 30)
        self.assertIn("第二段", editor.value)
        changed = editor.value.replace("节目里", "人工修改")
        editor.input(changed)
        self.item(app.selectbox, "草稿审批").set_value("approved")
        self.item(app.button, "保存草稿审核").click().run()
        self.healthy(app)
        with PlanStore(self.root / "db.sqlite3") as store:
            draft = store.get_plan(self.plan_id)["drafts"][0]
        self.assertEqual(draft["subtitle_text"], changed)
        restarted = self.app()
        self.item(restarted.selectbox, "选择剪辑方案").set_value(self.plan_id).run()
        self.assertEqual(self.item(restarted.text_area, "字幕（SRT，可直接修改文字和时间）").value, changed)
        self.assertFalse(self.item(restarted.checkbox, "使用自动字幕").value)

    def test_unchecking_automatic_also_prefills_without_original_or_asr(self):
        app = self.montage_app()
        (self.root / "fixture.mp4").unlink()
        with patch("core.transcriber.transcribe_audio", side_effect=AssertionError("must not re-transcribe")):
            self.item(app.checkbox, "使用自动字幕").uncheck().run()
        self.healthy(app)
        self.assertIn("第二段", self.item(app.text_area, "字幕（SRT，可直接修改文字和时间）").value)

    def test_srt_import_validates_and_keeps_buffer_on_invalid_upload(self):
        from ui.plans_page import _import_plan_srt
        from io import BytesIO
        valid = "1\n00:00:01,000 --> 00:00:02,000\n完全自定义的字幕\n"
        state = {"ksrt-upload": BytesIO(valid.encode("gb18030")), "ksrt": "原字幕"}
        with patch("ui.plans_page.st.session_state", state):
            _import_plan_srt("k", [{"start": 10, "end": 20}])
            self.assertEqual(state["ksrt"], valid)
            state["ksrt-upload"] = BytesIO(b"invalid srt")
            _import_plan_srt("k", [{"start": 10, "end": 20}])
        self.assertEqual(state["ksrt"], valid)
        self.assertIn("原编辑内容已保留", state["ksubtitle-error"])

    def test_changed_montage_ranges_require_caption_refresh(self):
        app = self.montage_app()
        self.item(app.button, "编辑字幕").click().run()
        with PlanStore(self.root / "db.sqlite3") as store:
            plan = store.get_plan(self.plan_id)
        key = f"review-plan-{self.plan_id}-{plan['revision']}-{self.draft_id}"
        # AppTest cannot interact with data_editor cells. Supply the edited
        # return value consistently across reruns, as the browser frontend does.
        from ui.plans_page import st
        original = st.data_editor
        def edited(data, *args, **kwargs):
            value = original(data, *args, **kwargs)
            if kwargs.get("key") == key + "segments":
                return [{"start": 10.0, "end": 40.0}, {"start": 105.0, "end": 130.0}]
            return value
        with patch("ui.plans_page.st.data_editor", side_effect=edited):
            app.run()
            self.assertTrue(self.item(app.button, "保存草稿审核").disabled)
            self.item(app.button, "按当前分段重新生成字幕").click().run()
        self.healthy(app)
        self.assertFalse(self.item(app.button, "保存草稿审核").disabled)


if __name__ == "__main__":
    unittest.main()
