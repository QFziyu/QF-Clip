"""Small, serializable export preferences; queue jobs store a validated snapshot."""
from __future__ import annotations

import copy
from pathlib import Path


SUBTITLE_FIELDS = frozenset({
    "preset", "font", "font_size", "vertical_font_size", "primary_color", "outline_color",
    "back_color", "back_alpha", "outline", "shadow", "bold", "italic", "spacing",
    "border_style", "margin_v", "vertical_margin_v", "alignment", "max_chars", "offset_y_width_ratio",
})


def normalize_export_options(options: dict | None = None) -> dict:
    from core.subtitle_styles import resolve_subtitle_settings

    options = {} if options is None else options
    if not isinstance(options, dict) or set(options) - {"subtitle", "burn_subtitles", "intro"}:
        raise ValueError("导出设置包含未知字段。")
    style = options.get("subtitle", {})
    if not isinstance(style, dict) or set(style) - SUBTITLE_FIELDS:
        raise ValueError("字幕设置包含未知字段。")
    burn = options.get("burn_subtitles", True)
    if not isinstance(burn, bool):
        raise ValueError("burn_subtitles 必须是布尔值。")
    intro = options.get("intro", {})
    if not isinstance(intro, dict) or set(intro) - {"enabled", "path"}:
        raise ValueError("片头设置包含未知字段。")
    enabled, path = intro.get("enabled", False), intro.get("path", "")
    if not isinstance(enabled, bool) or not isinstance(path, str) or "\0" in path:
        raise ValueError("片头需要有效的启用状态和本地文件路径。")
    path = path.strip().strip('"')
    if enabled and not path:
        raise ValueError("启用片头时请选择自己的片头视频。")
    if path:
        path = str(Path(path).expanduser().resolve())
    resolved = resolve_subtitle_settings(style)
    return {"subtitle": {k: v for k, v in resolved.items() if k in SUBTITLE_FIELDS},
            "burn_subtitles": burn, "intro": {"enabled": enabled, "path": path}}


def default_export_options(cfg: dict) -> dict:
    return normalize_export_options({"subtitle": cfg.get("subtitle", {}),
                                     "burn_subtitles": cfg.get("render", {}).get("burn_subtitles", True)})


def effective_export_config(cfg: dict, options: dict) -> dict:
    result = copy.deepcopy(cfg)
    options = normalize_export_options(options)
    result.setdefault("subtitle", {}).update(options["subtitle"])
    result.setdefault("render", {})["burn_subtitles"] = options["burn_subtitles"]
    return result


def project_export_options(db, project_id: str, cfg: dict, override: dict | None = None) -> dict:
    if override is not None:
        return normalize_export_options(override)
    saved = db.get_export_options(project_id)
    return normalize_export_options(saved) if saved else default_export_options(cfg)
