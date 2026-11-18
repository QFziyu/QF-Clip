"""One persistent production flow across single, sequence and dual-source inputs."""
from __future__ import annotations

import copy
import logging
from pathlib import Path

from core.utils import atomic_json, file_signature, fingerprint, read_json
from core.export_options import default_export_options, normalize_export_options
from core.editing import validate_segments
from storage.workspaces import WorkspaceStore

LOG = logging.getLogger(__name__)


def adopt_legacy(cfg):
    """Idempotent associations only: never rewrite old approvals or subtitles."""
    from storage.plans import PlanStore
    from storage.composites import CompositeStore
    with WorkspaceStore(cfg['database']) as store, PlanStore(cfg['database']) as plans:
        linked = {i['source'].get('project_id') for w in store.list_workspaces() for v in w['variants'] for i in v['items']}
        for project in store.list_projects():
            identity = project['id']
            previous = store.get_workspace(identity)
            if (previous and (previous.get('legacy_imported') or previous['assets'])) or identity in linked or project['metadata'].get('workflow_analysis'):
                continue
            w = store.create_workspace(project['name'], identity)
            w['assets'] = [dict(id=fingerprint(project['source']), path=project['source'], name=project['name'],
                                duration=project['duration'], meta=project['metadata'])]
            variants = []
            clips = store.list_clips(identity)
            if clips:
                items = []
                for clip in clips:
                    item = _item(clip['id'], clip['title'], [{'start': clip['start'], 'end': clip['end']}],
                                 project, clip.get('subtitle_text'), status=clip['status'])
                    item.update(origin={'kind': 'clip', 'id': clip['id']}, reason=clip['reason'], score=clip['score'],
                                description=clip['description'], category=clip['category'], transcript_key=clip['transcript_key'])
                    item['legacy_preview'] = clip.get('preview_path')
                    item['legacy_outputs'] = list(clip.get('exports', {}).values())
                    items.append(item)
                variants.append(dict(id='legacy', name='原有候选', form='shorts', items=items, events=[], warnings=[]))
            for plan in plans.list_plans(identity):
                items = []
                for draft in plan['drafts']:
                    item = _item(plan['id'][:12]+'-'+draft['id'], draft['title'], draft['segments'], project, draft.get('subtitle_text'), draft['status'])
                    preview = draft.get('preview')
                    preview = preview.get('video') if isinstance(preview,dict) else preview
                    item.update(origin={'kind': 'draft', 'plan_id': plan['id'], 'id': draft['id']},
                                transcript_key=plan['transcript_key'], description=draft.get('description', ''),
                                legacy_preview=preview)
                    items.append(item)
                variants.append(dict(id=plan['id'], name='原有方案 ' + plan['id'][:6], form=plan['options']['format'],
                                     items=items, events=plan['events'], warnings=plan.get('warnings', []), options=plan['options']))
            w.update(variants=variants, active_variant=variants[0]['id'] if variants else None,
                     step=3 if variants else 2, status='待审核' if variants else '等待设置',legacy_imported=True)
            w['packaging'] = {'export_options': store.get_export_options(identity) or default_export_options(cfg), 'subtitles': True}
            store.save_workspace(w)
        linked_composites = {i['source']['document']['id'] for w in store.list_workspaces() for v in w['variants']
                             for i in v['items'] if i['source']['kind'] == 'dual'}
        with CompositeStore(cfg['database']) as composites:
            for doc in composites.list_composites():
                identity = 'dual-' + doc['id']
                if store.get_workspace(identity) or doc['id'] in linked_composites:
                    continue
                w = store.create_workspace(doc['options']['title'], identity)
                w.update(step=2, status='等待核对同步', source_options={'organization': 'dual', 'composite_id': doc['id'],
                    'composite_options': doc['options'], 'subtitle_source': 'commentator'})
                w['assets'] = [dict(id=fingerprint(doc[n]['source']), path=doc[n]['source'], name=Path(doc[n]['source']).name,
                                    duration=doc[n]['duration'], meta=doc[n]) for n in ('match', 'commentator')]
                store.save_workspace(w)


def _item(identity, title, segments, project, subtitle=None, status='pending'):
    return dict(id=identity, title=title, segments=copy.deepcopy(segments), status=status, revision=0,
                subtitle_text=subtitle, subtitle_stale=False, source={'kind': 'single', 'meta': {'source':project['source'],'duration':project['duration'],**project['metadata']},
                'project_id': project['id'], 'project_dir': project['project_dir']},
                transcript_key=project['metadata'].get('transcript_key'), description='', reason='', score=0)


def visual_signature(item):
    return fingerprint({k:item.get(k) for k in ('source','segments','subtitle_text','subtitle_stale','transcript_key')})


def active_variant(w):
    return next((v for v in w['variants'] if v['id'] == w.get('active_variant')), None)


def get_item(w, identity):
    for variant in w['variants']:
        for item in variant['items']:
            if item['id'] == identity:
                return item, variant
    raise ValueError('找不到当前成品')


