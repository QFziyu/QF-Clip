"""Accurate FFmpeg rendering with intact foreground, atomic files, and NVENC fallback."""
from __future__ import annotations

from functools import lru_cache
import json
import logging
import math
from pathlib import Path
import shutil
import tempfile

from core.audio_extract import audio_filter
from core.file_access import restore_inherited_access
from core.video_input import run_media

LOG = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _has_nvenc(ffmpeg: str) -> bool:
    try:
        return "h264_nvenc" in run_media([ffmpeg, "-hide_banner", "-encoders"], timeout=30).stdout
    except RuntimeError:
        return False


def _dimensions(cfg: dict, orientation: str, preview: bool) -> tuple[int, int]:
    render = cfg.get("render", {})
    if preview:
        width, height = render.get("preview", [640, 360])
        if orientation == "vertical":
            width, height = min(width, height), max(width, height)
    else:
        width, height = render.get(orientation, [1080, 1920] if orientation == "vertical" else [1920, 1080])
    width, height = int(width), int(height)
    if min(width, height) < 32 or width % 2 or height % 2:
        raise ValueError("输出宽高必须是至少 32 的偶数。")
    return width, height


def render_clip(meta: dict, clip: dict, destination: str | Path, cfg: dict,
                orientation: str = "horizontal", preview: bool = False,
                subtitle_path: str | Path | None = None) -> Path:
    """clip.start/end already include buffers and use the canonical source video clock.

    Every result also has a .timeline.json sidecar recording the exact mapping.
    Subtitles must already be relative to the clip, usually the ASS from write_subtitles.
    """
    if orientation not in {"horizontal", "vertical"}:
        raise ValueError("orientation 应为 horizontal 或 vertical。")
    start, end = float(clip["start"]), float(clip["end"])
    duration = end - start
    if not all(map(math.isfinite, (start, end))) or start < 0 or end <= start or end > float(meta["duration"]) + 0.02:
        raise ValueError("切片范围必须满足 0 ≤ start < end ≤ 视频时长。")
    end = min(end, float(meta["duration"]))
    duration = end - start
    origin = float(meta.get("timeline_origin", 0))
    absolute_start, absolute_end = origin + start, origin + end
    width, height = _dimensions(cfg, orientation, preview)
    render = cfg.get("render", {})
    ffmpeg = str(cfg.get("ffmpeg", "ffmpeg"))
    requested_encoder = render.get("encoder", "auto")
    if requested_encoder not in {"auto", "h264_nvenc", "libx264"}:
        raise ValueError("编码器应为 auto、h264_nvenc 或 libx264。")
    encoder = "h264_nvenc" if requested_encoder == "h264_nvenc" or (requested_encoder == "auto" and _has_nvenc(ffmpeg)) else "libx264"
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fps = float(render.get("fps", 30))
    if not math.isfinite(fps) or not 1 <= fps <= 120:
        raise ValueError("输出帧率必须位于 1–120。")
    with tempfile.TemporaryDirectory(prefix=".render-", dir=destination.parent) as temp_dir:
        temp_root = Path(temp_dir)
        temporary = temp_root / "output.mp4"
        # A short ASCII relative subtitle name avoids FFmpeg filter escaping of C:\,
        # Chinese paths, apostrophes, brackets, and commas on Windows.
        subtitle_filter = ""
        if subtitle_path:
            original_subtitle = Path(subtitle_path).resolve()
            if not original_subtitle.is_file():
                raise FileNotFoundError(f"字幕文件不存在: {original_subtitle}")
            suffix = original_subtitle.suffix.lower()
            if suffix not in {".ass", ".srt"}:
                raise ValueError("硬字幕文件必须是 ASS 或 SRT。")
            subtitle_name = "captions" + suffix
            shutil.copyfile(original_subtitle, temp_root / subtitle_name)
            subtitle_filter = f",subtitles=filename={subtitle_name}"
        video = f"[0:{int(meta.get('video_stream', 0))}]trim=start={absolute_start:.9f}:end={absolute_end:.9f},setpts=PTS-({absolute_start:.9f})/TB[vcut];"
        if orientation == "vertical":
            video += (f"[vcut]split[background][foreground];"
                      f"[background]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},boxblur=luma_radius=min(w\\,h)/30:luma_power=1:chroma_radius=min(cw\\,ch)/30:chroma_power=1[blur];"
                      f"[foreground]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2[front];"
                      f"[blur][front]overlay=(W-w)/2:(H-h)/2,setsar=1{subtitle_filter},format=yuv420p[vout];")
        else:
            video += (f"[vcut]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                      f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1{subtitle_filter},format=yuv420p[vout];")
        filters = video + f"[0:{int(meta['selected_audio'])}]" + audio_filter(absolute_start, duration, 48000) + "[aout]"
        base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-copyts"]
        # Input -ss is relative to format start; -copyts retains source PTS for the
        # trim filters. Leave two seconds of preroll for audio/video decoders.
        seek = absolute_start - float(meta.get("format_start", 0)) - 2.0
        if seek > 0:
            base += ["-ss", f"{seek:.9f}"]
        base += ["-i", str(meta["source"]), "-filter_complex", filters, "-map", "[vout]", "-map", "[aout]", "-t", f"{duration:.9f}", "-r", str(fps)]
        bitrate = str(render.get("preview_bitrate", "700k") if preview else render.get("bitrate", "6M"))

        def encode(chosen: str):
            preset = "p4" if chosen == "h264_nvenc" else ("veryfast" if preview else "medium")
            command = base + ["-c:v", chosen, "-preset", preset, "-b:v", bitrate,
                              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k" if preview else "192k",
                              "-ac", "2", "-movflags", "+faststart", str(temporary)]
            run_media(command, cwd=temp_root)

        LOG.info("渲染 %s %.2f–%.2fs (%s/%s)", destination.name, start, end, orientation, encoder)
        try:
            encode(encoder)
        except RuntimeError:
            if encoder != "h264_nvenc":
                raise
            LOG.warning("NVENC 渲染失败，回退 libx264 软件编码。", exc_info=True)
            encoder = "libx264"
            temporary.unlink(missing_ok=True)
            encode(encoder)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("FFmpeg 没有生成视频。")
        mapping = {"source": str(meta["source"]), "output": str(destination), "start": start, "end": end,
                   "duration": duration, "source_pts_start": absolute_start, "source_pts_end": absolute_end,
                   "timeline_origin": origin, "timeline_basis": "seconds_after_first_video_frame",
                   "selected_audio": meta["selected_audio"], "orientation": orientation, "preview": preview,
                   "encoder": encoder, "width": width, "height": height}
        mapping_temp = temp_root / "timeline.json"
        mapping_temp.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
        restore_inherited_access(destination)
        mapping_temp.replace(destination.with_suffix(".timeline.json"))
        restore_inherited_access(destination.with_suffix(".timeline.json"))
    return destination
