"""One user entry; device and publishing are runtime choices, never old releases."""
from __future__ import annotations
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def launch_command(profile, publishing=False, no_browser=False):
    automation = ((ROOT.parent/'FootballWorkflow/main.py').is_file()
                  and (ROOT.parent/'软件下载/轻舟视频下载/server.py').is_file())
    script = 'launch_automation.py' if automation else ('launch_publishing.py' if publishing else 'launch.py')
    command = [sys.executable, str(ROOT/'scripts'/script), '--profile', profile]
    if automation and publishing:
        command.append('--publishing')
    if no_browser:
        command.append('--no-browser')
    return command


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='统一启动：当前源码，CPU/GPU，可选发布服务')
    parser.add_argument('--profile', choices=['gpu', 'cpu'])
    parser.add_argument('--publishing', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='只显示启动命令')
    args = parser.parse_args(argv)
    profile, publishing = args.profile, args.publishing
    if profile is None:
        print('足球切片工厂\n1. GPU · 本地制作（默认）\n2. CPU · 本地制作\n3. GPU · 制作与发布\n4. CPU · 制作与发布')
        try:
            choice = input('请选择 [1-4，回车选 1]：').strip() or '1'
        except (EOFError, KeyboardInterrupt):
            return 1
        if choice not in ('1', '2', '3', '4'):
            print('无效选择，请重新启动并选择 1～4。')
            return 1
        profile = 'cpu' if choice in ('2', '4') else 'gpu'
        publishing = publishing or choice in ('3', '4')
    command = launch_command(profile, publishing, args.no_browser)
    if args.dry_run:
        print(subprocess.list2cmdline(command))
        return 0
    try:
        # No model dependency crosses the independent ASR service boundary.
        sys.path.insert(0, str(ROOT))
        from core.config import load_config
        from core.local_asr import ensure_service
        ensure_service(load_config())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print('本地语音识别服务未就绪：'+str(exc)+'；工作台仍可打开，修复后可重试任务。', flush=True)
    result = subprocess.run(command, cwd=ROOT).returncode
    if result and Path(command[1]).name != 'launch.py':
        print('辅助服务未全部就绪，继续打开本地制作工作台。视频来源或发布功能需待对应服务恢复。', flush=True)
        fallback = [sys.executable, str(ROOT/'scripts/launch.py'), '--profile', profile]
        if args.no_browser:
            fallback.append('--no-browser')
        return subprocess.run(fallback, cwd=ROOT).returncode
    return result


if __name__ == '__main__':
    raise SystemExit(main())