def import_assets(identity, paths, cfg):
    from core.video_input import probe_video
    from core.local_file_picker import resolve_video_path
    with WorkspaceStore(cfg['database']) as store:
        w = store.get_workspace(identity)
        seen = {a['path'].casefold() for a in w['assets']}
        for path in paths:
            path = resolve_video_path(path)
            if path.casefold() in seen:
                continue
            meta = probe_video(path, cfg, require_audio=False)
            meta.pop('raw_probe', None)
            meta['source_signature'] = file_signature(path)
            w['assets'].append(dict(id=fingerprint(path), path=path, name=Path(path).name, duration=meta['duration'], meta=meta))
            seen.add(path.casefold())
        return store.save_workspace(w) if paths else w


def edit_item(identity, item_id, values, cfg):
    from core.subtitle import validate_srt
    with WorkspaceStore(cfg['database']) as store:
        w = store.get_workspace(identity)
        item, variant = get_item(w, item_id)
        previous_title = item['title']
        previous_platform_titles = dict(item.get('platform_titles') or {})
        if values.get('expected_revision', item['revision']) != item['revision']:
            raise ValueError('此条已更新，请刷新再编辑')
        if 'segments' in values:
            meta = item['source'].get('meta') or item['source']['document']['match']
            segments = validate_segments(values['segments'], meta['duration'], None if variant['form'] == 'full' else variant['form'])
            if item['source']['kind'] == 'dual':
                from core.composite_options import overlap_range, validate_ranges
                d = item['source']['document']
                validate_ranges(segments, overlap_range(d['match']['duration'], d['commentator']['duration'], item['source']['options']['offset_seconds']))
            if segments != item['segments']:
                item['subtitle_stale'] = item.get('subtitle_text') is not None
                item['segments'] = segments
                item.pop('subtitle_review',None)
        if 'title' in values:
            title = values['title'].strip()
            if not title or len(title) > 300:
                raise ValueError('标题需要 1～300 字符')
            if title != item['title']:
                item['title_source'] = 'manual'
            item['title'] = title
        if 'platform_titles' in values:
            from core.titles import validate_titles
            proposed = dict(values['platform_titles'])
            if item['title'] != previous_title:
                proposed = {p: item['title'] if p not in previous_platform_titles and t == previous_title else t for p,t in proposed.items()}
            titles = validate_titles(proposed)
            manual = set(item.get('manual_platform_titles', []))
            manual.update(p for p, t in titles.items() if t != (item.get('platform_titles') or {}).get(p))
            item['platform_titles'] = {**item.get('platform_titles', {}), **titles}
            item['manual_platform_titles'] = sorted(manual)
        if 'description' in values:
            description = values['description']
            if not isinstance(description, str) or len(description) > 2200 or '\x00' in description:
                raise ValueError('发布文案需要不超过 2200 字符')
            if description != item.get('description',''):
                item['description_source'] = 'manual'
            item['description'] = description
        if 'status' in values:
            if values['status'] not in ('approved', 'rejected', 'pending'):
                raise ValueError('审批状态无效')
            item['status'] = values['status']
        if 'user_rating' in values:
            rating=values['user_rating']
            if rating is not None and (type(rating) is not int or not 1<=rating<=5):
                raise ValueError('人工评分应为1～5星')
            item['user_rating']=rating
        if 'subtitle_text' in values:
            text = values['subtitle_text']
            if text is not None:
                validate_srt(text, sum(s['end'] - s['start'] for s in item['segments']))
            item['subtitle_text'] = text
            item['subtitle_stale'] = False
            item.pop('subtitle_review',None)
        item['revision'] += 1
        item.pop('legacy_preview', None)
        item.pop('legacy_outputs', None)
        w['status'] = '内容已修改，需重新检查成品'
        return store.save_workspace(w)


def map_transcript(transcript, offset, overlap):
    """commentator t = match t + offset; retain only evidenced overlap."""
    data = copy.deepcopy(transcript)
    segments = []
    for original in transcript['segments']:
        s = copy.deepcopy(original)
        s['start'], s['end'] = max(overlap['start'], s['start'] - offset), min(overlap['end'], s['end'] - offset)
        if s['end'] <= s['start']:
            continue
        if 'words' in s:
            words = []
            for word in s['words']:
                a, b = max(s['start'], word['start'] - offset), min(s['end'], word['end'] - offset)
                if b > a:
                    words.append({**word, 'start': a, 'end': b})
            s['words'] = words
        segments.append(s)
    data['segments'] = segments
    data['timeline_mapping'] = {'offset_seconds': offset, 'overlap': overlap}
    return data


