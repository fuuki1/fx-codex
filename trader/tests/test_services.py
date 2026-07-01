from __future__ import annotations

import pandas as pd
import pytest


# ---- common.notify throttle ------------------------------------------------
def test_notify_throttle(fake_redis, monkeypatch):
    import common

    calls: list[int] = []
    monkeypatch.setattr(common.settings, "discord_webhook_url", "http://example/wh")
    monkeypatch.setattr(common.httpx, "post", lambda *a, **k: calls.append(1))

    common.notify("hello", key="k1")
    common.notify("hello", key="k1")  # 同一 key はスロットルで抑制
    assert len(calls) == 1

    common.notify("urgent", throttle=False)  # 抑制なし
    assert len(calls) == 2


def test_kill_switch_fail_safe(monkeypatch):
    import common

    class Boom:
        def get(self, *_a, **_k):
            raise RuntimeError("redis down")

    monkeypatch.setattr(common, "_redis", Boom())
    # Redis 不通時は「発注停止（ON）」とみなす fail-safe
    assert common.kill_switch_on() is True


# ---- executor pure helpers -------------------------------------------------
def test_client_order_id_deterministic():
    import executor

    a = executor.client_order_id("sha256:abc")
    b = executor.client_order_id("sha256:abc")
    assert a == b
    assert a.startswith("tx-")
    assert executor.client_order_id("sha256:xyz") != a


def test_classify_symbol():
    import executor

    assert executor.classify_symbol("USDJPY", "fx") == "fx"
    assert executor.classify_symbol("7203", "") == "jp_stock"
    assert executor.classify_symbol("AAPL", "us_stock") == "us_stock"


# ---- risk service（状態収集をスタブして純粋エンジンの結線を検証）-------------
def _sig(**over):
    base = {"idem": "i", "symbol": "USDJPY", "asset": "fx", "side": "BUY", "qty": 1000, "type": "MARKET"}
    base.update(over)
    return base


@pytest.fixture
def risk_stub(fake_redis, monkeypatch):
    """risk の DB/通知 I/O を安全な既定にスタブする。"""
    import common
    import risk

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(common, "notify", lambda *a, **k: None)
    monkeypatch.setattr(risk, "_day_pnl", lambda: 0.0)
    monkeypatch.setattr(risk, "_week_pnl", lambda: 0.0)
    monkeypatch.setattr(risk, "_recent_pnls", lambda limit: [])
    monkeypatch.setattr(risk, "_open_positions", lambda: [])
    monkeypatch.setattr(risk._calendar, "get", lambda: [])
    return fake_redis


def test_risk_qty_over_limit(risk_stub):
    import risk

    assert risk.evaluate(_sig(idem="i1", qty=99_999_999)) is False


def test_risk_kill_switch(risk_stub):
    import risk

    risk_stub.set("kill_switch", "1")
    assert risk.evaluate(_sig(idem="i2")) is False


def test_risk_daily_loss_auto_kill(risk_stub, monkeypatch):
    import common
    import risk

    killed: list[bool] = []
    monkeypatch.setattr(common, "set_kill_switch", lambda on, **k: killed.append(on))
    monkeypatch.setattr(risk, "_day_pnl", lambda: -1_000_000.0)
    assert risk.evaluate(_sig(idem="i4")) is False
    assert killed == [True]


def test_risk_weekly_loss_auto_kill(risk_stub, monkeypatch):
    import risk

    monkeypatch.setattr(risk.settings, "max_weekly_loss_jpy", 150_000.0)
    monkeypatch.setattr(risk, "_week_pnl", lambda: -200_000.0)
    assert risk.evaluate(_sig(idem="wk")) is False


def test_risk_loss_streak_halts(risk_stub, monkeypatch):
    import risk

    monkeypatch.setattr(risk, "_recent_pnls", lambda limit: [-1.0] * 5)
    assert risk.evaluate(_sig(idem="ls")) is False


def test_risk_blackout_blocks(risk_stub, monkeypatch):
    import time

    import risk

    now = time.time()
    monkeypatch.setattr(risk._calendar, "get", lambda: [(now - 60, now + 60, "US CPI")])
    assert risk.evaluate(_sig(idem="bo")) is False


def test_risk_max_concurrent_positions(risk_stub, monkeypatch):
    import risk

    monkeypatch.setattr(
        risk, "_open_positions",
        lambda: [("EURUSD", 1000.0), ("GBPUSD", 1000.0), ("AUDUSD", 1000.0)],
    )
    assert risk.evaluate(_sig(idem="mp", symbol="USDJPY")) is False


def test_risk_approve(risk_stub):
    import risk

    assert risk.evaluate(_sig(idem="i3")) is True


