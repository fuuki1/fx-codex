"""フラクショナル・ケリーのポジションサイジングと VaR(別枠)の純粋関数。

レポート(FX AI.md)ギャップ⑧「クォーター〜ハーフケリー／VaR別枠」の土台。
risk.py の固定フラクショナル(1%固定)に対し、実現トレードのW/Rとペイオフから
ケリー比率を推定してリスク予算を動的に調整する層を、依存追加ゼロ(numpy/pandasのみ、
本モジュールは標準ライブラリだけ)で提供する。

レポートが繰り返し警告する2点をコードの既定に落とし込む:

1. フルケリーは危険 — 勝率/ペイオフの推定誤差を増幅し、フルケリーで50〜80%の
   ドローダウンもあり得る。「プロはほぼ全員ハーフ〜クォーターケリー」。よって
   既定は fraction=0.25(クォーター)、上限も設ける。過剰ベットは過小ベットより
   はるかに危険(フルケリーの2倍で長期成長率がゼロ)なので、決して 1.0 を超えない。

2. 標本が貯まるまで使わない — 「最低50〜100トレードで統計的に意味あるW/R」。
   min_trades 未満では固定フラクショナルにフォールバックし、min_trades〜
   full_confidence_trades の間はケリーへ線形にブレンドして急な切り替えを避ける。

VaR はレポートの核心「方向予測モデルはVaR推定に使えない(29.1%違反)」に従い、
サイジングとは独立に、実現リターン分布そのものから計算する別枠の監視指標として
提供する(historical / parametric)。サイズ決定には使わず、超過時のゲートに使う。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


# ---------------------------------------------------------------- フラクショナル・ケリー


@dataclass(frozen=True)
class KellyEstimate:
    """実現トレードから推定したケリー比率と、その素性。"""

    kelly_fraction: float  # 生のフルケリー f*(0〜1にクリップ)
    win_rate: float
    payoff_ratio: float  # 平均勝ちR ÷ 平均負けR(b)
    sample_size: int
    usable: bool  # min_trades を満たし推定が有効か
    note: str


def kelly_fraction_from_r_multiples(
    r_multiples: Sequence[float],
    *,
    min_trades: int = 50,
) -> KellyEstimate:
    """実現R倍数列からフルケリー比率 f* = (bp − q)/b を推定する。

    R倍数は「初期リスク(1R)に対する損益の倍数」。勝ち=正、負け=負。
    b = 平均勝ちR / 平均負けR(絶対値)、p = 勝率、q = 1 − p。
    - 負けトレードが1件も無い(b が定義できない)場合は保守的に usable=False。
    - サンプルが min_trades 未満なら usable=False(呼び出し側でフォールバック)。
    f* は [0, 1] にクリップする(負のエッジは 0=張らない、過大値は 1 で頭打ち)。
    """
    values = [float(r) for r in r_multiples if _is_finite(r)]
    n = len(values)
    if n < min_trades:
        return KellyEstimate(
            kelly_fraction=0.0, win_rate=0.0, payoff_ratio=0.0, sample_size=n,
            usable=False, note=f"サンプル不足({n}/{min_trades}トレード)",
        )

    wins = [v for v in values if v > 0]
    losses = [-v for v in values if v < 0]  # 負けの大きさ(正値)
    # 0R(建値撤退等)は勝ちにも負けにも数えないが分母 n には含める
    win_rate = len(wins) / n
    if not wins or not losses:
        return KellyEstimate(
            kelly_fraction=0.0, win_rate=round(win_rate, 4), payoff_ratio=0.0,
            sample_size=n, usable=False,
            note="勝ちまたは負けが皆無でペイオフ比を定義できない",
        )

    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)
    if avg_loss <= 0:
        return KellyEstimate(
            kelly_fraction=0.0, win_rate=round(win_rate, 4), payoff_ratio=0.0,
            sample_size=n, usable=False, note="平均負けRが非正",
        )
    b = avg_win / avg_loss
    p = win_rate
    q = 1.0 - p
    raw = (b * p - q) / b
    kelly = max(0.0, min(1.0, raw))
    note = f"f*={kelly:.3f} (勝率{p:.1%}, ペイオフ{b:.2f}, n={n})"
    return KellyEstimate(
        kelly_fraction=round(kelly, 4), win_rate=round(p, 4),
        payoff_ratio=round(b, 3), sample_size=n, usable=True, note=note,
    )


def fractional_kelly_risk_pct(
    estimate: KellyEstimate,
    *,
    baseline_pct: float,
    fraction: float = 0.25,
    max_risk_pct: float = 0.02,
    full_confidence_trades: int = 100,
) -> tuple[float, str]:
    """ケリー推定から「今回使うリスク%(equityに対する)」を決める。

    - usable=False(標本不足/推定不能)なら baseline_pct をそのまま返す。
    - usable なら target = kelly_fraction × fraction を基本に、標本が
      min〜full_confidence_trades の間は baseline から target へ線形ブレンド
      (急な倍率変化を避ける)。最終値は max_risk_pct でクリップし、
      決して baseline を無闇に上回らせない安全上限を掛ける。
    - fraction は 0<fraction<=1(既定0.25=クォーターケリー)。1.0(フルケリー)は
      レポートが強く戒めるため、呼び出し側が明示しない限り選ばれない。

    戻り値は (risk_pct, 理由文字列)。
    """
    if not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must satisfy 0 < fraction <= 1")
    if baseline_pct <= 0:
        raise ValueError("baseline_pct must be positive")
    if not estimate.usable:
        return baseline_pct, f"ケリー不使用({estimate.note}) → 固定{baseline_pct:.2%}"

    target = estimate.kelly_fraction * fraction
    # 標本数で baseline→target を線形ブレンド(full_confidence_trades で完全移行)
    span = max(full_confidence_trades - _min_trades_of(estimate), 1)
    progress = (estimate.sample_size - _min_trades_of(estimate)) / span
    blend = max(0.0, min(1.0, progress))
    risk = baseline_pct * (1.0 - blend) + target * blend
    risk = max(0.0, min(risk, max_risk_pct))
    label = "クォーター" if abs(fraction - 0.25) < 1e-9 else (
        "ハーフ" if abs(fraction - 0.5) < 1e-9 else f"×{fraction:g}"
    )
    note = (
        f"{label}ケリー: f*{estimate.kelly_fraction:.3f}×{fraction:g}→目標{target:.2%}, "
        f"標本ブレンド{blend:.0%} ⇒ 採用{risk:.2%}"
    )
    return round(risk, 6), note


# min_trades は KellyEstimate に持たないため、note には出さず内部推定に使う既定値。
# kelly_fraction_from_r_multiples の既定(50)と揃える。
_DEFAULT_MIN_TRADES = 50


def _min_trades_of(_estimate: KellyEstimate) -> int:
    return _DEFAULT_MIN_TRADES


# ---------------------------------------------------------------- VaR(別枠)


@dataclass(frozen=True)
class VaREstimate:
    """実現リターン分布から計算した VaR/CVaR(1期間、正値=損失の大きさ)。"""

    var_pct: float  # 信頼水準での損失(正値。例 0.03 = 3%の損失)
    cvar_pct: float  # VaR超過時の平均損失(テール期待値、正値)
    confidence: float
    sample_size: int
    method: str
    usable: bool


def historical_var(
    returns: Sequence[float],
    *,
    confidence: float = 0.95,
    min_samples: int = 30,
) -> VaREstimate:
    """ヒストリカルVaR。実現リターン列の経験分位点から損失側を測る。

    returns は各期間のリターン(equity比の増減、負=損失)。confidence=0.95 なら
    下位5%点を VaR とする。CVaR はその点を下回るリターンの平均。方向モデルとは
    独立に、実現分布そのものからテールリスクを測る(レポートの「VaR別枠」)。
    """
    values = sorted(float(r) for r in returns if _is_finite(r))
    n = len(values)
    if n < min_samples:
        return VaREstimate(0.0, 0.0, confidence, n, "historical", usable=False)
    alpha = 1.0 - confidence
    # 下側 alpha 分位点。VaR は「損失側 alpha 割合の境界」なので、昇順で累積 alpha に
    # 当たる位置 = ceil(alpha*n)-1 を採る。例: 100件・alpha0.05 → index4(下位5件の端)。
    # 1.0-0.95 が 0.05000...4 になる浮動小数誤差で ceil が1つ跳ねるため round で吸収。
    rank = max(0, min(n - 1, int(math.ceil(round(alpha * n, 9))) - 1))
    quantile = values[rank]
    var_pct = max(0.0, -quantile)  # 損失は正値で表す
    tail = values[: rank + 1]  # 分位点以下(=最悪側)のテール
    cvar_pct = max(0.0, -(sum(tail) / len(tail))) if tail else var_pct
    return VaREstimate(
        var_pct=round(var_pct, 6), cvar_pct=round(cvar_pct, 6),
        confidence=confidence, sample_size=n, method="historical", usable=True,
    )


def parametric_var(
    returns: Sequence[float],
    *,
    confidence: float = 0.95,
    min_samples: int = 30,
) -> VaREstimate:
    """ガウス仮定のパラメトリックVaR。VaR = -(μ + zσ)。

    正規分位点 z は依存を増やさないため有理近似(Acklam)で求める。ファットテールを
    過小評価する点は承知の上で、historical と併記して分布仮定の影響を可視化する用途。
    """
    values = [float(r) for r in returns if _is_finite(r)]
    n = len(values)
    if n < min_samples:
        return VaREstimate(0.0, 0.0, confidence, n, "parametric", usable=False)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(variance)
    z = _inv_norm_cdf(1.0 - confidence)  # 下側分位点(負値)
    var_pct = max(0.0, -(mean + z * std))
    # ガウスの CVaR: μ - σ·φ(z_α)/α
    alpha = 1.0 - confidence
    phi = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    cvar_pct = max(0.0, -(mean - std * phi / alpha)) if alpha > 0 else var_pct
    return VaREstimate(
        var_pct=round(var_pct, 6), cvar_pct=round(cvar_pct, 6),
        confidence=confidence, sample_size=n, method="parametric", usable=True,
    )


def var_breached(estimate: VaREstimate, limit_pct: float) -> bool:
    """VaR(損失側、正値)が許容上限を超えたか。usable でなければ False(判定保留)。"""
    if not estimate.usable or limit_pct <= 0:
        return False
    return estimate.var_pct > limit_pct


# ---------------------------------------------------------------- ヘルパ


def _is_finite(value: object) -> bool:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _inv_norm_cdf(p: float) -> float:
    """標準正規の逆累積分布(Acklam の有理近似)。scipy 非依存。

    p∈(0,1)。用途は VaR の分位点なので、実用十分な精度(絶対誤差 ~1e-9)。
    """
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p_low = 0.02425
    p_high = 1.0 - p_low
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
            ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )
