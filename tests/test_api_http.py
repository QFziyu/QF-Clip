"""Exercise the actual HTTP client against a local OpenAI-compatible stub."""
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

from core.clip_selector import CLIP_SCHEMA, validate_clip_payload
from core.llm_client import BudgetExceeded, LLMClient, LLMError, LLMTimeoutError


@contextmanager
def delayed_api(phase, delays):
    """A real local socket, with independently delayed headers or response body."""
    received = []
    request_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            with request_lock:
                index = len(received)
                received.append(self.path)
            delay = delays[min(index, len(delays) - 1)]
            data = json.dumps({"choices": [{"message": {"content": '{"clips":[]}'}}],
                               "usage": {"prompt_tokens": 12, "completion_tokens": 4}}).encode()
            try:
                if phase == "waiting_response_headers":
                    time.sleep(delay)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.flush()
                if phase == "reading_response_body":
                    time.sleep(delay)
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # A timeout deliberately disconnects clients from the first response.
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class HTTPClientTests(unittest.TestCase):
    def test_actual_http_serialization_auth_and_response(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                data = json.dumps({"choices": [{"message": {"content": '{"clips":[]}'}}],
                                   "usage": {"prompt_tokens": 40, "completion_tokens": 5}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ledger = Path(tmp) / "budget.json"
                options = {"api_key": "unit-test-placeholder", "model": "stub-model",
                           "base_url": f"http://127.0.0.1:{server.server_port}/v1", "max_retries": 0}
                client = LLMClient(options, ledger)
                result = client.complete([{"role": "user", "content": "请分析足球片段"}], CLIP_SCHEMA, validate_clip_payload)
                self.assertEqual(result, {"clips": []})
                self.assertEqual(requests[0]["path"], "/v1/chat/completions")
                self.assertEqual(requests[0]["auth"], "Bearer unit-test-placeholder")
                self.assertEqual(requests[0]["body"]["model"], "stub-model")
                self.assertTrue(requests[0]["body"]["response_format"]["json_schema"]["strict"])
                saved = json.loads(ledger.read_text(encoding="utf-8"))
                self.assertEqual(saved["attempts"][0]["input_tokens"], 40)
                self.assertFalse(saved["attempts"][0]["estimated"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_delayed_headers_and_body_retry_with_longer_timeout(self):
        for phase in ("waiting_response_headers", "reading_response_body"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp, \
                    delayed_api(phase, [0.25]) as (base_url, received):
                ledger = Path(tmp) / "budget.json"
                key = "local-timeout-test-secret"
                client = LLMClient({"api_key": key, "model": "slow-local-model", "base_url": base_url,
                                    "timeout": 0.15, "max_timeout": 0.6, "max_retries": 1}, ledger,
                                   sleeper=lambda _: None)
                errors = []
                real_request = client._request

                def transport(payload):
                    try:
                        return real_request(payload)
                    except LLMTimeoutError as error:
                        errors.append(error)
                        raise

                client.transport = transport
                with self.assertLogs("core.llm_client", level="WARNING") as logs:
                    result = client.complete([], CLIP_SCHEMA, validate_clip_payload)
                self.assertEqual(result, {"clips": []})
                self.assertEqual(len(received), 2)
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0].phase, phase)
                self.assertAlmostEqual(errors[0].timeout_seconds, 0.15)
                self.assertGreater(errors[0].elapsed_seconds, 0)
                saved_text = ledger.read_text(encoding="utf-8")
                entries = json.loads(saved_text)["attempts"]
                self.assertEqual([entry["status"] for entry in entries], ["failed", "success"])
                self.assertEqual([entry["timeout_seconds"] for entry in entries], [0.15, 0.3])
                self.assertEqual(entries[0]["error_type"], "LLMTimeoutError")
                self.assertEqual(entries[0]["phase"], phase)
                self.assertTrue(all(entry["elapsed_seconds"] > 0 for entry in entries))
                self.assertTrue(entries[0]["estimated"])
                self.assertFalse(entries[1]["estimated"])
                self.assertNotIn(key, saved_text + "\n".join(logs.output))
                self.assertNotIn("Authorization", saved_text)

    def test_repeated_real_timeouts_fail_finitely_and_keep_all_reservations(self):
        with tempfile.TemporaryDirectory() as tmp, \
                delayed_api("waiting_response_headers", [0.6]) as (base_url, received):
            ledger = Path(tmp) / "budget.json"
            client = LLMClient({"api_key": "local-test-only", "model": "slow-local-model",
                                "base_url": base_url, "timeout": 0.05, "max_timeout": 0.2,
                                "max_retries": 2}, ledger, sleeper=lambda _: None)
            with self.assertRaises(LLMError) as caught:
                client.complete([], CLIP_SCHEMA)
            self.assertIsInstance(caught.exception.__cause__, LLMTimeoutError)
            self.assertEqual(len(received), 3)
            entries = json.loads(ledger.read_text(encoding="utf-8"))["attempts"]
            self.assertEqual([entry["timeout_seconds"] for entry in entries], [0.05, 0.1, 0.2])
            self.assertTrue(all(entry["status"] == "failed" and entry["cost_usd"] > 0
                                and entry["estimated"] for entry in entries))

    def test_timeout_retries_cannot_bypass_call_budget(self):
        with tempfile.TemporaryDirectory() as tmp, \
                delayed_api("reading_response_body", [0.6]) as (base_url, received):
            ledger = Path(tmp) / "budget.json"
            client = LLMClient({"api_key": "local-test-only", "model": "slow-local-model",
                                "base_url": base_url, "timeout": 0.05, "max_timeout": 0.2,
                                "max_retries": 2, "max_calls": 2}, ledger, sleeper=lambda _: None)
            with self.assertRaises(BudgetExceeded):
                client.complete([], CLIP_SCHEMA)
            self.assertEqual(len(received), 2)
            entries = json.loads(ledger.read_text(encoding="utf-8"))["attempts"]
            self.assertEqual(len(entries), 2)
            self.assertTrue(all(entry["status"] == "failed" for entry in entries))


if __name__ == "__main__":
    unittest.main()
