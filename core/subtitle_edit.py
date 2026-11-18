"""Literal caption editing and ASR marker removal, independent of the UI.

Original transcription is evidence and is never changed by these helpers.
"""
from __future__ import annotations

import re

# Known recognizer metadata only. Ordinary English, scores and comparisons
# such as 2 < 3 are valid captions. Join adjacent cues before scanning because
# existing caption wrapping may have split a single <unk> across cue borders.
_MARKERS = re.compile(
    r"<\|[^<>\n]{0,80}\|>|</?(?:unk|s|pad|blank|noise|silence|speech|nospeech|"
    r"startoftranscript|endoftext)>|\[(?:unk|blank_audio|music|noise|silence)\]|"
    r"(?<![A-Za-z])unk>", re.IGNORECASE)


def marker_mask(text: str) -> bytearray:
    mask = bytearray(len(text))
    for match in _MARKERS.finditer(text):
        mask[match.start():match.end()] = b'\x01' * len(match.group())
    return mask


def _han_or_punctuation(char):
    code = ord(char)
    return (0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or
            0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x323AF or
            char.isspace() or char in '，。！？；：、（）《》“”‘’…—,.!?;:()·')


def clean_caption_texts(texts, *, chinese_only=False):
    """Return cleaned text and number of removed characters, retaining order."""
    combined = ''.join(texts)
    mask = marker_mask(combined)
    cursor, result, removed = 0, [], 0
    for text in texts:
        chars = []
        for i, char in enumerate(text):
            if mask[cursor+i] or (chinese_only and not _han_or_punctuation(char)):
                removed += 1
            else:
                chars.append(char)
        result.append(''.join(chars).strip())
        cursor += len(text)
    return result, removed


def replace_srt(text, duration, find, replacement):
    """Replace literal occurrences in cue bodies; never search SRT clocks/IDs."""
    from core.subtitle import parse_srt, cues_srt
    if not find or not find.strip():
        raise ValueError('请先输入需要查找的文字')
    if any(c in find + replacement for c in ('\n', '\r', '\x00')):
        raise ValueError('查找和替换内容不能包含换行或空字符')
    cues = parse_srt(text, duration)
    occurrences = sum(c['text'].count(find) for c in cues)
    matched = sum(find in c['text'] for c in cues)
    if not occurrences or find == replacement:
        return text, occurrences, matched
    changed = [{**cue, 'text':cue['text'].replace(find,replacement).strip()} for cue in cues]
    return cues_srt([c for c in changed if c['text']]), occurrences, matched


def clean_srt(text, duration, *, chinese_only=False):
    from core.subtitle import parse_srt, cues_srt
    cues = parse_srt(text, duration)
    cleaned, removed = clean_caption_texts([c['text'] for c in cues],chinese_only=chinese_only)
    if not removed:
        return text, 0
    result = [{**cue,'text':body} for cue,body in zip(cues,cleaned) if body]
    return cues_srt(result), removed
