"""OANDA v20 のローソク取得（アドバイザリー分析のデータ源）。

このモジュールは **相場データの取得のみ** を担う（実売買はしない）。OANDA の練習(demo)
アカウントは無料でリアルタイム配信を提供し、FX に特化しているためアドバイザリー分析の
データ源に向く。REST の candles を定期取得する（ストリーミングより実装・復旧が簡単）。

`parse_candles` は純粋関数（ネットワーク不要）でテストできる。
"""
from __future__ import annotations

from typing import Any

import httpx
import pandas as pd
from config import settings
from logging_setup import log_extra, setup_logging

log = setup_logging("oanda", settings.log_level)


def parse_candles(doc: dict[str, Any]) -> pd.DataFrame:
    """OANDA candles レスポンスを OHLC の DataFrame へ（time/open/high/low/close/complete）。

    price=M（mid）想定だが bid/ask が来ても拾う。壊れた要素はスキップして頑健にする。
    """
    rows: list[dict[str, Any]] = []
    for c in doc.get("candles", []):
        m = c.get("mid") or c.get("bid") or c.get("ask") or {}
        try:
            rows.append(
                {
                    "time": pd.to_datetime(c["time"], utc=True),
                    "open": float(m["o"]),
                    "high": float(m["h"]),
                    "low": float(m["l"]),
                    "close": float(m["c"]),
                    "complete": bool(c.get("complete", True)),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "complete"])


def fetch_candles(instrument: str, granularity: str, count: int) -> pd.DataFrame | None:
    """OANDA から直近 count 本のローソクを取得する。トークン未設定/失敗時は None。"""
    if not settings.oanda_api_token:
        return None
    url = f"{settings.oanda_host}/v3/instruments/{instrument}/candles"
    params = {"granularity": granularity, "count": int(count), "price": "M"}
    headers = {"Authorization": f"Bearer {settings.oanda_api_token}"}
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        df = parse_candles(resp.json())
        return df if not df.empty else None
    except Exception:
        log.exception(
            "oanda fetch failed", **log_extra(instrument=instrument, granularity=granularity)
        )
        return None
