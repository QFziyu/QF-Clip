"""Publishing controls share the workbench's final, approved export action."""
from __future__ import annotations

import re
from pathlib import Path

import streamlit as st

from ui.file_picker import request_file
from core.publisher_client import PublisherClient
from core.utils import fingerprint
from core.titles import publication_title

PLATFORMS = {'douyin':'抖音','bilibili':'B站'}


ACCOUNT_STATUS = {
    'login_needed': '等待登录', 'unverified': '待确认账号', 'ready': '已绑定',
    'account_mismatch': '登录账号与绑定账号不一致', 'assistance_needed': '需要检查浏览器',
}
JOB_STATUS = {
    'queued': '等待上传', 'validating': '正在检查成品', 'uploading': '正在上传',
    'prepared': '已上传 · 等待你最终发布', 'submitting': '正在提交发布',
    'submission_unknown': '发布结果待核实', 'submitted': '已提交 · 平台审核中',
    'published': '已发布', 'rejected': '未完成', 'login_needed': '需要登录',
    'account_mismatch': '账号不一致', 'assistance_needed': '需要你处理',
    'retry_wait': '等待重试', 'cancelled': '已停止',
}
ORIENTATION = {'horizontal': '横屏 16:9', 'vertical': '竖屏 9:16'}


def _rows(value, key):
    if isinstance(value, dict):
        value = value.get(key, [])
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def parse_hashtags(text):
    """Preserve the user's order without generating or silently adding topics."""
    return list(dict.fromkeys(part for part in re.split(r'[#＃\s,，]+', text.strip()) if part))


def frozen_caption(item, hashtags):
    description = item.get('description') or ''
    topics = ' '.join('#' + tag for tag in hashtags)
    return '\n'.join(piece for piece in (description, topics) if piece)


def account_is_ready(account):
    return account.get('status') == 'ready' and bool(account.get('creator_id'))


def _account_label(account):
    return account.get('label') or account.get('creator_id') or PLATFORMS.get(account.get('platform','douyin'),'发布')+'账号'


def _job_title(job):
    delivery = job.get('delivery') or {}
    return ((delivery.get('publication') or {}).get('title')
            or (job.get('publication') or {}).get('title') or '本批成品')


def _service_unavailable(cfg, message, key):
    st.info(message)
    st.caption('启动本机发布服务后刷新此页，再连接并确认所选平台账号。')
    if st.button('启动发布服务', key=key + '-start'):
        try:
            from core.publisher_client import start_publishing_services
            start_publishing_services(cfg)
            st.session_state['_wb_notice'] = '已启动发布服务，请稍后刷新；连接账号时再打开 Edge。'
            st.rerun()
        except RuntimeError as exc:
            st.error(str(exc))
    if st.button('刷新发布服务状态', key=key + '-refresh'):
        st.rerun()


def _settings_link(key):
    if st.button('前往设置连接账号', key=key):
        st.session_state['_wb_go_page'] = '设置'
        st.rerun()


def _choose_cover(key):
    request_file('选择本批发布封面','image',current_path=st.session_state.get(key,''),
                 action={'kind':'state','key':key})


