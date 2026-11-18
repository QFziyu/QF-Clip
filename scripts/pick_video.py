"""Native file dialog in its own GUI thread/process; never upload or copy a video."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _native_windows_choice(title, initial_dir=None, self_test=False, multiple=False, kind='video'):
    """CLI compatibility entry; Streamlit launches this PowerShell host directly."""
    with tempfile.TemporaryDirectory(prefix='fcf-picker-cli-') as directory:
        request_file, result_file = Path(directory)/'request.json', Path(directory)/'result.json'
        request_file.write_text(json.dumps(dict(title=title, initial_dir=initial_dir,
            self_test=self_test, multiple=multiple, kind=kind), ensure_ascii=False), encoding='utf-8')
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        command = [str(powershell), '-NoProfile', '-STA', '-ExecutionPolicy', 'RemoteSigned',
            '-File', str(Path(__file__).with_name('pick_file_windows.ps1')),
            '-RequestFile', str(request_file), '-ResultFile', str(result_file)]
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=600,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if not result_file.is_file():
            raise RuntimeError('Native file window did not return a result.')
        payload = json.loads(result_file.read_text(encoding='utf-8-sig'))
        if result.returncode or payload.get('error'):
            raise RuntimeError(payload.get('error', 'Native file window could not open.'))
        return payload


def pick_video(title, initial_dir=None, self_test=False, multiple=False, kind='video'):
    if os.name == 'nt':
        return _native_windows_choice(title, initial_dir, self_test, multiple, kind)
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    root.update()
    try:
        if self_test:
            return {'ready': True}
        options = {'parent': root, 'title': title}
        if initial_dir and Path(initial_dir).is_dir():
            options['initialdir'] = str(Path(initial_dir))
        if kind == 'folder':
            selected = filedialog.askdirectory(**options)
            return {'path': str(Path(selected).resolve()) if selected else None}
        filters = {'video': ('视频文件','*.mp4 *.mkv *.flv *.mov *.webm *.ts'),
                   'srt': ('SRT 字幕','*.srt'), 'json': ('转录 JSON','*.json'),
                   'image': ('封面图片','*.jpg *.jpeg *.png *.webp')}
        options['filetypes'] = [filters[kind], ('所有文件','*.*')]
        if multiple:
            selected = filedialog.askopenfilenames(**options)
            return {'paths': [str(Path(path).resolve()) for path in selected]}
        selected = filedialog.askopenfilename(**options)
        return {'path': str(Path(selected).resolve()) if selected else None}
    finally:
        root.destroy()


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser()
    parser.add_argument('--title', default='选择录像')
    parser.add_argument('--initial-dir')
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--multiple', action='store_true')
    parser.add_argument('--kind', choices=['video','folder','srt','json','image'], default='video')
    parser.add_argument('--request-file', type=Path)
    parser.add_argument('--result-file', type=Path)
    args = parser.parse_args()
    try:
        options = json.loads(args.request_file.read_text(encoding='utf-8')) if args.request_file else {}
        result = pick_video(options.get('title', args.title), options.get('initial_dir', args.initial_dir),
            options.get('self_test', args.self_test), options.get('multiple', args.multiple), options.get('kind', args.kind))
        if args.result_file:
            partial = args.result_file.with_suffix('.partial')
            partial.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            partial.replace(args.result_file)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
