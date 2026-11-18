"""Compact match-event notation; half separators also disambiguate stoppage time."""
from __future__ import annotations

import re

from core.editing_options import PERIOD_LABELS, match_seconds

EVENT_CODES = {'G': '进球', 'R': '红牌', 'V': 'VAR', 'E': '关键事件'}
_TIME = r'\d{1,3}(?:\+\d{1,2})?(?::[0-5]\d(?:\.\d+)?)?'
_TOKEN = re.compile(rf'(?:(?P<code>VAR|GOAL|G|R|V|E)(?P<time>{_TIME})|'
                    rf'(?P<time2>{_TIME})(?P<code2>VAR|GOAL|G|R|V|E))'
                    r'(?:\[(?P<label>[^\[\]\r\n]{1,300})\])?(?=\s|$)', re.I)


def parse_event_text(text):
    """Examples: G23:10 45+2G / R61 V90+3:20. Never ignore bad tokens."""
    if not isinstance(text, str) or len(text) > 12000 or '\0' in text:
        raise ValueError('比赛事件请填写不超过 12000 字符的文字')
    text = text.translate(str.maketrans({'／': '/', '：': ':', '＋': '+'})).strip()
    if not text:
        return []
    halves = re.split(r'/(?![^\[]*\])', text)
    if len(halves) > 4:
        raise ValueError('最多使用三个斜杠，依次区分上半场、下半场、加时上半场和加时下半场')
    events = []
    for period, section in zip(PERIOD_LABELS, halves):
        position = 0
        while position < len(section):
            if section[position].isspace():
                position += 1
                continue
            token = _TOKEN.match(section, position)
            if not token:
                invalid = section[position:].split()[0][:50]
                raise ValueError(f'{PERIOD_LABELS[period]}无法识别「{invalid}」；例如 G23:10 R40 / G61 V90+3:20，事件之间用空格')
            clock = token['time'] or token['time2']
            match_seconds(clock, period)
            code = (token['code'] or token['code2']).upper()
            code = {'GOAL': 'G', 'VAR': 'V'}.get(code, code)
            events.append({'period': period, 'match_time': clock,
                           'label': (token['label'] or EVENT_CODES[code]).strip()})
            if len(events) > 200:
                raise ValueError('必剪事件最多 200 个')
            position = token.end()
    return events


def format_event_text(events):
    """Round-trip old descriptions; brackets may contain spaces."""
    sections = []
    for period in PERIOD_LABELS:
        tokens = []
        for event in events:
            if event['period'] != period:
                continue
            label = event['label']
            code = next((k for k, v in EVENT_CODES.items() if v == label), 'E')
            suffix = '' if label == EVENT_CODES[code] else '[' + label.replace('[', '（').replace(']', '）') + ']'
            tokens.append(code + event['match_time'].strip() + suffix)
        sections.append(' '.join(tokens))
    while sections and not sections[-1]:
        sections.pop()
    return ' / '.join(sections)
