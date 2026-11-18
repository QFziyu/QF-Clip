"""Wheel DLL discovery/loading and honest device readiness; no GPU required."""
import builtins
import copy
from importlib import metadata
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.config import DEFAULTS
from core import gpu_runtime
from core.transcriber import DeviceUnavailable, _make_model, resolve_device
from main import doctor


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fcf-cuda-")
        self.root = Path(self.temporary.name)
        self.packages = {}
        for package, directory, names in (
            ("nvidia-cuda-runtime-cu12", "cuda_runtime", ["cudart64_12.dll"]),
            ("nvidia-cuda-nvrtc-cu12", "cuda_nvrtc", ["nvrtc64_120_0.dll", "nvrtc-builtins64_129.dll"]),
            ("nvidia-cublas-cu12", "cublas", ["cublas64_12.dll", "cublasLt64_12.dll"]),
            ("nvidia-cudnn-cu12", "cudnn", ["cudnn64_9.dll", "cudnn_ops64_9.dll"]),
        ):
            files = []
            for name in names:
                relative = Path("nvidia") / directory / "bin" / name
                destination = self.root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"test DLL name only; loader is mocked")
                files.append(relative)
            files.append(Path("irrelevant.py"))
            self.packages[package] = SimpleNamespace(version="test-version", files=files,
                                                     locate_file=lambda relative: self.root / relative)

    def tearDown(self):
        self.temporary.cleanup()

    def test_registers_only_wheel_dll_dirs_and_retains_handles_without_duplicate_path(self):
        directory_handle = Mock()
        library_handle = Mock()
        with patch.object(gpu_runtime.sys, "platform", "win32"), \
                patch.object(gpu_runtime.metadata, "distribution", side_effect=self.packages.__getitem__) as discovery, \
                patch.object(gpu_runtime.os, "add_dll_directory", create=True, return_value=directory_handle) as register, \
                patch.object(gpu_runtime.ctypes, "WinDLL", create=True, return_value=library_handle) as load, \
                patch.dict(gpu_runtime._directory_handles, {}, clear=True), \
                patch.dict(gpu_runtime._library_handles, {}, clear=True), \
                patch.dict(os.environ, {"PATH": "original-system-path"}):
            first = gpu_runtime.prepare_cuda_runtime()
            second = gpu_runtime.prepare_cuda_runtime()
            self.assertTrue(first["usable"])
            self.assertEqual(first, second)
            self.assertEqual(register.call_count, 4)
            self.assertEqual(load.call_count, 7)
            self.assertEqual(len(gpu_runtime._directory_handles), 4)
            self.assertEqual(len(gpu_runtime._library_handles), 7)
            self.assertEqual(os.environ["PATH"].split(os.pathsep)[-1], "original-system-path")
            self.assertEqual(len(os.environ["PATH"].split(os.pathsep)), 5)
            self.assertTrue(all(Path(call.args[0]).is_absolute() for call in load.call_args_list))
            self.assertEqual([Path(call.args[0]).name for call in load.call_args_list[:len(gpu_runtime._REQUIRED)]], list(gpu_runtime._REQUIRED))
            self.assertEqual({call.args[0] for call in discovery.call_args_list}, set(gpu_runtime._DISTRIBUTIONS))
            directory_handle.close.assert_not_called()

    def test_missing_cublas_fails_runtime_probe_even_if_other_dlls_load(self):
        def load(name):
            if name.lower().endswith("cublas64_12.dll"):
                raise OSError("[WinError 126] missing dependency")
            return Mock()
        with patch.object(gpu_runtime.sys, "platform", "win32"), \
                patch.object(gpu_runtime.metadata, "distribution", side_effect=self.packages.__getitem__), \
                patch.object(gpu_runtime.os, "add_dll_directory", create=True, return_value=Mock()), \
                patch.object(gpu_runtime.ctypes, "WinDLL", create=True, side_effect=load), \
                patch.dict(gpu_runtime._directory_handles, {}, clear=True), \
                patch.dict(gpu_runtime._library_handles, {}, clear=True), patch.dict(os.environ):
            report = gpu_runtime.prepare_cuda_runtime()
        self.assertFalse(report["usable"])
        self.assertFalse(report["libraries"]["cublas64_12.dll"]["ok"])
        self.assertIn("cublas64_12.dll", gpu_runtime.runtime_error(report))
        self.assertIn("requirements-gpu.txt", gpu_runtime.runtime_error(report))

    def test_existing_system_runtime_is_supported_without_pip_wheels(self):
        with patch.object(gpu_runtime.sys, "platform", "win32"), \
                patch.object(gpu_runtime.metadata, "distribution", side_effect=metadata.PackageNotFoundError), \
                patch.object(gpu_runtime.ctypes, "WinDLL", create=True, return_value=Mock()) as load, \
                patch.dict(gpu_runtime._directory_handles, {}, clear=True), \
                patch.dict(gpu_runtime._library_handles, {}, clear=True), patch.dict(os.environ, {"PATH": "test-system-path"}):
            report = gpu_runtime.prepare_cuda_runtime()
        self.assertTrue(report["usable"])
        self.assertEqual(report["directories"], [])
        self.assertEqual([c.args[0] for c in load.call_args_list], list(gpu_runtime._REQUIRED))

    def test_non_windows_does_not_change_loader_or_environment(self):
        with patch.object(gpu_runtime.sys, "platform", "linux"), \
                patch.object(gpu_runtime.metadata, "distribution", side_effect=AssertionError("not applicable")), \
                patch.dict(os.environ, {"PATH": "unchanged"}):
            report = gpu_runtime.prepare_cuda_runtime()
            self.assertEqual(os.environ["PATH"], "unchanged")
        self.assertFalse(report["applicable"])
        self.assertIsNone(report["usable"])

    def test_system_runtime_on_path_uses_absolute_loading_without_recursive_search(self):
        directory = self.root / "system CUDA" / "bin"
        directory.mkdir(parents=True)
        for name in gpu_runtime._REQUIRED:
            (directory / name).write_bytes(b"mock library")
        with patch.object(gpu_runtime.sys, "platform", "win32"), \
                patch.object(gpu_runtime.metadata, "distribution", side_effect=metadata.PackageNotFoundError), \
                patch.object(gpu_runtime.os, "add_dll_directory", create=True, return_value=Mock()) as register, \
                patch.object(gpu_runtime.ctypes, "WinDLL", create=True, return_value=Mock()) as load, \
                patch.dict(gpu_runtime._directory_handles, {}, clear=True), \
                patch.dict(gpu_runtime._library_handles, {}, clear=True), \
                patch.dict(os.environ, {"PATH": str(directory)}):
            report = gpu_runtime.prepare_cuda_runtime()
            self.assertEqual(os.environ["PATH"], str(directory))
        self.assertTrue(report["usable"])
        register.assert_called_once_with(str(directory))
        self.assertTrue(all(Path(call.args[0]).is_absolute() for call in load.call_args_list))
        self.assertTrue(all(value["source"] == "configured_path" for value in report["libraries"].values()))


class DeviceReadinessTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)
        self.ctr = SimpleNamespace(get_cuda_device_count=lambda: 1,
                                   get_supported_compute_types=lambda device: {"int8_float16"} if device == "cuda" else {"int8"})
        self.broken_runtime = {"usable": False, "libraries": {"cublas64_12.dll": {"ok": False}},
                               "errors": [], "remedy": "install requirements-gpu.txt"}

    def test_visible_gpu_with_missing_dll_requires_explicit_cpu_fallback(self):
        with patch.dict("sys.modules", {"ctranslate2": self.ctr}), \
                patch("core.transcriber.prepare_cuda_runtime", return_value=self.broken_runtime):
            with self.assertRaisesRegex(DeviceUnavailable, "cublas64_12.dll"):
                resolve_device(self.cfg["asr"])
            self.assertEqual(resolve_device(self.cfg["asr"], allow_cpu=True), ("cpu", "int8"))

    def test_cpu_mode_does_not_probe_or_load_cuda_libraries(self):
        self.cfg["asr"]["device"] = "cpu"
        with patch.dict("sys.modules", {"ctranslate2": self.ctr}), \
                patch("core.transcriber.prepare_cuda_runtime", side_effect=AssertionError("CPU needs no CUDA")):
            self.assertEqual(resolve_device(self.cfg["asr"]), ("cpu", "int8"))

    def test_registration_precedes_ctranslate2_and_whisper_imports(self):
        events = []
        import_original = builtins.__import__
        def observe(name, *args, **kwargs):
            if name in ("ctranslate2", "faster_whisper"):
                events.append(name)
            return import_original(name, *args, **kwargs)
        def prepare():
            events.append("register")
            return {"usable": True}
        whisper = SimpleNamespace(WhisperModel=Mock(return_value="model"))
        with patch.dict("sys.modules", {"ctranslate2": self.ctr, "faster_whisper": whisper}), \
                patch("core.transcriber.prepare_cuda_runtime", side_effect=prepare), \
                patch("builtins.__import__", side_effect=observe):
            self.assertEqual(_make_model(self.cfg, False), ("model", "cuda", "int8_float16"))
        self.assertEqual(events, ["register", "ctranslate2", "faster_whisper"])

    def test_doctor_separates_visible_gpu_from_usable_dlls(self):
        with patch.dict("sys.modules", {"ctranslate2": self.ctr}), \
                patch("core.gpu_runtime.prepare_cuda_runtime", return_value=self.broken_runtime), \
                patch("main.importlib.util.find_spec", return_value=object()), \
                patch("main.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="ffmpeg test version\n")):
            report = doctor(self.cfg)
            self.cfg["asr"]["device"] = "cpu"
            cpu_report = doctor(self.cfg)
        self.assertTrue(report["readiness"]["cuda_device_visible"])
        self.assertFalse(report["readiness"]["cuda_runtime_dlls_usable"])
        self.assertFalse(report["readiness"]["cuda_ready"])
        self.assertFalse(report["ok"])
        self.assertTrue(cpu_report["ok"])


if __name__ == "__main__":
    unittest.main()
