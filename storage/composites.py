"""Independent dual-source compositions; source projects and approvals are untouched."""
from __future__ import annotations

import json
from storage.database import Database, _json, _now


class CompositeStore(Database):
    def __init__(self, path):
        super().__init__(path)
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS composites (
                id TEXT PRIMARY KEY, document_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS composite_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, composite_id TEXT NOT NULL,
                before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
        ''')
        self.connection.commit()

    def get_composite(self, identity):
        row = self.connection.execute("SELECT document_json FROM composites WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_composites(self):
        return [json.loads(r[0]) for r in self.connection.execute("SELECT document_json FROM composites ORDER BY updated_at DESC,id")]

    def insert_composite(self, document):
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO composites VALUES(?,?,?)", (document['id'], _json(document), _now()))
        return self.get_composite(document['id'])

    def update_composite(self, identity, *, options=None, sync=None, visual_sync=None, output=None):
        from core.composite_options import normalize_composite_options
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            document = self.get_composite(identity)
            if not document:
                raise ValueError("找不到双路合成项目")
            before = _json(document)
            if options is not None:
                document['options'] = normalize_composite_options(options)
            if sync is not None:
                document['sync'] = sync
            if visual_sync is not None:
                document['visual_sync'] = visual_sync
            if output is not None:
                outputs = document.setdefault('outputs', [])
                outputs[:] = [r for r in outputs if r['video'] != output['video']]
                outputs.append(output)
            if _json(document) != before:
                self.connection.execute("UPDATE composites SET document_json=?,updated_at=? WHERE id=?", (_json(document), _now(), identity))
                self.connection.execute("INSERT INTO composite_audit(composite_id,before_json,after_json,created_at) VALUES(?,?,?,?)",
                                        (identity, before, _json(document), _now()))
        return document
