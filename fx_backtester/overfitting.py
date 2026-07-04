"""過剰最適化の統計検定: PBO(CSCV)・Deflated Sharpe Ratio・SPA検定。

試行ログ(trial_log.py)が記録した「探索した全試行」を入力に、
選ばれたパラメータが「本物のエッジ」か「多重検定の当たりくじ」かを推定する。

- PBO: Bailey, Borwein, López de Prado, Zhu (2015)
  "The Probability of Backtest Overfitting" の CSCV
  (Combinatorially Symmetric Cross-Validation)。
  時系列をS個のブロックに分け、半分をIS・半分をOOSとする全C(S, S/2)通りで
  「ISで最良だった試行がOOSで中央値未満に落ちる確率」を数える。
- DSR: Bailey & López de Prado (2014) "The Deflated Sharpe Ratio"。
  N回の探索でまぐれ当たりが達成しうる期待最大Sharpe(SR*)を差し引いた上で、
  観測Sharpeが偶然を上回る確率を歪度・尖度込みで返す。
- SPA: Hansen (2005) "A Test for Superior Predictive Ability"。
  多数の戦略を跨いで「最良戦略の優位はデータマイニングの産物か、真の予測力か」を
  定常ブートストラップで検定する。帰無仮説「どの戦略もベンチマーク超の期待性能を
  持たない」を、最良戦略の平均超過性能の分布から棄却できるか(p値)で判断する。

依存は numpy/pandas のみ(scipy 非依存。正規分布の逆CDFは Acklam 近似を実装)。
Sharpe は全試行が同じ足種であれば年率化不要(順位・比較はスケール不変)のため、
本モジュールは per-period Sharpe で統一する。
"""

from __future__ import annotations

import math
from itertools import combinations
from typing import Any
from collections.abc import Sequence

import numpy as np
import pandas as pd

# Euler-Mascheroni 定数(期待最大値の近似式で使用)
_EULER_GAMMA = 0.5772156649015329

# 実質的に「分散0」とみなす閾値。定数系列でも浮動小数の丸めでstdが~1e-18程度
# 残ることがあり、0との厳密比較では退化ケースを検出できない
_MIN_STD = 1e-12


