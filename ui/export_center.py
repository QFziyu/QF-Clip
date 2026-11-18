"""All approved projects share the fifth-step export API."""
import streamlit as st
from datetime import datetime,timezone,timedelta
from core.export_center import catalog,plan_exports,submit_exports


def panel(w,cfg):
    from ui.workbench import project_export_step,_pick_state
    mode=st.radio('导出范围',['全部项目已通过内容','当前项目导出与发布'],horizontal=True,key='export-center-mode')
    if mode=='当前项目导出与发布':
        project_export_step(w,cfg)
        return
    st.header('统一导出')
    st.caption('默认勾选未导出或已修改的通过项。各项目的人工字幕、标题和包装分别保留。')
    rows=catalog(cfg)
    project=st.selectbox('项目筛选',['全部项目']+sorted({r['project'] for r in rows}))
    state=st.selectbox('导出状态',['全部','待导出或已修改','已导出'])
    period=st.selectbox('最近修改',['全部日期','最近 7 天','最近 30 天'])
    cutoff=(datetime.now(timezone.utc)-timedelta(days=7 if period=='最近 7 天' else 30)).isoformat() if period!='全部日期' else ''
    visible=[r for r in rows if (project=='全部项目' or r['project']==project) and (state=='全部' or (r['export_status'] in ('exported','legacy'))==(state=='已导出'))]
    visible=[r for r in visible if not cutoff or r['updated_at']>=cutoff]
    st.caption('数据环境：'+cfg['database'])
    a,b=st.columns(2)
    if a.button('全选当前列表'):
        for r in visible:st.session_state['export-select-'+r['key']]=True
    if b.button('取消当前列表选择'):
        for r in visible:st.session_state['export-select-'+r['key']]=False
    selected=[];labels={'new':'未导出','changed':'修改后待导出','exported':'已导出','legacy':'旧版成品保留，内容需核对'}
    with st.container(height=330):
        if not visible:st.info('此范围暂无已通过内容。请先在第三步保存审核。')
        for r in visible:
            key='export-select-'+r['key'];st.session_state.setdefault(key,r['default_selected'])
            if st.checkbox(r['title'],key=key):selected.append(r['key'])
            subtitle={'manual':'人工','complete':'已复核','stale':'需核对','unverified':'未核验','empty':'无语音'}.get(r['subtitle_status'],r['subtitle_status'])
            st.caption(f"{r['project']} / {r['variant']} · {r['duration']/60:.1f} 分钟 · {labels[r['export_status']]} · 字幕 {subtitle} · 修改 {r['updated_at'][:10]}")
    if visible:
        with st.expander('返回某条内容审核'):
            lookup={r['key']:r for r in visible}
            review_key=st.selectbox('选择审核内容',list(lookup),format_func=lambda k:lookup[k]['project']+' / '+lookup[k]['title'])
            st.button('打开对应审核项',on_click=open_review,args=(lookup[review_key],cfg))
    orientations=st.multiselect('输出画幅',['horizontal','vertical'],default=['horizontal'],format_func=lambda x:'横屏 16:9' if x=='horizontal' else '竖屏 9:16',key='global-export-orientations')
    a,b=st.columns(2)
    size=a.selectbox('分辨率',['1080p','720p','原配置'],key='global-export-resolution')
    encoder=b.selectbox('编码器',['auto','h264_nvenc','libx264'],key='global-export-encoder')
    render={k:cfg['render'][k] for k in ('horizontal','vertical','fps','bitrate')};render['encoder']=encoder
    if size!='原配置':render.update(horizontal=[1920,1080] if size=='1080p' else [1280,720],vertical=[1080,1920] if size=='1080p' else [720,1280])
    st.session_state.setdefault('global-export-folder',cfg['output_dir'])
    st.button('选择输出文件夹…',on_click=_pick_state,args=('global-export-folder','folder','选择输出文件夹'))
    st.caption(st.session_state['global-export-folder'])
    plan=None
    if selected and orientations:
        try:
            plan=plan_exports(cfg,selected,orientations,render,st.session_state['global-export-folder'])
            if plan['issues']:
                st.warning(f"有 {len(plan['issues'])} 项需要处理。展开详情，或取消勾选这些项目后导出其余内容。")
                with st.expander('查看导出问题'):
                    for issue in plan['issues']:st.write((issue.get('title') or issue['key'])+'：'+issue['message'])
        except (ValueError,OSError,KeyError) as exc:st.error(str(exc))
    st.write(f"已选 {len(selected)} 条 · 正文总长 {sum(r['duration'] for r in visible if r['key'] in selected)/60:.1f} 分钟")
    st.caption('有问题的条目可取消勾选，先导出其他内容。此入口仅生成本地文件。')
    if st.button('开始导出',type='primary',disabled=not plan or bool(plan['issues'])):
        try:st.success('已提交后台批次：'+submit_exports(plan,cfg)['id'])
        except (ValueError,RuntimeError) as exc:st.error(str(exc))
    batch_progress(cfg)
    with st.expander('所有项目成品与历史'):
        history=[{'项目':r['project'],'标题':r['title'],'状态':labels[r['export_status']],'文件':o.get('video','')} for r in rows for o in r['outputs']]
        if history:st.dataframe(history,hide_index=True,width='stretch')
        if st.button('打开输出文件夹',key='global-export-open'):
            from core.local_file_picker import open_folder
            try:open_folder(st.session_state['global-export-folder'])
            except (ValueError,OSError) as exc:st.error(str(exc))


def open_review(row,cfg):
    from storage.workspaces import WorkspaceStore
    with WorkspaceStore(cfg['database']) as store:
        w=store.get_workspace(row['workspace_id']);w.update(active_variant=row['variant_id'],step=3)
        store.save_workspace(w);store.preference('current_workspace',w['id'])
    st.session_state['_wb_select_next']=w['id'];st.session_state['_wb_go_page']='工作台'
    st.session_state['wb-candidate-'+row['variant_id']]=row['item_id']


@st.fragment(run_every=5)
def batch_progress(cfg):
    from core.jobs import list_jobs,retry_job,cancel_job
    jobs=[j for j in list_jobs(cfg) if j['kind']=='export_batch'][:8]
    if not jobs:return
    st.subheader('导出批次')
    for job in jobs:
        st.write(job['id'][:12]+' · '+job['status'])
        if job.get('error'):st.warning(job['error'])
        result=job.get('result') or {}
        if result.get('exports'):st.caption(f"已生成 {len(result['exports'])} 个文件")
        if job['status'] in ('failed','interrupted','cancelled'):
            if st.button('重试未完成部分',key='batch-retry-'+job['id']):retry_job(job['id'],cfg);st.rerun()
        elif job['status'] in ('queued','running'):
            if st.button('取消批次',key='batch-cancel-'+job['id']):cancel_job(job['id'],cfg);st.rerun()
