"""Start the final review UI or an isolated preserved version; never kill other services."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.releases import current_environment, launch_environment, release_directory, verify_release, version_name
from core.version import RELEASE


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def healthy(port: int) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as response:
            return response.status == 200 and response.read(32).strip() == b"ok"
    except (OSError, urllib.error.URLError):
        return False


def process_command(pid: int) -> list[str]:
    """Read command arguments, so a reused PID/foreign Streamlit is never assumed ours."""
    if pid <= 0:
        return []
    try:
        if os.name != "nt":
            return [part.decode() for part in (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0") if part]
        # Windows 11 exposes the command line through a read-only process query.
        # This avoids requiring WMI/CIM service access, often unavailable in restricted sessions.
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return []
        try:
            query = ctypes.windll.ntdll.NtQueryInformationProcess
            query.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                              ctypes.POINTER(ctypes.c_ulong)]
            query.restype = ctypes.c_long
            size = ctypes.c_ulong()
            query(handle, 60, None, 0, ctypes.byref(size))  # ProcessCommandLineInformation
            if not 0 < size.value < 1024 * 1024:
                return []
            buffer = ctypes.create_string_buffer(size.value)
            if query(handle, 60, buffer, size, ctypes.byref(size)) != 0:
                return []
            class UnicodeString(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ushort), ("capacity", ctypes.c_ushort), ("buffer", ctypes.c_void_p)]
            value = UnicodeString.from_buffer(buffer)
            command = ctypes.wstring_at(value.buffer, value.length // 2)
        finally:
            kernel.CloseHandle(handle)
        count = ctypes.c_int()
        split = ctypes.windll.shell32.CommandLineToArgvW
        split.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        split.restype = ctypes.POINTER(ctypes.c_wchar_p)
        arguments = split(command, ctypes.byref(count))
        if not arguments:
            return []
        try:
            return [arguments[i] for i in range(count.value)]
        finally:
            free = ctypes.windll.kernel32.LocalFree
            free.argtypes = [ctypes.c_void_p]
            free.restype = ctypes.c_void_p
            free(arguments)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def listener_pids(port: int) -> set[int] | None:
    """Windows TCP table links the health response to its actual listening process."""
    if os.name != "nt":
        return None
    size = ctypes.c_ulong()
    query = ctypes.windll.iphlpapi.GetExtendedTcpTable
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int,
                      ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    query.restype = ctypes.c_ulong
    query(None, ctypes.byref(size), False, 2, 3, 0)  # IPv4 / TCP_TABLE_OWNER_PID_LISTENER
    if not 0 < size.value < 16 * 1024 * 1024:
        return set()
    buffer = ctypes.create_string_buffer(size.value)
    if query(buffer, ctypes.byref(size), False, 2, 3, 0) != 0:
        return set()
    count = ctypes.c_ulong.from_buffer(buffer).value
    if count * 24 + 4 > len(buffer):
        return set()
    rows = (ctypes.c_ulong * (count * 6)).from_buffer(buffer, 4)
    return {rows[i * 6 + 5] for i in range(count)
            if socket.ntohs(rows[i * 6 + 2] & 65535) == port}


def owned_server(record: Path, code: Path, port: int, version: str) -> bool:
    try:
        saved = json.loads(record.read_text(encoding="utf-8"))
        if saved.get("root") != str(code) or saved.get("port") != port or saved.get("version") != version:
            return False
        if version in ("final", "current-cpu") and saved.get("release") != (RELEASE.removesuffix("-gpu")+"-cpu" if version == "current-cpu" else RELEASE):
            return False  # A live process from an earlier primary release needs a fresh interpreter.
        expected = saved.get("command")
        if not isinstance(expected, list) or not expected or expected != process_command(int(saved["pid"])):
            return False
        # Command must contain the selected absolute app, not merely match an arbitrary PID record.
        if ["-m", "streamlit", "run", str(code / "ui/app.py")] != expected[1:5]:
            return False
        listeners = listener_pids(port)
        if listeners is not None and not any(process_command(pid) == expected for pid in listeners):
            return False
        return healthy(port)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def selected_python(root: Path, code: Path) -> Path:
    suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    dedicated = code / ".venv" / suffix
    primary = root / ".venv" / suffix
    if dedicated.is_file():
        return dedicated
    if primary.is_file():
        return primary
    for portable in (code / ".runtime/python.exe", root / ".runtime/python.exe"):
        if portable.is_file():
            return portable
    raise RuntimeError("未找到项目 Python 环境。请运行 powershell -ExecutionPolicy Bypass -File scripts/setup.ps1")


def default_port(version: str) -> int:
    if version == "final" or version.endswith("-gpu"):
        return 8501
    return 8502 if version.endswith("-cpu") else 8504


def existing_current_port(state_dir, code, port, runtime_id):
    for candidate in range(port + 1, min(port + 21, 65536)):
        record = state_dir / f'{runtime_id}-{candidate}.json'
        if record.is_file() and owned_server(record, code, candidate, runtime_id):
            return candidate
    return None


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="启动当前工作台；CPU 和 GPU 共用当前源码")
    parser.add_argument("--version", default="final", help="final 为当前源码；仅手动回退时指定历史版本")
    parser.add_argument("--profile", choices=["gpu", "cpu"], default="gpu", help="当前源码的运行设备")
    parser.add_argument("--port", type=int, help="默认 GPU 8501，CPU 8502")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.version != "final":
            version_name(args.version)
        port = args.port if args.port is not None else default_port("current-cpu" if args.version == "final" and args.profile == "cpu" else args.version)
        if not 1024 <= port <= 65535:
            raise ValueError("端口必须在 1024～65535 之间")
        code, env = current_environment(ROOT, args.profile) if args.version == "final" else launch_environment(ROOT, args.version)
        manifest_version = env["FCF_RELEASE_VERSION"]
        if args.version != "final" or (release_directory(ROOT) / f"{RELEASE}.manifest.json").is_file():
            check = verify_release(ROOT, RELEASE if args.version == "final" else manifest_version, code_root=code if args.version == "final" else None,
                                   allow_customization=args.version == "final")
            if not check["ok"]:
                raise RuntimeError("版本完整性校验失败: " + "; ".join(check["errors"][:8]))
            if check.get("customized_files"):
                print("已识别本地配置自定义: " + ", ".join(check["customized_files"]))
        else:
            print("提示：最终发布清单尚未创建，正在运行当前主目录。")
        if not (code / "ui/app.py").is_file():
            raise RuntimeError(f"版本不存在或缺少审批页面: {code}")
        interpreter = selected_python(ROOT, code)
        if code != ROOT and not interpreter.is_relative_to(code):
            print("保存版本复用主项目 Python 依赖；源码和数据使用独立目录。")
        state_dir = (Path(env["OUTPUT_DIR"]) if args.version != "final" or args.profile == "cpu" else ROOT / "output") / ".launcher"
        state_dir.mkdir(parents=True, exist_ok=True)
        runtime_id = args.version if args.version != "final" or args.profile == "gpu" else "current-cpu"
        record = state_dir / f"{runtime_id}-{port}.json"
        url = f"http://127.0.0.1:{port}/"
        if not port_available(port):
            if owned_server(record, code, port, runtime_id):
                print(f"审批界面已运行 ({manifest_version}): {url}")
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
            if args.port is None and args.version == 'final':
                existing = existing_current_port(state_dir, code, port, runtime_id)
                if existing is not None:
                    url = f'http://127.0.0.1:{existing}/'
                    print(f'当前工作台已运行 ({manifest_version}): {url}')
                    if not args.no_browser:
                        webbrowser.open(url)
                    return 0
            next_port = next((p for p in range(port + 1, min(port + 21, 65536)) if port_available(p)), None)
            suggestion = f" 请添加 --port {next_port} 使用空闲端口。" if next_port else "请指定其他空闲端口。"
            if args.port is not None or next_port is None:
                raise RuntimeError(f"端口 {port} 已被其他进程占用或无法确认其版本；不会关闭该进程。" + suggestion)
            print(f"端口 {port} 正在使用，当前版本改用 {next_port}。")
            port = next_port
            record = state_dir / f"{runtime_id}-{port}.json"
            url = f"http://127.0.0.1:{port}/"
        probe = subprocess.run([str(interpreter), "-c", "import streamlit"], env=env, cwd=code,
                               capture_output=True, timeout=30,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if probe.returncode:
            raise RuntimeError("项目环境缺少或无法加载 Streamlit。请运行 scripts/setup.ps1 安装依赖。")
        command = [str(interpreter), "-m", "streamlit", "run", str(code / "ui/app.py"),
                   "--server.address", "127.0.0.1", "--server.port", str(port),
                   "--server.headless", "true", "--browser.gatherUsageStats", "false"]
        log = state_dir / f"{args.version}-{port}.log"
        with log.open("ab") as output:
            child = subprocess.Popen(command, cwd=code, env=env, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT,
                                     creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0,
                                     start_new_session=os.name != "nt")
        record.write_text(json.dumps({"pid": child.pid, "root": str(code), "version": runtime_id, "release": manifest_version,
                                      "port": port, "command": command}, ensure_ascii=False, indent=2), encoding="utf-8")
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"审批页面启动失败，查看日志: {log}")
            if healthy(port):
                if not owned_server(record, code, port, runtime_id):
                    raise RuntimeError(f"端口响应的进程无法确认属于当前版本；查看日志: {log}")
                print(f"已启动 {manifest_version}: {url}\n源码: {code}\n日志: {log}")
                if args.version != "final" or args.profile == "cpu":
                    print(f"隔离数据: {env['OUTPUT_DIR']}（不会写入主项目审批数据）")
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
            time.sleep(0.4)
        raise RuntimeError(f"启动超过 35 秒，请查看日志后重新运行启动器: {log}")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
