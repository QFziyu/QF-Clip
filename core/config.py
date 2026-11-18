"""Validated configuration. Paths are relative to the project, never the shell cwd."""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULTS = {
    "output_dir": "output", "cache_dir": "cache", "database": "output/factory.sqlite3",
    "ffmpeg": "ffmpeg", "ffprobe": "ffprobe",
    "asr": {"model": "medium", "device": "cuda", "compute_type": "int8_float16",
            "engine": "faster_whisper", "local_url": "http://127.0.0.1:8765",
            "local_chunk_seconds": 60, "local_timeout": 120,
            "language": "zh", "chunk_seconds": 300, "overlap_seconds": 2,
            "vad_filter": True, "beam_size": 5},
    "openai": {"api_key": "", "base_url": "https://api.openai.com/v1", "model": "",
               "timeout": 180, "max_timeout": 600, "max_retries": 2, "max_calls": 80, "budget_usd": 2.0,
               "input_cost_per_million": 1.0, "output_cost_per_million": 4.0,
               "max_output_tokens": 3000, "response_format": "auto"},
    "selection": {"mode": "hybrid", "window_seconds": 360, "overlap_seconds": 60, "max_window_chars": 18000,
                  "context_before": 8, "context_after": 10, "min_seconds": 15,
                  "max_seconds": 240, "dedup_iou": 0.65, "max_candidates": 60,
                  "audio_events": True, "category_lengths": {
                      "goal_reaction": [15, 45], "humor": [20, 90],
                      "tactical_analysis": [45, 180], "halftime_summary": [60, 240]}},
    "render": {"encoder": "auto", "bitrate": "6M", "preview_bitrate": "700k",
               "horizontal": [1920, 1080], "vertical": [1080, 1920],
               "preview": [640, 360], "fps": 30, "burn_subtitles": True},
    "subtitle": {"font": "Microsoft YaHei", "font_size": 25.5, "vertical_font_size":25.5, "outline": 3, "shadow": 1,
                 "margin_v": 36, "vertical_margin_v":105, "max_chars": 18, "offset_y_width_ratio": 0.05},
    "title_generation": {"enabled": True, "default_platform": "douyin"},
    "subtitle_review": {"enabled":False,"chunk_cues":48,"max_chunk_chars":3200,"max_retries":1},
    "prompt_path": "prompts/clip_selection.md", "terms_path": "config/football_terms.txt",
}


