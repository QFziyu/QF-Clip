"""Real media + real local tiny ASR + local HTTP stub + review + export acceptance.

No real match footage and no paid API are used. Downloads are never started here.
Outputs are isolated from production projects and saved under output/acceptance-runs.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import load_config
from core.pipeline import process_video, export_project
from core.utils import atomic_json
from core.video_input import run_media
from storage.database import Database
from scripts.verify_exports import verify


def run_acceptance(cfg, full_hd=True):
    run_dir = Path(cfg["output_dir"]) / "acceptance-runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "synthetic_media": True, "real_asr": True,
              "llm": "local HTTP stub; not a live model", "paid_requests": 0, "checks": {}, "run_dir": str(run_dir)}
    started = time.monotonic()
    server = None
    try:
        model = ROOT / "cache/models/tiny"
        speech = ROOT / "output/asr_smoke/synthetic_zh.wav"
        if not (model / "model.bin").is_file() or not speech.is_file():
            raise RuntimeError("先运行 scripts/smoke_asr.py --allow-cpu，准备 tiny 本地模型和合成语音；验收不会自动下载。")
        source = run_dir / "合成中文 75秒.mp4"
        run_media([cfg["ffmpeg"], "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                   "testsrc2=size=640x360:rate=25:duration=75", "-stream_loop", "-1", "-i", str(speech),
                   "-t", "75", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                   "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(source)])
        report["checks"]["source_video_created"] = True
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                payload = json.loads(request["messages"][-1]["content"])
                segments = payload["segments"]
                picks = []
                for target in (4, 30, 58):
                    segment = min(segments, key=lambda s: abs(s["start"] - target))
                    picks.append({"start": segment["start"], "end": segment["end"], "category": "commentary",
                                  "title": f"[验收 HTTP 夹具] {segment['text'][:18]}",
                                  "description": "本地服务返回的测试候选；原文字句来自实际 Whisper 推理。",
                                  "reason": "验证 HTTP 协议、证据约束与媒体时间轴。", "score": 60,
                                  "tags": ["合成语音验收"], "evidence": [segment["text"]]})
                received.append({"path": self.path, "model": request["model"], "candidates": len(picks)})
                body = json.dumps({"choices": [{"message": {"content": json.dumps({"clips": picks}, ensure_ascii=False)}}],
                                   "usage": {"prompt_tokens": 1000, "completion_tokens": 400}}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        task_cfg = copy.deepcopy(cfg)
        task_cfg.update(cache_dir=str(run_dir / "cache"), output_dir=str(run_dir / "exports"),
                        database=str(run_dir / "factory.sqlite3"))
        task_cfg["asr"].update(model=str(model), device="cpu", compute_type="int8", chunk_seconds=20, overlap_seconds=2)
        task_cfg["selection"].update(mode="semantic", audio_events=False, context_before=3, context_after=5, min_seconds=15)
        task_cfg["openai"].update(api_key="local-test-placeholder", model="acceptance-http-fixture",
                                   base_url=f"http://127.0.0.1:{server.server_port}/v1", max_calls=4, budget_usd=1)
        if not full_hd:
            task_cfg["render"].update(horizontal=[640, 360], vertical=[360, 640])
        process = process_video(source, task_cfg, allow_cpu=True)
        if process.get("errors") or process["candidate_count"] != 3:
            raise AssertionError(f"应生成 3 条完整预览: {process}")
        report.update(project_id=process["project_id"], candidate_count=process["candidate_count"])
        report["checks"]["real_asr_to_http_to_preview"] = True
        with Database(task_cfg["database"]) as db:
            clips = db.list_clips(process["project_id"])
            db.review_clip(clips[0]["id"], "approved", title="[验收] 人工修改的标题")
            db.review_clip(clips[1]["id"], "rejected")
            db.review_clip(clips[2]["id"], "approved")
        with Database(task_cfg["database"]) as db:
            statuses = [clip["status"] for clip in db.list_clips(process["project_id"])]
        if statuses != ["approved", "rejected", "approved"]:
            raise AssertionError("审批重启持久化失败")
        report["checks"]["three_test_reviews_persisted"] = True
        export = export_project(process["project_id"], task_cfg, ("horizontal", "vertical"))
        if export["errors"] or len(export["exports"]) != 4:
            raise AssertionError(f"仅应导出已通过的 2 条 × 2 画幅: {export['errors']}")
        manifest = Path(task_cfg["output_dir"]) / process["project_id"] / "export_manifest.json"
        verification = verify(manifest, task_cfg)
        atomic_json(manifest.with_name("verification.json"), verification)
        report["checks"]["four_exports_decode_and_subtitles"] = verification["checked"] == 4
        video_times = {item["video"]: Path(item["video"]).stat().st_mtime_ns for item in export["exports"]}
        requests_before = len(received)
        with patch("core.transcriber._make_model", side_effect=AssertionError("缓存复用不应加载模型")):
            process_video(source, task_cfg, allow_cpu=True)
        repeated = export_project(process["project_id"], task_cfg, ("horizontal", "vertical"))
        if repeated["errors"] or any(Path(p).stat().st_mtime_ns != stamp for p, stamp in video_times.items()):
            raise AssertionError("重复导出意外重新编码")
        if len(received) != requests_before:
            raise AssertionError("重复处理意外调用 HTTP API")
        report["checks"]["rerun_reuses_asr_api_and_exports"] = True
        report.update(status="passed", http_requests=received, export_count=4, export_manifest=str(manifest),
                      resolutions=[[1920, 1080], [1080, 1920]] if full_hd else [[640, 360], [360, 640]],
                      note="审批是隔离夹具中的测试操作；未使用真实赛事或线上 LLM。")
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        logging.exception("验收失败，保留所有阶段数据")
    finally:
        if server:
            server.shutdown()
            server.server_close()
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        atomic_json(run_dir / "report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="仅降低输出分辨率；仍执行真实 ASR 和编码")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = run_acceptance(load_config(), full_hd=not args.quick)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
