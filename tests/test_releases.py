"""Fallbacks preserve source/data and integrity checks reject corruption and traversal."""
import json
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from core.releases import (create_release, launch_environment, release_directory, safe_member,
                           sha256, verify_release, version_name)
from core.version import RELEASE
from scripts.launch import main as launch_main, owned_server, port_available, process_command


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "中文项目"
        (self.root / "ui").mkdir(parents=True)
        (self.root / "ui/app.py").write_text("print('review')\n", encoding="utf-8")
        (self.root / "main.py").write_text("print('process')\n", encoding="utf-8")
        (self.root / "requirements.lock.txt").write_text("example==1.0\n", encoding="utf-8")
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def snapshot(self, version="v0.9-rc"):
        return create_release(self.root, version, "checkpoint")

    def test_snapshot_excludes_secrets_and_media_and_captures_lock(self):
        (self.root / ".env").write_text("OPENAI_API_KEY=very-secret-key\n", encoding="utf-8")
        (self.root / ".env.old").write_text("very-secret-key", encoding="utf-8")
        (self.root / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
        for directory in (".venv", "cache", "output", "input", ".git"):
            (self.root / directory).mkdir()
            (self.root / directory / "private.json").write_text("very-secret-key", encoding="utf-8")
        manifest = self.snapshot()
        self.assertEqual(set(manifest["source_files"]), {"main.py", "ui/app.py", "requirements.lock.txt", ".env.example"})
        with zipfile.ZipFile(manifest["archive"]) as archive:
            for name in archive.namelist():
                self.assertNotIn(b"very-secret-key", archive.read(name))
        self.assertTrue(verify_release(self.root, "v0.9-rc")["ok"])

    def test_codex_login_runtime_is_excluded_and_storage_code_is_preserved(self):
        storage = self.root / "storage"
        runtime = storage / "codex_official"
        runtime.mkdir(parents=True)
        (storage / "database.py").write_text("# actual application source\n", encoding="utf-8")
        auth = runtime / "auth.json"
        auth.write_text('{"token":"private-login-token"}', encoding="utf-8")
        temporary = runtime / "apply_patch.bat"
        temporary.write_text("runtime wrapper", encoding="utf-8")
        manifest = self.snapshot()
        self.assertIn("storage/database.py", manifest["source_files"])
        self.assertFalse(any("codex_official" in name for name in manifest["source_files"]))
        with zipfile.ZipFile(manifest["archive"]) as archive:
            self.assertFalse(any("codex_official" in name for name in archive.namelist()))
        temporary.unlink()
        auth.write_text('{"token":"refreshed-private-token"}', encoding="utf-8")
        (runtime / "models_cache.json").write_text("{}", encoding="utf-8")
        self.assertTrue(verify_release(self.root, "v0.9-rc", code_root=self.root)["ok"])

    def test_release_cannot_overwrite_existing_snapshot(self):
        first = self.snapshot()
        with self.assertRaises(FileExistsError):
            self.snapshot()
        self.assertEqual(first["source_files"], self.snapshot("v1.0-final")["source_files"])

    def test_repository_metadata_is_kept_and_local_overrides_are_excluded(self):
        for name in ('.gitattributes', '.editorconfig', '.github/workflows/tests.yml', 'output/.gitkeep'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# repository metadata\n', encoding='utf-8')
        for name in ('.streamlit/secrets.toml', 'config/settings.local.yaml', 'runtime/session.json', 'dist/private.json'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private value', encoding='utf-8')
        files = self.snapshot()['source_files']
        for name in ('.gitattributes', '.editorconfig', '.github/workflows/tests.yml'):
            self.assertIn(name, files)
        for name in ('.streamlit/secrets.toml', 'config/settings.local.yaml', 'runtime/session.json', 'dist/private.json'):
            self.assertNotIn(name, files)

    def test_zip_is_deterministic_for_same_source(self):
        first = self.snapshot()
        second = self.snapshot("v1.0-final")
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])

    def test_malicious_version_and_manifest_paths_are_rejected(self):
        for name in ("../v0.9-rc", "v0.9-rc/../../", "C:\\escape", "v1.0:stream", "v1.0-rc.", "", "final"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                version_name(name)
        for name in ("../outside.py", "C:/file.py", "/tmp/file.py", "x\\..\\outside.py", "./main.py", "a//b.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_member(self.root, name)
        self.snapshot()
        manifest_file = release_directory(self.root) / "v0.9-rc.manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["source_files"]["../secret.py"] = "0" * 64
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            verify_release(self.root, "v0.9-rc")

    def test_verify_detects_code_changes_added_code_and_archive_corruption(self):
        manifest = self.snapshot()
        code = Path(manifest["code_path"])
        (code / "main.py").write_text("print('tampered')\n", encoding="utf-8")
        (code / "ui/extra.py").write_text("print('unexpected')\n", encoding="utf-8")
        Path(manifest["archive"]).write_bytes(b"corrupt")
        report = verify_release(self.root, "v0.9-rc")
        self.assertFalse(report["ok"])
        self.assertIn("changed: main.py", report["errors"])
        self.assertIn("unexpected: ui/extra.py", report["errors"])
        self.assertIn("archive hash mismatch", report["errors"])

    def test_primary_verification_matches_snapshot_then_detects_edit(self):
        self.snapshot("v1.0-final")
        self.assertTrue(verify_release(self.root, "v1.0-final", code_root=self.root)["ok"])
        (self.root / "main.py").write_text("new", encoding="utf-8")
        self.assertFalse(verify_release(self.root, "v1.0-final", code_root=self.root)["ok"])

    def test_primary_allows_documented_config_customization_but_rejects_code_changes(self):
        (self.root / "config").mkdir()
        settings = self.root / "config/settings.yaml"
        settings.write_text("asr:\n  model: medium\n", encoding="utf-8")
        self.snapshot(RELEASE)
        settings.write_text("asr:\n  model: small\n", encoding="utf-8")
        runtime = verify_release(self.root, RELEASE, code_root=self.root, allow_customization=True)
        self.assertTrue(runtime["ok"])
        self.assertEqual(runtime["customized_files"], ["config/settings.yaml"])
        self.assertFalse(verify_release(self.root, RELEASE, code_root=self.root)["ok"])
        with patch("scripts.launch.ROOT", self.root), patch("scripts.launch.selected_python", return_value=Path(sys.executable)), patch("scripts.launch.port_available", return_value=False), patch("scripts.launch.owned_server", return_value=True), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(launch_main(["--no-browser"]), 0)
            (self.root / "main.py").write_text("changed code\n", encoding="utf-8")
            self.assertEqual(launch_main(["--no-browser"]), 1)
        self.assertFalse(verify_release(self.root, RELEASE, code_root=self.root, allow_customization=True)["ok"])

    def test_snapshot_configuration_stays_immutable(self):
        (self.root / "config").mkdir()
        (self.root / "config/settings.yaml").write_text("{}\n", encoding="utf-8")
        manifest = self.snapshot()
        (Path(manifest["code_path"]) / "config/settings.yaml").write_text("asr: {}\n", encoding="utf-8")
        self.assertFalse(verify_release(self.root, "v0.9-rc")["ok"])
        with self.assertRaises(ValueError):
            verify_release(self.root, "v0.9-rc", allow_customization=True)

    def test_sqlite_backup_recovers_committed_wal_data(self):
        (self.root / "output").mkdir()
        source = self.root / "output/factory.sqlite3"
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE reviews (title TEXT)")
            connection.execute("INSERT INTO reviews VALUES ('已通过')")
            connection.commit()
            self.assertTrue(source.with_suffix(".sqlite3-wal").exists())
            manifest = self.snapshot()
            with closing(sqlite3.connect(manifest["database_snapshot"])) as backup:
                self.assertEqual(backup.execute("SELECT title FROM reviews").fetchone()[0], "已通过")
                self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(sha256(manifest["database_snapshot"]), manifest["database_sha256"])
        self.assertTrue(verify_release(self.root, "v0.9-rc")["ok"])

    def test_fallback_environment_isolates_data_without_writing_secret_files(self):
        (self.root / ".env").write_text("OPENAI_API_KEY=secret\nOUTPUT_DIR=production\nDATABASE_PATH=production.db\n", encoding="utf-8")
        self.snapshot()
        code, env = launch_environment(self.root, "v0.9-rc", {"PATH": "test", "OUTPUT_DIR": "another-production"})
        self.assertEqual(env["OPENAI_API_KEY"], "secret")
        expected = release_directory(self.root) / "release-data/v0.9-rc"
        for key in ("CACHE_DIR", "OUTPUT_DIR", "DATABASE_PATH"):
            self.assertTrue(Path(env[key]).is_relative_to(expected))
        self.assertFalse((code / ".env").exists())
        _, final = launch_environment(self.root, "final", {"OPENAI_API_KEY": "environment-value"})
        self.assertEqual(final["OPENAI_API_KEY"], "environment-value")
        self.assertEqual(final["FCF_RELEASE_VERSION"], RELEASE)
        self.assertNotIn("DATABASE_PATH", final)

    def test_original_baseline_manifest_without_v_prefix_is_supported(self):
        self.snapshot("v0.1-baseline")
        manifest_file = release_directory(self.root) / "v0.1-baseline.manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest["version"] = "0.1-baseline"
        manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertTrue(verify_release(self.root, "v0.1-baseline")["ok"])

    def test_port_check_detects_foreign_listener_and_record_is_not_trusted(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            self.assertFalse(port_available(port))
            record = self.root / "fake.json"
            expected = ["python", "-m", "streamlit", "run", str(self.root / "ui/app.py")]
            record.write_text(json.dumps({"pid": 123, "root": str(self.root), "port": port,
                                           "version": "final", "release": RELEASE, "command": expected}), encoding="utf-8")
            with patch("scripts.launch.process_command", return_value=["foreign"]), patch("scripts.launch.healthy", return_value=True):
                self.assertFalse(owned_server(record, self.root, port, "final"))
            with patch("scripts.launch.process_command", return_value=expected), patch("scripts.launch.healthy", return_value=True), patch("scripts.launch.listener_pids", return_value={123}):
                self.assertTrue(owned_server(record, self.root, port, "final"))
            with patch("scripts.launch.process_command", return_value=expected), patch("scripts.launch.healthy", return_value=True), patch("scripts.launch.listener_pids", return_value=set()):
                self.assertFalse(owned_server(record, self.root, port, "final"))

    def test_server_from_earlier_primary_release_is_not_reused(self):
        record = self.root / "old-server.json"
        command = ["python", "-m", "streamlit", "run", str(self.root / "ui/app.py")]
        record.write_text(json.dumps({"pid": 123, "root": str(self.root), "port": 8501,
                                      "version": "final", "release": "v1.0-final", "command": command}), encoding="utf-8")
        with patch("scripts.launch.process_command", return_value=command), patch("scripts.launch.healthy", return_value=True), patch("scripts.launch.listener_pids", return_value={123}):
            self.assertFalse(owned_server(record, self.root, 8501, "final"))

    def test_can_verify_native_command_line_of_own_live_process(self):
        command = [sys.executable, "-c", "import time; time.sleep(10)"]
        child = subprocess.Popen(command)
        try:
            self.assertEqual(process_command(child.pid), command)
        finally:
            child.terminate()
            child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
