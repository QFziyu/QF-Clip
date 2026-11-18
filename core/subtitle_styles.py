"""Validated subtitle presets shared by ASS generation and the review UI.

Colors use ordinary ``#RRGGBB`` notation here.  Only the ASS writer converts
them to its alpha/BGR format, keeping persisted settings easy to understand.
"""
from __future__ import annotations

import math
import re
import unicodedata


_DEFAULTS = {
    "preset": "classic",
    "font": "Microsoft YaHei",
    "font_size": 25.5,
    "vertical_font_size": 25.5,
    "offset_y_width_ratio": 0.05,
    "primary_color": "#FFFFFF",
    "outline_color": "#000000",
    "back_color": "#000000",
    "back_alpha": 128,
    "outline": 3.0,
    "shadow": 1.0,
    "bold": False,
    "italic": False,
    "spacing": 0.0,
    "border_style": 1,
    "margin_v": 36,
    "vertical_margin_v": 105,
    "alignment": 2,
    "max_chars": 18,
}

SUBTITLE_PRESETS: dict[str, dict] = {
    "classic": {
        "label": "经典白字黑边",
        "primary_color": "#FFFFFF", "outline_color": "#000000",
        "back_color": "#000000", "back_alpha": 128,
        "outline": 3.0, "shadow": 1.0, "bold": False,
        "italic": False, "spacing": 0.0, "border_style": 1,
    },
    "gold": {
        "label": "金色加粗",
        "primary_color": "#FFD34E", "outline_color": "#302000",
        "back_color": "#000000", "back_alpha": 128,
        "outline": 3.5, "shadow": 2.0, "bold": True,
        "italic": False, "spacing": 0.5, "border_style": 1,
    },
    "cyan": {
        "label": "青蓝描边",
        "primary_color": "#58E5FF", "outline_color": "#06395A",
        "back_color": "#000000", "back_alpha": 128,
        "outline": 4.0, "shadow": 1.5, "bold": True,
        "italic": False, "spacing": 0.0, "border_style": 1,
    },
    "comic": {
        "label": "活力粉",
        "primary_color": "#FF83BF", "outline_color": "#3C1249",
        "back_color": "#000000", "back_alpha": 128,
        "outline": 4.0, "shadow": 2.0, "bold": True,
        "italic": False, "spacing": 1.0, "border_style": 1,
    },
    "boxed": {
        "label": "深色底框",
        "primary_color": "#FFFFFF", "outline_color": "#111827",
        "back_color": "#111827", "back_alpha": 64,
        "outline": 7.0, "shadow": 0.0, "bold": False,
        "italic": False, "spacing": 0.0, "border_style": 3,
    },
}


def _number(value, field: str, minimum: float, maximum: float, *, integer: bool = False):
    if isinstance(value, bool):
        raise ValueError(f"字幕 {field} 必须是数值。")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"字幕 {field} 必须是数值。") from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"字幕 {field} 必须介于 {minimum:g} 和 {maximum:g}。")
    if integer:
        if not result.is_integer():
            raise ValueError(f"字幕 {field} 必须是整数。")
        return int(result)
    return result


def resolve_subtitle_settings(settings: dict | None) -> dict:
    """Apply defaults, then the chosen preset, then explicit validated values.

    ``settings`` is the ``subtitle`` section, not the entire configuration.
    Unknown settings are retained for forward compatibility.  No font is
    installed by this function; the renderer uses fonts already on the system.
    """
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("字幕配置必须是对象。")
    preset = settings.get("preset", "classic")
    if not isinstance(preset, str) or preset not in SUBTITLE_PRESETS:
        raise ValueError("未知字幕预设，请使用 classic、gold、cyan、comic 或 boxed。")
    result = dict(_DEFAULTS)
    result.update({key: value for key, value in SUBTITLE_PRESETS[preset].items() if key != "label"})
    result.update(settings)
    result["preset"] = preset
    if "vertical_font_size" not in settings:
        result["vertical_font_size"] = result["font_size"]
    for name in ("primary_color", "outline_color", "back_color"):
        value = result[name]
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError(f"字幕 {name} 必须使用 #RRGGBB 颜色格式。")
        result[name] = value.upper()
    font = result["font"]
    if not isinstance(font, str) or not font.strip() or len(font) > 100:
        raise ValueError("字幕字体名称必须是 1～100 字符。")
    if any(unicodedata.category(char).startswith("C") or unicodedata.category(char) in {"Zl", "Zp"} or char in ",{}\\" for char in font):
        raise ValueError("字幕字体名称不能包含换行、逗号、花括号或反斜杠。")
    result["font"] = font.strip()
    for name in ("font_size", "vertical_font_size"):
        result[name] = _number(result[name], name, 12, 240)
    result['offset_y_width_ratio'] = _number(result['offset_y_width_ratio'], 'offset_y_width_ratio', -0.5, 0.5)
    for name in ("outline", "shadow"):
        result[name] = _number(result[name], name, 0, 20)
    result["spacing"] = _number(result["spacing"], "spacing", -5, 20)
    result["back_alpha"] = _number(result["back_alpha"], "back_alpha", 0, 255, integer=True)
    for name in ("bold", "italic"):
        if not isinstance(result[name], bool):
            raise ValueError(f"字幕 {name} 必须是布尔值。")
    result["border_style"] = _number(result["border_style"], "border_style", 1, 3, integer=True)
    if result["border_style"] not in (1, 3):
        raise ValueError("字幕 border_style 仅支持 1（描边）或 3（底框）。")
    result["alignment"] = _number(result["alignment"], "alignment", 1, 9, integer=True)
    result["max_chars"] = _number(result["max_chars"], "max_chars", 4, 80, integer=True)
    for name in ("margin_v", "vertical_margin_v"):
        result[name] = _number(result[name], name, 0, 2000, integer=True)
    return result


def ass_color(rgb: str, alpha: int = 0) -> str:
    """Convert validated RGB to ASS's ``&HAABBGGRR`` notation."""
    if not isinstance(rgb, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", rgb):
        raise ValueError("ASS 颜色必须使用 #RRGGBB 格式。")
    alpha = _number(alpha, "alpha", 0, 255, integer=True)
    return f"&H{alpha:02X}{rgb[5:7]}{rgb[3:5]}{rgb[1:3]}".upper()
