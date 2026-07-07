"""risk.py へのリスクエンジン配線（RISK_ENGINE_MODE: off/shadow/enforce）のテスト。

既存 8 チェックの挙動が mode=off で完全に不変であること、shadow が発注に影響しないこと、
enforce で却下・サイジング・Kill switch 連動が効くことを検証する。
"""
from __future__ import annotations

import json

import pytest
import risk_engine


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


def _quiet_legacy(monkeypatch):
    """既存チェックが通る状態に固定し、イベントログを捕捉する。"""
    import common
    import risk

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(common, "log_event", lambda kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(common, "notify", lambda *a, **k: None)
    monkeypatch.setattr(risk, "_net_position", lambda symbol: 0.0)
    monkeypatch.setattr(risk, "_today_realized_pnl", lambda: 0.0)
    return events


def _state(**over):
    base = dict(now=1000.0, day_pnl=0.0, week_pnl=0.0, recent_pnls=[], open_positions=[],
                blackout_windows=[], value_per_point=1.0)
    base.update(over)
    return risk_engine.RiskState(**base)


# ---- off（既定）-------------------------------------------------------------
def test_off_mode_never_gathers_state(fake_redis, monkeypatch):
    import risk

    _quiet_legacy(monkeypatch)
    monkeypatch.setattr(risk.settings, "risk_engine_mode", "off")

    def boom(_sig):
        raise AssertionError("off モードで gather_state が呼ばれた")

    monkeypatch.setattr(risk, "gather_state", boom)
    assert risk.evaluate(_sig(idem="off1")) is True


# ---- shadow -----------------------------------------------------------------
def test_shadow_logs_but_does_not_block(fake_redis, monkeypatch):
    import risk

    events = _quiet_legacy(monkeypatch)
    monkeypatch.setattr(risk.settings, "risk_engine_mode", "shadow")
    # ブラックアウト窓内 → エンジンは却下判断のはず
    monkeypatch.setattr(
        risk, "gather_state",
        lambda sig: _state(blackout_windows=[(900.0, 1100.0, "CPI")]),
    )

    sig = _sig(idem="sh1", qty=1000)
    assert risk.evaluate(sig) is True          # 発注はブロックされない
    assert sig["qty"] == 1000                  # 数量も書き換えない

    engine_events = [p for k, p in events if k == "risk_engine_decision"]
    assert len(engine_events) == 1
    assert engine_events[0]["mode"] == "shadow"
    assert engine_events[0]["approved"] is False
    assert engine_events[0]["reason"] == risk_engine.R_BLACKOUT


def test_shadow_gather_state_failure_does_not_block(fake_redis, monkeypatch):
    import risk

    _quiet_legacy(monkeypatch)
    monkeypatch.setattr(risk.settings, "risk_engine_mode", "shadow")

    def boom(_sig):
        raise RuntimeError("db down")

    monkeypatch.setattr(risk, "gather_state", boom)
    assert risk.evaluate(_sig(idem="sh2")) is True


# ---- enforce ------------------------------------------------------------------
def test_enforce_rejects_on_blackout(fake_redis, monkeypatch):
    import risk

    _quiet_legacy(monkeypatch)
    monkeypatch.setattr(risk.settings, "risk_engine_mode", "enforce")
    monkeypatch.setattr(
        risk, "gather_state",
        lambda sig: _state(blackout_windows=[(900.0, 1100.0, "FOMC")]),
    )
    assert risk.evaluate(_sig(idem="en1")) is False


def test_enforce_gather_state_failure_fail_closed(fake_redis, monkeypatch):
    import risk

    _quiet_legacy(monkeypatch)
    monkeypatch.setattr(risk.settings, "risk_engine_mode", "enforce")

    def boom(_sig):
        raise RuntimeError("db down")

    monkeypatch.setattr(risk, "gather_state", boom)
    assert risk.evaluate(_sig(idem="en2")) is False


def test_enforce_sizing_overrides_qty(fake_redis, monkeypatch):
    import risk

    _quiet_legacy(monkeypatch)
    monkeypatch.setattr(risk.settings, "risk_engine_mode", "enforce")
    monkeypatch.setattr(risk.settings, "risk_sizing_enabled", True)
    monkeypatch.setattr(risk, "gather_state", lambda sig: _state())

    # 残高100万 × 0.5% = 5000 / (stop 0.5 × 単価 1.0) = 10000
    sig = _sig(idem="en3", qty=1000, stop_distance=0.5)
    assert risk.evaluate(sig) is True
    assert sig["qty"] == 10000


def test_enforce_loss_streak_trips_kill(fake_redis, monkeypatch):
    import common
    import risk

    events = _quiet_legacy(monkeypatch)
    killed: list[bool] = []
    monkeypatch.setattr(common, "set_kill_switch", lambda on, **k: killed.append(on))
    monkeypatch.setattr(risk.settings, "risk_engine_mode", "enforce")
    monkeypatch.setattr(risk, "gather_state", lambda sig: _state(recent_pnls=[-1] * 5))

    assert risk.evaluate(_sig(idem="en4")) is False
    assert killed == [True]
    engine_events = [p for k, p in events if k == "risk_engine_decision"]
    assert engine_events[0]["reason"] == risk_engine.R_LOSS_STREAK


def test_enforce_close_signal_bypasses_entry_gates(fake_redis, monkeypatch):
    """決済（close=true）は intent=exit として入口ゲート（ブラックアウト等）を免除。"""
    import risk

    _quiet_legacy(monkeypatch)
    monkeypatch.setattr(risk.settings, "risk_engine_mode", "enforce")
    monkeypatch.setattr(
        risk, "gather_state",
        lambda sig: _state(blackout_windows=[(900.0, 1100.0, "CPI")]),
    )
    sig = _sig(idem="en5", close=True, stop_distance=None)
    assert risk.evaluate(sig) is True
    assert sig["qty"] == 1000


def test_shadow_kill_trip_does_not_touch_kill_switch(fake_redis, monkeypatch):
    """shadow では trip_kill_switch 判断でも Kill switch を操作しない。"""
    import common
    import risk

    _quiet_legacy(monkeypatch)
    killed: list[bool] = []
    monkeypatch.setattr(common, "set_kill_switch", lambda on, **k: killed.append(on))
    monkeypatch.setattr(risk.settings, "risk_engine_mode", "shadow")
    monkeypatch.setattr(risk, "gather_state", lambda sig: _state(recent_pnls=[-1] * 5))

    assert risk.evaluate(_sig(idem="sh3")) is True
    assert killed == []


# ---- BlackoutCalendar --------------------------------------------------------
def test_blackout_calendar_parse_and_hot_reload(tmp_path):
    import risk

    path = tmp_path / "cal.json"
    cal = risk.BlackoutCalendar(str(path))
    assert cal.get() == []                     # ファイル無し → 窓ゼロ

    path.write_text(json.dumps({"windows": [
        {"start": "2026-07-15T12:20:00Z", "end": "2026-07-15T13:00:00Z", "label": "US CPI"},
        {"start": "bogus"},                     # 不正はスキップ
    ]}))
    windows = cal.get()
    assert len(windows) == 1
    assert windows[0][2] == "US CPI"
    assert windows[0][0] < windows[0][1]

    path.unlink()
    assert cal.get() == []                     # 消えたら窓ゼロへ戻る


def test_blackout_calendar_swaps_reversed_window(tmp_path):
    import risk

    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"windows": [
        {"start": "2026-07-15T13:00:00Z", "end": "2026-07-15T12:20:00Z", "label": "rev"},
    ]}))
    windows = risk.BlackoutCalendar(str(path)).get()
    assert windows[0][0] < windows[0][1]       # start/end 逆転は入替えて救済


# ---- config ------------------------------------------------------------------
def test_config_parses_thin_windows_and_vpp():
    from config import Settings

    s = Settings(
        thin_liquidity_windows="20:55-22:05,23:30-00:30",
        risk_value_per_point="usdjpy=1.0,EURJPY=0.9",
    )
    assert s.thin_liquidity_windows == [(1255, 1325), (1410, 30)]
    assert s.risk_value_per_point == {"USDJPY": 1.0, "EURJPY": 0.9}


def test_config_rejects_martingale_reduce_factor():
    from config import Settings

    with pytest.raises(ValueError):
        Settings(loss_streak_reduce_factor=1.5)


def test_config_rejects_bad_thin_window():
    from config import Settings

    with pytest.raises(ValueError):
        Settings(thin_liquidity_windows="25:00-26:00")
