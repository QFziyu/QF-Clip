"""Validated editorial choices, independent of ASR/window sizes and API secrets."""
from __future__ import annotations

import math
import re

CONTENT_LABELS = {"match": "比赛", "conversation": "足球对话", "custom": "自定义提示词"}
FORMAT_LABELS = {"shorts": "切片", "continuous": "连续长段", "montage": "全场集锦"}
PERIOD_LABELS = {"first": "上半场", "second": "下半场", "extra_first": "加时上半场", "extra_second": "加时下半场"}
PERIOD_BASE = {"first": 0, "second": 45, "extra_first": 90, "extra_second": 105}
CONTENT_PROMPTS = {
    "match": "优先识别比赛进程、解说反应、判罚讨论、进球话题、战术变化及赛后总结。不能仅凭转录确认画面中的比赛事件。",
    "conversation": "这是足球对话或综艺节目。重点识别完整话题、新闻讨论、球员评价、嘉宾争论、趣味交流及观点的前因后果。无需寻找比赛现场进球；避免只截一句结论而遗漏讨论。",
    "custom": "按用户提供的主题与筛选偏好选择有原文依据的内容。",
}


def number(value, name, minimum=0, maximum=10800):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须是 {minimum:g}～{maximum:g} 的有限数值")
    return float(value)


def video_seconds(value):
    if isinstance(value, (float, int)):
        return number(value, "录像时间", maximum=86400)
    if not isinstance(value, str):
        raise ValueError("录像时间请使用秒数或 HH:MM:SS")
    pieces = value.strip().split(":")
    try:
        if len(pieces) == 1:
            return number(float(pieces[0]), "录像时间", maximum=86400)
        if len(pieces) not in (2, 3):
            raise ValueError()
        values = list(map(float, pieces))
        if any(not math.isfinite(x) or x < 0 for x in values) or any(x >= 60 for x in values[1:]):
            raise ValueError()
        return number(sum(x * 60 ** i for i, x in enumerate(reversed(values))), "录像时间", maximum=86400)
    except (TypeError, ValueError):
        raise ValueError("录像时间请使用秒数或 HH:MM:SS") from None


def match_seconds(value, period):
    """Use absolute match minutes; 45+2:10 is 47:10, second-half 61 is 61:00."""
    if period not in PERIOD_BASE:
        raise ValueError("请选择有效的比赛阶段")
    if not isinstance(value, str):
        raise ValueError("比赛时间请使用 35:20、61:00 或 45+2:10")
    match = re.fullmatch(r"\s*(\d{1,3})(?:\+(\d{1,2}))?(?::([0-5]\d(?:\.\d+)?))?\s*", value)
    if not match:
        raise ValueError("比赛时间请使用 35:20、61:00 或 45+2:10")
    minute, extra, second = match.groups()
    stoppage = extra is not None
    minute, extra, second = int(minute), int(extra or 0), float(second or 0)
    base = PERIOD_BASE[period]
    end = base + (45 if period in ("first", "second") else 15)
    if not base <= minute <= end or (stoppage and minute != end) or (minute == end and not stoppage and second > 0):
        raise ValueError(f"{PERIOD_LABELS[period]}请使用比赛累计分钟；补时例如 {end}+2:10")
    return (minute + extra) * 60 + second


