"""One native chooser per browser session, polled without blocking Streamlit."""
from pathlib import Path

import streamlit as st

from core.local_file_picker import start_local_picker, poll_local_picker, cancel_local_picker


ACTIVE = '_native_picker_active'
RESULT = '_native_picker_result'


def request_file(title, kind='video', multiple=False, current_path='', action=None):
    if st.session_state.get(ACTIVE):
        st.session_state['_wb_notice'] = '已有文件选择窗口正在等待，请先完成或取消当前选择。'
        return
    try:
        task = start_local_picker(title, kind, multiple=multiple, current_path=current_path)
        st.session_state[ACTIVE] = {'task': task, 'action': action or {}, 'title': title}
    except (OSError, RuntimeError, ValueError) as exc:
        st.session_state['_wb_notice'] = '文件窗口启动失败：' + str(exc)


def apply_file_result(cfg):
    """Apply before widgets are constructed, so their keys can safely change."""
    completed = st.session_state.pop(RESULT, None)
    if not completed:
        return
    result, action = completed['result'], completed['action']
    if result['status'] == 'cancelled':
        st.session_state['_wb_notice'] = '已取消文件选择，原有素材和设置保持不变。'
        return
    if result['status'] == 'error':
        st.session_state['_wb_notice'] = result.get('error') or '文件窗口未能完成，可重试或展开高级路径输入。'
        return
    value = result.get('value')
    if not value:
        return
    try:
        if action.get('kind') == 'assets':
            if action.get('folder'):
                from core.video_input import discover_videos
                value = [str(p) for p in discover_videos(value)]
            from core.workflow import import_assets
            with st.spinner('正在读取所选素材信息…'):
                import_assets(action['workspace_id'], value, cfg)
        elif action.get('kind') == 'state':
            st.session_state[action['key']] = value
        elif action.get('kind') == 'srt':
            path = Path(value)
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('SRT 请控制在 2 MiB 内')
            raw = path.read_bytes()
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeError:
                text = raw.decode('gb18030')
            from core.subtitle import validate_srt
            validate_srt(text, action['duration'])
            st.session_state[action['key']] = text
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        st.session_state['_wb_notice'] = '所选文件未能导入：' + str(exc)


def file_picker_panel():
    if not st.session_state.get(ACTIVE):
        return

    @st.fragment(run_every=1)
    def monitor():
        active = st.session_state.get(ACTIVE)
        if not active:
            return
        result = poll_local_picker(active['task'])
        if result['status'] != 'pending':
            st.session_state[RESULT] = {'result': result, 'action': active['action']}
            st.session_state.pop(ACTIVE, None)
            st.rerun(scope='app')
        st.info('系统文件选择窗口正在打开或等待选择：' + active['title'])
        st.caption('页面可以继续操作。请在系统窗口中选择文件；若没有看到窗口，可取消后重新打开。')
        if st.button('取消文件选择', key='cancel-native-file-picker'):
            cancel_local_picker(active['task'])
            st.session_state.pop(ACTIVE, None)
            st.session_state['_wb_notice'] = '已取消文件选择，原有素材和设置保持不变。'
            st.rerun(scope='app')
    monitor()
