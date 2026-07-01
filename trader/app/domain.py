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
# 発注の意図。exit/close/flat は「撤退（フラット化）」＝ executor が保護ストップを取消して
# フラット化し、risk は入口ゲートを課さず素通しする。未指定/その他は entry（新規）扱い。
_EXIT_INTENTS = {"exit", "close", "flat"}


class SignalError(ValueError):
    """正規化に失敗した（不正な）シグナル。"""


# ============================================================================
# 時刻パース / 鮮度
# ============================================================================
# これより大きい数値は「ミリ秒 epoch」とみなす（秒 epoch は ~1.7e9 / ミリ秒は ~1.7e12）。
_MILLIS_THRESHOLD = 1e12


def parse_ts(value: Any, *, now: float | None = None) -> float:
    """多様な時刻表現を UNIX 秒（float）へ。解釈不能/未指定は「現在時刻」を返す。

    受け付ける形式:
      - 数値 / 数値文字列（秒 epoch。大きすぎる値はミリ秒 epoch として補正）
      - ISO 8601 文字列（TradingView の ``{{timenow}}`` 例: ``2024-01-01T00:00:00Z``）

    ※ 文字列 epoch に対し ``float()`` を直接使うと ISO 文字列で ValueError になり
       normalize_signal が 500 を返してしまう。ここで吸収して堅牢化する。
    """
    fallback = now if now is not None else time.time()
    if value is None or value == "":
        return fallback
    if isinstance(value, bool):  # True/False を 1.0/0.0 と誤解しない
        return fallback
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > _MILLIS_THRESHOLD else float(value)
    s = str(value).strip()
    try:
        num = float(s)
        return num / 1000.0 if num > _MILLIS_THRESHOLD else num
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except ValueError:
        return fallback


def signal_is_stale(ts: float, now: float, max_age_sec: float) -> bool:
    """シグナルが古すぎる／未来すぎるなら True（max_age_sec<=0 で常に False=無効）。"""
    if max_age_sec <= 0:
        return False
    age = now - ts
    return age > max_age_sec or age < -max_age_sec


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

    # ストップ距離（リスク基準サイジングの入力）。明示の stop_distance を優先し、
    # 無ければ stop_price と約定基準価格から距離を導出する。どちらも無ければ None。
    stop_distance = _coerce_stop_distance(raw, price)
    # 利確距離（非対称性 R:R 判定の入力）。tp_distance / take_profit / target から導出。
    tp_distance = _coerce_tp_distance(raw, price)
    # トレード根拠（規律ゲート・ジャーナル用）。理由を文章化できないなら入らない。
    reason = raw.get("reason") or raw.get("comment")
    reason = str(reason).strip() if reason not in (None, "") else None

    # 発注意図。TradingView からも "intent":"exit" で手仕舞い（保護ストップ取消＋素通し）を
    # 指示できるようにする。未指定/不明は "entry"（新規）。
    intent_raw = str(raw.get("intent", "")).strip().lower()
    intent = intent_raw if intent_raw in _EXIT_INTENTS else "entry"

    # 鮮度判定用の時刻。TradingView は ``{{timenow}}``（発火時刻・ISO）を推奨。
    # 何も無ければ受信時刻（= 常に新鮮扱い）。bar 時刻 ``{{time}}`` は古くなりうるので非推奨。
    ts = parse_ts(raw.get("ts") or raw.get("timenow") or raw.get("time"))

    return {
        "source": source,
        "symbol": symbol,
        "asset": asset,
        "side": side,
        "qty": qty,
        "type": otype,
        "price": price,
        "stop_distance": stop_distance,
        "tp_distance": tp_distance,
        "reason": reason,
        "intent": intent,
        "ts": ts,
        "idem": compute_idem(raw),
    }


def _coerce_stop_distance(raw: dict[str, Any], price: float | None) -> float | None:
    """stop_distance（正の価格距離）を導出する。

    優先順位:
      1. ``stop_distance``（明示・正の値）
      2. ``stop_price`` と基準価格（``price`` / ``entry``）の差の絶対値
    解釈できない／非正なら None（= サイジングはシグナル qty にフォールバック）。
    """
    sd = raw.get("stop_distance")
    if sd is not None and sd != "":
        try:
            val = float(sd)
        except (TypeError, ValueError):
            raise SignalError(f"invalid stop_distance: {sd!r}") from None
        return val if val > 0 else None

    sp = raw.get("stop_price")
    if sp is not None and sp != "":
        try:
            stop_price = float(sp)
        except (TypeError, ValueError):
            raise SignalError(f"invalid stop_price: {sp!r}") from None
        ref = price
        if ref is None:
            ref_raw = raw.get("entry") or raw.get("entry_price")
            if ref_raw not in (None, ""):
                try:
                    ref = float(ref_raw)
                except (TypeError, ValueError):
                    ref = None
        if ref is not None:
            dist = abs(ref - stop_price)
            return dist if dist > 0 else None
    return None


def _coerce_tp_distance(raw: dict[str, Any], price: float | None) -> float | None:
    """利確距離（正の価格距離）を導出する。

    優先順位:
      1. ``tp_distance``（明示・正の値）
      2. ``take_profit`` / ``target``（利確価格）と基準価格の差の絶対値
    解釈できない／非正なら None（= R:R 判定はスキップ）。
    """
    td = raw.get("tp_distance")
    if td is not None and td != "":
        try:
            val = float(td)
        except (TypeError, ValueError):
            raise SignalError(f"invalid tp_distance: {td!r}") from None
        return val if val > 0 else None

    tp = raw.get("take_profit") or raw.get("target")
    if tp is not None and tp != "":
        try:
            tp_price = float(tp)
        except (TypeError, ValueError):
            raise SignalError(f"invalid take_profit: {tp!r}") from None
        ref = price
        if ref is None:
            ref_raw = raw.get("entry") or raw.get("entry_price")
            if ref_raw not in (None, ""):
                try:
                    ref = float(ref_raw)
                except (TypeError, ValueError):
                    ref = None
        if ref is not None:
            dist = abs(tp_price - ref)
            return dist if dist > 0 else None
    return None


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

    注意: 祝日は未考慮（拡張点）。本番では市場休日カレンダーの注入を推奨。
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
    """FX は概ね 日曜21:00UTC 〜 金曜21:00UTC（≒NYクローズ）。"""
    u = now.astimezone(UTC)
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
    if j.weekday() >= 5:
        return False
    m = j.hour * 60 + j.minute
    morning = 9 * 60 <= m < 11 * 60 + 30
    afternoon = 12 * 60 + 30 <= m < 15 * 60
    return morning or afternoon


def _us_equity_open(now: datetime) -> bool:
    e = now.astimezone(ET)
    if e.weekday() >= 5:
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
