"""OANDA v20 の完了済み bid/ask OHLC を採点用価格行へ変換する。

TradingView scanner の OHLC は取得時点で形成中の足であり、判断前の high/low を
含みうる。また bid/ask が返らない環境では約定可能な価格経路を検証できない。
このモジュールは OANDA の M5 完了足を ``price=BA`` で取得し、次を保証する。

- 未完了足は保存しない
- OANDA が返す足開始時刻と、粒度から計算した足終了時刻を両方保存する
- bid/ask を別々の OHLC 列として保持する
- 既存の方向学習用に mid 相当の OHLC も併記する
- 同じM5経路を各判断時間足へ複製し、既存の ``symbol × timeframe`` 採点と互換にする

ネットワーク入口はこのファイルだけに閉じ、パースはモックでテストできる。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import requests

PRACTICE_URL = "https://api-fxpractice.oanda.com"
LIVE_URL = "https://api-fxtrade.oanda.com"
DEFAULT_GRANULARITY = "M5"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_TARGET_TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h", "1d")

GRANULARITY_SECONDS = {
    "S5": 5,
    "S10": 10,
    "S15": 15,
    "S30": 30,
    "M1": 60,
    "M2": 120,
    "M3": 180,
    "M4": 240,
    "M5": 300,
    "M10": 600,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H2": 7200,
    "H3": 10800,
    "H4": 14400,
}


@dataclass(frozen=True)
class OandaPriceConfig:
    token: str
    environment: str = "practice"
    granularity: str = DEFAULT_GRANULARITY
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    account_id: str | None = None

    @property
    def base_url(self) -> str:
        return LIVE_URL if self.environment == "live" else PRACTICE_URL

    @classmethod
    def from_env(
        cls,
        *,
        project_root: str | Path | None = None,
        environment: str | None = None,
        granularity: str | None = None,
    ) -> OandaPriceConfig:
        root = Path(project_root) if project_root is not None else None
        token = _env_or_dotenv("OANDA_API_TOKEN", root) or _env_or_dotenv("OANDA_TOKEN", root)
        if not token:
            raise ValueError("OANDA_API_TOKEN が未設定です。.env に設定してから再実行してください")
        selected_environment = (
            (environment or _env_or_dotenv("OANDA_ENVIRONMENT", root) or "practice").strip().lower()
        )
        if selected_environment not in {"practice", "live"}:
            raise ValueError("OANDA_ENVIRONMENT は practice または live を指定してください")
        selected_granularity = (
            (granularity or _env_or_dotenv("OANDA_PRICE_GRANULARITY", root) or DEFAULT_GRANULARITY)
            .strip()
            .upper()
        )
        if selected_granularity not in GRANULARITY_SECONDS:
            raise ValueError(f"未対応のOANDA足粒度です: {selected_granularity}")
        return cls(
            token=token,
            environment=selected_environment,
            granularity=selected_granularity,
            account_id=_env_or_dotenv("OANDA_ACCOUNT_ID", root),
        )


def fetch_decision_quotes(
    symbols: Sequence[str],
    config: OandaPriceConfig,
    *,
    captured_at: datetime | None = None,
    session: Any = None,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Fetch current broker bid/ask quotes for decision-time input context.

    This uses the account pricing endpoint and is intentionally separate from
    completed candle collection.  The response is an entry/liquidity input,
    never a future TP/SL outcome path.
    """

    if not config.account_id:
        return {}, ["OANDA_ACCOUNT_ID未設定のため判断時quoteを取得できません"]
    captured = _utc(captured_at or datetime.now(UTC))
    instruments = [_oanda_instrument(symbol) for symbol in symbols]
    url = f"{config.base_url}/v3/accounts/{config.account_id}/pricing"
    http = session or requests
    try:
        response = http.get(
            url,
            headers={
                "Authorization": f"Bearer {config.token}",
                "Accept-Datetime-Format": "RFC3339",
                "User-Agent": "fx-codex-decision-quote/1.0",
            },
            params={"instruments": ",".join(instruments)},
            timeout=config.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as error:  # noqa: BLE001 - external broker boundary
        return {}, [f"OANDA判断時quote取得失敗: {error}"]
    try:
        quotes = parse_decision_quotes(payload, captured_at=captured)
    except ValueError as error:
        return {}, [f"OANDA判断時quote応答不正: {error}"]
    missing = sorted(set(_normalize_symbol(symbol) for symbol in symbols) - set(quotes))
    warnings = [f"OANDA判断時quote欠損: {symbol}" for symbol in missing]
    return quotes, warnings


def parse_decision_quotes(
    payload: object, *, captured_at: datetime | None = None
) -> dict[str, dict[str, object]]:
    if not isinstance(payload, Mapping):
        raise ValueError("pricing応答がJSON objectではありません")
    prices = payload.get("prices")
    if not isinstance(prices, list):
        raise ValueError("pricing応答にprices配列がありません")
    captured = _utc(captured_at or datetime.now(UTC))
    output: dict[str, dict[str, object]] = {}
    for raw in prices:
        if not isinstance(raw, Mapping):
            continue
        instrument = str(raw.get("instrument", ""))
        try:
            symbol = _normalize_symbol(instrument)
            observed = _parse_oanda_time(raw.get("time"))
            bid = _top_price(raw.get("bids"), "bids")
            ask = _top_price(raw.get("asks"), "asks")
        except ValueError:
            continue
        if ask < bid:
            continue
        source_record_id = f"{symbol}:pricing:{observed.isoformat()}"
        row: dict[str, object] = {
            "schema_version": 1,
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
            "observed_at": observed.isoformat(),
            "available_time": max(observed, captured).isoformat(),
            "ingested_time": captured.isoformat(),
            "source": "oanda_v20_pricing",
            "role": "decision_quote",
            "source_record_id": source_record_id,
            "tradeable": bool(raw.get("tradeable", True)),
            "status": str(raw.get("status", "")),
        }
        row["content_hash"] = _content_hash(row)
        output[symbol] = row
    return output


def _top_price(value: object, label: str) -> float:
    if not isinstance(value, list) or not value or not isinstance(value[0], Mapping):
        raise ValueError(f"pricing応答に{label}がありません")
    return _positive_float(value[0].get("price"), f"{label}.price")


def fetch_completed_bid_ask_rows(
    symbols: Sequence[str],
    config: OandaPriceConfig,
    *,
    target_timeframes: Sequence[str] = DEFAULT_TARGET_TIMEFRAMES,
    now: datetime | None = None,
    session: Any = None,
) -> tuple[list[dict[str, object]], list[str]]:
    """各銘柄の最新完了bid/ask足を取得し、既存採点用JSONL行へ変換する。"""

    captured_at = _utc(now or datetime.now(UTC))
    http = session or requests
    rows: list[dict[str, object]] = []
    warnings: list[str] = []
    for raw_symbol in symbols:
        symbol = _normalize_symbol(raw_symbol)
        instrument = _oanda_instrument(symbol)
        url = f"{config.base_url}/v3/instruments/{instrument}/candles"
        try:
            response = http.get(
                url,
                headers={
                    "Authorization": f"Bearer {config.token}",
                    "Accept-Datetime-Format": "RFC3339",
                    "User-Agent": "fx-codex-price-capture/1.0",
                },
                params={
                    "price": "BA",
                    "granularity": config.granularity,
                    "count": 3,
                    "smooth": "false",
                },
                timeout=config.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            candle = _latest_complete_candle(payload)
            if candle is None:
                warnings.append(f"OANDA {symbol}: 完了済み{config.granularity}足がありません")
                continue
            rows.extend(
                candle_to_rows(
                    symbol,
                    candle,
                    granularity=config.granularity,
                    target_timeframes=target_timeframes,
                    captured_at=captured_at,
                )
            )
        except Exception as error:  # noqa: BLE001 - 外部API起因。銘柄単位で継続する
            warnings.append(f"OANDA {symbol} bid/ask OHLC取得失敗: {error}")
    return rows, warnings


def candle_to_rows(
    symbol: str,
    candle: Mapping[str, object],
    *,
    granularity: str,
    target_timeframes: Sequence[str] = DEFAULT_TARGET_TIMEFRAMES,
    captured_at: datetime | None = None,
) -> list[dict[str, object]]:
    """OANDA candle 1本を、対象時間足ごとの監査可能な価格行へ変換する。"""

    if not candle.get("complete"):
        raise ValueError("未完了OANDA candleは保存できません")
    start = _parse_oanda_time(candle.get("time"))
    seconds = GRANULARITY_SECONDS.get(granularity.upper())
    if seconds is None:
        raise ValueError(f"未対応のOANDA足粒度です: {granularity}")
    end = start + timedelta(seconds=seconds)
    captured = _utc(captured_at or datetime.now(UTC))
    bid = _parse_side(candle.get("bid"), "bid")
    ask = _parse_side(candle.get("ask"), "ask")
    for key in ("open", "high", "low", "close"):
        if bid[key] > ask[key]:
            raise ValueError(f"OANDA candleの{key}でbidがaskを上回っています")

    mid = {key: round((bid[key] + ask[key]) / 2.0, 10) for key in bid}
    common: dict[str, object] = {
        "schema_version": 3,
        # 完了足の情報が利用可能になるのは足終了時刻以降。tsも終了時刻にする。
        "ts": end.isoformat(),
        "event_time": end.isoformat(),
        "available_time": max(captured, end).isoformat(),
        "ingested_time": captured.isoformat(),
        "symbol": _normalize_symbol(symbol),
        "bar_start": start.isoformat(),
        "bar_end": end.isoformat(),
        "bar_granularity": granularity.upper(),
        "complete": True,
        "source": "oanda_v20",
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
        "volume": _integer(candle.get("volume")),
    }
    rows: list[dict[str, object]] = []
    for timeframe in target_timeframes:
        row = dict(common)
        row["timeframe"] = str(timeframe)
        source_record_id = f"{row['symbol']}:{granularity.upper()}:{start.isoformat()}:{timeframe}"
        row["source_record_id"] = source_record_id
        row["content_hash"] = _content_hash(row)
        rows.append(row)
    return rows


def _latest_complete_candle(payload: object) -> Mapping[str, object] | None:
    if not isinstance(payload, Mapping):
        raise ValueError("OANDA応答がJSON objectではありません")
    candles = payload.get("candles")
    if not isinstance(candles, list):
        raise ValueError("OANDA応答にcandles配列がありません")
    complete = [item for item in candles if isinstance(item, Mapping) and item.get("complete")]
    return complete[-1] if complete else None


def _parse_side(value: object, label: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError(f"OANDA candleに{label} OHLCがありません")
    result = {
        "open": _positive_float(value.get("o"), f"{label}.o"),
        "high": _positive_float(value.get("h"), f"{label}.h"),
        "low": _positive_float(value.get("l"), f"{label}.l"),
        "close": _positive_float(value.get("c"), f"{label}.c"),
    }
    if result["high"] < max(result["open"], result["close"], result["low"]):
        raise ValueError(f"OANDA candleの{label} highがOHLC最大値未満です")
    if result["low"] > min(result["open"], result["close"], result["high"]):
        raise ValueError(f"OANDA candleの{label} lowがOHLC最小値より上です")
    return result


def _positive_float(value: object, label: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(f"OANDA candleの{label}が数値ではありません") from error
    if number <= 0:
        raise ValueError(f"OANDA candleの{label}は正である必要があります")
    return number


def _parse_oanda_time(value: object) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return _utc(datetime.fromisoformat(raw))
    except ValueError as error:
        raise ValueError(f"OANDA candle時刻を解釈できません: {value!r}") from error


def _normalize_symbol(symbol: str) -> str:
    cleaned = symbol.upper().replace("/", "").replace("_", "").strip()
    if len(cleaned) != 6 or not cleaned.isalpha():
        raise ValueError(f"通貨ペア名を解釈できません: {symbol}")
    return cleaned


def _oanda_instrument(symbol: str) -> str:
    cleaned = _normalize_symbol(symbol)
    return f"{cleaned[:3]}_{cleaned[3:]}"


def _content_hash(row: Mapping[str, object]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (str, int, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _env_or_dotenv(name: str, project_root: Path | None) -> str | None:
    value = os.environ.get(name)
    if value:
        return value.strip()
    if project_root is None:
        return None
    path = project_root / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
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
