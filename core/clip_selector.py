"""Overlapping, bounded transcript windows and recoverable clip selection."""
from __future__ import annotations

from core.llm_settings import cache_identity, configured

import hashlib
import json
import logging
import math
from pathlib import Path

from core.cache import read_cache
from core.llm_client import LLMClient, LLMError, atomic_json
from core.timeline import canonical_segments, clip_limits, consolidate_clips, normalize_candidates

log = logging.getLogger(__name__)

CATEGORIES = ["goal_reaction", "tactical_analysis", "player_review", "coach_review",
              "referee_controversy", "humor", "argument", "surprise", "halftime_summary",
              "postmatch_review", "opinion", "chat_interaction", "commentary"]
CLIP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["clips"], "properties": {
    "clips": {"type": "array", "maxItems": 100, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["start", "end", "category", "title", "description", "reason", "score", "tags", "evidence"],
        "properties": {
            "start": {"type": "number", "minimum": 0}, "end": {"type": "number", "minimum": 0},
            "category": {"type": "string", "enum": CATEGORIES},
            "title": {"type": "string", "minLength": 1, "maxLength": 160},
            "description": {"type": "string", "maxLength": 1200},
            "reason": {"type": "string", "maxLength": 1200},
            "score": {"type": "number", "minimum": 0, "maximum": 100},
            "tags": {"type": "array", "maxItems": 20, "items": {"type": "string", "maxLength": 80}},
            "evidence": {"type": "array", "minItems": 1, "maxItems": 20,
                         "items": {"type": "string", "minLength": 2, "maxLength": 2000}},
        }}}}}


