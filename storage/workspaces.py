"""Additive workbench association layer. Legacy records and IDs remain intact."""
from __future__ import annotations

import copy
import json
import uuid

from storage.database import Database, _json, _now


class WorkspaceStore(Database):
    def __init__(self, path):
        super().__init__(path)
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY, document_json TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_media (
                id INTEGER PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                item_id TEXT NOT NULL, snapshot_json TEXT NOT NULL, result_json TEXT NOT NULL,
                preview INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_audit (
                id INTEGER PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_preferences (
                key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_deliveries (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT UNIQUE NOT NULL, workspace_id TEXT NOT NULL,
                profile TEXT NOT NULL, document_json TEXT NOT NULL, created_at TEXT NOT NULL,
                snapshot_json TEXT);
            CREATE TABLE IF NOT EXISTS workflow_delivery_outbox (
                delivery_id TEXT PRIMARY KEY REFERENCES workflow_deliveries(delivery_id),
                publication_json TEXT, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                receipt_json TEXT, acknowledged_at TEXT, created_at TEXT NOT NULL);
        ''')
        columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(workflow_deliveries)')}
        if 'snapshot_json' not in columns:
            self.connection.execute('ALTER TABLE workflow_deliveries ADD COLUMN snapshot_json TEXT')
            self.connection.commit()

    def get_workspace(self, identity):
        row = self.connection.execute('SELECT * FROM workspaces WHERE id=?', (identity,)).fetchone()
        return {**json.loads(row['document_json']), 'revision': row['revision']} if row else None

    def list_workspaces(self):
        return [self.get_workspace(r[0]) for r in self.connection.execute('SELECT id FROM workspaces ORDER BY updated_at DESC')]

    def create_workspace(self, name='未命名项目', identity=None):
        identity = identity or uuid.uuid4().hex
        doc = dict(id=identity, name=name, step=1, assets=[], variants=[], active_variant=None,
                   jobs=[], packaging={}, source_options={}, status='等待导入')
        with self.connection:
            self.connection.execute('INSERT OR IGNORE INTO workspaces VALUES(?,?,0,?)', (identity, _json(doc), _now()))
        return self.get_workspace(identity)

    def save_workspace(self, document, expected_revision=None):
        doc = copy.deepcopy(document)
        expected = doc.pop('revision') if expected_revision is None else expected_revision
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            before = self.get_workspace(doc['id'])
            if not before or before['revision'] != expected:
                raise ValueError('项目已在其他页面更新，请刷新后重试；未覆盖已保存内容')
            self.connection.execute('UPDATE workspaces SET document_json=?,revision=revision+1,updated_at=? WHERE id=?',
                                    (_json(doc), _now(), doc['id']))
            after = self.get_workspace(doc['id'])
            self.connection.execute('INSERT INTO workflow_audit(workspace_id,before_json,after_json,created_at) VALUES(?,?,?,?)',
                                    (doc['id'], _json(before), _json(after), _now()))
        return after

    def preference(self, key, value=None):
        if value is not None:
            with self.connection:
                self.connection.execute('INSERT OR REPLACE INTO workflow_preferences VALUES(?,?)', (key, _json(value)))
        row = self.connection.execute('SELECT value_json FROM workflow_preferences WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def record_workflow_media(self, workspace_id, item_id, snapshot, result, preview=False):
        with self.connection:
            if result.get('render_key'):
                for record in self.list_media(workspace_id, preview):
                    if record['item_id']==item_id and record['result'].get('render_key')==result['render_key']:
                        return
            self.connection.execute('INSERT INTO workflow_media(workspace_id,item_id,snapshot_json,result_json,preview,created_at) VALUES(?,?,?,?,?,?)',
                                    (workspace_id, item_id, _json(snapshot), _json(result), int(preview), _now()))

    def list_media(self, workspace_id, preview=None):
        rows = self.connection.execute('SELECT * FROM workflow_media WHERE workspace_id=? ORDER BY id DESC', (workspace_id,))
        return [{**dict(r), 'snapshot': json.loads(r['snapshot_json']), 'result': json.loads(r['result_json'])}
                for r in rows if preview is None or r['preview'] == int(preview)]

    def register_delivery(self, delivery, publication=None, snapshot=None):
        """Caller owns the transaction, including the final current-content check."""
        existing = self.get_delivery(delivery['delivery_id'])
        if existing is not None:
            if existing != delivery:
                raise ValueError('交付标识已有不同内容，未覆盖历史交付')
            return existing
        self.connection.execute('INSERT INTO workflow_deliveries(delivery_id,workspace_id,profile,document_json,created_at,snapshot_json) VALUES(?,?,?,?,?,?)',
            (delivery['delivery_id'], delivery['workspace_id'], delivery['profile'], _json(delivery), _now(), _json(snapshot) if snapshot is not None else None))
        self.connection.execute('INSERT INTO workflow_delivery_outbox(delivery_id,publication_json,created_at) VALUES(?,?,?)',
            (delivery['delivery_id'], _json(publication) if publication is not None else None, _now()))
        return delivery

    def get_delivery(self, identity, profile=None):
        row = self.connection.execute('SELECT document_json,profile FROM workflow_deliveries WHERE delivery_id=?', (identity,)).fetchone()
        return json.loads(row['document_json']) if row and (profile is None or row['profile'] == profile) else None

    def get_delivery_snapshot(self, identity):
        row = self.connection.execute('SELECT snapshot_json FROM workflow_deliveries WHERE delivery_id=?', (identity,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def supersede_delivery(self, identity):
        with self.connection:
            self.connection.execute("UPDATE workflow_delivery_outbox SET state='superseded',last_error='上游已修改，历史交付不再自动发送' WHERE delivery_id=? AND state='pending'", (identity,))

    def list_deliveries(self, cursor=0, profile=None, limit=100):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('交付游标或数量无效')
        rows = self.connection.execute('SELECT sequence,document_json FROM workflow_deliveries WHERE sequence>? AND (? IS NULL OR profile=?) ORDER BY sequence LIMIT ?',
            (cursor, profile, profile, limit)).fetchall()
        return {'deliveries': [json.loads(r['document_json']) for r in rows],
                'next_cursor': str(rows[-1]['sequence'] if rows else cursor)}

    def pending_deliveries(self, profile=None, limit=100):
        rows = self.connection.execute('''SELECT d.document_json,o.publication_json,o.attempts,o.last_error
            FROM workflow_delivery_outbox o JOIN workflow_deliveries d ON d.delivery_id=o.delivery_id
            WHERE o.state='pending' AND o.publication_json IS NOT NULL AND (? IS NULL OR d.profile=?) ORDER BY d.sequence LIMIT ?''', (profile, profile, limit))
        return [dict(delivery=json.loads(r['document_json']), publication=json.loads(r['publication_json']) if r['publication_json'] else None,
                     attempts=r['attempts'], last_error=r['last_error']) for r in rows]

    def acknowledge_delivery(self, identity, receipt=None):
        with self.connection:
            self.connection.execute("UPDATE workflow_delivery_outbox SET state='acknowledged',receipt_json=?,acknowledged_at=?,last_error=NULL WHERE delivery_id=?",
                (_json(receipt), _now(), identity))

    def fail_delivery(self, identity, error):
        with self.connection:
            self.connection.execute("UPDATE workflow_delivery_outbox SET attempts=attempts+1,last_error=? WHERE delivery_id=? AND state='pending'",
                (str(error)[:2000], identity))
