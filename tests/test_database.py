"""Persistence tests exercise edits, recovery and export invalidation invariants."""

import math
import json
from pathlib import Path
import tempfile
import unittest

from storage.database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "review.sqlite3"
        self.db = Database(self.path)
        self.project = {"id": "project-1", "source": "D:/中文目录/a video.mp4", "name": "中文录像.mp4",
                        "duration": 300.0, "status": "ready", "metadata": {"audio_track": 1}}
        self.clip = {"id": "clip-1", "start": 10.0, "end": 40.0, "category": "tactical_analysis",
                     "title": "原始标题", "description": "说明", "reason": "完整观点", "score": 72, "tags": ["曼城"]}
        self.db.upsert_project(self.project)
        self.db.upsert_clips("project-1", [self.clip])

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_rerun_and_restart_preserve_edited_values_and_initial_candidate(self):
        self.db.review_clip("clip-1", "approved", title="人工标题", start=8, end=45)
        self.db.upsert_clips("project-1", [{**self.clip, "title": "模型新标题", "start": 11}])
        self.db.close()
        self.db = Database(self.path)
        result = self.db.get_clip("clip-1")
        self.assertEqual(result["title"], "人工标题")
        self.assertEqual(result["start"], 8)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["original"]["title"], "原始标题")
        self.assertEqual(result["candidate"]["title"], "模型新标题")
        self.assertEqual(len(self.db.list_clips("project-1")), 1)
        history = self.db.review_history("clip-1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["before"]["start"], 10)
        self.assertEqual(history[0]["after"]["title"], "人工标题")

    def test_invalid_values_do_not_write_audit_or_partial_edit(self):
        for start, end in [(math.nan, 20), (0, math.inf), (-1, 3), (3, 2), (10, 301), (True, 20)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.db.review_clip("clip-1", "approved", start=start, end=end)
        with self.assertRaises(ValueError):
            self.db.review_clip("clip-1", "unknown")
        self.assertEqual(self.db.get_clip("clip-1")["status"], "pending")
        self.assertEqual(self.db.review_history("clip-1"), [])

    def test_status_change_keeps_media_and_title_edit_invalidates_export_only(self):
        self.db.set_preview("clip-1", "preview.mp4", "fingerprint")
        self.db.review_clip("clip-1", "approved")
        self.db.set_export("clip-1", "horizontal", "final.mp4", {"title": "原始标题"})
        self.db.set_export("clip-1", "horizontal", "final.mp4", {"title": "原始标题"})
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM exports").fetchone()[0], 1)
        self.db.review_clip("clip-1", "approved", title="修改标题")
        clip = self.db.get_clip("clip-1")
        self.assertEqual(clip["preview_path"], "preview.mp4")
        self.assertEqual(clip["exports"], {})
        self.assertEqual(self.db.connection.execute("SELECT count(*) FROM exports").fetchone()[0], 1)
        self.db.review_clip("clip-1", "approved", end=42)
        self.assertIsNone(self.db.get_clip("clip-1")["preview_path"])

    def test_manual_subtitle_validation_and_reset(self):
        good = "1\n00:00:01,000 --> 00:00:04,000\n曼城本次换人很关键。\n"
        self.db.review_clip("clip-1", "pending", subtitle_text=good)
        self.db.set_preview("clip-1", "subtitled.mp4")
        with self.assertRaises(ValueError):
            self.db.review_clip("clip-1", "approved", end=12, subtitle_text=good)
        self.db.review_clip("clip-1", "approved", end=12, subtitle_text="")
        self.assertIsNone(self.db.get_clip("clip-1")["subtitle_text"])
        self.assertIsNone(self.db.get_clip("clip-1")["preview_path"])
        with self.assertRaises(ValueError):
            self.db.review_clip("clip-1", "approved", subtitle_text="not SRT")

    def test_time_edit_regenerates_manual_subtitles_when_no_replacement_given(self):
        good = "1\n00:00:01,000 --> 00:00:04,000\n曼城本次换人很关键。\n"
        self.db.review_clip("clip-1", "approved", subtitle_text=good)
        self.db.review_clip("clip-1", "approved", start=20, end=50)
        self.assertIsNone(self.db.get_clip("clip-1")["subtitle_text"])

    def test_stale_render_cannot_replace_current_media(self):
        original_revision = self.db.get_clip("clip-1")["revision"]
        self.db.review_clip("clip-1", "approved", start=9)
        with self.assertRaises(ValueError):
            self.db.set_preview("clip-1", "stale.mp4", expected_revision=original_revision)
        with self.assertRaises(ValueError):
            self.db.set_export("clip-1", "horizontal", "stale.mp4", expected_revision=original_revision)
        clip = self.db.get_clip("clip-1")
        self.assertIsNone(clip["preview_path"])
        self.assertEqual(clip["exports"], {})
        self.db.set_export("clip-1", "horizontal", "valid.mp4", expected_revision=clip["revision"])
        self.assertEqual(self.db.get_clip("clip-1")["exports"]["horizontal"]["path"], "valid.mp4")

    def test_exports_require_approval(self):
        with self.assertRaises(ValueError):
            self.db.set_export("clip-1", "horizontal", "pending.mp4")

    def test_counts_and_rates_distinguish_pending_from_rejected(self):
        self.db.upsert_clips("project-1", [{**self.clip, "id": "clip-2", "start": 50, "end": 70},
                                           {**self.clip, "id": "clip-3", "start": 90, "end": 100}])
        self.db.review_clip("clip-1", "approved")
        self.db.review_clip("clip-2", "rejected")
        project = self.db.list_projects()[0]
        self.assertEqual((project["candidate_count"], project["approved_count"], project["rejected_count"], project["pending_count"]), (3, 1, 1, 1))
        stats = self.db.stats()[0]
        self.assertAlmostEqual(stats["approval_rate"], 1 / 3)
        self.assertAlmostEqual(stats["reviewed_approval_rate"], 0.5)
        self.assertAlmostEqual(stats["average_duration"], 20)

    def test_id_collision_and_bad_candidate_batch_are_atomic(self):
        self.db.upsert_project({**self.project, "id": "project-2"})
        with self.assertRaises(ValueError):
            self.db.upsert_clips("project-2", [self.clip])
        with self.assertRaises(ValueError):
            self.db.upsert_clips("project-1", [{**self.clip, "id": "new-good"},
                                               {**self.clip, "id": "new-bad", "end": math.nan}])
        self.assertIsNone(self.db.get_clip("new-good"))
        self.db.review_clip("clip-1", "pending")
        self.assertEqual(self.db.review_history("clip-1"), [])

    def test_reselection_archives_only_superseded_pending_candidates(self):
        self.db.upsert_clips("project-1", [{**self.clip, "id": "reviewed"}, {**self.clip, "id": "rejected"}])
        self.db.review_clip("reviewed", "approved", title="人工保留")
        self.db.review_clip("rejected", "rejected")
        self.db.upsert_clips("project-1", [{**self.clip, "id": "replacement"}], replace_pending=True)
        self.assertEqual({c["id"] for c in self.db.list_clips("project-1")}, {"reviewed", "rejected", "replacement"})
        self.assertEqual(self.db.get_clip("clip-1")["active"], 0)
        self.assertEqual(self.db.get_clip("reviewed")["title"], "人工保留")
        self.assertEqual(len(self.db.review_history("reviewed")), 1)
        self.assertEqual(self.db.list_projects()[0]["candidate_count"], 3)
        self.assertEqual(self.db.stats()[0]["candidate_count"], 3)
        with self.assertRaises(ValueError):
            self.db.set_preview("clip-1", "archived.mp4")
        self.db.upsert_clips("project-1", [self.clip])
        self.assertEqual(self.db.get_clip("clip-1")["active"], 1)

    def test_old_version_manifest_is_invalidated_on_first_new_review(self):
        self.db.review_clip("clip-1", "approved")
        destination = self.path.parent / "project-1" / "clip-1" / "horizontal_legacy" / "video.mp4"
        destination.parent.mkdir(parents=True)
        self.db.set_export("clip-1", "horizontal", destination)
        manifest = destination.parents[2] / "export_manifest.json"
        manifest.write_text(json.dumps({"project_id": "project-1", "exports": [{"clip_id": "clip-1"}]}), encoding="utf-8")
        self.db.review_clip("clip-1", "rejected")
        updated = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(updated["exports"], [])
        self.assertEqual(updated["invalidated"][0]["clip_id"], "clip-1")


if __name__ == "__main__":
    unittest.main()
