"""Local ASR engines sharing canonical timestamps and durable chunk checkpoints."""
from __future__ import annotations

import dataclasses
import logging
import math
import wave
from pathlib import Path

from core.cache import read_cache
from core.gpu_runtime import prepare_cuda_runtime, runtime_error
from core.utils import atomic_json, fingerprint, read_json

log = logging.getLogger(__name__)


class DeviceUnavailable(RuntimeError):
    pass


def validate_transcript(data, duration):
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("转录时长必须是正有限数值")
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        raise ValueError("转录 JSON 必须包含 segments 数组")
    segments = []
    for i, segment in enumerate(data["segments"]):
        if not isinstance(segment, dict):
            raise ValueError(f"转录段 {i} 必须是对象")
        s, e = segment.get("start"), segment.get("end")
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (s, e)):
            raise ValueError(f"转录段 {i} 含非法时间戳")
        if not 0 <= s < min(e, duration) or e > duration + 0.05:
            raise ValueError(f"转录段 {i} 时间范围越界: {s}–{e} (视频 {duration:.2f}s)")
        if not isinstance(segment.get("text"), str):
            raise ValueError(f"转录段 {i} 缺少文本")
        # Normalize before clamping words. max(0, 0.0) otherwise returns an int
        # on the first pass and a float on the second, changing cache identity.
        s, e = float(s), float(e)
        item = dict(segment, id=str(segment.get("id", i)), start=float(s), end=min(float(e), duration))
        words = []
        raw_words = item.get("words") or []
        if not isinstance(raw_words, list):
            raise ValueError(f"转录段 {i} 的 words 必须是数组")
        for word in raw_words:
            if not isinstance(word, dict):
                raise ValueError(f"转录段 {i} 的词级记录必须是对象")
            ws, we = word.get("start"), word.get("end")
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (ws, we)):
                raise ValueError(f"转录段 {i} 的词级时间戳无效")
            if not s - 0.05 <= ws <= we <= e + 0.05:
                raise ValueError(f"转录段 {i} 的词级时间戳不在句子范围内")
            if not isinstance(word.get("word"), str):
                raise ValueError(f"转录段 {i} 的词级文本无效")
            clamped_start, clamped_end = max(s, ws), min(item["end"], we)
            if clamped_end < clamped_start:
                raise ValueError(f"转录段 {i} 的词级时间戳裁剪后无效")
            words.append(dict(word, start=clamped_start, end=clamped_end))
        item["words"] = words
        segments.append(item)
    segments.sort(key=lambda x: (x["start"], x["end"]))
    return dict(data, segments=segments)


