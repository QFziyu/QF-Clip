"""Task-oriented five-stage workbench: one import and one final export route."""
from __future__ import annotations

import copy
import html
import os
from pathlib import Path

import streamlit as st

from storage.workspaces import WorkspaceStore
from core.workflow import (adopt_legacy, active_variant, import_assets, edit_item, subtitle_default,
                           export_snapshot, get_item)
from core.jobs import submit_job, list_jobs, retry_job, tail_job_log
from core.local_file_picker import open_folder
from ui.file_picker import request_file, apply_file_result, file_picker_panel
from core.utils import fingerprint
from core.export_options import default_export_options, normalize_export_options

STEPS = ['导入素材', '选择剪辑方式', '预览审核', '字幕与包装', '统一导出']
ORGS = {'single':'单路录像', 'sequence':'多段顺序拼接', 'dual':'比赛＋主播双路合成'}
from core.editing_options import FORMAT_LABELS
FORMS = {**FORMAT_LABELS, 'full':'完整输出'}
STATUS = {'pending':'待审核','approved':'已通过','rejected':'已拒绝'}


def _save(w, cfg):
    with WorkspaceStore(cfg['database']) as store:
        return store.save_workspace(w)


def _go(identity, step, cfg):
    with WorkspaceStore(cfg['database']) as store:
        w = store.get_workspace(identity)
        w['step'] = step
        store.save_workspace(w)


def _open_subtitles(identity,item_id,cfg):
    st.session_state[identity+'-subtitle-item']=item_id
    _go(identity,4,cfg)


def _new_workspace(cfg):
    # Run before rendering the project selector. Rendering its old value before
    # st.rerun() can send stale widget state back with the next file-open click.
    with WorkspaceStore(cfg['database']) as store:
        new = store.create_workspace()
        store.preference('current_workspace', new['id'])
    st.session_state['_wb_select_next'] = new['id']


def _notice(error):
    st.session_state['_wb_notice'] = str(error)


def _pick_assets(identity, cfg, folder=False):
    request_file('选择素材文件夹' if folder else '选择录像（可多选）', 'folder' if folder else 'video',
                 multiple=not folder, action={'kind':'assets','workspace_id':identity,'folder':folder})


def _pick_state(key, kind, title):
    request_file(title,kind,current_path=st.session_state.get(key,''),action={'kind':'state','key':key})


def _queue(kind, payload, cfg):
    try:
        job = submit_job(kind, payload, cfg)
        st.session_state['_wb_notice'] = '任务已提交：' + job['id'][:8]
        st.rerun()
    except Exception as exc:
        st.error(str(exc))


def inline_tasks(w, cfg):
    """Progress lives at the step where it was requested, with in-place retry."""
    @st.fragment(run_every=4)
    def content():
        composite_id = w.get('source_options',{}).get('composite_id')
        jobs = [j for j in list_jobs(cfg) if j['payload'].get('workspace_id') == w['id'] or
                (composite_id and j['payload'].get('composite_id') == composite_id)]
        if not jobs:
            return
        job = next((j for j in jobs if j['status']=='running'),None) or next((j for j in jobs if j['status']=='queued'),jobs[0])
        state = job['status']
        if state == 'cancelling':
            st.info('正在取消后台任务，已完成的缓存和文件会保留。')
        elif state == 'cancelled':
            st.caption('后台任务已取消，已完成的缓存和文件已保留。')
        elif state in ('queued','running'):
            st.caption('后台任务：' + ('正在处理' if state == 'running' else '等待处理') + ' · ' + job['kind'] + '（关闭网页仍会继续）')
        elif state in ('failed','interrupted'):
            st.error('任务未完成：' + (job.get('error') or state))
            from ui.budget_approval import budget_action
            needs_approval = budget_action(job, cfg, 'wb-budget-'+job['id'])
            if not needs_approval and st.button('重试此任务', key='wb-retry-' + job['id']):
                retry_job(job['id'], cfg)
                st.rerun()
        details,refresh=st.columns([3,1])
        with details.expander('任务进度与日志' if state in ('queued','running','failed','interrupted') else '最近任务已完成 · 详细日志', expanded=False):
            st.code(tail_job_log(job, cfg)[-5000:] or '等待日志…', language='log')
            if job.get('result'): st.json(job['result'], expanded=False)
        if refresh.button('载入最新进度', key='wb-refresh-' + job['id']): st.rerun(scope='app')
    content()


def _thumb(asset, cfg):
    from core.video_input import run_media
    try:
        meta = asset['meta']
        key = fingerprint(meta.get('source_signature') or asset['path'])
        path = Path(cfg['cache_dir'])/'thumbnails'/f'{key}.jpg'
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            run_media([cfg['ffmpeg'],'-v','error','-nostdin','-y','-ss',str(min(3.,asset['duration']/2)),
                       '-i',asset['path'],'-frames:v','1','-vf','scale=240:-2',str(path)],timeout=20)
        return path
    except Exception:
        return None


def import_step(w, cfg):
    st.header('导入素材')
    source_mode=st.radio('来源方式',['本地文件','视频来源'],horizontal=True,key=w['id']+'source-mode')
    if source_mode=='视频来源':
        from ui.source_workflow import source_intake
        source_intake(w,cfg)
        return
    st.caption('直接读取本机文件，不上传、不复制整场录像。所有剪辑方式共用这里的素材。')
    a,b,_ = st.columns([1,1,3])
    a.button('打开文件…', on_click=_pick_assets, args=(w['id'],cfg), use_container_width=True)
    b.button('打开文件夹…', on_click=_pick_assets, args=(w['id'],cfg,True), use_container_width=True)
    for i,asset in enumerate(w['assets']):
        image_col, label, action = st.columns([1,4,1])
        available = Path(asset['path']).is_file()
        thumb = _thumb(asset,cfg) if available else None
        if thumb:
            image_col.image(str(thumb), use_container_width=True)
        label.markdown('**'+html.escape(asset['name'])+'**')
        label.caption(f'{asset["duration"]/60:.1f} 分钟 · ' + ('可访问' if available else '文件已移动或不可访问'))
        with label.expander('素材详情'):
            st.code(asset['path'],language=None)
            st.caption(f"{asset['meta'].get('width','?')} × {asset['meta'].get('height','?')} · 音轨 {len(asset['meta'].get('audio_streams',[]))}")
        if action.button('移除',key=f"asset-remove-{i}"):
            w['assets'].pop(i); _save(w,cfg); st.rerun()
    with st.expander('高级：手动路径备用入口'):
        text = st.text_area('每行一个本地视频路径',key='wb-manual-paths')
        if st.button('加入素材列表'):
            try:
                import_assets(w['id'],[p.strip() for p in text.splitlines() if p.strip()],cfg); st.rerun()
            except Exception as exc:
                st.error(str(exc))
    st.button('下一步：选择剪辑方式',type='primary',disabled=not w['assets'], on_click=_go,args=(w['id'],2,cfg))


