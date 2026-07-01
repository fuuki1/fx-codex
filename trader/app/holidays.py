"""市場休日カレンダーの読み込み（I/O）。

domain.py の `within_session()` は外部 I/O に依存しない純粋関数として保つ方針のため、
ファイル読込・キャッシュはここに分離する。呼び出し側（risk.py 等）が `get_calendar()` で
取得し、`within_session(..., holidays=get_calendar())` のように渡す。

settings.market_holidays_file（既定 "market_holidays.json"、app/ からの相対パス）を
mtime 監視して更新があれば再読込する（strategy.py の ParamStore と同じホットリロード方式）。
ファイルが無い/壊れている場合は「祝日を考慮しない」に安全側フォールバックする。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config import settings

log = logging.getLogger("holidays")

_cache: dict[str, frozenset[str]] = {}
_mtime: float = 0.0


def _resolve_path() -> Path:
    p = Path(settings.market_holidays_file)
    return p if p.is_absolute() else Path(__file__).parent / p


def get_calendar() -> dict[str, frozenset[str]]:
    """venue -> 休日集合（"YYYY-MM-DD"）を返す。ファイル未検出/破損時は前回値（初回は空）。"""
    global _cache, _mtime
    path = _resolve_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _cache
    if mtime == _mtime:
        return _cache
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        _cache = {
            venue: frozenset(dates)
            for venue, dates in raw.items()
            if not venue.startswith("_") and isinstance(dates, list)
        }
        _mtime = mtime
    except Exception:
        log.exception("failed to load market holidays; keeping previous calendar")
    return _cache
