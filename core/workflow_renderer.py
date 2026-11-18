"""Shared selected-range renderer. Dual sources never need a full-match render."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile

from core.utils import fingerprint, file_signature, atomic_json, read_json, named_video_path, project_lock
from core.export_options import default_export_options, effective_export_config
from core.clip_renderer import render_clip, _dimensions
from core.plan_renderer import compose_parts
from core.intro import probe_intro, prepend_intro
from core.subtitle import composition_cues, cues_srt, parse_srt, write_subtitles
from core.file_access import restore_inherited_access


def remap_manual(text, segments, measured):
    """Preserve manual wording; rebase cue boundaries on actual encoded frames."""
    nominal = output = 0.
    cues = parse_srt(text, sum(s['end'] - s['start'] for s in segments))
    result = []
    for segment, length in zip(segments, measured):
        duration = segment['end'] - segment['start']
        for cue in cues:
            a, b = max(nominal, cue['start']), min(nominal + min(duration, length), cue['end'])
            # Skip fragments that collapse at SRT millisecond precision.
            if b > a and round((output + b - nominal) * 1000) > round((output + a - nominal) * 1000):
                result.append({**cue, 'start': output + a - nominal, 'end': output + b - nominal})
        nominal += duration
        output += length
    return cues_srt(result)


def render_workflow_item(workspace_id, item, cfg, orientation='horizontal', preview=False, snapshot=None):
    from core.workflow import item_transcript, visual_signature
    from core.video_input import probe_video
    from core.compositor import _render_segment, _load
    source = item['source']
    settings = (snapshot or {}).get('packaging', {})
    options = settings.get('export_options') or default_export_options(cfg)
    cfg = effective_export_config(cfg, options)
    cfg['render'].update((snapshot or {}).get('render', {}))
    if preview:
        cfg['render'][orientation] = list(_dimensions(cfg, orientation, True))
        cfg['render']['bitrate'] = cfg['render']['preview_bitrate']
    root = Path(cfg['cache_dir']) / 'workflow_renders' / workspace_id
    root.mkdir(parents=True, exist_ok=True)
    if source['kind'] == 'dual':
        document, dual_root = _load(source['document']['id'], cfg)
        if any(document[n]['source_signature'] != source['document'][n]['source_signature'] for n in ('match','commentator')):
            raise ValueError('双路原始素材发生变化，请重新导入')
        meta = document['match']
    else:
        meta = source['meta']
        saved = meta.get('source_signature')
        if saved and file_signature(meta['source']) != saved:
            raise ValueError('源素材已经变化，请重新导入')
        for part in meta.get('parts', []):
            if file_signature(part['source']) != part['source_signature']:
                raise ValueError('顺序拼接的原始素材已改变')
    intro = None
    if not preview and options['intro']['enabled']:
        intro = probe_intro(options['intro']['path'], cfg)
    automatic_text = ''
    subtitle_review = {'status':'manual' if item.get('subtitle_text') is not None else 'disabled'}
    frozen = (snapshot or {}).get('frozen_subtitles', {}).get(item['id'])
    if not preview and settings.get('subtitles', True) and frozen is None:
        from core.workflow import freeze_subtitles
        frozen = freeze_subtitles([item], settings, cfg)[item['id']]
    if frozen is not None:
        from core.deliveries import text_digest
        if frozen['sha256'] != text_digest(frozen['text']):
            raise ValueError('冻结字幕校验失败')
        automatic_text, subtitle_review = frozen['text'], frozen['review']
    elif settings.get('subtitles',True) and item.get('subtitle_text') is None and not (preview and item.get('subtitle_stale')):
        from core.subtitle_review import item_cues
        automatic_cues,subtitle_review=item_cues(item,cfg,request=preview,
            enabled=settings.get('proofread_subtitles',cfg.get('subtitle_review',{}).get('enabled',False)))
        automatic_text=cues_srt(automatic_cues)
    key = fingerprint({'item': visual_signature(item) if preview else item, 'orientation': orientation, 'preview': preview, 'render': cfg['render'],
                       'packaging': settings, 'intro': intro, 'automatic_subtitles':automatic_text,'version': 5})
    base = Path((snapshot or {}).get('output_dir') or cfg['output_dir'])
    package = (root / 'previews' if preview else base / 'workflow_exports' / workspace_id[:16]) / item['id'] / f'{orientation}_{key[:12]}'
    package.mkdir(parents=True, exist_ok=True)
    with project_lock(root):
        if not preview and not (package/'verification.json').is_file():
            # A crash before the completion marker leaves an unfinished package.
            # Preserve its bytes and recover into a new directory; never reuse a
            # partially written subtitle or timeline as a finished deliverable.
            recovery = sorted(package.parent.glob(package.name+'_recovery_*'))
            completed = [p for p in recovery if (p/'metadata.json').is_file() and (p/'verification.json').is_file()]
            if completed:
                package = completed[-1]
            elif any(package.iterdir()):
                for attempt in range(1, 10000):
                    candidate = package.with_name(package.name+f'_recovery_{attempt:04}')
                    if not candidate.exists():
                        candidate.mkdir()
                        package = candidate
                        break
                else:
                    raise ValueError('未完成导出过多，请检查成品目录')
        metadata_path = package / 'metadata.json'
        if metadata_path.is_file():
            result = read_json(metadata_path)
            if Path(result['video']).is_file():
                if not preview:
                    from core.deliveries import verify_render_package
                    verify_render_package(result, cfg)
                return result
        destination = named_video_path(package, item['title'], item['id'])
        segments = copy.deepcopy(item['segments'])
        # Preview lowers resolution/bitrate, never truncates the editorial timeline.
        if meta.get('virtual_timeline'):
            from core.batch_timeline import split_ranges
            cuts = split_ranges(meta, segments)
        else:
            cuts = [{'meta': meta, **s, 'timeline_start': s['start'], 'timeline_end': s['end']} for s in segments]
        render_segments = [{'start': c['timeline_start'], 'end': c['timeline_end']} for c in cuts]
        if not cuts:
            raise ValueError('没有可渲染的素材区间')
        with tempfile.TemporaryDirectory(prefix='.workflow-', dir=package) as temporary:
            temp = Path(temporary)
            parts, mappings, lengths = [], [], []
            cursor = 0.
            for index, cut in enumerate(cuts):
                segment = {'start': cut['start'], 'end': cut['end']}
                part_meta = cut['meta']
                part = temp / f'p{index}.mp4'
                if source['kind'] == 'dual':
                    _render_segment(document, segment, source['options'], cfg, part, dual_root, preview)
                    # Preserve the whole PiP image in both orientations.
                    if orientation == 'vertical':
                        normalized = temp / f'v{index}.mp4'
                        normalized_meta = probe_video(part, cfg, require_audio=False)
                        render_clip(normalized_meta, {'start': 0., 'end': segment['end']-segment['start']}, normalized, cfg,
                                    orientation=orientation, preview=preview)
                        part = normalized
                else:
                    render_clip(part_meta, segment, part, cfg, orientation=orientation, preview=preview)
                measured = probe_intro(part, cfg)['duration']
                parts.append({'path': str(part), 'duration': measured})
                lengths.append(measured)
                mapping = dict(output_start=cursor, output_end=cursor+measured, source_start=segment['start'], source_end=segment['end'],
                               source=part_meta['source'], timeline_origin=part_meta.get('timeline_origin', 0),
                               timeline_start=cut['timeline_start'], timeline_end=cut['timeline_end'])
                if source['kind'] == 'dual':
                    offset = source['options']['offset_seconds']
                    mapping.update(match_start=segment['start'], match_end=segment['end'],
                                   commentator_start=segment['start']+offset, commentator_end=segment['end']+offset)
                    seq_meta = document['commentator']
                    if seq_meta.get('parts'):
                        from core.source_sequence import sequence_mapping
                        mapping['commentator_parts'] = sequence_mapping(seq_meta, segment['start']+offset, segment['end']+offset)
                elif meta.get('parts') and not meta.get('virtual_timeline'):
                    from core.source_sequence import sequence_mapping
                    mapping['source_parts'] = sequence_mapping(meta, segment['start'], segment['end'])
                mappings.append(mapping)
                cursor += measured
            text = ''
            if settings.get('subtitles', True) and not (preview and item.get('subtitle_stale')):
                text = remap_manual(item['subtitle_text'] if item.get('subtitle_text') is not None else automatic_text, render_segments, lengths)
            subs = write_subtitles({'segments': []}, {'start': 0, 'end': cursor}, temp/'body.srt', temp/'body.ass',
                                   cfg, orientation, edited_srt=text)
            body = temp/'body.mp4'
            actual = compose_parts(parts, body, cfg, subs['ass'] if cfg['render']['burn_subtitles'] and text else None)
            if abs(actual['duration']-cursor) > 1/cfg['render']['fps']+.03:
                raise ValueError('拼接边界校验失败，未发布成品')
            offset = 0.
            if intro:
                assembly = prepend_intro(body, options['intro']['path'], destination, cfg, orientation)
                if assembly['intro_signature'] != intro['signature']:
                    raise ValueError('片头在处理期间发生变化，请重试')
                offset = assembly['intro_duration']
            else:
                body.replace(destination)
            restore_inherited_access(destination)
            final_subs = write_subtitles({'segments': []}, {'start':0,'end':cursor}, package/'subtitles.srt', package/'subtitles.ass',
                                         cfg, orientation, edited_srt=text, time_offset=offset)
            for mapping in mappings:
                mapping['output_start'] += offset; mapping['output_end'] += offset
            if intro:
                mappings.insert(0, {'kind':'intro','source':intro['source'],'output_start':0.,'output_end':offset})
            result = dict(video=str(destination), title=item['title'], description=item.get('description',''), duration=cursor+offset,
                          orientation=orientation, preview=preview, preview_complete=bool(preview),
                          preview_note='完整时长 · 低码率预览' if preview else '',
                          item_id=item['id'], item_signature=fingerprint(item), visual_signature=visual_signature(item), render_key=key, segments=mappings,
                          packaging_signature=fingerprint(settings),
                          subtitles=final_subs, subtitle_review=subtitle_review, intro_duration=offset, sources=source)
            if not preview:
                result['verification_path'] = str(package/'verification.json')
            if item.get('subtitle_text') is None:
                original = (remap_manual(frozen['original_text'], render_segments, lengths) if frozen is not None and 'original_text' in frozen
                    else cues_srt(composition_cues(item_transcript(item,cfg,raw=True),render_segments,cfg['subtitle'].get('max_chars',18),lengths)))
                write_subtitles({'segments':[]},{'start':0,'end':cursor},package/'subtitles.original.srt',package/'subtitles.original.ass',
                    cfg,orientation,edited_srt=original,time_offset=offset)
                atomic_json(package/'subtitle_review.json',subtitle_review)
            atomic_json(destination.with_suffix('.timeline.json'), result)
            atomic_json(metadata_path, result)
            (package/'publication.txt').write_text(item['title']+'\n\n'+item.get('description',''), encoding='utf-8')
            if not preview:
                from core.deliveries import seal_render_package
                files = [metadata_path, destination.with_suffix('.timeline.json'), package/'publication.txt',
                         Path(final_subs['srt']), Path(final_subs['ass'])]
                if item.get('subtitle_text') is None:
                    files.extend([package/'subtitles.original.srt', package/'subtitles.original.ass', package/'subtitle_review.json'])
                seal_render_package(result, cfg, files, _dimensions(cfg, orientation, False))
            return result