def norm_cdf(x: float) -> float:
    """標準正規分布の累積分布関数。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """標準正規分布の逆CDF(Acklam 2003 の有理近似、相対誤差 ~1.15e-9)。"""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p は (0, 1) の範囲であること: {p}")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    p_low, p_high = 0.02425, 1.0 - 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > p_high:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    )


def per_period_sharpe(returns: pd.Series | np.ndarray) -> float:
    """per-period の Sharpe(mean/std, ddof=1)。分散0や観測不足は0を返す。"""
    series = pd.Series(returns, dtype=float).dropna()
    if len(series) < 2:
        return 0.0
    std = float(series.std(ddof=1))
    if std <= _MIN_STD or not math.isfinite(std):
        return 0.0
    return float(series.mean()) / std


def expected_max_sharpe(n_trials: int, trial_sharpe_variance: float) -> float:
    """N回の独立試行でまぐれが達成しうる期待最大Sharpe(SR*)。

    E[max] ≈ sqrt(V) * ((1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e)))
    N<2 または V<=0 なら探索による上振れは無いとみなし 0 を返す。
    """
    if n_trials < 2 or trial_sharpe_variance <= 0.0:
        return 0.0
    scale = math.sqrt(trial_sharpe_variance)
    return scale * (
        (1.0 - _EULER_GAMMA) * norm_ppf(1.0 - 1.0 / n_trials)
        + _EULER_GAMMA * norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    )


def deflated_sharpe_ratio(
    selected_returns: pd.Series | np.ndarray,
    trial_sharpes: Sequence[float],
) -> dict[str, Any]:
    """採択された戦略の Deflated Sharpe Ratio。

    selected_returns: 採択試行の per-period リターン系列(探索に使った区間のもの)。
    trial_sharpes:    探索した「全試行」の per-period Sharpe(採択自身を含む)。

    戻り値の dsr は「観測Sharpeが、N回探索のまぐれ期待値(SR*)を超えて
    本物である確率」。慣例的に 0.95 以上で合格とみなす。
    """
    series = pd.Series(selected_returns, dtype=float).dropna()
    n_obs = len(series)
    if n_obs < 3:
        raise ValueError(f"リターン観測数が不足({n_obs}件)。DSRには3件以上が必要")
    std = float(series.std(ddof=1))
    if std <= _MIN_STD or not math.isfinite(std):
        raise ValueError("リターンの分散が実質0のためSharpeが定義できない")

    finite_sharpes = np.asarray(
        [s for s in trial_sharpes if isinstance(s, int | float) and math.isfinite(s)],
        dtype=float,
    )
    n_trials = int(finite_sharpes.size)
    if n_trials < 1:
        raise ValueError("有効な試行Sharpeが1件も無い")
    variance = float(finite_sharpes.var(ddof=1)) if n_trials >= 2 else 0.0

    sharpe = float(series.mean()) / std
    sr_star = expected_max_sharpe(n_trials, variance)
    skewness = float(series.skew())
    kurtosis = float(series.kurt()) + 3.0  # pandasは過剰尖度を返すため正規=3に戻す

    denominator = 1.0 - skewness * sharpe + (kurtosis - 1.0) / 4.0 * sharpe**2
    if denominator <= 0.0 or not math.isfinite(denominator):
        raise ValueError(
            f"歪度・尖度の補正項が不正(denominator={denominator:.6f})でDSRを計算できない"
        )

    statistic = (sharpe - sr_star) * math.sqrt(n_obs - 1.0) / math.sqrt(denominator)
    return {
        "dsr": norm_cdf(statistic),
        "sharpe_per_period": sharpe,
        "expected_max_sharpe": sr_star,
        "n_trials": n_trials,
        "trial_sharpe_variance": variance,
        "n_observations": n_obs,
        "skewness": skewness,
        "kurtosis": kurtosis,
    }


def _sharpe_from_moments(sums: np.ndarray, squares: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """ブロック集計(合計・二乗和・件数)から per-period Sharpe を一括計算する。

    分散が0以下になる退化ケース(定数リターン等)は比較不能としてSharpe 0を割り当て、
    ±inf の伝播を防ぐ。
    """
    counts = counts.astype(float)
    mean = sums / counts
    variance = (squares - sums**2 / counts) / (counts - 1.0)
    std = np.sqrt(np.clip(variance, 0.0, None))
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpe = np.where(std > 0.0, mean / std, 0.0)
    return sharpe


def _safe_linfit(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """1次回帰(傾き, 切片)。退化データ(定数・非有限)では polyfit が LinAlgError や
    LAPACK 警告を出すため、事前に有限性・分散を確認し、不能なら (NaN, NaN) を返す。
    """
    finite = np.isfinite(x) & np.isfinite(y)
    xf, yf = x[finite], y[finite]
    if len(xf) < 2 or float(np.std(xf)) <= _MIN_STD:
        return float("nan"), float("nan")
    try:
        slope, intercept = np.polyfit(xf, yf, 1)
    except (np.linalg.LinAlgError, ValueError):
        return float("nan"), float("nan")
    return float(slope), float(intercept)


def probability_of_backtest_overfitting(
    matrix: pd.DataFrame,
    n_blocks: int = 16,
    min_observations_per_block: int = 2,
) -> dict[str, Any]:
    """CSCV による PBO(Probability of Backtest Overfitting)。

    matrix: 時刻×試行のリターン行列(trial_log.TrialLogger.returns_matrix() の出力)。
            欠測(NaN)は「その時点でポジション無し」として0リターン扱いにする。

    PBO は「ISで最良に見えた試行が、OOSでは全試行の中央値未満に沈む確率」。
    0.5 なら IS の順位に OOS 予測力が全く無い(=完全にノイズ選択)ことを意味し、
    それ以上は有害な過剰最適化を示唆する。合わせて以下も返す:
    - prob_oos_loss: 採択試行の OOS Sharpe が負になる組み合わせの割合
    - degradation_slope/intercept: 採択試行の IS→OOS Sharpe の線形回帰(劣化度)
    """
    if n_blocks < 4 or n_blocks % 2 != 0:
        raise ValueError(f"n_blocks は4以上の偶数であること: {n_blocks}")
    if matrix is None or matrix.empty:
        raise ValueError("リターン行列が空")
    values = matrix.to_numpy(dtype=float)
    values = np.where(np.isfinite(values), values, 0.0)
    n_obs, n_trials = values.shape
    if n_trials < 2:
        raise ValueError(f"試行が{n_trials}件ではPBOを計算できない(2件以上必要)")
    if n_obs < n_blocks * min_observations_per_block:
        raise ValueError(
            f"観測数が不足: {n_obs}(最低 {n_blocks}ブロック × {min_observations_per_block}件)"
        )

    # ブロックごとの十分統計量を先に集計し、組み合わせ側は行列積だけにする
    blocks = np.array_split(np.arange(n_obs), n_blocks)
    block_sums = np.stack([values[idx].sum(axis=0) for idx in blocks])  # (S, N)
    block_squares = np.stack([(values[idx] ** 2).sum(axis=0) for idx in blocks])
    block_counts = np.array([len(idx) for idx in blocks], dtype=float)  # (S,)

    combos = list(combinations(range(n_blocks), n_blocks // 2))
    mask = np.zeros((len(combos), n_blocks))
    for row, combo in enumerate(combos):
        mask[row, list(combo)] = 1.0

    is_sharpe = _sharpe_from_moments(
        mask @ block_sums, mask @ block_squares, (mask @ block_counts)[:, None]
    )
    oos_mask = 1.0 - mask
    oos_sharpe = _sharpe_from_moments(
        oos_mask @ block_sums, oos_mask @ block_squares, (oos_mask @ block_counts)[:, None]
    )

    best_idx = np.argmax(is_sharpe, axis=1)
    rows = np.arange(len(combos))
    selected_is = is_sharpe[rows, best_idx]
    selected_oos = oos_sharpe[rows, best_idx]

    # 採択試行のOOS順位(midrank)から相対順位ωとロジットλを出す
    count_less = (oos_sharpe < selected_oos[:, None]).sum(axis=1)
    count_equal = (oos_sharpe == selected_oos[:, None]).sum(axis=1)
    rank = count_less + (count_equal + 1.0) / 2.0
    omega = rank / (n_trials + 1.0)
    lam = np.log(omega / (1.0 - omega))

    # 劣化度回帰(診断用)。定数系列など退化ケースで polyfit は LinAlgError を投げ
    # LAPACK 警告も漏らすため、失敗時は slope/intercept を NaN にして PBO 本体は返す。
    slope, intercept = _safe_linfit(selected_is, selected_oos)
    return {
        "pbo": float(np.mean(lam < 0.0) + 0.5 * np.mean(lam == 0.0)),
        "n_trials": int(n_trials),
        "n_blocks": int(n_blocks),
        "n_combinations": int(len(combos)),
        "n_observations": int(n_obs),
        "lambda_median": float(np.median(lam)),
        "prob_oos_loss": float(np.mean(selected_oos < 0.0)),
        "degradation_slope": float(slope),
        "degradation_intercept": float(intercept),
    }


def _stationary_bootstrap_indices(
    n_obs: int, avg_block: float, rng: np.random.Generator
) -> np.ndarray:
    """Politis-Romano の定常ブートストラップで長さ n_obs のインデックス列を作る。

    各ステップで確率 1/avg_block で新しい開始点へ跳び、それ以外は連続で進む
    (幾何分布のブロック長)。時系列の自己相関を保ったまま再標本化するため、
    SPA検定の帰無分布に i.i.d. ブートストラップより適する。
    """
    p = 1.0 / max(avg_block, 1.0)
    indices = np.empty(n_obs, dtype=int)
    current = int(rng.integers(0, n_obs))
    for i in range(n_obs):
        indices[i] = current
        if rng.random() < p:
            current = int(rng.integers(0, n_obs))
        else:
            current = (current + 1) % n_obs
    return indices


def superior_predictive_ability(
    performance: pd.DataFrame,
    *,
    n_bootstrap: int = 1000,
    avg_block: float = 10.0,
    seed: int | None = None,
) -> dict[str, Any]:
    """Hansen (2005) の SPA検定。多数戦略の中の最良の優位が本物かを検定する。

    performance: 時刻×戦略の「超過性能」行列(各セル = 戦略のリターン − ベンチマーク。
                 ベンチマークが0=無リスクなら戦略リターンそのもの)。列=戦略、行=期間。

    帰無仮説 H0:「どの戦略もベンチマークを上回る期待性能を持たない」。
    各戦略の平均超過性能 d̄_k を標準化した統計量の最大値 T = max_k √n·d̄_k/ω_k を
    観測し、定常ブートストラップで H0 下(平均を中心化)の分布と比べて p値を出す。
    p値が小さいほど「最良戦略の優位はデータマイニングでは説明できない=真の予測力」。

    戻り値: spa_pvalue(小さいほど有意)、best_strategy(列名)、test_statistic、
    n_strategies、n_observations。scipy 非依存(乱数は numpy Generator)。
    """
    if performance is None or performance.empty:
        raise ValueError("performance 行列が空")
    values = performance.to_numpy(dtype=float)
    values = np.where(np.isfinite(values), values, np.nan)
    # 全 NaN 行/列を落とし、残る NaN は 0(その期間ポジション無し)扱い
    values = np.where(np.isnan(values), 0.0, values)
    n_obs, n_strategies = values.shape
    if n_strategies < 1:
        raise ValueError("戦略が1つも無い")
    if n_obs < 3:
        raise ValueError(f"観測数が不足({n_obs}件)。SPAには3件以上が必要")

    means = values.mean(axis=0)  # d̄_k
    # 各戦略の分散(標準化用 ω_k)。分散0は比較不能として大きな値で無効化する
    std = values.std(axis=0, ddof=1)
    omega = np.where(std > _MIN_STD, std, np.inf)
    studentized = np.sqrt(n_obs) * means / omega
    test_statistic = float(np.nanmax(studentized))
    best_idx = int(np.nanargmax(studentized))

    rng = np.random.default_rng(seed)
    # H0 下の中心化: 各戦略から自身の平均を引き「優位ゼロ」の世界を作る
    centered = values - means
    exceed = 0
    for _ in range(n_bootstrap):
        idx = _stationary_bootstrap_indices(n_obs, avg_block, rng)
        sample = centered[idx]
        boot_means = sample.mean(axis=0)
        boot_std = sample.std(axis=0, ddof=1)
        boot_omega = np.where(boot_std > _MIN_STD, boot_std, np.inf)
        boot_stat = float(np.nanmax(np.sqrt(n_obs) * boot_means / boot_omega))
        if boot_stat >= test_statistic:
            exceed += 1

    p_value = (exceed + 1) / (n_bootstrap + 1)  # +1 で 0 を避ける保守的推定
    return {
        "spa_pvalue": float(p_value),
        "best_strategy": performance.columns[best_idx],
        "best_mean_excess": float(means[best_idx]),
        "test_statistic": test_statistic,
        "n_strategies": int(n_strategies),
        "n_observations": int(n_obs),
        "n_bootstrap": int(n_bootstrap),
    }
