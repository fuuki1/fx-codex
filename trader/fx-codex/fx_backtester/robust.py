"""過剰最適化を定量する堅牢性指標（López de Prado / Bailey ら）。

バックテストの Sharpe が高くても、それが**多数の試行から選ばれた見せかけ**なら live で崩れる。
本モジュールは「そのバックテストは本物か」を測る:

  - Probabilistic Sharpe Ratio (PSR): Sharpe が基準を超える確率（歪度・尖度・標本長を考慮）。
  - Deflated Sharpe Ratio (DSR): 試行回数と試行間 Sharpe 分散で基準を引き上げ、多重検定を補正。
  - Probability of Backtest Overfitting (PBO): CSCV による過剰最適化確率（IS 最良が OOS で沈む割合）。
  - Monte Carlo（定常ブートストラップ）: リターンを再標本化し Sharpe・最大DD の分布を出す。

外部依存は numpy と標準ライブラリ（statistics.NormalDist）のみ。scipy 不要。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations
from statistics import NormalDist

import numpy as np

_N = NormalDist()
_EULER = 0.5772156649015329  # オイラー・マスケローニ定数


def _clean(returns: np.ndarray) -> np.ndarray:
    r = np.asarray(returns, dtype=float).ravel()
    return r[np.isfinite(r)]


def sharpe_ratio(returns: np.ndarray, periods_per_year: float | None = None) -> float:
    """1 観測あたり（または年率）Sharpe。標準偏差 0 なら 0。"""
    r = _clean(returns)
    if len(r) < 2:
        return 0.0
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0
    sr = r.mean() / sd
    return sr * np.sqrt(periods_per_year) if periods_per_year else sr


def _skew_kurt(r: np.ndarray) -> tuple[float, float]:
    """歪度と（非超過）尖度。正規分布の尖度は 3。"""
    n = len(r)
    m = r.mean()
    sd = r.std(ddof=0)
    if sd == 0 or n < 3:
        return 0.0, 3.0
    skew = float(((r - m) ** 3).mean() / sd**3)
    kurt = float(((r - m) ** 4).mean() / sd**4)
    return skew, kurt


def probabilistic_sharpe_ratio(
    returns: np.ndarray, sr_benchmark: float = 0.0
) -> float:
    """PSR: 真の Sharpe が sr_benchmark（1 観測あたり）を超える確率（0..1）。

    Bailey & López de Prado (2012)。歪度 γ3・尖度 γ4・標本長 n を織り込む。
    """
    r = _clean(returns)
    n = len(r)
    if n < 3:
        return 0.0
    sr = sharpe_ratio(r)
    skew, kurt = _skew_kurt(r)
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr**2
    if denom <= 0:
        return float(sr > sr_benchmark)
    z = (sr - sr_benchmark) * np.sqrt(n - 1) / np.sqrt(denom)
    return float(_N.cdf(z))


def expected_max_sharpe(sr_variance: float, n_trials: int) -> float:
    """N 回の独立試行で期待される「最大 Sharpe」（帰無仮説下）。DSR の基準値。"""
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    v = np.sqrt(sr_variance)
    a = _N.inv_cdf(1.0 - 1.0 / n_trials)
    b = _N.inv_cdf(1.0 - 1.0 / (n_trials * np.e))
    return float(v * ((1.0 - _EULER) * a + _EULER * b))


def deflated_sharpe_ratio(
    returns: np.ndarray, n_trials: int, sr_variance: float
) -> float:
    """DSR: 試行回数 n_trials と試行間 Sharpe 分散で基準を引き上げた PSR（多重検定補正）。

    高い（→1）ほど「多数の試行から選ばれた見せかけ」ではなく本物である確度が高い。
    """
    sr_star = expected_max_sharpe(sr_variance, n_trials)
    return probabilistic_sharpe_ratio(returns, sr_benchmark=sr_star)


def _rankdata(a: np.ndarray) -> np.ndarray:
    """平均順位（1..n）。scipy.stats.rankdata の最小実装。"""
    order = a.argsort()
    ranks = np.empty(len(a), dtype=float)
    ranks[order] = np.arange(1, len(a) + 1, dtype=float)
    # 同順位は平均に丸める
    _, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    return sums[inv] / counts[inv]


def _sharpe_columns(block: np.ndarray) -> np.ndarray:
    """各列（戦略）の 1 観測あたり Sharpe を返す。"""
    mean = block.mean(axis=0)
    sd = block.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sr = np.where(sd > 0, mean / sd, 0.0)
    return np.nan_to_num(sr)


def pbo_cscv(
    returns_matrix: np.ndarray,
    n_splits: int = 10,
    metric: Callable[[np.ndarray], np.ndarray] | None = None,
) -> float:
    """PBO（過剰最適化確率, 0..1）を CSCV で推定する。

    returns_matrix: shape (T 観測, N 戦略/構成) のリターン行列。
    手法: T を S 個に分割し、S/2 を IS・残りを OOS とする全組合せで、IS 最良戦略の OOS 順位を
    見る。OOS で中央値を下回る（logit<0）割合が PBO。0.5 以上なら過剰最適化の疑いが濃い。
    """
    M = np.asarray(returns_matrix, dtype=float)
    if M.ndim != 2 or M.shape[1] < 2:
        return float("nan")
    metric = metric or _sharpe_columns
    T, N = M.shape
    S = n_splits - (n_splits % 2)              # 偶数化
    S = max(2, min(S, T))
    groups = [g for g in np.array_split(np.arange(T), S) if len(g) > 0]
    S = len(groups)
    if S < 2:
        return float("nan")

    logits: list[float] = []
    for train_sel in combinations(range(S), S // 2):
        train_rows = np.concatenate([groups[i] for i in train_sel])
        test_rows = np.concatenate([groups[i] for i in range(S) if i not in train_sel])
        is_perf = metric(M[train_rows])
        oos_perf = metric(M[test_rows])
        n_star = int(np.argmax(is_perf))
        oos_rank = _rankdata(oos_perf)[n_star]          # 1..N（大きいほど良い）
        omega = oos_rank / (N + 1)
        omega = min(max(omega, 1e-6), 1.0 - 1e-6)
        logits.append(float(np.log(omega / (1.0 - omega))))
    if not logits:
        return float("nan")
    return float(np.mean(np.array(logits) < 0.0))


def max_drawdown(returns: np.ndarray) -> float:
    """リターン系列の最大ドローダウン（<=0）。"""
    r = _clean(returns)
    if len(r) == 0:
        return 0.0
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    return float((equity / peak - 1.0).min())


def _stationary_bootstrap(r: np.ndarray, length: int, mean_block: int, rng: np.random.Generator) -> np.ndarray:
    """定常ブートストラップ（Politis-Romano）でリターンを再標本化する。"""
    T = len(r)
    out = np.empty(length)
    i = 0
    p = 1.0 / max(1, mean_block)
    while i < length:
        start = int(rng.integers(0, T))
        blen = int(rng.geometric(p))
        for j in range(blen):
            if i >= length:
                break
            out[i] = r[(start + j) % T]
            i += 1
    return out


@dataclass(frozen=True)
class MonteCarloResult:
    paths: int
    sharpe_median: float
    sharpe_p05: float          # 5 パーセンタイル（悲観側）
    sharpe_p95: float
    maxdd_median: float
    maxdd_p95: float           # 95 パーセンタイル DD（最悪側の目安, 絶対値）
    prob_profit: float         # 総リターン>0 の割合

    def to_dict(self) -> dict[str, float | int]:
        return self.__dict__.copy()


def monte_carlo_bootstrap(
    returns: np.ndarray, n_paths: int = 1000, block: int | None = None, seed: int = 0
) -> MonteCarloResult:
    """定常ブートストラップで Sharpe・最大DD・勝ち越し確率の分布を出す。"""
    r = _clean(returns)
    T = len(r)
    if T < 10:
        return MonteCarloResult(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    block = block or max(1, int(round(np.sqrt(T))))
    rng = np.random.default_rng(seed)
    sharpes = np.empty(n_paths)
    mdds = np.empty(n_paths)
    totals = np.empty(n_paths)
    for k in range(n_paths):
        path = _stationary_bootstrap(r, T, block, rng)
        sd = path.std(ddof=1)
        sharpes[k] = path.mean() / sd if sd > 0 else 0.0
        mdds[k] = max_drawdown(path)
        totals[k] = np.prod(1.0 + path) - 1.0
    return MonteCarloResult(
        paths=n_paths,
        sharpe_median=float(np.median(sharpes)),
        sharpe_p05=float(np.percentile(sharpes, 5)),
        sharpe_p95=float(np.percentile(sharpes, 95)),
        maxdd_median=float(abs(np.median(mdds))),
        maxdd_p95=float(abs(np.percentile(mdds, 5))),   # DD は負。悲観側 = 5%tile の絶対値
        prob_profit=float((totals > 0).mean()),
    )