def _ordered_assets(w, selected, key):
    by_id = {a['id']:a for a in w['assets']}
    if len(selected) < 2:
        return selected
    rows = st.data_editor([{'素材':by_id[a]['name'],'顺序':i+1,'id':a} for i,a in enumerate(selected)],
                          hide_index=True,disabled=['素材','id'],column_order=['顺序','素材'],key=key,
                          column_config={'顺序':st.column_config.NumberColumn(min_value=1,step=1)})
    return [r['id'] for r in sorted(rows,key=lambda r:r['顺序'])]


def _dual_controls(w, cfg, settings):
    from storage.composites import CompositeStore
    from core.composite_options import normalize_composite_options, overlap_range
    identity = w.get('source_options',{}).get('composite_id')
    if not identity:
        return None
    with CompositeStore(cfg['database']) as store:
        document = store.get_composite(identity)
    if not document:
        return None
    options = normalize_composite_options(document['options'])
    with st.expander('对轴与画中画', expanded=True):
        a,b = st.columns(2)
        semantic = a.checkbox('共同声音不足时用语音事件辅助',key=w['id']+'semantic')
        if b.button('自动寻找同步位置'):
            _queue('compose_sync',{'composite_id':identity,'semantic':semantic,'allow_cpu':settings['allow_cpu']},cfg)
        sync = document.get('sync')
        if sync:
            st.caption(f"置信度：{sync.get('confidence')} · 建议偏移：{sync.get('offset_seconds')} 秒")
            st.write(sync.get('note',''))
            if sync.get('offset_seconds') is not None and st.button('采用建议偏移'):
                w['source_options']['composite_options'] = {**options,'offset_seconds':sync['offset_seconds'],'sync_confirmed':False}
                with CompositeStore(cfg['database']) as store: store.update_composite(identity,options=w['source_options']['composite_options'])
                _save(w,cfg); st.rerun()
        key = w['id']+'-'+fingerprint(options)[:8]
        def offset_changed():
            st.session_state[key+'confirmed']=False
        offset = st.number_input('同步偏移（主播时间 − 比赛时间，秒）',value=float(options['offset_seconds']),step=.05,
                                 key=key+'offset',on_change=offset_changed)
        st.caption('同一事件出现在比赛录像 120 秒、主播录像 360 秒，偏移就是 +240 秒。低置信度时请用两个事件位置手动校准。')
        with st.expander('按同一事件手动校准'):
            a,b = st.columns(2)
            m = a.number_input('事件在比赛录像的秒数',min_value=0.,key=key+'match-anchor')
            c = b.number_input('事件在主播录像的秒数',min_value=0.,key=key+'commentator-anchor')
            if st.button('使用这两个位置'):
                w['source_options']['composite_options'] = {**options,'offset_seconds':c-m,'sync_confirmed':False}
                with CompositeStore(cfg['database']) as store: store.update_composite(identity,options=w['source_options']['composite_options'])
                _save(w,cfg); st.rerun()
        # Reuse bounded visual event matching; it neither imports video nor exports final media.
        from ui.composite_page import _visual_controls
        _visual_controls(document,cfg,options)
        a,b = st.columns(2)
        layout = a.selectbox('主播画面',['fit','face'],index=['fit','face'].index(options['layout']),
                             format_func=lambda x:'完整缩放' if x=='fit' else '人脸跟踪 16:9',key=key+'layout')
        ratio = b.slider('主播宽度占比（%）',10,50,round(options['width_ratio']*100),key=key+'ratio')
        positions = ['bottom_left','bottom_right','top_left','top_right']
        position = a.selectbox('窗口位置',positions,index=positions.index(options['position']),format_func=lambda x:
                              dict(zip(positions,['左下','右下','左上','右上']))[x],key=key+'position')
        audio = b.selectbox('保留声音',['commentator','mix','match'],index=['commentator','mix','match'].index(options['audio']),
                            format_func=lambda x:{'commentator':'只保留主播（比赛静音）','mix':'主播＋比赛','match':'只保留比赛'}[x],key=key+'audio')
        mode = a.selectbox('比赛音轨处理',['original','mute','remove_vocals'],index=['original','mute','remove_vocals'].index(options['match_audio_mode']),
                           format_func=lambda x:{'original':'原声','mute':'静音','remove_vocals':'AI 去人声（可选依赖）'}[x],key=key+'mode')
        with st.expander('音量与去人声设备'):
            mv = st.slider('比赛音量',0.,2.,options['match_volume'],step=.05,key=key+'mv')
            cv = st.slider('主播音量',0.,2.,options['commentator_volume'],step=.05,key=key+'cv')
            device = st.selectbox('去人声设备',['auto','cpu','cuda'],index=['auto','cpu','cuda'].index(options['vocal_device']),key=key+'vd')
        confirmed = st.checkbox('已预览核对同步位置',value=options['sync_confirmed'],key=key+'confirmed')
        value = normalize_composite_options({**options,'offset_seconds':offset,'layout':layout,'width_ratio':ratio/100,
                'position':position,'audio':audio,'match_audio_mode':mode,'sync_confirmed':confirmed,
                'match_volume':mv,'commentator_volume':cv,'vocal_device':device})
        if st.button('保存同步与画面设置'):
            # Existing variants are snapshots. A new setting creates a new variant on continue.
            w['source_options']['composite_options'] = value
            with CompositeStore(cfg['database']) as store:
                store.update_composite(identity,options=value)
            _save(w,cfg); st.rerun()
        try:
            overlap=overlap_range(document['match']['duration'],document['commentator']['duration'],offset)
            st.caption(f"公共范围（比赛录像时间）：{overlap['start']:.2f} – {overlap['end']:.2f} 秒")
            start=st.number_input('同步检查预览起点（秒）',min_value=overlap['start'],max_value=overlap['end'],value=overlap['start'],key=key+'preview')
            if st.button('生成 20 秒同步预览',disabled=start>=overlap['end']):
                _queue('compose_render',{'composite_id':identity,'options':value,'preview':True,'preview_start':start},cfg)
        except ValueError as exc:
            st.warning(str(exc))
        for result in reversed(document.get('outputs',[])):
            if result.get('preview') and Path(result['video']).is_file():
                st.video(result['video']); break
    return {**settings,'composite_id':identity,'composite_options':value}


