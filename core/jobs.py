"""Durable local task queue. Only one worker may use a database at a time.

The queue stores commands, configuration *paths* and results, never API credentials.
Workers reload the selected YAML/.env before each task and survive browser closure.
"""
from __future__ import annotations

from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
KINDS = {"process", "batch", "export", "preview", "plan", "plan_preview", "plan_export"}
KINDS.update({'compose_sync', 'compose_render', 'compose_prepare', 'compose_visual'})
KINDS.update({'workflow_prepare', 'workflow_preview', 'workflow_export', 'workflow_transcribe','workflow_subtitle_review'})
KINDS.add('workflow_titles')
KINDS.update({'export_batch','workflow_repair'})
STATES = {"queued", "running", "completed", "failed", "interrupted", "cancelling", "cancelled"}
PROCESS_FIELDS = {"audio_track", "skip_render", "mock", "transcript_path", "allow_cpu", "selection_mode", "editing_options", "transcript_only"}
FIELDS = {"process": PROCESS_FIELDS | {"path"}, "batch": PROCESS_FIELDS | {"directory", "recursive"},
          "export": {"project_id", "orientations", "export_options"}, "preview": {"project_id", "clip_id"},
          "plan": {"project_id", "options", "mock", "skip_render"}, "plan_preview": {"plan_id", "draft_id"},
          "plan_export": {"plan_id", "orientations", "export_options"}}
FIELDS.update(compose_sync={'composite_id', 'semantic', 'allow_cpu'},
              compose_render={'composite_id', 'options', 'segments', 'preview', 'preview_start'},
              compose_prepare={'match', 'commentator', 'match_track', 'commentator_track'},
              compose_visual={'composite_id', 'search', 'analyze'})
FIELDS.update(workflow_prepare={'workspace_id', 'settings', 'expected_revision'},
              workflow_preview={'workspace_id', 'item_id'}, workflow_export={'workspace_id', 'snapshot'},
              workflow_transcribe={'workspace_id', 'item_id', 'settings'})
