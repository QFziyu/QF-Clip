"""Reuse transcript timings to create bounded UTF-8 SRT and safely escaped ASS."""
from __future__ import annotations

import math
from pathlib import Path
import re
import uuid

from core.subtitle_styles import ass_color, resolve_subtitle_settings

_TIME = re.compile(r"^(\d{2,}):(\d{2}):(\d{2}),(\d{3})$")
_SENTENCE_END = set("。！？!?；;")


def _seconds(value: str) -> float:
    match = _TIME.fullmatch(value.strip())
    if not match:
        raise ValueError(f"无效 SRT 时间戳: {value}")
    h, m, s, ms = map(int, match.groups())
    if m >= 60 or s >= 60:
        raise ValueError(f"无效 SRT 时间戳: {value}")
    return h * 3600 + m * 60 + s + ms / 1000


def parse_srt(text: str, duration: float) -> list[dict]:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("字幕时长必须大于 0。")
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    cues, previous_start = [], -1.0
    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        if not lines or " --> " not in lines[0] or len(lines) < 2:
            raise ValueError("字幕每段需要序号、时间轴和文字。")
        parts = lines[0].split(" --> ")
        if len(parts) != 2:
            raise ValueError("字幕时间轴格式错误。")
        start, end = map(_seconds, parts)
        content = "\n".join(lines[1:]).strip()
        if start < 0 or end <= start or end > duration + 0.02 or start < previous_start or not content or "\x00" in content:
            raise ValueError("字幕必须按起点排序，满足 0 ≤ 起点 < 终点 ≤ 切片时长，且文字不能为空。")
        previous_start = start
        cues.append({"start": start, "end": min(end, duration), "text": content})
    return cues


def validate_srt(text: str, duration: float) -> bool:
    parse_srt(text, duration)
    return True


def _stamp(seconds: float, ass: bool = False) -> str:
    scale = 100 if ass else 1000
    total = max(0, round(seconds * scale))
    hours, remainder = divmod(total, 3600 * scale)
    minutes, remainder = divmod(remainder, 60 * scale)
    secs, fraction = divmod(remainder, scale)
    return f"{hours}:{minutes:02}:{secs:02}.{fraction:02}" if ass else f"{hours:02}:{minutes:02}:{secs:02},{fraction:03}"


def cues_srt(cues: list[dict]) -> str:
    return "\n\n".join(f"{i}\n{_stamp(c['start'])} --> {_stamp(c['end'])}\n{c['text']}"
                        for i, c in enumerate(cues, 1)) + ("\n" if cues else "")


def composition_cues(transcript: dict, segments: list[dict], max_chars: int = 18,
                     lengths: list[float] | None = None) -> list[dict]:
    """Map source captions onto a concatenated body, excluding any intro.

    The editor uses nominal cut lengths; rendering supplies measured frame
    durations. Both use the same source clipping and chronological offsets.
    """
    if lengths is not None and len(lengths) != len(segments):
        raise ValueError("字幕分段与视频分段数量不一致")
    cues, cursor = [], 0.0
    for i, segment in enumerate(segments):
        length = lengths[i] if lengths is not None else segment["end"] - segment["start"]
        if not math.isfinite(length) or length <= 0:
            raise ValueError("字幕分段时长必须大于零")
        for cue in transcript_cues(transcript, segment, max_chars):
            end = min(cue["end"], length)
            if end - cue["start"] >= .001:
                cues.append({**cue, "start": cursor + cue["start"], "end": cursor + end})
        cursor += length
    return cues


def _timed_characters(segment: dict):
    words = segment.get("words") or []
    usable = [w for w in words if w.get("word", w.get("text", "")).strip() and isinstance(w.get("start"), (int, float)) and isinstance(w.get("end"), (int, float)) and w["end"] > w["start"]]
    spans = usable or [{"word": segment.get("text", ""), "start": segment["start"], "end": segment["end"]}]
    for span in spans:
        text = str(span.get("word", span.get("text", "")))
        start, end = float(span["start"]), float(span["end"])
        if not text or not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        step = (end - start) / len(text)
        for index, char in enumerate(text):
            yield char, start + index * step, start + (index + 1) * step


def _wrap(text: str, width: int) -> str:
    lines = []
    for original in text.splitlines():
        remainder = original.strip()
        while len(remainder) > width:
            boundary = max([remainder.rfind(" ", 0, width + 1)] +
                           [i+1 for i,c in enumerate(remainder[:width]) if c in "，、：；。！？,;:!?"])
            if boundary < width // 2:
                boundary = width
            lines.append(remainder[:boundary].rstrip())
            remainder = remainder[boundary:].lstrip()
        if remainder:
            lines.append(remainder)
    return "\n".join(lines)


