"""5分価格スナップショットと採点結合のテスト(ネットワーク不要)。

核心の回帰テスト: 判断ジャーナルが毎時しか追記されない現実の運用では、
15m 判断(採点窓[9,21分])は後続の判断行が得られず永久に採点されない。
fx_tf_snapshot.py が5分ごとに記録する価格系列を採点入力に結合することで
15m/1h も採点可能になることを、合成データで明示的に検証する。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, UTC

from fx_intel import price_history, tf_learning
from fx_intel.technicals import IntervalView, PairTechnicals

import fx_tf_snapshot

# 月曜 08:00 UTC 起点(市場オープン中)
START = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)


def _judgment(ts, timeframe, horizon, direction, close, atr=0.05):
    """時間足別ジャーナルの1判断行(direction 付き=採点対象)。"""
    return {
        "ts": ts.isoformat(),
        "symbol": "USDJPY",
        "timeframe": timeframe,
        "horizon_hours": horizon,
        "direction": direction,
        "conviction": 60,
        "composite": 0.4,
        "tech_score": 0.5,
        "news_score": 0.2,
        "close": close,
        "atr": atr,
        "features": {"rsi_1h": 55.0},
    }


def _five_min_prices(timeframe, hours, start_price, step_per_point):
    """5分刻みの価格スナップショット行(direction 無し=採点対象外)。"""
    rows = []
    price = start_price
    ts = START
    for _ in range(hours * 12):
        rows.append(
            {"ts": ts.isoformat(), "symbol": "USDJPY", "timeframe": timeframe, "close": price}
        )
        price += step_per_point
        ts += timedelta(minutes=5)
    return rows


# ------------------------------------------------- collect_closes / append


def test_collect_closes_reads_each_interval_close() -> None:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views["15m"] = IntervalView("15m", "BUY", 5, 1, 2, close=150.0)
    tech.views["1h"] = IntervalView("1h", "SELL", 1, 5, 2, close=150.2)
    closes = fx_tf_snapshot.collect_closes({"USDJPY": tech})
    assert closes["USDJPY"]["15m"] == 150.0
    assert closes["USDJPY"]["1h"] == 150.2
    # 取得できなかった足は None
    assert closes["USDJPY"]["4h"] is None


def test_append_snapshot_writes_jsonl(tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    rows = price_history.snapshot_entries({"USDJPY": {"1h": 157.0, "4h": 157.2}}, now=START)
    fx_tf_snapshot.append_snapshot(path, rows)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert {row["timeframe"] for row in parsed} == {"1h", "4h"}
    assert all(row["symbol"] == "USDJPY" and "direction" not in row for row in parsed)


# ------------------------------------------------- 核心: 5分価格で15mが採点可能に


def test_15m_unscorable_with_hourly_journal_only() -> None:
    """毎時判断だけでは 15m は採点窓に入る将来価格が無く、採点ゼロ。"""
    hourly = [_judgment(START + timedelta(hours=h), "15m", 0.25, "long", 150.0) for h in range(4)]
    calls = [
        c
        for c in tf_learning.evaluate_timeframe_history(hourly, "15m")
        if c.outcome in ("hit", "miss")
    ]
    assert calls == []  # 後続行が60分後しか無く、窓[9,21分]に入らない


def test_15m_scorable_after_merging_5min_snapshots() -> None:
    """5分価格を結合すると 15m 判断が採点できるようになる(上昇継続で全hit)。"""
    hourly = [_judgment(START + timedelta(hours=h), "15m", 0.25, "long", 150.0) for h in range(4)]
    prices = _five_min_prices("15m", hours=4, start_price=150.0, step_per_point=0.01)
    merged = hourly + prices
    calls = [
        c
        for c in tf_learning.evaluate_timeframe_history(merged, "15m")
        if c.outcome in ("hit", "miss")
    ]
    assert len(calls) == 4
    assert all(c.outcome == "hit" for c in calls)  # 15分後は必ず上昇している


def test_snapshot_rows_do_not_add_scoring_targets() -> None:
    """価格行(direction 無し)は将来価格系列にだけ寄与し、採点対象を増やさない。"""
    prices = _five_min_prices("15m", hours=4, start_price=150.0, step_per_point=0.01)
    # 判断行ゼロなら採点対象もゼロ(価格行だけでは call が立たない)
    calls = tf_learning.evaluate_timeframe_history(prices, "15m")
    assert calls == []


def test_current_snapshot_scores_the_just_matured_1h_decision() -> None:
    """現在価格を系列最新点に足すと、直前に成熟した 1h 判断を即採点できる。"""
    # 1時間前の 1h long 判断。将来価格はまだジャーナルに無い
    judged = [_judgment(START, "1h", 1.0, "long", 150.0)]
    # 今回の現在価格(記録から約1時間後、上昇)を snapshot_entries で最新点にする
    current = price_history.snapshot_entries(
        {"USDJPY": {"1h": 150.5}}, now=START + timedelta(hours=1)
    )
    merged = judged + current
    calls = [
        c
        for c in tf_learning.evaluate_timeframe_history(merged, "1h")
        if c.outcome in ("hit", "miss")
    ]
    assert len(calls) == 1
    assert calls[0].outcome == "hit"


def test_full_timeframe_learning_uses_merged_series() -> None:
    """derive_timeframe_learning が結合系列で 15m セルの学習を導けること。"""
    hourly = [_judgment(START + timedelta(hours=h), "15m", 0.25, "long", 150.0) for h in range(20)]
    prices = _five_min_prices("15m", hours=20, start_price=150.0, step_per_point=-0.01)
    # 下落継続なので long は全 miss → ペア別減衰が発動するはず
    learning = tf_learning.derive_timeframe_learning(hourly + prices, now=START + timedelta(days=2))
    assert ("USDJPY", "15m") in learning.profiles
    profile = learning.profiles[("USDJPY", "15m")]
    assert profile.evaluated > 0  # 15m が採点できている
    _, _, conviction_factor, _ = learning.profile_lookup("USDJPY", "15m")
    assert conviction_factor < 1.0  # 全 miss なので確信度が減衰
