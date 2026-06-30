"""risk_engine（プロ級リスクエンジン）の純粋ロジック・テスト。

Redis/DB 無しで、サイジング・連敗スロットル・損失上限・相関/同時保有・ブラックアウトの
各判断を直接検証する。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import risk_engine as re
from risk_engine import RiskParams, RiskState


# ---- 純粋ヘルパー ----------------------------------------------------------
def test_decompose_pair():
    assert re.decompose_pair("USDJPY") == ("USD", "JPY")
    assert re.decompose_pair("eurusd") == ("EUR", "USD")
    assert re.decompose_pair("7203") is None       # 日本株
    assert re.decompose_pair("AAPL") is None        # 4 文字
    assert re.decompose_pair("USDJP1") is None       # 数字混在


def test_net_currency_exposure():
    # USDJPY ロング + EURUSD ロング → USD は +1000-2000=-1000 で打ち消し合う
    exp = re.net_currency_exposure([("USDJPY", 1000), ("EURUSD", 2000)])
    assert exp["USD"] == 1000 - 2000
    assert exp["JPY"] == -1000
    assert exp["EUR"] == 2000


def test_loss_streak():
    assert re.loss_streak([]) == 0
    assert re.loss_streak([100, -50]) == 0           # 直近が勝ち → 連敗 0
    assert re.loss_streak([-10, -20, 50]) == 2       # 新しい順に 2 連敗で勝ちに当たる
    assert re.loss_streak([-1, -1, -1]) == 3
    assert re.loss_streak([-1, 0, -1]) == 2          # 引き分けは無視


def test_streak_size_factor():
    assert re.streak_size_factor(0, 3, 0.5) == 1.0
    assert re.streak_size_factor(2, 3, 0.5) == 1.0
    assert re.streak_size_factor(3, 3, 0.5) == 0.5
    assert re.streak_size_factor(4, 3, 0.5) == 0.5


def test_in_blackout():
    windows = [(100.0, 200.0, "CPI")]
    assert re.in_blackout(150.0, windows) == "CPI"
    assert re.in_blackout(100.0, windows) == "CPI"    # 端点は窓内
    assert re.in_blackout(250.0, windows) is None
    assert re.in_blackout(150.0, []) is None


def test_floor_to_step():
    assert re.floor_to_step(20500, 1000) == 20000
    assert re.floor_to_step(999, 1000) == 0
    assert re.floor_to_step(123.4, 0) == 123.4        # step<=0 はそのまま


def test_position_size_math():
    # 残高100万 × 1% = 1万。ストップ0.5・単価1.0 → 1万/0.5 = 2万 → ロット刻み2万
    q = re.position_size(
        equity=1_000_000, risk_pct=1.0, stop_distance=0.5,
        value_per_point=1.0, lot_step=1000, factor=1.0,
    )
    assert q == 20000
    # 連敗縮小 0.5 倍 → 1万
    q2 = re.position_size(
        equity=1_000_000, risk_pct=1.0, stop_distance=0.5,
        value_per_point=1.0, lot_step=1000, factor=0.5,
    )
    assert q2 == 10000
    # 不正入力は 0
    assert re.position_size(
        equity=0, risk_pct=1.0, stop_distance=0.5, value_per_point=1, lot_step=1000
    ) == 0.0


# ---- evaluate シナリオ ------------------------------------------------------
def _sig(**over):
    base = {"idem": "i", "symbol": "USDJPY", "asset": "fx", "side": "BUY", "qty": 1000, "type": "MARKET"}
    base.update(over)
    return base


def _params(**over):
    base = dict(
        sizing_enabled=False, account_equity=1_000_000, risk_per_trade_pct=1.0,
        require_stop_for_sizing=False, lot_step=1000, min_lot=1000,
        max_position_qty=100_000, max_daily_loss=50_000, max_weekly_loss=0.0,
        loss_streak_reduce_at=3, loss_streak_reduce_factor=0.5, loss_streak_halt_at=5,
        max_concurrent_positions=3, max_currency_exposure=0.0, enforce_session=False,
    )
    base.update(over)
    return RiskParams(**base)


def _state(**over):
    base = dict(now=None, day_pnl=0.0, week_pnl=0.0, recent_pnls=[], open_positions=[],
                blackout_windows=[], value_per_point=1.0)
    base.update(over)
    return RiskState(**base)


def test_evaluate_basic_approve_sizing_off():
    d = re.evaluate(_sig(), _state(), _params())
    assert d.approved is True
    assert d.sized_qty == 1000          # サイジング OFF はシグナル qty のまま


def test_evaluate_blackout():
    now = 1000.0
    d = re.evaluate(_sig(), _state(now=now, blackout_windows=[(900.0, 1100.0, "FOMC")]), _params())
    assert d.approved is False
    assert d.reason == re.R_BLACKOUT
    assert d.details["label"] == "FOMC"


def test_evaluate_session_rejects_weekend():
    sat = datetime(2024, 1, 6, 12, tzinfo=UTC)  # 土曜は FX クローズ
    d = re.evaluate(_sig(), _state(now=sat), _params(enforce_session=True))
    assert d.approved is False
    assert d.reason == re.R_SESSION


def test_evaluate_daily_loss_trips_kill():
    d = re.evaluate(_sig(), _state(day_pnl=-60_000), _params(max_daily_loss=50_000))
    assert d.approved is False
    assert d.reason == re.R_DAILY_LOSS
    assert d.trip_kill_switch is True


def test_evaluate_weekly_loss_trips_kill():
    d = re.evaluate(_sig(), _state(week_pnl=-200_000), _params(max_weekly_loss=150_000))
    assert d.approved is False
    assert d.reason == re.R_WEEKLY_LOSS
    assert d.trip_kill_switch is True


def test_evaluate_loss_streak_halt():
    d = re.evaluate(_sig(), _state(recent_pnls=[-1, -1, -1, -1, -1]), _params(loss_streak_halt_at=5))
    assert d.approved is False
    assert d.reason == re.R_LOSS_STREAK
    assert d.trip_kill_switch is True


def test_evaluate_loss_streak_reduces_size():
    # 3 連敗（停止には届かない）→ サイズ半減（サイジング OFF でも縮小は効く）
    d = re.evaluate(_sig(qty=1000), _state(recent_pnls=[-1, -1, -1]), _params())
    assert d.approved is True
    assert d.sized_qty == 500


def test_evaluate_sizing_with_stop():
    d = re.evaluate(
        _sig(stop_distance=0.5), _state(value_per_point=1.0),
        _params(sizing_enabled=True, risk_per_trade_pct=1.0),
    )
    assert d.approved is True
    assert d.sized_qty == 20000              # 100万×1% / 0.5
    assert d.intended_risk == pytest.approx(10000.0)  # 2万 × 0.5 × 1.0


def test_evaluate_sizing_stop_too_wide():
    # ストップが広すぎて、リスク予算では最小ロットに満たない → 却下
    d = re.evaluate(
        _sig(stop_distance=20.0), _state(),
        _params(sizing_enabled=True, risk_per_trade_pct=1.0, min_lot=1000, lot_step=1000),
    )
    assert d.approved is False
    assert d.reason == re.R_STOP_TOO_WIDE


def test_evaluate_sizing_requires_stop():
    d = re.evaluate(_sig(), _state(), _params(sizing_enabled=True, require_stop_for_sizing=True))
    assert d.approved is False
    assert d.reason == re.R_NO_STOP


def test_evaluate_sizing_no_stop_fallback_caps():
    d = re.evaluate(
        _sig(qty=5000), _state(),
        _params(sizing_enabled=True, require_stop_for_sizing=False, max_position_qty=100_000),
    )
    assert d.approved is True
    assert d.sized_qty == 5000               # ストップ無し → qty を上限内で使う
    assert d.intended_risk == 0.0


def test_evaluate_qty_over_limit_sizing_off():
    d = re.evaluate(_sig(qty=999_999), _state(), _params(max_position_qty=10_000))
    assert d.approved is False
    assert d.reason == re.R_QTY_LIMIT


def test_evaluate_max_concurrent_positions():
    open_pos = [("EURUSD", 1000), ("GBPUSD", 1000), ("AUDUSD", 1000)]
    # 4 つ目の別銘柄は却下
    d = re.evaluate(_sig(symbol="USDJPY"), _state(open_positions=open_pos),
                    _params(max_concurrent_positions=3))
    assert d.approved is False
    assert d.reason == re.R_MAX_POSITIONS
    # 既存銘柄への発注（積み増し/手仕舞い）はカウント外 → 通る
    d2 = re.evaluate(_sig(symbol="EURUSD"), _state(open_positions=open_pos),
                     _params(max_concurrent_positions=3))
    assert d2.approved is True


def test_evaluate_currency_exposure_blocks_stacking():
    # 既に USD を 1000 ロング。さらに USDJPY を 1500 買うと USD が 2500 で上限 2000 超 → 却下
    open_pos = [("USDJPY", 1000)]
    d = re.evaluate(_sig(symbol="USDJPY", side="BUY", qty=1500),
                    _state(open_positions=open_pos), _params(max_currency_exposure=2000))
    assert d.approved is False
    assert d.reason == re.R_CURRENCY_EXPOSURE
    assert d.details["currency"] == "USD"


def test_evaluate_currency_exposure_allows_reducing():
    # 既に USD を上限超で持っていても、減らす方向（売り）は許す
    open_pos = [("USDJPY", 5000)]
    d = re.evaluate(_sig(symbol="USDJPY", side="SELL", qty=1000),
                    _state(open_positions=open_pos), _params(max_currency_exposure=2000))
    assert d.approved is True


def test_evaluate_order_of_checks_blackout_before_session():
    # ブラックアウトは最優先（セッション外でも理由は blackout）
    sat = datetime(2024, 1, 6, 12, tzinfo=UTC)
    win = [(sat.timestamp() - 10, sat.timestamp() + 10, "CPI")]
    d = re.evaluate(_sig(), _state(now=sat, blackout_windows=win), _params(enforce_session=True))
    assert d.reason == re.R_BLACKOUT