def test_risk_handle_publishes_sized_order(risk_stub, monkeypatch):
    import json

    import risk

    # サイジングを有効化: ストップ距離 0.5・残高100万・1% → 数量2万・想定リスク1万
    monkeypatch.setattr(risk.settings, "risk_sizing_enabled", True)
    monkeypatch.setattr(risk.settings, "account_equity", 1_000_000.0)
    monkeypatch.setattr(risk.settings, "risk_per_trade_pct", 1.0)
    monkeypatch.setattr(risk.settings, "lot_step", 1000.0)
    monkeypatch.setattr(risk.settings, "min_lot", 1000.0)
    monkeypatch.setattr(risk.settings, "max_position_qty", 100_000.0)

    risk.handle(_sig(idem="sz", stop_distance=0.5))
    assert risk_stub.xlen("orders") == 1
    _id, fields = risk_stub.xrange("orders")[0]
    data = json.loads(fields["data"])
    assert data["qty"] == 20000
    assert data["intended_risk"] == pytest.approx(10000.0)


def test_equity_drawdown_tracks_all_time_hwm(fake_redis, monkeypatch):
    import risk

    monkeypatch.setattr(risk.settings, "max_drawdown_pct", 10.0)

    # 初回: cum=100k -> HWM=100k を必ず永続化し、DD=0（旧実装は初期化されず常に 0 だった）
    monkeypatch.setattr(risk, "_cumulative_pnl", lambda: 100_000.0)
    assert risk._equity_drawdown() == 0.0
    assert float(fake_redis.get(risk.KEY_PNL_HWM)) == 100_000.0

    # 累計が 60k へ低下 -> ピーク 100k からの DD=40k（損失で下がった分を検知）
    monkeypatch.setattr(risk, "_cumulative_pnl", lambda: 60_000.0)
    assert risk._equity_drawdown() == 40_000.0

    # 新高値 120k -> HWM 更新、DD=0
    monkeypatch.setattr(risk, "_cumulative_pnl", lambda: 120_000.0)
    assert risk._equity_drawdown() == 0.0
    assert float(fake_redis.get(risk.KEY_PNL_HWM)) == 120_000.0

    # 無効化なら常に 0（DB/Redis に触れない）
    monkeypatch.setattr(risk.settings, "max_drawdown_pct", 0.0)
    monkeypatch.setattr(risk, "_cumulative_pnl", lambda: (_ for _ in ()).throw(AssertionError("queried")))
    assert risk._equity_drawdown() == 0.0


# ---- strategy signal -------------------------------------------------------
def test_ma_cross_signal_directions():
    import strategy

    params = {"fast_window": 5, "slow_window": 20, "atr_window": 14, "atr_multiple": 2.0}
    up = pd.DataFrame({"close": list(range(1, 101))})
    assert strategy.ma_cross_signal(up, params)["target"] == 1
    down = pd.DataFrame({"close": list(range(100, 0, -1))})
    assert strategy.ma_cross_signal(down, params)["target"] == -1
    assert strategy.ma_cross_signal(pd.DataFrame({"close": [1, 2, 3]}), params) is None


def test_strategy_emit_only_on_change(fake_redis, monkeypatch):
    import common
    import strategy

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    # 初回 flat->long は発行
    strategy.emit_if_changed("USDJPY", "fx", 1, 0.01)
    assert fake_redis.hget("strategy:state", "USDJPY") == "1"
    assert fake_redis.xlen("signals") == 1
    # 同じ状態なら発行しない
    strategy.emit_if_changed("USDJPY", "fx", 1, 0.01)
    assert fake_redis.xlen("signals") == 1


# ---- optimizer score -------------------------------------------------------
def test_optimizer_score():
    import auto_optimize

    s = auto_optimize.score({"sharpe_ratio": 1.0, "profit_factor": 2.0, "max_drawdown_pct": 10})
    assert abs(s - 1.18) < 1e-9
    # 高 Sharpe・高 PF・低 DD のほうが高スコアになる
    better = auto_optimize.score({"sharpe_ratio": 2.0, "profit_factor": 3.0, "max_drawdown_pct": 5})
    assert better > s


def test_optimizer_deploy_gate():
    import auto_optimize

    # 検証クリーンなら配備可
    assert auto_optimize.should_deploy({"overfit_warning": False, "insufficient_trades": False}) is True
    # 過剰最適化 / 取引数不足は配備しない
    assert auto_optimize.should_deploy({"overfit_warning": True, "insufficient_trades": False}) is False
    assert auto_optimize.should_deploy({"overfit_warning": False, "insufficient_trades": True}) is False
    # FXBT_FORCE_DEPLOY 相当の強制
    assert auto_optimize.should_deploy({"overfit_warning": True}, force=True) is True
