"""A dual-source timeline: commentator_time = match_time + offset_seconds."""
from __future__ import annotations

import math


def finite(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} 必须是 {low}～{high} 的有限数值")
    return float(value)


def normalize_composite_options(value=None):
    value = {} if value is None else value
    defaults = {"offset_seconds": 0., "sync_confirmed": False, "layout": "fit", "width_ratio": .25,
                "position": "bottom_left", "margin_ratio": .02, "audio": "commentator", "match_volume": .25,
                "commentator_volume": 1., "face_sample_seconds": 1., "title": "比赛与主播合成",
                "match_audio_mode": "original", "vocal_device": "auto"}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("双路合成设置包含未知字段")
    result = {**defaults, **value}
    for field, low, high in [("offset_seconds", -86400, 86400), ("width_ratio", .1, .5), ("margin_ratio", 0, .1),
                              ("match_volume", 0, 2), ("commentator_volume", 0, 2), ("face_sample_seconds", .5, 5)]:
        result[field] = finite(result[field], field, low, high)
    if not isinstance(result["sync_confirmed"], bool):
        raise ValueError("sync_confirmed 必须为布尔值")
    for field, allowed in [("layout", ("fit", "face")), ("position", ("bottom_left", "bottom_right", "top_left", "top_right")),
                           ("audio", ("commentator", "match", "mix")),
                           ("match_audio_mode", ("original", "mute", "remove_vocals")),
                           ("vocal_device", ("auto", "cpu", "cuda"))]:
        if result[field] not in allowed:
            raise ValueError(f"{field} 设置无效")
    if not isinstance(result["title"], str) or not result["title"].strip() or len(result["title"]) > 300 or "\0" in result["title"]:
        raise ValueError("合成标题需要 1～300 字符")
    result["title"] = result["title"].strip()
    return result


def overlap_range(match_duration, commentator_duration, offset):
    start, end = max(0., -offset), min(match_duration, commentator_duration - offset)
    if end <= start:
        raise ValueError("两路录像在当前偏移下没有公共时间，请重新对轴")
    return {"start": float(start), "end": float(end)}


def validate_ranges(segments, overlap):
    if segments is None:
        return [overlap.copy()]
    if not isinstance(segments, list) or not 1 <= len(segments) <= 200:
        raise ValueError("合成需要 1～200 个比赛录像区间")
    result, previous = [], overlap["start"]
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {"start", "end"}:
            raise ValueError("合成区间只允许 start 和 end")
        start = finite(segment["start"], "合成起点", overlap["start"], overlap["end"])
        end = finite(segment["end"], "合成终点", overlap["start"], overlap["end"])
        if end <= start or start < previous - 1e-6:
            raise ValueError("合成区间必须按比赛录像时间排序、互不重叠，且起点小于终点")
        result.append({"start": start, "end": end})
        previous = end
    return result
