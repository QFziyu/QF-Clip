"""Durable ASR choice shared by CLI and background workers, separate from LLM."""
import os
from pathlib import Path
import tempfile

KEYS = {'ASR_ENGINE', 'LOCAL_ASR_URL', 'SUBTITLE_REVIEW_ENABLED'}
LABELS = {'faster_whisper': 'Whisper · 本地 GPU / CPU', 'zipformer_http': 'Zipformer 中文 · 本地 CPU'}


def settings_path(root):
    return Path(os.environ.get('FCF_PRIMARY_ROOT') or root) / '.env.asr'


def save_settings(cfg, engine, url, subtitle_review):
    from core.local_asr import validate_url
    if engine not in LABELS or type(subtitle_review) is not bool:
        raise ValueError('语音识别设置无效')
    if any(c in url for c in ('\n','\r','\x00','"')):
        raise ValueError('本地地址必须为单行文字')
    url = validate_url(url.strip())
    path = settings_path(cfg['root'])
    body = (f'# 本地语音识别设置；不包含 API Key。\nASR_ENGINE={engine}\n'
            f'LOCAL_ASR_URL={url}\nSUBTITLE_REVIEW_ENABLED={str(subtitle_review).lower()}\n')
    fd, temporary = tempfile.mkstemp(prefix='.asr-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path