def _refine_project(project,cfg,settings):
    if not cfg.get('subtitle_review',{}).get('enabled') or settings.get('mock'):
        return project
    from core.pipeline import load_clip_transcript
    from core.transcript_refine import refine_transcript
    raw=load_clip_transcript(project,{'transcript_key':project['metadata'].get('transcript_key')})
    if raw.get('semantic_refinement',{}).get('status')=='complete':
        return project
    if raw.get('raw_transcript_key'):
        raw=load_clip_transcript(project,{'transcript_key':raw['raw_transcript_key']})
    revised,report=refine_transcript(raw,cfg,project['project_dir'],request=True,retry_failed=True)
    from core.transcriber import validate_transcript
    revised=validate_transcript(revised,project['duration'])
    key=fingerprint(revised);raw_key=fingerprint(raw)
    root=Path(project['project_dir'])
    atomic_json(root/'transcripts'/f'{raw_key}.json',raw)
    atomic_json(root/'transcripts'/f'{key}.json',revised)
    project['metadata'].update(transcript_key=key,local_transcript_key=raw_key,semantic_review={k:v for k,v in report.items() if k!='changes'})
    with WorkspaceStore(cfg['database']) as db:db.upsert_project(project)
    if report['status'] not in ('complete','empty'):
        raise ValueError('字幕 API 语义复核未完成；本地转录和复核缓存已保存，请重试任务。不会将原始字幕标记为已复核。')
    return project


def _analysis_project(source, cfg, settings):
    from core.pipeline import process_video
    if source['kind'] != 'dual':
        if source['meta'].get('virtual_timeline'):
            from core.batch_timeline import analyze_timeline
            return _refine_project(analyze_timeline(source['meta'],cfg,settings),cfg,settings)
        result = process_video(source['meta']['source'], cfg, audio_track=settings.get('audio_track', 0), skip_render=True,
            transcript_only=True, allow_cpu=settings.get('allow_cpu', False), mock=settings.get('mock', False),
            transcript_path=settings.get('transcript_path'))
        with WorkspaceStore(cfg['database']) as db:
            return _refine_project(db.get_project(result['project_id']),cfg,settings)
    from core.compositor import _transcript, _wav
    from core.composite_options import overlap_range
    d = source['document']; options = source['options']
    recognition = settings.get('subtitle_source', 'commentator')
    meta = d[recognition]
    root = Path(cfg['cache_dir']) / 'workflow_analysis' / fingerprint({'document': d['id'], 'options': options,
            'recognition': recognition, 'asr': cfg['asr'], 'mock': settings.get('transcript_path')})
    root.mkdir(parents=True, exist_ok=True)
    if settings.get('transcript_path'):
        from core.transcriber import validate_transcript
        raw = validate_transcript(read_json(settings['transcript_path']), meta['duration'])
    else:
        raw = _transcript(meta, _wav(meta, root, cfg), root / 'asr', cfg, settings.get('allow_cpu', False))
    overlap = overlap_range(d['match']['duration'], d['commentator']['duration'], options['offset_seconds'])
    transcript = map_transcript(raw, options['offset_seconds'] if recognition == 'commentator' else 0, overlap)
    key = fingerprint(transcript)
    atomic_json(root / 'transcript.json', transcript)
    atomic_json(root / 'transcripts' / f'{key}.json', transcript)
    project = dict(id='analysis-' + root.name, source=d['match']['source'], name=Path(d['match']['source']).name,
                   duration=d['match']['duration'], project_dir=str(root), status='transcribed',
                   metadata={**d['match'], 'transcript_key': key, 'workflow_analysis': True})
    with WorkspaceStore(cfg['database']) as db:
        db.upsert_project(project)
    return _refine_project(project,cfg,settings)


