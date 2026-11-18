"""Load Windows CUDA runtime wheels from this Python environment.

NVIDIA's driver makes a device visible but does not install cuBLAS/cuDNN. Python
also does not automatically search the ``nvidia/*/bin`` folders of pip wheels.
This module registers those installed distribution files, without changing the
machine PATH or searching other environments/drives.
"""
from __future__ import annotations

import ctypes
from importlib import metadata
import os
from pathlib import Path
import sys
from threading import RLock

_DISTRIBUTIONS = ("nvidia-cuda-runtime-cu12", "nvidia-cuda-nvrtc-cu12", "nvidia-cublas-cu12", "nvidia-cudnn-cu12")
_REQUIRED = ("cudart64_12.dll", "nvrtc64_120_0.dll", "cublasLt64_12.dll", "cublas64_12.dll", "cudnn64_9.dll")
_directory_handles = {}
_library_handles = {}
_lock = RLock()


def _discover_runtime():
    packages, libraries, errors = {}, {}, []
    for name in _DISTRIBUTIONS:
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            packages[name] = {"installed": False}
            continue
        found = 0
        for relative in distribution.files or ():
            if Path(str(relative)).suffix.lower() != ".dll":
                continue
            path = Path(distribution.locate_file(relative)).resolve()
            if path.is_file():
                libraries.setdefault(path.name.lower(), path)
                found += 1
            else:
                errors.append(f"已安装分发包 {name} 缺少文件: {path}")
        packages[name] = {"installed": True, "version": distribution.version, "dll_count": found}
    return packages, libraries, errors


def _register_directories(directories):
    errors = []
    for directory in directories:
        identity = os.path.normcase(str(directory))
        if identity in _directory_handles:
            continue
        try:
            # Do not use a context manager: closing this handle removes the
            # directory, while CTranslate2 loads cuDNN lazily during inference.
            _directory_handles[identity] = os.add_dll_directory(str(directory))
        except (AttributeError, OSError) as exc:
            errors.append(f"无法注册 DLL 目录 {directory}: {exc}")
    # Native LoadLibrary calls do not all use Python's DLL search directories.
    # Prepend wheel directories to this process only; inherited workers benefit.
    existing = os.environ.get("PATH", "").split(os.pathsep)
    present = {os.path.normcase(item) for item in existing if item}
    additions = [str(path) for path in directories if os.path.normcase(str(path)) not in present]
    if additions:
        os.environ["PATH"] = os.pathsep.join(additions + existing)
    return errors


def _path_libraries():
    """Check exact required names on configured PATH, never recursively scan."""
    found = {}
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry:
            continue
        directory = Path(entry)
        if not directory.is_absolute():
            continue  # Do not add the current working directory to DLL search.
        for name in _REQUIRED:
            candidate = directory / name
            if name.lower() not in found and candidate.is_file():
                found[name.lower()] = candidate.resolve()
    return found


def prepare_cuda_runtime():
    """Register and probe DLLs before importing CTranslate2/faster-whisper.

    ``usable`` verifies library loading, not successful GPU model inference. It
    is None off Windows, where the native loader has different configuration.
    Discovery is retried on each call so newly installed packages are noticed;
    already-loaded library/directory handles are kept for the process lifetime.
    """
    report = {"platform": sys.platform, "applicable": sys.platform == "win32", "usable": None,
              "directories": [], "distributions": {}, "libraries": {}, "errors": [],
              "note": "DLL 加载检查不下载模型，也不等同于已完成 GPU 推理。"}
    if sys.platform != "win32":
        return report
    with _lock:
        packages, files, discovery_errors = _discover_runtime()
        wheel_names = set(files)
        for name, path in _path_libraries().items():
            files.setdefault(name, path)
        directories = sorted({path.parent for path in files.values()}, key=str)
        report.update(distributions=packages, directories=[str(path) for path in directories])
        report["errors"].extend(discovery_errors + _register_directories(directories))
        # Load CUDA/NVRTC, cuBLAS Lt before cuBLAS, then cuDNN components.
        names = list(_REQUIRED) + sorted(name for name in files if name not in {n.lower() for n in _REQUIRED})
        for name in names:
            path = files.get(name.lower())
            target = str(path) if path else name
            identity = os.path.normcase(target)
            source = "python_environment" if name.lower() in wheel_names else "configured_path" if path else "system_loader"
            entry = {"path": target, "source": source, "ok": False}
            try:
                if identity not in _library_handles:
                    _library_handles[identity] = ctypes.WinDLL(target)
                entry["ok"] = True
            except (OSError, AttributeError) as exc:
                entry["error"] = str(exc)
            report["libraries"][name] = entry
        report["usable"] = not report["errors"] and all(entry["ok"] for entry in report["libraries"].values())
        if not report["usable"]:
            report["remedy"] = ('使用项目 Python 安装 requirements-gpu.txt 中的 CUDA 12/cuDNN 9 运行库，'
                                '重启任务后重试；也可显式选择 --allow-cpu 或 DEVICE=cpu。')
    return report


def runtime_error(report):
    failed = [name for name, value in report.get("libraries", {}).items() if not value.get("ok")]
    detail = "、".join(failed) or "; ".join(report.get("errors", []))
    return f"CUDA 运行库无法加载: {detail}。{report.get('remedy', '')}"
