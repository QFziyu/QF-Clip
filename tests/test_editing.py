"""Editorial plans, match clocks, cache recovery and long-timeline invariants."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.config import DEFAULTS
from core.editing_options import normalize_editing_options, match_seconds, locate_events
from core.editing import build_drafts, create_editing_plan, coverage_report, export_blockers
from core.utils import atomic_json, fingerprint
from storage.plans import PlanStore


class EditorialClockTests(unittest.TestCase):
    def test_regulation_stoppage_and_half_are_distinct(self):
        self.assertEqual(match_seconds("45+2:10", "first"), 2830)
        self.assertEqual(match_seconds("45+0:30", "first"), 2730)
        self.assertEqual(match_seconds("61:20", "second"), 3680)
        self.assertEqual(match_seconds("105+1:00", "extra_first"), 6360)
        for value, period in [("10:00", "second"), ("45+2:99", "first"), ("35+2", "first"), ("-1", "first")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                match_seconds(value, period)

    def test_anchor_can_be_mid_half_and_halves_use_separate_offsets(self):
        options = normalize_editing_options({"anchors": [
            {"period": "first", "match_time": "10:00", "video_time": "00:15:00"},
            {"period": "second", "match_time": "61:00", "video_time": "01:15:00"}],
            "events": [{"period": "first", "match_time": "5:00", "label": "前段事件"},
                       {"period": "second", "match_time": "50:00", "label": "下半场事件"}]})
        first, second = locate_events(options, 7200)
        self.assertEqual(first["start"], 580)
        self.assertEqual(second["start"], 3820)
        self.assertFalse(first["confirmed"])

    def test_spliced_recording_gaps_and_outside_material_are_explicit(self):
        options = normalize_editing_options({"anchors": [
            {"period": "first", "match_time": "00:00", "video_time": 0, "video_end": 300},
            {"period": "first", "match_time": "20:00", "video_time": 300}],
            "events": [{"period": "first", "match_time": "10:00", "label": "被删节"},
                       {"period": "first", "match_time": "21:00", "label": "新连续区间"},
                       {"period": "second", "match_time": "50:00", "label": "无校准"}]})
        events = locate_events(options, 1000)
        self.assertEqual(events[0]["status"], "outside")
        self.assertEqual(events[1]["start"], 340)
        self.assertEqual(events[2]["status"], "unresolved")

    def test_repeated_match_clock_is_ambiguous_not_guessed(self):
        options = normalize_editing_options({"anchors": [
            {"period": "first", "match_time": "00:00", "video_time": 0},
            {"period": "first", "match_time": "00:00", "video_time": 300}],
            "events": [{"period": "first", "match_time": "2:00", "label": "重复录像"}]})
        self.assertEqual(locate_events(options, 600)[0]["status"], "ambiguous")

    def test_options_revalidate_and_reject_secret_unknown_and_invalid_limits(self):
        value = normalize_editing_options({"events": [{"period": "first", "match_time": "35:00", "label": "进球"}]})
        self.assertEqual(normalize_editing_options(value), value)
        for invalid in [{"api_key": "secret"}, {"count": True}, {"content": "custom"},
                        {"min_seconds": 100, "max_seconds": 20}, {"target_seconds": float("nan")},
                        {"anchors": [{"period": "first", "match_time": "00:00", "video_time": 0, "key": "secret"}]}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_editing_options(invalid)

    def test_three_hour_continuous_plan_exceeds_old_short_limit_without_duplicates(self):
        transcript = {"segments": [{"start": i, "end": i + 20, "text": "讨论足球战术"} for i in range(0, 10800, 20)]}
        candidates = [{"start": 4800, "end": 4850, "title": "战术观点", "score": 80},
                      {"start": 4820, "end": 4870, "title": "重复观点", "score": 79},
                      {"start": 8200, "end": 8300, "title": "另一话题", "score": 70}]
        opts = normalize_editing_options({"format": "continuous", "count": 2, "target_seconds": 1200,
                                           "min_seconds": 600, "max_seconds": 1800})
        drafts, _ = build_drafts(candidates, [], opts, transcript, 10800)
        self.assertEqual(len(drafts), 2)
        self.assertTrue(all(len(d["segments"]) == 1 and d["segments"][0]["end"] - d["segments"][0]["start"] >= 1200 for d in drafts))
        self.assertLessEqual(drafts[0]["segments"][0]["end"], drafts[1]["segments"][0]["start"])

    def test_mandatory_intervals_survive_silence_length_and_count_conflicts(self):
        opts = normalize_editing_options({"format": "montage", "count": 1, "min_seconds": 10, "target_seconds": 15, "max_seconds": 20})
        events = [{"id": "e1", "label": "无解说关键事件", "start": 10, "end": 40, "confirmed": True, "waived": False}]
        drafts, warnings = build_drafts([], events, opts, {"segments": []}, 100)
        self.assertEqual(drafts[0]["segments"], [{"start": 10, "end": 40}])
        plan = {"options": opts, "events": events, "drafts": drafts}
        drafts[0]["status"] = "approved"
        self.assertTrue(export_blockers(plan))
        self.assertTrue(warnings)
        self.assertEqual(coverage_report(plan)[0]["coverage"], "covered")
        drafts[0]["segments"][0]["end"] = 30
        self.assertEqual(coverage_report(plan)[0]["coverage"], "missing")


class EditorialPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fcf-editing-")
        self.root = Path(self.temp.name)
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(database=str(self.root / "fixture.sqlite3"), output_dir=str(self.root / "output"),
                        cache_dir=str(self.root / "cache"), prompt_path=str(Path(__file__).resolve().parents[1] / "prompts/clip_selection.md"))
        self.cfg["selection"].update(context_before=0, context_after=0)
        self.cfg["openai"].update(api_key="fixture-secret", model="mock-model")
        self.transcript = {"segments": [{"start": float(i), "end": float(i + 20), "text": f"讨论足球观点 {i}"} for i in range(0, 1800, 30)]}
        self.key = fingerprint(self.transcript)
        self.project_dir = self.root / "cache/project"
        atomic_json(self.project_dir / "transcripts" / f"{self.key}.json", self.transcript)
        with PlanStore(self.cfg["database"]) as db:
            db.upsert_project({"id": "p", "source": str(self.root / "fixture.mp4"), "duration": 1800,
                               "project_dir": str(self.project_dir), "status": "ready",
                               "metadata": {"source": "fixture.mp4", "duration": 1800, "transcript_key": self.key}})
            db.upsert_clips("p", [{"id": "old", "start": 10, "end": 40, "score": 70, "title": "旧候选"}])
            db.review_clip("old", "approved", title="旧审批标题")

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_rerun_retains_review_and_template_omits_match_events(self):
        opts = {"format": "continuous", "content": "conversation", "count": 1}
        first = create_editing_plan("p", self.cfg, opts, mock=True)
        with PlanStore(self.cfg["database"]) as db:
            plan = db.get_plan(first["plan_id"])
            draft = plan["drafts"][0]
            db.edit_plan(plan["id"], 0, {draft["id"]: {"status": "approved", "title": "人工方案标题"}})
            with self.assertRaises(ValueError):
                db.edit_plan(plan["id"], 0, {draft["id"]: {"title": "覆盖并发修改"}})
            db.save_template("节目讨论", {**opts, "events": [{"period": "first", "match_time": "35:00", "label": "本场事件"}]})
        with patch("core.clip_selector.select_clips", side_effect=AssertionError("already cached plan")):
            repeated = create_editing_plan("p", self.cfg, opts, mock=True)
        self.assertEqual(repeated["plan_id"], first["plan_id"])
        with PlanStore(self.cfg["database"]) as db:
            self.assertEqual(db.get_plan(first["plan_id"])["drafts"][0]["title"], "人工方案标题")
            self.assertEqual(db.get_clip("old")["title"], "旧审批标题")
            self.assertEqual(len(db.review_history("old")), 1)
            self.assertEqual(db.list_templates()["节目讨论"]["events"], [])
            self.assertEqual(db.connection.execute("SELECT count(*) FROM plan_audit").fetchone()[0], 1)

    def test_different_prompt_and_format_create_independent_schemes_without_asr(self):
        with patch("core.transcriber.transcribe_audio", side_effect=AssertionError("must reuse transcript")):
            a = create_editing_plan("p", self.cfg, {"content": "conversation"}, mock=True)
            b = create_editing_plan("p", self.cfg, {"content": "custom", "prompt": "重点战术", "format": "montage"}, mock=True)
        self.assertNotEqual(a["plan_id"], b["plan_id"])
        with PlanStore(self.cfg["database"]) as db:
            self.assertEqual(len(db.list_plans("p")), 2)

    def test_legacy_normalized_transcript_identity_recovers_only_matching_cache(self):
        from core.editing import load_plan_transcript
        from core.pipeline import load_clip_transcript
        with PlanStore(self.cfg["database"]) as db:
            project = db.get_project("p")
        validated = load_clip_transcript(project, {"transcript_key": self.key})
        normalized_key = fingerprint(validated)
        self.assertEqual(load_plan_transcript(project, normalized_key), validated)
        with self.assertRaises(ValueError):
            load_plan_transcript(project, "missing-unrelated-snapshot")

    def test_all_nine_content_format_combinations_reuse_transcript_and_keep_index(self):
        identities = set()
        with patch("core.transcriber.transcribe_audio", side_effect=AssertionError("no repeated ASR")):
            for content in ("match", "conversation", "custom"):
                for form in ("shorts", "continuous", "montage"):
                    with self.subTest(content=content, form=form):
                        result = create_editing_plan("p", self.cfg, {"content": content, "format": form,
                            "prompt": "重点完整战术观点", "count": 1, "min_seconds": 10,
                            "target_seconds": 60, "max_seconds": 600}, mock=True)
                        identities.add(result["plan_id"])
                        with PlanStore(self.cfg["database"]) as db:
                            plan = db.get_plan(result["plan_id"])
                            self.assertTrue(plan["drafts"])
                            self.assertTrue(Path(plan["index_path"]).is_file())
                            self.assertEqual(plan["options"]["content"], content)
                            self.assertEqual(plan["options"]["format"], form)
                            if form != "montage":
                                self.assertTrue(all(len(d["segments"]) == 1 for d in plan["drafts"]))
        self.assertEqual(len(identities), 9)

    def test_failed_window_resumes_finished_windows_and_uses_shared_budget(self):
        calls, ledgers = [], []
        from core.llm_client import LLMClient
        real_init = LLMClient.__init__
        def init(instance, options, ledger_path):
            ledgers.append(str(ledger_path))
            real_init(instance, options, ledger_path)
        def complete(messages, schema, validator):
            payload = json.loads(messages[1]["content"])
            calls.append(payload["window_start"])
            if len(calls) == 2:
                raise RuntimeError("simulated provider interruption")
            segment = payload["segments"][0]
            return {"clips": [{"start": segment["start"], "end": segment["end"], "category": "opinion", "title": "有原文观点",
                               "description": "测试", "reason": "测试", "score": 70, "tags": [], "evidence": [segment["text"]]}]}
        with patch.object(LLMClient, "__init__", init), patch.object(LLMClient, "complete", side_effect=complete):
            with self.assertRaises(RuntimeError):
                create_editing_plan("p", self.cfg, {"content": "conversation"})
            create_editing_plan("p", self.cfg, {"content": "conversation"})
        self.assertEqual(calls.count(0), 1)
        self.assertTrue(all(p == str(self.project_dir / "selection/api_budget.json") for p in ledgers))

    def test_queue_snapshot_dispatch_and_rejects_nested_secret(self):
        from core.jobs import submit_job, get_job, _execute
        options = {"content": "conversation", "format": "continuous"}
        with patch("core.jobs.ensure_worker"):
            job = submit_job("plan", {"project_id": "p", "options": options, "mock": True}, self.cfg)
            options["content"] = "match"
            saved = get_job(job["id"], self.cfg)
            self.assertEqual(saved["payload"]["options"]["content"], "conversation")
            with self.assertRaises(ValueError):
                submit_job("plan", {"project_id": "p", "options": {"api_key": "secret"}}, self.cfg)
        with patch("core.editing.create_editing_plan", return_value={"plan_id": "stub"}) as create:
            _execute("plan", saved["payload"], self.cfg)
        self.assertEqual(create.call_args.args[2]["content"], "conversation")
        self.assertNotIn("fixture-secret", json.dumps(saved))

    def test_event_confirmation_waiver_and_edit_validation(self):
        options = {"format": "montage", "count": 1, "min_seconds": 1, "target_seconds": 100, "max_seconds": 1000,
                   "events": [{"period": "first", "match_time": "5:00", "label": "事件"}],
                   "anchors": [{"period": "first", "match_time": "0:00", "video_time": 0}]}
        result = create_editing_plan("p", self.cfg, options, mock=True)
        with PlanStore(self.cfg["database"]) as db:
            plan = db.get_plan(result["plan_id"])
            self.assertTrue(export_blockers(plan))
            with self.assertRaises(ValueError):
                db.edit_plan(plan["id"], 0, event_edits={"e1": {"start": -1}})
            draft = plan["drafts"][0]
            plan = db.edit_plan(plan["id"], 0, {draft["id"]: {"status": "approved"}}, {"e1": {"confirmed": True}})
            self.assertEqual(export_blockers(plan), [])
            plan = db.edit_plan(plan["id"], 1, event_edits={"e1": {"start": 1700, "end": 1710}})
            self.assertTrue(export_blockers(plan))
            plan = db.edit_plan(plan["id"], 2, event_edits={"e1": {"waived": True}})
            self.assertEqual(export_blockers(plan), [])


if __name__ == "__main__":
    unittest.main()
