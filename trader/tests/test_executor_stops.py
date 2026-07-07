"""executor の保護ストップ追跡・取消（反転/決済時の残存ストップ対策）のテスト。

フラット化後に旧逆指値だけが約定すると「意図しない新規ポジション」になる。
発注前の取消（_cancel_tracked_stop）と発注後の追跡（_track_stop）を検証する。
"""
from __future__ import annotations

import json
from types import SimpleNamespace


class FakeIB:
    def __init__(self, trades=None):
        self._trades = list(trades or [])
        self.cancelled: list[object] = []
        self.placed: list[object] = []
        self.client = SimpleNamespace(getReqId=lambda: 1)

    def openTrades(self):
        return self._trades

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def placeOrder(self, contract, order):
        self.placed.append(order)
        return SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="Submitted"))

    def sleep(self, _sec):
        pass


def _trade(ref: str):
    return SimpleNamespace(order=SimpleNamespace(orderRef=ref))


def test_track_and_cancel_stop(fake_redis, monkeypatch):
    import executor

    ib = FakeIB(trades=[_trade("tx-abc-sl"), _trade("other")])
    monkeypatch.setattr(executor, "_ib", ib)

    executor._track_stop("USDJPY", "tx-abc-sl")
    stored = json.loads(fake_redis.hget(executor.KEY_STOP_ORDERS, "USDJPY"))
    assert stored == {"ref": "tx-abc-sl"}

    executor._cancel_tracked_stop("USDJPY")
    assert [o.orderRef for o in ib.cancelled] == ["tx-abc-sl"]
    assert fake_redis.hget(executor.KEY_STOP_ORDERS, "USDJPY") is None


def test_cancel_without_tracking_is_noop(fake_redis, monkeypatch):
    import executor

    class Boom:
        def openTrades(self):
            raise AssertionError("追跡が無いのに IB へアクセスした")

    monkeypatch.setattr(executor, "_ib", Boom())
    executor._cancel_tracked_stop("USDJPY")  # 例外なく静かに終わる


def test_cancel_when_stop_already_filled(fake_redis, monkeypatch):
    """ストップ到達でクローズ済み → openTrades に現れない → 取消せず無害に終わる。"""
    import common
    import executor

    notes: list[str] = []
    monkeypatch.setattr(common, "notify", lambda msg, **k: notes.append(msg))
    ib = FakeIB(trades=[])
    monkeypatch.setattr(executor, "_ib", ib)

    executor._track_stop("USDJPY", "tx-old-sl")
    executor._cancel_tracked_stop("USDJPY")
    assert ib.cancelled == []
    assert notes == []                        # 正常系なので警告も出さない


def test_track_stop_clears_when_no_stop(fake_redis):
    import executor

    executor._track_stop("USDJPY", "tx-abc-sl")
    executor._track_stop("USDJPY", None)      # ストップ無し発注 → 追跡クリア
    assert fake_redis.hget(executor.KEY_STOP_ORDERS, "USDJPY") is None


def test_cancel_failure_notifies(fake_redis, monkeypatch):
    import common
    import executor

    notes: list[str] = []
    monkeypatch.setattr(common, "notify", lambda msg, **k: notes.append(msg))

    class Broken:
        def openTrades(self):
            raise RuntimeError("ib down")

    monkeypatch.setattr(executor, "_ib", Broken())
    executor._track_stop("USDJPY", "tx-abc-sl")
    executor._cancel_tracked_stop("USDJPY")   # 例外を握りつぶし警告通知
    assert len(notes) == 1
    assert "旧ストップ" in notes[0]


def test_handle_cancels_before_place_and_tracks_after(fake_redis, monkeypatch):
    """発注フローの順序: 旧ストップ取消 → ブラケット送信 → 新ストップ追跡。"""
    import common
    import executor

    calls: list[str] = []
    ib = FakeIB()
    monkeypatch.setattr(executor, "_ib", ib)
    monkeypatch.setattr(executor, "_claim", lambda idem, coid: True)
    monkeypatch.setattr(executor, "_build_contract", lambda sig: object())
    monkeypatch.setattr(executor, "_record_fill", lambda *a, **k: None)
    monkeypatch.setattr(common, "db_execute", lambda *a, **k: None)
    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(common, "notify", lambda *a, **k: None)

    orig_place = ib.placeOrder
    monkeypatch.setattr(
        executor, "_cancel_tracked_stop", lambda symbol: calls.append(f"cancel:{symbol}")
    )
    monkeypatch.setattr(
        executor, "_track_stop", lambda symbol, ref: calls.append(f"track:{symbol}:{bool(ref)}")
    )
    ib.placeOrder = lambda c, o: (calls.append("place"), orig_place(c, o))[1]

    sig = {
        "idem": "sha256:h1", "symbol": "USDJPY", "asset": "fx", "side": "BUY",
        "qty": 1000, "type": "MARKET", "price": 150.0, "stop_distance": 0.5,
        "close": False,
    }
    executor.handle(sig)

    assert calls[0] == "cancel:USDJPY"        # 発注より前に取消
    assert calls[1] == "place"                # 親
    assert calls[2] == "place"                # 子ストップ
    assert calls[3] == "track:USDJPY:True"    # 送信後に追跡
