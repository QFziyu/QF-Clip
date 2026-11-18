"""One-request budget grants, scoped to a job, ledger and reservation index."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

_job = ContextVar('budget_job', default=None)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def notice_path(job):
    return Path(job['log_path']).with_suffix('.budget.json')


@contextmanager
def budget_context(job):
    token = _job.set({'job':job, 'blocked':None})
    try:
        yield
    finally:
        _job.reset(token)


def raise_if_budget_blocked():
    context = _job.get()
    request = context.get('blocked') if context else None
    if request:
        from core.llm_client import BudgetExceeded
        raise BudgetExceeded(f"本次请求预计超过项目预算：已计 ${request['spent_usd']:.4f}，预留 ${request['estimate_usd']:.4f}，上限 ${request['limit_usd']:.4f}")


def reservation_override(ledger, path, options, payload, spent, estimate, limit):
    """Called under the ledger lock. Consumption is committed with the reservation."""
    context = _job.get()
    if not context:
        return None
    job = context['job']
    request_hash = _digest(payload)
    for grant in ledger.get('budget_approvals', []):
        if (grant['job_id'] == job['id'] and not grant.get('consumed_at')
                and grant['attempt_index'] == len(ledger['attempts'])
                and abs(grant['spent_usd'] - spent) < 1e-9
                and abs(grant['limit_usd'] - limit) < 1e-9
                and grant['model'] == payload.get('model', '')
                and (grant['request_hash'] is None or grant['request_hash'] == request_hash)
                and estimate <= grant['estimate_usd'] + 1e-12):
            grant['consumed_at'] = _now()
            return grant['request_id']
    request = dict(job_id=job['id'], ledger_path=str(Path(path).resolve()),
                   attempt_index=len(ledger['attempts']), request_hash=request_hash,
                   spent_usd=spent, estimate_usd=estimate, limit_usd=limit,
                   model=payload.get('model', ''), provider=options.get('provider', 'api'))
    request['request_id'] = _digest(request)
    request['created_at'] = _now()
    context['blocked'] = request
    ledger.setdefault('budget_requests', {})[job['id']] = request
    from core.llm_client import atomic_json
    atomic_json(notice_path(job), request)
    return None


def _ledger_path(path, cfg):
    path = Path(path).resolve()
    if not any(path.is_relative_to(Path(cfg[key]).resolve()) for key in ('cache_dir', 'output_dir') if cfg.get(key)):
        raise ValueError('预算记录不属于当前项目目录')
    return path


def _legacy_request(job, cfg):
    """Recover a bounded next-request grant for failures made before notices existed."""
    match = re.search(r'已计\s*\$?([\d.]+).*预留\s*\$?([\d.]+).*上限\s*\$?([\d.]+)', job.get('error') or '')
    if not match:
        return None
    spent_label, estimate_label, limit = map(float, match.groups())
    if not all(math.isfinite(v) and v >= 0 for v in (spent_label, estimate_label, limit)):
        return None
    from storage.workspaces import WorkspaceStore
    payload = job['payload']
    directories = set()
    with WorkspaceStore(cfg['database']) as store:
        if payload.get('project_id'):
            project = store.get_project(payload['project_id'])
            if project:
                directories.add(project['project_dir'])
        if payload.get('workspace_id'):
            w = store.get_workspace(payload['workspace_id'])
            if w:
                sources = {str(Path(a['path']).resolve()) for a in w['assets']}
                directories.update(p['project_dir'] for p in store.list_projects()
                                   if str(Path(p['source']).resolve()) in sources)
                for variant in w.get('variants', []):
                    for item in variant['items']:
                        if item.get('source', {}).get('project_dir'):
                            directories.add(item['source']['project_dir'])
    candidates = []
    from core.llm_client import BudgetLedger
    for directory in directories:
        try:
            path = _ledger_path(Path(directory)/'selection/api_budget.json', cfg)
            if not path.is_file():
                continue
            ledger = BudgetLedger(path, cfg['openai'])._read()
            spent = sum(a['cost_usd'] for a in ledger['attempts'])
            if abs(spent - spent_label) > .000051 or not ledger['attempts']:
                continue
            # Old errors rounded to four decimals. Bound the allowance conservatively.
            request = dict(job_id=job['id'], ledger_path=str(path), attempt_index=len(ledger['attempts']),
                           request_hash=None, spent_usd=spent, estimate_usd=estimate_label+.0001,
                           limit_usd=limit, model=ledger['attempts'][-1].get('model', ''), provider='api', legacy=True)
            request['request_id'] = _digest(request)
            candidates.append(request)
        except (OSError, ValueError, RuntimeError):
            continue
    return candidates[0] if len(candidates) == 1 else None


def budget_request(job, cfg):
    if job.get('status') not in ('failed', 'interrupted'):
        return None
    if '最大调用次数' in (job.get('error') or ''):
        return None
    try:
        path = notice_path(job)
        request = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else _legacy_request(job, cfg)
        if not request or request['job_id'] != job['id']:
            return None
        ledger_path = _ledger_path(request['ledger_path'], cfg)
        from core.llm_client import BudgetLedger
        ledger = BudgetLedger(ledger_path, cfg['openai'])._read()
        if len(ledger['attempts']) != request['attempt_index']:
            return None
        if any(g['request_id'] == request['request_id'] and g.get('consumed_at') for g in ledger.get('budget_approvals', [])):
            return None
        return request
    except (OSError, ValueError, KeyError, RuntimeError):
        return None


def effective_options(job, cfg):
    from core.config import load_config
    return load_config(job['config_path'])['openai'] if job.get('config_path') else cfg['openai']


def resume_with_current(job_id, cfg):
    from core.jobs import get_job, retry_job, _connection
    job = get_job(job_id, cfg)
    if not job or job['status'] not in ('failed', 'interrupted'):
        raise ValueError('任务已不在等待恢复的状态，请刷新')
    if job['kind'] == 'workflow_subtitle_review':
        payload = {**job['payload'], 'retry_failed':True}
        with _connection(cfg['database']) as conn:
            conn.execute("UPDATE jobs SET payload_json=? WHERE id=? AND status IN ('failed','interrupted')",
                         (json.dumps(payload, ensure_ascii=False, sort_keys=True), job_id))
    return retry_job(job_id, cfg)


def approve_and_retry(job_id, request_id, cfg):
    from core.jobs import get_job, retry_job
    from core.llm_client import BudgetLedger, _ledger_lock, atomic_json
    job = get_job(job_id, cfg)
    request = budget_request(job, cfg) if job else None
    if not request or request['request_id'] != request_id:
        raise ValueError('预算申请已变化或任务已结束，请刷新后重新检查')
    options = effective_options(job, cfg)
    if options.get('provider', 'api') != request['provider'] or options.get('model', '') != request['model']:
        raise ValueError('模型来源已变化，请先重试任务，按新配置生成预算申请')
    if abs(float(options.get('budget_usd', 2)) - request['limit_usd']) > 1e-9:
        raise ValueError('项目预算配置已变化，请先重试任务')
    path = _ledger_path(request['ledger_path'], cfg)
    with _ledger_lock(path.with_suffix('.lock')):
        ledger = BudgetLedger(path, cfg['openai'])._read()
        spent = sum(a['cost_usd'] for a in ledger['attempts'])
        if len(ledger['attempts']) >= int(options.get('max_calls', 80)):
            raise ValueError('当前任务已达到最大调用次数，本窗口只批准金额超限')
        if len(ledger['attempts']) != request['attempt_index'] or abs(spent-request['spent_usd']) > 1e-9:
            raise ValueError('预算记录已变化，请刷新后重新检查')
        grants = ledger.setdefault('budget_approvals', [])
        if not any(g['request_id'] == request_id for g in grants):
            grants.append({**request, 'approved_at':_now(), 'consumed_at':None})
        atomic_json(path, ledger)
    return resume_with_current(job_id, cfg)
