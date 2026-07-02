"""バックテストエンジン（バー単位ループ・先読みなし）。

設計上の保証:
- **1 バー約定遅延**: バー i-1 の終値で出した判断は、バー i の終値で執行する
  （= 判断に使った情報より後の価格で約定）。未来を参照しない。
- **ATR ストップ**: 保有中はバーの高値/安値でストップ到達を判定し、ギャップ時は
  寄り(open)で約定（不利側）。
- **コスト**: ポジション変更（エントリ/エグジット）ごとにスプレッド+スリッページを課す。
- **イベント no-trade 窓**: blocked のバーは新規/保有を許さずフラット。

ベクトル化より「監査しやすさ」を優先し、明示ループで実装している。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import CostModel


@dataclass(frozen=True)
class Trade:
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    direction: int          # +1 / -1
    entry_price: float
    exit_price: float
    return_pct: float       # コスト控除後の 1 トレード損益率
    reason: str             # "signal" | "stop" | "eod"


@dataclass
class BacktestResult:
    bar_returns: pd.Series
    equity: pd.Series
    trades: list[Trade] = field(default_factory=list)


def run(
    df: pd.DataFrame,
    signal: pd.DataFrame,
    cost: CostModel,
    blocked: pd.Series | None = None,
) -> BacktestResult:
    idx = df.index
    open_ = df["open"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    target = signal["target_position"].to_numpy(int)
    stopd = signal["stop_distance"].to_numpy(float)
    blk = (
        blocked.reindex(idx).fillna(False).to_numpy(bool)
        if blocked is not None
        else np.zeros(len(idx), bool)
    )
    cost_price = cost.cost_price()

    n = len(df)
    bar_ret = np.zeros(n)
    trades: list[Trade] = []
    pos = 0
    entry_price = 0.0
    entry_i = -1
    stop_price = 0.0

    def _close_trade(exit_i: int, exit_price: float, reason: str) -> None:
        nonlocal pos, entry_i
        gross = pos * (exit_price / entry_price - 1.0)
        ret = gross - 2.0 * cost_price / entry_price  # entry + exit コスト
        trades.append(
            Trade(idx[entry_i], idx[exit_i], pos, entry_price, exit_price, ret, reason)
        )
        pos = 0
        entry_i = -1

    for i in range(1, n):
        prev_close = close[i - 1]

        # 1) 保有ポジションを当バーへ反映（ストップ優先）
        if pos != 0:
            stopped = False
            fill = 0.0
            if pos == 1 and low[i] <= stop_price:
                fill = open_[i] if open_[i] <= stop_price else stop_price
                stopped = True
            elif pos == -1 and high[i] >= stop_price:
                fill = open_[i] if open_[i] >= stop_price else stop_price
                stopped = True

            if stopped:
                bar_ret[i] += pos * (fill / prev_close - 1.0)
                bar_ret[i] -= cost_price / prev_close          # エグジットコスト
                _close_trade(i, fill, "stop")
            else:
                bar_ret[i] += pos * (close[i] / prev_close - 1.0)

        # 2) バー i-1 の判断を当バー終値で執行
        desired = int(target[i - 1])
        if blk[i]:
            desired = 0
        if desired != pos:
            if pos != 0:
                bar_ret[i] -= cost_price / close[i]            # 既存をクローズ
                _close_trade(i, close[i], "signal")
            if desired != 0:
                bar_ret[i] -= cost_price / close[i]            # 新規エントリ
                pos = desired
                entry_price = close[i]
                entry_i = i
                stop_price = entry_price - desired * stopd[i]

    # 期末に建玉が残っていれば終値でクローズ（トレード集計の完結用）
    if pos != 0:
        _close_trade(n - 1, close[-1], "eod")

    bar_returns = pd.Series(bar_ret, index=idx, name="return")
    equity = (1.0 + bar_returns).cumprod()
    equity.name = "equity"
    return BacktestResult(bar_returns=bar_returns, equity=equity, trades=trades)
