"""IBKR paper Gateway の実行可能 bid/ask を学習用価格契約へ変換する。

接続は常に read-only。判断時 quote と完了済み5分 BID/ASK 足だけを取得し、
注文 API は呼ばない。IBKR の import は遅延させ、未導入環境でも他の取得元を
利用できるようにする。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from collections.abc import Callable

DEFAULT_TARGET_TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h", "1d")
BAR_SECONDS = 300


@dataclass(frozen=True)
class IbkrPriceConfig:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 71
    timeout_seconds: float = 8.0

    @classmethod
    def from_env(cls, *, project_root: str | Path | None = None) -> IbkrPriceConfig:
        root = Path(project_root) if project_root is not None else None
        host = _env_or_dotenv("IBKR_HOST", root) or "127.0.0.1"
        port = _integer_env("IBKR_PORT", root, 4002)
        if port == 4001:
            raise ValueError(
                "IBKR live port 4001 は学習データ取得に使用できません。paper 4002を指定してください"
            )
        if not 1 <= port <= 65535:
            raise ValueError("IBKR_PORT が不正です")
        client_id = _integer_env("IBKR_PRICE_CLIENT_ID", root, 71)
        timeout_raw = _env_or_dotenv("IBKR_PRICE_TIMEOUT_SECONDS", root)
        try:
            timeout = float(timeout_raw) if timeout_raw else 8.0
        except ValueError as error:
            raise ValueError("IBKR_PRICE_TIMEOUT_SECONDS が数値ではありません") from error
        if timeout <= 0:
            raise ValueError("IBKR_PRICE_TIMEOUT_SECONDS は正である必要があります")
        return cls(host=host, port=port, client_id=client_id, timeout_seconds=timeout)


def fetch_decision_quotes(
    symbols: Sequence[str],
    config: IbkrPriceConfig,
    *,
    captured_at: datetime | None = None,
    ib_factory: Callable[[], Any] | None = None,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """IBKR snapshot quote を判断時点の実行可能価格として取得する。"""

    captured = _utc(captured_at or datetime.now(UTC))
    ib = None
    try:
        ib, forex = _connect(config, ib_factory=ib_factory, client_id_offset=0)
        contracts = [forex(_normalize_symbol(symbol)) for symbol in symbols]
        qualified = list(ib.qualifyContracts(*contracts))
        tickers = list(ib.reqTickers(*qualified))
        quotes = parse_tickers(tickers, captured_at=captured)
    except Exception as error:  # noqa: BLE001 - broker boundary
        return {}, [f"IBKR paper判断時quote取得失敗: {_error_text(error)}"]
    finally:
        _disconnect(ib)
    missing = sorted(set(_normalize_symbol(symbol) for symbol in symbols) - set(quotes))
    return quotes, [f"IBKR paper判断時quote欠損: {symbol}" for symbol in missing]


def parse_tickers(
    tickers: Sequence[object], *, captured_at: datetime | None = None
) -> dict[str, dict[str, object]]:
    captured = _utc(captured_at or datetime.now(UTC))
    output: dict[str, dict[str, object]] = {}
    for ticker in tickers:
        contract = getattr(ticker, "contract", None)
        symbol_raw = f"{getattr(contract, 'symbol', '')}{getattr(contract, 'currency', '')}"
        try:
            symbol = _normalize_symbol(symbol_raw)
            bid = _positive_float(getattr(ticker, "bid", None), "bid")
            ask = _positive_float(getattr(ticker, "ask", None), "ask")
        except ValueError:
            continue
        if ask < bid:
            continue
        raw_time = getattr(ticker, "time", None)
        observed = _utc(raw_time) if isinstance(raw_time, datetime) else captured
        row: dict[str, object] = {
            "schema_version": 1,
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "spread": round(ask - bid, 10),
            "observed_at": observed.isoformat(),
            "available_time": max(observed, captured).isoformat(),
            "ingested_time": captured.isoformat(),
            "source": "ibkr_paper_snapshot",
            "role": "decision_quote",
            "source_record_id": f"{symbol}:ibkr-snapshot:{observed.isoformat()}",
            "tradeable": True,
            "status": "paper",
        }
        row["content_hash"] = _content_hash(row)
        output[symbol] = row
    return output


def fetch_completed_bid_ask_rows(
    symbols: Sequence[str],
    config: IbkrPriceConfig,
    *,
    target_timeframes: Sequence[str] = DEFAULT_TARGET_TIMEFRAMES,
    now: datetime | None = None,
    ib_factory: Callable[[], Any] | None = None,
) -> tuple[list[dict[str, object]], list[str]]:
    """最新の共通完了済み5分BID/ASK足を対象時間足ごとに返す。"""

    captured = _utc(now or datetime.now(UTC))
    rows: list[dict[str, object]] = []
    warnings: list[str] = []
    ib = None
    try:
        ib, forex = _connect(config, ib_factory=ib_factory, client_id_offset=1)
        for raw_symbol in symbols:
            symbol = _normalize_symbol(raw_symbol)
            try:
                contract = ib.qualifyContracts(forex(symbol))[0]
                kwargs = {
                    "contract": contract,
                    "endDateTime": "",
                    "durationStr": "2 D",
                    "barSizeSetting": "5 mins",
                    "useRTH": False,
                    "formatDate": 2,
                    "keepUpToDate": False,
                    "timeout": config.timeout_seconds,
                }
                bid_bars = ib.reqHistoricalData(whatToShow="BID", **kwargs)
                ask_bars = ib.reqHistoricalData(whatToShow="ASK", **kwargs)
                pair = latest_completed_pair(bid_bars, ask_bars, captured_at=captured)
                if pair is None:
                    warnings.append(f"IBKR paper {symbol}: 共通の完了済み5分BID/ASK足がありません")
                    continue
                rows.extend(
                    bars_to_rows(
                        symbol,
                        pair[0],
                        pair[1],
                        target_timeframes=target_timeframes,
                        captured_at=captured,
                    )
                )
            except Exception as error:  # noqa: BLE001 - symbol-level broker boundary
                warnings.append(f"IBKR paper {symbol} bid/ask OHLC取得失敗: {_error_text(error)}")
    except Exception as error:  # noqa: BLE001 - connection boundary
        warnings.append(f"IBKR paper接続失敗: {_error_text(error)}")
    finally:
        _disconnect(ib)
    return rows, warnings


def latest_completed_pair(
    bid_bars: Sequence[object],
    ask_bars: Sequence[object],
    *,
    captured_at: datetime,
) -> tuple[object, object] | None:
    captured = _utc(captured_at)
    bids = {_bar_start(bar): bar for bar in bid_bars if _bar_completed(bar, captured)}
    asks = {_bar_start(bar): bar for bar in ask_bars if _bar_completed(bar, captured)}
    common = sorted(set(bids) & set(asks))
    return (bids[common[-1]], asks[common[-1]]) if common else None


def bars_to_rows(
    symbol: str,
    bid_bar: object,
    ask_bar: object,
    *,
    target_timeframes: Sequence[str] = DEFAULT_TARGET_TIMEFRAMES,
    captured_at: datetime | None = None,
) -> list[dict[str, object]]:
    start = _bar_start(bid_bar)
    if _bar_start(ask_bar) != start:
        raise ValueError("IBKR BID/ASK足の開始時刻が一致しません")
    end = start + timedelta(seconds=BAR_SECONDS)
    captured = _utc(captured_at or datetime.now(UTC))
    bid = _bar_ohlc(bid_bar, "bid")
    ask = _bar_ohlc(ask_bar, "ask")
    for key in ("open", "high", "low", "close"):
        if bid[key] > ask[key]:
            raise ValueError(f"IBKR 5分足の{key}でbidがaskを上回っています")
    mid = {key: round((bid[key] + ask[key]) / 2.0, 10) for key in bid}
    common: dict[str, object] = {
        "schema_version": 3,
        "ts": end.isoformat(),
        "event_time": end.isoformat(),
        "available_time": max(captured, end).isoformat(),
        "ingested_time": captured.isoformat(),
        "symbol": _normalize_symbol(symbol),
        "bar_start": start.isoformat(),
        "bar_end": end.isoformat(),
        "bar_granularity": "M5",
        "complete": True,
        "source": "ibkr_paper_historical",
        "ohlc_scope": "completed_bid_ask_bar",
        "data_quality_flags": [],
        "open": mid["open"],
        "high": mid["high"],
        "low": mid["low"],
        "close": mid["close"],
        "bid_open": bid["open"],
        "bid_high": bid["high"],
        "bid_low": bid["low"],
        "bid_close": bid["close"],
        "ask_open": ask["open"],
        "ask_high": ask["high"],
        "ask_low": ask["low"],
        "ask_close": ask["close"],
        "bid": bid["close"],
        "ask": ask["close"],
        "spread": round(ask["close"] - bid["close"], 10),
        "volume": None,
    }
    rows: list[dict[str, object]] = []
    for timeframe in target_timeframes:
        row = dict(common)
        row["timeframe"] = str(timeframe)
        row["source_record_id"] = f"{row['symbol']}:IBKR:M5:{start.isoformat()}:{timeframe}"
        row["content_hash"] = _content_hash(row)
        rows.append(row)
    return rows


def _connect(
    config: IbkrPriceConfig,
    *,
    ib_factory: Callable[[], Any] | None,
    client_id_offset: int,
) -> tuple[Any, Any]:
    forex: Any
    fetch_fields: Any
    if ib_factory is None:
        try:
            from ib_async import Forex, IB, StartupFetch
        except ImportError as error:
            raise RuntimeError("ib_async が未導入です") from error
        ib_factory = IB
        forex = Forex
        fetch_fields = StartupFetch(0)
    else:
        forex = _SimpleForex
        fetch_fields = 0
    ib = ib_factory()
    ib.connect(
        config.host,
        config.port,
        clientId=config.client_id + client_id_offset,
        timeout=config.timeout_seconds,
        readonly=True,
        fetchFields=fetch_fields,
    )
    return ib, forex


@dataclass(frozen=True)
class _SimpleForex:
    pair: str


def _disconnect(ib: Any) -> None:
    if ib is None:
        return
    try:
        if ib.isConnected():
            ib.disconnect()
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


def _bar_start(bar: object) -> datetime:
    value = getattr(bar, "date", None)
    if not isinstance(value, datetime):
        raise ValueError("IBKR足にdatetimeのdateがありません")
    return _utc(value)


def _bar_completed(bar: object, captured_at: datetime) -> bool:
    try:
        return _bar_start(bar) + timedelta(seconds=BAR_SECONDS) <= captured_at
    except ValueError:
        return False


def _bar_ohlc(bar: object, label: str) -> dict[str, float]:
    values = {
        key: _positive_float(getattr(bar, key, None), f"{label}.{key}")
        for key in ("open", "high", "low", "close")
    }
    if values["high"] < max(values.values()) or values["low"] > min(values.values()):
        raise ValueError(f"IBKR {label}足のOHLC関係が不正です")
    return values


def _normalize_symbol(symbol: str) -> str:
    cleaned = symbol.upper().replace("/", "").replace("_", "").strip()
    if len(cleaned) != 6 or not cleaned.isalpha():
        raise ValueError(f"通貨ペア名を解釈できません: {symbol}")
    return cleaned


def _positive_float(value: object, label: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}が数値ではありません") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}は有限の正数である必要があります")
    return number


def _content_hash(row: Mapping[str, object]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _error_text(error: Exception) -> str:
    detail = str(error).strip()
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def _integer_env(name: str, root: Path | None, default: int) -> int:
    raw = _env_or_dotenv(name, root)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} が整数ではありません") from error


def _env_or_dotenv(name: str, project_root: Path | None) -> str | None:
    value = os.environ.get(name)
    if value:
        return value.strip()
    if project_root is None:
        return None
    try:
        lines = (project_root / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = stripped.split("=", 1)
        if key.strip() == name:
            return raw.strip().strip("\"'") or None
    return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