def publication_controls(w, cfg, orientations):
    """Return publication options and readiness; default exports stay local."""
    prefix = w['id'] + '-publication-'
    destination = st.radio('输出去向', ['local', 'douyin', 'bilibili'], horizontal=True, key=prefix + 'destination',
        format_func=lambda value: '仅导出本地' if value == 'local' else '导出并发布到'+PLATFORMS[value])
    if destination == 'local':
        return None, True, None
    label=PLATFORMS[destination]
    try:
        client = PublisherClient(cfg)
        health=client.health()
        if destination not in health.get('caps',{}).get('platforms',['douyin']):
            raise RuntimeError('当前发布服务尚不支持'+label+'，请重启发布服务加载新版。')
        accounts = [a for a in _rows(client.accounts(), 'accounts') if a.get('platform','douyin')==destination]
    except RuntimeError as exc:
        _service_unavailable(cfg, str(exc), prefix)
        return None, False, None
    bound = [account for account in accounts if account_is_ready(account)]
    if not bound:
        st.info('尚未绑定可用的'+label+'账号。请在设置中登录、核对账号身份并确认绑定。')
        for account in accounts:
            st.caption(_account_label(account) + ' · ' + ACCOUNT_STATUS.get(account.get('status'), '待检查'))
        _settings_link(prefix + 'settings')
        return None, False, client
    by_id = {account['id']: account for account in bound}
    account_id = st.selectbox('发布账号', list(by_id), key=prefix + 'account',
        format_func=lambda identity: _account_label(by_id[identity]))
    account = by_id[account_id]
    st.caption(('已绑定B站 UID：' if destination=='bilibili' else '已绑定抖音号：') + str(account['creator_id']))
    modes = ['prepare', 'auto'] if account.get('auto_submit_verified') is True else ['prepare']
    mode_key = prefix + 'mode-' + account_id
    if st.session_state.get(mode_key) not in modes:
        st.session_state[mode_key] = 'prepare'
    mode = st.radio('提交方式', modes, horizontal=True, key=mode_key,
        format_func=lambda value: '准备发布（由我最终确认）' if value == 'prepare' else '自动提交发布')
    if not account.get('auto_submit_verified'):
        st.caption('此账号尚未完成样片校准；本批将停在待发布页面。可在设置中检查样片后启用自动提交。')
    else:
        st.caption('自动提交后仍需等待平台审核；只有确认发布成功才显示“已发布”。')
    available = [orientation for orientation in orientations if orientation in ORIENTATION]
    if not available:
        st.warning('请先选择至少一种本地输出画幅，再指定其中一种用于'+label+'发布。')
        return None, False, client
    orientation = st.selectbox('发布画幅（每条只发布一种）', available, key=prefix + 'orientation',
        format_func=ORIENTATION.get)
    hashtags = parse_hashtags(st.text_input('话题（以空格分隔）', key=prefix + 'hashtags', placeholder='#足球 #比赛集锦'))
    cover_key = prefix + 'cover'
    with st.expander('封面'):
        st.caption('默认使用最终成品视频的封面；也可为本批选择同一张封面图片。')
        st.caption('自定义封面控件需在当前平台账号校准；选择后任务会暂停等待检查。')
        st.button('选择封面图片…', key=prefix + 'pick-cover', on_click=_choose_cover, args=(cover_key,))
        cover = st.session_state.get(cover_key)
        if cover:
            st.caption('自定义封面：' + Path(cover).name)
            if st.button('恢复默认封面', key=prefix + 'clear-cover'):
                st.session_state.pop(cover_key, None)
                st.rerun()
    publication = dict(platform=destination, account_id=account_id, mode=mode,
                       orientation=orientation, hashtags=hashtags)
    if destination=='bilibili':
        st.caption('B站使用第三步保存的B站投稿标题；标签填写1～10个，需与视频相关。')
        copyright=st.radio('B站投稿类型',[2,1],format_func=lambda x:'转载' if x==2 else '自制',horizontal=True,key=prefix+'copyright')
        source=st.text_input('转载来源（转载必填）',key=prefix+'source',disabled=copyright==1)
        category=st.text_input('B站分区路径',value='运动 / 足球',key=prefix+'category',help='按当前B站投稿页的分区名称填写，以 / 分隔。')
        publication.update(copyright=copyright,source=source if copyright==2 else '',category_path=[s.strip() for s in category.split('/') if s.strip()])
        try:
            from core.bilibili_options import validate_bilibili_options
            validate_bilibili_options(publication)
        except ValueError as exc:
            st.info(str(exc));return None,False,client
    if st.session_state.get(cover_key):
        publication['cover_path'] = st.session_state[cover_key]
    return publication, True, client


def publication_confirmation(w, snapshot, publication):
    """Authorization is scoped to the exact frozen batch and expires after use."""
    st.subheader('本批发布内容')
    st.caption('使用当前方案已通过审核的成品。修改标题或文案请返回第三步；修改后需要重新授权。')
    label=PLATFORMS[publication['platform']]
    st.dataframe([{'标题': publication_title(item,publication['platform']), '发布文案': frozen_caption(item, publication['hashtags']),
                   '画幅': ORIENTATION[publication['orientation']]} for item in snapshot['items']],
        hide_index=True, use_container_width=True)
    generation = st.session_state.get(w['id'] + '-publication-generation', 0)
    token = fingerprint({'snapshot': snapshot, 'publication': publication, 'generation': generation})
    verb = '上传并提交发布' if publication['mode'] == 'auto' else '上传并填写，等待我最终确认发布'
    return st.checkbox(f'我已核对以上 {len(snapshot["items"])} 条成品，授权本批向所选{label}账号{verb}。',
                       value=False, key=w['id'] + '-publication-authorize-' + token)