def method_step(w,cfg):
    st.header('选择剪辑方式')
    if not w['assets']:
        st.info('先导入素材，再选择处理方式。'); return
    if w['variants']:
        ids=[v['id'] for v in w['variants']]
        selected=st.selectbox('当前项目中的方案',ids,index=ids.index(w['active_variant']) if w['active_variant'] in ids else 0,
                              format_func=lambda x:next(v['name'] for v in w['variants'] if v['id']==x))
        if selected != w['active_variant']:
            w['active_variant']=selected; _save(w,cfg); st.rerun()
        if st.button('继续审核已有方案',on_click=_go,args=(w['id'],3,cfg)):
            return
    saved=w.get('source_options',{}); prefix=w['id']+'method-'
    org=st.selectbox('素材组织',list(ORGS),index=list(ORGS).index(saved.get('organization','single')),format_func=ORGS.get,key=prefix+'org')
    asset_map={a['id']:a for a in w['assets']}
    selected=st.multiselect('使用已导入素材',list(asset_map),default=[a for a in saved.get('asset_ids',list(asset_map)) if a in asset_map],
                           format_func=lambda x:asset_map[x]['name'],key=prefix+'assets')
    if org=='sequence':
        st.caption('修改顺序数字，按 1 → 2 → 3 首尾连接。录制缺段不会自动补时间。')
        selected=_ordered_assets(w,selected,prefix+'sequence')
    settings={'organization':org,'asset_ids':selected}
    if org=='dual' and len(selected)>=2:
        match=st.selectbox('比赛主画面',selected,format_func=lambda x:asset_map[x]['name'],key=prefix+'match')
        commentators=st.multiselect('主播画面（可多段）',[a for a in selected if a!=match],default=[a for a in selected if a!=match],
                                   format_func=lambda x:asset_map[x]['name'],key=prefix+'commentators')
        commentators=_ordered_assets(w,commentators,prefix+'commentator-order')
        settings.update(match_id=match,commentator_ids=commentators)
    with st.expander('识别与音轨设置'):
        from core.asr_settings import LABELS as ASR_LABELS
        st.caption('当前语音识别：'+ASR_LABELS[cfg['asr'].get('engine','faster_whisper')]+'；可在侧栏「设置」切换。')
        a,b=st.columns(2)
        track=a.number_input('主素材音轨（从 0 开始）',0,30,int(saved.get('audio_track',0)),key=prefix+'track')
        ctr=b.number_input('主播音轨（从 0 开始）',0,30,int(saved.get('commentator_track',0)),key=prefix+'ctr')
        allow=st.checkbox('CUDA 不可用时允许 CPU 回退',value=saved.get('allow_cpu',False),key=prefix+'cpu')
        recognition=st.selectbox('双路识别声音来源',['commentator','match'],index=0 if saved.get('subtitle_source','commentator')=='commentator' else 1,
                                 format_func=lambda x:'主播转录（默认）' if x=='commentator' else '比赛音轨',key=prefix+'recognition')
        st.button('选择已有转录 JSON…',on_click=_pick_state,args=(prefix+'transcript','json','选择对应素材转录'))
        transcript=st.session_state.get(prefix+'transcript',saved.get('transcript_path'))
        if transcript: st.caption(Path(transcript).name)
        mock=st.checkbox('模拟选片（仅测试，需已有转录）',key=prefix+'mock')
    settings.update(audio_track=track,commentator_track=ctr,allow_cpu=allow,subtitle_source=recognition,mock=mock,transcript_path=transcript)
    if org=='dual':
        previous=(saved.get('match_id',selected[0] if selected else None)==settings.get('match_id')
                  and saved.get('commentator_ids',selected[1:])==settings.get('commentator_ids')
                  and saved.get('audio_track',0)==track and saved.get('commentator_track',0)==ctr)
        if saved.get('composite_id') and previous:
            settings=_dual_controls(w,cfg,settings)
        else:
            st.info('先准备双路素材，再在本步骤对轴和设置画中画。')
            if st.button('准备双路素材',type='primary',disabled=len(selected)<2 or not settings.get('commentator_ids')):
                w['source_options']=settings; _save(w,cfg)
                _queue('workflow_prepare',{'workspace_id':w['id'],'settings':{**settings,'form':'full','prepare_only':True}},cfg)
            return
    form=st.selectbox('成品形式',list(FORMS),index=list(FORMS).index(saved.get('form','shorts')),format_func=FORMS.get,key=prefix+'form')
    settings['form']=form
    if form == 'continuous':
        st.caption('连续长段：例如保留一段连续 10 分钟的原片，段内不跳剪。')
    elif form == 'montage':
        st.caption('全场集锦：从整场比赛选取多个精彩片段，拼成一条集锦。')
    elif form == 'shorts':
        st.caption('切片：把精彩内容分别做成独立短视频。')
    if org=='single' and len(selected)>1:
        default_scope=saved.get('batch_scope','combined' if form=='montage' else 'separate')
        scope=st.radio('多素材制作范围',['combined','separate'],index=0 if default_scope=='combined' else 1,
            format_func=lambda x:'整批合成：数量与时长对全部素材生效' if x=='combined' else '分别制作：每份素材各自生成指定数量',
            key=prefix+'scope-'+form)
        settings['batch_scope']=scope
        if scope=='combined':
            with st.expander('素材先后顺序'):
                selected=_ordered_assets(w,selected,prefix+'batch-order')
            settings['asset_ids']=selected
    if form!='full':
        from ui.plans_page import editorial_controls
        with st.expander('内容标准、数量、时长与必剪事件',expanded=True):
            options=editorial_controls(cfg,prefix+'editorial-',forced_format=form,initial_options=saved.get('editing_options'))
        settings['editing_options']=options
        if options:
            scope_label='每份素材' if org=='single' and len(selected)>1 and settings.get('batch_scope')=='separate' else '整批总计'
            st.info(f"{scope_label} {options['count']} 条成品 · 每条目标 {options['target_seconds']/60:g} 分钟（{options['min_seconds']/60:g}～{options['max_seconds']/60:g} 分钟）")
    settings['name']=st.text_input('方案名称',value=f'方案 {len(w["variants"])+1}',key=prefix+'name')
    if st.button('保存当前设置，稍后处理'):
        w['source_options']=settings;_save(w,cfg);st.rerun()
    if st.button('保存并生成待审核成品',type='primary',disabled=not selected or (form!='full' and not settings.get('editing_options'))):
        if mock and not transcript:
            st.error('模拟选片需要先选择已有转录 JSON');return
        w['source_options']=settings; _save(w,cfg)
        _queue('workflow_prepare',{'workspace_id':w['id'],'settings':settings},cfg)


