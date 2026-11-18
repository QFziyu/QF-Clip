"""ASR engine selection; its costs and capabilities are distinct from LLM selection."""
import streamlit as st
from core.asr_settings import LABELS, save_settings


def panel(cfg):
    st.subheader('本地语音识别')
    st.caption('这一步把声音转为带时间戳文字，两种引擎都在本机运行，不使用云端语音 API。')
    notice = st.session_state.pop('_asr_notice', None)
    if notice:
        st.success(notice)
    current = cfg['asr'].get('engine','faster_whisper')
    with st.form('asr-settings'):
        engine = st.selectbox('识别引擎', list(LABELS), index=list(LABELS).index(current),
                              format_func=LABELS.get)
        url = st.text_input('Zipformer 本机服务地址', value=cfg['asr']['local_url'])
        review = st.checkbox('字幕生成后再调用大语言模型校对', value=cfg['subtitle_review']['enabled'],
                             help='校对会使用下方配置的 LLM/API 或账户额度；关闭仍可人工改字幕。选片、标题是独立步骤。')
        st.caption('Zipformer 使用 CPU，不占 GPU 显存；主要支持中文，未自带标点恢复。字幕采用 CTC 时间戳，字尾为估计。')
        saved = st.form_submit_button('保存语音识别设置')
    if saved:
        try:
            save_settings(cfg, engine, url, review)
            st.session_state['_asr_notice'] = '已保存，后续任务使用新引擎。已有成品和人工字幕保留；旧项目需要重新生成方案才会重新转录。'
            st.rerun()
        except (ValueError, OSError) as exc:
            st.error(str(exc))
    if st.button('检查本地中文 ASR 连接'):
        from core.local_asr import ZipformerHTTP
        try:
            client = ZipformerHTTP(dict(cfg['asr'],local_url=url))
            st.success('已连接：Zipformer Small 中文 · CPU · 时间戳接口就绪')
        except (ValueError, RuntimeError) as exc:
            st.error(str(exc))
    st.caption('服务未运行时，双击 D:/Codex/Hugging Face/Start-ASR-Background.cmd；该服务与切片工厂使用独立 Python 环境。')
