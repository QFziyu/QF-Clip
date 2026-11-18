"""Optional actual 3-hour synthetic input smoke test; no real ASR or paid API."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import load_config
from core.pipeline import process_video
from core.editing import export_editing_plan, create_editing_plan
from core.utils import atomic_json
from core.video_input import run_media
from storage.plans import PlanStore


def main():
    cfg = load_config()
    report_dir = ROOT / "output/modes_upgrade"
    report_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="fcf-long-synthetic-", dir=report_dir) as temporary:
        directory = Path(temporary)
        cfg = copy.deepcopy(cfg)
        cfg.update(database=str(directory / "db.sqlite3"), output_dir=str(directory / "output"), cache_dir=str(directory / "cache"))
        cfg["render"].update(encoder="libx264", horizontal=[128, 72], preview=[128, 72], fps=2, bitrate="100k", burn_subtitles=False)
        cfg["selection"].update(mode="semantic", context_before=0, context_after=0, audio_events=False)
        source = directory / "explicit-synthetic-three-hours.mp4"
        run_media([cfg["ffmpeg"], "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                   "color=c=0x223344:size=128x72:rate=1:duration=10800", "-f", "lavfi", "-i",
                   "anullsrc=sample_rate=16000:channel_layout=mono", "-t", "10800", "-c:v", "libx264",
                   "-preset", "ultrafast", "-c:a", "aac", "-b:a", "16k", str(source)])
        transcript = directory / "explicit-artificial-transcript.json"
        atomic_json(transcript, {"provenance": "synthetic fixture, not actual ASR", "segments": [
            {"start": float(i), "end": float(i + 20), "text": f"合成测试讨论足球观点，第 {i} 秒。"} for i in range(0, 10800, 30)]})
        options = {"content": "conversation", "format": "continuous", "count": 2,
                   "min_seconds": 300, "target_seconds": 600, "max_seconds": 900}
        first = process_video(source, cfg, mock=True, transcript_path=transcript, skip_render=True, editing_options=options)
        with PlanStore(cfg["database"]) as store:
            plan = store.get_plan(first["plan_id"])
            assert len(plan["drafts"]) == 2
            store.edit_plan(plan["id"], 0, {d["id"]: {"status": "approved"} for d in plan["drafts"]})
        result = export_editing_plan(first["plan_id"], cfg, export_options={"burn_subtitles": False})
        assert not result["errors"] and len(result["exports"]) == 2
        for record in result["exports"]:
            run_media([cfg["ffmpeg"], "-v", "error", "-i", record["video"], "-f", "null", "-"])
        resumed = create_editing_plan(first["project_id"], cfg, options, mock=True)
        assert resumed["plan_id"] == first["plan_id"]
        report = {"passed": True, "input_seconds": 10800, "source_bytes": source.stat().st_size,
                  "transcript_segments": 360, "outputs_decoded": len(result["exports"]),
                  "output_durations": [r["duration"] for r in result["exports"]],
                  "plan_cache_reused": True, "elapsed_seconds": round(time.monotonic() - started, 2),
                  "scope": "Actual FFmpeg 3-hour synthetic input/extraction, imported artificial transcript, explicit mock selection, two continuous outputs fully decoded. Not real ASR/LLM or real broadcast quality validation. Temporary media removed."}
        atomic_json(report_dir / "long_synthetic_report.json", report)
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