def normalize_editing_options(value=None):
    value = {} if value is None else value
    allowed = {"content", "format", "prompt", "count", "target_seconds", "min_seconds", "max_seconds",
               "anchors", "events", "event_before", "event_after", "clock_mode"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("剪辑方案参数包含未知字段")
    if value.get('clock_mode', 'manual') not in ('auto', 'manual'):
        raise ValueError('比赛对轴方式应为 auto 或 manual')
    content, form = value.get("content", "match"), value.get("format", "shorts")
    if not isinstance(content, str) or not isinstance(form, str) or content not in CONTENT_LABELS or form not in FORMAT_LABELS:
        raise ValueError("未知内容标准或成品形式")
    prompt = value.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > 12000 or "\0" in prompt:
        raise ValueError("自定义提示词须为不超过 12000 字符的文本")
    if content == "custom" and not prompt.strip():
        raise ValueError("自定义标准需要填写提示词")
    count = value.get("count", 10 if form == "shorts" else 2)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 60:
        raise ValueError("目标条数须为 1～60 的整数")
    low = number(value.get("min_seconds", 90 if form == "shorts" else 300), "最短时长", 1)
    high = number(value.get("max_seconds", 180 if form == "shorts" else 1200), "最长时长", 1)
    target = number(value.get("target_seconds", 120 if form == "shorts" else 600), "目标时长", 1)
    if not low <= target <= high:
        raise ValueError("需满足 最短时长 ≤ 目标时长 ≤ 最长时长")
    anchors, events = value.get("anchors", []), value.get("events", [])
    if not isinstance(anchors, list) or not isinstance(events, list) or len(anchors) > 100 or len(events) > 200:
        raise ValueError("校准点最多 100 个，必剪事件最多 200 个")
    normalized_anchors = []
    for anchor in anchors:
        if not isinstance(anchor, dict) or set(anchor) - {"period", "match_time", "video_time", "video_start", "video_end"}:
            raise ValueError("校准点需要 period、match_time、video_time，可选 video_end")
        period = anchor.get("period", "first")
        match_seconds(anchor.get("match_time", ""), period)
        item = {"period": period, "match_time": anchor["match_time"], "video_time": video_seconds(anchor.get("video_time"))}
        if anchor.get("video_start") not in (None, ""):
            item["video_start"] = video_seconds(anchor["video_start"])
            if item["video_start"] > item["video_time"]:
                raise ValueError("校准区间开始不能晚于校准点")
        if anchor.get("video_end") not in (None, ""):
            item["video_end"] = video_seconds(anchor["video_end"])
            if item["video_end"] <= item["video_time"]:
                raise ValueError("校准区间终点必须晚于校准点")
        normalized_anchors.append(item)
    normalized_events = []
    for event in events:
        if not isinstance(event, dict) or set(event) - {"id", "period", "match_time", "label", "before", "after"}:
            raise ValueError("事件需要 period、match_time、label，可选 before、after")
        period = event.get("period", "first")
        match_seconds(event.get("match_time", ""), period)
        label = event.get("label", "关键事件")
        if not isinstance(label, str) or not label.strip() or len(label) > 300:
            raise ValueError("事件说明需要 1～300 字符")
        item = {"id": f"e{len(normalized_events) + 1}", "period": period, "match_time": event["match_time"], "label": label.strip()}
        for field in ("before", "after"):
            if field in event:
                item[field] = number(event[field], f"事件{field}", 0, 600)
        normalized_events.append(item)
    # Stable sort allows interrupted recordings to define explicit spans. Never
    # resolve overlapping recording spans by guessing which clock is correct.
    normalized_anchors.sort(key=lambda a: a["video_time"])
    for i, anchor in enumerate(normalized_anchors):
        if i and anchor["video_time"] == normalized_anchors[i - 1]["video_time"]:
            raise ValueError("校准点录像起点不能重复")
        if i + 1 < len(normalized_anchors) and anchor.get("video_end", normalized_anchors[i + 1]["video_time"]) > normalized_anchors[i + 1]["video_time"]:
            raise ValueError("校准录像区间不能重叠")
    result = {"content": content, "format": form, "prompt": prompt.strip(), "count": count,
            "min_seconds": low, "max_seconds": high, "target_seconds": target,
            "anchors": normalized_anchors, "events": normalized_events,
            "event_before": number(value.get("event_before", 20), "事件前缓冲", 0, 600),
            "event_after": number(value.get("event_after", 40), "事件后缓冲", 0, 600)}
    if 'clock_mode' in value:
        result['clock_mode'] = value['clock_mode']
    return result


def public_options(options):
    """Remove generated event identities before revalidating a saved snapshot."""
    return {**options, "events": [{k: v for k, v in e.items() if k != "id"} for e in options.get("events", [])]}


def locate_events(options, duration):
    result = []
    anchors = options["anchors"]
    spans = []
    for i, anchor in enumerate(anchors):
        # A calibration point may be anywhere in a half, not just kickoff.
        # Infer that half's beginning, and use a new same-half anchor as the
        # start of a later uninterrupted span (e.g. a recording splice).
        inferred = (anchor["video_time"] if i and anchors[i - 1]["period"] == anchor["period"] else
                    max(0, anchor["video_time"] + PERIOD_BASE[anchor["period"]] * 60 - match_seconds(anchor["match_time"], anchor["period"])))
        spans.append(anchor.get("video_start", inferred))
    for i, anchor in enumerate(anchors):
        end = anchor.get("video_end", spans[i + 1] if i + 1 < len(spans) else duration)
        if not 0 <= spans[i] <= anchor["video_time"] < min(end, duration) or (i + 1 < len(spans) and end > spans[i + 1]):
            raise ValueError("校准点须在对应录像区间内，各区间不能重叠；请检查区间开始/结束和素材时长")
    for event in options["events"]:
        timestamp = match_seconds(event["match_time"], event["period"])
        positions = []
        for i, anchor in enumerate(anchors):
            if anchor["period"] != event["period"]:
                continue
            begin = spans[i]
            end = min(duration, anchor.get("video_end", spans[i + 1] if i + 1 < len(anchors) else duration))
            center = anchor["video_time"] + timestamp - match_seconds(anchor["match_time"], anchor["period"])
            if begin <= center < end:
                positions.append((center, begin, end))
        row = {**event, "confirmed": False, "waived": False, "start": None, "end": None}
        if len(positions) == 1:
            center, begin, end = positions[0]
            row.update(start=max(begin, center - event.get("before", options["event_before"])),
                       end=min(end, center + event.get("after", options["event_after"])),
                       status="located", note="按比赛时间校准定位，请预览后确认实际事件与完整范围")
            if row["end"] <= row["start"]:
                row.update(start=None, end=None, status="unresolved", note="事件缓冲为空，请调整")
        elif positions:
            row.update(status="ambiguous", note="对应多个录像区间，需手工确认")
        else:
            row.update(status="outside" if any(a["period"] == event["period"] for a in anchors) else "unresolved",
                       note="不在已校准录像区间内" if any(a["period"] == event["period"] for a in anchors) else "本比赛阶段尚未校准，请设置比赛与录像时间校准点")
        result.append(row)
    return result
