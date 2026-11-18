"""Two independent source clocks, bounded decoding, atomic picture-in-picture exports."""
from __future__ import annotations

import copy
import logging
from pathlib import Path
import tempfile

from core.audio_extract import audio_filter, extract_audio
from core.audio_sync import match_audio
from core.clip_renderer import _dimensions, _has_nvenc
from core.composite_options import normalize_composite_options, overlap_range, validate_ranges
from core.face_tracker import track_faces
from core.file_access import restore_inherited_access
from core.intro import probe_intro
from core.plan_renderer import compose_parts
from core.utils import atomic_json, file_signature, fingerprint, named_video_path, project_lock, read_json
from core.video_input import probe_video, run_media
from storage.composites import CompositeStore

LOG = logging.getLogger(__name__)


def create_composite(match, commentator, cfg, match_track=0, commentator_track=0):
    sources = []
    for path, track in ((match, match_track), (commentator, commentator_track)):
        if isinstance(path, (list, tuple)):
            from core.source_sequence import prepare_sequence
            sources.append(prepare_sequence(path, cfg, track))
            continue
        meta = probe_video(path, cfg, track, require_audio=False)
        meta.pop('raw_probe', None)
        meta['source_signature'] = file_signature(meta['source'])
        sources.append(meta)
    identity = fingerprint({'sources': [s['source_signature'] for s in sources], 'tracks': [match_track, commentator_track], 'version': 1})
    document = {'id': identity, 'match': sources[0], 'commentator': sources[1],
                'options': normalize_composite_options(), 'outputs': []}
    with CompositeStore(cfg['database']) as db:
        return db.insert_composite(document)


def _load(identity, cfg):
    with CompositeStore(cfg['database']) as db:
        document = db.get_composite(identity)
    if not document:
        raise ValueError('找不到双路合成项目')
    for name in ('match', 'commentator'):
        meta = document[name]
        if file_signature(meta['source']) != meta['source_signature']:
            raise ValueError(f'{name} 素材已改变，请重新创建合成项目')
        for part in meta.get('parts', []):
            if file_signature(part['source']) != part['source_signature']:
                raise ValueError('拼接用的原始主播素材已改变，请重新创建合成项目')
    root = Path(cfg['cache_dir']) / 'composites' / identity
    root.mkdir(parents=True, exist_ok=True)
    return document, root


def synchronize_visual(identity, cfg, search, analyze=True):
    from core.visual_sync import visual_alignment
    document, root = _load(identity, cfg)
    with project_lock(root):
        result = visual_alignment(document['match'], document['commentator'], cfg, root, search, analyze)
        with CompositeStore(cfg['database']) as db:
            db.update_composite(identity, visual_sync=result)
        return result


def _wav(meta, root, cfg):
    if meta['selected_audio'] is None:
        raise ValueError('自动对轴需要两路都有音轨；无音轨时请手动校准')
    destination = root / (fingerprint({'source': meta['source_signature'], 'track': meta['audio_track']}) + '.wav')
    if not destination.is_file():
        extract_audio(meta, destination, cfg)
    return destination


def synchronize_composite(identity, cfg, semantic=False, allow_cpu=False):
    document, root = _load(identity, cfg)
    with project_lock(root):
        waves = [_wav(document[name], root, cfg) for name in ('match', 'commentator')]
        result = match_audio(*waves, root)
        if semantic and result['confidence'] != 'high':
            from core.semantic_sync import semantic_alignment
            try:
                from core.llm_settings import configured
                if not configured(cfg['openai']):
                    raise ValueError('请先配置 OPENAI_API_KEY 和 OPENAI_MODEL，再启用语义对轴')
                transcripts = [_transcript(document[name], wav, root / name, cfg, allow_cpu)
                               for name, wav in zip(('match', 'commentator'), waves)]
                advisory = semantic_alignment(*transcripts, cfg, root / 'sync_api_ledger.json')
                result['semantic'] = advisory
                if result['confidence'] == 'low' and advisory['offset_seconds'] is not None:
                    result = {**advisory, 'audio_result': result}
            except Exception as exc:
                # Keep the completed fingerprint result if the optional API fails.
                result['semantic_error'] = str(exc).replace(cfg['openai'].get('api_key') or '\0', '[redacted]')
        with CompositeStore(cfg['database']) as db:
            db.update_composite(identity, sync=result)
        atomic_json(root / 'sync.json', result)
        return result


