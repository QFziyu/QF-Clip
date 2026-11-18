"""Ordered, resumable source normalization. Original media are never modified."""
from __future__ import annotations

import logging
import math
from pathlib import Path
import tempfile

from core.audio_extract import audio_filter
from core.clip_renderer import _has_nvenc
from core.file_access import restore_inherited_access
from core.intro import _video_filter
from core.utils import atomic_json, file_signature, fingerprint, project_lock, read_json
from core.video_input import probe_video, run_media

LOG = logging.getLogger(__name__)


def source_paths(paths):
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 50:
        raise ValueError('主播素材需要 1～50 个本地视频，列表顺序就是拼接顺序')
    result = []
    for path in paths:
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ValueError('素材路径无效')
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f'素材不存在：{path}')
        result.append(str(path))
    if len(set(result)) != len(result):
        raise ValueError('同一素材不能重复添加，请检查主播素材顺序')
    return result


def prepare_sequence(paths, cfg, audio_track=0):
    paths = source_paths(paths)
    if isinstance(audio_track, bool) or not isinstance(audio_track, int) or audio_track < 0:
        raise ValueError('音轨必须是非负整数')
    metas = []
    for path in paths:
        meta = probe_video(path, cfg, audio_track, require_audio=False)
        meta.pop('raw_probe', None)
        meta['source_signature'] = file_signature(path)
        metas.append(meta)
    fps = cfg['render']['fps']
    # Retain the first source's aspect ratio, cap working copies at 1080p.
    ratio = min(1., 1920 / metas[0]['width'], 1080 / metas[0]['height'])
    width, height = [max(2, round(metas[0][k] * ratio / 2) * 2) for k in ('width', 'height')]
    spec = {'signatures': [m['source_signature'] for m in metas], 'track': audio_track,
            'width': width, 'height': height, 'fps': fps, 'version': 1}
    root = Path(cfg['cache_dir']) / 'source_sequences' / fingerprint(spec)
    root.mkdir(parents=True, exist_ok=True)
    with project_lock(root):
        manifest = root / 'sequence.json'
        if manifest.is_file():
            cached = read_json(manifest)
            if Path(cached['source']).is_file() and file_signature(cached['source']) == cached['source_signature']:
                return cached
        parts, cursor, entries = [], 0., ['ffconcat version 1.0']
        for i, meta in enumerate(metas):
            frames = max(1, math.ceil(meta['duration'] * fps - 1e-7))
            duration = frames / fps
            part = root / f'part_{i:03d}.nut'
            LOG.info('主播首尾拼接：标准化 %d/%d · %s', i + 1, len(metas), Path(meta['source']).name)
            if not part.is_file():
                graph = _video_filter(0, meta, width, height, fps, frames, 'horizontal')
                if meta['selected_audio'] is None:
                    graph += f'anullsrc=r=48000:cl=stereo,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[a0]'
                else:
                    graph += f'[0:{meta["selected_audio"]}]' + audio_filter(meta['timeline_origin'], duration, 48000) + ',aformat=channel_layouts=stereo[a0]'
                base = [cfg['ffmpeg'], '-v', 'error', '-nostdin', '-y', '-copyts', '-i', meta['source'],
                        '-filter_complex_threads', '2', '-filter_complex', graph, '-map', '[v0]', '-map', '[a0]',
                        '-t', str(duration), '-r', str(fps)]
                encoder = cfg['render']['encoder']
                if encoder == 'auto':
                    encoder = 'h264_nvenc' if _has_nvenc(cfg['ffmpeg']) else 'libx264'
                with tempfile.TemporaryDirectory(prefix='.normalize-', dir=root) as temp:
                    staging = Path(temp) / 'part.nut'
                    def encode(chosen):
                        run_media(base + ['-c:v', chosen, '-preset', 'p4' if chosen == 'h264_nvenc' else 'fast',
                            '-b:v', str(cfg['render']['bitrate']), '-pix_fmt', 'yuv420p',
                            '-c:a', 'pcm_s16le', '-ar', '48000', '-ac', '2', str(staging)])
                    try:
                        encode(encoder)
                    except RuntimeError:
                        if encoder != 'h264_nvenc':
                            raise
                        LOG.warning('主播拼接 NVENC 不可用，回退软件编码')
                        encode('libx264')
                    staging.replace(part)
            parts.append({**meta, 'sequence_start': cursor, 'sequence_end': cursor + duration,
                          'normalized_duration': duration, 'silent': meta['selected_audio'] is None})
            cursor += duration
            entries += [f'file {part.name}', f'duration {duration:.9f}']
        (root / 'sequence.ffconcat').write_text('\n'.join(entries) + '\n', encoding='utf-8')
        destination = root / '主播顺序拼接.mkv'
        staging = root / 'joining.mkv'
        LOG.info('连接 %d 段主播录像，总长 %.3f 秒（视频/PCM 音频不再次编码）', len(parts), cursor)
        run_media([cfg['ffmpeg'], '-v', 'error', '-nostdin', '-y', '-f', 'concat', '-safe', '1',
                   '-i', 'sequence.ffconcat', '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy', str(staging)], cwd=root)
        staging.replace(destination)
        for meta in metas:
            if file_signature(meta['source']) != meta['source_signature']:
                raise ValueError('原始素材在拼接期间改变，请等待录制结束后重试')
        restore_inherited_access(destination)
        result = probe_video(destination, cfg, 0, require_audio=False)
        result.pop('raw_probe', None)
        result.update(source_signature=file_signature(destination), parts=parts, sequence_spec=spec,
                      sequence_duration=cursor)
        atomic_json(manifest, result)
        return result


def sequence_mapping(meta, start, end):
    """Resolve joined time to original-file time, including boundary padding."""
    result = []
    for part in meta.get('parts', []):
        a, b = max(start, part['sequence_start']), min(end, part['sequence_end'])
        if b > a:
            local_a, local_b = a - part['sequence_start'], b - part['sequence_start']
            result.append({'source': part['source'], 'source_signature': part['source_signature'],
                'sequence_start': a, 'sequence_end': b, 'source_start': min(local_a, part['duration']),
                'source_end': min(local_b, part['duration']), 'padded_tail_seconds': max(0., local_b - part['duration'])})
    return result
