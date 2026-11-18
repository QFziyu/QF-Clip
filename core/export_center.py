"""Shared, local-only export planning for the UI, CLI and paired control API.

Plans freeze approved content. Queue idempotency and verified render packages
make retries safe; this entry never grants review or publishing authorization.
"""
from __future__ import annotations

import copy
from pathlib import Path

from core.utils import fingerprint
from core.workflow import export_snapshot, snapshot_is_current, run_workflow_export
from storage.workspaces import WorkspaceStore


def eligible(document):
    return not any(document.get(k) for k in ('archived', 'superseded', 'superseded_by'))


def item_key(w, v, i):
    return f"{w['id']}/{v['id']}/{i['id']}"


def _source_files(item):
    source = item['source']
    metas = [source['document'][k] for k in ('match', 'commentator')] if source['kind'] == 'dual' else [source['meta']]
    paths = []
    for meta in metas:
        if meta.get('virtual_timeline'):
            from core.batch_timeline import split_ranges
            paths.extend(c['meta']['source'] for c in split_ranges(meta, item['segments']))
        else:
            paths.append(meta['source'])
    return paths


def catalog(cfg):
    """List all approved, non-archived variants in this data environment."""
    rows = []
    with WorkspaceStore(cfg['database']) as store:
        for w in store.list_workspaces():
            if not eligible(w):
                continue
            media = store.list_media(w['id'], False)
            updated = store.connection.execute('SELECT updated_at FROM workspaces WHERE id=?', (w['id'],)).fetchone()[0]
            for v in w['variants']:
                if not eligible(v):
                    continue
                for i in v['items']:
                    if i['status'] != 'approved' or not eligible(i):
                        continue
                    records = [r for r in media if r['item_id'] == i['id']]
                    current=[]
                    for r in records:
                        try:
                            snapshot=copy.deepcopy(r['snapshot'])
                            snapshot['items']=[old for old in snapshot['items'] if old['id']==i['id']]
                            if snapshot['items'] and Path(r['result']['video']).is_file() and snapshot_is_current(w,snapshot,cfg,allow_inactive=True):current.append(r)
                        except (KeyError,TypeError,ValueError):pass  # Legacy history is still shown.
                    legacy=[{'video':o['path'],'legacy':True} for o in i.get('legacy_outputs',[]) if o.get('path')]
                    legacy_present=any(Path(o['video']).is_file() for o in legacy)
                    state = 'exported' if current else 'changed' if records else 'legacy' if legacy_present and not i.get('revision') else 'changed' if legacy else 'new'
                    delivery_rows = store.connection.execute('SELECT document_json FROM workflow_deliveries WHERE workspace_id=?', (w['id'],)).fetchall()
                    import json
                    deliveries = [json.loads(r[0]) for r in delivery_rows]
                    linked = [d for d in deliveries if d.get('item_id') == i['id']]
                    rows.append(dict(key=item_key(w,v,i), workspace_id=w['id'], project=w['name'], variant_id=v['id'],
                        variant=v['name'], item_id=i['id'], title=i['title'], revision=i.get('revision',0),
                        duration=sum(s['end']-s['start'] for s in i['segments']), updated_at=updated,
                        export_status=state, default_selected=state not in ('exported','legacy'),
                        subtitle_status='stale' if i.get('subtitle_stale') else 'manual' if i.get('subtitle_text') is not None else (i.get('subtitle_review') or {}).get('status','unverified'),
                        publication_status='see_receipt' if linked else 'no_receipt',
                        outputs=[r['result'] for r in records]+legacy, signature=fingerprint(i)))
    return rows


