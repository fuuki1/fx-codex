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


def test_attempts_field_cleared_on_success(fake_redis):
    import json

    import common

    stream, group = "teststream", "testgroup"
    common.ensure_group(stream, group)
    fields = {"data": json.dumps({"x": 1})}
    msg_id = fake_redis.xadd(stream, fields)
    fake_redis.xreadgroup(group, "c", {stream: ">"}, count=1)  # pending にする

    def boom(_obj):
        raise RuntimeError("boom")

    # 失敗 → attempts フィールドが作られ、未 ACK
    common._handle_one(stream, group, msg_id, fields, boom)
    assert fake_redis.hget(f"attempts:{stream}", msg_id) == "1"
    # 成功 → ACK ＋ attempts フィールドを掃除（肥大化を防ぐ）
    common._handle_one(stream, group, msg_id, fields, lambda _obj: None)
    assert fake_redis.hget(f"attempts:{stream}", msg_id) is None


# ---- config: リスク設定の範囲検証（誤設定を起動時に落とす）--------------------
def test_config_rejects_dangerous_values():
    import pydantic
    from config import Settings

    # reduce_factor>1 は連敗でサイズが増える（マルチンゲール）→ 拒否
    with pytest.raises(pydantic.ValidationError):
        Settings(loss_streak_reduce_factor=1.5)
    with pytest.raises(pydantic.ValidationError):
        Settings(risk_per_trade_pct=0)       # 0 以下は不可
    with pytest.raises(pydantic.ValidationError):
        Settings(max_position_qty=0)         # 0 以下は不可
    # 妥当値は通る
    s = Settings(loss_streak_reduce_factor=0.5, risk_per_trade_pct=0.75, max_position_qty=5000)
    assert s.loss_streak_reduce_factor == 0.5


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


def test_realized_r_multiple():
    import executor

    assert executor.realized_r_multiple(1000.0, 500.0) == 2.0    # +2R
    assert executor.realized_r_multiple(-500.0, 500.0) == -1.0   # -1R
    assert executor.realized_r_multiple(1000.0, None) is None    # リスク不明
    assert executor.realized_r_multiple(1000.0, 0.0) is None     # 0 除算回避


def test_entry_risk_uses_entry_not_exit(fake_redis):
    import executor

    # エントリーのリスクだけを保持（決済注文のリスクで上書きしない = hsetnx）
    executor.record_entry_risk("USDJPY", 5000.0)   # entry
    executor.record_entry_risk("USDJPY", 9999.0)   # exit 側の値では上書きされない
    assert executor.pop_entry_risk("USDJPY") == 5000.0
    assert executor.pop_entry_risk("USDJPY") is None   # 決済で消える
    # サイジング無し（intended_risk<=0）は記録しない → R は NULL 扱い
    executor.record_entry_risk("EURUSD", 0.0)
    assert executor.pop_entry_risk("EURUSD") is None


# ---- executor: 保護ストップ・撤退・約定の純粋ヘルパー（Issue 1/2）------------
def test_executor_stop_pure_helpers():
    import executor

    assert executor.reverse_action("BUY") == "SELL"
    assert executor.reverse_action("sell") == "BUY"
    # ロング（BUY エントリー）は下側、ショート（SELL エントリー）は上側にストップ
    assert executor.protective_stop_price("BUY", 150.0, 0.3) == pytest.approx(149.7)
    assert executor.protective_stop_price("SELL", 150.0, 0.3) == pytest.approx(150.3)
    assert executor.is_exit_order({"intent": "exit"}) is True
    assert executor.is_exit_order({"intent": "entry"}) is False
    assert executor.wants_protective_stop({"stop_distance": 0.3}) is True
    assert executor.wants_protective_stop({"stop_distance": 0}) is False
    assert executor.wants_protective_stop({"intent": "exit", "stop_distance": 0.3}) is False
    assert executor.exec_side_to_action("BOT") == "BUY"
    assert executor.exec_side_to_action("SLD") == "SELL"


def test_parse_execution_and_symbol_match():
    import executor

    class E:
        execId = "0001"
        side = "BOT"
        shares = 1000.0
        price = 150.25
        orderRef = "tx-abc"

    class C:
        symbol = "USD"
        currency = "JPY"
        localSymbol = "USD.JPY"

    class Fill:
        execution = E()
        contract = C()

    ex = executor.parse_execution(Fill())
    assert ex["exec_id"] == "0001" and ex["side"] == "BUY" and ex["shares"] == 1000.0
    assert ex["price"] == 150.25 and ex["ref"] == "tx-abc"
    assert executor.symbol_matches_contract(C(), "USDJPY") is True
    assert executor.symbol_matches_contract(C(), "EURUSD") is False
    # execId が無ければ None（記録しない）
    class NoId:
        execution = type("X", (), {"execId": ""})()
    assert executor.parse_execution(NoId()) is None