def _media(w,item,cfg):
    with WorkspaceStore(cfg['database']) as store:
        records=store.list_media(w['id'])
    from core.workflow import visual_signature
    sig=visual_signature(item)
    for record in sorted(records,key=lambda r:not bool(r['preview'])):
        result=record['result']
        if result.get('packaging_signature')!=fingerprint(w.get('packaging') or {}):
            continue
        if record['preview'] and not result.get('preview_complete'):
            continue  # Old sampled previews cannot stand in for the full reviewed video.
        prior=record.get('snapshot',{}).get('item') or result.get('snapshot',{}).get('item')
        recorded=result.get('visual_signature') or (visual_signature(prior) if prior else None)
        if (recorded==sig or result.get('item_signature')==fingerprint(item)) and Path(result['video']).is_file():
            return result['video'],result.get('preview_note','')
    if len(item['segments'])==1 and item.get('legacy_preview') and Path(item['legacy_preview']).is_file():
        return item['legacy_preview'],'原有预览'
    for output in item.get('legacy_outputs',[]):
        if Path(output.get('path','')).is_file():
            return output['path'],'原有成品'
    return None,None


def _range_rows(item):
    """Show original filenames/local times separately from the combined timeline."""
    meta=item['source'].get('meta',{})
    if meta.get('virtual_timeline'):
        from core.batch_timeline import split_ranges
        cuts=split_ranges(meta,item['segments'])
    else:
        cuts=[{'meta':meta,**s} for s in item['segments']]
    return [{'顺序':i,'素材':Path(c['meta'].get('source','比赛录像')).name,
             '原片开始（秒）':round(c['start'],2),'原片结束（秒）':round(c['end'],2),
             '时长（秒）':round(c['end']-c['start'],2)} for i,c in enumerate(cuts,1)]


def review_step(w,cfg):
    st.header('预览审核')
    variant=active_variant(w)
    if not variant or not variant['items']:
        st.info('当前还没有候选。请返回第二步生成方案。');return
    items=variant['items']; lookup={i['id']:i for i in items}
    st.caption(f"{variant['name']} · 最终成品 {len(items)} 条 · {sum(i['status']=='approved' for i in items)} 条通过")
    if st.button('AI 生成本批抖音 / B站标题'):
        _queue('workflow_titles',{'workspace_id':w['id']},cfg)
    if st.button('从已有话题恢复本批标题（不联网）'):
        _queue('workflow_titles',{'workspace_id':w['id'],'cached_only':True},cfg)
    target=variant.get('options',{}).get('count')
    if variant['form']=='montage' and target and len(items)>target and len(w['assets'])>1 and w.get('source_options',{}).get('batch_scope')!='separate':
        st.warning(f'此旧方案按每份素材分别生成了 {len(items)} 条。要整批只生成 {target} 条，请返回第二步选择“整批合成”。旧审核记录会保留。')
    with st.container(key='review-layout'):
        left,middle,right=st.columns([1,2.4,1.5],gap='medium')
    with left:
        state=st.selectbox('筛选',['all','pending','approved','rejected'],format_func=lambda x:'全部' if x=='all' else STATUS[x])
        choices=[i['id'] for i in items if state=='all' or i['status']==state]
        if not choices: st.info('当前筛选没有候选');return
        next_item=st.session_state.pop('_wb_next_item',None)
        if next_item in choices:
            st.session_state['wb-candidate-'+variant['id']]=next_item
        if st.session_state.get('wb-candidate-'+variant['id']) not in choices:
            st.session_state['wb-candidate-'+variant['id']]=choices[0]
        chooser=st.radio if len(choices)<=4 else st.selectbox
        selection=chooser('成品列表',choices,format_func=lambda x:f"{items.index(lookup[x])+1}. {lookup[x]['title']} · {sum(s['end']-s['start'] for s in lookup[x]['segments'])/60:.1f} 分钟 · {STATUS[lookup[x]['status']]}",key='wb-candidate-'+variant['id'])
    item=lookup[selection]
    key=item['id']+'-'+str(item['revision'])
    with middle:
        path,note=_media(w,item,cfg)
        if path:
            st.video(path);st.caption(note or '')
        else:
            st.info('尚无当前版本的预览。生成后可播放；原有成品保留为历史。')
        if st.button('重新生成预览' if path else '重试生成预览'):
            _queue('workflow_preview',{'workspace_id':w['id'],'item_id':item['id']},cfg)
        st.button('修改字幕',key=key+'-edit-captions',on_click=_open_subtitles,args=(w['id'],item['id'],cfg),type='tertiary')
        st.caption(f"正文时长 {sum(s['end']-s['start'] for s in item['segments'])/60:.2f} 分钟 · {len(item['segments'])} 个组成片段")
        scored=item.get('score') is not None and (item.get('score')!=0 or item.get('score_source'))
        st.metric('AI 推荐评分', f"{item['score']:g} / 100" if scored else '未评分')
        st.caption('用于比较候选的启发式评分，不代表传播概率。')
        if item.get('reason'): st.write(item['reason'])
        review=item.get('subtitle_review') or {}
        if review.get('status')=='complete': st.caption(f"字幕 AI 已校对 · 修正 {review.get('changed',0)} 处")
        elif item.get('subtitle_text') is not None: st.caption('字幕来源：已保存的人工字幕')
        else: st.warning('此条尚无完整的 API 字幕复核记录，不能视为已校对。')
        if st.button('修复本方案字幕、评分并生成预览',key='quality-repair-'+variant['id']):
            _queue('workflow_repair',{'workspace_id':w['id']},cfg)
        if item['source']['kind']=='dual': st.caption('时间范围以比赛录像为准，主播画面按已保存偏移同步。')
        if variant['form']=='montage':
            with st.expander(f"组成片段 · {len(item['segments'])} 段（不是独立成品）"):
                st.dataframe(_range_rows(item),hide_index=True,width='stretch')
    with right:
        with st.form('wb-review-'+key):
            title=st.text_input('标题',value=item['title'])
            platform_titles=item.get('platform_titles') or {}
            douyin_title=st.text_input('抖音投稿标题',value=platform_titles.get('douyin',item['title']),max_chars=100)
            bilibili_title=st.text_input('B站投稿标题',value=platform_titles.get('bilibili',item['title']),max_chars=80)
            description=st.text_area('发布文案（可选）',value=item.get('description') or '',height=90)
            if variant['form']=='montage':
                with st.expander('修改组成区间（整批时间轴秒数）'):
                    rows=st.data_editor(item['segments'],num_rows='dynamic',hide_index=True,width='stretch',height=220,
                        column_config={'start':st.column_config.NumberColumn('开始（秒）',width='small'),
                                       'end':st.column_config.NumberColumn('结束（秒）',width='small')},key=key+'ranges')
            else:
                start=st.number_input('原片开始（秒）',min_value=0.,value=float(item['segments'][0]['start']),step=.1,key=key+'start')
                end=st.number_input('原片结束（秒）',min_value=0.,value=float(item['segments'][0]['end']),step=.1,key=key+'end')
                rows=[{'start':start,'end':end}]
            decision=st.radio('审批决定',['pending','approved','rejected'],index=['pending','approved','rejected'].index(item['status']),format_func=STATUS.get,horizontal=True)
            rating=st.selectbox('我的评分',[None,1,2,3,4,5],index=[None,1,2,3,4,5].index(item.get('user_rating')),format_func=lambda x:'暂不评分' if x is None else '★'*x)
            st.caption(f'当前 {choices.index(selection)+1} / {len(choices)} 条；切换时保存本条修改。')
            advance=st.checkbox('保存后切换下一条',value=True)
            save=st.form_submit_button('保存审核',use_container_width=True,type='primary')
            prev_col,next_col=st.columns(2)
            previous=prev_col.form_submit_button('上一条',use_container_width=True)
            following=next_col.form_submit_button('下一条',use_container_width=True)
        if save or previous or following:
            try:
                edit_item(w['id'],item['id'],{'title':title,'platform_titles':{'douyin':douyin_title,'bilibili':bilibili_title},'description':description,'segments':rows,'status':decision,'user_rating':rating,'expected_revision':item['revision']},cfg)
                if (previous or following or advance) and len(choices)>1:
                    st.session_state['_wb_next_item']=choices[(choices.index(selection)+(-1 if previous else 1))%len(choices)]
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        if st.button('AI 重新生成本条双平台标题',key=key+'-titles'):
            _queue('workflow_titles',{'workspace_id':w['id'],'item_id':item['id'],'force':True},cfg)
        title_state=item.get('title_generation',{})
        if title_state.get('status') in ('failed','unconfigured'):
            st.caption('自动标题尚未完成，可重试；当前保存的标题保留。')
        if item.get('manual_platform_titles'):
            st.caption('已保存的人工投稿标题会保留；AI 重试只更新未锁定的平台标题。')
        if item.get('subtitle_stale'):
            st.warning('范围已改变，人工字幕已保留；请到第四步核对。')
    if variant.get('events'):
        if variant.get('clock_reports'):
            with st.expander('自动比赛对轴结果'):
                for report in variant['clock_reports']:
                    st.caption(report['source'])
                    st.dataframe(report['anchors'], hide_index=True)
        with st.expander('必剪事件核对',expanded=True):
            st.caption('确认需以画面为准；未包含在通过成品中的事件会阻止导出。')
            rows=st.data_editor(variant['events'],disabled=['id','label','period','match_seconds'],hide_index=True,key=variant['id']+'events-review')
            if st.button('保存事件核对'):
                variant['events']=rows;_save(w,cfg);st.rerun()
    warnings=list(dict.fromkeys(variant.get('warnings',[])))
    if warnings:
        with st.expander(f'需要处理的问题 · {len(warnings)} 项'):
            for warning in warnings: st.caption(warning)
    st.button('下一步：字幕与包装',type='primary',on_click=_go,args=(w['id'],4,cfg))


