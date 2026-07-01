"""マルチタイムフレーム(MTF)アドバイザリー分析（純粋ロジック・レジーム対応の多因子合議）。

「トッププロが重視するのは予測ではなくリスクと地合い」というリサーチ結論を、**発注しない助言**
として実装する。素朴な MA クロスは FX の大半を占めるレンジで**ダマシを量産**するため、以下で
底上げする（過剰最適化に強いルールベース）:

  1. レジーム判定: ADX（トレンド強度）× 効率比（一方向性）× ATR パーセンタイル（ボラ）で
     "trend / range / transition" と "high_vol" を判定。
  2. 多因子合議: トレンド(EMA/KAMA)・モメンタム(RSI/ROC)・ブレイクアウト(ドンチャン)・
     平均回帰(z スコア) を各 [-1,1] に正規化し、**レジーム別の重み**で合成（trend では
     順張り、range では逆張りを重視）。
  3. MTF: 上位足の合議でトレンド方向を、下位足の合議でタイミングを取り、**同方向で一致**した
     ときだけ「入る」。
  4. 適応エグジット: 損切り = ATR × レジーム別倍率（高ボラで拡大）、利確 = ストップ × レジーム別 R:R。

すべて「そのバーまで」の情報で計算し、外部 I/O を持たない（単体テスト可能）。
"""
from __future__ import annotations

import time as _time
from dataclasses import asdict, dataclass, field
from typing import Any

import indicators as ind
import numpy as np
import pandas as pd
from domain import within_session
from risk_engine import reward_risk_ratio


# ============================================================================
# レジーム判定
# ============================================================================
@dataclass(frozen=True)
class Regime:
    label: str                # "trend" | "range" | "transition"
    adx: float
    efficiency_ratio: float
    atr_pct: float
    high_vol: bool


def classify_regime(df: pd.DataFrame, params: dict[str, Any]) -> Regime:
    adx_w = int(params.get("adx_window", 14))
    er_w = int(params.get("er_window", 10))
    atr_w = int(params.get("atr_window", 14))
    lookback = int(params.get("atr_lookback", 100))
    adx_trend = float(params.get("adx_trend", 25.0))
    adx_range = float(params.get("adx_range", 18.0))
    er_trend = float(params.get("er_trend", 0.30))
    er_range = float(params.get("er_range", 0.20))

    adx_s = ind.adx(df, adx_w)
    adx_v = float(adx_s.iloc[-1]) if len(adx_s) and pd.notna(adx_s.iloc[-1]) else float("nan")
    er_s = ind.efficiency_ratio(df["close"], er_w)
    er_v = float(er_s.iloc[-1]) if len(er_s) and pd.notna(er_s.iloc[-1]) else float("nan")
    atr_pct = ind.atr_percentile(df, atr_w, lookback)

    if pd.notna(adx_v) and pd.notna(er_v) and adx_v >= adx_trend and er_v >= er_trend:
        label = "trend"
    elif (pd.notna(adx_v) and adx_v < adx_range) or (pd.notna(er_v) and er_v < er_range):
        label = "range"
    else:
        label = "transition"
    return Regime(label=label, adx=adx_v, efficiency_ratio=er_v, atr_pct=atr_pct,
                  high_vol=atr_pct >= 0.85)


# ============================================================================
# 多因子スコア（各 [-1, 1]）と合議
# ============================================================================
def _f(x: float) -> float:
    return 0.0 if x is None or pd.isna(x) else float(x)


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def factor_scores(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, float]:
    """トレンド/モメンタム/ブレイクアウト/平均回帰のスコア（各 [-1,1]）。"""
    close = df["close"]
    fast = int(params.get("fast_window", 20))
    slow = int(params.get("slow_window", 60))
    atr_w = int(params.get("atr_window", 14))
    er_w = int(params.get("er_window", 10))
    rsi_w = int(params.get("rsi_window", 14))
    roc_w = int(params.get("roc_window", 10))
    bb_w = int(params.get("bb_window", 20))
    don_w = int(params.get("donchian_window", 20))

    a = _f(ind.atr(df, atr_w).iloc[-1])
    ema_f = ind.ema(close, fast)
    ema_s = ind.ema(close, slow)
    gap = (_f(ema_f.iloc[-1]) - _f(ema_s.iloc[-1])) / a if a > 0 else 0.0
    kama_slope = ind.slope_sign(ind.kama(close, er_w), 3)
    trend = _clip(0.6 * np.tanh(gap) + 0.4 * kama_slope)

    rsi_v = _f(ind.rsi(close, rsi_w).iloc[-1])
    roc_v = _f(ind.roc(close, roc_w).iloc[-1])
    momentum = _clip(0.5 * ((rsi_v - 50.0) / 50.0) + 0.5 * np.tanh(roc_v))

    breakout = _clip(_f(ind.donchian_position(df, don_w).iloc[-1]))

    z = _f(ind.bollinger_z(close, bb_w).iloc[-1])
    mean_reversion = _clip(-z / 2.0)

    return {"trend": trend, "momentum": momentum, "breakout": breakout,
            "mean_reversion": mean_reversion}