def _serialize(obj):
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, "_asdict"):
        return {k: _serialize(v) for k, v in obj._asdict().items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


def resolve_device(asr_cfg, allow_cpu=False):
    requested = asr_cfg["device"]
    device = "cuda" if requested == "auto" else requested
    # CTranslate2 imports/later lazy CUDA calls must see wheel DLL directories.
    runtime = prepare_cuda_runtime() if device == "cuda" else {"usable": None}
    try:
        import ctranslate2
    except ImportError as exc:
        raise RuntimeError("未安装 faster-whisper：请执行 pip install -r requirements.txt") from exc
    try:
        if device == "cuda" and ctranslate2.get_cuda_device_count() < 1:
            raise RuntimeError("CTranslate2 没有检测到可用的 NVIDIA CUDA 设备")
        if device == "cuda" and runtime.get("usable") is False:
            raise RuntimeError(runtime_error(runtime))
        supported = ctranslate2.get_supported_compute_types(device)
    except Exception as exc:
        if device == "cuda" and allow_cpu:
            log.warning("CUDA 不可用 (%s)，根据 --allow-cpu 改用 CPU", exc)
            device = "cpu"
            supported = ctranslate2.get_supported_compute_types(device)
        else:
            raise DeviceUnavailable(f"{device} 不可用: {exc}。检查 NVIDIA 驱动/CUDA 12/cuDNN 9；"
                                    "可加 --allow-cpu 或设置 DEVICE=cpu。") from exc
    compute = asr_cfg["compute_type"]
    if compute not in supported:
        preferred = ("int8_float16", "float16", "int8", "float32") if device == "cuda" else ("int8", "float32")
        compute = next((x for x in preferred if x in supported), "default")
        log.warning("%s 不支持 %s，使用 %s；可用精度: %s", device, asr_cfg["compute_type"], compute, sorted(supported))
    return device, compute


def _make_model(cfg, allow_cpu):
    device, compute = resolve_device(cfg["asr"], allow_cpu)
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel(cfg["asr"]["model"], device=device, compute_type=compute)
    except Exception as exc:
        if device == "cuda" and allow_cpu:
            log.warning("CUDA 模型加载失败 (%s)，尝试 CPU/int8", exc)
            device, compute = "cpu", "int8"
            model = WhisperModel(cfg["asr"]["model"], device=device, compute_type=compute)
        else:
            raise DeviceUnavailable(f"Whisper 模型加载失败: {exc}。首次运行需要下载模型，"
                                    "请检查网络/本地模型路径；CUDA 问题可加 --allow-cpu。") from exc
    log.info("Whisper 模型已加载: %s · device=%s · compute_type=%s", cfg["asr"]["model"], device, compute)
    return model, device, compute


def _slice_wav(source, destination, start, end):
    with wave.open(str(source), "rb") as src, wave.open(str(destination), "wb") as dst:
        dst.setparams(src.getparams())
        rate = src.getframerate()
        src.setpos(min(int(start * rate), src.getnframes()))
        remaining = int((end - start) * rate)
        while remaining > 0:
            count = min(remaining, rate * 5)
            block = src.readframes(count)
            if not block:
                break
            dst.writeframesraw(block)
            remaining -= count


def asr_cache_key(meta, cfg):
    """Preserve legacy Whisper keys and isolate local model/algorithm revisions."""
    duration = float(meta['duration'])
    local = cfg['asr'].get('engine', 'faster_whisper') == 'zipformer_http'
    identity = {k:v for k,v in cfg['asr'].items() if k != 'engine' and not k.startswith('local_')}
    if local:
        from core.local_asr import MODEL_SHA256
        identity = {'engine':'zipformer_http', 'model_sha256':MODEL_SHA256,
                    'chunk_seconds':cfg['asr'].get('local_chunk_seconds',60),
                    'overlap_seconds':cfg['asr']['overlap_seconds'], 'timeline_version':1}
    return fingerprint({"asr": identity, "terms": [] if local else cfg["terms"], "source": meta["source"],
                       "duration": duration, "track": meta.get("selected_audio"),
                       "source_signature": meta.get("source_signature"), "version": 2})


def transcribe_audio(wav_path, meta, cfg, project_dir, allow_cpu=False, transcript_path=None):
    directory = Path(project_dir)
    duration = float(meta["duration"])
    final = directory / "transcript.json"
    if transcript_path:
        original = read_json(transcript_path)
        data = validate_transcript(original, duration)
        data.update(video=meta["source"], provenance="imported", timeline_origin=meta.get("timeline_origin", 0))
        atomic_json(directory / "transcript.imported.raw.json", original)
        atomic_json(final, data)
        return data
    local = cfg['asr'].get('engine', 'faster_whisper') == 'zipformer_http'
    key = asr_cache_key(meta, cfg)
    cached = read_cache(final, lambda data: validate_transcript(data, duration))
    if cached and cached.get("cache_key") == key:
        log.info("复用完整转录缓存: %s", final)
        return validate_transcript(cached, duration)
    checkpoint = directory / "asr" / key
    checkpoint.mkdir(parents=True, exist_ok=True)
    chunk_size = cfg["asr"].get('local_chunk_seconds', 60) if local else cfg["asr"]["chunk_seconds"]
    overlap = cfg["asr"]["overlap_seconds"]
    total = math.ceil(duration / chunk_size)
    model = None
    all_segments = []
    runtime = {}
    for index in range(total):
        core_start = index * chunk_size
        core_end = min(duration, core_start + chunk_size)
        start = max(0, core_start - overlap)
        end = min(duration, core_end + overlap)
        chunk_json = checkpoint / f"chunk_{index:04d}.json"

        def validate_chunk(value):
            if value.get("start") != start or value.get("end") != end:
                raise ValueError("转录块时间范围与检查点不一致")
            validate_transcript(value, end - start)
            if not isinstance(value.get("runtime", {}), dict):
                raise ValueError("转录块运行信息无效")

        chunk = read_cache(chunk_json, validate_chunk)
        if chunk is not None:
            runtime = chunk.get("runtime", {})
            log.info("复用转录块 %s/%s", index + 1, total)
        else:
            if model is None:
                if local:
                    from core.local_asr import ZipformerHTTP, MODEL_SHA256
                    model = ZipformerHTTP(cfg['asr'])
                    runtime = {'device': 'cpu', 'compute_type': 'int8', 'engine': 'zipformer_http',
                               'model_sha256': MODEL_SHA256,
                               'timestamp_method': 'ctc-byte-bpe-onsets-estimated-ends-v1'}
                    log.info('本地 Zipformer 中文 CPU 识别 · %s · 无云端 ASR · 分块 %.0fs', model.url, chunk_size)
                else:
                    model, device, compute = _make_model(cfg, allow_cpu)
                    runtime = {"device": device, "compute_type": compute}
            chunk_wav = checkpoint / f"chunk_{index:04d}.wav"
            _slice_wav(wav_path, chunk_wav, start, end)
            for attempt in range(2):
                try:
                    segments, info = model.transcribe(str(chunk_wav), language=cfg["asr"].get("language") or None,
                        word_timestamps=True, vad_filter=cfg["asr"]["vad_filter"],
                        beam_size=cfg["asr"]["beam_size"], initial_prompt="足球术语：" + "、".join(cfg["terms"])[:1500])
                    raw = []
                    for seg in segments:
                        raw.append(_serialize(seg))
                        log.info("转录 %d/%d · %.1f%% · %.1f/%.1f 秒", index + 1, total,
                                 min(100, (start + seg.end) / duration * 100), start + seg.end, duration)
                    chunk = {"start": start, "end": end, "segments": raw, "runtime": runtime,
                             "language": getattr(info, "language", None)}
                    if local:
                        chunk['raw_result'] = model.last_raw
                    validate_chunk(chunk)
                    atomic_json(chunk_json, chunk)
                    break
                except Exception as exc:
                    is_cuda_error = any(x in str(exc).lower() for x in ("cuda", "cudnn", "cublas", "out of memory"))
                    if attempt == 0 and runtime.get("device") == "cuda" and allow_cpu and is_cuda_error:
                        log.warning("转录期间 GPU 失败 (%s)，保留已完成块并使用 CPU 重试本块", exc)
                        del model
                        from faster_whisper import WhisperModel
                        model = WhisperModel(cfg["asr"]["model"], device="cpu", compute_type="int8")
                        runtime = {"device": "cpu", "compute_type": "int8"}
                    else:
                        hint = '请检查本地 ASR 服务后重试。' if local else '可修复依赖后重新运行；GPU 错误可加 --allow-cpu。'
                        raise RuntimeError(f"转录块 {index+1}/{total} 失败，已完成块已缓存: {exc}。" + hint) from exc
            chunk_wav.unlink(missing_ok=True)
        # Ownership by word midpoint eliminates overlap duplication without dropping a
        # sentence merely because its beginning is in the adjacent chunk.
        for raw in chunk["segments"]:
            words = []
            for word in raw.get("words") or []:
                ws, we = start + word["start"], start + word["end"]
                if core_start <= (ws + we) / 2 < core_end and we > ws:
                    words.append(dict(word, start=max(core_start, ws), end=min(core_end, we)))
            if raw.get("words"):
                if not words:
                    continue
                segment = {"start": words[0]["start"], "end": words[-1]["end"],
                           "text": "".join(w["word"] for w in words), "words": words}
            else:
                s, e = start + raw["start"], start + raw["end"]
                if not core_start <= (s + e) / 2 < core_end:
                    continue
                segment = {"start": max(0, s), "end": min(duration, e), "text": raw["text"], "words": []}
            if segment["end"] > segment["start"]:
                all_segments.append(segment)
    data = validate_transcript({"video": meta["source"], "segments": all_segments, "cache_key": key,
                               "provenance": "zipformer-local-cpu" if local else "faster-whisper", "runtime": runtime,
                               "timeline_origin": meta.get("timeline_origin", 0)}, duration)
    atomic_json(final, data)
    return data