def plan_exports(cfg, keys, orientations=None, render=None, output_dir=None):
    if not isinstance(keys, list) or any(not isinstance(k,str) for k in keys) or len(set(keys)) != len(keys):
        raise ValueError('请选择不重复的成品标识')
    orientations = orientations or ['horizontal']
    render = copy.deepcopy(render if render is not None else {k:cfg['render'][k] for k in ('horizontal','vertical','encoder','fps','bitrate')})
    output_dir = str(Path(output_dir or cfg['output_dir']).resolve())
    selected = set(keys); found = set(); groups = []; issues = []
    with WorkspaceStore(cfg['database']) as store:
        workspaces = store.list_workspaces()
    for w in workspaces:
        if not eligible(w): continue
        for v in w['variants']:
            if not eligible(v): continue
            items = [i for i in v['items'] if item_key(w,v,i) in selected]
            if not items: continue
            found.update(item_key(w,v,i) for i in items)
            usable = []
            for i in items:
                try:
                    if i['status'] != 'approved' or not eligible(i): raise ValueError('此条未通过审核或已归档')
                    for path in _source_files(i):
                        if not Path(path).is_file(): raise ValueError('素材不存在：'+str(path))
                    candidate = copy.deepcopy(w); candidate['active_variant'] = v['id']
                    variant = next(x for x in candidate['variants'] if x['id']==v['id'])
                    variant['items'] = [copy.deepcopy(i)]
                    # Events are validated once against the complete selected group below.
                    variant['events'] = []
                    export_snapshot(candidate,cfg,orientations,render,output_dir)
                    usable.append(copy.deepcopy(i))
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    issues.append(dict(key=item_key(w,v,i), title=i['title'], code='ITEM_NOT_READY', message=str(exc), retryable=False))
            if not usable: continue
            candidate = copy.deepcopy(w); candidate['active_variant'] = v['id']
            variant = next(x for x in candidate['variants'] if x['id']==v['id']); variant['items'] = usable
            try:
                snapshot = export_snapshot(candidate,cfg,orientations,render,output_dir)
                groups.append(dict(workspace_id=w['id'], project=w['name'], snapshot=snapshot))
            except (ValueError,OSError,KeyError,TypeError) as exc:
                issues.append(dict(key=w['id']+'/'+v['id'], code='GROUP_NOT_READY', message=str(exc), retryable=False))
    for key in selected-found:
        issues.append(dict(key=key,code='NOT_FOUND',message='成品不存在或已归档，请刷新',retryable=False))
    plan = dict(schema_version=1, environment=fingerprint(str(Path(cfg['database']).resolve())), groups=groups,
                issues=issues, selected_keys=sorted(keys))
    plan['id'] = fingerprint(plan)
    plan['item_count'] = sum(len(g['snapshot']['items']) for g in groups)
    plan['duration'] = sum(s['end']-s['start'] for g in groups for i in g['snapshot']['items'] for s in i['segments'])
    return plan


def validate_plan(plan, cfg=None):
    from core.workflow import validate_snapshot
    if not isinstance(plan,dict) or plan.get('schema_version')!=1 or not plan.get('groups') or plan.get('issues'):
        raise ValueError('批次为空或仍有问题，请取消勾选问题项后重新预检')
    body = {k:v for k,v in plan.items() if k not in ('id','item_count','duration')}
    if fingerprint(body)!=plan.get('id'): raise ValueError('批次计划校验失败，请重新预检')
    if cfg and plan['environment']!=fingerprint(str(Path(cfg['database']).resolve())):
        raise ValueError('批次所属数据环境不一致')
    for g in plan['groups']:
        validate_snapshot(g['snapshot'])
        if g['workspace_id']!=g['snapshot']['workspace_id'] or 'publication' in g['snapshot']:
            raise ValueError('全局导出仅接收本地导出计划，发布需单独授权')


def submit_exports(plan,cfg):
    from core.jobs import submit_job
    validate_plan(plan,cfg)
    with WorkspaceStore(cfg['database']) as store:
        for g in plan['groups']:
            w=store.get_workspace(g['workspace_id'])
            if not w or not snapshot_is_current(w,g['snapshot'],cfg,allow_inactive=True):
                raise ValueError('预检后内容已修改，请刷新并重新预检')
    return submit_job('export_batch', {'plan':plan}, cfg, idempotency_key='export-batch:'+plan['id'])


def run_export_batch(plan,cfg):
    """One durable parent job, checkpointed project/variant children.

    Re-running a failed child uses the renderer's verified package cache.
    Completed children are verified and skipped; historical local output is kept.
    """
    validate_plan(plan,cfg)
    from core.deliveries import verify_render_package
    preference='export-batch:'+plan['id']
    with WorkspaceStore(cfg['database']) as store:
        progress=store.preference(preference) or {'id':plan['id'],'children':{}}
    for g in plan['groups']:
        child=fingerprint(g['snapshot']); prior=progress['children'].get(child)
        if prior and not prior.get('errors'):
            try:
                for result in prior['exports']: verify_render_package(result,cfg)
                if prior['exports']: continue
            except (ValueError, OSError, RuntimeError): pass
        try:
            result=run_workflow_export(g['workspace_id'],g['snapshot'],cfg,allow_historical=True)
        except Exception as exc:
            result={'workspace_id':g['workspace_id'],'exports':[], 'errors':[{'error':str(exc)}]}
        progress['children'][child]=result
        with WorkspaceStore(cfg['database']) as store: store.preference(preference,progress)
    exports=[r for child in progress['children'].values() for r in child.get('exports',[])]
    errors=[{'workspace_id':child['workspace_id'],**e} for child in progress['children'].values() for e in child.get('errors',[])]
    return dict(batch_id=plan['id'],status='partial' if errors and exports else 'failed' if errors else 'completed',
                exports=exports,errors=errors,children=progress['children'])


