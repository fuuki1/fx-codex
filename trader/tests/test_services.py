from __future__ import annotations

import pandas as pd


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


# ---- risk.evaluate ---------------------------------------------------------
def _sig(**over):
    base = {"idem": "i", "symbol": "USDJPY", "asset": "fx", "side": "BUY", "qty": 1000, "type": "MARKET"}
    base.update(over)
    return base


def test_risk_qty_over_limit(fake_redis, monkeypatch):
    import common
    import risk

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    assert risk.evaluate(_sig(idem="i1", qty=99_999_999)) is False


def test_risk_kill_switch(fake_redis, monkeypatch):
    import common
    import risk

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    fake_redis.set("kill_switch", "1")
    assert risk.evaluate(_sig(idem="i2")) is False


def test_risk_daily_loss_auto_kill(fake_redis, monkeypatch):
    import common
    import risk

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(common, "notify", lambda *a, **k: None)
    killed: list[bool] = []
    monkeypatch.setattr(common, "set_kill_switch", lambda on, **k: killed.append(on))
    monkeypatch.setattr(risk, "_today_realized_pnl", lambda: -1_000_000.0)
    assert risk.evaluate(_sig(idem="i4")) is False
    assert killed == [True]


def test_risk_approve(fake_redis, monkeypatch):
    import common
    import risk

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(risk, "_today_realized_pnl", lambda: 0.0)
    assert risk.evaluate(_sig(idem="i3")) is True


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
