"""GPU and CPU entry points share dependencies but enforce their device profile."""
from pathlib import Path
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.config import load_config
from core.releases import launch_environment, release_directory
from core.transcriber import _make_model
from scripts.launch import default_port, selected_python


class RuntimeProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "factory"
        self.root.mkdir()
        (self.root / ".env").write_text(
            "DEVICE=cuda\nCOMPUTE_TYPE=int8_float16\nFCF_RENDER_ENCODER=h264_nvenc\n",
            encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_cpu_profile_overrides_gpu_environment_without_copying_secrets(self):
        code, env = launch_environment(self.root, "v1.0.3-cpu", {
            "DEVICE": "cuda", "COMPUTE_TYPE": "float16", "FCF_RENDER_ENCODER": "h264_nvenc"})
        self.assertEqual((env["DEVICE"], env["COMPUTE_TYPE"], env["FCF_RENDER_ENCODER"]),
                         ("cpu", "int8", "libx264"))
        data = release_directory(self.root) / "release-data/v1.0.3-cpu"
        for key in ("CACHE_DIR", "OUTPUT_DIR", "DATABASE_PATH"):
            self.assertTrue(Path(env[key]).is_relative_to(data))
        self.assertFalse((code / ".env").exists())
        self.assertFalse(code.exists())  # Preparing the environment never copies code or keys.

    def test_latest_gpu_profile_overrides_cpu_environment(self):
        with patch("core.releases.RELEASE", "v1.0.3-gpu"):
            for version in ("final", "v1.0.3-gpu"):
                with self.subTest(version=version):
                    _, env = launch_environment(self.root, version, {
                        "DEVICE": "cpu", "COMPUTE_TYPE": "int8", "FCF_RENDER_ENCODER": "libx264"})
                    self.assertEqual((env["DEVICE"], env["COMPUTE_TYPE"], env["FCF_RENDER_ENCODER"]),
                                     ("cuda", "int8_float16", "auto"))

    def test_encoder_environment_overrides_dotenv_and_yaml(self):
        (self.root / "config").mkdir()
        (self.root / "config/settings.yaml").write_text("render:\n  encoder: auto\n", encoding="utf-8")
        with patch("core.config.ROOT", self.root), patch.dict(os.environ, {
                "DEVICE": "cpu", "COMPUTE_TYPE": "int8", "FCF_RENDER_ENCODER": "libx264"}, clear=True):
            cfg = load_config()
        self.assertEqual(cfg["render"]["encoder"], "libx264")
        self.assertEqual(cfg["asr"]["device"], "cpu")
        with patch("core.config.ROOT", self.root), patch.dict(os.environ, {
                "FCF_RENDER_ENCODER": "unknown_encoder"}, clear=True):
            with self.assertRaisesRegex(ValueError, "render.encoder"):
                load_config()

    def test_cpu_model_creation_never_initializes_or_queries_cuda(self):
        cuda_count = Mock(side_effect=AssertionError("CPU profile queried CUDA"))
        compute_types = Mock(return_value={"int8", "float32"})
        ctranslate = SimpleNamespace(get_cuda_device_count=cuda_count,
                                     get_supported_compute_types=compute_types)
        constructor = Mock(return_value=object())
        modules = {"ctranslate2": ctranslate,
                   "faster_whisper": SimpleNamespace(WhisperModel=constructor)}
        cfg = {"asr": {"model": "medium", "device": "cpu", "compute_type": "int8"}}
        with patch.dict(sys.modules, modules), patch("core.transcriber.prepare_cuda_runtime",
                side_effect=AssertionError("CPU profile initialized CUDA")):
            _, device, compute = _make_model(cfg, allow_cpu=False)
        self.assertEqual((device, compute), ("cpu", "int8"))
        compute_types.assert_called_once_with("cpu")
        constructor.assert_called_once_with("medium", device="cpu", compute_type="int8")
        cuda_count.assert_not_called()

    def test_profiles_reuse_primary_interpreter_without_duplicating_venv(self):
        suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
        shared = self.root / ".venv" / suffix
        shared.parent.mkdir(parents=True)
        shared.touch()
        code = release_directory(self.root) / "v1.0.3-cpu"
        self.assertEqual(selected_python(self.root, code), shared)
        self.assertFalse((code / ".venv").exists())

    def test_profile_default_ports(self):
        self.assertEqual(default_port("final"), 8501)
        self.assertEqual(default_port("v1.0.3-gpu"), 8501)
        self.assertEqual(default_port("v1.0.3-cpu"), 8502)


if __name__ == "__main__":
    unittest.main()