def _transcript(meta, wav, directory, cfg, allow_cpu):
    from core.transcriber import transcribe_audio, validate_transcript, asr_cache_key
    with CompositeStore(cfg['database']) as db:
        for project in db.list_projects():
            if project['metadata'].get('source_signature') == meta['source_signature'] and project['metadata'].get('selected_audio') == meta['selected_audio']:
                candidate = Path(project['project_dir']) / 'transcript.json'
                if candidate.is_file():
                    try:
                        data = validate_transcript(read_json(candidate), meta['duration'])
                        # A new local-ASR task must not silently reuse an old
                        # Whisper transcript from another project.
                        if cfg['asr'].get('engine') == 'zipformer_http' and data.get('cache_key') != asr_cache_key(meta, cfg):
                            continue
                        return data
                    except (ValueError, OSError):
                        LOG.warning('已存在转录不可用，将使用独立对轴缓存')
    return transcribe_audio(wav, meta, cfg, directory, allow_cpu=allow_cpu)


def _render_segment(document, segment, options, cfg, destination, root, preview):
    width, height = _dimensions(cfg, 'horizontal', preview)
    duration = segment['end'] - segment['start']
    command = [cfg['ffmpeg'], '-v', 'error', '-nostdin', '-y', '-copyts']
    starts, filters = [], []
    for index, name in enumerate(('match', 'commentator')):
        meta = document[name]
        relative = segment['start'] + (options['offset_seconds'] if index else 0)
        absolute = meta['timeline_origin'] + relative
        starts.append(absolute)
        seek = absolute - meta['format_start'] - 2
        if seek > 0:
            command += ['-ss', str(seek)]
        command += ['-i', meta['source']]
        filters.append(f'[{index}:{meta["video_stream"]}]trim=start={absolute:.9f}:end={absolute+duration:.9f},setpts=PTS-({absolute:.9f})/TB,fps={cfg["render"]["fps"]},setsar=1[v{index}]')
    filters.append(f'[v0]scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2[main]')
    pipw = max(16, round(width * options['width_ratio'] / 2) * 2)
    piph = max(16, round(pipw * 9 / 16 / 2) * 2)
    tracking = {'mode': 'fit'}
    if options['layout'] == 'face':
        tracking = track_faces(document['commentator'], segment['start'] + options['offset_seconds'],
                               segment['end'] + options['offset_seconds'], cfg, root, options['face_sample_seconds'])
    if tracking['mode'] == 'face':
        points = tracking['points']
        commands = [f"{p['time']:.3f} crop@face x {p['x']}, crop@face y {p['y']};" for p in points[1:]]
        (destination.parent / 'face.cmd').write_text('\n'.join(commands) + '\n', encoding='ascii')
        control = 'sendcmd=f=face.cmd,' if commands else ''
        small = f'{control}crop@face={tracking["crop_width"]}:{tracking["crop_height"]}:{points[0]["x"]}:{points[0]["y"]},scale={pipw}:{piph}'
    else:
        small = f'scale={pipw}:{piph}:force_original_aspect_ratio=decrease,pad={pipw}:{piph}:(ow-iw)/2:(oh-ih)/2'
    filters.append(f'[v1]{small}[small]')
    margin = round(min(width, height) * options['margin_ratio'])
    x = str(margin) if options['position'].endswith('left') else f'W-w-{margin}'
    y = str(margin) if options['position'].startswith('top') else f'H-h-{margin}'
    filters.append(f'[main][small]overlay=x={x}:y={y}:eof_action=pass:shortest=1[outv]')
    selected = (0, 1) if options['audio'] == 'mix' else ((0,) if options['audio'] == 'match' else (1,))
    mode = options['match_audio_mode']
    if mode == 'mute':
        selected = tuple(index for index in selected if index != 0)
    separated = None
    if mode == 'remove_vocals' and 0 in selected and options['match_volume'] > 0:
        from core.vocal_remover import prepare_background
        background, separated = prepare_background(document['match'], segment['start'], segment['end'], cfg, root, options['vocal_device'])
        command += ['-i', str(background)]
        tracking.setdefault('audio_processing', separated)
    for index in selected:
        meta = document['match' if index == 0 else 'commentator']
        if meta['selected_audio'] is None:
            raise ValueError('所选声音来源没有音轨，请更换音轨/声音来源')
        gain = options['match_volume' if index == 0 else 'commentator_volume']
        if index == 0 and separated is not None:
            filters.append(f'[2:a:0]aresample=48000,apad,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS,volume={gain}[a0]')
        else:
            filters.append(f'[{index}:{meta["selected_audio"]}]{audio_filter(starts[index], duration, 48000)},volume={gain}[a{index}]')
    if len(selected) == 2:
        filters.append('[a0][a1]amix=inputs=2:duration=longest:normalize=0,alimiter=level=false[outa]')
        audio = '[outa]'
    elif selected:
        audio = f'[a{selected[0]}]'
    else:
        filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={duration:.9f}[silent]')
        audio = '[silent]'
    command += ['-filter_complex_threads', '1', '-filter_complex', ';'.join(filters), '-map', '[outv]', '-map', audio,
                '-t', str(duration), '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-b:a', '192k', '-movflags', '+faststart']
    encoder = cfg['render']['encoder']
    if encoder == 'auto':
        encoder = 'h264_nvenc' if _has_nvenc(cfg['ffmpeg']) else 'libx264'
    def encode(chosen):
        run_media(command + ['-c:v', chosen, '-preset', 'p4' if chosen == 'h264_nvenc' else 'fast', '-b:v',
                             str(cfg['render']['preview_bitrate'] if preview else cfg['render']['bitrate']), str(destination)], cwd=destination.parent)
    try:
        encode(encoder)
    except RuntimeError:
        if encoder != 'h264_nvenc':
            raise
        LOG.warning('双路合成 NVENC 不可用，回退软件编码')
        encode('libx264')
    return probe_intro(destination, cfg), tracking