FIELDS['workflow_subtitle_review']={'workspace_id','item_id','retry_failed'}
FIELDS['workflow_titles']={'workspace_id','item_id','force','cached_only'}
FIELDS['export_batch']={'plan'}
FIELDS['workflow_repair']={'workspace_id'}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@contextmanager
def _connection(database):
    database = str(Path(database).resolve())
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                config_path TEXT, paths_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','interrupted')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT,
                finished_at TEXT, worker_pid INTEGER, attempt INTEGER NOT NULL DEFAULT 0,
                error TEXT, result_json TEXT, log_path TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
            CREATE TABLE IF NOT EXISTS job_requests (
                request_key TEXT PRIMARY KEY, signature TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES jobs(id));
            CREATE TABLE IF NOT EXISTS job_cancellations (
                job_id TEXT PRIMARY KEY, requested_at TEXT NOT NULL);
        """)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


class WorkerLock:
    """OS-released file lock; a crash cannot leave a stale lock behind."""

    def __init__(self, database):
        self.path = Path(str(Path(database).resolve()) + ".worker.lock")
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            self.file.close()
            self.file = None
            return False

    def release(self):
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of death.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _decode(row):
    if row is None:
        return None
    item = dict(row)
    cancelled = item.pop('cancel_requested', False)
    if cancelled:
        item['status'] = 'cancelling' if item['status'] == 'running' else 'cancelled'
    for field in ("payload", "paths", "result"):
        value = item.pop(field + "_json")
        item[field] = json.loads(value) if value else None
    return item


def _validate_payload(kind, payload):
    if kind not in KINDS:
        raise ValueError(f"不支持的任务类型: {kind}")
    if not isinstance(payload, dict) or set(payload) - FIELDS[kind]:
        raise ValueError("任务参数包含未知字段；不能向队列保存 API 配置或任意命令")
    result = dict(payload)
    for field in ("path", "directory", "transcript_path"):
        if field in result and result[field]:
            if not isinstance(result[field], (str, Path)):
                raise ValueError(f"{field} 必须是本地路径")
            value = Path(result[field]).expanduser().resolve()
            if not value.exists() or (field == "directory" and not value.is_dir()) or (field != "directory" and not value.is_file()):
                raise ValueError(f"找不到{'目录' if field == 'directory' else '文件'}: {value}")
            result[field] = str(value)
    for field in ("skip_render", "mock", "allow_cpu", "recursive", "transcript_only", "semantic", "preview", "analyze",'retry_failed'):
        if field in result and not isinstance(result[field], bool):
            raise ValueError(f"{field} 必须是布尔值")
    if "audio_track" in result and (isinstance(result["audio_track"], bool) or not isinstance(result["audio_track"], int) or result["audio_track"] < 0):
        raise ValueError("audio_track 必须是非负整数")
    if "selection_mode" in result and result["selection_mode"] not in ("semantic", "hybrid", "audio"):
        raise ValueError("selection_mode 必须是 semantic、hybrid 或 audio")
    required = {"process": ("path",), "batch": ("directory",), "export": ("project_id",), "preview": ("project_id", "clip_id"),
                "plan": ("project_id", "options"), "plan_preview": ("plan_id", "draft_id"), "plan_export": ("plan_id",)}
    required.update(compose_sync=('composite_id',), compose_render=('composite_id', 'options'))
    required.update(compose_prepare=('match',), compose_visual=('composite_id',))
    required.update(workflow_prepare=('workspace_id',), workflow_preview=('workspace_id','item_id'),
                    workflow_export=('workspace_id',), workflow_transcribe=('workspace_id','item_id'))
    required['workflow_subtitle_review']=('workspace_id',)
    required['workflow_titles']=('workspace_id',)
    required['workflow_repair']=('workspace_id',)
    required['export_batch']=()
    if kind=='export_batch':
        from core.export_center import validate_plan
        validate_plan(result.get('plan'))
    if kind == 'workflow_titles' and 'force' in result and type(result['force']) is not bool:
        raise ValueError('force必须为布尔值')
    if kind == 'workflow_titles' and 'cached_only' in result and type(result['cached_only']) is not bool:
        raise ValueError('cached_only必须为布尔值')
    for field in required[kind]:
        if field == "options":
            if field not in result:
                raise ValueError("缺少剪辑方案设置")
            continue
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise ValueError(f"缺少任务参数: {field}")
    if kind in ("process", "batch") and result.get("mock") and not result.get("transcript_path"):
        raise ValueError("模拟模式需要已有转录 JSON")
    if "editing_options" in result or kind == "plan":
        from core.editing_options import normalize_editing_options
        name = "options" if kind == "plan" else "editing_options"
        result[name] = normalize_editing_options(result[name])
    if kind in ("export", "plan_export"):
        orientations = result.get("orientations", ["horizontal"])
        if not isinstance(orientations, (list, tuple)) or not orientations or any(o not in ("horizontal", "vertical") for o in orientations):
            raise ValueError("输出画幅必须是 horizontal 或 vertical")
        result["orientations"] = list(dict.fromkeys(orientations))
        if "export_options" in result:
            from core.export_options import normalize_export_options
            result["export_options"] = normalize_export_options(result["export_options"])
            intro = result["export_options"]["intro"]
            if intro["enabled"] and not Path(intro["path"]).is_file():
                raise ValueError(f"片头文件不存在: {intro['path']}")
    if kind == 'compose_prepare':
        from core.source_sequence import source_paths
        result['match'] = source_paths([result['match']])[0]
        result['commentator'] = source_paths(result.get('commentator'))
        for field in ('match_track', 'commentator_track'):
            value = result.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError('音轨必须是非负整数')
            result[field] = value
    if kind.startswith('workflow_'):
        if kind == 'workflow_export':
            from core.workflow import validate_snapshot
            validate_snapshot(result.get('snapshot', {}))
        elif kind in ('workflow_prepare', 'workflow_transcribe'):
            settings = result.get('settings', {})
            allowed = {'organization','form','asset_ids','match_id','commentator_ids','composite_id','composite_options',
                       'subtitle_source','audio_track','commentator_track','allow_cpu','mock','transcript_path','editing_options','name','prepare_only','batch_scope'}
            if not isinstance(settings, dict) or set(settings)-allowed:
                raise ValueError('工作台设置含未知字段，禁止保存 API 配置')
            result['settings'] = settings
    if kind == 'compose_visual':
        from core.visual_sync import normalize_search
        result['search'] = normalize_search(result.get('search'))
    if kind == 'compose_render':
        from core.composite_options import normalize_composite_options, validate_ranges, finite
        result['options'] = normalize_composite_options(result['options'])
        if result.get('segments') is not None:
            result['segments'] = validate_ranges(result['segments'], {'start': 0., 'end': 86400.})
        if result.get('preview_start') is not None:
            result['preview_start'] = finite(result['preview_start'], '预览起点', 0, 86400)
    return result


def submit_job(kind, payload, cfg, *, idempotency_key=None):
    payload = _validate_payload(kind, payload)
    database = str(Path(cfg["database"]).resolve())
    job_id = uuid.uuid4().hex
    log_path = Path(database).parent / "job_logs" / f"{job_id}.log"
    paths = {key: str(Path(cfg[key]).resolve()) for key in ("cache_dir", "output_dir") if cfg.get(key)}
    config_path = cfg.get("_config_path") or cfg.get("config_path")
    if config_path:
        config_path = str(Path(config_path).resolve())
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    paths_json = json.dumps(paths, ensure_ascii=False, sort_keys=True)
    with _connection(database) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            from core.utils import fingerprint
            signature = fingerprint([kind, payload_json, paths_json, config_path])
            previous = conn.execute('SELECT * FROM job_requests WHERE request_key=?', (idempotency_key,)).fetchone()
            if previous:
                if previous['signature'] != signature:
                    raise ValueError('幂等键已用于不同的任务参数')
                return get_job_after_cancel(previous['job_id'], conn)
        duplicate = conn.execute("""SELECT id FROM jobs WHERE kind=? AND payload_json=? AND paths_json=?
            AND config_path IS ? AND status IN ('queued','running')
            AND NOT EXISTS (SELECT 1 FROM job_cancellations c WHERE c.job_id=jobs.id)
            ORDER BY created_at LIMIT 1""",
            (kind, payload_json, paths_json, config_path)).fetchone()
        if duplicate:
            job_id = duplicate["id"]
        else:
            conn.execute("""INSERT INTO jobs(id,kind,payload_json,config_path,paths_json,status,created_at,updated_at,log_path)
                        VALUES(?,?,?,?,?,'queued',?,?,?)""",
                         (job_id, kind, payload_json, config_path, paths_json, _now(), _now(), str(log_path)))
        if idempotency_key:
            conn.execute('INSERT INTO job_requests VALUES(?,?,?)', (idempotency_key, signature, job_id))
    # If launch fails the queued record remains recoverable on the next UI start.
    try:
        ensure_worker(cfg)
    except OSError as exc:
        with _connection(database) as conn:
            conn.execute("UPDATE jobs SET error=?,updated_at=? WHERE id=?", (f"后台启动失败，可重新启动队列: {exc}", _now(), job_id))
    return get_job(job_id, cfg)


def _reconcile(conn):
    for row in conn.execute("SELECT id,worker_pid FROM jobs WHERE status='running'").fetchall():
        if not _pid_alive(row["worker_pid"]):
            cancelled = conn.execute('SELECT 1 FROM job_cancellations WHERE job_id=?', (row['id'],)).fetchone() is not None
            conn.execute("UPDATE jobs SET status='interrupted',error=?,finished_at=?,updated_at=? WHERE id=? AND status='running'",
                         ('用户已取消任务；缓存和审批记录已保留。' if cancelled else "后台进程已退出。缓存和审批记录已保留；可手动重试。", _now(), _now(), row["id"]))


def reconcile_jobs(cfg):
    lock = WorkerLock(cfg["database"])
    if not lock.acquire():
        return
    try:
        with _connection(cfg["database"]) as conn:
            _reconcile(conn)
    finally:
        lock.release()


def list_jobs(cfg, limit=100):
    reconcile_jobs(cfg)
    with _connection(cfg["database"]) as conn:
        return [_decode(row) for row in conn.execute("SELECT jobs.*, c.job_id AS cancel_requested FROM jobs LEFT JOIN job_cancellations c ON c.job_id=jobs.id ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit), 1000)),))]


def get_job(job_id, cfg):
    reconcile_jobs(cfg)
    with _connection(cfg["database"]) as conn:
        return _decode(conn.execute("SELECT jobs.*, c.job_id AS cancel_requested FROM jobs LEFT JOIN job_cancellations c ON c.job_id=jobs.id WHERE jobs.id=?", (job_id,)).fetchone())


def cancellation_requested(database, job_id):
    with closing(sqlite3.connect(str(database), timeout=5)) as conn:
        return conn.execute('SELECT 1 FROM job_cancellations WHERE job_id=?', (job_id,)).fetchone() is not None


def cancel_job(job_id, cfg):
    """Persist intent atomically; only the owning worker stops its own task tree."""
    with _connection(cfg['database']) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('找不到任务')
        if conn.execute('SELECT 1 FROM job_cancellations WHERE job_id=?', (job_id,)).fetchone():
            return get_job_after_cancel(job_id, conn)
        if row['status'] not in ('queued', 'running'):
            raise ValueError('任务已经结束，不能取消')
        conn.execute('INSERT INTO job_cancellations VALUES(?,?)', (job_id, _now()))
        if row['status'] == 'queued':
            conn.execute("UPDATE jobs SET status='interrupted',error='用户已取消任务',finished_at=?,updated_at=? WHERE id=?", (_now(), _now(), job_id))
        else:
            conn.execute("UPDATE jobs SET error='正在取消任务…',updated_at=? WHERE id=?", (_now(), job_id))
        return get_job_after_cancel(job_id, conn)


def get_job_after_cancel(job_id, conn):
    return _decode(conn.execute('SELECT jobs.*, c.job_id AS cancel_requested FROM jobs LEFT JOIN job_cancellations c ON c.job_id=jobs.id WHERE jobs.id=?', (job_id,)).fetchone())


def retry_job(job_id, cfg):
    reconcile_jobs(cfg)
    with _connection(cfg["database"]) as conn:
        conn.execute('BEGIN IMMEDIATE')
        cursor = conn.execute("""UPDATE jobs SET status='queued',error=NULL,result_json=NULL,worker_pid=NULL,
            started_at=NULL,finished_at=NULL,updated_at=? WHERE id=? AND status IN ('failed','interrupted')""", (_now(), job_id))
        if cursor.rowcount != 1:
            raise ValueError("只有失败或中断的任务可以重试")
        conn.execute('DELETE FROM job_cancellations WHERE job_id=?', (job_id,))
    ensure_worker(cfg)
    return get_job(job_id, cfg)


def ensure_worker(cfg):
    """Launch only when queued work exists and the OS worker lock is free."""
    database = str(Path(cfg["database"]).resolve())
    lock = WorkerLock(database)
    if not lock.acquire():
        return False
    try:
        with _connection(database) as conn:
            _reconcile(conn)
            if not conn.execute("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1").fetchone():
                return False
        options = {"cwd": str(ROOT), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                   "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        subprocess.Popen([sys.executable, str(ROOT / "scripts/job_worker.py"), "--database", database], **options)
        return True
    finally:
        lock.release()


def tail_job_log(job, cfg=None, limit=16000):
    if not job:
        return ""
    path = Path(job["log_path"])
    if not path.is_file():
        return "任务尚未开始，暂无日志。"
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - max(1024, min(int(limit), 64000))))
        return handle.read().decode("utf-8", errors="replace")


def _redact(text, secret=""):
    value = str(text)
    if secret:
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+", r"\1[REDACTED]", value)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)


class _SafeFormatter(logging.Formatter):
    def __init__(self, secret):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secret = secret

    def format(self, record):
        return _redact(super().format(record), self.secret)


class _LogStream:
    def write(self, value):
        if value.strip():
            logging.getLogger("worker.output").info(value.rstrip())
        return len(value)

    def flush(self):
        pass


def _execute(kind, payload, cfg):
    if kind=='export_batch':
        from core.export_center import run_export_batch
        return run_export_batch(payload['plan'],cfg)
    if kind=='workflow_repair':
        from core.workflow_quality import repair_previews
        return repair_previews(payload['workspace_id'],cfg)
    if kind.startswith('workflow_'):
        from core.workflow import prepare_workflow, preview_workflow, run_workflow_export, transcribe_workflow
        identity = payload['workspace_id']
        if kind == 'workflow_titles':
            from core.titles import generate_workspace_titles
            return generate_workspace_titles(identity,cfg,payload.get('item_id'),payload.get('force',False),payload.get('cached_only',False))
        if kind == 'workflow_prepare':
            return prepare_workflow(identity, payload['settings'], cfg, payload.get('expected_revision'))
        if kind == 'workflow_preview':
            return preview_workflow(identity, payload['item_id'], cfg)
        if kind == 'workflow_transcribe':
            return transcribe_workflow(identity, payload['item_id'], cfg, payload.get('settings'))
        if kind == 'workflow_subtitle_review':
            from core.workflow import review_workflow_subtitles
            return review_workflow_subtitles(identity,cfg,payload.get('item_id'),payload.get('retry_failed',False))
        return run_workflow_export(identity, payload['snapshot'], cfg)
    if kind == 'compose_prepare':
        from core.compositor import create_composite
        return create_composite(cfg=cfg, **payload)
    if kind == 'compose_visual':
        from core.compositor import synchronize_visual
        values = dict(payload)
        identity = values.pop('composite_id')
        return synchronize_visual(identity, cfg, **values)
    if kind in ('compose_sync', 'compose_render'):
        from core.compositor import synchronize_composite, render_composite
        values = dict(payload)
        identity = values.pop('composite_id')
        return (synchronize_composite if kind == 'compose_sync' else render_composite)(identity, cfg, **values)
    from core.pipeline import batch_process, export_project, process_video, render_preview
    if kind in ("plan", "plan_preview", "plan_export"):
        from core.editing import create_editing_plan, preview_plan_draft, export_editing_plan
        if kind == "plan":
            return create_editing_plan(payload["project_id"], cfg, payload["options"],
                                       mock=payload.get("mock", False), skip_render=payload.get("skip_render", True))
        if kind == "plan_preview":
            return preview_plan_draft(payload["plan_id"], payload["draft_id"], cfg)
        return export_editing_plan(payload["plan_id"], cfg, payload["orientations"], payload.get("export_options"))
    if kind == "process":
        values = dict(payload)
        if values.get("selection_mode"):
            cfg["selection"]["mode"] = values.pop("selection_mode")
        return process_video(values.pop("path"), cfg, **values)
    if kind == "batch":
        values = dict(payload)
        if values.get("selection_mode"):
            cfg["selection"]["mode"] = values.pop("selection_mode")
        return batch_process(values.pop("directory"), cfg, **values)
    if kind == "export":
        return export_project(payload["project_id"], cfg, orientations=tuple(payload["orientations"]), approved_only=True,
                              export_options=payload.get("export_options"))
    if kind == "preview":
        return {"project_id": payload["project_id"], "clip_id": payload["clip_id"],
                "preview_path": render_preview(payload["project_id"], payload["clip_id"], cfg)}
    raise ValueError("未知任务类型")


def _result_error(result):
    items = result if isinstance(result, list) else [result]
    failed = [item for item in items if isinstance(item, dict) and (item.get("errors") or item.get('needs_retry') or item.get("status") in ('failed','partial'))]
    return f"{len(failed)} 个处理结果含失败项。已完成的文件已保留，详情见结果；可重试恢复。" if failed else None


class JobCancelled(Exception):
    pass


def _stop_task(process):
    """Terminate only the process tree launched for this task, never the worker."""
    if process.poll() is not None:
        return
    if os.name == 'nt':
        # Direct process handles work for owned children when Windows denies
        # taskkill's process enumeration in a restricted desktop session.
        import psutil
        parent=None
        try:
            parent=psutil.Process(process.pid)
            parent.suspend()
            descendants=parent.children(recursive=True)
            for child in reversed(descendants):
                try: child.kill()
                except psutil.NoSuchProcess: pass
            parent.kill()
            _,alive=psutil.wait_procs(descendants+[parent],timeout=10)
            if alive: raise RuntimeError('任务子进程未完全停止，请重试取消')
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as error:
            if parent is not None:
                try: parent.resume()
                except psutil.Error: pass
            raise RuntimeError("无法完全取消任务，已恢复暂停的进程；请检查权限后重试") from error
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=15)


def _execute_isolated(job, cfg):
    if cancellation_requested(cfg['database'], job['id']):
        raise JobCancelled()
    output = Path(job['log_path']).with_suffix('.attempt-' + str(job['attempt']+1) + '.json')
    command = [sys.executable, str(ROOT/'scripts/job_task.py'), '--database', cfg['database'],
               '--job', job['id'], '--result', str(output)]
    options = dict(cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    process = subprocess.Popen(command, **options)
    try:
        while process.poll() is None:
            if cancellation_requested(cfg['database'], job['id']):
                _stop_task(process)
                raise JobCancelled()
            time.sleep(.25)
        if cancellation_requested(cfg['database'], job['id']):
            raise JobCancelled()
        if not output.is_file():
            raise RuntimeError('任务子进程退出，未返回结果；请查看日志后重试。')
        report = json.loads(output.read_text(encoding='utf-8'))
        if report.get('error'):
            raise RuntimeError(report['error'])
        if process.returncode:
            raise RuntimeError('任务子进程异常退出')
        return report.get('result')
    finally:
        _stop_task(process)


def run_task(database, job_id, output):
    """Private subprocess entry; reload secrets locally instead of serializing them."""
    from core.config import load_config
    from core.utils import atomic_json
    job = get_job(job_id, {'database': database})
    result, error, secret = None, None, ''
    handler = RotatingFileHandler(job['log_path'], maxBytes=1024*1024, backupCount=2, encoding='utf-8')
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        cfg = load_config(job['config_path'])
        cfg.update(job['paths'])
        cfg['database'] = database
        secret = cfg.get('openai', {}).get('api_key', '')
        handler.setFormatter(_SafeFormatter(secret))
        if cancellation_requested(database, job_id):
            raise JobCancelled()
        from core.budget_approval import budget_context, raise_if_budget_blocked
        with budget_context(job), redirect_stdout(_LogStream()), redirect_stderr(_LogStream()):
            result = _execute(job['kind'], job['payload'], cfg)
            raise_if_budget_blocked()
    except Exception as exc:
        error = _redact(str(exc), secret) or type(exc).__name__
        logging.getLogger(__name__).exception('任务执行结束：%s', error)
    finally:
        logger.removeHandler(handler)
        handler.close()
    report = json.loads(_redact(json.dumps({'result':result, 'error':error}, ensure_ascii=False, default=str), secret))
    atomic_json(output, report)
    return 1 if error else 0


def run_worker(cfg, *, isolate=False):
    """Drain the queue; a free OS lock plus a dead PID proves interruption."""
    database = str(Path(cfg["database"]).resolve())
    lock = WorkerLock(database)
    # The parent still holds the startup lock briefly after Popen.
    for _ in range(40):
        if lock.acquire():
            break
        time.sleep(0.05)
    else:
        return 0
    try:
        with _connection(database) as conn:
            _reconcile(conn)
        while True:
            with _connection(database) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if row is None:
                    # Release while holding the write transaction: an enqueue cannot
                    # commit between our empty check and the lock release.
                    lock.release()
                    return 0
                job = _decode(row)
                conn.execute("UPDATE jobs SET status='running',worker_pid=?,attempt=attempt+1,started_at=?,updated_at=?,error=NULL WHERE id=?",
                             (os.getpid(), _now(), _now(), job["id"]))
            secret, result, error = "", None, None
            path = Path(job["log_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
            root_log = logging.getLogger()
            previous_level = root_log.level
            root_log.setLevel(logging.INFO)
            root_log.addHandler(handler)
            try:
                from core.config import load_config
                current = load_config(job["config_path"])
                current.update(job["paths"])
                current["database"] = database
                secret = current.get("openai", {}).get("api_key", "")
                handler.setFormatter(_SafeFormatter(secret))
                logging.getLogger(__name__).info("任务 %s · %s · 第 %d 次执行", job["id"], job["kind"], job["attempt"] + 1)
                from core.budget_approval import budget_context, raise_if_budget_blocked
                with budget_context(job), redirect_stdout(_LogStream()), redirect_stderr(_LogStream()):
                    result = _execute_isolated(job, current) if isolate else _execute(job["kind"], job["payload"], current)
                    raise_if_budget_blocked()
                error = _result_error(result)
                logging.getLogger(__name__).info("%s", error or "任务完成")
            except JobCancelled:
                error = '用户已取消任务'
                logging.getLogger(__name__).info('用户已取消任务；停止当前任务及其子进程。')
            except Exception as exc:
                error = _redact(str(exc), secret)
                logging.getLogger(__name__).exception("任务失败，可修复后重试")
            finally:
                root_log.removeHandler(handler)
                handler.close()
                root_log.setLevel(previous_level)
            with _connection(database) as conn:
                conn.execute('BEGIN IMMEDIATE')
                cancelled = conn.execute('SELECT 1 FROM job_cancellations WHERE job_id=?', (job['id'],)).fetchone() is not None
                encoded = json.dumps(result, ensure_ascii=False, default=str) if result is not None else None
                conn.execute("UPDATE jobs SET status=?,result_json=?,error=?,finished_at=?,updated_at=? WHERE id=?",
                             ('interrupted' if cancelled else ("failed" if error else "completed"), _redact(encoded, secret) if encoded else None,
                              '用户已取消任务；已完成的缓存和文件保留。' if cancelled else error, _now(), _now(), job["id"]))
    finally:
        lock.release()

