from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from contextlib import contextmanager
from pathlib import Path


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def safe_filename_stem(title, fallback="clip", max_units=120) -> str:
    """Keep readable Unicode titles within Windows' UTF-16 filename limits."""
    if max_units < 16:
        raise ValueError("输出目录路径过长，请将 OUTPUT_DIR 改为更短的路径后重试。")

    def clean(value):
        value = unicodedata.normalize("NFC", str(value or ""))
        value = "".join("_" if c in '<>:"/\\|?*' or unicodedata.category(c) in {"Cc", "Cs"}
                        else c for c in value)
        value = re.sub(r"_+", "_", value).strip().rstrip(". ")
        # Windows treats reserved device basenames as devices even with suffixes.
        head = value.split(".", 1)[0].rstrip(". ").upper()
        if head in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", head):
            value = "_" + value
        return value

    value = clean(title) or clean(fallback) or "clip"
    if _utf16_length(value) > max_units:
        suffix = "_" + fingerprint(value)[:8]
        limit, prefix = max_units - len(suffix), []
        units = 0
        for character in value:
            width = _utf16_length(character)
            if units + width > limit:
                break
            prefix.append(character)
            units += width
        value = "".join(prefix).rstrip(". ") + suffix
    return value


def named_video_path(directory, title, fallback="clip", reusable=None) -> Path:
    """Choose a title-based MP4 path without replacing unrelated existing files.

    ``reusable`` identifies a video already owned by this export. Other files,
    including orphaned timeline sidecars, are treated as naming collisions.
    A conservative full-path budget also leaves room for the timeline sidecar.
    """
    directory = Path(directory).resolve()
    budget = min(120, 240 - _utf16_length(str(directory)) - 1 - len(".timeline.json"))
    stem = safe_filename_stem(title, fallback, max_units=budget)
    current = Path(reusable).resolve() if reusable is not None else None
    token = fingerprint({"title": str(title), "fallback": str(fallback)})[:8]
    for attempt in range(10000):
        suffix = "" if attempt == 0 else "_" + token + (f"_{attempt}" if attempt > 1 else "")
        candidate_stem = safe_filename_stem(stem, fallback, max_units=budget - len(suffix)) + suffix
        candidate = directory / (candidate_stem + ".mp4")
        sidecar = candidate.with_suffix(".timeline.json")
        if candidate == current or (not candidate.exists() and not sidecar.exists()):
            return candidate
    raise FileExistsError("同名导出文件过多，无法分配安全文件名。")


def rename_video_for_title(video_path, title, fallback="clip") -> Path:
    """Rename one MP4 and its timeline, returning its new path; never reencode.

    Callers must update their metadata/SQLite/manifest references after return.
    Existing unrelated videos and sidecars are preserved under all names.
    """
    source = Path(video_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination = named_video_path(source.parent, title, fallback, reusable=source)
    if destination == source:
        return source
    old_mapping, new_mapping = source.with_suffix(".timeline.json"), destination.with_suffix(".timeline.json")
    mapping = read_json(old_mapping) if old_mapping.is_file() else None
    if mapping is not None and not isinstance(mapping, dict):
        raise ValueError(f"视频时间轴记录无效: {old_mapping}")
    # Windows rename refuses an existing target. The project lock serializes
    # application exports; explicitly check sidecars before moving either file.
    if destination.exists() or new_mapping.exists():
        raise FileExistsError(destination)
    source.rename(destination)
    try:
        if mapping is not None:
            old_mapping.rename(new_mapping)
            atomic_json(new_mapping, {**mapping, "output": str(destination)})
    except Exception:
        if new_mapping.exists() and not old_mapping.exists():
            new_mapping.rename(old_mapping)
        destination.rename(source)
        raise
    return destination


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                      default=str).encode("utf-8")).hexdigest()[:24]


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def file_signature(path):
    p = Path(path).resolve()
    s = p.stat()
    # Sample both ends, so a same-size accidental replacement is usually noticed
    # without reading several GB of broadcast data on each rerun.
    with p.open("rb") as f:
        sample = f.read(1024 * 1024)
        if s.st_size > 1024 * 1024:
            f.seek(max(0, s.st_size - 1024 * 1024))
            sample += f.read(1024 * 1024)
    return {"path": str(p), "size": s.st_size, "mtime_ns": s.st_mtime_ns,
            "sample_sha256": hashlib.sha256(sample).hexdigest()}


@contextmanager
def project_lock(directory):
    """OS lock releases after crashes; stale lock files themselves are harmless."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    f = (directory / ".project.lock").open("a+b")
    f.seek(0, 2)
    if f.tell() == 0:
        f.write(b"0")
        f.flush()
    f.seek(0)
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise RuntimeError("该项目正在被另一个任务处理，请完成后重试") from exc
        yield
    finally:
        if acquired:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
