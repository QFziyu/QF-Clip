"""Generated-media acceptance check; --live-api explicitly opts into one vision call."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import load_config
from core.match_clock import _frame, read_clock_frames, verified_pair
from core.utils import atomic_json
from core.video_input import probe_video, run_media


def verify(live_api=False, config_root=None):
    from PIL import Image, ImageDraw, ImageFont
    import core.config as config_module
    previous = config_module.ROOT
    try:
        if config_root:
            config_module.ROOT = Path(config_root)
        cfg = load_config()
    finally:
        config_module.ROOT = previous
    root = ROOT / 'output' / 'clock_acceptance'
    root.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf', 34)
    small = ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf', 28)

    def picture(clock, phase='1ST', extra='', allowance='', index=0):
        frame = Image.new('RGB', (1280, 720), '#206f35')
        draw = ImageDraw.Draw(frame)
        for x in range(0, 1280, 160):
            draw.rectangle((x, 160, x + 78, 720), fill='#247a3b')
        draw.rectangle((100, 200, 1180, 680), outline='white', width=3)
        draw.line((640, 200, 640, 680), fill='white', width=3)
        draw.ellipse((550, 350, 730, 530), outline='white', width=3)
        for i in range(12):
            x, y = 150 + (i * 79 + index * 3) % 980, 250 + i % 4 * 95
            draw.ellipse((x, y, x + 12, y + 18), fill='red' if i < 6 else 'white')
        draw.rectangle((36, 36, 1000, 102), fill='#101a24')
        draw.text((50, 50), f'{phase}    HOME  1 - 0  AWAY', font=font, fill='white')
        draw.text((760, 50), clock, font=font, fill='white')
        if extra:
            draw.rectangle((750, 106, 1000, 158), fill='#101a24')
            draw.text((770, 114), extra, font=small, fill='white')
        if allowance:
            draw.rectangle((620, 106, 735, 158), fill='#f4d24c')
            draw.text((650, 114), allowance, font=small, fill='black')
        return frame

    for i in range(12):
        picture(f'23:{10 + i:02d}', index=i).save(root / f'input_{i:02d}.png')
    source = root / '有起始偏移的比赛测试.mp4'
    run_media([cfg['ffmpeg'], '-v', 'error', '-nostdin', '-y', '-framerate', '1', '-i', str(root / 'input_%02d.png'),
               '-r', '10', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-output_ts_offset', '7', str(source)], timeout=60)
    meta = probe_video(source, cfg, require_audio=False)
    assert abs(meta['timeline_origin'] - 7) < .01, meta['timeline_origin']
    assert abs(meta['duration'] - 12) < .2, meta['duration']
    frames = [_frame(meta, t, root, cfg) for t in (4., 8.)]
    for frame in frames:
        assert Path(frame['path']).is_file()
    split = root / 'split_stoppage.jpg'
    picture('45:00', extra='02:10', allowance='+4').save(split, quality=90)
    continuous = root / 'continuous_stoppage.jpg'
    picture('93:20', phase='2ND', allowance='+4').save(continuous, quality=90)
    frames.extend([{'id': 'split', 'time': 0., 'path': str(split)}, {'id': 'continuous', 'time': 0., 'path': str(continuous)}])
    report = {'real_ffmpeg': True, 'nonzero_pts_origin': meta['timeline_origin'], 'duration': meta['duration'],
              'live_api': live_api, 'frames': frames}
    if live_api:
        readings = read_clock_frames(frames, cfg, root, root / 'api_ledger.json')
        by_id = {r['id']: r for r in readings}
        assert by_id[frames[0]['id']].get('match_seconds') == 1394, readings
        assert by_id[frames[1]['id']].get('match_seconds') == 1398, readings
        assert verified_pair(by_id[frames[0]['id']], by_id[frames[1]['id']]) == -1390, readings
        assert by_id['split'].get('match_seconds') == 2830, readings
        assert by_id['continuous'].get('match_seconds') == 5600, readings
        report['readings'] = readings
    report['success'] = True
    atomic_json(root / 'acceptance.json', report)
    print({'success': True, 'live_api': live_api, 'report': str(root / 'acceptance.json')})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-api', action='store_true')
    parser.add_argument('--config-root', type=Path)
    args = parser.parse_args()
    verify(args.live_api, args.config_root)