def prepare_workflow(identity, settings, cfg, expected_revision=None):
    """Heavy work runs in a durable job; stale results never replace current work."""
    from core.video_input import probe_video
    from core.editing import create_editing_plan
    from storage.plans import PlanStore
    with WorkspaceStore(cfg['database']) as store:
        w = store.get_workspace(identity)
    baseline = copy.deepcopy(w)
    if expected_revision is not None and w['revision'] != expected_revision:
        raise ValueError('素材或方案已修改，请重新提交处理')
    asset_map = {a['id']: a for a in w['assets']}
    selected = settings.get('asset_ids') or list(asset_map)
    if not selected or any(a not in asset_map for a in selected):
        raise ValueError('请先在第一步导入并选择素材')
    assets = [asset_map[a] for a in selected]
    organization = settings.get('organization', 'single')
    form = settings.get('form', 'shorts')
    sources = []
    if organization == 'dual':
        from core.compositor import create_composite, _load
        from core.composite_options import normalize_composite_options
        match = settings.get('match_id', selected[0])
        commentators = settings.get('commentator_ids', selected[1:])
        if match not in selected or not commentators or match in commentators or any(c not in selected for c in commentators):
            raise ValueError('比赛与主播需使用不同的已导入素材')
        if settings.get('composite_id'):
            doc, _ = _load(settings['composite_id'], cfg)
        else:
            doc = create_composite(asset_map[match]['path'], [asset_map[a]['path'] for a in commentators], cfg,
                                   settings.get('audio_track', 0), settings.get('commentator_track', 0))
        options = normalize_composite_options(settings.get('composite_options', doc['options']))
        sources = [{'kind': 'dual', 'document': doc, 'options': options}]
    elif organization == 'sequence' and form == 'full':
        from core.source_sequence import prepare_sequence
        meta = prepare_sequence([a['path'] for a in assets], cfg, settings.get('audio_track', 0))
        sources = [{'kind': 'single', 'meta': meta}]
    elif organization in ('single','sequence'):
        sources = [{'kind': 'single', 'meta': probe_video(a['path'], cfg, settings.get('audio_track', 0), require_audio=False)} for a in assets]
    else:
        raise ValueError('素材组织方式无效')
    for source in sources:
        if source['kind'] == 'single':
            source['meta'].pop('raw_probe', None)
            source['meta']['source_signature'] = file_signature(source['meta']['source'])
    scope=settings.get('batch_scope','combined' if form=='montage' and len(assets)>1 else 'separate')
    if scope not in ('combined','separate'):raise ValueError('批次制作范围无效')
    if len(sources)>1 and (organization=='sequence' or (organization=='single' and scope=='combined')):
        from core.batch_timeline import virtual_timeline
        sources=[{'kind':'single','meta':virtual_timeline([s['meta'] for s in sources],cfg)}]
        settings={**settings,'batch_scope':'combined'}
    if settings.get('prepare_only'):
        if organization != 'dual':
            raise ValueError('仅双路模式需要先准备素材')
        settings = {**settings, 'composite_id': sources[0]['document']['id']}
        settings.pop('prepare_only', None)
        w.update(source_options=settings, step=2, status='素材就绪，请对轴')
        with WorkspaceStore(cfg['database']) as store:
            current = store.get_workspace(identity)
            if current['assets'] != baseline['assets'] or current['source_options'] != baseline['source_options']:
                raise ValueError('处理期间素材或设置已变更，请重新提交；已完成缓存保留')
            current.update(source_options=settings,step=2,status=w['status'])
            store.save_workspace(current)
        return {'workspace_id': identity, 'composite_id': settings['composite_id']}
    items, warnings, events, clock_reports = [], [], [], []
    variant_id = fingerprint({'workspace': identity, 'settings': settings, 'sources': sources, 'workflow_version':2, 'subtitle_review':cfg.get('subtitle_review',{})})
    existing = next((v for v in w['variants'] if v['id'] == variant_id), None)
    if existing:
        w.update(active_variant=variant_id, step=3)
    else:
        for index, source in enumerate(sources):
            project = None
            if form != 'full':
                project = _analysis_project(source, cfg, settings)
                result = create_editing_plan(project['id'], cfg, settings['editing_options'], mock=settings.get('mock', False))
                with PlanStore(cfg['database']) as plans:
                    plan = plans.get_plan(result['plan_id'])
                drafts = copy.deepcopy(plan['drafts'])
                if plan.get('clock_report'):
                    clock_reports.append({'source': project['name'], **plan['clock_report']})
                warnings.extend(plan.get('warnings', [])); events.extend(plan['events'])
            else:
                meta = source.get('meta') or source['document']['match']
                bounds = {'start': 0., 'end': meta['duration']}
                if source['kind'] == 'dual':
                    from core.composite_options import overlap_range
                    bounds = overlap_range(meta['duration'], source['document']['commentator']['duration'], source['options']['offset_seconds'])
                drafts = [dict(id='full', title=Path(meta['source']).stem, segments=[bounds], subtitle_text=None, status='pending')]
            for draft in drafts:
                if source['kind'] == 'dual':
                    from core.composite_options import overlap_range
                    bounds = overlap_range(source['document']['match']['duration'], source['document']['commentator']['duration'], source['options']['offset_seconds'])
                    draft['segments'] = [{'start': max(s['start'], bounds['start']), 'end': min(s['end'], bounds['end'])}
                                         for s in draft['segments'] if min(s['end'], bounds['end']) > max(s['start'], bounds['start'])]
                    if not draft['segments']:
                        continue
                item = dict(id=variant_id[:12] + f'-{index}-' + draft['id'], title=draft['title'], segments=draft['segments'],
                            subtitle_text=draft.get('subtitle_text'), status='pending', revision=0, subtitle_stale=False,
                            source=copy.deepcopy(source), description=draft.get('description', ''), reason=draft.get('reason', ''),
                            score=draft.get('score'), score_details=draft.get('score_details',[]), category=draft.get('category','commentary'))
                if project:
                    item['source'].update(project_id=project['id'], project_dir=project['project_dir'])
                    item['transcript_key'] = project['metadata']['transcript_key']
                    if cfg.get('subtitle_review',{}).get('enabled') and not settings.get('mock'):
                        from core.subtitle_review import item_cues
                        _,item['subtitle_review']=item_cues(item,cfg,request=True,
                            enabled=w.get('packaging',{}).get('proofread_subtitles',True))
                items.append(item)
        if form != 'full':
            from core.titles import generate_item_titles
            items, title_warnings = generate_item_titles(items, cfg, identity, mock=settings.get('mock', False))
            warnings.extend(title_warnings)
        w['variants'].append(dict(id=variant_id, name=settings.get('name') or f'方案 {len(w["variants"])+1}', form=form,
                                  items=items, warnings=warnings, events=events, clock_reports=clock_reports,
                                  options=settings.get('editing_options', {}) if form != 'full' else {}))
        w.update(active_variant=variant_id, step=3, status='待审核', source_options=settings)
    with WorkspaceStore(cfg['database']) as store:
        current = store.get_workspace(identity)
        if current['assets'] != baseline['assets'] or current['source_options'] != baseline['source_options']:
            raise ValueError('处理期间素材或设置已变更，请重新提交；已完成缓存保留')
        if not any(v['id'] == variant_id for v in current['variants']):
            current['variants'].append(active_variant(w))
        current.update(active_variant=variant_id,step=3,status='待审核',source_options=settings)
        store.save_workspace(current)
    # A completed generation task means every candidate already has a preview.
    # Retry resumes the saved variant and reuses completed render packages.
    results,errors=[],[]
    if settings.get('auto_preview',True):
        with WorkspaceStore(cfg['database']) as db:
            current=db.get_workspace(identity)
        for n,item in enumerate(active_variant(current)['items'],1):
            try:
                LOG.info('自动生成审核预览 %s/%s：%s',n,len(active_variant(current)['items']),item['title'])
                results.append(preview_workflow(identity,item['id'],cfg))
            except Exception as exc:
                LOG.exception('预览失败，保留候选和已完成预览')
                errors.append({'item_id':item['id'],'error':str(exc)})
    with WorkspaceStore(cfg['database']) as db:
        current=db.get_workspace(identity)
        if current.get('active_variant')==variant_id:
            current['status']='部分预览失败，可重试' if errors else '预览就绪，待审核' if settings.get('auto_preview',True) else '待生成预览'
            db.save_workspace(current)
    return {'workspace_id':identity,'variant_id':variant_id,'candidate_count':len(active_variant(w)['items']),
            'preview_count':len(results),'errors':errors}


