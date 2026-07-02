from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from fx_backtester.models import instrument_for, normalize_symbol

REQUIRED_PRICE_COLUMNS = {"timestamp", "open", "high", "low", "close"}
EVENT_COLUMNS = ["timestamp", "currency", "symbol", "impact", "name"]
IMPACT_LEVELS = {"low": 1, "medium": 2, "high": 3}
KNOWN_SYMBOLS = (
    "USDJPY",
    "EURUSD",
    "GBPUSD",
    "AUDUSD",
    "USDCHF",
    "USDCAD",
    "NZDUSD",
    "EURJPY",
    "GBPJPY",
    "AUDJPY",
)


def _standardize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    renamed = frame.copy()
    renamed.columns = [str(column).strip().lower() for column in renamed.columns]
    aliases = {
        "datetime": "timestamp",
        "date": "timestamp",
        "time": "timestamp",
        "bidopen": "open",
        "bidhigh": "high",
        "bidlow": "low",
        "bidclose": "close",
    }
    renamed = renamed.rename(columns={k: v for k, v in aliases.items() if k in renamed.columns})
    return renamed


def _symbol_from_path(path: str | Path) -> str:
    stem = Path(path).stem.upper().replace("_", "").replace("-", "")
    for candidate in KNOWN_SYMBOLS:
        if candidate in stem:
            return candidate
    raise ValueError(
        f"{path} has no symbol column. Pass a CSV with symbol column or name the file like EURUSD.csv."
    )


def load_price_csv(path: str | Path, symbol: str | None = None, timezone: str | None = None) -> dict[str, pd.DataFrame]:
    """Load OHLC price data.

    Accepted columns: timestamp, symbol (optional), open, high, low, close,
    volume/spread_pips/spread_price/spread (optional).
    Returns a dict keyed by normalized symbols such as EURUSD.
    """
    frame = _standardize_columns(pd.read_csv(path))
    missing = REQUIRED_PRICE_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing required columns: {sorted(missing)}")

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=False)
    if timezone:
        if frame["timestamp"].dt.tz is None:
            frame["timestamp"] = frame["timestamp"].dt.tz_localize(timezone)
        else:
            frame["timestamp"] = frame["timestamp"].dt.tz_convert(timezone)

    if "symbol" not in frame.columns:
        frame["symbol"] = normalize_symbol(symbol or _symbol_from_path(path))
    else:
        frame["symbol"] = frame["symbol"].map(normalize_symbol)

    output: dict[str, pd.DataFrame] = {}
    numeric_columns = ["open", "high", "low", "close"]
    for optional_column in ("volume", "spread_pips", "spread_price", "spread"):
        if optional_column in frame.columns:
            numeric_columns.append(optional_column)

    for symbol_name, symbol_frame in frame.groupby("symbol", sort=True):
        instrument_for(symbol_name)
        prepared = symbol_frame.sort_values("timestamp").set_index("timestamp")
        if prepared.index.has_duplicates:
            duplicates = prepared.index[prepared.index.duplicated()].unique()
            raise ValueError(f"{symbol_name} has duplicate timestamps: {duplicates[:3].tolist()}")
        prepared[numeric_columns] = prepared[numeric_columns].astype(float)
        output[symbol_name] = prepared[numeric_columns]

    return output


def load_price_csvs(paths: list[str | Path]) -> dict[str, pd.DataFrame]:
    loaded: dict[str, pd.DataFrame] = {}
    for path in paths:
        for symbol, frame in load_price_csv(path).items():
            if symbol in loaded:
                loaded[symbol] = pd.concat([loaded[symbol], frame]).sort_index()
                if loaded[symbol].index.has_duplicates:
                    raise ValueError(f"{symbol} has duplicate timestamps across input files")
            else:
                loaded[symbol] = frame
    if not loaded:
        raise ValueError("No price data loaded")
    return loaded