def test_record_execution_idempotent_and_records_actual_fill(monkeypatch):
    import common
    import executor

    inserts: list = []
    exists = {"v": False}
    monkeypatch.setattr(common, "db_query", lambda sql, params=None: [(1,)] if exists["v"] else [])
    monkeypatch.setattr(common, "db_execute", lambda sql, params=None: inserts.append((sql, params)))

    execu = {"exec_id": "e1", "side": "BUY", "shares": 1000.0, "price": 150.25,
             "ref": "tx-abc", "symbol": "USD.JPY"}
    ctx = {"symbol": "USDJPY", "idem": "i1", "intended_risk": 5000.0, "stop_distance": 0.3}
    executor.record_execution(execu, ctx)
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "INSERT INTO fills" in sql
    # 実約定価格・数量・execId が記録される（想定値ではない）
    assert 150.25 in params and 1000.0 in params and "e1" in params and "i1" in params
    # 正規化ペア（USDJPY）を保存する（ブローカーの localSymbol USD.JPY ではない）
    assert "USDJPY" in params and "USD.JPY" not in params
    # 2 回目は既存（exec_id 一致）なので INSERT しない（冪等）
    exists["v"] = True
    executor.record_execution(execu, ctx)
    assert len(inserts) == 1


def test_on_commission_updates_by_exec_id(fake_redis, monkeypatch):
    import common
    import executor

    updates: list = []
    monkeypatch.setattr(common, "db_query", lambda sql, params=None: [("USDJPY",)])
    monkeypatch.setattr(common, "db_execute", lambda sql, params=None: updates.append((sql, params)))
    executor.record_entry_risk("USDJPY", 5000.0)

    report = type("R", (), {"realizedPNL": 10000.0, "execId": "e9"})()
    fill = type("F", (), {"execution": type("E", (), {"execId": "e9"})()})()
    trade = type("T", (), {"order": type("O", (), {"orderRef": "tx-abc:stop"})()})()
    executor._on_commission(trade, fill, report)

    assert updates and "UPDATE fills SET realized_pnl" in updates[0][0]
    _, params = updates[0]
    assert params == (10000.0, 2.0, "e9")             # R = 10000 / 5000 = 2.0（execId 基準）
    assert executor.pop_entry_risk("USDJPY") is None  # エントリーリスクは消費済み


# ---- executor: 発注ハンドラ（保護ストップ設置・撤退取消）---------------------
class _FakeIB:
    def __init__(self):
        self.placed: list = []
        self.cancelled: list = []
        self.open_trades: list = []

    def placeOrder(self, contract, order):
        self.placed.append(order)
        status = type("S", (), {"status": "Filled", "avgFillPrice": 150.0})()
        return type("T", (), {"order": order, "orderStatus": status, "contract": contract})()

    def sleep(self, _s):
        pass

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def openTrades(self):
        return self.open_trades


@pytest.fixture
def exec_stub(fake_redis, monkeypatch):
    import common
    import executor

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(common, "notify", lambda *a, **k: None)
    monkeypatch.setattr(common, "db_execute", lambda *a, **k: None)
    monkeypatch.setattr(common, "db_query", lambda *a, **k: [])   # _prior_status 等
    fake = _FakeIB()
    monkeypatch.setattr(executor, "_ib", fake)
    return fake


def _order_sig(**over):
    base = {"idem": "o1", "symbol": "USDJPY", "asset": "fx", "side": "BUY",
            "qty": 1000, "type": "MARKET", "intent": "entry"}
    base.update(over)
    return base


def test_handle_entry_places_protective_stop(exec_stub, fake_redis):
    import executor

    executor.handle(_order_sig(idem="e1", stop_distance=0.3))
    # 親（成行）＋ 保護ストップの 2 件が発注される（バックテストの ATR ストップに対応）
    assert len(exec_stub.placed) == 2
    stop = exec_stub.placed[1]
    assert stop.orderType == "STP"
    assert stop.action == "SELL"                       # BUY エントリーの反対
    assert stop.auxPrice == pytest.approx(149.7)       # 150.0 - 0.3
    assert stop.totalQuantity == 1000
    assert str(stop.orderRef).endswith(":stop")


