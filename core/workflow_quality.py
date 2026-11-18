"""Repair existing previews from cached ASR, without replacing human decisions."""
import copy
import json
import logging
from pathlib import Path

from core.utils import fingerprint, atomic_json, read_json
from storage.workspaces import WorkspaceStore

LOG=logging.getLogger(__name__)


def merge_scoped_review(raw,scoped,revised):
    """Keep the full raw timeline even when only selected ranges were reviewed."""
    selected={fingerprint(s) for s in scoped['segments']}
    result=copy.deepcopy(revised)
    result['segments']=sorted(result['segments']+[copy.deepcopy(s) for s in raw['segments'] if fingerprint(s) not in selected],key=lambda s:(s['start'],s['end']))
    result['raw_transcript_key']=fingerprint(raw)
    if result.get('semantic_refinement'):
        scope=scoped['review_scope'];pad=scope['context_seconds']
        result['semantic_refinement']['reviewed_ranges']=[{'start':max(0,r['start']-pad),'end':r['end']+pad} for r in scope['ranges']]
    return result


def subtitle_state(item,cfg):
    if item.get('subtitle_text') is not None: return 'manual'
    if not item.get('transcript_key'): return 'no_transcript'
    from core.subtitle_review import item_cues
    try: return item_cues(item,cfg,request=False)[1]['status']
    except (ValueError,OSError,KeyError): return 'unverified'


def _scores(items,cfg,root):
    from core.workflow import item_transcript
    from core.llm_client import LLMClient
    # Scoring and subtitle review share each source project's existing budget.
    sources={i['source']['project_dir'] for i in items}
    if len(sources)>1:
        combined={}
        for directory in sources:
            combined.update(_scores([i for i in items if i['source']['project_dir']==directory],cfg,root))
        return combined
    ledger=Path(next(iter(sources)))/'selection/api_budget.json' if sources else root/'selection/api_budget.json'
    rows=[]
    for item in items:
        if item.get('score') and item.get('reason'): continue
        transcript=item_transcript(item,cfg)
        text=''.join(s['text'] for s in transcript['segments'] if any(s['end']>r['start'] and s['start']<r['end'] for r in item['segments']))
        if text.strip(): rows.append({'id':item['id'],'title':item['title'],'text':text[:2400]})
    schema={'type':'object','additionalProperties':False,'required':['scores'],'properties':{'scores':{'type':'array','items':{
        'type':'object','additionalProperties':False,'required':['id','score','reason'],
        'properties':{'id':{'type':'string'},'score':{'type':'number','minimum':0,'maximum':100},'reason':{'type':'string'}}}}}}
    answer={}
    for start in range(0,len(rows),5):
        group=rows[start:start+5]
        key=fingerprint({'group':group,'model':cfg['openai'].get('model'),'endpoint':cfg['openai'].get('base_url'),'version':1})
        path=root/'score_review'/f'{key}.json'
        expected={r['id'] for r in group}
        def validate(value):
            entries=value.get('scores',[])
            if len(entries)!=len(expected) or {r.get('id') for r in entries}!=expected: raise ValueError('评分条目不匹配')
            for row in entries:
                if type(row.get('score')) not in (int,float) or not 0<=row['score']<=100 or not isinstance(row.get('reason'),str) or not row['reason'].strip(): raise ValueError('评分格式无效')
            return value
        if path.exists(): value=validate(read_json(path))
        else:
            LOG.info('恢复AI评分 %s/%s',start+1,len(rows))
            value=LLMClient(cfg['openai'],ledger).complete([
                {'role':'system','content':'根据给定真实转录，为每条足球切片评0至100的启发式推荐分并写中文理由。衡量观点完整性、讨论价值和表达清晰度；仅据转录，不假设发生进球，不把标题当事实，不预测真实流量。不改标题或时间。'},
                {'role':'user','content':json.dumps(group,ensure_ascii=False)}],schema,validator=validate,response_key='scores')
            atomic_json(path,value)
        answer.update({r['id']:r for r in value['scores']})
    return answer


