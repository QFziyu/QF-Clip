"""Existing video migration preserves reviews, bytes, references and recovery."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.config import DEFAULTS
from core.jobs import _connection
from core.utils import atomic_json, read_json
from scripts.sync_exports import rollback_journal, sync_exports
from storage.database import Database


class SyncExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fcf-migrate-")
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.database = self.output / "factory.sqlite3"
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg.update(database=str(self.database), output_dir=str(self.output))
        project_dir = self.root / "cache" / "p"
        with Database(self.database) as db:
            db.upsert_project({"id": "p", "source": "source.mp4", "name": "source.mp4", "duration": 60,
                               "status": "ready", "metadata": {}, "project_dir": str(project_dir)})
            db.upsert_clips("p", [{"id": "c", "start": 2, "end": 30, "category": "opinion", "title": "AI旧标题",
                                   "description": "观点说明", "reason": "文字支持", "score": 80, "tags": []}])
            db.review_clip("c", "approved", title="人工修改标题：曼城/战术 ⚽")
            self.clip = db.get_clip("c")
            self.package = self.output / "p" / "c" / "horizontal_cached"
            self.package.mkdir(parents=True)
            self.old = self.package / "video.mp4"
            self.old.write_bytes(b"real bytes are preserved; synthetic fixture not playable")
            self.original_bytes = self.old.read_bytes()
            self.metadata = {"clip_id": "c", "project_id": "p", "title": "AI旧标题", "video": str(self.old),
                             "revision": self.clip["revision"], "orientation": "horizontal", "original": self.clip["original"]}
            atomic_json(self.package / "metadata.json", self.metadata)
            atomic_json(self.old.with_suffix(".timeline.json"), {"output": str(self.old), "start": 2, "end": 30})
            (self.package / "publication.txt").write_text("AI旧标题\n\n观点说明\n\n#曼城", encoding="utf-8")
            (self.package / "subtitles.srt").write_text("fixture subtitles", encoding="utf-8")
            db.set_export("c", "horizontal", self.old, self.metadata)
            self.manifest = self.output / "p" / "export_manifest.json"
            db.write_export_manifest("p", self.manifest, [self.metadata], [])
            self.review_rows = [tuple(r) for r in db.connection.execute("SELECT * FROM review_audit")]
            self.clip_row = tuple(db.connection.execute("SELECT * FROM clips WHERE id='c'").fetchone())
        with _connection(self.database) as conn:
            conn.execute("""INSERT INTO jobs(id,kind,payload_json,paths_json,status,created_at,updated_at,result_json,log_path)
                            VALUES('j','export','{}','{}','completed','before','before',?,'fixture.log')""",
                         (json.dumps({"exports": [self.metadata]}),))
        # This unregistered formal package and unrelated preview must survive too.
        self.unregistered = self.output / "p" / "unknown" / "vertical_cached"
        self.unregistered.mkdir(parents=True)
        (self.unregistered / "video.mp4").write_bytes(b"unregistered export bytes")
        atomic_json(self.unregistered / "metadata.json", {"clip_id": "unknown", "title": "未登记标题", "video": str(self.unregistered / "video.mp4")})
        self.unrelated = self.output / "preview.mp4"
        self.unrelated.write_bytes(b"preserved preview")
        self.acl = patch("scripts.sync_exports.restore_inherited_access", side_effect=lambda p: Path(p))
        self.acl_mock = self.acl.start()

    def tearDown(self):
        self.acl.stop()
        self.temp.cleanup()

    def test_dry_run_changes_nothing_then_apply_updates_all_references_idempotently(self):
        dry = sync_exports(self.cfg)
        self.assertEqual(dry["status"], "dry_run")
        self.assertTrue(self.old.exists())
        self.assertFalse((self.output / "cleanup").exists())
        report = sync_exports(self.cfg, apply=True)
        self.assertEqual(report["preserved_videos"], 3)
        new = self.package / "人工修改标题：曼城_战术 ⚽.mp4"
        self.assertEqual(new.read_bytes(), self.original_bytes)
        self.assertFalse(self.old.exists())
        self.assertEqual(read_json(new.with_suffix(".timeline.json"))["output"], str(new))
        self.assertEqual(read_json(self.package / "metadata.json")["video"], str(new))
        self.assertEqual(read_json(self.package / "metadata.json")["title"], self.clip["title"])
        self.assertEqual(read_json(self.manifest)["exports"][0]["video"], str(new))
        self.assertEqual((self.package / "publication.txt").read_text(encoding="utf-8").splitlines()[0], self.clip["title"])
        self.assertTrue((self.unregistered / "未登记标题.mp4").exists())
        self.assertEqual(self.unrelated.read_bytes(), b"preserved preview")
        with Database(self.database) as db:
            record = db.connection.execute("SELECT * FROM exports").fetchone()
            self.assertEqual(record["path"], str(new))
            self.assertEqual(json.loads(record["metadata_json"])["title"], self.clip["title"])
            self.assertEqual(tuple(db.connection.execute("SELECT * FROM clips WHERE id='c'").fetchone()), self.clip_row)
            self.assertEqual([tuple(r) for r in db.connection.execute("SELECT * FROM review_audit")], self.review_rows)
            result = json.loads(db.connection.execute("SELECT result_json FROM jobs WHERE id='j'").fetchone()[0])
            self.assertEqual(result["exports"][0]["video"], str(new))
            self.assertEqual(result["exports"][0]["original"]["title"], "AI旧标题")
        journal = read_json(report["journal"])
        self.assertEqual(journal["status"], "completed")
        self.assertTrue(Path(journal["database_backup"]).is_file())
        self.assertEqual(len(journal["moves"]), 2)
        self.assertGreater(self.acl_mock.call_count, 5)
        second = sync_exports(self.cfg, apply=True)
        self.assertTrue(all(m["old_path"] == m["new_path"] for m in second["moves"]))
        self.assertEqual(len(list(self.output.rglob("*.mp4"))), 3)

    def test_failure_rolls_back_video_and_sidecars_without_touching_reviews(self):
        original_manifest = self.manifest.read_bytes()
        with patch("scripts.sync_exports._transform", side_effect=RuntimeError("simulated failure after move")):
            with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                sync_exports(self.cfg, apply=True)
        self.assertEqual(self.old.read_bytes(), self.original_bytes)
        self.assertEqual(self.manifest.read_bytes(), original_manifest)
        self.assertEqual(read_json(self.old.with_suffix(".timeline.json"))["output"], str(self.old))
        journal_path = next((self.output / "cleanup").glob("sync_exports_*.json"))
        self.assertEqual(read_json(journal_path)["status"], "rolled_back")
        with Database(self.database) as db:
            self.assertEqual(db.connection.execute("SELECT path FROM exports").fetchone()[0], str(self.old))
            self.assertEqual(tuple(db.connection.execute("SELECT * FROM clips WHERE id='c'").fetchone()), self.clip_row)

    def test_explicit_rollback_restores_original_fields_but_preserves_later_review(self):
        report = sync_exports(self.cfg, apply=True)
        with Database(self.database) as db:
            db.review_clip("c", "approved", title="迁移之后的新审批标题")
            later = tuple(db.connection.execute("SELECT * FROM clips WHERE id='c'").fetchone())
            audit = [tuple(r) for r in db.connection.execute("SELECT * FROM review_audit")]
        rollback_journal(report["journal"])
        self.assertEqual(self.old.read_bytes(), self.original_bytes)
        with Database(self.database) as db:
            self.assertEqual(tuple(db.connection.execute("SELECT * FROM clips WHERE id='c'").fetchone()), later)
            self.assertEqual([tuple(r) for r in db.connection.execute("SELECT * FROM review_audit")], audit)
            self.assertEqual(db.connection.execute("SELECT path FROM exports").fetchone()[0], str(self.old))
        self.assertEqual(read_json(self.manifest)["exports"], [])

    def test_worker_lock_prevents_migration(self):
        from core.jobs import WorkerLock
        lock = WorkerLock(self.database)
        self.assertTrue(lock.acquire())
        try:
            with self.assertRaisesRegex(RuntimeError, "后台队列"):
                sync_exports(self.cfg, apply=True)
        finally:
            lock.release()
        self.assertTrue(self.old.exists())

    def test_nested_unregistered_package_uses_metadata_title_and_owns_only_its_video(self):
        nested = self.output / "historical" / "acceptance" / "exports" / "old-project" / "old-clip" / "vertical_abc"
        nested.mkdir(parents=True)
        old = nested / "video.mp4"
        old.write_bytes(b"nested historic video")
        sibling = nested / "pattern.mp4"
        sibling.write_bytes(b"unrelated test pattern")
        saved = {"clip_id": "historic-clip", "title": "旧包中保存的标题", "video": str(old)}
        atomic_json(nested / "metadata.json", saved)
        manifest = nested.parents[1] / "export_manifest.json"
        atomic_json(manifest, {"exports": [saved]})
        not_owned = self.output / "patterns"
        not_owned.mkdir()
        pattern = not_owned / "pattern.mp4"
        pattern.write_bytes(b"plain test pattern")
        atomic_json(not_owned / "metadata.json", {"title": "不能据此改名", "pattern": str(pattern)})
        report = sync_exports(self.cfg, apply=True)
        new = nested / "旧包中保存的标题.mp4"
        self.assertTrue(new.exists())
        self.assertFalse(old.exists())
        self.assertEqual(sibling.read_bytes(), b"unrelated test pattern")
        self.assertEqual(pattern.read_bytes(), b"plain test pattern")
        self.assertEqual(read_json(manifest)["exports"][0]["video"], str(new))
        self.assertEqual(report["preserved_videos"], 6)
        moved_sources = {move["old_path"] for move in report["moves"]}
        self.assertIn(str(old), moved_sources)
        self.assertNotIn(str(sibling), moved_sources)
        self.assertNotIn(str(pattern), moved_sources)


if __name__ == "__main__":
    unittest.main()
