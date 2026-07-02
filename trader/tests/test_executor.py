"""executor の発注経路（ブラケット構築と handle の配線）のテスト。

IB Gateway は FakeIB で置き換え、「親注文＋ストップが対で送信される」
「ストップ無し（close）は単発 transmit=True で送信される」を固定する。
"""
from __future__ import annotations

import types

import executor


# ---- stop_price_for ---------------------------------------------------------
def test_stop_price_for_buy_distance_jpy_rounding():
    sig = {"symbol": "USDJPY", "asset": "fx", "side": "BUY", "price": 150.0, "stop_distance": 0.5}
    assert executor.stop_price_for(sig) == 149.5


def test_stop_price_for_sell_distance_eurusd_rounding():
    sig = {"symbol": "EURUSD", "asset": "fx", "side": "SELL", "price": 1.09123, "stop_distance": 0.0025}
    assert executor.stop_price_for(sig) == 1.09373


def test_stop_price_for_absolute_price_wins():
    sig = {"symbol": "USDJPY", "side": "BUY", "stop_price": 148.2, "stop_distance": 0.5, "price": 150.0}
    assert executor.stop_price_for(sig) == 148.2


def test_stop_price_for_close_is_none():
    assert executor.stop_price_for({"symbol": "USDJPY", "side": "SELL", "close": True}) is None


def test_stop_price_for_missing_stop_is_none():
    assert executor.stop_price_for({"symbol": "USDJPY", "side": "BUY", "price": 150.0}) is None


# ---- _build_orders ----------------------------------------------------------
def _next_id_from(start: int):
    counter = {"v": start - 1}

    def _next() -> int:
        counter["v"] += 1
        return counter["v"]

    return _next


def test_build_orders_bracket_links_parent_and_stop():
    sig = {
        "symbol": "USDJPY",
        "asset": "fx",
        "side": "BUY",
        "qty": 1000,
        "type": "MARKET",
        "price": 150.0,
        "stop_distance": 0.5,
    }
    parent, stop = executor._build_orders(sig, "tx-abc", next_id=_next_id_from(100))
    # 親は transmit=False（子ストップと束ねて送信 = 裸ポジションの隙を作らない）
    assert parent.transmit is False
    assert parent.orderId == 100
    assert parent.orderRef == "tx-abc"
    assert stop is not None
    assert stop.parentId == 100
    assert stop.orderId == 101
    assert stop.action == "SELL"  # BUY の逆
    assert float(stop.totalQuantity) == 1000.0
    assert float(stop.auxPrice) == 149.5
    assert stop.transmit is True
    assert stop.orderRef == "tx-abc-sl"


def test_build_orders_close_is_single_order():
    sig = {"symbol": "USDJPY", "asset": "fx", "side": "SELL", "qty": 1000, "type": "MARKET", "close": True}
    parent, stop = executor._build_orders(sig, "tx-abc", next_id=_next_id_from(1))
    assert stop is None
    assert parent.transmit is True


# ---- handle（FakeIB で配線を確認）------------------------------------------
class _FakeTrade:
    def __init__(self, order):
        self.order = order
        self.orderStatus = types.SimpleNamespace(status="Submitted")


class _FakeIB:
    def __init__(self):
        self.placed = []
        self._id = 0
        self.client = types.SimpleNamespace(getReqId=self._next_id)

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return _FakeTrade(order)

    def sleep(self, _seconds: float) -> None:
        pass


def _handle_sig(**over):
    base = {
        "idem": "exec-test-1",
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


def _patch_io(monkeypatch):
    import common

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(common, "notify", lambda *a, **k: None)
    monkeypatch.setattr(common, "db_execute", lambda *a, **k: None)


def test_handle_places_parent_and_stop(fake_redis, monkeypatch):
    _patch_io(monkeypatch)
    monkeypatch.setattr(executor, "_claim", lambda idem, coid: True)
    fake = _FakeIB()
    monkeypatch.setattr(executor, "_ib", fake)

    executor.handle(_handle_sig())

    assert len(fake.placed) == 2
    parent = fake.placed[0][1]
    stop = fake.placed[1][1]
    assert parent.transmit is False
    assert stop.parentId == parent.orderId
    assert float(stop.auxPrice) == 149.5


def test_handle_close_places_single_order(fake_redis, monkeypatch):
    _patch_io(monkeypatch)
    monkeypatch.setattr(executor, "_claim", lambda idem, coid: True)
    fake = _FakeIB()
    monkeypatch.setattr(executor, "_ib", fake)

    executor.handle(_handle_sig(idem="exec-test-2", close=True, stop_distance=None))

    assert len(fake.placed) == 1
    assert fake.placed[0][1].transmit is True


def test_handle_skips_when_already_claimed(fake_redis, monkeypatch):
    _patch_io(monkeypatch)
    monkeypatch.setattr(executor, "_claim", lambda idem, coid: False)
    monkeypatch.setattr(executor, "_prior_status", lambda idem: "submitted")
    fake = _FakeIB()
    monkeypatch.setattr(executor, "_ib", fake)

    executor.handle(_handle_sig(idem="exec-test-3"))

    assert fake.placed == []  # 二重発注しない


def test_handle_blocked_by_kill_switch(fake_redis, monkeypatch):
    _patch_io(monkeypatch)
    fake_redis.set("kill_switch", "1")
    fake = _FakeIB()
    monkeypatch.setattr(executor, "_ib", fake)

    executor.handle(_handle_sig(idem="exec-test-4"))

    assert fake.placed == []
