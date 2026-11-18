"""Prove that a cleanup retained every snapshotted MP4, including renamed exports.

Examples:
  python scripts/verify_preserved_media.py snapshot --root output --output output/cleanup/before.json
  python scripts/verify_preserved_media.py verify --before output/cleanup/before.json --journal output/cleanup/sync.json --output output/cleanup/verification.json --decode-exports

Only the JSON report is written. Videos are read in bounded chunks and never
renamed, rewritten, deleted, or reencoded by this tool.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.utils import atomic_json


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def is_formal_export(path: Path, relative: Path | None = None) -> bool:
    """Recognize current metadata packages and the older exports directory layout."""
    if relative is not None and "exports" in relative.parts:
        return True
    metadata_path = path.parent / "metadata.json"
    if not metadata_path.is_file():
        return False
    metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    if not isinstance(metadata, dict) or not isinstance(metadata.get("video"), str):
        return False
    recorded = Path(metadata["video"])
    if not recorded.is_absolute():
        recorded = metadata_path.parent / recorded
    return _key(path) == _key(recorded)


def snapshot(directory: str | Path) -> dict:
    directory = Path(directory).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("快照根路径必须是目录")
    files = []
    for path in sorted(directory.rglob("*"), key=lambda item: str(item).lower()):
        if not path.is_file() or path.suffix.lower() != ".mp4":
            continue
        stat = path.stat()
        digest = sha256(path)
        current = path.stat()
        if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise RuntimeError(f"建立快照时视频发生变化: {path}")
        relative = path.relative_to(directory)
        files.append({"path": str(path.resolve()), "relative_path": str(relative),
                      "size": stat.st_size, "sha256": digest,
                      "formal_export": is_formal_export(path, relative)})
    return {"schema": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "root": str(directory), "count": len(files),
            "total_bytes": sum(item["size"] for item in files),
            "formal_export_count": sum(item["formal_export"] for item in files), "files": files}


def _moves(journal: dict | None) -> dict[str, str]:
    mapping = {}
    if journal is None:
        return mapping
    for move in journal.get("moves", []):
        old, new = move.get("old_path"), move.get("new_path")
        if not old or not new:
            raise ValueError("迁移记录必须包含 old_path 和 new_path")
        old_key = _key(old)
        if old_key in mapping and _key(mapping[old_key]) != _key(new):
            raise ValueError(f"迁移记录存在冲突: {old}")
        mapping[old_key] = str(Path(new).resolve())
    return mapping


def _resolve_move(path: str, mapping: dict[str, str]) -> Path:
    current = str(Path(path).resolve())
    seen = set()
    while _key(current) in mapping:
        key = _key(current)
        following = mapping[key]
        if _key(following) == key:
            break
        if key in seen:
            raise ValueError(f"迁移记录形成循环: {path}")
        seen.add(key)
        current = following
    return Path(current)


def check_playable(path: Path, ffmpeg: str, ffprobe: str, timeout: float = 600) -> dict:
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,width,height", "-of", "json", str(path)],
        shell=False, capture_output=True, text=True, errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if probe.returncode:
        raise RuntimeError(f"FFprobe失败: {probe.stderr.strip()[:2000]}")
    data = json.loads(probe.stdout)
    video = next((stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"), None)
    duration = float(data.get("format", {}).get("duration", 0))
    if not video or not math.isfinite(duration) or duration <= 0:
        raise ValueError("成品缺少视频轨或有效时长")
    decode = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
         "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
        shell=False, capture_output=True, text=True, errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if decode.returncode:
        raise RuntimeError(f"FFmpeg完整解码失败: {decode.stderr.strip()[:2000]}")
    return {"duration": duration, "width": video.get("width"), "height": video.get("height"),
            "video_codec": video.get("codec_name"), "probe": "passed", "decode": "passed"}


def verify(before: dict, journal: dict | None = None, *, decode_exports: bool = False,
           ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", media_timeout: float = 600) -> dict:
    files = before.get("files", [])
    if not isinstance(files, list) or before.get("count") != len(files):
        raise ValueError("保全快照的文件数量无效")
    if len({_key(item["path"]) for item in files}) != len(files):
        raise ValueError("保全快照包含重复路径")
    if before.get("total_bytes") != sum(item["size"] for item in files):
        raise ValueError("保全快照的总字节数无效")
    mapping = _moves(journal)
    results, destinations = [], set()
    for item in files:
        path = _resolve_move(item["path"], mapping)
        result = {"old_path": item["path"], "path": str(path), "expected_size": item["size"],
                  "expected_sha256": item["sha256"], "passed": False}
        try:
            destination_key = _key(path)
            if destination_key in destinations:
                raise ValueError("多个原始视频被映射到同一个目标，无法证明逐个保留")
            destinations.add(destination_key)
            if not path.is_file():
                raise FileNotFoundError(f"保全视频不存在: {path}")
            stat = path.stat()
            result["size"] = stat.st_size
            if stat.st_size != item["size"]:
                raise ValueError("视频字节数改变")
            result["sha256"] = sha256(path)
            if result["sha256"] != item["sha256"]:
                raise ValueError("视频SHA-256改变")
            final_stat = path.stat()
            if (final_stat.st_size, final_stat.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError("验证过程中视频发生变化")
            formal_export = item.get("formal_export", False) or is_formal_export(
                path, Path(item.get("relative_path", "")))
            if decode_exports and formal_export:
                result["media"] = check_playable(path, ffmpeg, ffprobe, media_timeout)
            result["passed"] = True
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            result["error"] = str(error)
        results.append(result)
    return {"schema": 1, "verified_at": datetime.now(timezone.utc).isoformat(),
            "passed": all(item["passed"] for item in results), "count": len(results),
            "passed_count": sum(item["passed"] for item in results),
            "total_bytes": sum(item["size"] for item in files),
            "decoded_count": sum("media" in item for item in results), "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("snapshot", help="建立只读视频保全快照，不覆盖已有快照")
    capture.add_argument("--root", required=True)
    capture.add_argument("--output", required=True)
    check = commands.add_parser("verify", help="根据旧新路径迁移记录验证视频保全")
    check.add_argument("--before", required=True)
    check.add_argument("--journal")
    check.add_argument("--output", required=True)
    check.add_argument("--decode-exports", action="store_true")
    check.add_argument("--ffmpeg")
    check.add_argument("--ffprobe")
    check.add_argument("--media-timeout", type=float, default=600)
    args = parser.parse_args()
    if Path(args.output).suffix.lower() != ".json":
        parser.error("报告必须使用.json文件，不能覆盖视频")
    if args.command == "verify" and any(
            _key(args.output) == _key(source) for source in (args.before, args.journal) if source):
        parser.error("验证报告不能覆盖保全快照或迁移记录")
    if args.command == "snapshot":
        if Path(args.output).exists():
            parser.error("快照已存在；为保留原始证据不允许覆盖")
        report = snapshot(args.root)
    else:
        if not math.isfinite(args.media_timeout) or args.media_timeout <= 0:
            parser.error("--media-timeout必须为有限正数")
        before = json.loads(Path(args.before).read_text(encoding="utf-8-sig"))
        journal = json.loads(Path(args.journal).read_text(encoding="utf-8-sig")) if args.journal else None
        ffmpeg, ffprobe = args.ffmpeg, args.ffprobe
        if args.decode_exports and (not ffmpeg or not ffprobe):
            from core.config import load_config
            cfg = load_config()
            ffmpeg, ffprobe = ffmpeg or cfg["ffmpeg"], ffprobe or cfg["ffprobe"]
        report = verify(before, journal, decode_exports=args.decode_exports,
                        ffmpeg=ffmpeg or "ffmpeg", ffprobe=ffprobe or "ffprobe",
                        media_timeout=args.media_timeout)
    atomic_json(args.output, report)
    print(json.dumps({key: report[key] for key in ("passed", "count", "total_bytes", "decoded_count") if key in report}, ensure_ascii=False))
    return 0 if report.get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
