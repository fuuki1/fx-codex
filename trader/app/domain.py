"""純粋ロジック（外部I/Oに依存しない部分）。

ここに集約することで、Redis/DB/ブローカー無しで単体テストできる。
- シグナル正規化 / idem 生成
- 取引セッション判定（within_session）
- レート制限（Redis 互換クライアントを引数で受ける＝テストは fakeredis）
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import market_calendar

JST = ZoneInfo("Asia/Tokyo")
ET = ZoneInfo("America/New_York")

VALID_SIDES = {"BUY", "SELL"}
VALID_TYPES = {"MARKET", "LIMIT"}
_SIDE_ALIASES = {"BUY": "BUY", "LONG": "BUY", "B": "BUY", "SELL": "SELL", "SHORT": "SELL", "S": "SELL"}


class SignalError(ValueError):
    """正規化に失敗した（不正な）シグナル。"""


# ============================================================================
# 正規化
# ============================================================================
def compute_idem(raw: dict[str, Any]) -> str:
    """明示の id/idem があれば使い、無ければ内容ハッシュで冪等キーを作る。"""
    explicit = raw.get("idem") or raw.get("id")
    if explicit:
        return str(explicit)
    canonical = json.dumps(
        {k: raw.get(k) for k in ("symbol", "side", "qty", "type", "price")},
        sort_keys=True,
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def normalize_signal(raw: dict[str, Any], *, source: str = "tradingview") -> dict[str, Any]:
    """外部 JSON を内部正規形へ。不正なら SignalError を投げる。"""
    symbol = str(raw.get("symbol", "")).strip().upper()
    if not symbol:
        raise SignalError("symbol is required")

    side_raw = str(raw.get("side", "")).strip().upper()
    side = _SIDE_ALIASES.get(side_raw)
    if side not in VALID_SIDES:
        raise SignalError(f"invalid side: {raw.get('side')!r}")

    otype = str(raw.get("type", "MARKET")).strip().upper()
    if otype not in VALID_TYPES:
        raise SignalError(f"invalid type: {raw.get('type')!r}")

    try:
        qty = float(raw.get("qty"))
    except (TypeError, ValueError):
        raise SignalError(f"invalid qty: {raw.get('qty')!r}") from None
    if qty <= 0:
        raise SignalError(f"qty must be > 0: {qty}")

    price = raw.get("price")
    if price is not None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise SignalError(f"invalid price: {price!r}") from None
    if otype == "LIMIT" and not (price and price > 0):
        raise SignalError("LIMIT order requires a positive price")

    # ストップロス指定（stop_price=絶対価格 / stop_distance=参照価格からの距離）。
    # stop_distance は基準となる price（TradingView なら {{close}}）が無いと解決できない。
    stop_price = _optional_positive(raw.get("stop_price"), "stop_price")
    stop_distance = _optional_positive(raw.get("stop_distance"), "stop_distance")
    close = bool(raw.get("close", False))
    if stop_distance is not None and stop_price is None:
        if price is None:
            raise SignalError("stop_distance requires a reference price")
        if side == "BUY" and stop_distance >= price:
            raise SignalError(
                f"stop_distance {stop_distance} must be smaller than price {price}"
            )

    asset = str(raw.get("asset", "")).strip().lower() or _infer_asset(symbol)

    return {
        "source": source,
        "symbol": symbol,
        "asset": asset,
        "side": side,
        "qty": qty,
        "type": otype,
        "price": price,
        "stop_price": stop_price,
        "stop_distance": stop_distance,
        "close": close,
        "ts": float(raw.get("ts") or time.time()),
        "idem": compute_idem(raw),
    }


def _optional_positive(value: Any, name: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SignalError(f"invalid {name}: {value!r}") from None
    if number <= 0:
        raise SignalError(f"{name} must be > 0: {number}")
    return number


def _infer_asset(symbol: str) -> str:
    if symbol.isdigit():
        return "jp_stock"
    if len(symbol) == 6 and symbol.isalpha():
        return "fx"
    return "us_stock"


# ============================================================================
# 取引セッション
# ============================================================================
def within_session(asset: str, symbol: str, now: datetime | None = None) -> bool:
    """対象市場が取引時間内かを返す。

    週末/時間帯に加え、market_calendar の祝日・半日取引を考慮する。祝日テーブルの
    収録レンジ外の日付はフェイルセーフに「祝日でない」扱い（週末/時間帯判定に委ねる）。
    """
    now = now or datetime.now(UTC)
    a = (asset or "").lower()
    if symbol and symbol.isdigit():
        return _jp_equity_open(now)
    if a in ("fx", "forex", "cash", "currency"):
        return _fx_open(now)
    if a in ("jp", "jp_stock", "jpstock", "stock_jp"):
        return _jp_equity_open(now)
    if a in ("us", "us_stock", "usstock", "stock", "equity"):
        return _us_equity_open(now)
    # 不明な資産は取引を止めない（明示的に塞ぎたい場合は呼び出し側で制御）
    return True


def _fx_open(now: datetime) -> bool:
    """FX は概ね 日曜21:00UTC 〜 金曜21:00UTC（≒NYクローズ）。元日/クリスマスは停止。"""
    u = now.astimezone(UTC)
    if market_calendar.is_fx_holiday(u.date()):
        return False
    wd = u.weekday()  # Mon=0 .. Sun=6
    minutes = u.hour * 60 + u.minute
    close = 21 * 60
    if wd == 5:           # 土曜は終日クローズ
        return False
    if wd == 6:           # 日曜は 21:00 以降オープン
        return minutes >= close
    if wd == 4:           # 金曜は 21:00 まで
        return minutes < close
    return True            # 月〜木は 24h


def _jp_equity_open(now: datetime) -> bool:
    j = now.astimezone(JST)
    if j.weekday() >= 5 or market_calendar.is_jp_equity_holiday(j.date()):
        return False
    m = j.hour * 60 + j.minute
    morning = 9 * 60 <= m < 11 * 60 + 30
    afternoon = 12 * 60 + 30 <= m < 15 * 60
    return morning or afternoon


def _us_equity_open(now: datetime) -> bool:
    e = now.astimezone(ET)
    if e.weekday() >= 5 or market_calendar.is_us_equity_holiday(e.date()):
        return False
    m = e.hour * 60 + e.minute
    # 早終い（13:00 ET クローズ）の日は通常より早くクローズ
    close = market_calendar.us_equity_early_close_minute(e.date()) or 16 * 60
    return 9 * 60 + 30 <= m < close


# ============================================================================
# レート制限（スライディングウィンドウ / Redis 永続）
# ============================================================================
def rate_limit_allow(
    rds: Any, key: str, max_per_window: int, now: float | None = None, window_sec: int = 60
) -> bool:
    """直近 window_sec 内の発注数が max 未満なら True を返し、自身を記録する。

    Redis の ZSET（score=epoch）でスライディングウィンドウを実装。コンテナ再起動でも
    消えない（= ARCHITECTURE.md の「レート制限の永続化」ギャップを解消）。
    """
    now = now if now is not None else time.time()
    cutoff = now - window_sec
    rds.zremrangebyscore(key, 0, cutoff)
    if rds.zcard(key) >= max_per_window:
        return False
    rds.zadd(key, {f"{now:.6f}:{uuid.uuid4().hex}": now})
    rds.expire(key, window_sec + 1)
    return True