# レジーム別の因子ウェイト。trend では順張り(trend/breakout/momentum)を、range では
# 逆張り(mean_reversion)を重視する。合計は 1.0。
_WEIGHTS: dict[str, dict[str, float]] = {
    "trend":      {"trend": 0.40, "momentum": 0.25, "breakout": 0.30, "mean_reversion": 0.05},
    "transition": {"trend": 0.30, "momentum": 0.25, "breakout": 0.20, "mean_reversion": 0.25},
    "range":      {"trend": 0.10, "momentum": 0.15, "breakout": 0.05, "mean_reversion": 0.70},
}


def ensemble_score(scores: dict[str, float], regime_label: str) -> float:
    """レジーム別ウェイトで因子スコアを合成（[-1,1]）。"""
    w = _WEIGHTS.get(regime_label, _WEIGHTS["transition"])
    return _clip(sum(w[k] * scores.get(k, 0.0) for k in w))


# レジーム別の適応エグジット。損切り = ATR × 倍率、利確 = ストップ × R:R。
_STOP_MULT = {"trend": 2.2, "transition": 1.8, "range": 1.3}
_RR_TARGET = {"trend": 2.2, "transition": 1.7, "range": 1.2}


# ============================================================================
# 助言
# ============================================================================
@dataclass(frozen=True)
class Recommendation:
    action: str                       # "BUY" | "SELL" | "WAIT"
    strength: str                     # "strong" | "setup" | "none"
    last_price: float
    entry: float | None
    stop: float | None
    take_profit: float | None
    stop_distance: float | None
    rr: float | None
    trend_htf: int                    # 上位足バイアス（+1/-1/0）
    signal_ltf: int                   # 下位足バイアス（+1/-1/0）
    fresh_cross: int                  # 下位足の直近クロス（+1/-1/0）
    session_open: bool
    regime: str = "unknown"           # 下位足レジーム
    regime_htf: str = "unknown"       # 上位足レジーム
    conviction: float = 0.0           # 確信度（0..1）
    score_htf: float = 0.0            # 上位足の合議スコア（[-1,1]）
    score_ltf: float = 0.0            # 下位足の合議スコア（[-1,1]）
    adx: float = 0.0
    efficiency_ratio: float = 0.0
    atr_pct: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)  # 下位足の因子内訳
    reasons: list[str] = field(default_factory=list)
    ts: float = 0.0

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
    """下位足(ltf)・上位足(htf)からレジーム対応・多因子合議で売買タイミングを助言する。"""
    slow = int(params.get("slow_window", 60))
    atr_w = int(params.get("atr_window", 14))
    thr = float(params.get("signal_threshold", 0.25))

    ts = _time.time()
    last_price = float(ltf["close"].iloc[-1]) if len(ltf) else 0.0
    session_open = within_session("fx", symbol, now)

    # 十分なバー数（指標が確定する）を確保。
    need = slow + max(int(params.get("adx_window", 14)), atr_w) + 5
    if len(ltf) < need or len(htf) < need:
        return _wait(last_price, session_open, ["データ不足（バー数が足りない）"], ts)
    if not session_open:
        return _wait(last_price, session_open, ["セッション外（FX は土日クローズ等）— 新規は待つ"], ts)

    htf_regime = classify_regime(htf, params)
    htf_scores = factor_scores(htf, params)
    score_htf = ensemble_score(htf_scores, htf_regime.label)

    ltf_regime = classify_regime(ltf, params)
    ltf_scores = factor_scores(ltf, params)
    score_ltf = ensemble_score(ltf_scores, ltf_regime.label)

    ema_f = ind.ema(ltf["close"], int(params.get("fast_window", 20)))
    ema_s = ind.ema(ltf["close"], slow)
    cross = ind.fresh_cross(ema_f, ema_s)

    common_kw = dict(
        trend_htf=_sign(score_htf, thr), signal_ltf=_sign(score_ltf, thr), fresh_cross=cross,
        regime=ltf_regime.label, regime_htf=htf_regime.label,
        score_htf=round(score_htf, 3), score_ltf=round(score_ltf, 3),
        adx=round(_f(ltf_regime.adx), 1), efficiency_ratio=round(_f(ltf_regime.efficiency_ratio), 3),
        atr_pct=round(ltf_regime.atr_pct, 2),
        factors={k: round(v, 3) for k, v in ltf_scores.items()},
    )

    # 上位足に明確なトレンド・バイアスが無い（レンジ or 弱い）なら見送る（順張り規律）。
    htf_bias = _sign(score_htf, thr)
    if htf_bias == 0 or htf_regime.label == "range":
        return _wait(
            last_price, session_open,
            [f"上位足に明確なトレンド無し（{htf_regime.label} / スコア {score_htf:+.2f}）— 様子見"],
            ts, **common_kw,
        )

    direction = htf_bias
    # 下位足が上位足と同方向で、かつ一定の確信度に達していなければ「待ち」。
    if _sign(score_ltf, thr) != direction:
        return _wait(
            last_price, session_open,
            [
                f"上位足は{_dir(direction)}（{htf_regime.label}）だが下位足が未一致"
                f"（スコア {score_ltf:+.2f}）— タイミング待ち",
            ],
            ts, **common_kw,
        )

    atr_ltf = _f(ind.atr(ltf, atr_w).iloc[-1])
    if atr_ltf <= 0:
        return _wait(last_price, session_open, ["ATR がゼロ（ストップ距離を出せない）"], ts, **common_kw)

    mult = _STOP_MULT.get(ltf_regime.label, 1.8) * (1.25 if ltf_regime.high_vol else 1.0)
    rr_target = _RR_TARGET.get(ltf_regime.label, 1.7)
    stop_distance = atr_ltf * mult
    entry = last_price
    stop = entry - direction * stop_distance
    take_profit = entry + direction * stop_distance * rr_target
    rr = reward_risk_ratio(stop_distance * rr_target, stop_distance)

    conviction = _clip(0.5 * abs(score_htf) + 0.5 * abs(score_ltf))
    strong = cross == direction and conviction >= 0.4
    action = "BUY" if direction > 0 else "SELL"
    reasons = [
        f"レジーム: 上位足 {htf_regime.label} / 下位足 {ltf_regime.label}"
        f"（ADX {_f(ltf_regime.adx):.0f} / 効率比 {_f(ltf_regime.efficiency_ratio):.2f}）",
        f"合議スコア: 上位足 {score_htf:+.2f} / 下位足 {score_ltf:+.2f} → {_dir(direction)}で一致",
        "因子: " + " / ".join(f"{k}{v:+.2f}" for k, v in ltf_scores.items()),
        "直近クロス発生（好機）" if strong else "トレンド継続中（押し目/戻り目を待つ）",
        f"損切り {stop:.3f}（ATR×{mult:.2f}）/ 利確 {take_profit:.3f}（{rr_target:.1f}R）"
        + ("／高ボラ→ストップ拡大" if ltf_regime.high_vol else ""),
        f"確信度 {conviction:.0%}",
    ]
    return Recommendation(
        action=action, strength="strong" if strong else "setup", last_price=last_price,
        entry=entry, stop=stop, take_profit=take_profit, stop_distance=stop_distance, rr=rr,
        session_open=session_open, conviction=round(conviction, 3), reasons=reasons, ts=ts,
        **common_kw,
    )


def _wait(last_price: float, session_open: bool, reasons: list[str], ts: float, **kw: Any) -> Recommendation:
    base: dict[str, Any] = dict(
        trend_htf=0, signal_ltf=0, fresh_cross=0, regime="unknown", regime_htf="unknown",
        score_htf=0.0, score_ltf=0.0, adx=0.0, efficiency_ratio=0.0, atr_pct=0.0, factors={},
    )
    base.update(kw)
    return Recommendation(
        action="WAIT", strength="none", last_price=last_price, entry=None, stop=None,
        take_profit=None, stop_distance=None, rr=None, session_open=session_open,
        conviction=0.0, reasons=reasons, ts=ts, **base,
    )


def _sign(score: float, threshold: float) -> int:
    if score >= threshold:
        return 1
    if score <= -threshold:
        return -1
    return 0


def _dir(sign: int) -> str:
    return "上昇" if sign > 0 else "下降" if sign < 0 else "中立"