def render_composite(identity, cfg, options=None, segments=None, preview=False, preview_start=None):
    document, root = _load(identity, cfg)
    options = normalize_composite_options(document['options'] if options is None else options)
    if not preview and not options['sync_confirmed']:
        raise ValueError('请先预览两路同步效果并勾选「已核对同步位置」，再正式导出')
    overlap = overlap_range(document['match']['duration'], document['commentator']['duration'], options['offset_seconds'])
    ranges = validate_ranges(segments, overlap)
    if preview:
        start = overlap['start'] if preview_start is None else preview_start
        ranges = validate_ranges([{'start': start, 'end': min(start + 20, overlap['end'])}], overlap)
    key = fingerprint({'id': identity, 'options': options, 'ranges': ranges, 'preview': preview, 'render': cfg['render'], 'version': 1})
    package = Path(cfg['output_dir']) / 'composites' / identity[:12] / key[:12]
    package.mkdir(parents=True, exist_ok=True)
    with project_lock(root):
        metadata = package / 'metadata.json'
        if metadata.is_file():
            result = read_json(metadata)
            if result.get('render_key') == key and Path(result.get('video', '')).is_file():
                with CompositeStore(cfg['database']) as db:
                    db.update_composite(identity, output=result)
                return result
        destination = named_video_path(package, options['title'], identity)
        mappings, parts, warnings, audio_processing, cursor = [], [], [], [], 0.
        with tempfile.TemporaryDirectory(prefix='.pip-', dir=package) as temporary:
            temp = Path(temporary)
            for index, segment in enumerate(ranges):
                LOG.info('合成第 %d/%d 段：比赛 %.3f–%.3f 秒；主播偏移 %+.3f 秒',
                         index + 1, len(ranges), segment['start'], segment['end'], options['offset_seconds'])
                part = temp / f'part{index}.mp4'
                measured, tracking = _render_segment(document, segment, options, cfg, part, root, preview)
                duration = measured['duration']
                parts.append({'path': str(part), 'duration': duration})
                mappings.append({'output_start': cursor, 'output_end': cursor + duration,
                    'match_start': segment['start'], 'match_end': segment['end'],
                    'commentator_start': segment['start'] + options['offset_seconds'],
                    'commentator_end': segment['end'] + options['offset_seconds']})
                if document['commentator'].get('parts'):
                    from core.source_sequence import sequence_mapping
                    mappings[-1]['commentator_parts'] = sequence_mapping(document['commentator'],
                        segment['start'] + options['offset_seconds'], segment['end'] + options['offset_seconds'])
                cursor += duration
                if tracking.get('warning'):
                    warnings.append(tracking['warning'])
                if tracking.get('audio_processing'):
                    audio_processing.append(tracking['audio_processing'])
                    warnings.append(tracking['audio_processing']['note'])
                LOG.info('第 %d 段已完成，输出 %.3f 秒', index + 1, duration)
            if len(parts) == 1:
                part.replace(destination)
            else:
                local_cfg = copy.deepcopy(cfg)
                if preview:
                    local_cfg['render']['bitrate'] = cfg['render']['preview_bitrate']
                compose_parts(parts, temp / 'joined.mp4', local_cfg)
                (temp / 'joined.mp4').replace(destination)
        restore_inherited_access(destination)
        result = {'render_key': key, 'composite_id': identity, 'video': str(destination), 'preview': preview,
                  'duration': cursor, 'options': options, 'mappings': mappings, 'sources': {n: document[n] for n in ('match', 'commentator')},
                  'warnings': list(dict.fromkeys(warnings)), 'audio_processing': audio_processing}
        atomic_json(destination.with_suffix('.timeline.json'), result)
        atomic_json(metadata, result)
        with CompositeStore(cfg['database']) as db:
            db.update_composite(identity, output=result)
        return result
