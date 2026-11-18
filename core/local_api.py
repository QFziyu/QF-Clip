"""Small authenticated loopback JSON server, without a web-framework dependency."""
from __future__ import annotations
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs


class ApiError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def make_server(port, token, dispatch, service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def handle_api(self):
            host = self.headers.get('Host', '')
            allowed = (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')
            if host not in allowed or self.headers.get('Origin'):
                return self.reply(403, {'error': '只允许已配对的本机服务访问'})
            if not hmac.compare_digest(self.headers.get('X-Workflow-Token', ''), token):
                return self.reply(401, {'error': '本机服务未配对'})
            try:
                body = {}
                if self.command == 'POST':
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 2 * 1024 * 1024:
                        raise ApiError('请求大小无效')
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ApiError('请求必须是 JSON 对象')
                parsed = urlsplit(self.path)
                if self.command == 'GET':
                    body = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
                result = dispatch(self.command, parsed.path, body)
                self.reply(200, result)
            except (ApiError, ValueError, KeyError, FileNotFoundError) as exc:
                self.reply(getattr(exc, 'code', 400), {'error': str(exc)})
            except Exception:
                self.reply(500, {'error': f'{service} 暂时无法处理请求，请查看本地任务记录'})

        do_GET = handle_api
        do_POST = handle_api

        def reply(self, code, value):
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server
