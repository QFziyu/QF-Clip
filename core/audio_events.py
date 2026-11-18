"""Bounded-memory audio-energy hints. These are not match-event detectors."""
from __future__ import annotations

from collections import deque
import logging
import math
import wave

log = logging.getLogger(__name__)


def analyze_audio(wav_path, cfg: dict) -> list[dict]:
    """Stream PCM WAV in one-second chunks and detect local energy rises.

    Silence and ordinary steady music are not sufficient evidence of an event.
    Loudness cannot reliably classify goals, laughter, or emotional state; those
    labels are deliberately not emitted. Memory is independent of audio length.
    """
    import numpy as np

    options = cfg.get("audio_analysis", {})
    block_seconds = max(0.1, float(options.get("block_seconds", 1.0)))
    threshold = max(1.05, float(options.get("rise_ratio", 2.0)))
    minimum_rms = max(0, float(options.get("minimum_rms", 0.015)))
    max_events = max(1, int(options.get("max_events", 120)))
    history = deque(maxlen=max(5, int(60 / block_seconds)))
    events = []
    with wave.open(str(wav_path), "rb") as audio:
        width, rate, channels = audio.getsampwidth(), audio.getframerate(), audio.getnchannels()
        if audio.getcomptype() != "NONE" or width not in (1, 2, 3, 4):
            raise ValueError("声音分析需要未压缩 PCM WAV（8/16/24/32 位）")
        frames_per_block = max(1, int(rate * block_seconds))
        total_duration = audio.getnframes() / rate
        sample_position = 0
        while True:
            raw = audio.readframes(frames_per_block)
            if not raw:
                break
            if width == 1:
                values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
            elif width == 3:
                data = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
                integers = data[:, 0] | data[:, 1] << 8 | data[:, 2] << 16
                integers = np.where(integers & 0x800000, integers - 0x1000000, integers)
                values = integers.astype(np.float32) / 8388608
            else:
                values = np.frombuffer(raw, dtype="<i2" if width == 2 else "<i4").astype(np.float64)
                values /= 32768 if width == 2 else 2147483648
            # Energy across channels avoids phase cancellation from stereo downmix.
            rms = float(np.sqrt(np.mean(values * values)))
            frames = len(values) // channels
            start, end = sample_position / rate, (sample_position + frames) / rate
            baseline = float(np.median(history)) if history else rms
            ratio = rms / max(baseline, minimum_rms / threshold, 1e-8)
            if len(history) >= 3 and rms >= minimum_rms and ratio >= threshold:
                item = {"start": start, "end": end, "kind": "energy_rise",
                        "strength": round(min(ratio, 100), 3), "rms": round(rms, 6)}
                if events and start - events[-1]["end"] <= block_seconds * 2:
                    events[-1]["end"] = end
                    events[-1]["strength"] = max(events[-1]["strength"], item["strength"])
                else:
                    events.append(item)
                # Keep bounded event storage even with very long/noisy recordings.
                if len(events) > max_events * 2:
                    events = sorted(sorted(events, key=lambda row: -row["strength"])[:max_events], key=lambda row: row["start"])
            history.append(rms)
            sample_position += frames
    results = sorted(sorted(events, key=lambda item: -item["strength"])[:max_events], key=lambda item: item["start"])
    log.info("声音分析完成：%d 个音量变化提示（不代表已识别比赛事件），时长 %.1f 秒", len(results), total_duration)
    return results
