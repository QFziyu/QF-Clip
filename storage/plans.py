"""Separate editorial plans and audit history; legacy clips remain untouched."""
from __future__ import annotations

import copy
import json

from storage.database import Database, _now, _json


class PlanStore(Database):
    def __init__(self, path):
        super().__init__(path)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS editing_plans (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                document_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS editing_plans_project ON editing_plans(project_id);
            CREATE TABLE IF NOT EXISTS plan_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL REFERENCES editing_plans(id),
                before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS editing_templates (
                name TEXT PRIMARY KEY, options_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        self.connection.commit()

    def get_plan(self, plan_id):
        row = self.connection.execute("SELECT * FROM editing_plans WHERE id=?", (plan_id,)).fetchone()
        return {**json.loads(row["document_json"]), "revision": row["revision"]} if row else None

    def list_plans(self, project_id):
        return [self.get_plan(row[0]) for row in self.connection.execute(
            "SELECT id FROM editing_plans WHERE project_id=? ORDER BY created_at DESC,id", (project_id,)).fetchall()]

    def insert_plan(self, document):
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO editing_plans VALUES(?,?,?,0,?,?)",
                                    (document["id"], document["project_id"], _json(document), _now(), _now()))
        return self.get_plan(document["id"])

    def edit_plan(self, plan_id, expected_revision, draft_edits=None, event_edits=None):
        from core.editing import validate_plan_document
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            before = self.get_plan(plan_id)
            if not before or before["revision"] != expected_revision:
                raise ValueError("方案已被其他页面修改，请刷新后重试")
            after = copy.deepcopy(before)
            for kind, edits, allowed in [("drafts", draft_edits, {"title", "status", "segments", "subtitle_text"}),
                                          ("events", event_edits, {"start", "end", "confirmed", "waived"})]:
                by_id = {item["id"]: item for item in after[kind]}
                for identity, fields in (edits or {}).items():
                    if identity not in by_id or not isinstance(fields, dict) or set(fields) - allowed:
                        raise ValueError("未知草稿、事件或编辑字段")
                    if kind == "drafts" and "segments" in fields and fields["segments"] != by_id[identity]["segments"]:
                        old_subtitle = by_id[identity].get("subtitle_text")
                        if old_subtitle and fields.get("subtitle_text") == old_subtitle:
                            raise ValueError("源区间已改变，请重设自动字幕或同步调整手工 SRT")
                        if "subtitle_text" not in fields:
                            by_id[identity]["subtitle_text"] = None
                    by_id[identity].update(fields)
            validate_plan_document(after, self.get_project(after["project_id"])["duration"])
            if before == after:
                return before
            after["revision"] += 1
            # Every edit produces a new revision. Historical files remain, but
            # the current document must never advertise an older composition.
            for draft in after["drafts"]:
                draft.pop("preview", None)
                draft.pop("preview_key", None)
                draft.pop("outputs", None)
            self.connection.execute("UPDATE editing_plans SET document_json=?,revision=?,updated_at=? WHERE id=?",
                                    (_json(after), after["revision"], _now(), plan_id))
            self.connection.execute("INSERT INTO plan_audit(plan_id,before_json,after_json,created_at) VALUES(?,?,?,?)",
                                    (plan_id, _json(before), _json(after), _now()))
        return after

    def record_media(self, plan_id, draft_id, revision, media, preview=False):
        """Reject rendering completed against a stale approval snapshot."""
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            plan = self.get_plan(plan_id)
            if not plan or plan["revision"] != revision:
                raise ValueError("渲染期间方案已修改，文件保留为历史，不计入当前结果")
            draft = next((d for d in plan["drafts"] if d["id"] == draft_id), None)
            if not draft or (not preview and draft["status"] != "approved"):
                raise ValueError("当前草稿尚未通过审批")
            if preview:
                draft["preview"], draft["preview_key"] = media["video"], media["render_key"]
            else:
                draft.setdefault("outputs", {})[media["orientation"]] = media
            self.connection.execute("UPDATE editing_plans SET document_json=?,updated_at=? WHERE id=?",
                                    (_json(plan), _now(), plan_id))

    def save_template(self, name, options):
        from core.editing_options import normalize_editing_options
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("模板名称需要 1～80 字符")
        normalized = normalize_editing_options(options)
        # Match anchors/events belong to a particular recording, not a reusable
        # personal style template. Do not reuse another game's mandatory events.
        normalized.update(anchors=[], events=[])
        with self.connection:
            self.connection.execute("INSERT INTO editing_templates VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET options_json=excluded.options_json,updated_at=excluded.updated_at",
                                    (name.strip(), _json(normalized), _now()))

    def list_templates(self):
        return {row["name"]: json.loads(row["options_json"]) for row in self.connection.execute(
            "SELECT * FROM editing_templates ORDER BY name")}