def item_transcript(item, cfg, raw=False):
    from core.pipeline import load_clip_transcript
    if not item.get('transcript_key'):
        return {'segments': []}
    source = item['source']
    meta = source.get('meta') or source['document']['match']
    project={'project_dir':source['project_dir'],'duration':meta['duration'],'metadata':{'transcript_key':item['transcript_key']}}
    data=load_clip_transcript(project,item)
    if raw and data.get('raw_transcript_key'):
        return load_clip_transcript(project,{'transcript_key':data['raw_transcript_key']})
    return data


def subtitle_default(item, cfg):
    if item.get('subtitle_text') is not None:
        return item['subtitle_text']
    from core.subtitle import cues_srt
    from core.subtitle_review import item_cues
    return cues_srt(item_cues(item,cfg)[0])


def review_workflow_subtitles(identity, cfg, item_id=None, retry_failed=False):
    """Durable worker entry; preserve manual text and detect concurrent range edits."""
    from core.subtitle_review import item_cues
    with WorkspaceStore(cfg['database']) as store:w=store.get_workspace(identity)
    variant=active_variant(w)
    if not variant:raise ValueError('先生成剪辑方案')
    requested=[i for i in variant['items'] if item_id is None or i['id']==item_id]
    if not requested:raise ValueError('找不到候选')
    reports=[]
    for item in requested:
        if item.get('subtitle_text') is not None or not item.get('transcript_key'):
            reports.append({'item_id':item['id'],'status':'manual' if item.get('subtitle_text') is not None else 'no_transcript'})
            continue
        reviewed_item=copy.deepcopy(item)
        prior=item_transcript(item,cfg)
        if retry_failed and prior.get('semantic_refinement',{}).get('status') in ('partial','failed','pending'):
            from core.transcript_refine import refine_transcript
            from core.transcriber import validate_transcript
            raw=item_transcript(item,cfg,raw=True)
            corrected,_=refine_transcript(raw,cfg,item['source']['project_dir'],request=True,retry_failed=True)
            meta=item['source'].get('meta') or item['source']['document']['match']
            corrected=validate_transcript(corrected,meta['duration'])
            key=fingerprint(corrected)
            atomic_json(Path(item['source']['project_dir'])/'transcripts'/f'{key}.json',corrected)
            reviewed_item['transcript_key']=key
        _,report=item_cues(reviewed_item,cfg,request=True,retry_failed=retry_failed,
            enabled=w.get('packaging',{}).get('proofread_subtitles',cfg.get('subtitle_review',{}).get('enabled',False)))
        with WorkspaceStore(cfg['database']) as store:
            current=store.get_workspace(identity);target,_=get_item(current,item['id'])
            if fingerprint(target)!=fingerprint(item):raise ValueError('校对期间字幕或范围已修改，缓存保留；请刷新后重新校对')
            if target.get('subtitle_review')!=report or target['transcript_key']!=reviewed_item['transcript_key']:
                target['transcript_key']=reviewed_item['transcript_key']
                target['subtitle_review']=report;target['revision']+=1
                store.save_workspace(current)
        reports.append({'item_id':item['id'],**report})
    return {'workspace_id':identity,'reports':reports,'needs_retry':any(r['status'] in ('failed','partial','pending','unconfigured') for r in reports)}


