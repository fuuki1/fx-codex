"""マルチタイムフレーム(MTF)アドバイザリー分析（純粋ロジック）。

このプロジェクトのバックテスタ（MA クロス + ATR）とリスク観点を、**実売買しない助言**として
再利用する。上位足(HTF)でトレンド方向を、下位足(LTF)でエントリー・タイミングを見る:

  - HTF トレンド = fast SMA と slow SMA の位置（+1 上昇 / -1 下降 / 0 中立）
  - LTF シグナル = 同じ MA クロス
  - **助言** = HTF と LTF が同方向のときだけ「入る」。直近でクロスしたら "strong"（好機）、
    既にトレンド継続中なら "setup"（押し目/戻り待ち）。不一致・セッション外・データ不足は "WAIT"。
  - 損切り = ATR × 倍率（バックテストの ATR ストップと同義）、利確 = ストップ距離 × R:R 目標。

すべて「そのバーまで」の情報で計算し、外部 I/O を持たない（単体テスト可能）。
"""
from __future__ import annotations

import time as _time
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd
from domain import within_session
from risk_engine import reward_risk_ratio


# ============================================================================
# 指標（バックテスタと同義の SMA / ATR）
# ============================================================================
def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def atr(df: pd.DataFrame, window: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window).mean()


def trend_state(df: pd.DataFrame, fast: int, slow: int) -> int:
    """最新バーの MA 位置（+1/-1/0）。データ不足や NaN は 0（中立）。"""
    if len(df) < slow + 1:
        return 0
    f = sma(df["close"], fast).iloc[-1]
    s = sma(df["close"], slow).iloc[-1]
    if pd.isna(f) or pd.isna(s):
        return 0
    return 1 if f > s else -1 if f < s else 0


def fresh_cross(df: pd.DataFrame, fast: int, slow: int) -> int:
    """直近バーでクロスが発生していれば新方向（+1/-1）、無ければ 0。"""
    if len(df) < slow + 2:
        return 0
    f = sma(df["close"], fast)
    s = sma(df["close"], slow)
    prev = f.iloc[-2] - s.iloc[-2]
    now = f.iloc[-1] - s.iloc[-1]
    if pd.isna(prev) or pd.isna(now):
        return 0
    prev_sign = 1 if prev > 0 else -1 if prev < 0 else 0
    now_sign = 1 if now > 0 else -1 if now < 0 else 0
    if now_sign == 0 or prev_sign == now_sign:
        return 0
    return now_sign


# ============================================================================
# 助言
# ============================================================================
@dataclass(frozen=True)
class Recommendation:
    action: str                       # "BUY" | "SELL" | "WAIT"
    strength: str                     # "strong"（好機）| "setup"（待ち）| "none"
    last_price: float
    entry: float | None
    stop: float | None
    take_profit: float | None
    stop_distance: float | None
    rr: float | None                  # 報酬/リスク比（= rr_target）
    trend_htf: int                    # 上位足トレンド（+1/-1/0）
    signal_ltf: int                   # 下位足シグナル（+1/-1/0）
    fresh_cross: int                  # 直近クロス（+1/-1/0）
    session_open: bool
    reasons: list[str] = field(default_factory=list)
    ts: float = 0.0                   # 分析時刻（epoch 秒）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze(
    ltf: pd.DataFrame,
    htf: pd.DataFrame,
    params: dict[str, Any],
    *,
    symbol: str = "USDJPY",
    now: Any = None,
) -> Recommendation:
    """下位足(ltf)・上位足(htf)の OHLC から売買タイミングの助言を作る。"""
    fast = int(params.get("fast_window", 20))
    slow = int(params.get("slow_window", 60))
    atr_w = int(params.get("atr_window", 14))
    atr_m = float(params.get("atr_multiple", 1.5))
    rr_target = float(params.get("rr_target", 1.5))

    ts = _time.time()
    last_price = float(ltf["close"].iloc[-1]) if len(ltf) else 0.0
    trend_htf = trend_state(htf, fast, slow)
    signal_ltf = trend_state(ltf, fast, slow)
    cross = fresh_cross(ltf, fast, slow)
    session_open = within_session("fx", symbol, now)

    def wait(reasons: list[str], strength: str = "none") -> Recommendation:
        return Recommendation(
            action="WAIT", strength=strength, last_price=last_price, entry=None, stop=None,
            take_profit=None, stop_distance=None, rr=None, trend_htf=trend_htf,
            signal_ltf=signal_ltf, fresh_cross=cross, session_open=session_open,
            reasons=reasons, ts=ts,
        )

    if len(ltf) < slow + 1 or len(htf) < slow + 1:
        return wait(["データ不足（バー数が足りない）"])
    if not session_open:
        return wait(["セッション外（FX は土日クローズ等）— 新規は待つ"])
    if trend_htf == 0 or signal_ltf == 0 or trend_htf != signal_ltf:
        return wait(
            [f"上位足トレンド({_dir(trend_htf)})と下位足({_dir(signal_ltf)})が不一致 — 様子見"]
        )

    direction = trend_htf  # ここでは trend_htf == signal_ltf（±1）
    atr_ltf = atr(ltf, atr_w).iloc[-1]
    stop_distance = float(atr_ltf) * atr_m if pd.notna(atr_ltf) else None
    if not stop_distance or stop_distance <= 0:
        return wait(["ATR が算出できない/ゼロ（ストップ距離を出せない）"])

    entry = last_price
    stop = entry - direction * stop_distance
    take_profit = entry + direction * stop_distance * rr_target
    rr = reward_risk_ratio(stop_distance * rr_target, stop_distance)

    strong = cross == direction
    reasons = [
        f"上位足({_tf_label('htf')})トレンド {_dir(direction)}",
        f"下位足({_tf_label('ltf')})も {_dir(direction)} で一致",
        "直近でクロス発生（好機）" if strong else "トレンド継続中（押し目/戻り目を待つ）",
        f"損切り {stop:.3f}（ATR×{atr_m:g}）/ 利確 {take_profit:.3f}（{rr_target:g}R）",
        "セッション内",
    ]
    action = "BUY" if direction > 0 else "SELL"
    return Recommendation(
        action=action, strength="strong" if strong else "setup", last_price=last_price,
        entry=entry, stop=stop, take_profit=take_profit, stop_distance=stop_distance, rr=rr,
        trend_htf=trend_htf, signal_ltf=signal_ltf, fresh_cross=cross,
        session_open=session_open, reasons=reasons, ts=ts,
    )


def _dir(sign: int) -> str:
    return "上昇" if sign > 0 else "下降" if sign < 0 else "中立"


# ラベルは params に依存しないため簡易表記（表示用）。
def _tf_label(_which: str) -> str:
    return "HTF" if _which == "htf" else "LTF"