def consume_authorization(w):
    key = w['id'] + '-publication-generation'
    st.session_state[key] = st.session_state.get(key, 0) + 1


def publication_jobs_panel(w, cfg, client=None):
    """Show only this project's publication jobs, never enqueue historical media."""
    @st.fragment(run_every=5)
    def content():
        try:
            service = client or PublisherClient(cfg)
            jobs = _rows(service.jobs(workspace_id=w['id']), 'jobs')
        except RuntimeError:
            return
        if not jobs:
            return
        st.subheader('本项目发布任务')
        rows = []
        for job in jobs:
            publication = job.get('publication') or {}
            delivery = job.get('delivery') or {}
            state = job.get('state', job.get('status', 'unknown'))
            rows.append({'标题': publication.get('title') or delivery.get('title') or '本批成品',
                         '状态': JOB_STATUS.get(state, '等待核实'),
                         '画幅': ORIENTATION.get(publication.get('orientation', (delivery.get('media') or {}).get('orientation')), '—')})
            rows[-1]['标题'] = _job_title(job)
            receipt = (job.get('result') or {}).get('receipt') or {}
            rows[-1]['平台'] = PLATFORMS.get(publication.get('platform'),'抖音')
            rows[-1]['作品 ID / BVID'] = receipt.get('remote_id', '—')
        st.dataframe(rows, hide_index=True, use_container_width=True)
        for job in jobs:
            state = job.get('state', job.get('status', 'unknown'))
            retryable = state in ('rejected', 'login_needed', 'account_mismatch', 'assistance_needed', 'retry_wait') and not job.get('submission_started_at')
            cancellable = state not in ('submitting', 'submission_unknown', 'submitted', 'published', 'cancelled')
            verifiable = state in ('prepared', 'submission_unknown', 'submitted')
            receipt = (job.get('result') or {}).get('receipt') or {}
            remote_id = str(receipt.get('remote_id') or '')
            bilibili=(job.get('publication') or {}).get('platform')=='bilibili'
            if bilibili and re.fullmatch(r'BV[A-Za-z0-9]{10}',remote_id):
                st.link_button('查看B站作品 · '+_job_title(job),'https://www.bilibili.com/video/'+remote_id)
            elif not bilibili and re.fullmatch(r'[0-9]{5,}', remote_id):
                st.link_button('查看抖音作品 · ' + _job_title(job), 'https://www.douyin.com/video/' + remote_id)
            if not (retryable or cancellable or verifiable):
                continue
            identity = str(job['id'])
            publication = job.get('publication') or {}
            with st.expander(_job_title(job) + ' · ' + JOB_STATUS.get(state, '等待核实'), expanded=False):
                error = job.get('last_error') or {}
                if isinstance(error, dict) and error.get('message'):
                    st.caption(str(error['message']))
                if state == 'submission_unknown':
                    st.info('请先在对应平台创作中心核对本条结果，避免重复发布。此状态不会自动重新上传。')
                if state == 'prepared':
                    st.info('请先检查并手动发布此条后核实作品 ID，或停止此任务；队列会保留后续作品，避免覆盖当前页面。')
                if retryable:
                    if st.button('重试发布任务', key='publishing-retry-' + identity):
                        try:
                            service.retry_job(identity)
                            st.rerun()
                        except RuntimeError as exc:
                            st.error(str(exc))
                if cancellable:
                    if st.button('停止此发布任务', key='publishing-cancel-' + identity):
                        try:
                            service.cancel_job(identity)
                            st.rerun()
                        except RuntimeError as exc:
                            st.error(str(exc))
                if verifiable:
                    st.caption('请从对应平台作品页面复制作品 ID 或 BVID。系统仅核对指定作品。')
                    remote_id = st.text_input('B站 BVID' if bilibili else '抖音作品 ID', key='publishing-remote-id-' + identity)
                    if st.button('核实发布结果', disabled=not bool(re.fullmatch(r'BV[A-Za-z0-9]{10}' if bilibili else r'[0-9]{5,}', remote_id)),
                                 key='publishing-verify-' + identity):
                        try:
                            service.verify_job(identity, remote_id)
                            st.session_state['_wb_notice'] = '已请求核实指定作品。页面确认匹配后才会显示“已发布”。'
                            st.rerun()
                        except RuntimeError as exc:
                            st.error(str(exc))
        if st.button('刷新发布任务', key=w['id'] + '-publishing-refresh-jobs'):
            st.rerun(scope='app')
    content()


