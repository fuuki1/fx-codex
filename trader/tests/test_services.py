from __future__ import annotations

import json

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


def test_stop_order_side_is_opposite():
    import executor

    assert executor.stop_order_side("BUY") == "SELL"
    assert executor.stop_order_side("SELL") == "BUY"


def test_compute_stop_price_below_for_buy_above_for_sell():
    import executor

    assert executor.compute_stop_price("BUY", 150.0, 0.5) == 149.5
    assert executor.compute_stop_price("SELL", 150.0, 0.5) == 150.5


def test_compute_stop_price_rejects_non_positive_inputs():
    import executor

    with pytest.raises(ValueError):
        executor.compute_stop_price("BUY", 0, 0.5)
    with pytest.raises(ValueError):
        executor.compute_stop_price("BUY", 150.0, 0)


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


def test_strategy_emit_flip_doubles_order_qty(fake_redis, monkeypatch):
    """反転(1->-1)は現在の建玉を閉じて反対方向を建てるため 2 倍量を発注する。"""
    import common
    import strategy

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    strategy.emit_if_changed("USDJPY", "fx", 1, 0.01)  # flat -> long
    strategy.emit_if_changed("USDJPY", "fx", -1, 0.02)  # long -> short (flip)

    msgs = fake_redis.xrange("signals")
    assert len(msgs) == 2
    first = json.loads(msgs[0][1]["data"])
    second = json.loads(msgs[1][1]["data"])
    assert first["qty"] == strategy.settings.strategy_qty
    assert first["position_qty"] == strategy.settings.strategy_qty
    assert second["qty"] == 2 * strategy.settings.strategy_qty
    assert second["position_qty"] == strategy.settings.strategy_qty
    assert second["side"] == "SELL"


# ---- optimizer score -------------------------------------------------------
def test_optimizer_score():
    import auto_optimize

    s = auto_optimize.score({"sharpe_ratio": 1.0, "profit_factor": 2.0, "max_drawdown_pct": 10})
    assert abs(s - 1.18) < 1e-9
    # 高 Sharpe・高 PF・低 DD のほうが高スコアになる
    better = auto_optimize.score({"sharpe_ratio": 2.0, "profit_factor": 3.0, "max_drawdown_pct": 5})
    assert better > s


# ---- auto_optimize: データ選択と過学習ゲート ------------------------------
def _patch_optimize_paths(auto_optimize, tmp_path, monkeypatch):
    monkeypatch.setattr(auto_optimize, "FXCODEX_DIR", tmp_path)
    monkeypatch.setattr(auto_optimize, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auto_optimize, "PARAMS_FILE", tmp_path / "strategy_params.json")
    monkeypatch.setattr(auto_optimize, "RESULT_LOG", tmp_path / "optimize_result.log")


def _fake_backtest_result(**validation_over):
    validation = {
        "overfit_warning": False,
        "insufficient_trades": False,
        "oos_sharpe_mean": 1.0,
        "oos_is_ratio": 0.8,
        "param_stability": 1.0,
        "oos_total_trades": 50,
    }
    validation.update(validation_over)
    payload = {
        "fast_window": 10, "slow_window": 40, "atr_window": 14, "stop_atr_multiple": 2.0,
        "atr_multiple": 2.0, "_validation": validation,
    }
    return json.dumps(payload)


def test_optimize_skips_deploy_when_validation_fails(tmp_path, monkeypatch):
    import subprocess as sp
    from types import SimpleNamespace

    import auto_optimize

    _patch_optimize_paths(auto_optimize, tmp_path, monkeypatch)
    previous = json.dumps({"fast_window": 20, "slow_window": 60})
    auto_optimize.PARAMS_FILE.write_text(previous)

    def fake_run(cmd, **kwargs):
        if "export_history.py" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="no ib gateway")
        return SimpleNamespace(returncode=0, stdout=_fake_backtest_result(overfit_warning=True), stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    result = auto_optimize.optimize()

    assert result["deployed"] is False
    # 検証に落ちたので既存ファイルは変更されない
    assert auto_optimize.PARAMS_FILE.read_text() == previous


def test_optimize_deploys_when_validation_passes(tmp_path, monkeypatch):
    import subprocess as sp
    from types import SimpleNamespace

    import auto_optimize

    _patch_optimize_paths(auto_optimize, tmp_path, monkeypatch)

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "export_history.py" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="no ib gateway")
        return SimpleNamespace(returncode=0, stdout=_fake_backtest_result(), stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    result = auto_optimize.optimize()

    assert result["deployed"] is True
    written = json.loads(auto_optimize.PARAMS_FILE.read_text())
    assert written["fast_window"] == 10
    # ヒストリカルデータ取得に失敗 -> 同梱サンプルへフォールバックしている
    backtest_cmd = next(c for c in calls if "export_history.py" not in c)
    assert "/fx-codex/examples/sample_prices.csv" in backtest_cmd
    assert "/fx-codex/examples/sample_events.csv" in backtest_cmd


def test_optimize_prefers_real_history_when_export_succeeds(tmp_path, monkeypatch):
    import subprocess as sp
    from types import SimpleNamespace

    import auto_optimize

    _patch_optimize_paths(auto_optimize, tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        if "export_history.py" in cmd:
            return SimpleNamespace(returncode=0, stdout="wrote 1000 bars", stderr="")
        return SimpleNamespace(returncode=0, stdout=_fake_backtest_result(), stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    result = auto_optimize.optimize()

    assert result["deployed"] is True
