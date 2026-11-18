"""Local paired boundary: no platform session or paid API is used."""
import json
import os
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

from core.publisher_client import PublisherClient, ensure_configuration


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='发布客户端-')
        self.root = Path(self.temp.name)
        self.cfg = {'output_dir': str(self.root/'output'), 'asr': {'device': 'cuda'}}
        self.calls = []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self): self.respond()
            def do_POST(self): self.respond()
            def respond(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                outer.calls.append((self.path, self.headers.get('X-Workflow-Token'), body))
                value = {'service': 'football-publisher', 'api_version': 1, 'profile': 'gpu',
                         'job': {'id': 'one'}, 'accounts': [], 'jobs': []}
                raw = json.dumps(value).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        with patch.dict(os.environ, {'FCF_RELEASE_VERSION': 'v1.7.0-gpu'}):
            data = ensure_configuration(self.cfg)
        self.token = data['publisher']['token']
        data['publisher'].update(url=f'http://127.0.0.1:{self.server.server_port}', port=self.server.server_port)
        self.path = self.root/'output/.automation/connection.json'
        self.path.write_text(json.dumps(data), encoding='utf-8')
        self.env = patch.dict(os.environ, {'FCF_RELEASE_VERSION': 'v1.7.0-gpu'}); self.env.start()
    def tearDown(self):
        self.env.stop(); self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()
    def test_migration_preserves_other_components_and_tokens(self):
        data = json.loads(self.path.read_text()); data['hub'] = {'token': 'keep'}
        self.path.write_text(json.dumps(data))
        updated = ensure_configuration(self.cfg)
        self.assertEqual(updated['publisher']['token'], self.token)
        self.assertEqual(updated['hub'], {'token': 'keep'})
    def test_http_and_idempotency_are_profile_scoped(self):
        client = PublisherClient(self.cfg); self.assertEqual(client.health()['api_version'], 1)
        delivery = {'verified': True, 'profile': 'gpu', 'delivery_id': 'd1', 'media': {'path': str(self.root/'成品/人工标题.mp4')}}
        intent = {'platform': 'douyin', 'account_id': 'a1'}
        for _ in range(2): self.assertEqual(client.submit_job(delivery, intent)['id'], 'one')
        self.assertEqual(self.calls[-1][2]['idempotency_key'], self.calls[-2][2]['idempotency_key'])
        self.assertTrue(self.calls[-1][2]['idempotency_key'].startswith('factory:gpu:d1:'))
        self.assertTrue(all(call[1] == self.token for call in self.calls))
        self.assertIn(str((self.root/'成品').resolve()), json.loads(self.path.read_text())['export_roots'])
        client.verify_job('one', '123456', 'https://www.douyin.com/video/123456')
        self.assertEqual(self.calls[-1][2]['remote_id'], '123456')
    def test_profile_and_unverified_media_are_rejected_before_request(self):
        client = PublisherClient(self.cfg)
        with self.assertRaises(RuntimeError): client.submit_job({'verified': False, 'profile': 'gpu'}, {})
        self.assertFalse(self.calls)
        with patch.dict(os.environ, {'FCF_RELEASE_VERSION': 'v1.7.0-cpu'}):
            with self.assertRaises(RuntimeError): PublisherClient(self.cfg)
    def test_non_loopback_endpoint_is_rejected_without_credentials_in_error(self):
        data = json.loads(self.path.read_text()); data['publisher']['url'] = 'https://example.com'
        self.path.write_text(json.dumps(data))
        with self.assertRaises(RuntimeError) as caught: PublisherClient(self.cfg)
        self.assertNotIn(self.token, str(caught.exception))
    def test_offline_service_error_does_not_expose_pairing(self):
        self.server.shutdown(); self.server.server_close()
        with self.assertRaises(RuntimeError) as caught: PublisherClient(self.cfg, timeout=.2).health()
        self.assertNotIn(self.token, str(caught.exception))


if __name__ == '__main__': unittest.main()
