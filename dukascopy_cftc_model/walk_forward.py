"""ウォークフォワード・バックテスト(パイプライン7段目)。

fx_backtester/walk_forward.py の train/test/step/purge/embargo 思想を、Ridge回帰
+ シグナルの評価用に薄く実装する。各 fold で:

  1. train区間(過去)で標準化+Ridgeを学習。α は train内の時系列CVで選ぶ
     (リーク無し。将来のtestは一切見ない)。
  2. purge/embargo で train終端とtest始端の間にギャップを空ける。ラベルは
     horizon バー先を見るので、purge >= horizon にしないと train のラベルが
     test 区間に染み出す(リーク)。
  3. test区間で predict → signal(train予測のstdを基準にz閾値)。
  4. signal を「horizon 保有した実現リターン」に写像してトレード列を作る。

fold横断で out-of-sample のトレードを連結し、fx_backtester/metrics.py の
calculate_metrics に渡して 期待値・勝率・DD・PF・Sharpe を得る。特徴量寄与は
各foldのRidge係数を平均して集約する。

リターン→USD損益の換算は「リスク一定サイジング」を仮定する:
1トレードにつき初期資金 × risk_per_trade を賭け、実現log-returnぶんだけ
損益が出る(方向×リターン)。これで metrics の期待値/PF/勝率が意味を持つ。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from fx_backtester.metrics import calculate_metrics

from .config import PipelineConfig, WalkForwardConfig
from .ridge import RidgeRegressor
from .signal import predictions_to_signals, signal_scale


@dataclass
class FoldResult:
    """1 fold の学習・評価結果。"""

    fold: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    alpha: float
    n_train: int
    n_test: int
    n_trades: int
    coefficients: dict[str, float]


@dataclass
class WalkForwardResult:
    """全fold横断のバックテスト結果。"""

    metrics: dict[str, float | int]
    trades: pd.DataFrame
    equity_curve: pd.DataFrame
    folds: list[FoldResult] = field(default_factory=list)
    feature_importance: list[tuple[str, float]] = field(default_factory=list)
    fold_sharpes: list[float] = field(default_factory=list)


# ---------------------------------------------------------------- α選択(時系列CV)


def _time_series_cv_score(X: np.ndarray, y: np.ndarray, alpha: float, folds: int) -> float:
    """train内を時系列に folds 分割し、逐次的にMSEを評価(小さいほど良い)。

    各分割で「過去で学習→直後の区画を予測」する expanding window。未来では
    学習しない。分割数が確保できない場合は全体で1回だけ評価する。
    """
    n = len(X)
    if n < (folds + 1) * 10:
        # 分割に足りない → in-sample MSE(粗いが決定論的)
        model = RidgeRegressor(alpha=alpha).fit(X, y)
        return float(np.mean((model.predict(X) - y) ** 2))

    block = n // (folds + 1)
    scores: list[float] = []
    for k in range(1, folds + 1):
        train_end = block * k
        val_end = block * (k + 1)
        Xtr, ytr = X[:train_end], y[:train_end]
        Xval, yval = X[train_end:val_end], y[train_end:val_end]
        if len(Xval) == 0 or len(Xtr) < 10:
            continue
        model = RidgeRegressor(alpha=alpha).fit(Xtr, ytr)
        scores.append(float(np.mean((model.predict(Xval) - yval) ** 2)))
    return float(np.mean(scores)) if scores else float("inf")


def select_alpha(X: np.ndarray, y: np.ndarray, alpha_grid: list[float], folds: int) -> float:
    """時系列CVで最良(最小MSE)の α を選ぶ。"""
    best_alpha = alpha_grid[0]
    best_score = float("inf")
    for alpha in alpha_grid:
        score = _time_series_cv_score(X, y, alpha, folds)
        if score < best_score:
            best_score = score
            best_alpha = alpha
    return best_alpha


# ---------------------------------------------------------------- fold生成


def _generate_folds(n: int, cfg: WalkForwardConfig):
    """(train_slice, test_slice) を purge/embargo 込みで生成する。

    train: [t0, t0+train_bars)
    gap:   purge_bars(trainの後) + embargo_bars(testの前)
    test:  [train_end + gap, train_end + gap + test_bars)
    """
    step = cfg.effective_step()
    start = 0
    while True:
        train_start = start
        train_end = train_start + cfg.train_bars
        test_start = train_end + cfg.purge_bars + cfg.embargo_bars
        test_end = test_start + cfg.test_bars
        if test_end > n:
            break
        yield (
            slice(train_start, train_end),
            slice(test_start, test_end),
        )
        start += step


# ---------------------------------------------------------------- トレード構築


def _build_trades(
    signals: np.ndarray,
    future_returns: np.ndarray,
    timestamps: pd.DatetimeIndex,
    risk_amount: float,
) -> pd.DataFrame:
    """シグナル(+1/0/-1)と実現将来リターンからトレード列を作る。

    net_pnl = 方向 × 実現log-return × (risk_amount / 想定ボラ基準)。ここでは
    「1トレード=risk_amount 相当のエクスポージャで log-return ぶん損益」と単純化し、
    net_pnl = 方向 × log-return × risk_amount とする(rはlog-return/基準リスク)。
    r_multiple は方向×log-return を、そのfoldの平均絶対リターンで正規化した値。
    """
    mask = signals != 0
    if not mask.any():
        return pd.DataFrame(columns=["timestamp", "direction", "net_pnl", "r_multiple"])
    dirs = signals[mask]
    rets = future_returns[mask]
    ts = timestamps[mask]
    realized = dirs * rets  # 方向が合っていれば正
    net_pnl = realized * risk_amount
    denom = np.mean(np.abs(rets)) if np.mean(np.abs(rets)) > 0 else 1.0
    r_multiple = realized / denom
    return pd.DataFrame(
        {
            "timestamp": ts,
            "direction": dirs,
            "net_pnl": net_pnl,
            "r_multiple": r_multiple,
        }
    )


# ---------------------------------------------------------------- メイン


def run_walk_forward(
    X: pd.DataFrame,
    y: pd.Series,
    future_returns: pd.Series,
    cfg: PipelineConfig,
) -> WalkForwardResult:
    """特徴量・ラベル・実現将来リターンを受けて walk-forward を実行する。

    X, y, future_returns は同じ index(整列済み・finite)である前提。
    future_returns は「signalを建てた時点から horizon 先までの実現log-return」。
    """
    wf = cfg.walk_forward
    feature_names = list(X.columns)
    Xv = X.to_numpy(dtype=float)
    yv = y.to_numpy(dtype=float)
    fret = future_returns.to_numpy(dtype=float)
    index = X.index
    n = len(X)

    risk_amount = cfg.initial_cash * cfg.risk_per_trade

    all_trades: list[pd.DataFrame] = []
    folds: list[FoldResult] = []
    coef_accum: dict[str, list[float]] = {name: [] for name in feature_names}

    for fold_id, (train_sl, test_sl) in enumerate(_generate_folds(n, wf)):
        Xtr, ytr = Xv[train_sl], yv[train_sl]
        Xte = Xv[test_sl]
        if len(Xtr) < wf.min_train_samples or len(Xte) == 0:
            continue

        alpha = select_alpha(Xtr, ytr, wf.alpha_grid, wf.cv_folds)
        model = RidgeRegressor(alpha=alpha).fit(Xtr, ytr, feature_names=feature_names)

        train_pred = model.predict(Xtr)
        scale = signal_scale(train_pred)
        test_pred = model.predict(Xte)
        signals = predictions_to_signals(test_pred, scale, wf.signal_z_threshold)

        trades = _build_trades(
            signals,
            fret[test_sl],
            index[test_sl],
            risk_amount,
        )
        all_trades.append(trades)

        for name, c in model.coefficients().items():
            coef_accum[name].append(c)

        folds.append(
            FoldResult(
                fold=fold_id,
                train_start=index[train_sl][0],
                train_end=index[train_sl][-1],
                test_start=index[test_sl][0],
                test_end=index[test_sl][-1],
                alpha=alpha,
                n_train=len(Xtr),
                n_test=len(Xte),
                n_trades=int((signals != 0).sum()),
                coefficients=model.coefficients(),
            )
        )

    trades_df = (
        pd.concat(all_trades, ignore_index=True)
        if all_trades
        else pd.DataFrame(columns=["timestamp", "direction", "net_pnl", "r_multiple"])
    )
    equity_curve = _equity_curve_from_trades(trades_df, cfg.initial_cash)
    metrics = calculate_metrics(
        equity_curve, trades_df.drop(columns=["timestamp"], errors="ignore"), cfg.initial_cash
    )

    importance = _aggregate_importance(coef_accum)
    fold_sharpes = [_fold_sharpe(f, trades_df) for f in folds]

    return WalkForwardResult(
        metrics=metrics,
        trades=trades_df,
        equity_curve=equity_curve,
        folds=folds,
        feature_importance=importance,
        fold_sharpes=fold_sharpes,
    )


def _equity_curve_from_trades(trades: pd.DataFrame, initial_cash: float) -> pd.DataFrame:
    """トレードの net_pnl を時刻順に累積して equity曲線を作る。

    トレードが無ければ「初期資金のまま2点」の平坦曲線(metricsが空にならない
    ように)。annualization のため DatetimeIndex を持たせる。
    """
    if trades.empty:
        idx = pd.to_datetime(["2000-01-01", "2000-01-02"], utc=True)
        return pd.DataFrame({"equity": [initial_cash, initial_cash]}, index=idx)
    ordered = trades.sort_values("timestamp")
    equity = initial_cash + ordered["net_pnl"].cumsum()
    curve = pd.DataFrame(
        {"equity": equity.to_numpy()}, index=pd.DatetimeIndex(ordered["timestamp"])
    )
    curve.index.name = "timestamp"
    return curve


def _aggregate_importance(coef_accum: dict[str, list[float]]) -> list[tuple[str, float]]:
    """fold横断で係数を平均し、|平均係数| 降順に並べる(特徴量寄与の集約)。"""
    means: dict[str, float] = {}
    for name, values in coef_accum.items():
        if values:
            means[name] = float(np.mean(values))
    return sorted(means.items(), key=lambda kv: abs(kv[1]), reverse=True)


def _fold_sharpe(fold: FoldResult, trades: pd.DataFrame) -> float:
    """そのfoldのtest区間トレードだけのper-trade Sharpe(PBO/DSR用)。"""
    if trades.empty:
        return 0.0
    ts = pd.DatetimeIndex(trades["timestamp"])
    mask = (ts >= fold.test_start) & (ts <= fold.test_end)
    pnl = trades.loc[mask, "net_pnl"].to_numpy(dtype=float)
    if len(pnl) < 2 or pnl.std(ddof=1) == 0:
        return 0.0
    return float(pnl.mean() / pnl.std(ddof=1))