def test_handle_entry_without_stop_distance_no_stop(exec_stub):
    import executor

    executor.handle(_order_sig(idem="e2"))             # stop_distance 無し
    assert len(exec_stub.placed) == 1                  # 成行のみ、保護ストップ無し


def test_handle_exit_cancels_protective_stops(exec_stub):
    import executor

    # 既存の保護ストップ（USDJPY）を openTrades に用意
    contract = type("C", (), {"symbol": "USD", "currency": "JPY", "localSymbol": "USD.JPY"})()
    stop_order = type("O", (), {"orderRef": "tx-parent:stop"})()
    exec_stub.open_trades = [type("T", (), {"order": stop_order, "contract": contract})()]

    executor.handle(_order_sig(idem="x1", side="SELL", intent="exit", stop_distance=None))
    # 撤退では既存ストップを取り消してからフラット化する（残ると反対建てになる）
    assert stop_order in exec_stub.cancelled
    assert len(exec_stub.placed) == 1                  # 撤退の成行のみ（新規ストップは付けない）


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


def test_risk_exit_order_passthrough(risk_stub):
    import risk

    # 撤退（フラット化）は要求数量そのまま承認（リサイズしない・intended_risk=0）
    d = risk.decide(_sig(idem="ex1", intent="exit", qty=1000, symbol="USDJPY"))
    assert d.approved is True
    assert d.sized_qty == 1000
    assert d.intended_risk == 0.0


def test_risk_exit_bypasses_entry_gates(risk_stub, monkeypatch):
    import time

    import risk

    now = time.time()
    # 通常はブラックアウトで新規却下される状況でも、撤退は素通しできる（手仕舞いを妨げない）
    monkeypatch.setattr(risk._calendar, "get", lambda: [(now - 60, now + 60, "US CPI")])
    assert risk.evaluate(_sig(idem="exb", intent="exit")) is True
    assert risk.evaluate(_sig(idem="enb")) is False   # 対照: entry は却下される


# ---- reconcile: 保護ストップの子注文を孤児と誤検知しない（Issue 1 付随）--------
def test_reconcile_known_ref_accepts_stop_children():
    import reconcile

    known = {"tx-abc", "tx-def"}
    assert reconcile._is_known_ref("tx-abc", known) is True
    assert reconcile._is_known_ref("tx-abc:stop", known) is True    # 既知の親の保護ストップ
    assert reconcile._is_known_ref("tx-zzz:stop", known) is False   # 未知の親 → 孤児
    assert reconcile._is_known_ref("tx-zzz", known) is False


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


def test_day_week_pnl_use_configured_timezone(monkeypatch):
    import common
    import risk

    captured: list[tuple] = []

    def fake_query(sql, params=None):
        captured.append((sql, params))
        return [(0,)]

    monkeypatch.setattr(common, "db_query", fake_query)
    monkeypatch.setattr(risk.settings, "risk_day_timezone", "Asia/Tokyo")

    risk._day_pnl()
    sql, params = captured[-1]
    assert "AT TIME ZONE" in sql            # UTC 固定ではなく設定 tz で境界を切る
    assert params == ("day", "Asia/Tokyo", "Asia/Tokyo")

    risk._week_pnl()
    _, params = captured[-1]
    assert params == ("week", "Asia/Tokyo", "Asia/Tokyo")


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
    monkeypatch.setattr(strategy.settings, "strategy_qty", 1000)
    # 初回 flat->long は entry を 1 件発行
    strategy.emit_transition("USDJPY", "fx", 1, 0.01, current_pos=0.0)
    assert fake_redis.hget("strategy:state", "USDJPY") == "1"
    assert fake_redis.xlen("signals") == 1
    # 既にロング（目標と同方向）なら積み増さない = 発行しない
    strategy.emit_transition("USDJPY", "fx", 1, 0.01, current_pos=1000.0)
    assert fake_redis.xlen("signals") == 1


