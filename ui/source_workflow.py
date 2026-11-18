"""Source intake and template controls inside the existing five-step workbench."""
import copy
import uuid
from pathlib import Path
import streamlit as st
from core.automation_client import AutomationClient
from core.utils import fingerprint

LABELS={'acquiring':'获取来源','resolving':'识别来源','downloading':'下载中','preparing':'转录与选片',
        'previewing':'生成预览','awaiting_review':'待审核','exporting':'导出中','completed':'已完成',
        'failed':'失败，可恢复','needs_assistance':'需要协助','cancelled':'已取消'}


def source_intake(w,cfg):
    st.caption('提交回放网页，选择制作模板。候选准备好后人工审核，整批确认后输出成品。')
    try:
        client=AutomationClient(cfg);client.call('health')
        templates=client.call('templates')['templates']
    except RuntimeError as exc:
        st.info(str(exc));return
    with st.form('source-submit'):
        url=st.text_input('视频来源网页',placeholder='咪咕全场回放，或包含 recordId 的海星回放地址')
        template_ids=[t['id'] for t in templates]
        template_id=st.selectbox('制作模板',template_ids,index=template_ids.index('match-shorts') if 'match-shorts' in template_ids else 0,
            format_func=lambda x:next(t['name'] for t in templates if t['id']==x))
        title=st.text_input('批次名称（可选）')
        selected=next(t for t in templates if t['id']==template_id)
        content=selected['settings'].get('editing_options',{})
        count_label=f"{content['count']} 条" if 'count' in content else '完整输出'
        st.caption(f"模板修订 {selected['revision']} · {count_label} · 来源至少 {selected['source'].get('minimum_minutes',0)} 分钟 · 导出 {'、'.join('横屏' if x=='horizontal' else '竖屏' for x in selected['export']['orientations'])}")
        submitted=st.form_submit_button('提交来源并自动生成候选',type='primary')
    if submitted:
        try:
            data={'source_url':url.strip(),'template_id':template_id,'template_revision':selected['revision'],'title':title.strip()}
            signature=fingerprint(data)
            request_id=st.session_state.setdefault('source-requests',{}).setdefault(signature,uuid.uuid4().hex)
            flow=client.call('flows',{**data,'idempotency_key':request_id})
            st.success('已提交。可关闭网页，任务会在后台继续。');st.session_state['source-last-flow']=flow['id']
        except RuntimeError as exc:st.error(str(exc))
    st.button('刷新来源任务',key='source-refresh')
    try:
        components=client.call('connections')
        browser=components.get('lightboat',{}).get('browser_connected',False)
        if not browser:st.caption('海星公开回放可直接处理。咪咕首次使用：在 Edge 轻舟扩展中点击“连接自动工作流”，并保持 Edge 运行。')
    except RuntimeError:pass
    try:flows=client.call('flows')['flows']
    except RuntimeError as exc:st.warning(str(exc));return
    if not flows:st.info('尚无来源任务。');return
    for flow in flows[:30]:
        with st.expander(flow['title']+' · '+LABELS.get(flow['status'],flow['status']),expanded=flow['id']==st.session_state.get('source-last-flow')):
            st.write(flow['message']);st.caption(flow['source_url'])
            job=flow.get('download') or {}
            if job.get('total'):st.progress(min(1.,job.get('completed',0)/job['total']),text=f"下载分段 {job.get('completed',0)} / {job['total']}")
            if flow['workspace_id']:
                if st.button('进入本批工作台',key='source-open-'+flow['id']):
                    st.session_state['_wb_select_next']=flow['workspace_id'];st.rerun()
            if flow['status'] in ('failed','needs_assistance'):
                st.link_button('打开原回放网页',flow['source_url'])
                candidates=job.get('candidate_summaries') or []
                if candidates:
                    selected_id=st.selectbox('确认回放资源',[c['id'] for c in candidates],key='source-choice-'+flow['id'],
                        format_func=lambda x:next(f"{c['duration']/60:.1f} 分钟 · {c['resolution'] or '源画质'} · {c['title']}" for c in candidates if c['id']==x))
                    if st.button('采用此回放并继续',key='source-choose-'+flow['id']):
                        try:client.call('flows/'+flow['id']+'/choose',{'candidate_id':selected_id});st.rerun()
                        except RuntimeError as exc:st.error(str(exc))
                if st.button('恢复此任务',key='source-retry-'+flow['id']):
                    try:client.call('flows/'+flow['id']+'/retry',{});st.rerun()
                    except RuntimeError as exc:st.error(str(exc))
            if flow['status'] not in ('completed','cancelled'):
                if st.button('停止后续自动处理',key='source-cancel-'+flow['id']):
                    try:client.call('flows/'+flow['id']+'/cancel',{});st.rerun()
                    except RuntimeError as exc:st.error(str(exc))


def save_template_panel(w,cfg,orientations,render,output_dir):
    with st.expander('保存为自动制作模板'):
        st.caption('仅保存可复用的剪辑规则、字幕样式、片头和导出设置，不保存当前素材或人工字幕文字。')
        name=st.text_input('制作模板名称',value='我的制作模板',key=w['id']+'auto-template-name')
        minimum=st.number_input('来源最短时长（分钟，0 表示不限）',min_value=0,max_value=1440,value=0,key=w['id']+'auto-minimum')
        if st.button('保存制作模板',key=w['id']+'auto-save-template'):
            try:
                variant=next(v for v in w['variants'] if v['id']==w['active_variant'])
                options=copy.deepcopy(variant.get('options') or {})
                options.update(anchors=[],events=[])
                settings={'form':variant['form'],'editing_options':options,
                    'audio_track':w.get('source_options',{}).get('audio_track',0),'allow_cpu':w.get('source_options',{}).get('allow_cpu',False)}
                data={'name':name,'settings':settings,'packaging':w.get('packaging') or {},
                    'source':{'minimum_minutes':minimum,'require_audio':True},
                    'export':{'orientations':orientations,'render':render,'output_dir':output_dir}}
                client=AutomationClient(cfg);client.call('templates',data);st.success('已保存，提交来源时可选择。')
            except (RuntimeError,ValueError,StopIteration) as exc:st.error(str(exc))
