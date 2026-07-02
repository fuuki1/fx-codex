"""実現損益の計上（日次損失ガードの入力）のテスト。

日次損失ガードは fills.realized_pnl の合計だけを見るため、
「加算ではなく上書き」「重複加算」「子注文の計上漏れ」はそのまま
損失上限の誤作動（空振り / 早すぎる発動）につながる。ここで固定する。
"""
from __future__ import annotations

import types

import executor


def _trade(ref: str, symbol: str = "USDJPY", side: str = "SELL", qty: float = 1000):
    return types.SimpleNamespace(
        order=types.SimpleNamespace(orderRef=ref, action=side, totalQuantity=qty),
        contract=types.SimpleNamespace(localSymbol=symbol, symbol=symbol),
    )


def _report(pnl: float, exec_id: str = "e1"):
    return types.SimpleNamespace(realizedPNL=pnl, execId=exec_id)


class _DBRecorder:
    """db_query / db_execute の呼び出しを記録する簡易フェイク。"""

    def __init__(self, update_hits: bool):
        self.queries: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []
        self._update_hits = update_hits

    def query(self, sql: str, params=None):
        self.queries.append((sql, params))
        return [(1,)] if self._update_hits else []

    def execute(self, sql: str, params=None):
        self.executes.append((sql, params))


def _wire(monkeypatch, *, update_hits: bool) -> _DBRecorder:
    import common

    db = _DBRecorder(update_hits)
    monkeypatch.setattr(common, "db_query", db.query)
    monkeypatch.setattr(common, "db_execute", db.execute)
    return db


def test_commission_accumulates_partial_fills(fake_redis, monkeypatch):
    """部分約定で複数回届く PnL は加算する（上書きだと過少計上）。"""
    db = _wire(monkeypatch, update_hits=True)
    executor._on_commission(_trade("tx-a"), None, _report(-300.0, "e1"))
    executor._on_commission(_trade("tx-a"), None, _report(-200.0, "e2"))

    assert len(db.queries) == 2
    for sql, _params in db.queries:
        assert "realized_pnl = realized_pnl + " in sql  # 加算であること
    assert db.queries[0][1] == (-300.0, "tx-a")
    assert db.queries[1][1] == (-200.0, "tx-a")


def test_commission_dedupes_resent_report(fake_redis, monkeypatch):
    """同じ execId の再送（IB 再接続時など）は二重加算しない。"""
    db = _wire(monkeypatch, update_hits=True)
    executor._on_commission(_trade("tx-b"), None, _report(-500.0, "dup"))
    executor._on_commission(_trade("tx-b"), None, _report(-500.0, "dup"))
    assert len(db.queries) == 1


def test_commission_inserts_row_for_unknown_ref(fake_redis, monkeypatch):
    """fills に無い ref（子ストップ注文・手動注文）は行を新規作成して計上する。"""
    db = _wire(monkeypatch, update_hits=False)
    executor._on_commission(_trade("tx-c-sl", side="SELL"), None, _report(-800.0, "e3"))

    assert len(db.executes) == 1
    sql, params = db.executes[0]
    assert sql.startswith("INSERT INTO fills")
    assert params[0] == "USDJPY"
    assert params[5] == "tx-c-sl"
    assert params[6] == -800.0


def test_commission_ignores_sentinel_and_missing_ref(fake_redis, monkeypatch):
    db = _wire(monkeypatch, update_hits=True)
    executor._on_commission(_trade("tx-d"), None, _report(1e301, "e4"))  # IB の sentinel
    executor._on_commission(_trade(""), None, _report(-100.0, "e5"))  # ref 無し
    assert db.queries == []
    assert db.executes == []


def test_daily_pnl_uses_jst_day_boundary(fake_redis, monkeypatch):
    """日次損失の集計窓が JST 日界であること（UTC のままだと朝 9 時にリセットされる）。"""
    import common
    import risk

    captured: list[str] = []

    def fake_query(sql, params=None):
        captured.append(sql)
        return [(0.0,)]

    monkeypatch.setattr(common, "db_query", fake_query)
    assert risk._today_realized_pnl() == 0.0
    assert "Asia/Tokyo" in captured[0]
