"""予測リターン → 売買シグナルへの変換(パイプライン6.5段目)。

Ridge は連続値(将来log-return予測)を出す。これを閾値でロング/ショート/
様子見の離散シグナルに落とす。

閾値は「予測の標準偏差」を単位にする(z-score)。train区間の予測の std を
基準にし、|予測|/std > z_threshold のときだけ建玉する。こうすると:

- 予測の絶対水準(モデルのスケール)に依らず、確信度の相対的な大きさで建てる。
- z_threshold を上げるほど「よほど強い予測」だけに絞られ、取引数は減るが
  1トレードの質は上がる(期待値最大化のトレードオフ)。

signal ∈ {+1(ロング), 0(様子見), -1(ショート)}。
"""

from __future__ import annotations

import numpy as np


def predictions_to_signals(
    predictions: np.ndarray,
    scale: float,
    z_threshold: float,
) -> np.ndarray:
    """予測値配列 → シグナル配列(+1/0/-1)。

    scale は基準となる標準偏差(通常 train 予測の std)。scale<=0 のときは
    全て様子見(0)にする(判断できるほどのばらつきが無い)。
    """
    predictions = np.asarray(predictions, dtype=float)
    signals = np.zeros_like(predictions, dtype=int)
    if scale <= 0 or not np.isfinite(scale):
        return signals
    z = predictions / scale
    signals[z > z_threshold] = 1
    signals[z < -z_threshold] = -1
    return signals


def signal_scale(train_predictions: np.ndarray) -> float:
    """train予測から閾値の基準スケール(標準偏差)を求める。"""
    train_predictions = np.asarray(train_predictions, dtype=float)
    if train_predictions.size < 2:
        return 0.0
    std = float(np.std(train_predictions, ddof=1))
    return std
