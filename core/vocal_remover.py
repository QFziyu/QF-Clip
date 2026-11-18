"""Demucs background stem, short source chunks, resumable PCM cache and exact clocks."""
from __future__ import annotations

import importlib.util
import logging
import math
from pathlib import Path
import tempfile
import wave

import numpy as np

from core.audio_extract import audio_filter
from core.utils import atomic_json, fingerprint, read_json
from core.video_input import run_media

LOG = logging.getLogger(__name__)
MODEL = 'htdemucs'
RATE = 44100
CHUNK_SECONDS = 30
CONTEXT = 2


def available():
    return all(importlib.util.find_spec(name) is not None for name in ('torch', 'torchaudio', 'demucs'))


class BackgroundSeparator:
    """Load one pretrained model per render; CPU/GPU failure never restores unwanted vocals."""

    def __init__(self, device='auto', model_dir=None):
        if not available():
            raise RuntimeError('AI 去人声依赖未安装，请运行 python -m pip install -r requirements-vocals.txt；也可先选择比赛录像静音')
        import torch
        from demucs.pretrained import get_model
        from demucs.apply import apply_model
        self.torch, self.apply = torch, apply_model
        if model_dir is not None:
            torch.hub.set_dir(str(Path(model_dir)))
        self.device = ('cuda' if torch.cuda.is_available() else 'cpu') if device == 'auto' else device
        if self.device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('去人声所需 PyTorch CUDA 不可用，请选择自动或 CPU；与 Whisper CUDA 配置独立')
        LOG.info('加载 Demucs %s，去人声设备 %s（首次运行可能下载预训练模型）', MODEL, self.device)
        # Small chunks avoid reading a multi-hour recording into model memory.
        self.model = get_model(name=MODEL).eval()
        self.vocal_index = self.model.sources.index('vocals')
        try:
            self.model.to(self.device)
        except RuntimeError:
            if device != 'auto' or self.device != 'cuda':
                raise
            LOG.warning('Demucs GPU 初始化失败，回退 CPU')
            self.device = 'cpu'
            self.model.to('cpu')
        self.auto_fallback = device == 'auto'

    def separate(self, audio):
        torch = self.torch
        # audio is planar [2, frames], float32; avoid auto resampling/time stretching.
        tensor = torch.from_numpy(np.ascontiguousarray(audio))
        reference = tensor.mean(0)
        mean, std = reference.mean(), reference.std()
        if float(std) < 1e-7:
            return np.zeros_like(audio)
        normalized = (tensor - mean) / std
        def infer():
            with torch.inference_mode():
                stems = self.apply(self.model, normalized[None], device=self.device, shifts=0,
                                   split=True, overlap=.25, segment=7.8, progress=False, num_workers=0)[0]
                # Match Demucs two-stems behavior: sum all non-vocal stems.
                stems = stems.cpu() * std + mean
                return sum(stems[i] for i in range(len(self.model.sources)) if i != self.vocal_index).numpy()
        try:
            result = infer()
        except RuntimeError:
            if not self.auto_fallback or self.device != 'cuda':
                raise
            LOG.warning('Demucs GPU 推理失败，回退 CPU 重试当前块')
            self.device = 'cpu'
            self.model.to('cpu')
            torch.cuda.empty_cache()
            result = infer()
        if result.shape != audio.shape or not np.isfinite(result).all():
            raise RuntimeError('去人声模型返回的音频形状或数值无效')
        return result


def _valid_wav(path, frames):
    try:
        with wave.open(str(path), 'rb') as wav:
            return wav.getnchannels() == 2 and wav.getsampwidth() == 2 and wav.getframerate() == RATE and wav.getnframes() == frames
    except (OSError, EOFError, wave.Error):
        return False