def _load_srt(key, item, cfg):
    request_file('选择 SRT 字幕','srt',action={'kind':'srt','key':key,
                 'duration':sum(s['end']-s['start'] for s in item['segments'])})


def packaging_step(w,cfg):
    from core.subtitle_styles import SUBTITLE_PRESETS
    st.header('字幕与包装')
    variant=active_variant(w)
    if not variant or not variant['items']: st.info('先生成并审核成品。');return
    defaults=w.get('packaging') or {}
    options=defaults.get('export_options') or default_export_options(cfg)
    prefix=w['id']+'pack-'
    enabled=st.checkbox('为本批成品添加字幕',value=defaults.get('subtitles',True),key=prefix+'enabled')
    proofread=st.checkbox('自动字幕生成后使用 AI 校对',value=defaults.get('proofread_subtitles',cfg.get('subtitle_review',{}).get('enabled',False)),
        key=prefix+'proofread',disabled=not enabled)
    if enabled:
        choices=[i['id'] for i in variant['items']]
        chooser_key=w['id']+'-subtitle-item'
        if st.session_state.get(chooser_key) not in choices:
            st.session_state[chooser_key]=choices[0]
        item_id=st.selectbox('逐条编辑字幕',choices,key=chooser_key,format_func=lambda x:next(i['title'] for i in variant['items'] if i['id']==x))
        item=next(i for i in variant['items'] if i['id']==item_id)
        subtitle_key=item_id+'-srt-'+str(item['revision'])+'-'+str(proofread)
        subtitle_cfg=copy.deepcopy(cfg);subtitle_cfg['subtitle_review']['enabled']=proofread
        if subtitle_key not in st.session_state:
            try: st.session_state[subtitle_key]=subtitle_default(item,subtitle_cfg)
            except Exception: st.session_state[subtitle_key]=item.get('subtitle_text') or ''
        if item.get('subtitle_stale'): st.warning('范围已修改。以下保留了原人工字幕，请按新范围核对文字和时间，再保存。')
        if not item.get('transcript_key'):
            st.info('完整输出尚未转录，可识别声音或导入 SRT。也可关闭本批字幕。')
        a,b=st.columns(2)
        a.button('打开 SRT…',on_click=_load_srt,args=(subtitle_key,item,cfg))
        recognition=b.selectbox('识别声音来源',['commentator','match'] if item['source']['kind']=='dual' else ['single'],
                               format_func=lambda x:{'commentator':'主播','match':'比赛','single':'原素材'}[x])
        if b.button('识别 / 切换声音来源'):
            _queue('workflow_transcribe',{'workspace_id':w['id'],'item_id':item_id,'settings':{'subtitle_source':recognition}},cfg)
        from ui.subtitle_tools import subtitle_tools
        subtitle_tools(subtitle_key,sum(s['end']-s['start'] for s in item['segments']))
        text=st.text_area('字幕文字与时间（正文相对时间，SRT）',height=230,key=subtitle_key)
        if st.button('保存此条人工字幕'):
            try: edit_item(w['id'],item_id,{'subtitle_text':text},cfg);st.rerun()
            except Exception as exc: st.error(str(exc))
        if st.button('恢复自动字幕（替换此条人工修改）'):
            try: edit_item(w['id'],item_id,{'subtitle_text':None},cfg);st.rerun()
            except Exception as exc: st.error(str(exc))
        if proofread:
            from core.subtitle_review import item_cues
            try: _,review=item_cues(item,subtitle_cfg)
            except Exception: review={'status':'pending','changed':0}
            labels={'complete':'已校对','partial':'部分完成，失败部分保留原文','failed':'失败，已保留原文','pending':'待校对',
                'unconfigured':'需配置 API 密钥和模型','manual':'人工字幕，跳过自动校对','mock':'模拟字幕，跳过 API','disabled':'未启用','empty':'没有字幕','no_transcript':'尚未识别'}
            st.caption(f"AI 校对：{labels.get(review['status'],review['status'])} · 修正 {review.get('changed',0)} 段 · {review.get('rejected',0)} 段改动过大已保留原文")
            with st.expander('AI 校对记录与重试'):
                st.caption('对照本地 ASR 原文进行文本上下文校对与断句；不代表模型重新听过音频。不确定人名保留，人工字幕跳过。与选片共用项目 API 预算。')
                changes=review.get('changes',[])
                if changes: st.dataframe([{'原文':c['before'],'建议':c.get('after',c.get('proposed')),'处理':'已修正' if c['applied'] else '改动过大，保留原文'} for c in changes],hide_index=True)
                if st.button('AI 校对本批自动字幕（失败项可重试）'):
                    try:
                        if text!=subtitle_default(item,subtitle_cfg):
                            st.warning('有未保存的文字修改，请先保存人工字幕再校对其他自动字幕。')
                        else:
                            w['packaging']={**defaults,'proofread_subtitles':True};_save(w,cfg)
                            _queue('workflow_subtitle_review',{'workspace_id':w['id'],'retry_failed':True},cfg)
                    except Exception as exc: st.error(str(exc))
        st.caption('字幕时间从剪辑正文开头计算；系统会在导出时加上片头时长。')
    st.subheader('本批统一外观')
    style=options['subtitle']
    def preset_change():
        selected=SUBTITLE_PRESETS[st.session_state[prefix+'preset']]
        for field in ('primary_color','outline_color','font_size','outline','shadow','bold','italic','border_style','back_alpha'):
            if field in selected: st.session_state[prefix+field]=selected[field]
    for field,value in style.items():
        st.session_state.setdefault(prefix+field,value)
    for field in ('font_size','vertical_font_size'):
        st.session_state[prefix+field]=float(st.session_state[prefix+field])
    a,b,c=st.columns([2,1,1])
    preset=a.selectbox('字幕样式',list(SUBTITLE_PRESETS),key=prefix+'preset',format_func=lambda x:SUBTITLE_PRESETS[x]['label'],on_change=preset_change)
    color=b.color_picker('文字颜色',key=prefix+'primary_color')
    outline_color=c.color_picker('描边颜色',key=prefix+'outline_color')
    with st.expander('字体与布局'):
        font=st.text_input('字体',key=prefix+'font')
        a,b=st.columns(2)
        size=a.number_input('横屏字号',min_value=12.,max_value=200.,step=.5,key=prefix+'font_size')
        vsize=b.number_input('竖屏字号',min_value=12.,max_value=200.,step=.5,key=prefix+'vertical_font_size')
        border=a.number_input('描边',min_value=0.,max_value=20.,value=float(style['outline']),key=prefix+'border')
        shadow=b.number_input('阴影',min_value=0.,max_value=20.,value=float(style['shadow']),key=prefix+'shadow-value')
        margin=a.number_input('横屏底部间距',min_value=0,max_value=1000,key=prefix+'margin_v')
        vm=b.number_input('竖屏底部间距',min_value=0,max_value=1500,key=prefix+'vertical_margin_v')
        offset=st.number_input('字幕下移（画面宽度 %）',min_value=-50.,max_value=50.,value=float(style.get('offset_y_width_ratio',.05))*100,step=.5,key=prefix+'y-offset')
        st.caption('默认字号为原34号的75%（25.5号）；下移超过画面底边时保留4像素安全距离。')
    burn=st.checkbox('将字幕烧录到视频（同时保留 SRT / ASS）',value=options['burn_subtitles'],key=prefix+'burn')
    intro=st.checkbox('本批成品添加片头',value=options['intro']['enabled'],key=prefix+'intro-on')
    st.session_state.setdefault(prefix+'intro',options['intro']['path'])
    if intro:
        st.button('打开片头视频…',on_click=_pick_state,args=(prefix+'intro','video','选择自己的片头'))
        st.caption(Path(st.session_state[prefix+'intro']).name or '尚未选择片头')
    if st.button('保存包装并前往导出',type='primary'):
        try:
            try: original_text=subtitle_default(item,subtitle_cfg) if enabled else ''
            except Exception: original_text=item.get('subtitle_text') or ''
            if enabled and text != original_text:
                edit_item(w['id'],item_id,{'subtitle_text':text},cfg)
                with WorkspaceStore(cfg['database']) as store: w=store.get_workspace(w['id'])
            selected_style={**style,'preset':preset,'font':font,'font_size':size,'vertical_font_size':vsize,
                'primary_color':color,'outline_color':outline_color,'outline':border,'shadow':shadow,'margin_v':margin,'vertical_margin_v':vm,'offset_y_width_ratio':offset/100}
            for f in ('bold','italic','border_style','back_alpha'): selected_style[f]=st.session_state[prefix+f]
            export_options=normalize_export_options({'subtitle':selected_style,'burn_subtitles':burn,'intro':{'enabled':intro,'path':st.session_state[prefix+'intro']}})
            if intro and not Path(export_options['intro']['path']).is_file(): raise ValueError('请先选择可访问的片头文件')
            w['packaging']={'export_options':export_options,'subtitles':enabled,'proofread_subtitles':proofread};w['step']=5
            _save(w,cfg);st.rerun()
        except Exception as exc: st.error(str(exc))


