"""Launch the independently paired factory/publisher and optional workbench."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import load_config
from core.publisher_client import ensure_configuration
from core.releases import current_environment
from core.version import RELEASE, VERSION
from scripts.launch_automation import start_service


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', choices=['gpu', 'cpu'], default='gpu')
    parser.add_argument('--services-only', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    version = 'final'
    code, env = current_environment(ROOT, args.profile)
    try:
        previous = dict(os.environ)
        try:
            os.environ.update(env)
            cfg = load_config(code / 'config/settings.yaml')
        finally:
            os.environ.clear(); os.environ.update(previous)
        state = Path(cfg['output_dir']) / '.automation'
        connection = ensure_configuration(cfg, args.profile)
        path = state / 'connection.json'
        python = str(ROOT / '.venv/Scripts/python.exe')
        publisher = ROOT.parent / 'FootballPublisher'
        if not (publisher / 'main.py').is_file():
            raise RuntimeError('缺少独立 FootballPublisher 源码，请使用完整发布包')
        env.update(PYTHONUTF8='1', FCF_PROFILE=args.profile, FFMPEG_PATH=cfg['ffmpeg'], FFPROBE_PATH=cfg['ffprobe'])
        runtime = publisher / 'runtime' / args.profile
        runtime.mkdir(parents=True, exist_ok=True)
        start_service('factory', [python, str(code / 'scripts/factory_service.py'), '--connection', str(path)],
            code, env, connection['factory'], state, 'football-factory', args.profile, VERSION)
        start_service('publisher', [python, str(publisher / 'main.py'), '--connection', str(path),
            '--database', str(runtime / 'publisher.sqlite3'), '--profile', args.profile],
            publisher, env, connection['publisher'], state, 'football-publisher', args.profile, '1.1.0')
        if not args.services_only:
            command = [python, str(ROOT / 'scripts/launch.py'), '--profile', args.profile]
            if args.no_browser:
                command.append('--no-browser')
            if subprocess.run(command, cwd=ROOT, env=env).returncode:
                raise RuntimeError('发布服务已启动，工作台启动失败，请检查工作台日志')
        print('发布服务已就绪；在设置中连接抖音账号，在第五步选择导出并发布。')
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print('发布启动失败：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