def _save(path, audio):
    temporary = path.with_suffix('.partial.wav')
    try:
        with wave.open(str(temporary), 'wb') as wav:
            wav.setparams((2, 2, RATE, 0, 'NONE', 'not compressed'))
            wav.writeframes((np.clip(audio.T, -1, 1) * 32767).astype('<i2').tobytes())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_background(meta, start, end, cfg, cache_dir, device='auto'):
    """Return an exact-length WAV relative to [start,end] of the match video.

    Fixed source chunks with context can be reused across previews and overlapping
    exports. Caller holds the composition lock; every completed chunk is atomic.
    """
    if meta['selected_audio'] is None:
        raise ValueError('比赛录像没有可分离的音轨')
    if not 0 <= start < end <= meta['duration'] + 1e-6:
        raise ValueError('去人声区间超出比赛录像')
    identity = fingerprint({'source':meta['source_signature'], 'track':meta['selected_audio'],
                            'model':MODEL,'rate':RATE,'device':device,'version':1})
    root = Path(cache_dir)/'background'/identity
    root.mkdir(parents=True, exist_ok=True)
    first, last = round(start*RATE), round(end*RATE)
    destination = root/f'{first}_{last}.wav'
    if _valid_wav(destination, last-first) and destination.with_suffix('.json').is_file():
        return destination, read_json(destination.with_suffix('.json'))
    separator, used_devices = None, set()
    chunk_frames = CHUNK_SECONDS * RATE
    total_frames = round(meta['duration']*RATE)
    blocks = []
    for index in range(first//chunk_frames, (last-1)//chunk_frames+1):
        begin, finish = index*chunk_frames, min(total_frames,(index+1)*chunk_frames)
        block = root/f'chunk_{index:05d}.wav'
        manifest = block.with_suffix('.json')
        if not _valid_wav(block, finish-begin) or not manifest.is_file():
            context_start = max(0, begin-CONTEXT*RATE)/RATE
            context_end = min(total_frames, finish+CONTEXT*RATE)/RATE
            if separator is None:
                separator = BackgroundSeparator(device, Path(cfg['cache_dir'])/'vocal_models')
            LOG.info('AI 去比赛人声：%s，源 %.1f–%.1f 秒（30 秒分块缓存）', separator.device, begin/RATE, finish/RATE)
            with tempfile.TemporaryDirectory(prefix='.vocals-',dir=root) as temporary:
                extracted = Path(temporary)/'input.wav'
                absolute = meta['timeline_origin']+context_start
                command = [cfg['ffmpeg'],'-v','error','-nostdin','-y','-copyts']
                seek = absolute-meta['format_start']-2
                if seek>0:
                    command += ['-ss',str(seek)]
                command += ['-i',meta['source'],'-map',f"0:{meta['selected_audio']}",'-vn','-af',
                    audio_filter(absolute,context_end-context_start,RATE),'-ar',str(RATE),'-ac','2','-c:a','pcm_s16le',str(extracted)]
                run_media(command)
                with wave.open(str(extracted),'rb') as wav:
                    audio = np.frombuffer(wav.readframes(wav.getnframes()),dtype='<i2').reshape(-1,2).T.astype(np.float32)/32768
                background = separator.separate(audio)
                left = begin-round(context_start*RATE)
                core = background[:,left:left+finish-begin]
                if core.shape[1] != finish-begin:
                    raise RuntimeError('去人声输出长度不足，已完成块保留；请重试')
                _save(block,core)
                atomic_json(manifest, {'model':MODEL,'device':separator.device,'source_start':begin/RATE,'source_end':finish/RATE})
        used_devices.add(read_json(manifest)['device'])
        blocks.append((block,max(first,begin)-begin,min(last,finish)-begin))
    temporary = destination.with_suffix('.partial.wav')
    try:
        with wave.open(str(temporary),'wb') as output:
            output.setparams((2,2,RATE,0,'NONE','not compressed'))
            for block, begin, finish in blocks:
                with wave.open(str(block),'rb') as source:
                    source.setpos(begin)
                    remaining=finish-begin
                    while remaining:
                        count=min(remaining,RATE*5)
                        output.writeframesraw(source.readframes(count))
                        remaining-=count
        if not _valid_wav(temporary,last-first):
            raise RuntimeError('去人声缓存时间轴校验失败')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    info={'model':MODEL,'devices':sorted(used_devices),'source_start':start,'source_end':end,
          'note':'AI 分离可能残留解说，也可能削弱欢呼、口哨或音乐，请先听预览。'}
    atomic_json(destination.with_suffix('.json'),info)
    return destination,info
