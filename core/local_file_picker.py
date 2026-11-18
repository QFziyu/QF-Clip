"""Desktop file selection without putting multi-GB video in Streamlit upload RAM."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from core.video_input import VIDEO_EXTENSIONS

KINDS = frozenset({'video', 'folder', 'srt', 'json', 'image'})
PICKER_TIMEOUT = 600


def _request_options(title, kind, multiple, current_path):
    if kind not in KINDS or kind == 'folder' and multiple:
        raise ValueError('不支持的文件选择方式')
    options = {'title': str(title)[:300], 'kind': kind, 'multiple': bool(multiple)}
    if current_path:
        initial = Path(str(current_path).strip().strip('"')).expanduser()
        if not initial.is_dir():
            initial = initial.parent
        if initial.is_dir():
            options['initial_dir'] = str(initial.resolve())
    return options


def _command(request_file, result_file):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    if os.name == 'nt':
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        # A venv Python redirector spawns a child that can survive cancellation.
        return [str(powershell), '-NoProfile', '-STA', '-ExecutionPolicy', 'RemoteSigned',
                '-File', str(scripts/'pick_file_windows.ps1'),
                '-RequestFile', str(request_file), '-ResultFile', str(result_file)]
    return [sys.executable, str(scripts/'pick_video.py'),
            '--request-file', str(request_file), '--result-file', str(result_file)]


@dataclass
class LocalPickerTask:
    process: object
    directory: object
    result_file: Path
    kind: str
    multiple: bool
    started: float = field(default_factory=time.monotonic)
    timeout: float = PICKER_TIMEOUT
    result: dict | None = None
    timer: object = None
    lock: object = field(default_factory=threading.RLock, repr=False)


def start_local_picker(title='选择文件', kind='video', multiple=False, current_path=''):
    """Open the native window and return immediately; callers poll from the UI."""
    options = _request_options(title, kind, multiple, current_path)
    directory = tempfile.TemporaryDirectory(prefix='fcf-file-picker-')
    request_file = Path(directory.name)/'request.json'
    result_file = Path(directory.name)/'result.json'
    request_file.write_text(json.dumps(options, ensure_ascii=False), encoding='utf-8')
    try:
        process = subprocess.Popen(_command(request_file, result_file),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError as exc:
        directory.cleanup()
        raise RuntimeError('系统文件窗口未能启动，可展开高级路径输入') from exc
    task = LocalPickerTask(process, directory, result_file, kind, bool(multiple))
    # Close a stale dialog even if the browser disappears before polling.
    task.timer = threading.Timer(task.timeout, lambda: _expire(task))
    task.timer.daemon = True
    task.timer.start()
    return task


def _finish(task, result):
    task.result = result
    if task.timer is not None:
        task.timer.cancel()
    task.directory.cleanup()
    return dict(result)


def _stop_process(task):
    if task.process.poll() is None:
        task.process.terminate()
    try:
        task.process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        task.process.kill()
        task.process.communicate(timeout=2)


def _expire(task):
    with task.lock:
        if task.result is not None:
            return dict(task.result)
        try:
            _stop_process(task)
        except (OSError, subprocess.SubprocessError):
            pass
        return _finish(task, {'status':'error', 'value':None, 'error':'选择文件超时，窗口已关闭，请重新点击打开文件'})


def _selected_result(payload, kind, multiple):
    if not isinstance(payload, dict):
        raise ValueError('系统文件选择结果无效')
    if payload.get('error'):
        return {'status':'error', 'value':None, 'error':'系统文件窗口未能完成：' + str(payload['error'])[:300]}
    result_key = 'paths' if multiple else 'path'
    if result_key not in payload:
        raise ValueError('系统文件选择结果无效')
    value = payload[result_key]
    if multiple and not isinstance(value, list):
        raise ValueError('系统文件选择结果无效')
    if value is None and not multiple or multiple and value == []:
        return {'status':'cancelled', 'value':None}
    values = value if multiple else [value]
    if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError('系统文件选择结果无效')
    selected = []
    for item in values:
        path = Path(item).expanduser().resolve()
        if not (path.is_dir() if kind == 'folder' else path.is_file()):
            raise ValueError('选中的文件已移动或无法访问，请重新选择')
        selected.append(str(path))
    return {'status':'selected', 'value':selected if multiple else selected[0]}


def poll_local_picker(task):
    """Return pending/selected/cancelled/error without waiting for user input."""
    with task.lock:
        if task.result is not None:
            return dict(task.result)
        if time.monotonic()-task.started >= task.timeout:
            return _expire(task)
        if task.process.poll() is None:
            return {'status':'pending', 'value':None}
        try:
            stdout, _ = task.process.communicate(timeout=1)
            if task.result_file.is_file():
                payload = json.loads(task.result_file.read_text(encoding='utf-8-sig'))
            else:
                payload = json.loads(stdout.decode('utf-8-sig') if isinstance(stdout, bytes) else stdout)
            result = _selected_result(payload, task.kind, task.multiple)
            if task.process.returncode and result['status'] != 'error':
                result = {'status':'error', 'value':None, 'error':'系统文件窗口未能启动，可展开高级路径输入'}
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            result = {'status':'error', 'value':None, 'error':'系统文件窗口未能完成，可重新打开或展开高级路径输入'}
        return _finish(task, result)


def cancel_local_picker(task):
    """Cancel this owned helper; retain all previous user selections."""
    with task.lock:
        if task.result is not None:
            return dict(task.result)
        try:
            _stop_process(task)
        except (OSError, subprocess.SubprocessError):
            pass
        return _finish(task, {'status':'cancelled', 'value':None})


def resolve_video_path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('请先选择比赛录像和主播录像')
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    path = Path(value).expanduser().resolve()
    if path.is_file():
        return str(path)
    # Accept a copied file path with its extension omitted, only when unambiguous.
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        matches = [Path(str(path) + ext) for ext in sorted(VIDEO_EXTENSIONS) if Path(str(path) + ext).is_file()]
        if len(matches) == 1:
            return str(matches[0])
        if len(matches) > 1:
            raise ValueError('存在多个同名视频，请点击选择文件，或填写完整后缀')
    raise FileNotFoundError(f'视频不存在：{path}')


def choose_video(title='选择录像', current_path='', multiple=False):
    selected = choose_local(title, 'video', multiple, current_path)
    if multiple:
        return [resolve_video_path(p) for p in selected]
    return resolve_video_path(selected) if selected else None


def choose_videos(title='按顺序添加主播录像', current_path=''):
    return choose_video(title, current_path, multiple=True)


def choose_local(title='选择文件', kind='video', multiple=False, current_path=''):
    """Blocking compatibility API; main workbench uses start/poll instead."""
    task = start_local_picker(title, kind, multiple, current_path)
    try:
        while True:
            result = poll_local_picker(task)
            if result['status'] == 'error':
                raise RuntimeError(result['error'])
            if result['status'] != 'pending':
                return result['value'] if result['status'] == 'selected' else ([] if multiple else None)
            time.sleep(.05)
    finally:
        if task.result is None:
            cancel_local_picker(task)


def open_folder(path):
    import os
    folder = Path(path).resolve()
    if not folder.is_dir():
        raise FileNotFoundError('输出文件夹尚不存在')
    if os.name == 'nt':
        os.startfile(str(folder))
    else:
        subprocess.Popen(['xdg-open', str(folder)])
