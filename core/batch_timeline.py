"""A file-backed virtual timeline: analyze originals, render selected ranges only."""
from __future__ import annotations
import copy
import logging
import math
from pathlib import Path

from core.utils import atomic_json, fingerprint, read_json, project_lock

LOG=logging.getLogger(__name__)


def virtual_timeline(metas, cfg):
    if not metas:raise ValueError('整批合成至少需要一份素材')
    parts=[];cursor=0.
    for meta in metas:
        part=copy.deepcopy(meta)
        part.update(sequence_start=cursor,sequence_end=cursor+meta['duration'])
        parts.append(part);cursor=part['sequence_end']
    identity=fingerprint({'sources':parts,'version':1})
    root=Path(cfg['cache_dir'])/'batch_timelines'/identity;root.mkdir(parents=True,exist_ok=True)
    meta={**copy.deepcopy(metas[0]),'source':str(root/'timeline.json'),'duration':cursor,
        'timeline_origin':0.,'virtual_timeline':True,'parts':parts}
    meta.pop('source_signature',None)
    atomic_json(root/'timeline.json',meta)
    return meta


def analyze_timeline(meta, cfg, settings):
    from core.workflow import _analysis_project
    from core.pipeline import load_clip_transcript
    from storage.workspaces import WorkspaceStore
    root=Path(meta['source']).parent;segments=[];projects=[]
    for index,part in enumerate(meta['parts']):
        LOG.info('整批时间轴：复用或识别素材 %d/%d · %s',index+1,len(meta['parts']),Path(part['source']).name)
        project=_analysis_project({'kind':'single','meta':part},cfg,settings)
        transcript=load_clip_transcript(project,{'transcript_key':project['metadata']['transcript_key']})
        offset=part['sequence_start']
        for original in transcript['segments']:
            a,b=max(0.,original['start']),min(part['duration'],original['end'])
            if b<=a:continue
            s={**copy.deepcopy(original),'start':a+offset,'end':b+offset}
            if 'words' in s:s['words']=[{**word,'start':max(a,word['start'])+offset,'end':min(b,word['end'])+offset}
                for word in s['words'] if min(b,word['end'])>max(a,word['start'])]
            segments.append(s)
        projects.append({'id':project['id'],'transcript_key':project['metadata']['transcript_key'],'offset':offset,'duration':part['duration']})
    transcript={'segments':segments,'duration':meta['duration'],'timeline_mapping':projects}
    key=fingerprint(transcript)
    atomic_json(root/'transcripts'/f'{key}.json',transcript)
    project={'id':'batch-'+root.name,'source':meta['source'],'name':'整批素材时间轴','duration':meta['duration'],
        'project_dir':str(root),'status':'transcribed','metadata':{**meta,'transcript_key':key,
            'workflow_analysis':True,'batch_projects':projects}}
    with WorkspaceStore(cfg['database']) as db:db.upsert_project(project)
    return project


def create_batch_plan(project, cfg, options, mock=False, clock_report=None):
    """Reuse each source's grounded topic index, then apply count/length once globally."""
    from core.editing import create_editing_plan,build_drafts,validate_plan_document
    from core.editing_options import locate_events
    from core.pipeline import load_clip_transcript
    from storage.plans import PlanStore
    candidates=[];references=[]
    # Match-clock anchors are defined once on the combined recording, never once per file.
    local_options={**options,'anchors':[],'events':[]}
    with PlanStore(cfg['database']) as db:
        for source in project['metadata']['batch_projects']:
            result=create_editing_plan(source['id'],cfg,local_options,mock=mock)
            local=db.get_plan(result['plan_id'])
            if local['transcript_key']!=source['transcript_key']:
                # Legacy plans use the normalized transcript fingerprint. Verify exact contents.
                from core.editing import load_plan_transcript
                original=db.get_project(source['id'])
                if fingerprint(load_plan_transcript(original,local['transcript_key']))!=fingerprint(load_clip_transcript(original,{'transcript_key':source['transcript_key']})):
                    raise ValueError('素材的选片索引与转录版本不符，请重新分析该素材')
            path=Path(local.get('index_path',''))
            indexed=read_json(path).get('clips',[]) if path.is_file() else [
                {'start':s['start'],'end':s['end'],'title':d['title'],'reason':d.get('reason',''),'score':d.get('score',0)}
                for d in local['drafts'] for s in d['segments']]
            references.append({'plan_id':local['id'],'offset':source['offset'],'transcript_key':local['transcript_key'],'index':fingerprint(indexed)})
            for clip in indexed:
                a,b=clip['start'],clip['end']
                if not all(isinstance(n,(int,float)) and not isinstance(n,bool) and math.isfinite(n) for n in (a,b)) or not 0<=a<b<=source['duration']+.001:
                    raise ValueError('已缓存索引的素材时间范围无效')
                candidates.append({**clip,'start':a+source['offset'],'end':min(b,source['duration'])+source['offset']})
        identity_spec={'project':project['id'],'options':options,'indexes':references,'version':1}
        if clock_report:identity_spec['clock_verified']=clock_report['verified_events']
        identity=fingerprint(identity_spec)
        with project_lock(project['project_dir']):
            existing=db.get_plan(identity)
            if existing:return {'project_id':project['id'],'plan_id':identity,'draft_count':len(existing['drafts']),'warnings':existing['warnings'],'errors':[]}
            transcript=load_clip_transcript(project,{'transcript_key':project['metadata']['transcript_key']})
            from core.match_clock import locate_verified_events
            events=locate_verified_events(options,project['duration'],clock_report)
            drafts,warnings=build_drafts(candidates,events,options,transcript,project['duration'])
            warnings.extend((clock_report or {}).get('warnings', []))
            for i,draft in enumerate(drafts,1):
                if options['format']=='montage':
                    draft.update(title=f'赛后长集锦 {i}',reason=f'从 {len(references)} 份素材的已校验话题统一选片，按素材顺序组织；整批目标 {options["count"]} 条',
                        description='按已导入素材的顺序组合精彩内容，保留原片时间映射，等待人工审核。')
            plan={'id':identity,'project_id':project['id'],'options':options,'transcript_key':project['metadata']['transcript_key'],
                'mock':bool(mock),'drafts':drafts,'events':events,'warnings':warnings,'index_count':len(candidates),
                'batch_indexes':references,'revision':0}
            if clock_report:plan['clock_report']=clock_report
            validate_plan_document(plan,project['duration']);db.insert_plan(plan)
            atomic_json(Path(project['project_dir'])/'plans'/identity/'initial_plan.json',plan)
    return {'project_id':project['id'],'plan_id':identity,'draft_count':len(drafts),'warnings':warnings,'errors':[]}


def split_ranges(meta, segments):
    """Map combined ranges (including cross-file cuts) to originals without copying video."""
    result=[]
    for segment in segments:
        for part in meta['parts']:
            a,b=max(segment['start'],part['sequence_start']),min(segment['end'],part['sequence_end'])
            if b>a:
                result.append({'meta':part,'start':a-part['sequence_start'],'end':b-part['sequence_start'],
                    'timeline_start':a,'timeline_end':b})
    return result
