"""Exercise real detached process -> synthetic review -> export in an isolated DB.

Uses a generated test pattern, test tone and labelled fixture transcript; no API or
Whisper model is used. It never changes approvals in the normal project database.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.jobs import get_job, submit_job, tail_job_log
from core.utils import atomic_json
from core.video_input import run_media
from storage.database import Database
from scripts.verify_exports import verify


def await_job(job, cfg, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = get_job(job["id"], cfg)
        if current["status"] in ("completed", "failed", "interrupted"):
            if current["status"] != "completed":
                raise RuntimeError(f"任务 {job['id']} {current['status']}: {current['error']}\n{tail_job_log(current)}")
            if current["worker_pid"] == os.getpid():
                raise AssertionError("任务没有在独立后台进程中执行")
            return current
        time.sleep(0.2)
    raise TimeoutError(f"后台任务超时: {job['id']}")


def main():
    import yaml

    output = ROOT / "output/queue_smoke"
    run = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"_{os.getpid()}")
    run.mkdir(parents=True, exist_ok=True)
    config_path = run / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "database": str(run / "queue.sqlite3"), "cache_dir": str(run / "cache"), "output_dir": str(run / "exports"),
        "render": {"horizontal": [640, 360], "vertical": [360, 640], "preview": [320, 180],
                   "encoder": "libx264", "bitrate": "600k", "preview_bitrate": "200k", "fps": 25},
        "selection": {"min_seconds": 6, "max_seconds": 12, "context_before": 1, "context_after": 1,
                      "audio_events": False}, "subtitle": {"font_size": 20, "margin_v": 16}
    }, allow_unicode=True), encoding="utf-8")
    # Explicit environment values also outrank .env, and are inherited by the
    # detached worker. Removing shell overrides alone would expose .env defaults.
    os.environ.update(OUTPUT_DIR=str(run / "exports"), CACHE_DIR=str(run / "cache"),
                      DATABASE_PATH=str(run / "queue.sqlite3"))
    cfg = load_config(config_path)
    if Path(cfg["database"]).resolve().parent != run.resolve():
        raise AssertionError("验收数据库没有隔离，拒绝执行")
    source = run / "队列 模拟录像.mp4"
    transcript = run / "模拟转录.json"
    run_media([cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=45",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=45",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "33", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-shortest", str(source)])
    atomic_json(transcript, {"mock": True, "segments": [
        {"start": 3, "end": 9, "text": "这是模拟字幕，讨论曼城的边路进攻。"},
        {"start": 18, "end": 24, "text": "这是模拟字幕，讨论哈兰德的跑位。"},
        {"start": 33, "end": 39, "text": "这是模拟字幕，完整总结中场战术。"}
    ]})
    started = time.monotonic()
    process = await_job(submit_job("process", {"path": str(source), "mock": True,
        "transcript_path": str(transcript)}, cfg), cfg)
    project_id = process["result"]["project_id"]
    with Database(cfg["database"]) as db:
        clips = db.list_clips(project_id)
        if len(clips) != 3 or any(not Path(clip["preview_path"]).is_file() for clip in clips):
            raise AssertionError("应该生成三条可播放的候选预览")
        for clip in clips:
            db.review_clip(clip["id"], "approved")  # Explicitly synthetic acceptance decisions.
    export = await_job(submit_job("export", {"project_id": project_id,
        "orientations": ["horizontal", "vertical"]}, cfg), cfg)
    manifest = Path(cfg["output_dir"]) / project_id / "export_manifest.json"
    checked = verify(manifest, cfg)
    if checked["checked"] != 6:
        raise AssertionError("应该输出六个横竖屏视频")
    report = {"status": "passed", "simulation": True,
        "notice": "合成测试图 / 正弦音 / 模拟转录与选片 / 自动测试审批；不代表真实比赛或线上 LLM 验收。",
        "created_at": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": round(time.monotonic() - started, 2),
        "database": cfg["database"], "process_job": {key: process[key] for key in ("id", "status", "worker_pid", "attempt", "log_path")},
        "export_job": {key: export[key] for key in ("id", "status", "worker_pid", "attempt", "log_path")},
        "project_id": project_id, "candidate_count": len(clips), "approved_count": 3,
        "export_count": checked["checked"], "verification": checked, "config_path": str(config_path)}
    atomic_json(output / "report.json", report)
    print(json.dumps({"status": "passed", "candidates": 3, "exports": 6,
        "elapsed_seconds": report["elapsed_seconds"], "report": str(output / "report.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