def filter_price_data_by_date(
    data: dict[str, pd.DataFrame],
    start: Any | None = None,
    end: Any | None = None,
) -> dict[str, pd.DataFrame]:
    start_ts = _parse_datetime_bound(start, is_end=False)
    end_ts = _parse_datetime_bound(end, is_end=True)
    if start_ts is None and end_ts is None:
        return data

    filtered: dict[str, pd.DataFrame] = {}
    for symbol, frame in data.items():
        selected = frame
        if start_ts is not None:
            selected = selected[selected.index >= start_ts]
        if end_ts is not None:
            selected = selected[selected.index <= end_ts]
        filtered[symbol] = selected.copy()

    if all(frame.empty for frame in filtered.values()):
        raise ValueError("date range removed all price data")
    return filtered


def load_economic_events_csv(path: str | Path | None) -> pd.DataFrame:
    if path is None:
        return pd.DataFrame(columns=EVENT_COLUMNS).set_index(pd.DatetimeIndex([], name="timestamp"))

    frame = _standardize_columns(pd.read_csv(path))
    if "timestamp" not in frame.columns:
        raise ValueError(f"{path} is missing required column: timestamp")

    for column in EVENT_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=False)
    frame["currency"] = frame["currency"].astype(str).str.upper().str.strip()
    frame["symbol"] = frame["symbol"].astype(str).str.upper().str.replace("/", "", regex=False).str.strip()
    frame["impact"] = frame["impact"].astype(str).str.lower().str.strip().replace("", "high")
    return frame[EVENT_COLUMNS].sort_values("timestamp").set_index("timestamp")


def filter_economic_events_by_date(
    events: pd.DataFrame,
    start: Any | None = None,
    end: Any | None = None,
    *,
    minutes_before: int = 0,
    minutes_after: int = 0,
) -> pd.DataFrame:
    if events.empty:
        return events
    start_ts = _parse_datetime_bound(start, is_end=False)
    end_ts = _parse_datetime_bound(end, is_end=True)
    selected = events
    if start_ts is not None:
        selected = selected[selected.index >= start_ts - pd.Timedelta(minutes=minutes_before)]
    if end_ts is not None:
        selected = selected[selected.index <= end_ts + pd.Timedelta(minutes=minutes_after)]
    return selected.copy()


def build_no_trade_mask(
    index: pd.DatetimeIndex,
    symbol: str,
    events: pd.DataFrame,
    minutes_before: int = 30,
    minutes_after: int = 30,
    min_impact: str = "medium",
) -> pd.Series:
    if events.empty:
        return pd.Series(False, index=index)

    inst = instrument_for(symbol)
    min_level = IMPACT_LEVELS[min_impact.lower()]
    mask = pd.Series(False, index=index)

    for timestamp, event in events.iterrows():
        impact_level = IMPACT_LEVELS.get(str(event.get("impact", "high")).lower(), 3)
        if impact_level < min_level:
            continue

        event_symbol = str(event.get("symbol", "")).strip().upper().replace("/", "")
        event_currency = str(event.get("currency", "")).strip().upper()
        applies_to_symbol = event_symbol in ("", "NAN") or event_symbol == inst.symbol
        applies_to_currency = event_currency in ("", "NAN") or event_currency in {inst.base, inst.quote}
        if not (applies_to_symbol and applies_to_currency):
            continue

        start = timestamp - pd.Timedelta(minutes=minutes_before)
        end = timestamp + pd.Timedelta(minutes=minutes_after)
        mask |= (index >= start) & (index <= end)

    return mask


def _parse_datetime_bound(value: Any | None, *, is_end: bool) -> pd.Timestamp | None:
    if value is None:
        return None
    raw = str(value).strip()
    timestamp = pd.Timestamp(raw)
    if is_end and _looks_like_date_only(raw):
        return timestamp + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    return timestamp


def _looks_like_date_only(value: str) -> bool:
    return len(value) == 10 and value[4] == "-" and value[7] == "-"