def transcribe_workflow(identity, item_id, cfg, settings=None):
    with WorkspaceStore(cfg['database']) as store:
        w = store.get_workspace(identity)
    item, _ = get_item(w, item_id)
    item_before = copy.deepcopy(item)
    project = _analysis_project(item['source'], cfg, {**w.get('source_options', {}), **(settings or {})})
    item['source'].update(project_id=project['id'], project_dir=project['project_dir'])
    item['transcript_key'] = project['metadata']['transcript_key']
    item['revision'] += 1
    if item.get('subtitle_text') is not None:
        item['subtitle_stale'] = True
    with WorkspaceStore(cfg['database']) as store:
        current = store.get_workspace(identity)
        existing, variant = get_item(current,item_id)
        if fingerprint(existing) != fingerprint(item_before):
            raise ValueError('转录期间成品已修改，缓存已保留，请重新载入识别')
        variant['items'][variant['items'].index(existing)] = item
        store.save_workspace(current)
    return {'workspace_id': identity, 'item_id': item_id, 'transcript_key': item['transcript_key']}


def export_snapshot(w, cfg, orientations, render, output_dir, publication=None):
    variant = active_variant(w)
    if not variant:
        raise ValueError('请先建立剪辑方案')
    items = [copy.deepcopy(i) for i in variant['items'] if i['status'] == 'approved']
    if not items:
        raise ValueError('当前方案没有通过审核的成品')
    package = w.get('packaging') or {}
    options = normalize_export_options(package.get('export_options', default_export_options(cfg)))
    snapshot = dict(workspace_id=w['id'], variant_id=variant['id'], items=items, events=copy.deepcopy(variant.get('events', [])),
                    editing_options=copy.deepcopy(variant.get('options', {})), packaging=copy.deepcopy({**package, 'export_options': options}),
                    orientations=list(orientations), render=copy.deepcopy(render), output_dir=str(Path(output_dir).resolve()))
    if publication is not None:
        snapshot['publication'] = copy.deepcopy(publication)
        if isinstance(snapshot['publication'], dict) and snapshot['publication'].get('cover_path'):
            from core.deliveries import file_record
            frozen_publication = snapshot['publication']
            # Approval binds the image bytes, not just its mutable filesystem name.
            # Replayed frozen intentions must retain their original proof.
            if 'cover_proof' not in frozen_publication:
                cover = frozen_publication['cover_path']
                if not isinstance(cover, str) or not Path(cover).is_absolute():
                    raise ValueError('发布封面必须是现有本地文件')
                frozen_publication['cover_path'] = str(Path(cover).resolve())
                frozen_publication['cover_proof'] = file_record(frozen_publication['cover_path'])
    if snapshot['packaging'].get('subtitles', True):
        snapshot['frozen_subtitles'] = freeze_subtitles(items, snapshot['packaging'], cfg)
    validate_snapshot(snapshot)
    return snapshot


def freeze_subtitles(items, packaging, cfg):
    """Resolve final text using read-only caches before the approval is submitted."""
    from core.deliveries import text_digest
    from core.subtitle import cues_srt, validate_srt, composition_cues
    from core.subtitle_review import item_cues
    frozen = {}
    for item in items:
        if item.get('subtitle_stale'):
            raise ValueError('请先核对改动范围后的人工字幕')
        if item.get('subtitle_text') is not None:
            text, report = item['subtitle_text'], {'status': 'manual'}
            original = None
        else:
            if not item.get('transcript_key'):
                raise ValueError('尚无字幕：请在第四步识别声音、导入 SRT，或选择不添加字幕')
            enabled = packaging.get('proofread_subtitles', cfg.get('subtitle_review', {}).get('enabled', False))
            cues, report = item_cues(item, cfg, request=False, enabled=enabled)
            if enabled and report['status'] not in ('complete', 'empty', 'mock'):
                raise ValueError('自动字幕校对尚未完成，请返回第四步完成校对并检查字幕')
            text = cues_srt(cues)
            original = cues_srt(composition_cues(item_transcript(item, cfg), item['segments'], cfg['subtitle'].get('max_chars', 18)))
        validate_srt(text, sum(s['end'] - s['start'] for s in item['segments']))
        frozen[item['id']] = {'text': text, 'sha256': text_digest(text), 'review': copy.deepcopy(report)}
        if original is not None:
            frozen[item['id']]['original_text'] = original
    return frozen


