"""Project-level LLM profiles shared by UI, CLI and workers."""
from __future__ import annotations
import copy
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

EFFORTS = ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
LABELS = {"default":"默认（不指定）", "none":"关闭 · none", "minimal":"最低 · minimal",
          "low":"低 · low", "medium":"中 · medium", "high":"高 · high",
          "xhigh":"很高 · xhigh", "max":"最高 · max", "ultra":"极高 · ultra"}
KEYS = {"FCF_LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
        "OPENAI_REASONING_EFFORT", "CODEX_CLI_PATH", "FCF_CODEX_HOME", "CODEX_MODEL",
        "CODEX_REASONING_EFFORT"}


def settings_path(root):
    primary = Path(os.environ.get("FCF_PRIMARY_ROOT") or root).resolve()
    return primary / ".env.llm"


def configured(options):
    return options.get("provider", "api") == "codex_cli" or bool(options.get("api_key") and options.get("model"))


def cache_identity(options):
    if options.get("provider", "api") == "api" and options.get("reasoning_effort", "default") == "default":
        return {}
    return {"llm_provider": options.get("provider", "api"),
            "reasoning_effort": options.get("reasoning_effort", "default"),
            "codex_home": options.get("codex_home", "")}


def apply_profiles(cfg, env):
    provider = env.get("FCF_LLM_PROVIDER", "api").strip() or "api"
    if provider not in ("api", "codex_cli"):
        raise ValueError("FCF_LLM_PROVIDER 应为 api 或 codex_cli")
    api = copy.deepcopy(cfg["openai"])
    api.update(provider="api", reasoning_effort=env.get("OPENAI_REASONING_EFFORT", "default").strip() or "default")
    codex = {"provider":"codex_cli", "model":env.get("CODEX_MODEL", "").strip(),
             "reasoning_effort":env.get("CODEX_REASONING_EFFORT", "default").strip() or "default",
             "codex_cli_path":env.get("CODEX_CLI_PATH", "").strip(),
             "codex_home":env.get("FCF_CODEX_HOME", "").strip()}
    for profile in (api, codex):
        if profile["reasoning_effort"] not in EFFORTS or (profile["provider"] == "api" and profile["reasoning_effort"] == "ultra"):
            raise ValueError("思考等级无效或该调用方式不支持")
    cfg["_llm_api_profile"] = api
    cfg["_llm_codex_profile"] = codex
    cfg["_llm_settings_path"] = str(settings_path(cfg["root"]))
    cfg["openai"] = copy.deepcopy(api)
    if provider == "codex_cli":
        cfg["openai"].update(codex)
        cfg["openai"].update(api_key="", base_url="codex-cli://chatgpt",
                             input_cost_per_million=0.0, output_cost_per_million=0.0)


def save_settings(cfg, provider, fields):
    from core.config import _dotenv
    from core.llm_client import _ledger_lock
    if provider not in ("api", "codex_cli"):
        raise ValueError("请选择调用来源")
    if set(fields) - KEYS:
        raise ValueError("包含未知 LLM 配置")
    cleaned = {}
    for key, value in fields.items():
        if not isinstance(value, str) or any(c in value for c in ("\n", "\r", "\x00")):
            raise ValueError("配置必须为单行文字")
        value = value.strip()
        if key.endswith("REASONING_EFFORT") and (value not in EFFORTS or (key.startswith("OPENAI") and value == "ultra")):
            raise ValueError("思考等级无效")
        if key == "OPENAI_API_KEY" and not value:
            continue
        if key == "OPENAI_BASE_URL":
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("API 地址需要完整的 http(s) 地址，且不含账号、查询参数或片段")
        cleaned[key] = value
    if provider == "api":
        if not cleaned.get("OPENAI_MODEL", cfg["_llm_api_profile"].get("model", "")):
            raise ValueError("请填写 API 模型名称")
        if not cleaned.get("OPENAI_API_KEY", cfg["_llm_api_profile"].get("api_key", "")):
            raise ValueError("请填写 API Key")
    path = Path(cfg["_llm_settings_path"])
    with _ledger_lock(path.with_suffix(".llm.lock")):
        values = {key:value for key,value in _dotenv(path).items() if key in KEYS}
        values.update(cleaned)
        values["FCF_LLM_PROVIDER"] = provider
        body = "# 网页 LLM 设置；优先于 .env 与启动进程中的 LLM 变量。\n"
        body += "".join(key+"="+chr(34)+value+chr(34)+"\n" for key,value in sorted(values.items()))
        fd, temporary = tempfile.mkstemp(prefix=".llm-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return path
