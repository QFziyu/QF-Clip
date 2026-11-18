"""Small OpenAI-compatible client with durable, conservative request budgets."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import random
import tempfile
import time
from typing import Callable
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """An API response could not be used safely."""


class LLMTimeoutError(LLMError):
    """A network timeout with a safe phase and timing diagnosis."""

    def __init__(self, timeout_seconds: float, elapsed_seconds: float, phase: str):
        self.timeout_seconds = float(timeout_seconds)
        self.elapsed_seconds = float(elapsed_seconds)
        self.phase = phase
        label = "等待响应头/首字节" if phase == "waiting_response_headers" else "读取响应内容"
        super().__init__(f"LLM 请求超时：{label}，已耗时 {elapsed_seconds:.1f} 秒，"
                         f"本次网络等待上限 {timeout_seconds:g} 秒；"
                         "已完成转录和选片窗口可复用")


class BudgetExceeded(LLMError):
    """The durable per-project budget would be exceeded."""


class HTTPStatusError(LLMError):
    def __init__(self, status: int, body: str):
        self.status, self.body = status, body[:1500]
        super().__init__(f"LLM HTTP {status}: {self.body}")


def atomic_json(path: str | Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _ledger_lock(path: Path):
    """Serialize reservations, including concurrent CLI/UI processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BudgetLedger:
    """All network attempts count; uncertain failures retain their reservation.

    Prices are user-supplied estimates, not provider billing guarantees. Input
    reservations use UTF-8 byte count plus overhead as a conservative token bound.
    A successful response with usage replaces its estimate with reported usage.
    """

    def __init__(self, path: str | Path, options: dict):
        self.path, self.options = Path(path), options

    def _read(self):
        if not self.path.exists():
            return {"version": 1, "attempts": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("attempts"), list):
                raise ValueError("missing attempts")
            for index, attempt in enumerate(value["attempts"]):
                if not isinstance(attempt, dict):
                    raise ValueError(f"attempt {index} is not an object")
                cost = attempt.get("cost_usd")
                if (isinstance(cost, bool) or not isinstance(cost, (int, float))
                        or not math.isfinite(cost) or cost < 0):
                    raise ValueError(f"attempt {index} has an invalid cost")
                if attempt.get("id", index) != index or attempt.get("status") not in {"reserved", "success", "failed"}:
                    raise ValueError(f"attempt {index} has invalid identity/status")
            return value
        except (ValueError, OSError) as error:
            raise LLMError(f"预算记录损坏，拒绝重置已用预算：{self.path}: {error}") from error

    def _cost(self, input_tokens, output_tokens):
        incoming = float(self.options.get("input_cost_per_million", 1.0))
        outgoing = float(self.options.get("output_cost_per_million", 4.0))
        if any(not math.isfinite(price) or price < 0 for price in (incoming, outgoing)):
            raise LLMError("API 输入和输出单价必须是非负有限数值")
        return (input_tokens * incoming + output_tokens * outgoing) / 1_000_000

    def reserve(self, payload: dict, *, timeout_seconds: float | None = None) -> int:
        estimate = self._cost(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 512,
                              int(self.options.get("max_output_tokens", 3000)))
        limit = float(self.options.get("budget_usd", 2.0))
        if not math.isfinite(estimate) or estimate < 0 or not math.isfinite(limit) or limit < 0:
            raise LLMError("API 预算和单价必须是非负有限数值")
        with _ledger_lock(self.path.with_suffix(".lock")):
            ledger = self._read()
            attempts = ledger["attempts"]
            spent = sum(float(item["cost_usd"]) for item in attempts)
            if len(attempts) >= int(self.options.get("max_calls", 80)):
                raise BudgetExceeded("本项目已达到 API 最大调用次数；已有缓存仍可使用")
            if spent + estimate > limit + 1e-12:
                from core.budget_approval import reservation_override
                approval = reservation_override(ledger, self.path, self.options, payload, spent, estimate, limit)
                if approval is None:
                    atomic_json(self.path, ledger)
                    raise BudgetExceeded(f"本次请求预计超过项目预算：已计 ${spent:.4f}，预留 ${estimate:.4f}，上限 ${limit:.4f}")
            else:
                approval = None
            index = len(attempts)
            attempts.append({"id": index, "at": datetime.now(timezone.utc).isoformat(),
                             "status": "reserved", "cost_usd": estimate, "estimated": True,
                             "model": payload.get("model", ""),
                             "request_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))})
            if timeout_seconds is not None:
                attempts[-1]["timeout_seconds"] = timeout_seconds
            if approval:
                attempts[-1]['budget_approval_id'] = approval
            atomic_json(self.path, ledger)
            return index

    def finish(self, index: int, status: str, usage: dict | None = None, *,
               elapsed_seconds: float | None = None, error_type: str | None = None,
               phase: str | None = None, http_status: int | None = None) -> None:
        with _ledger_lock(self.path.with_suffix(".lock")):
            ledger = self._read()
            entry = ledger["attempts"][index]
            entry["status"] = status
            if elapsed_seconds is not None:
                entry["elapsed_seconds"] = round(elapsed_seconds, 3)
            # Store classifications, never request headers, transcript, or raw error bodies.
            if error_type is not None:
                entry["error_type"] = error_type
            if phase is not None:
                entry["phase"] = phase
            if http_status is not None:
                entry["http_status"] = http_status
            if isinstance(usage, dict):
                incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
                if (isinstance(incoming, int) and not isinstance(incoming, bool) and incoming >= 0
                        and isinstance(outgoing, int) and not isinstance(outgoing, bool) and outgoing >= 0):
                    entry.update(cost_usd=self._cost(incoming, outgoing), estimated=False,
                                 input_tokens=incoming, output_tokens=outgoing)
            atomic_json(self.path, ledger)


def extract_json(content: str, required_key="clips") -> dict:
    """Accept a JSON object, a fenced object, or prose containing one object."""
    def reject_constant(value):
        raise ValueError(f"non-finite JSON number {value}")

    decoder = json.JSONDecoder(parse_constant=reject_constant)
    content = content.strip()
    # Deliberately do not eval JavaScript or repair/trust fabricated numeric data.
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            result, _ = decoder.raw_decode(content[index:])
            if isinstance(result, dict) and required_key in result:
                return result
        except (ValueError, json.JSONDecodeError):
            continue
    raise LLMError(f"LLM 输出中没有可解析的 {required_key} JSON 对象")


class LLMClient:
    def __init__(self, options: dict, ledger_path: str | Path,
                 transport: Callable[[dict], dict] | None = None, sleeper=time.sleep):
        self.options = options
        ledger_options = dict(options)
        if options.get("provider", "api") == "codex_cli":
            path = Path(ledger_path)
            ledger_path = path.with_name(path.stem + "_codex" + path.suffix)
            ledger_options.update(input_cost_per_million=0.0, output_cost_per_million=0.0)
        self.ledger = BudgetLedger(ledger_path, ledger_options)
        self.transport = transport or self._request
        self.sleeper = sleeper
        self._request_timeout = float(options.get("timeout", 180))

    def _request(self, payload: dict) -> dict:
        if self.options.get("provider", "api") == "codex_cli":
            from core.codex_cli import request
            return request(self.options, payload, self._request_timeout)
        api_key = str(self.options.get("api_key") or "").strip()
        if not api_key:
            raise LLMError("未配置 OPENAI_API_KEY；配置后重试，或使用显式 --mock 模拟模式")
        base = str(self.options.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        request = urllib.request.Request(endpoint, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
        started = time.monotonic()
        phase = "waiting_response_headers"
        try:
            with urllib.request.urlopen(request, timeout=self._request_timeout) as response:
                phase = "reading_response_body"
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise LLMError("LLM 响应过大")
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                detail = error.read(1500).decode("utf-8", errors="replace")
            except OSError:
                detail = "HTTP 错误内容读取失败"
            raise HTTPStatusError(error.code, detail) from error
        except TimeoutError as error:
            raise LLMTimeoutError(self._request_timeout, time.monotonic() - started, phase) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise LLMTimeoutError(self._request_timeout, time.monotonic() - started, phase) from error
            raise LLMError(f"LLM 请求失败：{type(error).__name__}: {error}") from error
        except (OSError, ValueError) as error:
            raise LLMError(f"LLM 请求失败：{type(error).__name__}: {error}") from error

    def complete(self, messages: list[dict], schema: dict, validator=None, response_key="clips") -> dict:
        from core.budget_approval import raise_if_budget_blocked
        raise_if_budget_blocked()
        is_codex = self.options.get("provider", "api") == "codex_cli"
        mode = "json_schema" if is_codex else str(self.options.get("response_format", "auto"))
        formats = ["json_schema", "json_object", "text"] if mode == "auto" else [mode]
        if mode in {"none", "off"}:
            formats = ["text"]
        format_index, retries = 0, 0
        token_parameter = "max_tokens"
        max_retries = max(0, min(10, int(self.options.get("max_retries", 2))))
        # Reset per window. Slow windows must not force all subsequent calls to wait longer.
        self._request_timeout = float(self.options.get("timeout", 180))
        max_timeout = float(self.options.get("max_timeout", max(600, self._request_timeout)))
        if (not math.isfinite(self._request_timeout) or self._request_timeout <= 0
                or not math.isfinite(max_timeout) or max_timeout < self._request_timeout):
            raise LLMError("API timeout 必须为正有限数值，max_timeout 必须不小于 timeout")
        last_error = None
        while True:
            payload = {"model": self.options.get("model") or ("Codex default" if is_codex else "gpt-4o-mini"), "messages": messages,
                       token_parameter: int(self.options.get("max_output_tokens", 3000))}
            effort = self.options.get("reasoning_effort", "default")
            if not is_codex and effort != "default":
                payload["reasoning_effort"] = effort
            selected_format = formats[format_index]
            if selected_format == "json_schema":
                payload["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "football_clips", "strict": True, "schema": schema}}
            elif selected_format == "json_object":
                payload["response_format"] = {"type": "json_object"}
            attempt = self.ledger.reserve(payload, timeout_seconds=self._request_timeout)
            started = time.monotonic()
            log.info("LLM 请求 #%d：模型 %s，格式 %s，网络等待上限 %g 秒，请求 %d 字节",
                     attempt + 1, payload["model"], selected_format, self._request_timeout,
                     len(json.dumps(payload, ensure_ascii=False).encode("utf-8")))
            response = None
            try:
                response = self.transport(payload)
                message = response["choices"][0]["message"]
                if message.get("refusal"):
                    raise LLMError("模型拒绝了本次片段分析")
                content = message.get("content", "")
                if isinstance(content, list):
                    content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
                if not isinstance(content, str):
                    raise LLMError("LLM content 不是文本")
                result = extract_json(content, response_key)
                if validator:
                    validator(result)
                elapsed = time.monotonic() - started
                self.ledger.finish(attempt, "success", response.get("usage"), elapsed_seconds=elapsed)
                log.info("LLM 请求 #%d 成功：耗时 %.1f 秒", attempt + 1, elapsed)
                return result
            except (LLMError, KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
                last_error = error
                self.ledger.finish(attempt, "failed", response.get("usage") if isinstance(response, dict) else None,
                                   elapsed_seconds=time.monotonic() - started, error_type=type(error).__name__,
                                   phase=getattr(error, "phase", None),
                                   http_status=error.status if isinstance(error, HTTPStatusError) else None)
                # Some reasoning-model gateways renamed max_tokens. The budget
                # remains reserved for both attempts, including this rejected one.
                if (isinstance(error, HTTPStatusError) and error.status in (400, 422)
                        and token_parameter == "max_tokens" and "max_tokens" in error.body.lower()
                        and any(word in error.body.lower() for word in ("unsupported", "not supported", "max_completion_tokens"))):
                    token_parameter = "max_completion_tokens"
                    continue
                unsupported = (isinstance(error, HTTPStatusError) and error.status in (400, 404, 422)
                               and any(word in error.body.lower() for word in
                                       ("response_format", "json_schema", "json_object", "structured", "unsupported")))
                if unsupported and format_index + 1 < len(formats):
                    format_index += 1
                    log.warning("API 不支持 %s，降级为 %s", selected_format, formats[format_index])
                    continue
                from core.codex_cli import CodexCLIError
                if isinstance(error, CodexCLIError):
                    break
                if isinstance(error, HTTPStatusError) and error.status in (400, 401, 403, 404, 422):
                    break
                if retries >= max_retries:
                    break
                retries += 1
                if isinstance(error, LLMTimeoutError):
                    self._request_timeout = min(max_timeout, self._request_timeout * 2)
                    log.warning("本窗口发生网络超时，下次等待上限调整为 %g 秒（最多 %g 秒）；不改变模型或选片窗口",
                                self._request_timeout, max_timeout)
                    delay = min(5 * 2 ** (retries - 1), 30) + random.uniform(0, 1)
                else:
                    delay = min(2 ** retries, 8)
                log.warning("LLM 请求/格式校验失败，将重试 %d/%d：%s", retries, max_retries, error)
                log.info("重试前等待 %.1f 秒；已完成窗口的缓存保留", delay)
                self.sleeper(delay)
        raise LLMError(f"LLM 分析在有限次数重试后失败：{last_error}") from last_error
