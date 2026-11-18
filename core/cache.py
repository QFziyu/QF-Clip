"""Recover disposable caches without silently resetting durable review/budget data."""
from __future__ import annotations

import logging
from pathlib import Path
import uuid

from core.utils import read_json

log = logging.getLogger(__name__)


def read_cache(path, validator=None):
    """Return a valid cache or quarantine malformed data for reconstruction.

    This helper is deliberately not used for SQLite, user-provided transcripts,
    or the API ledger. Those are authoritative records, not disposable caches.
    Permission and I/O errors propagate rather than being mistaken for corruption.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        value = read_json(path)
        if validator:
            validator(value)
        return value
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex[:10]}")
        path.replace(backup)
        log.warning("缓存损坏，已保留到 %s；将复用其他检查点重建：%s", backup, exc)
        return None
