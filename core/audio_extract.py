"""Stream selected audio to a canonical-timeline mono WAV, without loading it in RAM."""
from __future__ import annotations

import logging
from pathlib import Path
import uuid

from core.video_input import run_media

LOG = logging.getLogger(__name__)


def audio_filter(origin: float, duration: float, sample_rate: int = 16000) -> str:
    """Keep absolute PTS until subtraction; first_pts=0 inserts any leading silence."""
    return (f"atrim=start={origin:.9f}:end={origin + duration:.9f},"
            f"asetpts=PTS-({origin:.9f})/TB,"
            f"aresample={sample_rate}:async=1:first_pts=0,apad,atrim=duration={duration:.9f}")


def extract_audio(meta: dict, destination: str | Path, cfg: dict) -> Path:
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}-{uuid.uuid4().hex}.wav")
    command = [str(cfg.get("ffmpeg", "ffmpeg")), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-copyts",
               "-i", str(meta["source"]), "-map", f"0:{int(meta['selected_audio'])}", "-vn",
               "-af", audio_filter(float(meta.get("timeline_origin", 0)), float(meta["duration"])),
               "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(temporary)]
    LOG.info("提取音轨 %s → %s", meta.get("audio_track", 0), destination)
    try:
        run_media(command)
        if not temporary.exists() or temporary.stat().st_size <= 44:
            raise RuntimeError("FFmpeg 未输出有效音频。")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
