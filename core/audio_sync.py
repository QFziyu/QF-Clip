"""Bounded-memory spectral matching over entire recordings, with local verification."""
from __future__ import annotations

import logging
from pathlib import Path
import wave
import numpy as np

LOG = logging.getLogger(__name__)


def audio_features(path, step=.5, start=0., duration=None):
    rows, energies = [], []
    with wave.open(str(path), 'rb') as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("对轴需要单声道 16 位 PCM WAV")
        rate = wav.getframerate()
        hop = max(1, round(step * rate))
        wav.setpos(min(wav.getnframes(), round(start * rate)))
        remaining = round(duration * rate) if duration is not None else wav.getnframes() - wav.tell()
        frequencies = np.fft.rfftfreq(hop, 1 / rate)
        edges = np.geomspace(80, min(rate / 2, 7000), 33)
        bins = [np.flatnonzero((frequencies >= a) & (frequencies < b)) for a, b in zip(edges[:-1], edges[1:])]
        window = np.hanning(hop)
        while remaining >= hop:
            data = wav.readframes(hop)
            if len(data) != 2 * hop:
                break
            samples = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768
            power = np.abs(np.fft.rfft(samples * window)) ** 2
            rows.append([np.log1p(float(power[indices].sum())) for indices in bins])
            energies.append(float(np.mean(samples ** 2)))
            remaining -= hop
    if not rows:
        return np.zeros((0, 32), dtype=np.float32)
    values = np.asarray(rows, dtype=np.float32)
    # Remove spectral colour and slowly changing speaker/microphone response.
    values -= values.mean(axis=1, keepdims=True)
    kernel = np.ones(min(len(values), max(1, round(5 / step))))
    kernel /= len(kernel)
    for band in range(values.shape[1]):
        values[:, band] -= np.convolve(np.pad(values[:, band], (len(kernel), len(kernel)), mode='edge'), kernel, mode='same')[len(kernel):-len(kernel)]
    values /= np.maximum(values.std(axis=0, keepdims=True), .15)
    values = np.clip(values, -4, 4)
    values[np.asarray(energies) < 1e-8] = 0
    return values


def correlation_candidates(a, b, step=.5, minimum_seconds=30., limit=4, lag_bounds=None):
    """Return offsets d where B(t+d) corresponds to A(t), normalized by overlap."""
    n, m = len(a), len(b)
    required = max(4, round(minimum_seconds / step))
    if min(n, m) < required or a.shape[1:] != b.shape[1:]:
        return []
    size = 1 << (n + m - 2).bit_length()
    cross = np.fft.irfft((np.fft.rfft(b, size, axis=0) * np.fft.rfft(a[::-1], size, axis=0)).sum(axis=1), size)[:n + m - 1]
    lags = np.arange(-(n - 1), m)
    starts, ends = np.maximum(0, -lags), np.minimum(n, m - lags)
    ea, eb = np.r_[0, np.cumsum((a * a).sum(axis=1), dtype=np.float64)], np.r_[0, np.cumsum((b * b).sum(axis=1), dtype=np.float64)]
    aa = ea[ends] - ea[starts]
    bb = eb[ends + lags] - eb[starts + lags]
    denom = np.sqrt(np.maximum(0, aa * bb))
    valid = (ends - starts >= required) & (aa > required) & (bb > required)
    if lag_bounds is not None:
        valid &= (lags * step >= lag_bounds[0]) & (lags * step <= lag_bounds[1])
    scores = np.full(len(lags), -np.inf)
    scores[valid] = cross[valid] / np.maximum(denom[valid], 1e-9)
    result = []
    for _ in range(limit):
        i = int(np.argmax(scores))
        if not np.isfinite(scores[i]):
            break
        result.append({'offset_seconds': float(lags[i] * step), 'score': float(scores[i]),
                       'overlap_seconds': float((ends[i] - starts[i]) * step)})
        scores[np.abs(lags - lags[i]) <= max(2, round(5 / step))] = -np.inf
    return result


def match_audio(match_wav, commentator_wav, cache_dir):
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    features = []
    for name, path in [('match', match_wav), ('commentator', commentator_wav)]:
        cached = directory / f'{name}_spectrum_v1.npy'
        try:
            values = np.load(cached, allow_pickle=False)
            if values.ndim != 2 or values.shape[1] != 32 or not np.isfinite(values).all():
                raise ValueError('bad feature cache')
        except (OSError, ValueError):
            LOG.info("分析 %s 全长声音指纹（每 0.5 秒一帧）", name)
            values = audio_features(path)
            with cached.with_suffix('.tmp').open('wb') as output:
                np.save(output, values, allow_pickle=False)
            cached.with_suffix('.tmp').replace(cached)
        features.append(values)
    minimum = min(30., max(5., min(map(len, features)) * .5 / 4))
    candidates = correlation_candidates(*features, minimum_seconds=minimum)
    result = {'method': 'audio_fingerprint', 'offset_seconds': None, 'confidence': 'low', 'candidates': candidates,
              'anchors': [], 'note': '没有足够的共同声音证据，请使用语音辅助或手动校准。'}
    if not candidates or candidates[0]['score'] < .18:
        return result
    best = candidates[0]
    offset = best['offset_seconds']
    begin = max(0., -offset)
    length = best['overlap_seconds']
    local_duration = min(30., length / 3)
    offsets = []
    for fraction in (.15, .5, .85):
        a_start = begin + max(0., length - local_duration) * fraction
        b_start = max(0., a_start + offset - 1.5)
        a = audio_features(match_wav, .05, a_start, local_duration)
        b = audio_features(commentator_wav, .05, b_start, local_duration + 3)
        local = correlation_candidates(a, b, .05, max(3, local_duration - 3), limit=1, lag_bounds=(0, 3))
        if local and local[0]['score'] >= .2:
            value = b_start - a_start + local[0]['offset_seconds']
            offsets.append(value)
            result['anchors'].append({'match_time': a_start, 'commentator_time': a_start + value,
                                      'offset_seconds': value, 'score': local[0]['score']})
    gap = best['score'] - (candidates[1]['score'] if len(candidates) > 1 else 0)
    residual = max(offsets) - min(offsets) if offsets else None
    confidence = ('high' if best['score'] >= .45 and gap >= .07 and len(offsets) >= 2 and residual <= .3 else
                  'medium' if best['score'] >= .3 and gap >= .04 and offsets and residual <= 1 else 'low')
    result.update(offset_seconds=round(float(np.median(offsets)) if offsets else offset, 3), confidence=confidence,
                  peak_margin=gap, local_offset_spread=residual,
                  note='声音匹配为同步候选，请预览开头与后段；若中途剪断、插播或逐渐漂移，应分区间单独校准。')
    return result
