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
    base = {
        "idem": "i",
        "symbol": "USDJPY",
        "asset": "fx",
        "side": "BUY",
        "qty": 1000,
        "type": "MARKET",
        "price": 150.0,
        "stop_distance": 0.5,
    }
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
    monkeypatch.setattr(risk, "_net_position", lambda symbol: 0.0)
    monkeypatch.setattr(risk, "_today_realized_pnl", lambda: -1_000_000.0)
    assert risk.evaluate(_sig(idem="i4")) is False
    assert killed == [True]


def test_risk_approve(fake_redis, monkeypatch):
    import common
    import risk

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(risk, "_net_position", lambda symbol: 0.0)
    monkeypatch.setattr(risk, "_today_realized_pnl", lambda: 0.0)
    assert risk.evaluate(_sig(idem="i3")) is True


def test_risk_stop_loss_required(fake_redis, monkeypatch):
    """REQUIRE_STOP_LOSS（既定 ON）: ストップ情報の無い新規建てシグナルは却下。"""
    import common
    import risk

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    assert risk.evaluate(_sig(idem="i5", stop_distance=None, stop_price=None)) is False


def test_risk_stop_price_accepted(fake_redis, monkeypatch):
    import common
    import risk

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(risk, "_net_position", lambda symbol: 0.0)
    monkeypatch.setattr(risk, "_today_realized_pnl", lambda: 0.0)
    assert risk.evaluate(_sig(idem="i6", stop_distance=None, stop_price=148.0)) is True


def test_risk_close_exempt_from_stop(fake_redis, monkeypatch):
    """決済シグナル（close=true）はストップ不要で通る。"""
    import common
    import risk

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(risk, "_net_position", lambda symbol: 0.0)
    monkeypatch.setattr(risk, "_today_realized_pnl", lambda: 0.0)
    assert risk.evaluate(_sig(idem="i7", stop_distance=None, stop_price=None, close=True)) is True


# ---- 銘柄許可リスト / 純建玉上限 --------------------------------------------
def _patch_risk_io(monkeypatch, *, net: float = 0.0, pnl: float = 0.0):
    import common
    import risk

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(common, "notify", lambda *a, **k: None)
    monkeypatch.setattr(risk, "_net_position", lambda symbol: net)
    monkeypatch.setattr(risk, "_today_realized_pnl", lambda: pnl)
    return risk


def test_risk_symbol_not_in_allowlist_rejected(fake_redis, monkeypatch):
    risk = _patch_risk_io(monkeypatch)
    monkeypatch.setattr(risk.settings, "symbol_allowlist", ["USDJPY"])
    assert risk.evaluate(_sig(idem="a1", symbol="EURUSD")) is False
    assert risk.evaluate(_sig(idem="a2", symbol="USDJPY")) is True


def test_risk_empty_allowlist_allows_all(fake_redis, monkeypatch):
    risk = _patch_risk_io(monkeypatch)
    monkeypatch.setattr(risk.settings, "symbol_allowlist", [])
    assert risk.evaluate(_sig(idem="a3", symbol="EURUSD")) is True


def test_risk_net_position_blocks_accumulation(fake_redis, monkeypatch):
    """既に上限近くまで買い持ち → さらに BUY は却下（積み上がり防止）。"""
    risk = _patch_risk_io(monkeypatch, net=9_500.0)
    assert risk.evaluate(_sig(idem="n1", side="BUY", qty=1000)) is False


def test_risk_net_position_allows_reducing(fake_redis, monkeypatch):
    """上限超の建玉でも、減らす方向（決済）は通す。"""
    risk = _patch_risk_io(monkeypatch, net=12_000.0)
    assert risk.evaluate(_sig(idem="n2", side="SELL", qty=1000)) is True


def test_risk_net_position_short_side(fake_redis, monkeypatch):
    """売り持ちの積み上がりも同様に制限する。"""
    risk = _patch_risk_io(monkeypatch, net=-9_500.0)
    assert risk.evaluate(_sig(idem="n3", side="SELL", qty=1000)) is False
    assert risk.evaluate(_sig(idem="n4", side="BUY", qty=1000)) is True


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
    strategy.emit_if_changed("USDJPY", "fx", 1, 0.01, 150.0)
    assert fake_redis.hget("strategy:state", "USDJPY") == "1"
    assert fake_redis.xlen("signals") == 1
    # 同じ状態なら発行しない
    strategy.emit_if_changed("USDJPY", "fx", 1, 0.01, 150.0)
    assert fake_redis.xlen("signals") == 1


def test_strategy_skip_without_valid_stop(fake_redis, monkeypatch):
    """ATR が計算できない（stop_distance=0）状態ではシグナルを出さない。"""
    import common
    import strategy

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    strategy.emit_if_changed("EURUSD", "fx", 1, 0.0, 1.09)
    assert fake_redis.xlen("signals") == 0


def _emitted(fake_redis) -> list[dict]:
    import json as _json

    return [_json.loads(fields["data"]) for _id, fields in fake_redis.xrange("signals")]


def test_strategy_flip_doubles_qty_and_sets_position_qty(fake_redis, monkeypatch):
    """反転(+1→-1)は決済+新規の 2 倍量。position_qty は反転後の建玉サイズ。

    従来は反転でも固定 STRATEGY_QTY を送っていたため、実際にはフラット化する
    だけで Redis 状態(-1)と実建玉(0)が乖離していた（発見#1）。
    """
    import common
    import strategy

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    strategy.emit_if_changed("USDJPY", "fx", 1, 0.01, 150.0)   # flat -> long
    strategy.emit_if_changed("USDJPY", "fx", -1, 0.02, 150.5)  # long -> short (flip)

    first, second = _emitted(fake_redis)
    q = strategy.settings.strategy_qty
    assert first["side"] == "BUY" and first["qty"] == q
    assert first["position_qty"] == q
    assert second["side"] == "SELL" and second["qty"] == 2 * q
    assert second["position_qty"] == q
    assert fake_redis.hget("strategy:state", "USDJPY") == "-1"