def _merge(target, values):
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _dotenv(path: Path) -> dict:
    values = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def load_config(config_path=None) -> dict:
    import yaml

    cfg = copy.deepcopy(DEFAULTS)
    path = Path(config_path).resolve() if config_path else ROOT / "config/settings.yaml"
    values = {}
    if config_path and not path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    if path.exists():
        values = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        if not isinstance(values, dict):
            raise ValueError("配置文件必须是 YAML mapping")
        _merge(cfg, values)
    rules = ROOT / "config/clip_rules.yaml"
    if rules.exists():
        # Explicit settings override the shipped selection defaults.
        rule_values = yaml.safe_load(rules.read_text(encoding="utf-8-sig")) or {}
        selection = copy.deepcopy(DEFAULTS["selection"])
        _merge(selection, rule_values)
        if path.exists():
            _merge(selection, values.get("selection", {}))
        cfg["selection"] = selection
    env = _dotenv(ROOT / ".env")
    env.update(os.environ)
    # ASR has its own settings file; no LLM key is read or saved here.
    from core.asr_settings import settings_path as asr_settings_path, KEYS as ASR_KEYS
    env.update({key:value for key,value in _dotenv(asr_settings_path(ROOT)).items() if key in ASR_KEYS})
    # UI settings are reread by the UI, CLI and workers, overriding stale launcher variables.
    from core.llm_settings import settings_path, KEYS
    env.update({key:value for key,value in _dotenv(settings_path(ROOT)).items() if key in KEYS})
    mappings = {"OPENAI_API_KEY": ("openai", "api_key"), "OPENAI_BASE_URL": ("openai", "base_url"),
                "OPENAI_MODEL": ("openai", "model"), "WHISPER_MODEL": ("asr", "model"),
                "DEVICE": ("asr", "device"), "COMPUTE_TYPE": ("asr", "compute_type"),
                "ASR_ENGINE": ("asr", "engine"), "LOCAL_ASR_URL": ("asr", "local_url"),
                "FCF_RENDER_ENCODER": ("render", "encoder")}
    for name, (section, key) in mappings.items():
        if env.get(name, "").strip():
            cfg[section][key] = env[name].strip()
    if env.get('SUBTITLE_REVIEW_ENABLED', '').strip():
        if env['SUBTITLE_REVIEW_ENABLED'].lower().strip() not in ('true','false'):
            raise ValueError('SUBTITLE_REVIEW_ENABLED 必须是 true 或 false')
        cfg['subtitle_review']['enabled'] = env['SUBTITLE_REVIEW_ENABLED'].lower().strip() == 'true'
    for name, key in {"OPENAI_TIMEOUT": "timeout", "OPENAI_MAX_TIMEOUT": "max_timeout"}.items():
        if env.get(name, "").strip():
            try:
                cfg["openai"][key] = float(env[name].strip())
            except ValueError as error:
                raise ValueError(f"{name} 必须是正数（秒）") from error
    # Preserve older YAML configs which explicitly use a wait longer than the new default cap.
    if "max_timeout" not in values.get("openai", {}) and not env.get("OPENAI_MAX_TIMEOUT", "").strip():
        requested_timeout = cfg["openai"]["timeout"]
        if isinstance(requested_timeout, (float, int)) and not isinstance(requested_timeout, bool):
            cfg["openai"]["max_timeout"] = max(600, requested_timeout)
    for name, key in {"OUTPUT_DIR": "output_dir", "CACHE_DIR": "cache_dir",
                      "FFMPEG_PATH": "ffmpeg", "FFPROBE_PATH": "ffprobe"}.items():
        if env.get(name, "").strip():
            cfg[key] = env[name].strip()
    if env.get("OUTPUT_DIR", "").strip() and not env.get("DATABASE_PATH"):
        cfg["database"] = str(Path(cfg["output_dir"]) / "factory.sqlite3")
    if env.get("DATABASE_PATH", "").strip():
        cfg["database"] = env["DATABASE_PATH"].strip()
    cfg["root"] = str(ROOT)
    cfg["_config_path"] = str(path)
    for key in ("output_dir", "cache_dir", "database", "prompt_path", "terms_path"):
        p = Path(cfg[key]).expanduser()
        cfg[key] = str((ROOT / p).resolve() if not p.is_absolute() else p.resolve())
    terms_file = Path(cfg["terms_path"])
    cfg["terms"] = [s.strip() for s in terms_file.read_text(encoding="utf-8-sig").splitlines()
                    if s.strip() and not s.startswith("#")] if terms_file.exists() else []
    for section, fields in {"asr": ["chunk_seconds", "beam_size", "local_chunk_seconds", "local_timeout"],
                            "selection": ["window_seconds", "max_window_chars", "min_seconds", "max_seconds", "max_candidates"],
                            "openai": ["timeout", "max_timeout", "max_calls", "budget_usd", "max_output_tokens"]}.items():
        for field in fields:
            v = cfg[section][field]
            if isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0:
                raise ValueError(f"{section}.{field} 必须是正数")
    if cfg["openai"]["max_timeout"] < cfg["openai"]["timeout"]:
        raise ValueError("openai.max_timeout 必须不小于 openai.timeout")
    for section, window in (("selection", "window_seconds"), ("asr", "chunk_seconds")):
        overlap = cfg[section]["overlap_seconds"]
        if not 0 <= overlap < cfg[section][window]:
            raise ValueError(f"{section}.overlap_seconds 必须小于 {window}")
    if cfg["selection"]["min_seconds"] > cfg["selection"]["max_seconds"]:
        raise ValueError("min_seconds 不能大于 max_seconds")
    if cfg["asr"]["device"] not in ("cuda", "cpu", "auto"):
        raise ValueError("DEVICE 应为 cuda、cpu 或 auto")
    if cfg['asr']['engine'] not in ('faster_whisper','zipformer_http'):
        raise ValueError('ASR_ENGINE 应为 faster_whisper 或 zipformer_http')
    from core.local_asr import validate_url
    validate_url(cfg['asr']['local_url'])
    if not cfg['asr']['overlap_seconds'] < cfg['asr']['local_chunk_seconds'] <= 900:
        raise ValueError('asr.local_chunk_seconds 必须大于重叠时长且不超过 900 秒')
    if cfg["openai"]["max_retries"] < 0:
        raise ValueError("max_retries 不能为负数")
    if cfg["selection"]["mode"] not in ("semantic", "hybrid", "audio"):
        raise ValueError("selection.mode 应为 semantic、hybrid 或 audio")
    for section, keys in {"asr": ("beam_size",), "selection": ("max_window_chars", "max_candidates"),
                          "openai": ("max_calls", "max_output_tokens", "max_retries")}.items():
        for key in keys:
            value = cfg[section][key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{section}.{key} 必须是整数")
    for section, keys in {"selection": ("context_before", "context_after"),
                          "openai": ("input_cost_per_million", "output_cost_per_million")}.items():
        for key in keys:
            value = cfg[section][key]
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{section}.{key} 必须是非负有限数值")
    if not 0 < cfg["selection"]["dedup_iou"] <= 1:
        raise ValueError("selection.dedup_iou 应在 0～1 之间")
    for category, bounds in cfg["selection"].get("category_lengths", {}).items():
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or any(
            isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0 for v in bounds
        ) or bounds[0] > bounds[1]:
            raise ValueError(f"selection.category_lengths.{category} 必须是 [最短秒数, 最长秒数]")
    if cfg["render"]["encoder"] not in ("auto", "libx264", "h264_nvenc"):
        raise ValueError("render.encoder 应为 auto、libx264 或 h264_nvenc")
    fps = cfg["render"]["fps"]
    if isinstance(fps, bool) or not isinstance(fps, (float, int)) or not math.isfinite(fps) or not 1 <= fps <= 120:
        raise ValueError("render.fps 必须在 1～120 之间")
    for key in ("horizontal", "vertical", "preview"):
        dims = cfg["render"][key]
        if len(dims) != 2 or any(not isinstance(n, int) or n <= 0 or n % 2 for n in dims):
            raise ValueError(f"render.{key} 需要两个正偶数")
    review=cfg['subtitle_review']
    if not isinstance(review,dict) or type(review.get('enabled')) is not bool:
        raise ValueError('subtitle_review.enabled 必须是布尔值')
    for key,low,high in [('chunk_cues',1,100),('max_chunk_chars',100,12000),('max_retries',0,2)]:
        if type(review.get(key)) is not int or not low<=review[key]<=high:
            raise ValueError(f'subtitle_review.{key} 必须是 {low}～{high} 的整数')
    from core.llm_settings import apply_profiles
    title = cfg['title_generation']
    if not isinstance(title,dict) or type(title.get('enabled')) is not bool or title.get('default_platform') not in ('douyin','bilibili'):
        raise ValueError('title_generation需要有效enabled和default_platform设置')
    apply_profiles(cfg, env)
    return cfg
