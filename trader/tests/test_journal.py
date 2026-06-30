"""journal（成績分析）の純粋ロジック・テスト。"""
from __future__ import annotations

import journal
import pytest


def test_compute_journal_empty():
    j = journal.compute_journal([])
    assert j["num_trades"] == 0
    assert j["expectancy"] == 0.0
    assert j["profit_factor"] == 0.0


def test_compute_journal_excludes_unrealized():
    # realized_pnl == 0 は未確定として除外
    j = journal.compute_journal([{"realized_pnl": 0, "realized_r": None}])
    assert j["num_trades"] == 0


def test_compute_journal_basic_metrics():
    # 新しい順: 勝ち100(2R) / 負け-50(-1R) / 勝ち100(2R)
    trades = [
        {"realized_pnl": 100, "realized_r": 2.0},
        {"realized_pnl": -50, "realized_r": -1.0},
        {"realized_pnl": 100, "realized_r": 2.0},
    ]
    j = journal.compute_journal(trades)
    assert j["num_trades"] == 3
    assert j["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert j["expectancy"] == pytest.approx(50.0)          # (100-50+100)/3
    assert j["expectancy_r"] == pytest.approx(1.0)         # (2-1+2)/3
    assert j["profit_factor"] == pytest.approx(200 / 50)   # 4.0
    assert j["payoff_ratio"] == pytest.approx(100 / 50)    # 平均利益/平均損失
    assert j["total_pnl"] == 150.0


def test_compute_journal_current_loss_streak():
    # 新しい順に 2 連敗 → current_loss_streak=2
    trades = [
        {"realized_pnl": -10, "realized_r": -1.0},
        {"realized_pnl": -20, "realized_r": -2.0},
        {"realized_pnl": 50, "realized_r": 1.0},
    ]
    j = journal.compute_journal(trades)
    assert j["current_loss_streak"] == 2


def test_compute_journal_max_loss_streak():
    # どこかに 3 連敗が含まれる（新しい順で渡す）
    trades = [
        {"realized_pnl": 10, "realized_r": None},
        {"realized_pnl": -1, "realized_r": None},
        {"realized_pnl": -1, "realized_r": None},
        {"realized_pnl": -1, "realized_r": None},
        {"realized_pnl": 10, "realized_r": None},
    ]
    j = journal.compute_journal(trades)
    assert j["max_loss_streak"] == 3
    assert j["current_loss_streak"] == 0      # 直近は勝ち
    assert j["expectancy_r"] == 0.0            # R サンプル無し


def test_compute_journal_profit_factor_no_losses():
    j = journal.compute_journal([{"realized_pnl": 100, "realized_r": None}])
    assert j["profit_factor"] == 999.0        # 損失ゼロは上限でクリップ


def test_format_summary_no_trades():
    s = journal.format_summary(journal.compute_journal([]), 30)
    assert "確定トレードなし" in s