def repair_previews(identity,cfg):
    from core.workflow import active_variant,item_transcript,get_item,preview_workflow
    from core.transcript_refine import refine_transcript
    from core.transcriber import validate_transcript
    from core.subtitle_review import item_cues
    if not cfg.get('subtitle_review',{}).get('enabled'):
        raise ValueError('请在设置启用字幕AI复核后重试；不会将CPU原始转录当作已复核')
    with WorkspaceStore(cfg['database']) as store:
        w=store.get_workspace(identity)
    variant=active_variant(w)
    if not variant: raise ValueError('没有可修复的候选')
    if w.get('packaging',{}).get('proofread_subtitles') is False:
        raise ValueError('当前项目关闭了AI字幕复核，请先在第四步启用')
    groups={}; errors=[]
    for i in variant['items']:
        if i.get('subtitle_text') is not None or not i.get('transcript_key'): continue
        groups.setdefault((i['source']['project_dir'],i['transcript_key']),[]).append(i)
    for (directory,key),items in groups.items():
        raw=item_transcript(items[0],cfg,raw=True)
        needs=any(subtitle_state(i,cfg) not in ('complete','empty') for i in items)
        if not needs: continue
        # Review selected clips and their surrounding context, not unrelated hours.
        ranges=[r for i in items for r in i['segments']]
        scoped=copy.deepcopy(raw)
        scoped['segments']=[s for s in raw['segments'] if any(s['end']>r['start']-12 and s['start']<r['end']+12 for r in ranges)]
        scoped['review_scope']={'kind':'selected_ranges_with_context','ranges':ranges,'context_seconds':12}
        revised,report=refine_transcript(scoped,cfg,directory,request=True,retry_failed=True)
        revised=merge_scoped_review(raw,scoped,revised)
        duration=(items[0]['source'].get('meta') or items[0]['source']['document']['match'])['duration']
        revised=validate_transcript(revised,duration); newkey=fingerprint(revised)
        atomic_json(Path(directory)/'transcripts'/f'{fingerprint(scoped)}.json',scoped)
        atomic_json(Path(directory)/'transcripts'/f'{fingerprint(raw)}.json',raw)
        atomic_json(Path(directory)/'transcripts'/f'{newkey}.json',revised)
        with WorkspaceStore(cfg['database']) as store:
            current=store.get_workspace(identity)
            for original in items:
                target,_=get_item(current,original['id'])
                if fingerprint(target)!=fingerprint(original):
                    errors.append({'item_id':target['id'],'error':'复核期间内容已改变，保留人工修改，请重试'});continue
                target['transcript_key']=newkey
                target['subtitle_review']=item_cues(target,cfg,request=False)[1]
                target['revision']+=1
            store.save_workspace(current)
        if report['status'] not in ('complete','empty'):
            errors.append({'error':'字幕语义复核未完成，原文和已完成块已缓存；请重试本任务'})
    with WorkspaceStore(cfg['database']) as store:w=store.get_workspace(identity)
    items=active_variant(w)['items']
    try:
        scores=_scores(items,cfg,Path(cfg['cache_dir'])/'workflow_quality'/identity)
        with WorkspaceStore(cfg['database']) as store:
            current=store.get_workspace(identity)
            for original in items:
                if original['id'] not in scores: continue
                target,_=get_item(current,original['id'])
                if fingerprint(target)!=fingerprint(original):
                    errors.append({'item_id':target['id'],'error':'评分期间内容已改变，请重试'});continue
                row=scores[original['id']];target.update(score=row['score'],reason=row['reason'],score_source='llm_transcript_review')
                target['revision']+=1
            if scores: store.save_workspace(current)
    except Exception as exc: errors.append({'error':'评分恢复失败：'+str(exc)})
    previews=[]
    with WorkspaceStore(cfg['database']) as store:w=store.get_workspace(identity)
    for i in active_variant(w)['items']:
        try:
            if subtitle_state(i,cfg) not in ('manual','complete','empty'):
                errors.append({'item_id':i['id'],'error':'字幕复核未完成，保留已有预览'});continue
            previews.append(preview_workflow(identity,i['id'],cfg))
        except Exception as exc: errors.append({'item_id':i['id'],'error':str(exc)})
    with WorkspaceStore(cfg['database']) as store:
        current=store.get_workspace(identity)
        current.update(step=3,status='修复未完成，可重试' if errors else 'AI复核与评分已恢复，预览就绪')
        store.save_workspace(current)
    return {'workspace_id':identity,'preview_count':len(previews),'errors':errors}