def account_connection_panel(cfg):
    platform=st.selectbox('发布账号平台',list(PLATFORMS),format_func=PLATFORMS.get,key='publishing-account-platform')
    label=PLATFORMS[platform]
    st.subheader(label+'发布账号')
    try:
        client = PublisherClient(cfg)
        client.health()
        accounts = [a for a in _rows(client.accounts(), 'accounts') if a.get('platform','douyin')==platform]
    except RuntimeError as exc:
        _service_unavailable(cfg, str(exc), 'settings-publishing')
        return
    st.caption('发布服务已连接。仅在点击连接账号时打开 Edge；登录状态保存在本机。')
    if st.button('连接'+label+'账号（打开 Edge）', key='publishing-connect-account'):
        try:
            client.connect_account() if platform=='douyin' else client.connect_account(platform)
            st.session_state['_wb_notice'] = '已打开账号连接窗口。请在 Edge 中完成登录，再刷新账号身份。'
            st.rerun()
        except RuntimeError as exc:
            st.error(str(exc))
    if not accounts:
        st.info('尚无'+label+'账号。连接后需要核对身份并确认绑定。')
        return
    for account in accounts:
        identity = account['id']
        st.write(_account_label(account) + ' · ' + ACCOUNT_STATUS.get(account.get('status'), '待检查'))
        if account.get('creator_id'):
            st.caption(('绑定B站 UID：' if platform=='bilibili' else '绑定抖音号：') + str(account['creator_id']))
        if st.button('刷新账号身份', key='publishing-refresh-account-' + identity):
            try:
                client.refresh_account(identity)
                st.rerun()
            except RuntimeError as exc:
                st.error(str(exc))
        observed = account.get('observed_identity') or {}
        if observed.get('creator_id') and not account.get('creator_id'):
            st.info('浏览器当前账号：' + str(observed.get('label') or label+'账号') + ' · '+('UID ' if platform=='bilibili' else '抖音号 ') + str(observed['creator_id']))
            if st.button('确认此账号', key='publishing-bind-account-' + identity):
                try:
                    client.refresh_account(identity, expected_creator_id=observed['creator_id'])
                    st.session_state['_wb_notice'] = '已确认并绑定账号。首次发布请先准备样片并检查。'
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))
        if not account_is_ready(account):
            continue
        if account.get('auto_submit_verified'):
            st.caption('已完成自动提交校准；每批发布仍需在第五步明确授权。')
            continue
        with st.expander('检查样片并启用自动提交', expanded=False):
            st.caption('先在第五步以“准备发布”导出一条样片，在 Edge 核对账号、视频、封面和发布文案。')
            try:
                samples = [job for job in _rows(client.jobs(), 'jobs') if job.get('state') == 'prepared'
                           and (job.get('publication') or {}).get('account_id') == identity]
            except RuntimeError as exc:
                st.info(str(exc))
                samples = []
            if not samples:
                st.info('此账号还没有可检查的待发布样片。')
                continue
            by_id = {job['id']: job for job in samples}
            sample = st.selectbox('已准备的样片', list(by_id), key='publishing-sample-' + identity,
                format_func=lambda value: _job_title(by_id[value]))
            checked = st.checkbox('我已在 Edge 核对这条样片、账号、封面和文案，同意启用此账号的自动提交。',
                value=False, key='publishing-calibration-check-' + identity + '-' + sample)
            if st.button('确认样片并启用自动提交', disabled=not checked, key='publishing-calibrate-' + identity):
                try:
                    client.calibrate_account(identity, sample)
                    st.session_state['_wb_notice'] = '此账号已启用自动提交。每批仍需在第五步核对并授权。'
                    st.rerun()
                except RuntimeError as exc:
                    st.error(str(exc))
