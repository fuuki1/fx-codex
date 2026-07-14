"""価格/イベントの読み込みと検証。

mission-critical の分析では「壊れたデータで黙って数字を出す」のが最悪なので、
データ品質チェックに通らなければ即座に例外を投げる（fail-fast）。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

_SECONDS_PER_YEAR = 365.25 * 24 * 3600
_DAY = 24 * 3600
_REQUIRED = ("open", "high", "low", "close")
_BLOCKING_KINDS = {"news", "halt", "blackout", "high_impact"}


class DataError(ValueError):
    """データ品質チェック違反。"""


def load_prices(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise DataError(f"price file not found: {p}")
    df = pd.read_csv(p)
    cols = {c.lower(): c for c in df.columns}
    if "timestamp" not in cols:
        raise DataError("price file requires a 'timestamp' column")
    df = df.rename(columns={cols["timestamp"]: "timestamp"})
    for c in _REQUIRED:
        if c not in cols:
            raise DataError(f"price file missing column: {c}")
        df = df.rename(columns={cols[c]: c})

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    if df["timestamp"].isna().any():
        raise DataError("unparseable timestamp(s) present")
    df = df.set_index("timestamp").sort_index()

    if df.index.has_duplicates:
        raise DataError("duplicate timestamps present")
    for c in _REQUIRED:
        if df[c].isna().any():
            raise DataError(f"NaN present in column: {c}")
    if (df[list(_REQUIRED)] <= 0).any().any():
        raise DataError("non-positive price present")
    if (df["high"] < df["low"]).any():
        raise DataError("high < low on some bars")
    if ((df["high"] < df["open"]) | (df["high"] < df["close"])).any():
        raise DataError("high below open/close on some bars")
    if ((df["low"] > df["open"]) | (df["low"] > df["close"])).any():
        raise DataError("low above open/close on some bars")
    return df[list(_REQUIRED)]


def load_events(path: str | Path | None) -> pd.DataFrame | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    ev = pd.read_csv(p)
    cols = {c.lower(): c for c in ev.columns}
    if "timestamp" not in cols:
        raise DataError("events file requires a 'timestamp' column")
    ev = ev.rename(columns={cols["timestamp"]: "timestamp"})
    ev["timestamp"] = pd.to_datetime(ev["timestamp"], utc=True, errors="coerce")
    ev = ev.dropna(subset=["timestamp"])
    if "kind" in cols:
        ev = ev.rename(columns={cols["kind"]: "kind"})
    else:
        ev["kind"] = "news"
    return ev[["timestamp", "kind"]]


def blocked_mask(index: pd.DatetimeIndex, events: pd.DataFrame | None) -> pd.Series:
    """新規エントリを禁止するバーを True にしたマスク（ニュース等の no-trade 窓）。"""
    mask = pd.Series(False, index=index)
    if events is None or events.empty:
        return mask
    blocking = events[events["kind"].str.lower().isin(_BLOCKING_KINDS)]
    if blocking.empty:
        return mask
    hit = index.isin(pd.DatetimeIndex(blocking["timestamp"]))
    mask[:] = hit
    return mask


def infer_periods_per_year(index: pd.DatetimeIndex) -> float:
    """バー間隔から年率換算係数を推定（日次は取引日 252 慣行）。"""
    if len(index) < 3:
        return 252.0
    delta = index.to_series().diff().dropna().dt.total_seconds().median()
    if delta <= 0:
        return 252.0
    if 0.5 * _DAY <= delta <= 1.5 * _DAY:
        return 252.0
    if 6 * _DAY <= delta <= 8 * _DAY:
        return 52.0
    if 25 * _DAY <= delta <= 35 * _DAY:
        return 12.0
    return _SECONDS_PER_YEAR / delta
