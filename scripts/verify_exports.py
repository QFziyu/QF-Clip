"""Decode a current export manifest and check dimensions, durations and subtitle bounds."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import load_config
from core.subtitle import validate_srt
from core.utils import atomic_json
from core.video_input import probe_video, run_media


def verify(manifest, cfg):
    data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if data.get("errors"):
        raise ValueError("导出清单含失败记录")
    results = []
    for item in data["exports"]:
        path = Path(item["video"])
        meta = probe_video(path, cfg)
        expected = item["end"] - item["start"]
        if abs(meta["duration"] - expected) > 0.15:
            raise ValueError(f"成品时长不符: {path}: {meta['duration']} != {expected}")
        ratio = meta["width"] / meta["height"]
        expected_ratio = 16/9 if item["orientation"] == "horizontal" else 9/16
        if abs(ratio-expected_ratio) > 0.005:
            raise ValueError(f"成品画幅不符: {path}")
        validate_srt(Path(item["subtitles"]["srt"]).read_text(encoding="utf-8-sig"), expected)
        run_media([cfg["ffmpeg"], "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"])
        mapping = json.loads(path.with_suffix(".timeline.json").read_text(encoding="utf-8"))
        results.append({"video": str(path), "duration": meta["duration"],
                        "resolution": [meta["width"], meta["height"]], "decode": "passed", "timeline": mapping})
    return {"checked": len(results), "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    args = parser.parse_args()
    result = verify(args.manifest, load_config())
    atomic_json(Path(args.manifest).with_name("verification.json"), result)
    print(json.dumps({"checked": result["checked"], "decode": "all passed"}, ensure_ascii=False))