def _validate_publication_cover(publication):
    """Recheck the exact approved image without replacing its frozen proof."""
    cover, proof = publication.get('cover_path'), publication.get('cover_proof')
    if cover is None:
        if proof is not None:
            raise ValueError('发布封面证明缺少对应路径')
        return
    if not isinstance(cover, str) or not Path(cover).is_absolute():
        raise ValueError('发布封面必须是现有本地文件')
    if (not isinstance(proof, dict) or set(proof) != {'path', 'bytes', 'sha256'}
            or proof.get('path') != cover or str(Path(cover).resolve()) != cover
            or type(proof.get('bytes')) is not int or proof['bytes'] <= 0
            or not isinstance(proof.get('sha256'), str) or len(proof['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in proof['sha256'])):
        raise ValueError('发布封面冻结记录无效，请重新检查并授权')
    if Path(cover).suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        raise ValueError('发布封面需要 PNG、JPEG 或 WebP 图片')
    from core.deliveries import verify_file
    try:
        verify_file(proof)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError('发布封面已变化或不可访问，请重新检查并授权本批') from exc


def validate_snapshot(snapshot):
    required = {'workspace_id','variant_id','items','events','editing_options','packaging','orientations','render','output_dir'}
    if not isinstance(snapshot, dict) or not required.issubset(snapshot) or set(snapshot) - required - {'publication', 'frozen_subtitles'}:
        raise ValueError('导出快照字段不完整或含未知配置')
    if not snapshot['orientations'] or any(o not in ('horizontal','vertical') for o in snapshot['orientations']):
        raise ValueError('请选择输出画幅')
    r = snapshot['render']
    if set(r) - {'horizontal','vertical','encoder','fps','bitrate'} or r.get('encoder', 'auto') not in ('auto','libx264','h264_nvenc'):
        raise ValueError('编码设置无效')
    for key in ('horizontal', 'vertical'):
        dims = r.get(key, [1920, 1080])
        if not isinstance(dims, list) or len(dims) != 2 or any(type(n) is not int or n < 16 or n > 7680 or n % 2 for n in dims):
            raise ValueError('输出分辨率需要有效偶数')
    if not snapshot['items']:
        raise ValueError('没有待导出成品')
    if 'publication' in snapshot:
        publication = snapshot['publication']
        if not isinstance(publication, dict) or set(publication) - {'platform','account_id','mode','orientation','hashtags','cover_path','cover_proof','copyright','source','category_path'}:
            raise ValueError('发布设置含未知字段')
        if publication.get('platform') not in ('douyin', 'bilibili') or publication.get('mode') not in ('prepare', 'auto'):
            raise ValueError('发布平台或授权方式无效')
        if publication['platform'] == 'bilibili':
            from core.bilibili_options import validate_bilibili_options
            validate_bilibili_options(publication)
        account = publication.get('account_id')
        if not isinstance(account, str) or not account.strip() or len(account) > 120 or any(ord(c) < 32 for c in account):
            raise ValueError('请选择已配置的发布账号')
        if publication.get('orientation') not in snapshot['orientations']:
            raise ValueError('发布画幅必须已勾选导出')
        tags = publication.get('hashtags', [])
        if not isinstance(tags, list) or len(tags) > 20 or any(not isinstance(t, str) or not 1 <= len(t.strip()) <= 100 or any(ord(c) < 32 for c in t) for t in tags):
            raise ValueError('发布话题无效')
        _validate_publication_cover(publication)
        if snapshot['packaging'].get('subtitles', True) and 'frozen_subtitles' not in snapshot:
            raise ValueError('发布前需要在第五步冻结最终字幕，请重新检查并提交')
    frozen = snapshot.get('frozen_subtitles')
    if frozen is not None and (not isinstance(frozen, dict) or set(frozen) != {i['id'] for i in snapshot['items']}):
        raise ValueError('冻结字幕与待导出成品不匹配')
    for item in snapshot['items']:
        if item['status'] != 'approved' or (snapshot['packaging'].get('subtitles',True) and item.get('subtitle_stale')):
            raise ValueError('请先批准成品并核对改动范围后的人工字幕')
        if snapshot['packaging'].get('subtitles', True) and item.get('subtitle_text') is None and not item.get('transcript_key'):
            raise ValueError('尚无字幕：请在第四步识别声音、导入 SRT，或选择不添加字幕')
        if item['source']['kind'] == 'dual' and not item['source']['options']['sync_confirmed']:
            raise ValueError('双路成品尚未确认同步，请返回第二步核对')
        if 'publication' in snapshot:
            from core.titles import publication_title
            platform = snapshot['publication']['platform']
            if len(publication_title(item, platform)) > (80 if platform == 'bilibili' else 100) or len(item.get('description', '')) > (2000 if platform == 'bilibili' else 4000):
                raise ValueError('投稿标题或简介超过所选平台长度，请返回第三步编辑')
        if frozen is not None:
            from core.deliveries import text_digest
            from core.subtitle import validate_srt
            entry = frozen[item['id']]
            if not isinstance(entry, dict) or set(entry) - {'text','sha256','review','original_text'} or not {'text','sha256','review'}.issubset(entry):
                raise ValueError('冻结字幕记录无效')
            if not isinstance(entry['text'], str) or entry['sha256'] != text_digest(entry['text']):
                raise ValueError('冻结字幕文字校验失败')
            if item.get('subtitle_text') is not None and entry['text'] != item['subtitle_text']:
                raise ValueError('冻结字幕与人工字幕不符')
            validate_srt(entry['text'], sum(s['end'] - s['start'] for s in item['segments']))
    for event in snapshot['events']:
        if event.get('waived'):
            continue
        if not event.get('confirmed') or event.get('start') is None or not any(
            s['start'] <= event['start'] and s['end'] >= event['end'] for i in snapshot['items'] for s in i['segments']):
            raise ValueError('必剪事件尚未确认或未被已通过成品覆盖：' + event.get('label', '事件'))
    options = snapshot['editing_options']
    if options:
        for i in snapshot['items']:
            duration = sum(s['end']-s['start'] for s in i['segments'])
            if not options['min_seconds']-.001 <= duration <= options['max_seconds']+.001:
                raise ValueError('成品时长超出当前方案范围：' + i['title'])
    return snapshot


def run_workflow_export(identity, snapshot, cfg, *, allow_historical=False):
    from core.workflow_renderer import render_workflow_item
    validate_snapshot(snapshot)
    if snapshot['workspace_id'] != identity:
        raise ValueError('导出项目不匹配')
    with WorkspaceStore(cfg['database']) as store:
        w = store.get_workspace(identity)
        if not allow_historical and not snapshot_is_current(w, snapshot, cfg):
            raise ValueError('提交后上游内容已修改，请回到导出页重新检查并提交')
    results, errors = [], []
    for item in snapshot['items']:
        for orientation in snapshot['orientations']:
            try:
                result = render_workflow_item(identity, item, cfg, orientation, snapshot=snapshot)
                with WorkspaceStore(cfg['database']) as store:
                    result['historical'] = not snapshot_is_current(store.get_workspace(identity), snapshot, cfg, allow_inactive=allow_historical)
                    store.record_workflow_media(identity, item['id'], snapshot, result)
                results.append(result)
            except Exception as exc:
                LOG.exception('成品 %s 导出失败', item['title'])
                errors.append({'item_id': item['id'], 'error': str(exc)})
    deliveries = []
    # A partial batch keeps its completed files but cannot enter the publish
    # outbox. A retry reuses verified packages and registers the complete batch.
    if not errors and not any(r.get('historical') for r in results) and len(results) == len(snapshot['items']) * len(snapshot['orientations']):
        from core.deliveries import make_delivery
        try:
            item_map = {i['id']: i for i in snapshot['items']}
            candidates = [make_delivery(snapshot, item_map[r['item_id']], r, cfg) for r in results if not r.get('historical')]
            with WorkspaceStore(cfg['database']) as store, store.connection:
                store.connection.execute('BEGIN IMMEDIATE')
                if snapshot_is_current(store.get_workspace(identity), snapshot, cfg, allow_inactive=allow_historical):
                    for delivery in candidates:
                        intent = snapshot.get('publication')
                        if intent is not None and delivery['media']['orientation'] != intent['orientation']:
                            intent = None
                        deliveries.append(store.register_delivery(delivery, intent, snapshot))
                else:
                    for result in results:
                        result['historical'] = True
        except Exception as exc:
            LOG.exception('成品交付校验失败')
            errors.append({'delivery': True, 'error': str(exc)})
    return {'workspace_id': identity, 'exports': results, 'errors': errors, 'deliveries': deliveries}


def snapshot_is_current(w, snapshot, cfg, *, allow_inactive=False):
    if not w:
        return False
    if snapshot.get('publication') is not None:
        try:
            _validate_publication_cover(snapshot['publication'])
        except (OSError, ValueError, TypeError, AttributeError):
            return False
    variant = next((v for v in w['variants'] if v['id']==snapshot['variant_id']),None) if allow_inactive else active_variant(w)
    if not variant or variant['id'] != snapshot['variant_id']:
        return False
    if any(w.get(k) or variant.get(k) for k in ('archived','superseded','superseded_by')):
        return False
    current = {i['id']: i for i in variant['items']}
    if any(i['id'] not in current or fingerprint(current[i['id']]) != fingerprint(i) for i in snapshot['items']):
        return False
    packaging = copy.deepcopy(w.get('packaging') or {})
    packaging['export_options'] = normalize_export_options(packaging.get('export_options', default_export_options(cfg)))
    return (packaging == snapshot['packaging'] and variant.get('events', []) == snapshot['events']
            and variant.get('options', {}) == snapshot['editing_options'])


def preview_workflow(identity, item_id, cfg):
    from core.workflow_renderer import render_workflow_item
    if cfg.get('subtitle_review',{}).get('enabled'):
        review_result=review_workflow_subtitles(identity,cfg,item_id)
        if review_result.get('needs_retry'):
            raise ValueError('字幕 API 复核未完成，原有预览保留；请重试字幕复核。')
    with WorkspaceStore(cfg['database']) as store:
        w = store.get_workspace(identity)
    item, _ = get_item(w, item_id)
    packaging = copy.deepcopy(w.get('packaging') or {})
    result = render_workflow_item(identity, item, cfg, preview=True, snapshot={'packaging': packaging})
    with WorkspaceStore(cfg['database']) as store:
        store.record_workflow_media(identity, item_id, {'item': item, 'packaging': packaging}, result, True)
    return result
