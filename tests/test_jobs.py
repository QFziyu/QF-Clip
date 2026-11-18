"""Queue persistence, serialization, recovery and real worker process tests."""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core.config import load_config
from core.jobs import (WorkerLock, _connection, _execute, ensure_worker, get_job,
                       list_jobs, retry_job, run_worker, submit_job, tail_job_log)

ROOT = Path(__file__).resolve().parents[1]


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = load_config()
        self.cfg.update(database=str(self.root / "queue.sqlite3"), output_dir=str(self.root / "output"),
                        cache_dir=str(self.root / "cache"))
        self.source = self.root / "中文 录像.mp4"
        self.source.write_bytes(b"fake video only for queue dispatch tests")
        self.launch = patch("core.jobs.ensure_worker", return_value=False)
        self.launch.start()

    def tearDown(self):
        self.launch.stop()
        self.temp.cleanup()

    def submit(self, **extra):
        return submit_job("process", {"path": str(self.source), **extra}, self.cfg)

    def test_durable_queue_and_duplicate_active_submission(self):
        first = self.submit(skip_render=True)
        second = self.submit(skip_render=True)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], "queued")
        self.assertEqual(len(list_jobs(self.cfg)), 1)
        self.assertEqual(get_job(first["id"], self.cfg)["payload"]["path"], str(self.source))

    def test_unknown_commands_credentials_and_missing_file_rejected(self):
        for kind, payload in [("shell", {"path": str(self.source)}),
                              ("process", {"path": str(self.source), "api_key": "secret"}),
                              ("process", {"path": str(self.root / "missing.mp4")}),
                              ("process", {"path": str(self.source), "selection_mode": "unknown"}),
                              ("export", {"project_id": "p", "orientations": ["square"]})]:
            with self.subTest(kind=kind, payload=payload), self.assertRaises(ValueError):
                submit_job(kind, payload, self.cfg)

    def test_credentials_not_persisted(self):
        self.cfg["openai"]["api_key"] = "never-write-this-secret-key"
        self.submit()
        with _connection(self.cfg["database"]) as conn:
            saved = "\n".join(conn.iterdump())
        self.assertNotIn("never-write-this-secret-key", saved)
        self.assertNotIn('"openai"', saved)

    def test_lock_blocks_second_worker(self):
        one, two = WorkerLock(self.cfg["database"]), WorkerLock(self.cfg["database"])
        self.assertTrue(one.acquire())
        try:
            self.assertFalse(two.acquire())
        finally:
            one.release()
        self.assertTrue(two.acquire())
        two.release()

    def test_dead_running_job_becomes_interrupted_but_live_pid_is_preserved(self):
        job = self.submit()
        with _connection(self.cfg["database"]) as conn:
            conn.execute("UPDATE jobs SET status='running',worker_pid=? WHERE id=?", (os.getpid(), job["id"]))
        self.assertEqual(get_job(job["id"], self.cfg)["status"], "running")
        with patch("core.jobs._pid_alive", return_value=False):
            interrupted = get_job(job["id"], self.cfg)
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(retry_job(job["id"], self.cfg)["status"], "queued")
        with self.assertRaises(ValueError):
            retry_job(job["id"], self.cfg)

    def test_held_lock_prevents_orphan_detection(self):
        job = self.submit()
        with _connection(self.cfg["database"]) as conn:
            conn.execute("UPDATE jobs SET status='running',worker_pid=99999999 WHERE id=?", (job["id"],))
        lock = WorkerLock(self.cfg["database"])
        self.assertTrue(lock.acquire())
        try:
            self.assertEqual(get_job(job["id"], self.cfg)["status"], "running")
        finally:
            lock.release()

    def test_worker_success_failure_and_retry_preserve_results(self):
        first = self.submit(skip_render=True)
        second = submit_job("preview", {"project_id": "p", "clip_id": "c"}, self.cfg)
        with patch("core.jobs._execute", side_effect=[{"project_id": "p", "errors": []}, RuntimeError("fixture failure")]) as execute:
            self.assertEqual(run_worker(self.cfg), 0)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(get_job(first["id"], self.cfg)["status"], "completed")
        failed = get_job(second["id"], self.cfg)
        self.assertEqual(failed["status"], "failed")
        self.assertIn("fixture failure", tail_job_log(failed))
        retry_job(second["id"], self.cfg)
        with patch("core.jobs._execute", return_value={"preview_path": "preview.mp4"}):
            run_worker(self.cfg)
        self.assertEqual(get_job(second["id"], self.cfg)["attempt"], 2)
        self.assertEqual(get_job(second["id"], self.cfg)["status"], "completed")

    def test_partial_output_is_failed_and_result_retained(self):
        job = submit_job("export", {"project_id": "p", "orientations": ["horizontal"]}, self.cfg)
        result = {"exports": [{"video": "complete.mp4"}], "errors": [{"error": "fixture"}]}
        with patch("core.jobs._execute", return_value=result):
            run_worker(self.cfg)
        saved = get_job(job["id"], self.cfg)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["result"], result)

    def test_reload_configuration_and_redact_exception_log(self):
        config = self.root / "custom.yaml"
        config.write_text("openai:\n  api_key: private-fixture-secret\nrender:\n  bitrate: 1M\n", encoding="utf-8")
        self.cfg["_config_path"] = str(config)
        job = self.submit()
        config.write_text("openai:\n  api_key: private-fixture-secret\nrender:\n  bitrate: 2M\n", encoding="utf-8")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch("core.llm_settings.settings_path", return_value=self.root/".env.llm"), patch("core.jobs._execute", side_effect=RuntimeError("private-fixture-secret")) as execute:
            run_worker(self.cfg)
        self.assertEqual(execute.call_args.args[2]["render"]["bitrate"], "2M")
        saved = get_job(job["id"], self.cfg)
        self.assertNotIn("private-fixture-secret", saved["error"])
        self.assertNotIn("private-fixture-secret", tail_job_log(saved))

    def test_selection_mode_is_not_forwarded_as_unknown_pipeline_kwarg(self):
        with patch("core.pipeline.process_video", return_value={}) as process:
            _execute("process", {"path": str(self.source), "selection_mode": "audio"}, self.cfg)
        self.assertEqual(process.call_args.args[1]["selection"]["mode"], "audio")
        self.assertNotIn("selection_mode", process.call_args.kwargs)

    def test_log_tail_is_bounded(self):
        job = self.submit()
        path = Path(job["log_path"])
        path.parent.mkdir(parents=True)
        path.write_text("x" * 100000 + "last line", encoding="utf-8")
        tail = tail_job_log(job, limit=2048)
        self.assertLessEqual(len(tail), 2048)
        self.assertTrue(tail.endswith("last line"))

    def test_real_worker_subprocess_reports_missing_source_without_api(self):
        job = self.submit()
        self.source.unlink()
        result = subprocess.run([sys.executable, str(ROOT / "scripts/job_worker.py"), "--database", self.cfg["database"]],
                                capture_output=True, text=True, timeout=30, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = get_job(job["id"], self.cfg)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["attempt"], 1)
        self.assertTrue(saved["error"])
        self.assertIn("任务失败", tail_job_log(saved))


if __name__ == "__main__":
    unittest.main()