def test_strategy_flip_uses_actual_position_when_stop_already_fired(fake_redis, monkeypatch):
    """保護ストップ約定で実建玉=0 なのに Redis 状態が +1 のまま → 実建玉基準で 1 倍量。

    Redis 状態だけで 2 倍量を送ると、意図の 2 倍のショートを建ててしまう。
    実建玉が読めるときはそれを優先し、乖離は position_divergence として記録する。
    """
    import common
    import strategy

    events: list[str] = []
    monkeypatch.setattr(common, "log_event", lambda kind, payload: events.append(kind))
    fake_redis.hset("strategy:state", "USDJPY", "1")  # 状態は long のまま

    strategy.emit_if_changed("USDJPY", "fx", -1, 0.02, 150.0, actual_position=0.0)

    (sig,) = _emitted(fake_redis)
    assert sig["side"] == "SELL"
    assert sig["qty"] == strategy.settings.strategy_qty  # 2倍ではなく 1 倍
    assert "position_divergence" in events


def test_strategy_skips_when_already_at_target(fake_redis, monkeypatch):
    """実建玉が既に目標建玉（乖離時にありうる）→ 発注せず状態だけ同期する。"""
    import common
    import strategy

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    fake_redis.hset("strategy:state", "USDJPY", "1")
    q = strategy.settings.strategy_qty

    strategy.emit_if_changed("USDJPY", "fx", -1, 0.02, 150.0, actual_position=-q)

    assert fake_redis.xlen("signals") == 0
    assert fake_redis.hget("strategy:state", "USDJPY") == "-1"


def test_strategy_refuses_when_position_exceeds_target(fake_redis, monkeypatch):
    """実建玉が目標を同方向に超過（手動介入疑い）→ 自動で減らさず通知して見送る。"""
    import common
    import strategy

    notes: list[str] = []
    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(common, "notify", lambda msg, **k: notes.append(msg))
    fake_redis.hset("strategy:state", "USDJPY", "1")
    q = strategy.settings.strategy_qty

    strategy.emit_if_changed("USDJPY", "fx", -1, 0.02, 150.0, actual_position=-2 * q)

    assert fake_redis.xlen("signals") == 0
    assert len(notes) == 1 and "超過" in notes[0]


def test_actual_position_matches_fx_and_stock_contracts():
    """Forex は localSymbol("USD.JPY")/symbol+currency、株は symbol で照合する。"""
    from types import SimpleNamespace

    import strategy

    def pos(symbol, currency, local, amount):
        return SimpleNamespace(
            contract=SimpleNamespace(symbol=symbol, currency=currency, localSymbol=local),
            position=amount,
        )

    class FakeIB:
        def positions(self):
            return [
                pos("USD", "JPY", "USD.JPY", 1000.0),
                pos("AAPL", "USD", "AAPL", 50.0),
            ]

    ib = FakeIB()
    assert strategy._actual_position(ib, "USDJPY") == 1000.0
    assert strategy._actual_position(ib, "AAPL") == 50.0
    assert strategy._actual_position(ib, "EURUSD") == 0.0  # 建玉なし = フラット


def test_actual_position_returns_none_on_error():
    import strategy

    class BrokenIB:
        def positions(self):
            raise RuntimeError("ib down")

    assert strategy._actual_position(BrokenIB(), "USDJPY") is None


# ---- optimizer score -------------------------------------------------------
def test_optimizer_score():
    import auto_optimize

    s = auto_optimize.score({"sharpe_ratio": 1.0, "profit_factor": 2.0, "max_drawdown_pct": 10})
    assert abs(s - 1.18) < 1e-9
    # 高 Sharpe・高 PF・低 DD のほうが高スコアになる
    better = auto_optimize.score({"sharpe_ratio": 2.0, "profit_factor": 3.0, "max_drawdown_pct": 5})
    assert better > s


# ---- optimizer data guard ---------------------------------------------------
def test_optimizer_rejects_missing_data_env():
    import auto_optimize

    path, error = auto_optimize.validate_data_path(None)
    assert path is None and "OPTIMIZE_DATA" in error
    path, error = auto_optimize.validate_data_path("   ")
    assert path is None


def test_optimizer_rejects_bundled_sample(tmp_path):
    """合成サンプルデータで最適化したパラメータの自動配備を拒否する。"""
    import auto_optimize

    sample = tmp_path / "sample_prices.csv"
    sample.write_text("timestamp,open,high,low,close\n")
    path, error = auto_optimize.validate_data_path(str(sample), sample=sample)
    assert path is None
    assert "サンプル" in error


def test_optimizer_rejects_nonexistent_path(tmp_path):
    import auto_optimize

    path, error = auto_optimize.validate_data_path(str(tmp_path / "missing.csv"))
    assert path is None
    assert "存在しない" in error


def test_optimizer_accepts_real_data_path(tmp_path):
    import auto_optimize

    real = tmp_path / "usdjpy_2020_2025.csv"
    real.write_text("timestamp,open,high,low,close\n")
    path, error = auto_optimize.validate_data_path(str(real))
    assert path == real.resolve()
    assert error == ""
