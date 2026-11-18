"""Shared caption find/replace; all changes stay in the draft until saved."""
import streamlit as st

from core.subtitle_edit import replace_srt, clean_srt


def _toggle(key):
    st.session_state[key] = not st.session_state.get(key,False)


def _apply(editor, duration, prefix, operation):
    before = st.session_state.get(editor,'')
    try:
        if operation == 'replace':
            after,count,_ = replace_srt(before,duration,st.session_state.get(prefix+'find',''),
                                       st.session_state.get(prefix+'replacement',''))
            message = f'已替换 {count} 处'
        else:
            after,count = clean_srt(before,duration,chinese_only=st.session_state.get(prefix+'han',False))
            message = f'已清理 {count} 个字符'
        if after != before:
            st.session_state[prefix+'undo'] = {'before':before,'after':after}
            st.session_state[editor] = after
        else:
            message = '没有需要修改的内容'
        st.session_state[prefix+'message'] = message+'；请保存人工字幕后重新生成预览。'
        st.session_state.pop(prefix+'error',None)
    except ValueError as exc:
        st.session_state[prefix+'error'] = str(exc)


def _undo(editor,prefix):
    entry = st.session_state.get(prefix+'undo')
    if entry and st.session_state.get(editor,'') == entry['after']:
        st.session_state[editor] = entry['before']
        st.session_state.pop(prefix+'undo',None)
        st.session_state[prefix+'message'] = '已撤销上次批量操作，请保存人工字幕。'
        st.session_state.pop(prefix+'error',None)


def subtitle_tools(editor_key,duration,*,shortcut=True):
    prefix=editor_key+'-tools-'
    st.button('查找与替换',key=prefix+'open',shortcut='Ctrl+Shift+F' if shortcut else None,
              on_click=_toggle,args=(prefix+'visible',),help='统一修改当前视频的字幕正文，时间轴保持不变。')
    if not st.session_state.get(prefix+'visible'): return
    with st.container(border=True):
        a,b=st.columns(2)
        find=a.text_input('查找文字',key=prefix+'find',placeholder='例如：洛桑')
        replacement=b.text_input('替换为',key=prefix+'replacement',placeholder='例如：胡桑诺夫')
        valid=True
        if find.strip():
            try:
                _,count,cues=replace_srt(st.session_state.get(editor_key,''),duration,find,replacement)
                st.caption(f'当前视频：{cues} 条字幕、{count} 处匹配。仅按原文精确匹配；替换为空会删除匹配文字。')
            except ValueError as exc:
                st.warning(str(exc));valid=False
        st.button('全部替换',key=prefix+'replace',disabled=not find.strip() or not valid,
                  on_click=_apply,args=(editor_key,duration,prefix,'replace'))
        st.checkbox('仅保留汉字与标点（同时移除英文和数字）',key=prefix+'han')
        a,b=st.columns(2)
        a.button('清理识别标记',key=prefix+'clean',on_click=_apply,args=(editor_key,duration,prefix,'clean'),
                 help='删除 <unk> 等模型标记；空字幕自动移除，其他字幕时间不变。')
        undo=st.session_state.get(prefix+'undo')
        b.button('撤销上次批量操作',key=prefix+'undo-button',on_click=_undo,args=(editor_key,prefix),
                 disabled=not undo or st.session_state.get(editor_key,'')!=undo['after'])
        if st.session_state.get(prefix+'error'): st.error(st.session_state[prefix+'error'])
        elif st.session_state.get(prefix+'message'): st.success(st.session_state[prefix+'message'])
