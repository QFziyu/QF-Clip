"""Normalize a user-selected opening video and prepend it to a rendered clip."""
from __future__ import annotations

from fractions import Fraction
import json
import logging
import math
from pathlib import Path
import tempfile

from core.audio_extract import audio_filter
from core.clip_renderer import _dimensions, _has_nvenc
from core.file_access import restore_inherited_access
from core.utils import file_signature
from core.video_input import run_media

LOG = logging.getLogger(__name__)


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def probe_intro(path: str | Path, cfg: dict) -> dict:
    """Probe a video without requiring an audio stream.

    All durations are relative to its first decoded video frame. Audio keeps its
    original offset from that frame; delayed audio therefore produces silence,
    rather than moving out of sync during concatenation.
    """
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"片头或成品视频不存在: {source}")
    ffprobe = str(cfg.get("ffprobe", "ffprobe"))
    try:
        data = json.loads(run_media([ffprobe, "-v", "error", "-show_format", "-show_streams",
                                    "-of", "json", str(source)], timeout=90).stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"无法读取视频信息: {source}") from error
    videos = [stream for stream in data.get("streams", [])
              if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
    if not videos:
        raise ValueError("片头必须包含可解码的视频画面。")
    video = videos[0]
    fmt = data.get("format", {})
    format_start = _number(fmt.get("start_time"))
    origin = _number(video.get("start_time"), format_start)
    video_stream = int(video["index"])
    try:
        frames = json.loads(run_media([
            ffprobe, "-v", "error", "-select_streams", str(video_stream), "-read_intervals", "%+2",
            "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(source),
        ], timeout=60).stdout).get("frames", [])
        if frames:
            origin = _number(frames[0].get("best_effort_timestamp_time"), origin)
    except (RuntimeError, json.JSONDecodeError):
        LOG.warning("无法读取片头首帧时间戳，使用视频流起点。")
    stream_duration = _number(video.get("duration"))
    duration = stream_duration or max(0, _number(fmt.get("duration")) + format_start - origin)
    if not stream_duration:
        try:
            hours, minutes, seconds = video.get("tags", {}).get("DURATION", "").split(":")
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds) - origin
        except (ValueError, AttributeError):
            pass
        # Only inspect the tail, so a long user-selected file is never loaded in
        # memory. Container duration may include an audio stream longer than video.
        try:
            packets = json.loads(run_media([
                ffprobe, "-v", "error", "-select_streams", str(video_stream), "-read_intervals",
                f"{max(origin, origin + duration - 10):.6f}%", "-show_packets",
                "-show_entries", "packet=pts_time,duration_time", "-of", "json", str(source),
            ], timeout=90).stdout).get("packets", [])
            ends = [_number(packet.get("pts_time"), float("nan")) + _number(packet.get("duration_time"))
                    for packet in packets]
            ends = [end for end in ends if math.isfinite(end)]
            if ends and max(ends) > origin:
                duration = max(ends) - origin
        except (RuntimeError, json.JSONDecodeError):
            LOG.warning("无法读取片头尾帧时间戳，使用容器时长。")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("无法确定片头时长，文件可能损坏或尚未录制完成。")
    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    if min(width, height) <= 0:
        raise ValueError("片头视频分辨率无效。")
    try:
        fps = float(Fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0"))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    audios = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"]
    tracks = [{"index": int(stream["index"]), "channels": stream.get("channels"),
               "sample_rate": stream.get("sample_rate"),
               "start_time": _number(stream.get("start_time"), format_start),
               "offset_from_video": _number(stream.get("start_time"), format_start) - origin}
              for stream in audios]
    return {"source": str(source), "duration": duration, "width": width, "height": height, "fps": fps,
            "timeline_origin": origin, "format_start": format_start, "video_stream": video_stream,
            "selected_audio": tracks[0]["index"] if tracks else None, "audio_streams": tracks,
            "signature": file_signature(source)}


def _video_filter(index: int, meta: dict, width: int, height: int, fps: float,
                  frames: int, orientation: str) -> str:
    origin = meta["timeline_origin"]
    duration = meta["duration"]
    quantized = frames / fps
    # tpad + frame trim gives an exact number of frames even with a non-integral
    # source duration/rate. This measured boundary is also the subtitle offset.
    graph = (f"[{index}:{meta['video_stream']}]trim=start={origin:.9f}:end={origin + duration:.9f},"
             f"setpts=PTS-({origin:.9f})/TB,fps={fps:.12g},"
             f"tpad=stop_mode=clone:stop_duration={quantized:.9f},trim=end_frame={frames},"
             f"settb=AVTB,setpts=N/({fps:.12g}*TB)[cut{index}];")
    if orientation == "vertical":
        graph += (f"[cut{index}]split[bg{index}][fg{index}];"
                  f"[bg{index}]scale={width}:{height}:force_original_aspect_ratio=increase,"
                  f"crop={width}:{height},boxblur=luma_radius=min(w\\,h)/30:luma_power=1:"
                  f"chroma_radius=min(cw\\,ch)/30:chroma_power=1[blur{index}];"
                  f"[fg{index}]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2[front{index}];"
                  f"[blur{index}][front{index}]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v{index}];")
    else:
        graph += (f"[cut{index}]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[v{index}];")
    return graph


