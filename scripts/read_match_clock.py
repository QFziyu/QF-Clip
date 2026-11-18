"""Read a scoreboard image, or automatically locate events in a local recording."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import load_config
from core.editing_options import normalize_editing_options
from core.match_clock import read_clock_frames, scan_match_clock
from core.match_events import parse_event_text
from core.utils import file_signature, fingerprint
from core.video_input import probe_video


def main():
    parser = argparse.ArgumentParser(description='自动读取足球记分牌；不需要填写录像时间')
    parser.add_argument('source', help='本地录像或记分牌图片路径')
    parser.add_argument('--events', default='', help='例如 G23:10 R40 / G61 V90+3:20')
    args = parser.parse_args()
    cfg = load_config()
    source = Path(args.source).resolve()
    root = Path(cfg['cache_dir']) / 'clock_reader' / fingerprint(file_signature(source))
    root.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'):
        report = read_clock_frames([{'id': 'image', 'time': 0., 'path': str(source)}], cfg, root, root / 'api_ledger.json')
    else:
        options = normalize_editing_options({'events': parse_event_text(args.events)})
        meta = probe_video(source, cfg, require_audio=False)
        report = scan_match_clock(meta, cfg, root, options['events'])
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