def validate_clip_payload(value: dict) -> None:
    """Strict validation without requiring another runtime package."""
    if not isinstance(value, dict) or set(value) != {"clips"} or not isinstance(value["clips"], list):
        raise ValueError("输出必须是仅含 clips 数组的 JSON 对象")
    if len(value["clips"]) > 100:
        raise ValueError("单窗口候选超过 100 条")
    required = set(CLIP_SCHEMA["properties"]["clips"]["items"]["required"])
    for clip in value["clips"]:
        if not isinstance(clip, dict) or set(clip) != required:
            raise ValueError("候选字段缺失或包含额外字段")
        for key in ("start", "end", "score"):
            number = clip[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ValueError(f"{key} 必须是有限数值")
        if not 0 <= clip["start"] < clip["end"] or not 0 <= clip["score"] <= 100:
            raise ValueError("候选时间/分数非法")
        if clip["category"] not in CATEGORIES:
            raise ValueError("不支持的内容类别")
        for key, maximum in (("title", 160), ("description", 1200), ("reason", 1200)):
            if not isinstance(clip[key], str) or len(clip[key]) > maximum or (key == "title" and not clip[key].strip()):
                raise ValueError(f"候选 {key} 非法")
        for key, minimum, maximum_length in (("tags", 0, 80), ("evidence", 1, 2000)):
            values = clip[key]
            if not isinstance(values, list) or not minimum <= len(values) <= 20:
                raise ValueError(f"候选 {key} 必须是字符串数组")
            if any(not isinstance(item, str) or len(item) > maximum_length or (key == "evidence" and len(item) < 2)
                   for item in values):
                raise ValueError(f"候选 {key} 元素非法")


def build_windows(transcript: dict, duration: float, cfg: dict) -> list[dict]:
    """Temporal overlap plus bounded text; splitting retains boundary sentences."""
    rules = cfg.get("selection", {})
    window_size = max(10, float(rules.get("window_seconds", 360)))
    overlap = max(0, min(float(rules.get("overlap_seconds", 60)), window_size * 0.8))
    max_chars = max(512, int(rules.get("max_window_chars", 18000)))
    segments = canonical_segments(transcript, duration)
    if not segments:
        return []
    windows, fingerprints = [], set()
    start = max(0, segments[0]["start"] - overlap)
    while start < min(duration, segments[-1]["end"]):
        end = min(duration, start + window_size)
        source = [segment for segment in segments if segment["end"] > start and segment["start"] < end]
        pieces = []
        for segment in source:
            # Extreme ASR segments may be split in text, but never invent new times.
            allowance = max(64, max_chars - 200)
            for offset in range(0, len(segment["text"]), allowance):
                pieces.append({"id": segment["id"], "start": segment["start"], "end": segment["end"],
                               "text": segment["text"][offset:offset + allowance]})
        index, previous = 0, None
        while index < len(pieces):
            batch, used = [], 2
            if previous is not None:
                size = len(json.dumps(previous, ensure_ascii=False)) + 2
                next_size = len(json.dumps(pieces[index], ensure_ascii=False)) + 2
                if size + next_size + 2 <= max_chars:
                    batch, used = [previous], size + 2
            while index < len(pieces):
                item = pieces[index]
                size = len(json.dumps(item, ensure_ascii=False)) + 2
                if batch and used + size > max_chars:
                    break
                batch.append(item)
                used += size
                index += 1
            if not batch:
                break
            previous = batch[-1]
            fingerprint = hashlib.sha256(json.dumps(batch, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if fingerprint not in fingerprints:
                windows.append({"start": max(0, min(start, batch[0]["start"])),
                                "end": min(duration, max(end, batch[-1]["end"])), "segments": batch})
                fingerprints.add(fingerprint)
        start += window_size - overlap
    return windows


def _mock_candidates(transcript: dict, duration: float, cfg: dict) -> list[dict]:
    segments = canonical_segments(transcript, duration)
    if not segments:
        return []
    # Explicit simulation uses the real supplied fixture's times and quotations.
    count = min(len(segments), max(3, min(12, math.ceil(duration / 30))))
    chosen = [segments[min(len(segments) - 1, index * len(segments) // count)] for index in range(count)]
    raw = [{"start": s["start"], "end": s["end"], "category": "commentary",
            "title": f"[模拟] 转录片段 {index + 1}：{s['text'][:24]}",
            "description": "模拟选片，仅用于验证流水线；未调用大模型。", "reason": "引用已有转录，验证候选与字幕时间轴。",
            "score": 50, "tags": ["模拟测试"], "evidence": [s["text"]], "source": "mock"}
           for index, s in enumerate(chosen)]
    return normalize_candidates(raw, transcript, duration, cfg)


def _audio_candidates(events: list[dict], duration: float, cfg: dict) -> list[dict]:
    results = []
    minimum, maximum = clip_limits("surprise", cfg)
    for event in events:
        start, end, strength = event.get("start"), event.get("end"), event.get("strength", 1)
        if any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
               for value in (start, end, strength)) or not 0 <= start < end <= duration:
            continue
        if event.get("kind") != "energy_rise":
            continue
        begin, finish = max(0, start - 8), min(duration, end + 12)
        if finish - begin < min(minimum, duration):
            finish = min(duration, begin + minimum)
            begin = max(0, finish - min(minimum, duration))
        if finish - begin > maximum:
            finish = begin + maximum
        results.append({"start": round(begin, 3), "end": round(finish, 3), "category": "surprise",
                        "title": "音量明显升高（待人工确认）", "description": "检测到局部音量升高，需播放确认内容。",
                        "reason": f"音频能量约为附近基线的 {strength:.1f} 倍；不能据此确认比赛事件或情绪。",
                        "score": min(75, 35 + strength * 5), "tags": ["声音提示"], "source": "audio",
                        "evidence": [f"RMS 能量变化：{start:.3f}–{end:.3f} 秒"], "segment_ids": [],
                        "audio_event": dict(event), "needs_human_review": True, "grounding_status": "measured_audio_only"})
    return results


def select_clips(transcript: dict, meta: dict, cfg: dict, project_dir, mock=False,
                 audio_events=None) -> list[dict]:
    duration = float(meta["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("视频时长必须是正有限数值")
    selection_dir = Path(project_dir) / "selection"
    selection_dir.mkdir(parents=True, exist_ok=True)
    events = audio_events or []
    mode = cfg.get("selection", {}).get("mode", "hybrid")
    if mode not in ("semantic", "hybrid", "audio"):
        raise ValueError("selection.mode 必须为 semantic、hybrid 或 audio")
    if mock:
        clips = _mock_candidates(transcript, duration, cfg)
        atomic_json(selection_dir / "mock_decisions.json", {"mock": True, "clips": clips})
        return clips
    use_audio = mode == "audio" or (mode == "hybrid" and cfg.get("selection", {}).get("audio_events", True))
    if mode == "audio":
        result = consolidate_clips(_audio_candidates(events, duration, cfg), cfg)
        atomic_json(selection_dir / "decisions.json", {"mock": False, "mode": mode, "clips": result})
        return result
    if not use_audio:
        events = []
    windows = build_windows(transcript, duration, cfg)
    if not windows:
        return consolidate_clips(_audio_candidates(events, duration, cfg), cfg) if use_audio else []
    options = cfg.get("openai", {})
    prompt_path = Path(cfg.get("prompt_path") or Path(__file__).parents[1] / "prompts" / "clip_selection.md")
    prompt = prompt_path.read_text(encoding="utf-8")
    client = LLMClient(options, Path(cfg.get("_budget_path") or selection_dir / "api_budget.json"))
    candidates = []
    for number, window in enumerate(windows, 1):
        hints = [event for event in events if event["end"] > window["start"] and event["start"] < window["end"]][:20]
        payload = {"video_duration": duration, "window_start": window["start"], "window_end": window["end"],
                   "category_lengths": cfg.get("selection", {}).get("category_lengths", {}),
                   "segments": window["segments"], "audio_hints": hints}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        identity = {"version": 2, "model": options.get("model"), "base_url": options.get("base_url"),
                    "prompt": prompt, "payload": text, "schema": CLIP_SCHEMA, **cache_identity(options)}
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        cache_path = selection_dir / f"window_{key}.json"
        response = None
        cache = read_cache(cache_path, lambda value: validate_clip_payload(value["response"]))
        if cache and cache.get("key") == key:
            response = cache["response"]
            log.info("选片窗口 %d/%d：复用缓存", number, len(windows))
        if response is None:
            if options.get("provider", "api") != "codex_cli" and not options.get("api_key"):
                raise LLMError("未配置 OPENAI_API_KEY；转录和已完成窗口可复用，配置密钥后重试选片，或显式使用 --mock")
            if options.get("provider", "api") != "codex_cli" and not str(options.get("model") or "").strip():
                raise LLMError("未配置 OPENAI_MODEL；请填写服务商实际支持的模型名称，转录和已完成窗口可继续复用")
            log.info("选片窗口 %d/%d：%.1f–%.1f 秒", number, len(windows), window["start"], window["end"])
            response = client.complete([{"role": "system", "content": prompt}, {"role": "user", "content": text}],
                                       CLIP_SCHEMA, validate_clip_payload)
            atomic_json(cache_path, {"key": key, "response": response})
        # Ground against exactly the text supplied in this window, not distant text.
        grounded = normalize_candidates(response["clips"], {"segments": window["segments"]}, duration, cfg)
        candidates.extend(grounded)
    if use_audio:
        for audio_clip in _audio_candidates(events, duration, cfg):
            event = audio_clip["audio_event"]
            # An energy burst already wholly covered by a semantic candidate
            # adds no new footage; retain the measured evidence on that candidate.
            covered = next((clip for clip in candidates if clip["start"] <= event["start"]
                            and clip["end"] >= event["end"]), None)
            if covered is None:
                candidates.append(audio_clip)
            else:
                covered.setdefault("audio_events", []).append(event)
    result = consolidate_clips(candidates, cfg)
    atomic_json(selection_dir / "decisions.json", {"mock": False, "source": meta.get("source"),
                                                   "duration": duration, "clips": result})
    return result