# ---- strategy: バックテストと一致するポジション遷移（Issue 3）---------------
def test_plan_transition_matches_backtest_transitions():
    import strategy

    # フラット→ロング: entry 1 件（stop 付き）
    o = strategy.plan_transition(0.0, 1, 1000.0, 0.3)
    assert o == [{"side": "BUY", "qty": 1000.0, "intent": "entry", "stop_distance": 0.3}]

    # ロング→フラット(目標0): 現在建玉ぶんの exit（stop なし）
    o = strategy.plan_transition(1000.0, 0, 1000.0, 0.3)
    assert o == [{"side": "SELL", "qty": 1000.0, "intent": "exit"}]

    # ロング→ショート(反転): exit（現在建玉）＋ entry（1 単位）= 実質 2 単位
    o = strategy.plan_transition(1000.0, -1, 1000.0, 0.3)
    assert o == [
        {"side": "SELL", "qty": 1000.0, "intent": "exit"},
        {"side": "SELL", "qty": 1000.0, "intent": "entry", "stop_distance": 0.3},
    ]

    # ショート→ロング(反転): BUY で決済＋BUY で新規
    o = strategy.plan_transition(-2000.0, 1, 1000.0, 0.3)
    assert o == [
        {"side": "BUY", "qty": 2000.0, "intent": "exit"},
        {"side": "BUY", "qty": 1000.0, "intent": "entry", "stop_distance": 0.3},
    ]

    # 既に目標方向で保有: 何もしない（積み増さない）
    assert strategy.plan_transition(1000.0, 1, 1000.0, 0.3) == []
    # 既にフラットで目標フラット: 何もしない
    assert strategy.plan_transition(0.0, 0, 1000.0, 0.3) == []


def test_plan_transition_reenters_after_stop_out():
    import strategy

    # ストップで建玉が消えて（pos=0）目標が続く（+1）なら再エントリー（バックテスト同様）
    o = strategy.plan_transition(0.0, 1, 1000.0, 0.25)
    assert o == [{"side": "BUY", "qty": 1000.0, "intent": "entry", "stop_distance": 0.25}]


def test_emit_transition_reversal_emits_exit_then_entry(fake_redis, monkeypatch):
    import json

    import common
    import strategy

    monkeypatch.setattr(common, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(strategy.settings, "strategy_qty", 1000)
    # 現在ロング 1000、目標ショート → exit(SELL) と entry(SELL,stop) の 2 件
    strategy.emit_transition("USDJPY", "fx", -1, 0.3, current_pos=1000.0)
    assert fake_redis.xlen("signals") == 2
    msgs = [json.loads(f["data"]) for _id, f in fake_redis.xrange("signals")]
    assert [m["intent"] for m in msgs] == ["exit", "entry"]
    assert "stop_distance" not in msgs[0]          # 撤退にストップは付けない
    assert msgs[1]["stop_distance"] == 0.3         # エントリーにストップを載せる
    assert msgs[0]["idem"] != msgs[1]["idem"]      # 冪等キーは別
    assert fake_redis.hget("strategy:state", "USDJPY") == "-1"


def test_net_from_positions_matches_fx_and_stock():
    import strategy

    class C:
        def __init__(self, symbol, currency="", local=""):
            self.symbol = symbol
            self.currency = currency
            self.localSymbol = local

    class P:
        def __init__(self, contract, position):
            self.contract = contract
            self.position = position

    positions = [
        P(C("USD", "JPY", "USD.JPY"), 1000.0),   # USDJPY ロング
        P(C("EUR", "USD", "EUR.USD"), -500.0),   # 別ペア（無視される）
        P(C("USD", "JPY", "USD.JPY"), 500.0),    # 同ペア加算
    ]
    assert strategy.net_from_positions(positions, "USDJPY") == 1500.0
    assert strategy.net_from_positions([P(C("AAPL"), 10.0)], "AAPL") == 10.0
    assert strategy.net_from_positions(positions, "GBPUSD") == 0.0


# ---- strategy: 履歴バーの十分性（Issue 4）----------------------------------
def test_required_bars_default_covers_slow_window():
    import strategy

    # 既定 slow=60 -> 61 本以上を要求（旧実装の 40 本では常に None だった回帰の防止）
    need = strategy.required_bars(strategy.DEFAULT_PARAMS)
    assert need >= strategy.DEFAULT_PARAMS["slow_window"] + 1
    # 5 秒バーで十分な本数の期間になっていること
    dur = strategy.hist_duration_str(need)
    secs = int(dur.split()[0])
    assert secs // strategy.HIST_BAR_SECONDS >= need


def test_hist_duration_str_clamps_to_safe_max():
    import strategy

    huge = strategy.hist_duration_str(100_000)
    assert int(huge.split()[0]) == strategy.HIST_MAX_DURATION_SEC
    small = strategy.hist_duration_str(1)
    assert int(small.split()[0]) >= 300


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
