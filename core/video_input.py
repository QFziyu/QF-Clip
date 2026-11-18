"""Local inputs and FFprobe metadata. All public times are relative to video frame 0."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import subprocess
from fractions import Fraction

LOG = logging.getLogger(__name__)
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".flv", ".mov", ".webm", ".ts"}


def run_media(command: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run argv without a shell, preserving Windows spaces and Unicode paths."""
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到 {command[0]}，请安装 FFmpeg 并配置 ffmpeg/ffprobe 路径。") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"媒体命令超时: {command[0]}") from exc
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} 失败 (exit {result.returncode}): {result.stderr[-5000:]}")
    return result


def _number(value, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def discover_videos(path: str | Path, recursive: bool = False) -> list[Path]:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"输入不存在: {source}")
    if source.is_file():
        if source.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"不支持的视频格式: {source.suffix}")
        return [source]
    iterator = source.rglob("*") if recursive else source.glob("*")
    return sorted((p.resolve() for p in iterator if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS), key=str)


def probe_video(path: str | Path, cfg: dict, audio_track: int = 0, *, require_audio: bool = True) -> dict:
    """audio_track is the zero-based *audio* track number, not absolute stream index."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"视频不存在: {source}")
    command = [str(cfg.get("ffprobe", "ffprobe")), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)]
    try:
        data = json.loads(run_media(command, timeout=90).stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FFprobe 返回无效数据: {source}") from exc
    videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise ValueError("素材没有可解码的视频流。")
    audios = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audios and require_audio:
        raise ValueError("素材没有音轨，无法进行语音识别。")
    if isinstance(audio_track, bool) or not isinstance(audio_track, int) or audio_track < 0 or (audios and audio_track >= len(audios)):
        raise ValueError(f"音轨序号必须在 0 至 {len(audios) - 1} 之间。")
    video = videos[0]
    fmt = data.get("format", {})
    format_start = _number(fmt.get("start_time"))
    origin = _number(video.get("start_time"), format_start)
    # Stream start_time may differ from the first decoded frame (B frames/edit lists).
    frame_command = [str(cfg.get("ffprobe", "ffprobe")), "-v", "error", "-select_streams", str(video["index"]),
                     "-read_intervals", "%+2", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(source)]
    try:
        frames = json.loads(run_media(frame_command, timeout=60).stdout).get("frames", [])
        if frames:
            origin = _number(frames[0].get("best_effort_timestamp_time"), origin)
    except (RuntimeError, json.JSONDecodeError):
        LOG.warning("无法读取首帧时间戳，使用视频流起点。")
    stream_duration = _number(video.get("duration"))
    duration = stream_duration or max(0, _number(fmt.get("duration")) + format_start - origin)
    # Matroska DURATION tags describe the final absolute timestamp. In particular,
    # a file starting at PTS 5 and ending at PTS 13 contains 8 seconds, not 13.
    if not stream_duration:
        tagged_duration = video.get("tags", {}).get("DURATION", "")
        try:
            hours, minutes, seconds = tagged_duration.split(":")
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds) - origin
        except (ValueError, AttributeError):
            pass
        # Container duration can include longer audio tracks. Inspect only the last
        # few video packets so candidate ranges do not extend beyond video EOF.
        tail_command = [str(cfg.get("ffprobe", "ffprobe")), "-v", "error", "-select_streams", str(video["index"]),
                        "-read_intervals", f"{max(origin, origin + duration - 10):.6f}%", "-show_packets",
                        "-show_entries", "packet=pts_time,duration_time", "-of", "json", str(source)]
        try:
            packets = json.loads(run_media(tail_command, timeout=90).stdout).get("packets", [])
            ends = [_number(p.get("pts_time"), float("nan")) + _number(p.get("duration_time")) for p in packets]
            ends = [value for value in ends if math.isfinite(value)]
            if ends and max(ends) > origin:
                duration = max(ends) - origin
        except (RuntimeError, json.JSONDecodeError):
            LOG.warning("无法读取尾部视频时间戳，使用容器时长。")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("无法确定素材时长；文件可能损坏或尚未完成录制。")
    try:
        fps = float(Fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0"))
    except (ValueError, ZeroDivisionError):
        try:
            fps = float(Fraction(video.get("r_frame_rate") or "0"))
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    if width <= 0 or height <= 0:
        raise ValueError("素材视频分辨率无效。")
    tracks = [{"track": i, "index": int(s["index"]), "codec": s.get("codec_name"),
               "channels": s.get("channels"), "sample_rate": s.get("sample_rate"),
               "start_time": _number(s.get("start_time"), format_start),
               "offset_from_video": _number(s.get("start_time"), format_start) - origin,
               "language": s.get("tags", {}).get("language"), "title": s.get("tags", {}).get("title")}
              for i, s in enumerate(audios)]
    return {"source": str(source), "duration": duration, "width": width, "height": height,
            "fps": fps, "video_stream": int(video["index"]), "audio_streams": tracks,
            "audio_track": audio_track, "selected_audio": tracks[audio_track]["index"] if tracks else None,
            "timeline_origin": origin, "format_start": format_start,
            "timeline_basis": "seconds_after_first_video_frame", "raw_probe": data}