def prepend_intro(body_path: str | Path, intro_path: str | Path, destination: str | Path,
                  cfg: dict, orientation: str = "horizontal") -> dict:
    """Prepend an opening video to a completed body; captions are left intact.

    Both inputs are decoded incrementally by FFmpeg, normalized to the selected
    layout, and re-encoded once. Return the exact normalized intro/body boundary
    for external subtitle and source-timeline mapping. No source is modified.
    """
    if orientation not in {"horizontal", "vertical"}:
        raise ValueError("orientation 应为 horizontal 或 vertical。")
    intro, body = probe_intro(intro_path, cfg), probe_intro(body_path, cfg)
    destination = Path(destination).expanduser().resolve()
    if destination in {Path(intro["source"]), Path(body["source"])}:
        raise ValueError("片头输出路径不能覆盖片头或原成品。")
    if destination.suffix.lower() != ".mp4":
        raise ValueError("片头合成输出必须是 MP4 文件。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    render = cfg.get("render", {})
    fps = render.get("fps", 30)
    if isinstance(fps, bool) or not isinstance(fps, (float, int)) or not math.isfinite(fps) or not 1 <= fps <= 120:
        raise ValueError("输出帧率必须位于 1–120。")
    width, height = _dimensions(cfg, orientation, False)
    requested = render.get("encoder", "auto")
    if requested not in {"auto", "h264_nvenc", "libx264"}:
        raise ValueError("编码器应为 auto、h264_nvenc 或 libx264。")
    ffmpeg = str(cfg.get("ffmpeg", "ffmpeg"))
    encoder = "h264_nvenc" if requested == "h264_nvenc" or (requested == "auto" and _has_nvenc(ffmpeg)) else "libx264"
    parts, boundaries = [], []
    for index, meta in enumerate((intro, body)):
        count = max(1, math.ceil(meta["duration"] * fps - 1e-7))
        duration = count / fps
        boundaries.append(duration)
        parts.append(_video_filter(index, meta, width, height, fps, count, orientation))
        if meta["selected_audio"] is None:
            parts.append(f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[a{index}];")
        else:
            parts.append(f"[{index}:{meta['selected_audio']}]" + audio_filter(meta["timeline_origin"], duration, 48000)
                         + f",aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{index}];")
    parts.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]")
    total = sum(boundaries)
    with tempfile.TemporaryDirectory(prefix=".intro-", dir=destination.parent) as temporary_dir:
        temporary = Path(temporary_dir) / "output.mp4"
        base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-copyts",
                "-i", intro["source"], "-i", body["source"], "-filter_complex_threads", "2",
                "-filter_complex", "".join(parts), "-map", "[vout]", "-map", "[aout]",
                "-t", f"{total:.9f}", "-r", f"{fps:.12g}"]

        def encode(chosen):
            run_media(base + ["-c:v", chosen, "-preset", "p4" if chosen == "h264_nvenc" else "medium",
                              "-b:v", str(render.get("bitrate", "6M")), "-pix_fmt", "yuv420p",
                              "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
                              "-movflags", "+faststart", str(temporary)])

        LOG.info("添加片头 %s → %s (%s/%s)", Path(intro["source"]).name, destination.name, orientation, encoder)
        try:
            encode(encoder)
        except RuntimeError:
            if encoder != "h264_nvenc":
                raise
            LOG.warning("片头合成 NVENC 失败，回退 libx264 软件编码。", exc_info=True)
            encoder = "libx264"
            temporary.unlink(missing_ok=True)
            encode(encoder)
        if not temporary.is_file() or not temporary.stat().st_size:
            raise RuntimeError("FFmpeg 没有生成片头合成视频。")
        checked = probe_intro(temporary, cfg)
        if abs(checked["duration"] - total) > 1 / fps + 0.002:
            raise RuntimeError("片头合成结果时长异常，未替换成品。")
        temporary.replace(destination)
        restore_inherited_access(destination)
    return {"intro_duration": boundaries[0], "body_duration": boundaries[1], "duration": total,
            "intro_source": intro["source"], "intro_signature": intro["signature"],
            "body_source": body["source"], "output": str(destination),
            "fps": fps, "width": width, "height": height, "encoder": encoder}
