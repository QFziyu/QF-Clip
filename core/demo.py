"""Synthetic integration fixture: real video/tone, artificial transcript, no API call."""
from __future__ import annotations

import copy
from pathlib import Path

from core.pipeline import process_video, export_project
from core.utils import atomic_json
from core.video_input import run_media
from storage.database import Database


def create_demo(cfg, approve=False, export=False, full_hd=False):
    root = Path(cfg["root"]) / "input" / "demo"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "模拟足球 测试90秒.mp4"
    transcript_path = root / "模拟转录.json"
    if not source.exists():
        run_media([cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=90",
                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=90",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart", str(source)])
    if not transcript_path.exists():
        sentences = [(5, 22, "这是模拟字幕。曼城的阵型正在变化，我们讨论边路如何制造空间。"),
                     (35, 52, "这是模拟字幕。讨论哈兰德的跑位，以及传球线路如何影响进攻选择。"),
                     (65, 82, "这是模拟字幕。中场总结需要完整观点，也需要保留前后的分析铺垫。")]
        segments = []
        for start, end, text in sentences:
            length = (end-start) / len(text)
            words = [{"start": start+i*length, "end": start+(i+1)*length, "word": char} for i, char in enumerate(text)]
            segments.append({"start": start, "end": end, "text": text, "words": words})
        atomic_json(transcript_path, {"mock": True, "note": "画面是测试图，声音是正弦音，文字是人为测试夹具，不是真实识别结果。", "segments": segments})
    demo_cfg = copy.deepcopy(cfg)
    if not full_hd:
        demo_cfg["render"].update(horizontal=[960, 540], vertical=[540, 960])
    result = process_video(source, demo_cfg, mock=True, transcript_path=transcript_path)
    if approve:
        with Database(cfg["database"]) as db:
            for clip in db.list_clips(result["project_id"])[:3]:
                db.review_clip(clip["id"], "approved")
    if export:
        result["export"] = export_project(result["project_id"], demo_cfg, ("horizontal", "vertical"))
    result["simulation_notice"] = "仅合成视频/模拟转录/模拟选片；--approve 是测试审批，不是真人审核。"
    atomic_json(Path(cfg["output_dir"]) / "demo_report.json", result)
    return result
