"""SQLite storage. Reruns retain human decisions and append-only review history."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any

from .models import REVIEW_STATUSES, finite_number, validate_range

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


class Database:
    """Each CLI invocation or Streamlit rerun owns one short-lived connection."""

    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
                duration REAL NOT NULL CHECK(duration > 0), status TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}', project_dir TEXT NOT NULL DEFAULT '',
                error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                start REAL NOT NULL, end REAL NOT NULL, category TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL, reason TEXT NOT NULL,
                score REAL NOT NULL, tags_json TEXT NOT NULL,
                original_json TEXT NOT NULL, candidate_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','approved','rejected')),
                subtitle_text TEXT, preview_path TEXT, preview_fingerprint TEXT,
                revision INTEGER NOT NULL DEFAULT 0,
                render_revision INTEGER NOT NULL DEFAULT 0,
                metadata_revision INTEGER NOT NULL DEFAULT 0,
                transcript_key TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                CHECK(start >= 0 AND end > start)
            );
            CREATE INDEX IF NOT EXISTS clips_project ON clips(project_id, start);
            CREATE TABLE IF NOT EXISTS review_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id TEXT NOT NULL REFERENCES clips(id),
                before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id TEXT NOT NULL REFERENCES clips(id), orientation TEXT NOT NULL,
                path TEXT NOT NULL, metadata_json TEXT NOT NULL,
                revision INTEGER NOT NULL, render_revision INTEGER NOT NULL,
                metadata_revision INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS exports_clip ON exports(clip_id, id);
            CREATE TABLE IF NOT EXISTS export_manifests (
                path TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id)
            );
            CREATE TABLE IF NOT EXISTS export_preferences (
                project_id TEXT PRIMARY KEY REFERENCES projects(id),
                options_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(clips)")}
        if "active" not in columns:
            self.connection.execute("ALTER TABLE clips ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if "transcript_key" not in columns:
            self.connection.execute("ALTER TABLE clips ADD COLUMN transcript_key TEXT")
        self.connection.commit()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def upsert_project(self, project: dict) -> dict:
        old = self.get_project(str(project["id"])) or {}
        current = {**old, **project}
        duration = finite_number(current["duration"], "duration")
        if duration <= 0:
            raise ValueError("Project duration must be positive")
        now = _now()
        with self.connection:
            self.connection.execute("""
                INSERT INTO projects
                (id,source,name,duration,status,metadata_json,project_dir,error,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET source=excluded.source,name=excluded.name,
                    duration=excluded.duration,status=excluded.status,metadata_json=excluded.metadata_json,
                    project_dir=excluded.project_dir,error=excluded.error,updated_at=excluded.updated_at
            """, (str(current["id"]), str(current["source"]), current.get("name", Path(current["source"]).name),
                  duration, current.get("status", "imported"), _json(current.get("metadata", {})),
                  str(current.get("project_dir", "")), current.get("error"), now, now))
        return self.get_project(str(current["id"]))

    @staticmethod
    def _project(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def get_project(self, project_id: str) -> dict | None:
        return self._project(self.connection.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def list_projects(self) -> list[dict]:
        rows = self.connection.execute("""
            SELECT p.*, count(c.id) AS candidate_count,
                coalesce(sum(c.status='approved'),0) AS approved_count,
                coalesce(sum(c.status='rejected'),0) AS rejected_count,
                coalesce(sum(c.status='pending'),0) AS pending_count
            FROM projects p LEFT JOIN clips c ON c.project_id=p.id AND c.active=1
            GROUP BY p.id ORDER BY p.updated_at DESC
        """).fetchall()
        return [self._project(row) for row in rows]

    def upsert_clips(self, project_id: str, clips: list[dict], replace_pending: bool = False) -> None:
        project = self.get_project(project_id)
        if not project:
            raise ValueError(f"Unknown project: {project_id}")
        validated = []
        for item in clips:
            clip = dict(item)
            clip["id"] = str(clip["id"])
            clip["start"], clip["end"] = validate_range(clip["start"], clip["end"], project["duration"])
            clip["score"] = finite_number(clip.get("score", 0), "score")
            if not 0 <= clip["score"] <= 100:
                raise ValueError("score must be between 0 and 100")
            if not isinstance(clip.get("tags", []), list):
                raise ValueError("tags must be a list")
            old = self.get_clip(clip["id"])
            if old and old["project_id"] != project_id:
                raise ValueError(f"Clip ID already belongs to another project: {clip['id']}")
            validated.append(clip)
        now = _now()
        with self.connection:
            if replace_pending:
                self.connection.execute("UPDATE clips SET active=0 WHERE project_id=? AND status='pending'", (project_id,))
            for clip in validated:
                # The initial AI candidate stays immutable. A rerun only records its
                # latest suggestion; reviewed and rendered values are never overwritten.
                self.connection.execute("""
                    INSERT INTO clips
                    (id,project_id,start,end,category,title,description,reason,score,tags_json,
                     original_json,candidate_json,created_at,updated_at,transcript_key)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET candidate_json=excluded.candidate_json,active=1
                """, (clip["id"], project_id, clip["start"], clip["end"], clip.get("category", "other"),
                      clip.get("title", "未命名候选"), clip.get("description", ""), clip.get("reason", ""),
                      clip["score"], _json(clip.get("tags", [])), _json(clip), _json(clip), now, now,
                      clip.get("transcript_key") or project["metadata"].get("transcript_key")))

    def bind_transcript(self, project_id: str, transcript_key: str) -> None:
        """One-time migration: never rebind a reviewed clip to a new transcript."""
        with self.connection:
            self.connection.execute("UPDATE clips SET transcript_key=? WHERE project_id=? AND transcript_key IS NULL",
                                    (transcript_key, project_id))

    def _clip(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags_json"))
        result["original"] = json.loads(result.pop("original_json"))
        result["candidate"] = json.loads(result.pop("candidate_json"))
        result["exports"] = {}
        if result["status"] != "approved" or not result["active"]:
            return result
        for record in self.connection.execute("""
            SELECT * FROM exports WHERE clip_id=? AND render_revision=? AND metadata_revision=?
            ORDER BY id
        """, (result["id"], result["render_revision"], result["metadata_revision"])):
            export = dict(record)
            export["metadata"] = json.loads(export.pop("metadata_json"))
            result["exports"][export["orientation"]] = export
        return result

    def get_clip(self, clip_id: str) -> dict | None:
        return self._clip(self.connection.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone())

    def list_clips(self, project_id: str, status: str | None = None) -> list[dict]:
        if status is not None and status not in REVIEW_STATUSES:
            raise ValueError(f"Unknown review status: {status}")
        sql = "SELECT * FROM clips WHERE project_id=? AND active=1"
        params = [project_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        return [self._clip(row) for row in self.connection.execute(sql + " ORDER BY start,id", params).fetchall()]

    def review_clip(self, clip_id: str, status: str, title: str | None = None,
                    start: float | None = None, end: float | None = None,
                    subtitle_text: str | None = None, reset_subtitle: bool = False) -> dict:
        if status not in REVIEW_STATUSES:
            raise ValueError(f"Unknown review status: {status}")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            before = self.get_clip(clip_id)
            if before is None:
                raise ValueError(f"Unknown clip: {clip_id}")
            project = self.get_project(before["project_id"])
            new_start, new_end = validate_range(before["start"] if start is None else start,
                                                before["end"] if end is None else end, project["duration"])
            new_title = before["title"] if title is None else str(title).strip()
            if not new_title:
                raise ValueError("Title cannot be empty")
            bounds_changed = (new_start, new_end) != (before["start"], before["end"])
            if bounds_changed and before["subtitle_text"] is not None and not reset_subtitle:
                if subtitle_text is None:
                    reset_subtitle = True
                elif subtitle_text == before["subtitle_text"]:
                    raise ValueError("时间范围已改变，请选择自动字幕，或同步调整手工 SRT 时间轴。")
            new_subtitle = None if reset_subtitle or subtitle_text == "" else (before["subtitle_text"] if subtitle_text is None else str(subtitle_text))
            if new_subtitle is not None:
                from core.subtitle import validate_srt
                validate_srt(new_subtitle, new_end - new_start)
            media_changed = (new_start, new_end, new_subtitle) != (
                before["start"], before["end"], before["subtitle_text"])
            title_changed = new_title != before["title"]
            changed = media_changed or title_changed or status != before["status"]
            if not changed:
                return before
            self.connection.execute("""
                UPDATE clips SET start=?,end=?,title=?,subtitle_text=?,status=?,revision=revision+1,
                    render_revision=render_revision+?,metadata_revision=metadata_revision+?,
                    preview_path=?,preview_fingerprint=?,updated_at=? WHERE id=?
            """, (new_start, new_end, new_title, new_subtitle, status, int(media_changed), int(title_changed),
                  None if media_changed else before["preview_path"],
                  None if media_changed else before["preview_fingerprint"], _now(), clip_id))
            after = self.get_clip(clip_id)
            self.connection.execute("INSERT INTO review_audit(clip_id,before_json,after_json,created_at) VALUES(?,?,?,?)",
                                    (clip_id, _json(before), _json(after), _now()))
            self._invalidate_manifests(before["project_id"], clip_id)
        return after

    def _invalidate_manifests(self, project_id: str, clip_id: str) -> None:
        """A review edit immediately removes its stale publication entries."""
        from core.utils import atomic_json, read_json
        # Existing 0.1 exports predate the registry. Their documented package
        # layout lets us register the canonical manifest on the first new review.
        for record in self.connection.execute("SELECT path FROM exports WHERE clip_id=?", (clip_id,)):
            video = Path(record["path"])
            if len(video.parents) >= 3 and video.parents[2].name == project_id:
                legacy = video.parents[2] / "export_manifest.json"
                if legacy.exists():
                    self.connection.execute("INSERT OR IGNORE INTO export_manifests(path,project_id) VALUES(?,?)",
                                            (str(legacy.resolve()), project_id))
        for row in self.connection.execute("SELECT path FROM export_manifests WHERE project_id=?", (project_id,)):
            path = Path(row["path"])
            if not path.exists():
                continue
            try:
                report = read_json(path)
                records = report["exports"]
                stale = [item for item in records if item["clip_id"] == clip_id]
                if not stale:
                    continue
                report["exports"] = [item for item in records if item["clip_id"] != clip_id]
                report.setdefault("invalidated", []).append({"clip_id": clip_id, "reason": "审批或编辑已变更，请重新导出。"})
            except (ValueError, TypeError, KeyError, AttributeError):
                report = {"project_id": project_id, "exports": [], "errors": [],
                          "invalidated": [{"clip_id": clip_id, "reason": "旧清单损坏且审批已变更，请重新导出。"}]}
            atomic_json(path, report)

    def write_export_manifest(self, project_id: str, path: str | Path, results: list[dict], errors: list[dict]) -> dict:
        """Commit a manifest against one locked review snapshot.

        FFmpeg runs outside this transaction. Recheck earlier render results here,
        since another browser can edit a clip while later clips are rendering.
        Subsequent review changes invalidate registered manifests under this lock.
        """
        from core.utils import atomic_json
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current, invalidated = [], []
            for item in results:
                clip = self.get_clip(item["clip_id"])
                if clip and clip["active"] and clip["status"] == "approved" and clip["revision"] == item["revision"]:
                    current.append(item)
                else:
                    invalidated.append({"clip_id": item["clip_id"], "orientation": item["orientation"],
                                        "error": "导出期间审批或编辑已变更，本版本已从发布清单移除。"})
            report = {"project_id": project_id, "exports": current, "errors": errors + invalidated,
                      "note": "以此清单为本次导出结果；历史版本保留在原路径。后续编辑会移除对应旧清单条目。"}
            atomic_json(path, report)
            self.connection.execute("INSERT INTO export_manifests(path,project_id) VALUES(?,?) "
                                    "ON CONFLICT(path) DO UPDATE SET project_id=excluded.project_id",
                                    (str(Path(path).resolve()), project_id))
        return report

    def review_history(self, clip_id: str) -> list[dict]:
        return [{"id": row["id"], "clip_id": row["clip_id"], "before": json.loads(row["before_json"]),
                 "after": json.loads(row["after_json"]), "created_at": row["created_at"]}
                for row in self.connection.execute("SELECT * FROM review_audit WHERE clip_id=? ORDER BY id", (clip_id,))]

    def get_export_options(self, project_id: str) -> dict:
        row = self.connection.execute("SELECT options_json FROM export_preferences WHERE project_id=?",
                                      (project_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_export_options(self, project_id: str, options: dict) -> None:
        from core.export_options import normalize_export_options
        normalized = normalize_export_options(options)
        if not self.get_project(project_id):
            raise ValueError("找不到要保存导出设置的项目。")
        with self.connection:
            self.connection.execute("INSERT INTO export_preferences(project_id,options_json,updated_at) VALUES(?,?,?) "
                                    "ON CONFLICT(project_id) DO UPDATE SET options_json=excluded.options_json,updated_at=excluded.updated_at",
                                    (project_id, _json(normalized), _now()))

    def set_preview(self, clip_id: str, path: str | Path, fingerprint: str | None = None,
                    expected_revision: int | None = None) -> None:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            clip = self.get_clip(clip_id)
            if clip is None:
                raise ValueError(f"Unknown clip: {clip_id}")
            if not clip["active"]:
                raise ValueError("Candidate was replaced by a new selection; render an active candidate")
            if expected_revision is not None and clip["revision"] != expected_revision:
                raise ValueError("Clip changed during preview rendering; render the latest revision again")
            self.connection.execute("UPDATE clips SET preview_path=?,preview_fingerprint=?,updated_at=? WHERE id=?",
                                    (str(path), fingerprint, _now(), clip_id))

    def set_export(self, clip_id: str, orientation: str, path: str | Path, metadata: dict | None = None,
                   expected_revision: int | None = None) -> None:
        if orientation not in ("horizontal", "vertical"):
            raise ValueError("orientation must be horizontal or vertical")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            clip = self.get_clip(clip_id)
            if clip is None:
                raise ValueError(f"Unknown clip: {clip_id}")
            if not clip["active"]:
                raise ValueError("Candidate was replaced by a new selection; export an active candidate")
            if clip["status"] != "approved":
                raise ValueError("Only approved clips can be exported")
            if expected_revision is not None and clip["revision"] != expected_revision:
                raise ValueError("Clip changed during export rendering; export the latest revision again")
            existing = clip["exports"].get(orientation)
            if existing and existing["path"] == str(path) and existing["metadata"] == (metadata or {}):
                return
            self.connection.execute("""
                INSERT INTO exports(clip_id,orientation,path,metadata_json,revision,render_revision,metadata_revision,created_at)
                VALUES(?,?,?,?,?,?,?,?)
            """, (clip_id, orientation, str(path), _json(metadata or {}), clip["revision"],
                  clip["render_revision"], clip["metadata_revision"], _now()))

    def stats(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute("""
            SELECT category, count(*) AS candidate_count,
                sum(status='approved') AS approved_count, sum(status='rejected') AS rejected_count,
                sum(status='pending') AS pending_count,
                avg(end-start) AS average_duration,
                1.0*sum(status='approved')/count(*) AS approval_rate,
                CASE WHEN sum(status!='pending')>0 THEN
                    1.0*sum(status='approved')/sum(status!='pending') ELSE NULL END AS reviewed_approval_rate
            FROM clips WHERE active=1 GROUP BY category ORDER BY candidate_count DESC,category
        """)]
