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

    asset = str(raw.get("asset", "")).strip().lower() or _infer_asset(symbol)

    return {
        "source": source,
        "symbol": symbol,
        "asset": asset,
        "side": side,
        "qty": qty,
        "type": otype,
        "price": price,
        "ts": float(raw.get("ts") or time.time()),
        "idem": compute_idem(raw),
    }


def _infer_asset(symbol: str) -> str:
    if symbol.isdigit():
        return "jp_stock"
    if len(symbol) == 6 and symbol.isalpha():
        return "fx"
    return "us_stock"


# ============================================================================
# 取引セッション
# ============================================================================
def within_session(
    asset: str,
    symbol: str,
    now: datetime | None = None,
    holidays: dict[str, Any] | None = None,
) -> bool:
    """対象市場が取引時間内かを返す。

    holidays: {"jp_stock": {"YYYY-MM-DD", ...}, "us_stock": {...}, "fx": {...}} を渡すと
    その市場区分の休日を休場として扱う。省略時（None）は祝日を考慮しない（従来動作）。
    ファイルからの読み込みは I/O なのでこの純粋関数の外（holidays.py）で行う。
    """
    now = now or datetime.now(UTC)
    holidays = holidays or {}
    a = (asset or "").lower()
    if symbol and symbol.isdigit():
        return _jp_equity_open(now, holidays.get("jp_stock"))
    if a in ("fx", "forex", "cash", "currency"):
        return _fx_open(now, holidays.get("fx"))
    if a in ("jp", "jp_stock", "jpstock", "stock_jp"):
        return _jp_equity_open(now, holidays.get("jp_stock"))
    if a in ("us", "us_stock", "usstock", "stock", "equity"):
        return _us_equity_open(now, holidays.get("us_stock"))
    # 不明な資産は取引を止めない（明示的に塞ぎたい場合は呼び出し側で制御）
    return True


def _is_holiday(local_now: datetime, holiday_dates: Any) -> bool:
    if not holiday_dates:
        return False
    return local_now.date().isoformat() in holiday_dates


def _fx_open(now: datetime, holiday_dates: Any = None) -> bool:
    """FX は概ね 日曜21:00UTC 〜 金曜21:00UTC（≒NYクローズ）。"""
    u = now.astimezone(UTC)
    if _is_holiday(u, holiday_dates):
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


def _jp_equity_open(now: datetime, holiday_dates: Any = None) -> bool:
    j = now.astimezone(JST)
    if j.weekday() >= 5:
        return False
    if _is_holiday(j, holiday_dates):
        return False
    m = j.hour * 60 + j.minute
    morning = 9 * 60 <= m < 11 * 60 + 30
    afternoon = 12 * 60 + 30 <= m < 15 * 60
    return morning or afternoon


def _us_equity_open(now: datetime, holiday_dates: Any = None) -> bool:
    e = now.astimezone(ET)
    if e.weekday() >= 5:
        return False
    if _is_holiday(e, holiday_dates):
        return False
    m = e.hour * 60 + e.minute
    return 9 * 60 + 30 <= m < 16 * 60


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
