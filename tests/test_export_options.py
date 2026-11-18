"""Persistent export preferences and immutable, credential-free queue snapshots."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.export_options import (default_export_options, effective_export_config,
                                 normalize_export_options, project_export_options)
from core.jobs import get_job, list_jobs, submit_job
from storage.database import Database


class ExportOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fcf-export-preferences-")
        self.root = Path(self.temporary.name)
        self.database = self.root / "preferences.sqlite3"
        self.cfg = {"database": str(self.database), "output_dir": str(self.root / "output"),
                    "cache_dir": str(self.root / "cache"),
                    "render": {"burn_subtitles": True, "horizontal": [1920, 1080]},
                    "subtitle": {"font": "Microsoft YaHei", "font_size": 48}}
        self.intro = self.root / "自己的 中文片头.mp4"
        self.intro.write_bytes(b"local fixture; queue validation does not decode media")
        with Database(self.database) as db:
            db.upsert_project({"id": "p", "source": str(self.root / "source.mp4"), "duration": 120,
                               "name": "自己的素材", "status": "ready"})
            db.upsert_clips("p", [{"id": "c", "start": 10, "end": 40,
                                    "title": "候选标题", "category": "other", "score": 75}])
            db.review_clip("c", "approved", title="人工批准标题")
        self.worker = patch("core.jobs.ensure_worker", return_value=False)
        self.worker.start()

    def tearDown(self):
        self.worker.stop()
        self.temporary.cleanup()

    def test_safe_defaults_and_preset_colors_survive_normalization(self):
        options = normalize_export_options()
        self.assertEqual(set(options), {"subtitle", "burn_subtitles", "intro"})
        self.assertEqual(options["intro"], {"enabled": False, "path": ""})
        self.assertTrue(options["burn_subtitles"])
        self.assertEqual(options["subtitle"]["preset"], "classic")
        self.assertEqual(options["subtitle"]["primary_color"], "#FFFFFF")
        self.assertNotIn("label", options["subtitle"])
        golden = normalize_export_options({"subtitle": {"preset": "gold"}})
        self.assertEqual(golden["subtitle"]["primary_color"], "#FFD34E")
        self.assertTrue(golden["subtitle"]["bold"])
        customized = normalize_export_options({"subtitle": {"preset": "gold", "primary_color": "#a1b2c3"},
                                                "burn_subtitles": False})
        self.assertEqual(customized["subtitle"]["primary_color"], "#A1B2C3")
        self.assertFalse(customized["burn_subtitles"])
        self.assertEqual(normalize_export_options(customized), customized)

    def test_invalid_unknown_and_nested_credentials_are_rejected(self):
        bad_options = [
            [], "classic", {"api_key": "secret"}, {"openai": {"api_key": "secret"}},
            {"subtitle": []}, {"subtitle": {"api_key": "secret"}},
            {"subtitle": {"font": {"api_key": "secret"}}},
            {"subtitle": {"preset": "unknown"}}, {"subtitle": {"primary_color": "red"}},
            {"burn_subtitles": "true"}, {"intro": []},
            {"intro": {"enabled": False, "api_key": "secret"}},
            {"intro": {"enabled": 1}}, {"intro": {"enabled": True}},
            {"intro": {"enabled": True, "path": "\0bad.mp4"}},
            {"intro": {"path": {"api_key": "secret"}}},
        ]
        for options in bad_options:
            with self.subTest(options=options), self.assertRaises(ValueError):
                normalize_export_options(options)
        with Database(self.database) as db:
            for options in bad_options:
                with self.subTest(storage=options), self.assertRaises(ValueError):
                    db.set_export_options("p", options)
            self.assertEqual(db.get_export_options("p"), {})

    def test_effective_configuration_is_a_deep_copy_without_leaking_extra_fields(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["openai"] = {"api_key": "in-memory-credential", "nested": {"values": [1, 2]}}
        original = copy.deepcopy(cfg)
        options = {"subtitle": {"preset": "cyan", "font_size": 72}, "burn_subtitles": False,
                   "intro": {"enabled": True, "path": str(self.intro)}}
        original_options = copy.deepcopy(options)
        effective = effective_export_config(cfg, options)
        self.assertEqual(effective["subtitle"]["primary_color"], "#58E5FF")
        self.assertEqual(effective["subtitle"]["font_size"], 72)
        self.assertFalse(effective["render"]["burn_subtitles"])
        self.assertNotIn("intro", effective)
        effective["render"]["horizontal"][0] = 100
        effective["openai"]["nested"]["values"].append(3)
        effective["subtitle"]["font"] = "Arial"
        self.assertEqual(cfg, original)
        self.assertEqual(options, original_options)
        defaults = default_export_options(cfg)
        self.assertNotIn("in-memory-credential", json.dumps(defaults))

    def test_preferences_persist_across_restart_without_touching_review_or_history(self):
        options = {"subtitle": {"preset": "comic", "primary_color": "#DDAABB"},
                   "intro": {"enabled": True, "path": str(self.intro)}}
        expected = normalize_export_options(options)
        with Database(self.database) as db:
            before = db.get_clip("c")
            audit = db.review_history("c")
            db.set_export_options("p", options)
            options["subtitle"]["primary_color"] = "#000000"
            retrieved = db.get_export_options("p")
            retrieved["subtitle"]["font"] = "Arial"
        with Database(self.database) as db:
            self.assertEqual(db.get_export_options("p"), expected)
            self.assertEqual(db.get_clip("c"), before)
            self.assertEqual(db.review_history("c"), audit)
            self.assertEqual(project_export_options(db, "p", self.cfg), expected)
            override = project_export_options(db, "p", self.cfg, {"subtitle": {"preset": "boxed"}})
            self.assertEqual(override["subtitle"]["preset"], "boxed")
            self.assertEqual(db.get_export_options("p"), expected)
            with self.assertRaises(ValueError):
                db.set_export_options("missing-project", {})

    def test_project_without_preferences_uses_config_defaults(self):
        with Database(self.database) as db:
            self.assertEqual(project_export_options(db, "p", self.cfg), default_export_options(self.cfg))

    def test_job_persists_normalized_snapshot_independent_of_input_and_saved_preferences(self):
        options = {"subtitle": {"preset": "gold", "primary_color": "#a1b2c3"},
                   "intro": {"enabled": True, "path": f'"{self.intro}"'}}
        expected = normalize_export_options(options)
        with Database(self.database) as db:
            db.set_export_options("p", options)
        payload = {"project_id": "p", "orientations": ["horizontal", "horizontal", "vertical"],
                   "export_options": options}
        job = submit_job("export", payload, self.cfg)
        self.assertEqual(job["payload"]["export_options"], expected)
        self.assertEqual(job["payload"]["orientations"], ["horizontal", "vertical"])
        options["subtitle"]["preset"] = "comic"
        options["subtitle"]["primary_color"] = "#000000"
        options["intro"]["enabled"] = False
        payload["orientations"].clear()
        job["payload"]["export_options"]["subtitle"]["font"] = "Arial"
        with Database(self.database) as db:
            db.set_export_options("p", {"subtitle": {"preset": "boxed"}})
            self.assertEqual(db.get_export_options("p")["subtitle"]["preset"], "boxed")
        reread = get_job(job["id"], self.cfg)
        self.assertEqual(reread["payload"]["export_options"], expected)
        self.assertEqual(reread["payload"]["orientations"], ["horizontal", "vertical"])
        self.assertEqual(reread["status"], "queued")

    def test_missing_intro_and_nested_secret_rejected_before_job_is_saved(self):
        missing = {"intro": {"enabled": True, "path": str(self.root / "不存在.mp4")}}
        directory = {"intro": {"enabled": True, "path": str(self.root)}}
        secret = {"subtitle": {"api_key": "must-never-be-persisted"}}
        for options in (missing, directory, secret):
            with self.subTest(options=options), self.assertRaises(ValueError):
                submit_job("export", {"project_id": "p", "export_options": options}, self.cfg)
        self.assertEqual(list_jobs(self.cfg), [])


if __name__ == "__main__":
    unittest.main()
