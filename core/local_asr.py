"""Loopback Zipformer adapter. No model packages, API keys or proxy on this side."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import wave
import subprocess

MODEL_REVISION = "a5f60fe00dcfbaf68fcc1c6b5cf53061e144d6da"
MODEL_SHA256 = "68c9c943840f7d9cf3e8a4970ba50f404feb5277f611fa82b7e72267786fa84a"


def validate_url(value):
    p = urlsplit(value)
    if (p.scheme != 'http' or p.hostname not in ('127.0.0.1', 'localhost', '::1') or
            p.username or p.password or p.path not in ('', '/') or p.query or p.fragment):
        raise ValueError('本地 ASR 地址必须是仅本机 HTTP 根地址，例如 http://127.0.0.1:8765')
    try:
        p.port
    except ValueError as exc:
        raise ValueError('本地 ASR 端口无效') from exc
    return value.rstrip('/')


class LocalASRError(RuntimeError):
    pass


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list


@dataclass
class Info:
    language: str = 'zh'


class ZipformerHTTP:
    def __init__(self, options):
        self.url = validate_url(options['local_url'])
        self.timeout = options.get('local_timeout', 120)
        # Loopback audio must never be sent to an HTTP proxy, redirected to a
        # remote host, or retried against a paid/cloud ASR fallback.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.health = self._request('/health')
        if (self.health.get('service') != 'zipformer-small-zh-cpu' or
                'ctc_timestamps' not in self.health.get('capabilities', []) or
                self.health.get('model_revision') != MODEL_REVISION or
                self.health.get('model_sha256') != MODEL_SHA256):
            raise LocalASRError('本地 ASR 服务版本/模型或时间戳能力不匹配；请重启更新后的本地 ASR 服务。')

    def _request(self, route, data=None):
        req = urllib.request.Request(self.url + route, data=data,
            headers={'Content-Type': 'audio/wav', 'X-ASR-Response-Format': 'verbose_json'})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise ValueError('response is not a JSON object')
            return value
        except urllib.error.HTTPError as exc:
            raise LocalASRError(f'本地 ASR 返回 HTTP {exc.code}；检查服务日志，修复后重试，已完成分块会复用。') from exc
        except (OSError, ValueError) as exc:
            raise LocalASRError('本地中文 ASR 未连接或响应无效。请运行 D:/Codex/Hugging Face/Start-ASR-Background.cmd，'
                                '并检查 /health；不会改用付费识别。') from exc

    def transcribe(self, path, **kwargs):
        path = Path(path)
        if path.stat().st_size > 32 * 1024 * 1024:
            raise LocalASRError('本地 ASR 单块超过 32 MiB，请减小 local_chunk_seconds')
        with wave.open(str(path), 'rb') as wav:
            duration = wav.getnframes() / wav.getframerate()
        result = self._request('/v1/audio/transcriptions', path.read_bytes())
        if result.get('model_revision') != MODEL_REVISION or result.get('model_sha256') != MODEL_SHA256:
            raise LocalASRError('本地 ASR 推理期间模型身份发生变化，已停止接收该块')
        if result.get('timestamp_method') != 'ctc-byte-bpe-onsets-estimated-ends-v1':
            raise LocalASRError('本地 ASR 未提供可验证时间轴；不使用纯文字估造字幕时间戳')
        from core.transcriber import validate_transcript
        result = validate_transcript(result, duration)
        if ''.join(s['text'] for s in result['segments']) != result.get('text'):
            raise LocalASRError('本地 ASR 字幕文本与原始识别不一致')
        if result.get('text') and any(not s.get('words') for s in result['segments']):
            raise LocalASRError('本地 ASR 返回了缺少字级时间轴的结果')
        self.last_raw = result
        return iter(Segment(s['start'], s['end'], s['text'], s['words'])
                    for s in result['segments']), Info()


def ensure_service(cfg):
    """Launcher convenience for the verified sibling deployment on default port.

    An occupied port or incompatible healthy service is never stopped/replaced.
    Direct CLI/worker errors remain resumable when an external service is down.
    """
    if cfg['asr'].get('engine') != 'zipformer_http':
        return
    try:
        return ZipformerHTTP(cfg['asr']).health
    except LocalASRError as exc:
        cause = exc.__cause__
        if not isinstance(cause, urllib.error.URLError) or not isinstance(cause.reason, ConnectionRefusedError):
            raise
        if validate_url(cfg['asr']['local_url']) != 'http://127.0.0.1:8765':
            raise
        root = Path(cfg['root']).parent / 'Hugging Face'
        python = root / '.venv/Scripts/python.exe'
        starter = root / 'tools/start_background.py'
        if not python.is_file() or not starter.is_file() or not (root/'asr_service.py').is_file():
            raise
        result = subprocess.run([str(python),str(starter)],cwd=root,capture_output=True,
            timeout=25,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise LocalASRError('本地 ASR 启动失败，请检查 D:/Codex/Hugging Face/service.log') from None
        return ZipformerHTTP(cfg['asr']).health