def export_step(w,cfg):
    from ui.export_center import panel
    panel(w,cfg)


def project_export_step(w,cfg):
    from ui.publishing import (publication_controls, publication_confirmation,
                               publication_jobs_panel, consume_authorization)
    st.header('统一导出')
    variant=active_variant(w)
    if not variant: st.info('请先生成剪辑方案。');return
    approved=[i for i in variant['items'] if i['status']=='approved']
    st.caption('当前项目 · '+w['name']+' / '+variant['name'])
    st.dataframe([{'标题':i['title'],'时长（秒）':round(sum(s['end']-s['start'] for s in i['segments']),2),
                   '字幕':'需要核对' if i.get('subtitle_stale') else '人工' if i.get('subtitle_text') is not None else '自动'} for i in approved],hide_index=True,use_container_width=True)
    saved=w.get('export_settings',{});prefix=w['id']+'export-'
    saved={**saved.get('render',{}),**saved}
    orientations=st.multiselect('输出画幅',['horizontal','vertical'],default=saved.get('orientations',['horizontal']),
                                format_func=lambda x:'横屏 16:9' if x=='horizontal' else '竖屏 9:16（完整画面＋模糊背景）',key=prefix+'orientations')
    a,b=st.columns(2)
    resolution=a.selectbox('分辨率',['1080p','720p','原配置'],index=['1080p','720p','原配置'].index(saved.get('resolution','1080p')),key=prefix+'resolution')
    encoder=b.selectbox('编码器',['auto','h264_nvenc','libx264'],index=['auto','h264_nvenc','libx264'].index(saved.get('encoder','auto')),
                         format_func=lambda x:{'auto':'自动（优先 NVIDIA）','h264_nvenc':'NVIDIA NVENC（失败自动回退）','libx264':'CPU 软件编码'}[x],key=prefix+'encoder')
    st.session_state.setdefault(prefix+'folder',saved.get('output_dir',cfg['output_dir']))
    st.button('选择输出文件夹…',on_click=_pick_state,args=(prefix+'folder','folder','选择输出文件夹'))
    st.caption(st.session_state[prefix+'folder'])
    with st.expander('高级输出参数'):
        bitrate=st.text_input('视频码率',value=saved.get('bitrate',cfg['render']['bitrate']),key=prefix+'bitrate')
        fps=st.number_input('帧率',min_value=1,max_value=120,value=int(saved.get('fps',cfg['render']['fps'])),key=prefix+'fps')
    render={k:saved.get('render',{}).get(k,cfg['render'][k]) for k in ('horizontal','vertical')}
    if resolution!='原配置':
        render.update(horizontal=[1920,1080] if resolution=='1080p' else [1280,720],vertical=[1080,1920] if resolution=='1080p' else [720,1280])
    render.update(encoder=encoder,bitrate=bitrate,fps=fps)
    publication,publication_ready,publisher=publication_controls(w,cfg,orientations)
    snapshot=None
    try:
        if publication:
            snapshot=export_snapshot(w,cfg,orientations,render,st.session_state[prefix+'folder'],publication=publication)
        else:
            snapshot=export_snapshot(w,cfg,orientations,render,st.session_state[prefix+'folder'])
    except Exception as exc:
        st.warning('导出前需要处理：'+str(exc))
        if publication and '字幕' in str(exc):
            st.button('返回第四步完成字幕',key=prefix+'return-subtitles',on_click=_go,args=(w['id'],4,cfg))
    st.write(f"{len(approved)} 条通过 · 正文总时长 {sum(s['end']-s['start'] for i in approved for s in i['segments'])/60:.1f} 分钟 · {len(orientations)} 种画幅")
    publication_authorized=publication_confirmation(w,snapshot,publication) if publication and snapshot else False
    automation=w.get('automation') or {}
    export_label='完成本批审核并自动导出' if automation.get('flow_id') else '开始导出'
    if st.session_state.get(w['id']+'-publication-destination') in ('douyin','bilibili'):
        target_label='B站' if st.session_state[w['id']+'-publication-destination']=='bilibili' else '抖音'
        export_label='导出并准备发布到'+target_label
    if publication:
        target_label='B站' if publication['platform']=='bilibili' else '抖音'
        export_label=('导出并准备发布到' if publication['mode']=='prepare' else '导出并自动提交到')+target_label
    if st.button(export_label,type='primary',disabled=snapshot is None or not publication_ready or (publication is not None and not publication_authorized)):
        if publication: consume_authorization(w)
        w['export_settings']={'orientations':orientations,'resolution':resolution,'encoder':encoder,'output_dir':st.session_state[prefix+'folder'],'fps':fps,'bitrate':bitrate,'render':render}
        _save(w,cfg)
        if automation.get('flow_id'):
            try:
                from core.automation_client import AutomationClient
                AutomationClient(cfg).call('flows/'+automation['flow_id']+'/confirm',{'snapshot':snapshot})
                st.session_state['_wb_notice']='已确认本批审核，后台将导出通过项。'+('发布任务将按本批授权继续处理。' if publication else '')
                st.rerun()
            except RuntimeError as exc:st.error(str(exc))
        else:
            _queue('workflow_export',{'workspace_id':w['id'],'snapshot':snapshot},cfg)
    from ui.source_workflow import save_template_panel
    save_template_panel(w,cfg,orientations,render,st.session_state[prefix+'folder'])
    with WorkspaceStore(cfg['database']) as store:
        records=store.list_media(w['id'],False)
    if records:
        st.subheader('成品与历史')
        lookup={i['id']:i for v in w['variants'] for i in v['items']}
        rows=[]
        for record in records:
            r=record['result']; current=lookup.get(r['item_id'])
            fresh=(current and current in variant['items'] and fingerprint(current)==r['item_signature']
                   and r.get('packaging_signature')==fingerprint(w.get('packaging') or {}) and not r.get('historical'))
            rows.append({'标题':r['title'],'画幅':r['orientation'],'版本':'当前' if fresh else '历史，内容已修改','文件':r['video']})
        st.dataframe(rows,use_container_width=True,hide_index=True)
        if st.button('打开输出文件夹'):
            try: open_folder(str(Path(records[0]['result']['video']).parent))
            except Exception as exc: st.error(str(exc))
    # Existing media are never rewritten or renamed during migration.
    if any(i.get('legacy_outputs') for i in variant['items']):
        with st.expander('升级前成品（保留原位置）'):
            st.dataframe([{'标题':i['title'],'文件':o['path']} for i in variant['items'] for o in i.get('legacy_outputs',[])],hide_index=True)
    publication_jobs_panel(w,cfg,publisher)


