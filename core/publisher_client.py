"""Paired HTTP boundary to the independent publisher; no browser/session imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
from urllib import request, error, parse

from core.utils import atomic_json, fingerprint, project_lock


def runtime_profile(cfg):
    release = os.environ.get('FCF_RELEASE_VERSION', '')
    return 'cpu' if release.endswith('-cpu') or cfg.get('asr', {}).get('device') == 'cpu' else 'gpu'


def connection_path(cfg):
    return Path(cfg['output_dir']) / '.automation' / 'connection.json'


def ensure_configuration(cfg, profile=None):
    """Extend a trusted local connection file, preserving all existing pairing."""
    path = connection_path(cfg)
    profile = profile or runtime_profile(cfg)
    with project_lock(path.parent / '.publisher-config'):
        data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'profile': profile}
        if data.get('profile') != profile:
            raise RuntimeError('发布服务与当前 GPU/CPU 数据环境不一致')
        for name, port in (('factory', 17655), ('publisher', 17656)):
            data.setdefault(name, {'url': f'http://127.0.0.1:{port}', 'port': port, 'token': secrets.token_urlsafe(32)})
        data.setdefault('download_roots', [])
        data.setdefault('export_roots', [])
        output = str(Path(cfg['output_dir']).resolve())
        if output not in data['export_roots']:
            data['export_roots'].append(output)
        atomic_json(path, data)
    return data


class PublisherClient:
    def __init__(self, cfg, timeout=3):
        self.cfg, self.timeout = cfg, timeout
        self.path = connection_path(cfg)
        try:
            self.connection = json.loads(self.path.read_text(encoding='utf-8'))
            self.endpoint = self.connection['publisher']
            parsed = parse.urlsplit(self.endpoint['url'])
            if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
                    or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                    or not self.endpoint.get('token')):
                raise ValueError('endpoint')
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError('发布服务尚未配对，请点击“启动发布服务”或运行发布启动入口') from None
        self.profile = self.connection.get('profile')
        if self.profile != runtime_profile(cfg):
            raise RuntimeError('发布连接属于其他 GPU/CPU 数据环境，请使用对应启动入口')

    def call(self, path, data=None, timeout=None):
        req = request.Request(self.endpoint['url'] + '/api/v1/' + path,
            data=json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8') if data is not None else None,
            headers={'X-Workflow-Token': self.endpoint['token'], 'Content-Type': 'application/json'})
        try:
            with request.build_opener(request.ProxyHandler({})).open(req, timeout=timeout or self.timeout) as response:
                value = json.load(response)
        except error.HTTPError as exc:
            try:
                message = json.load(exc).get('error', '发布请求被拒绝')
                if isinstance(message, dict):
                    message = message.get('message', '发布请求被拒绝')
                message = str(message)[:400]
                if self.endpoint['token'] in message or any(s in message.lower() for s in ('bearer ', 'cookie:', 'authorization:')):
                    message = '发布请求被拒绝，请查看发布任务状态'
            except (OSError, ValueError, AttributeError):
                message = '发布请求被拒绝，请检查账号、成品及任务状态'
            raise RuntimeError(message) from None
        except (OSError, ValueError):
            raise RuntimeError('发布服务未响应；本地成品已保留，可启动服务后恢复') from None
        if not isinstance(value, dict):
            raise RuntimeError('发布服务返回格式无效')
        return value

    def health(self):
        value = self.call('health')
        if value.get('service') != 'football-publisher' or value.get('api_version') != 1 or value.get('profile') != self.profile:
            raise RuntimeError('发布组件版本或 GPU/CPU 环境不匹配')
        return value

    def accounts(self):
        return self.call('accounts').get('accounts', [])

    def connect_account(self, platform='douyin'):
        return self.call('accounts/connect', {'platform': platform}, timeout=35)

    def refresh_account(self, account_id, expected_creator_id=None):
        data = {}
        if expected_creator_id is not None:
            data['expected_creator_id'] = expected_creator_id
        return self.call('accounts/' + parse.quote(account_id, safe='') + '/refresh', data, timeout=20)

    def calibrate_account(self, account_id, sample_job_id):
        return self.call('accounts/' + parse.quote(account_id, safe='') + '/calibrate', {
            'sample_job_id': sample_job_id,
            'acknowledge': 'I confirmed this account and prepared sample; enable automatic submission'})

    def jobs(self, workspace_id=None):
        path = 'publish-jobs'
        if workspace_id:
            path += '?' + parse.urlencode({'workspace_id': workspace_id})
        return self.call(path).get('jobs', [])

    def get_job(self, identity):
        return self.call('publish-jobs/' + parse.quote(identity, safe='')).get('job')

    def retry_job(self, identity):
        return self.call('publish-jobs/' + parse.quote(identity, safe='') + '/retry', {})

    def cancel_job(self, identity):
        return self.call('publish-jobs/' + parse.quote(identity, safe='') + '/cancel', {})

    def verify_job(self, identity, remote_id, url=None):
        data = {'remote_id': remote_id}
        if url:
            data['url'] = url
        return self.call('publish-jobs/' + parse.quote(identity, safe='') + '/verify', data, timeout=20)

    def submit_job(self, delivery, publication):
        if delivery.get('verified') is not True or delivery.get('profile') != self.profile:
            raise RuntimeError('只允许提交当前环境已校验的正式成品')
        # These paths originate in the authenticated factory's immutable delivery,
        # after the operator selected the export folder and optional cover.
        roots = [str(Path(delivery['media']['path']).resolve().parent)]
        if publication.get('cover_path'):
            roots.append(str(Path(publication['cover_path']).resolve().parent))
        with project_lock(self.path.parent / '.publisher-config'):
            data = json.loads(self.path.read_text(encoding='utf-8'))
            allowed = data.setdefault('export_roots', [])
            for root in roots:
                if root not in allowed:
                    allowed.append(root)
            atomic_json(self.path, data)
        key = 'factory:' + self.profile + ':' + delivery['delivery_id'] + ':' + fingerprint(publication)
        return self.call('publish-jobs', {'idempotency_key': key, 'delivery': delivery, 'publication': publication}).get('job')


def start_publishing_services(cfg):
    primary = Path(os.environ.get('FCF_PRIMARY_ROOT') or cfg['root']).resolve()
    command = [str(primary / '.venv/Scripts/python.exe'), str(primary / 'scripts/launch_publishing.py'),
               '--profile', runtime_profile(cfg), '--services-only', '--no-browser']
    try:
        result = subprocess.run(command, cwd=primary, capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=55,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('发布服务未能及时启动，请检查发布启动日志后重试') from None
    if result.returncode:
        raise RuntimeError('发布服务启动失败，请查看 output/.automation 的启动日志；端口可能被其他环境占用')
    return {'started': True, 'profile': runtime_profile(cfg)}