def transcript_cues(transcript: dict, clip: dict, max_chars: int = 18) -> list[dict]:
    start, end = float(clip["start"]), float(clip["end"])
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("切片时间范围无效。")
    cues = []
    max_chars = max(4, int(max_chars))
    # Filter model metadata before wrapping/clipping; a token may span several
    # words, segments or the edge of a selected cut. Keep every surviving
    # character's original timestamps and preserve the raw transcript cache.
    from core.subtitle_edit import marker_mask
    segments = transcript.get('segments',[])
    characters = [list(_timed_characters(segment)) for segment in segments]
    mask = marker_mask(''.join(c[0] for row in characters for c in row))
    cursor = 0
    for segment, row in zip(segments,characters):
        offset = cursor
        cursor += len(row)
        if float(segment["end"]) <= start or float(segment["start"]) >= end:
            continue
        group = []

        def flush():
            if not group:
                return
            content = "".join(item[0] for item in group).strip()
            cue_start, cue_end = max(start, group[0][1]) - start, min(end, group[-1][2]) - start
            if content and cue_end - cue_start >= 0.001:
                cues.append({"start": cue_start, "end": cue_end, "text": _wrap(content, max_chars)})
            group.clear()

        for index, (char, char_start, char_end) in enumerate(row):
            if mask[offset+index]:
                continue
            if char_end <= start or char_start >= end:
                continue
            if group and char_start - group[-1][2] > 0.65:
                flush()
            group.append((char, char_start, char_end))
            content_length = len("".join(item[0] for item in group).strip())
            if content_length >= (max_chars*2 if segment.get('semantic_sentence') else max_chars) or (char in _SENTENCE_END and content_length >= 5):
                flush()
        flush()
    return sorted(cues, key=lambda cue: (cue["start"], cue["end"]))


def _atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8-sig" if path.suffix.lower() == ".srt" else "utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _ass_text(text: str) -> str:
    # Full-width literal brackets and backslashes cannot become ASS override tags.
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", r"\N")


def write_subtitles(transcript: dict, clip: dict, srt_path: str | Path, ass_path: str | Path,
                    cfg: dict, orientation: str = "horizontal", edited_srt: str | None = None,
                    time_offset: float = 0) -> dict:
    if orientation not in {"horizontal", "vertical"}:
        raise ValueError("orientation 应为 horizontal 或 vertical。")
    settings = resolve_subtitle_settings(cfg.get("subtitle", {}))
    if isinstance(time_offset, bool) or not isinstance(time_offset, (int, float)) or not math.isfinite(time_offset) or time_offset < 0:
        raise ValueError("字幕时间偏移必须是大于或等于 0 的有限秒数。")
    time_offset = float(time_offset)
    duration = float(clip["end"]) - float(clip["start"])
    cues = parse_srt(edited_srt, duration) if edited_srt is not None else transcript_cues(transcript, clip, settings.get("max_chars", 18))
    cues = [{**cue, "start": cue["start"] + time_offset, "end": cue["end"] + time_offset} for cue in cues]
    srt_text = "\n\n".join(f"{i}\n{_stamp(cue['start'])} --> {_stamp(cue['end'])}\n{cue['text']}" for i, cue in enumerate(cues, 1))
    if srt_text:
        srt_text += "\n"
    render_cfg = cfg.get("render", {})
    width, height = render_cfg.get(orientation, [1080, 1920] if orientation == "vertical" else [1920, 1080])
    font = settings["font"]
    font_size = float(settings.get("vertical_font_size" if orientation == "vertical" else "font_size", settings.get("font_size", 25.5)))
    font_size = max(12, min(font_size, int(height) // 8))
    margin = int(settings.get("vertical_margin_v" if orientation == "vertical" else "margin_v", 150 if orientation == "vertical" else 60))
    margin = round(margin - width * settings.get('offset_y_width_ratio', 0.05))
    margin = max(4, min(margin, int(height) // 3))
    alignment = int(settings.get("alignment", 2))
    if alignment not in range(1, 10):
        alignment = 2
    primary_color = ass_color(settings["primary_color"])
    # In ASS BorderStyle 3 the outline becomes the rectangle around the text.
    # Give that rectangle the same configurable transparency as its background.
    outline_color = ass_color(settings["outline_color"], settings["back_alpha"] if settings["border_style"] == 3 else 0)
    back_color = ass_color(settings["back_color"], settings["back_alpha"])
    bold, italic = (-1 if settings["bold"] else 0), (-1 if settings["italic"] else 0)
    ass_text = ("[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
                f"PlayResX: {int(width)}\nPlayResY: {int(height)}\n\n[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                f"Style: Default,{font},{font_size:g},{primary_color},&H000000FF,{outline_color},{back_color},{bold},{italic},0,0,100,100,{settings['spacing']:g},0,{settings['border_style']},{settings['outline']:g},{settings['shadow']:g},{alignment},{max(24, int(width) // 20)},{max(24, int(width) // 20)},{margin},1\n\n"
                "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    for cue in cues:
        ass_text += f"Dialogue: 0,{_stamp(cue['start'], True)},{_stamp(cue['end'], True)},Default,,0,0,0,,{_ass_text(_wrap(cue['text'], int(settings.get('max_chars', 18))))}\n"
    srt_path, ass_path = Path(srt_path).resolve(), Path(ass_path).resolve()
    _atomic_text(srt_path, srt_text)
    _atomic_text(ass_path, ass_text)
    return {"srt": str(srt_path), "ass": str(ass_path), "cue_count": len(cues), "time_offset": time_offset}