def library(cfg):
    st.header('项目库')
    with WorkspaceStore(cfg['database']) as store:
        workspaces=store.list_workspaces()
    if not workspaces: st.info('还没有项目。进入工作台导入第一份素材。');return
    for w in workspaces:
        a,b=st.columns([5,1])
        variant=active_variant(w);items=variant['items'] if variant else []
        a.write(w['name']);a.caption(f"{len(w['assets'])} 份素材 · {len(items)} 条候选 · {sum(i['status']=='approved' for i in items)} 条通过 · {w['status']}")
        if b.button('打开工作台',key='open-'+w['id']):
            with WorkspaceStore(cfg['database']) as store: store.preference('current_workspace',w['id'])
            st.session_state['_wb_select_next']=w['id'];st.session_state['_wb_go_page']='工作台';st.rerun()
    with st.expander('原有候选统计'):
        from ui.app import stats_page
        with WorkspaceStore(cfg['database']) as store: stats_page(store)


def main():
    from core.config import load_config
    from core.jobs import ensure_worker
    from core.version import VERSION
    from ui.app import jobs_page
    st.set_page_config(page_title='Football Clip Factory',page_icon='⚽',layout='wide')
    st.markdown('''<style>
      .block-container {padding-top:1.5rem; max-width:1550px; padding-bottom:3rem}
      [data-testid="stSidebar"] h1 {font-size:1.2rem}
      div.stButton>button {min-height:2.6rem}
      h1 {font-size:1.8rem !important} h2 {font-size:1.35rem !important}
      [data-testid="stMetricValue"] {font-size:1.3rem}
      video {max-height:340px; object-fit:contain; background:#000}
      .st-key-workbench-header {position:sticky;top:2.85rem;z-index:9;background:light-dark(#fff,#0e1117);padding-bottom:.5rem}
      @media(max-width:1000px){
        .block-container{padding-left:1rem;padding-right:1rem}
        .st-key-review-layout [data-testid="stHorizontalBlock"] {flex-wrap:wrap}
        .st-key-review-layout [data-testid="stColumn"] {flex:1 1 100%;width:100%;min-width:0}
      }
    </style>''',unsafe_allow_html=True)
    cfg=load_config();ensure_worker(cfg);adopt_legacy(cfg)
    apply_file_result(cfg)
    file_picker_panel()
    st.sidebar.title('Football Clip Factory');st.sidebar.caption('制作工作台 · v'+VERSION)
    from core.asr_settings import LABELS as ASR_LABELS
    st.sidebar.caption('语音识别：'+ASR_LABELS[cfg['asr'].get('engine','faster_whisper')])
    review_provider='API' if cfg['openai'].get('provider','api')=='api' else 'Codex'
    st.sidebar.caption('字幕语义复核：'+(review_provider+' 文本复核已启用' if cfg.get('subtitle_review',{}).get('enabled') else '未启用'))
    st.sidebar.caption('本地识别后再校对文字；并非两套音频识别。')
    go_page=st.session_state.pop('_wb_go_page',None)
    if go_page: st.session_state['wb-page']=go_page
    def open_api_settings():
        st.session_state['wb-page']='API 设置'
    st.sidebar.button('配置 API',key='open-api-settings',on_click=open_api_settings,use_container_width=True,type='primary')
    page=st.sidebar.radio('导航',['工作台','项目库','任务记录','API 设置','设置'],key='wb-page')
    with WorkspaceStore(cfg['database']) as store:
        workspaces=store.list_workspaces()
        if not workspaces: workspaces=[store.create_workspace()]
        ids=[w['id'] for w in workspaces];saved=store.preference('current_workspace')
        select_next=st.session_state.pop('_wb_select_next',None)
        if select_next in ids: st.session_state['wb-project']=select_next
        if st.session_state.get('wb-project') not in ids: st.session_state['wb-project']=saved if saved in ids else ids[0]
        identity=st.sidebar.selectbox('当前项目',ids,format_func=lambda x:next(w['name'] for w in workspaces if w['id']==x)+' · '+x[:8],key='wb-project')
        store.preference('current_workspace',identity)
        w=store.get_workspace(identity)
    st.sidebar.button('＋ 新建项目', on_click=_new_workspace, args=(cfg,))
    st.sidebar.caption('进度、审批、人工字幕保存在本地。')
    from ui.llm_settings import PROVIDERS
    st.sidebar.caption('AI：'+PROVIDERS[cfg['openai'].get('provider', 'api')]+' · '+
                       (cfg['openai'].get('model') or '默认模型')+' · '+cfg['openai'].get('reasoning_effort', 'default'))
    notice=st.session_state.pop('_wb_notice',None)
    if notice: st.info(notice)
    if page=='API 设置':
        from ui.llm_settings import panel as llm_panel
        llm_panel(cfg)
        return
    if page=='项目库': library(cfg);return
    if page=='任务记录': jobs_page(cfg);return
    if page=='设置':
        st.header('设置')
        from ui.asr_settings import panel as asr_panel
        from ui.llm_settings import panel as llm_panel
        llm_panel(cfg)
        asr_panel(cfg)
        st.caption('配置入口（GPU / CPU 启动器共用主目录配置）')
        st.code(str(Path(os.environ.get('FCF_PRIMARY_ROOT') or cfg['root'])/'.env'),language=None)
        st.caption('GPU 是否可用以任务日志和 doctor 检查为准。CPU 启动入口使用独立数据环境。')
        from ui.publishing import account_connection_panel
        account_connection_panel(cfg)
        return
    with st.container(key='workbench-header'):
        st.title(w['name'])
        st.caption(f"当前：{w['step']} / 5 · {STEPS[w['step']-1]}　｜　{w['status']}")
        columns=st.columns(5)
        for i,col in enumerate(columns,1):
            col.button(f'{i:02}  {STEPS[i-1]}',key=f'wb-step-{i}',use_container_width=True,
                       disabled=i==w['step'],on_click=_go,args=(identity,i,cfg))
    with st.sidebar.expander('项目名称'):
        name=st.text_input('项目名称',value=w['name'],label_visibility='collapsed')
        if st.button('保存名称'):
            w['name']=name.strip() or '未命名项目';_save(w,cfg);st.rerun()
    inline_tasks(w,cfg)
    try:
        [import_step,method_step,review_step,packaging_step,export_step][w['step']-1](w,cfg)
    except Exception as exc:
        st.error('本步骤未完成：'+str(exc))
        st.caption('已保存的内容保持不变；可刷新重试或查看任务记录。')