def summary(cfg):
    from core.jobs import list_jobs
    with WorkspaceStore(cfg['database']) as store: workspaces=store.list_workspaces()
    items=[i for w in workspaces if eligible(w) for v in w['variants'] if eligible(v) for i in v['items'] if eligible(i)]
    rows=catalog(cfg); jobs=list_jobs(cfg)
    from core.llm_settings import configured
    return dict(environment=fingerprint(str(Path(cfg['database']).resolve())), database=cfg['database'],
        subtitle_pipeline={'asr_engine':cfg['asr'].get('engine'),'semantic_review_enabled':cfg.get('subtitle_review',{}).get('enabled',False),
            'llm_provider':cfg['openai'].get('provider','api'),'llm_model':cfg['openai'].get('model'),'llm_configured':configured(cfg['openai']),
            'independent_second_audio_asr':False},
        project_count=len(workspaces), pending_review=sum(i['status']=='pending' for i in items),
        approved=len(rows), needs_export=sum(r['default_selected'] for r in rows),
        jobs=[{k:j.get(k) for k in ('id','kind','status','updated_at','error')} for j in jobs if j['status'] in ('running','queued','failed','interrupted')],
        next_action='review_candidates' if any(i['status']=='pending' for i in items) else 'plan_exports')


RULES = {
    'subtitle_pipeline':'local_cpu_asr -> llm_text_semantic_review -> automatic_preview',
    'independent_audio_comparison':False,
    'review_success_states':['complete','empty','manual'],
    'incomplete_review':'Keep raw transcript and cache. Report incomplete; retry before final export.',
    'scores':'Preserve AI score/reason and manual rating. Missing score is unknown, never invented.',
    'approval':'Only explicit human decisions may approve items. Preparing previews does not approve.',
    'publication':'Export is local only. Publishing requires a separately confirmed batch.',
}


def project_manifest(identity,cfg):
    from core.workflow import visual_signature
    with WorkspaceStore(cfg['database']) as store:
        w=store.get_workspace(identity)
        if not w:raise ValueError('找不到项目')
        records=store.list_media(identity)
    variants=[]
    for variant in w['variants']:
        items=[]
        for i in variant['items']:
            media=[r['result'] for r in records if r['item_id']==i['id']]
            previews=[r for r in media if r.get('preview') and r.get('visual_signature')==visual_signature(i) and Path(r['video']).is_file()]
            review=i.get('subtitle_review') or {}
            items.append({k:i.get(k) for k in ('id','title','status','revision','segments','score','reason','user_rating')} | {
                'sources':_source_files(i),'manual_subtitles':i.get('subtitle_text') is not None,'subtitle_stale':bool(i.get('subtitle_stale')),
                'transcript':str(Path(i['source']['project_dir'])/'transcripts'/(i['transcript_key']+'.json')) if i.get('transcript_key') else None,
                'subtitle_review':{k:review.get(k) for k in ('status','method','changed','rejected','cache_dir')},
                'current_previews':previews,'media_history':media})
        variants.append({'id':variant['id'],'name':variant['name'],'active':variant['id']==w['active_variant'],'eligible':eligible(variant),'items':items})
    return {'id':identity,'name':w['name'],'revision':w['revision'],'step':w['step'],'status':w['status'],'variants':variants}


def agent_call(method, route, data, cfg):
    """Versioned HTTP/CLI surface. No arbitrary SQL, shell, credentials or edits."""
    from core.jobs import get_job, retry_job, cancel_job, submit_job
    if method=='GET' and route=='capabilities':
        from core.version import VERSION
        return dict(api_version=1,version=VERSION,rules=RULES,routes=['summary','projects','projects/{id}','approved','exports/plan','exports/submit','jobs/{id}','jobs/{id}/retry','jobs/{id}/cancel','projects/{id}/repair-previews'])
    if method=='GET' and route=='summary': return summary(cfg)
    if method=='GET' and route=='approved': return {'items':catalog(cfg)}
    if method=='GET' and route=='projects':
        with WorkspaceStore(cfg['database']) as store:
            return {'projects':[{'id':w['id'],'name':w['name'],'step':w['step'],'revision':w['revision'],'status':w['status'],
                'variants':[{'id':v['id'],'name':v['name'],'items':[{'id':i['id'],'title':i['title'],'status':i['status'],'revision':i['revision'],
                    'score':i.get('score'),'subtitle_review':(i.get('subtitle_review') or {}).get('status','unverified')} for i in v['items']]} for v in w['variants']]} for w in store.list_workspaces()]}
    if method=='POST' and route=='exports/plan': return plan_exports(cfg,**data)
    if method=='POST' and route=='exports/submit': return submit_exports(data['plan'],cfg)
    parts=route.split('/')
    if method=='GET' and len(parts)==2 and parts[0]=='projects':return project_manifest(parts[1],cfg)
    if len(parts)>=2 and parts[0]=='jobs':
        job=get_job(parts[1],cfg)
        if not job: raise ValueError('找不到任务')
        if method=='GET' and len(parts)==2:
            return {k:job.get(k) for k in ('id','kind','status','created_at','updated_at','attempt','result','error')}
        if method=='POST' and parts[2:]==['retry']: return retry_job(parts[1],cfg)
        if method=='POST' and parts[2:]==['cancel']: return cancel_job(parts[1],cfg)
    if method=='POST' and len(parts)==3 and parts[0]=='projects' and parts[2]=='repair-previews':
        return submit_job('workflow_repair',{'workspace_id':parts[1]},cfg)
    raise ValueError('未知AI操作接口')
