"""Synchronize existing export filenames with reviewed titles, without encoding.

Run without --apply to inspect the plan. The apply journal and online SQLite
backup stay under output/cleanup; --rollback-journal can recover interruption.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.file_access import restore_inherited_access
from core.jobs import WorkerLock
from core.utils import (atomic_json, named_video_path, project_lock, read_json,
                        rename_video_for_title)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path, root):
    selected = Path(path)
    if selected.is_symlink():
        raise ValueError(f"不通过符号链接迁移输出文件: {selected}")
    resolved = selected.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"输出路径超出 OUTPUT_DIR: {resolved}")
    return resolved


def _inventory(root):
    return [{"path": str(path.resolve()), "sha256": _hash(path), "bytes": path.stat().st_size}
            for path in sorted(root.rglob("*.mp4")) if path.is_file()]


def _connect(path):
    conn = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=rw", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _output_records(root, filename):
    """Archived/test export trees are valid; recovery copies are not live data."""
    ignored = {"cleanup", "backup", "backups", "journal", "journals"}
    return (path for path in root.rglob(filename)
            if not any(part.lower() in ignored for part in path.relative_to(root).parts[:-1]))


def _plan(conn, root):
    titles = {row["id"]: row["title"] for row in conn.execute("SELECT id,title FROM clips")}
    found, packages, warnings = {}, set(), []
    for row in conn.execute("SELECT clip_id,path,metadata_json FROM exports ORDER BY id"):
        try:
            video = _inside(row["path"], root)
            if video.is_file():
                saved = json.loads(row["metadata_json"])
                found[str(video)] = {"clip_id": row["clip_id"],
                                     "title": titles.get(row["clip_id"]) or saved.get("title") or row["clip_id"]}
                packages.add(video.parent)
        except (ValueError, TypeError, OSError) as exc:
            warnings.append(str(exc))
    for metadata_path in sorted(_output_records(root, "metadata.json")):
        try:
            directory = _inside(metadata_path.parent, root)
            saved = read_json(metadata_path)
            if not isinstance(saved, dict) or not isinstance(saved.get("video"), str):
                continue  # an arbitrary metadata/pattern file does not own videos
            clip_id = saved.get("clip_id") or directory.parent.name
            title = titles.get(clip_id) or saved.get("title") or clip_id
            video = _inside(saved["video"], root)
            if video.parent == directory and video.suffix.lower() == ".mp4" and video.is_file():
                found.setdefault(str(video), {"clip_id": clip_id, "title": title})
                packages.add(directory)
        except (ValueError, TypeError, OSError, AttributeError) as exc:
            warnings.append(f"跳过无法确认的发布包 {metadata_path}: {exc}")
    moves, reserved = [], set()
    for old_path, info in sorted(found.items()):
        old = Path(old_path)
        target = named_video_path(old.parent, info["title"], info["clip_id"], reusable=old)
        if str(target) in reserved:
            # Only unusual packages with multiple existing MP4s reach this path.
            title = info["title"] + "_" + hashlib.sha256(old_path.encode()).hexdigest()[:8]
            target = named_video_path(old.parent, title, info["clip_id"], reusable=old)
        else:
            title = info["title"]
        reserved.add(str(target))
        moves.append({"old_path": old_path, "new_path": str(target), "sha256": _hash(old),
                      "bytes": old.stat().st_size, **info, "filename_title": title, "state": "planned"})
    manifests = {root / p["id"] / "export_manifest.json" for p in conn.execute("SELECT id FROM projects")}
    manifests.update(_output_records(root, "export_manifest.json"))
    if "export_manifests" in _tables(conn):
        for row in conn.execute("SELECT path FROM export_manifests"):
            manifests.add(_inside(row["path"], root))
    return moves, packages, {p for p in manifests if p.is_file()}, titles, warnings


def _transform(value, path_map, titles):
    if isinstance(value, str):
        return path_map.get(value, value)
    if isinstance(value, list):
        return [_transform(item, path_map, titles) for item in value]
    if isinstance(value, dict):
        result = {key: _transform(item, path_map, titles) for key, item in value.items()}
        if result.get("clip_id") in titles and "title" in result:
            result["title"] = titles[result["clip_id"]]
        return result
    return value


def _path_map(moves):
    result = {}
    for move in moves:
        old, new = Path(move["old_path"]), Path(move["new_path"])
        for source, target in [(old, new), (old.with_suffix(".timeline.json"), new.with_suffix(".timeline.json"))]:
            result[str(source)] = str(target)
            result[source.as_posix()] = target.as_posix()
    return result


def _backup_files(paths):
    return [{"path": str(path), "content_base64": base64.b64encode(path.read_bytes()).decode("ascii")}
            for path in sorted(paths) if path.is_file()]


def _rollback(journal, conn):
    """Restore only migrated export/job fields; never replace approvals/audit."""
    root = Path(journal["output_dir"]).resolve()
    original_paths = {item["path"] for item in journal["file_backups"]}
    for move in reversed(journal["moves"]):
        old, new = _inside(move["old_path"], root), _inside(move["new_path"], root)
        if old != new and new.is_file() and not old.exists():
            if _hash(new) != move["sha256"]:
                raise RuntimeError(f"回滚目标内容变化，保留现场: {new}")
            new.rename(old)
        new_mapping = new.with_suffix(".timeline.json")
        if old != new and str(new_mapping) not in original_paths and new_mapping.exists():
            new_mapping.unlink()  # generated sidecar only; videos are never deleted
    for item in journal["file_backups"]:
        path = _inside(item["path"], root)
        path.write_bytes(base64.b64decode(item["content_base64"]))
        # Recovery can run after another human review. Keep that review's
        # invalidation effective rather than restoring stale publishing entries.
        if path.name == "export_manifest.json":
            report = read_json(path)
            retained = []
            for exported in report.get("exports", []):
                current = conn.execute("SELECT status,active,revision FROM clips WHERE id=?",
                                       (exported.get("clip_id"),)).fetchone()
                if current is None or (current["status"] == "approved" and current["active"] and current["revision"] == exported.get("revision")):
                    retained.append(exported)
                else:
                    report.setdefault("invalidated", []).append({"clip_id": exported.get("clip_id"),
                                                                 "reason": "迁移回滚期间发现审批已变更，请重新导出。"})
            report["exports"] = retained
            atomic_json(path, report)
        restore_inherited_access(path)
    with closing(sqlite3.connect(journal["database_backup"])) as backup:
        backup.row_factory = sqlite3.Row
        for row in backup.execute("SELECT id,path,metadata_json FROM exports"):
            if row["id"] in journal.get("updated_exports", []):
                conn.execute("UPDATE exports SET path=?,metadata_json=? WHERE id=?",
                             (row["path"], row["metadata_json"], row["id"]))
        if "jobs" in _tables(backup):
            for row in backup.execute("SELECT id,result_json FROM jobs"):
                if row["id"] in journal.get("updated_jobs", []):
                    conn.execute("UPDATE jobs SET result_json=? WHERE id=?", (row["result_json"], row["id"]))
    expected = Counter((item["sha256"], item["bytes"]) for item in journal.get("media_before", []))
    actual = Counter((item["sha256"], item["bytes"]) for item in _inventory(root))
    if expected - actual:
        raise RuntimeError("迁移回滚后发现原有视频缺失或内容变化，已保留备份和journal。")
    conn.commit()
    journal["status"] = "rolled_back"


def sync_exports(cfg, apply=False):
    root, database = Path(cfg["output_dir"]).resolve(), Path(cfg["database"]).resolve()
    conn = _connect(database)
    worker = WorkerLock(database)
    journal, journal_path = None, None
    try:
        if apply and not worker.acquire():
            raise RuntimeError("后台队列仍在运行，请等任务结束并关闭队列后重试。")
        with ExitStack() as locks:
            if apply:
                for project in conn.execute("SELECT project_dir FROM projects ORDER BY id"):
                    if project["project_dir"]:
                        locks.enter_context(project_lock(project["project_dir"]))
                conn.execute("BEGIN IMMEDIATE")  # freeze review edits until DB/files agree
                cleanup = root / "cleanup"
                cleanup.mkdir(parents=True, exist_ok=True)
                for previous in cleanup.glob("sync_exports_*.json"):
                    saved = read_json(previous)
                    if saved.get("status") not in {"completed", "rolled_back"}:
                        raise RuntimeError(f"发现未完成迁移，请先 --rollback-journal {previous}")
            moves, packages, manifests, titles, warnings = _plan(conn, root)
            if not apply:
                return {"status": "dry_run", "moves": moves, "warnings": warnings,
                        "package_count": len(packages), "note": "未更改文件或数据库；确认后加 --apply。"}
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
            database_backup = cleanup / ("exports_before_" + stamp + ".sqlite3")
            # Separate read connection can make a consistent online backup while
            # this connection holds a reserved write lock (including WAL state).
            with closing(sqlite3.connect(str(database))) as live, closing(sqlite3.connect(str(database_backup))) as backup:
                live.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("数据库在线备份校验失败")
            assets = set(manifests)
            for package in packages:
                assets.update(p for p in package.iterdir() if p.is_file() and p.suffix.lower() != ".mp4")
            before = _inventory(root)
            journal_path = cleanup / ("sync_exports_" + stamp + ".json")
            journal = {"status": "running", "database": str(database), "output_dir": str(root),
                       "database_backup": str(database_backup), "moves": moves, "warnings": warnings,
                       "file_backups": _backup_files(assets), "media_before": before,
                       "updated_exports": [], "updated_jobs": [], "acl_files": 0}
            atomic_json(journal_path, journal)
            for move in moves:
                old = Path(move["old_path"])
                # Re-evaluate after earlier moves have freed names; journal the
                # exact target before changing any file, even if the process dies.
                target = named_video_path(old.parent, move["filename_title"], move["clip_id"], reusable=old)
                move["new_path"] = str(target)
                atomic_json(journal_path, journal)
                actual = rename_video_for_title(old, move["filename_title"], move["clip_id"])
                if actual != target or _hash(actual) != move["sha256"] or actual.stat().st_size != move["bytes"]:
                    raise RuntimeError(f"改名后视频内容校验失败: {actual}")
                move["state"] = "renamed"
                atomic_json(journal_path, journal)
            path_map = _path_map(moves)
            for package in sorted(packages):
                metadata_path = package / "metadata.json"
                if metadata_path.is_file():
                    saved = _transform(read_json(metadata_path), path_map, titles)
                    atomic_json(metadata_path, saved)
                publication = package / "publication.txt"
                owned = next((m for m in moves if Path(m["new_path"]).parent == package), None)
                if publication.is_file() and owned:
                    text = publication.read_text(encoding="utf-8-sig")
                    tail = text.partition("\n")[2]
                    publication.write_text(owned["title"] + ("\n" + tail if "\n" in text else ""), encoding="utf-8")
            for manifest in manifests:
                atomic_json(manifest, _transform(read_json(manifest), path_map, titles))
            for row in conn.execute("SELECT id,path,metadata_json FROM exports").fetchall():
                saved = _transform(json.loads(row["metadata_json"]), path_map, titles)
                new_path = path_map.get(row["path"], row["path"])
                if saved != json.loads(row["metadata_json"]) or new_path != row["path"]:
                    journal["updated_exports"].append(row["id"])
                    atomic_json(journal_path, journal)
                    conn.execute("UPDATE exports SET path=?,metadata_json=? WHERE id=?",
                                 (new_path, json.dumps(saved, ensure_ascii=False), row["id"]))
            if "jobs" in _tables(conn):
                for row in conn.execute("SELECT id,result_json FROM jobs WHERE result_json IS NOT NULL").fetchall():
                    saved = json.loads(row["result_json"])
                    updated = _transform(saved, path_map, titles)
                    if updated != saved:
                        journal["updated_jobs"].append(row["id"])
                        atomic_json(journal_path, journal)
                        conn.execute("UPDATE jobs SET result_json=? WHERE id=?", (json.dumps(updated, ensure_ascii=False), row["id"]))
            for package in sorted(packages):
                for path in sorted(package.iterdir()):
                    if path.is_file():
                        restore_inherited_access(path)
                        journal["acl_files"] += 1
            after = _inventory(root)
            counts = lambda items: Counter((item["sha256"], item["bytes"]) for item in items)
            if counts(before) != counts(after):
                raise RuntimeError("输出视频保全检查失败：数量、字节数或内容hash发生变化。")
            journal["media_after"] = after
            journal["status"] = "verified"
            atomic_json(journal_path, journal)
            conn.commit()
            journal["status"] = "completed"
            atomic_json(journal_path, journal)
            return {"status": "completed", "moves": moves, "warnings": warnings,
                    "journal": str(journal_path), "database_backup": str(database_backup),
                    "preserved_videos": len(after), "acl_files": journal["acl_files"]}
    except Exception as exc:
        conn.rollback()
        if journal is not None:
            journal["error"] = str(exc)
            try:
                _rollback(journal, conn)
            except Exception as recovery:
                journal["status"] = "rollback_failed"
                journal["rollback_error"] = str(recovery)
            atomic_json(journal_path, journal)
        raise
    finally:
        conn.close()
        worker.release()


def rollback_journal(path):
    selected = Path(path).resolve()
    journal = read_json(selected)
    if journal.get("status") == "rolled_back":
        return {"status": "rolled_back", "journal": str(selected)}
    worker = WorkerLock(journal["database"])
    if not worker.acquire():
        raise RuntimeError("后台队列仍在运行，无法回滚迁移。")
    conn = _connect(journal["database"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        _rollback(journal, conn)
        atomic_json(selected, journal)
    finally:
        conn.close()
        worker.release()
    return {"status": journal["status"], "journal": str(selected)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback-journal")
    args = parser.parse_args()
    result = rollback_journal(args.rollback_journal) if args.rollback_journal else sync_exports(load_config(args.config), args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
