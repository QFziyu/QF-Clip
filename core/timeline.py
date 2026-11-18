"""Ground model intervals in observed speech, extend context, and deduplicate."""
from __future__ import annotations

import hashlib
import logging
import math
import re

log = logging.getLogger(__name__)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _text(value):
    return re.sub(r"\s+", "", str(value))


def canonical_segments(transcript: dict, duration: float) -> list[dict]:
    result = []
    for index, segment in enumerate(transcript.get("segments", [])):
        start, end = segment.get("start"), segment.get("end")
        if not _number(start) or not _number(end) or start < 0 or end <= start or start >= duration:
            continue
        if not str(segment.get("text", "")).strip():
            continue
        result.append({**segment, "id": str(segment.get("id", index)), "start": float(start),
                       "end": min(float(end), duration), "text": str(segment["text"]).strip()})
    return sorted(result, key=lambda item: (item["start"], item["end"]))


def clip_limits(category: str, cfg: dict):
    rules = cfg.get("selection", cfg)
    pair = rules.get("category_lengths", {}).get(category)
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        low, high = pair
    elif isinstance(pair, dict):
        low, high = pair.get("min", 15), pair.get("max", 240)
    else:
        low, high = rules.get("min_seconds", 15), rules.get("max_seconds", 240)
    return max(0.1, float(low)), max(float(low), float(high))


def _assign_id(clip: dict):
    identity = f"{clip['start']:.3f}|{clip['end']:.3f}|{clip['category']}|{clip['title']}"
    clip["id"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return clip


def consolidate_clips(clips: list[dict], cfg: dict) -> list[dict]:
    """Do not discard distinct viewpoints merely because their times overlap."""
    rules = cfg.get("selection", cfg)
    threshold = float(rules.get("dedup_iou", 0.65))
    selected = []
    for clip in sorted(clips, key=lambda item: (-item.get("score", 0), item["start"])):
        duplicate = None
        for previous in selected:
            intersection = max(0, min(clip["end"], previous["end"]) - max(clip["start"], previous["start"]))
            union = max(clip["end"], previous["end"]) - min(clip["start"], previous["start"])
            overlap = intersection / union if union else 0
            quotes = {_text(item) for item in clip.get("evidence", []) if isinstance(item, str)}
            prior_quotes = {_text(item) for item in previous.get("evidence", []) if isinstance(item, str)}
            same_support = any(a in b or b in a for a in quotes for b in prior_quotes if min(len(a), len(b)) >= 4)
            same_title = _text(clip["title"]) == _text(previous["title"])
            audio_duplicate = clip.get("source") == previous.get("source") == "audio" and overlap >= threshold
            if overlap >= threshold and (same_support or same_title or audio_duplicate):
                duplicate = previous
                break
        if duplicate is not None:
            low, high = clip_limits(duplicate["category"], cfg)
            combined_start, combined_end = min(clip["start"], duplicate["start"]), max(clip["end"], duplicate["end"])
            if combined_end - combined_start <= high:
                duplicate["start"], duplicate["end"] = combined_start, combined_end
                duplicate["evidence"] = list(dict.fromkeys(duplicate.get("evidence", []) + clip.get("evidence", [])))
                duplicate["segment_ids"] = list(dict.fromkeys(duplicate.get("segment_ids", []) + clip.get("segment_ids", [])))
            duplicate["duplicate_count"] = duplicate.get("duplicate_count", 0) + 1
            continue
        selected.append(dict(clip))
    maximum = max(1, int(rules.get("max_candidates", 60)))
    if len(selected) > maximum:
        log.warning("候选 %d 条，按分数保留配置上限 %d 条；提高 max_candidates 可提高召回", len(selected), maximum)
    return [_assign_id(clip) for clip in sorted(selected[:maximum], key=lambda item: item["start"])]


def normalize_candidates(candidates: list[dict], transcript: dict, duration: float, cfg: dict) -> list[dict]:
    """Every accepted semantic candidate must cite literal, timed transcript text.

    This checks evidence and timing, not whether commentary accurately describes
    the match. Every result still requires human review of all factual claims.
    """
    rules = cfg.get("selection", cfg)
    segments = canonical_segments(transcript, duration)
    accepted = []
    for candidate in candidates:
        start, end = candidate.get("start"), candidate.get("end")
        if not _number(start) or not _number(end) or not 0 <= start < end <= duration:
            log.warning("丢弃非法或越界候选时间：%r 到 %r", start, end)
            continue
        overlapping = [s for s in segments if s["end"] > start and s["start"] < end]
        evidence = candidate.get("evidence", [])
        if isinstance(evidence, str):
            evidence = [evidence]
        if not isinstance(evidence, list) or not evidence:
            log.warning("丢弃没有原文依据的候选：%s", candidate.get("title", ""))
            continue
        supported, ids = [], []
        for quote in evidence:
            if not isinstance(quote, str) or len(_text(quote)) < 2:
                continue
            matching = [s for s in overlapping if _text(quote) in _text(s["text"])]
            if matching:
                supported.append(quote)
                ids.extend(s["id"] for s in matching)
        if len(supported) != len(evidence):
            log.warning("丢弃无法在候选时间内证实全部引用的片段：%s", candidate.get("title", ""))
            continue
        category = str(candidate.get("category", "commentary"))
        minimum, maximum = clip_limits(category, cfg)
        # Snap to complete ASR segments; refuse spans which cannot fit intact.
        anchor_start, anchor_end = overlapping[0]["start"], overlapping[-1]["end"]
        if anchor_end - anchor_start > maximum + 0.001:
            log.warning("候选完整语句跨度超过该分类最大长度，跳过：%s", candidate.get("title", ""))
            continue
        before = max(0, float(rules.get("context_before", 8)))
        after = max(0, float(rules.get("context_after", 10)))
        available = maximum - (anchor_end - anchor_start)
        proportion = min(1, available / max(before + after, 0.0001))
        target_start = max(0, anchor_start - before * proportion)
        target_end = min(duration, anchor_end + after * proportion)
        # Include nearby complete sentences only when their complete boundaries fit.
        for s in reversed(segments):
            if target_start <= s["end"] <= anchor_start and target_end - s["start"] <= maximum:
                target_start = min(target_start, s["start"])
        for s in segments:
            if anchor_end <= s["start"] <= target_end and s["end"] - target_start <= maximum:
                target_end = max(target_end, s["end"])
        missing = min(minimum, duration) - (target_end - target_start)
        if missing > 0:
            target_start = max(0, target_start - missing / 2)
            target_end = min(duration, max(target_end, target_start + min(minimum, duration)))
            target_start = max(0, target_end - min(minimum, duration))
        result = {k: candidate.get(k, default) for k, default in
                  (("category", "commentary"), ("title", "待审核候选"), ("description", ""),
                   ("reason", ""), ("score", 50), ("tags", []))}
        if not _number(result["score"]):
            continue
        result.update(start=round(target_start, 3), end=round(target_end, 3), evidence=supported,
                      segment_ids=list(dict.fromkeys(ids)), score=max(0, min(100, float(result["score"]))),
                      source=candidate.get("source", "llm"), needs_human_review=True,
                      grounding_status="quoted_text_and_timestamps_checked",
                      requested_start=start, requested_end=end)
        accepted.append(result)
    return consolidate_clips(accepted, cfg)
