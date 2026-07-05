"""時間足別の採点・学習(fx_intel.tf_learning)のテスト(ネットワーク不要)。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, UTC

from fx_intel.learning import NEWS_WEIGHT, TECH_WEIGHT
from fx_intel.tf_learning import (
    TimeframeLearning,
    auxiliary_horizon_report_ja,
    derive_timeframe_learning,
    entries_for_timeframe,
    evaluate_timeframe_history,
    load_timeframe_learning,
    merge_timeframe_learning,
    save_timeframe_learning,
)

# 月曜 08:00 UTC 起点(市場オープン中)
START = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)


def _entry(ts, timeframe, horizon, direction, close, atr=0.10, **features):
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
        "features": features or {"rsi_1h": 55.0},
    }


def _losing_1h(count: int = 20) -> list[dict]:
    """1h の long 判断を連続記録し、実際は下落し続ける(全 miss)履歴。"""
    entries = []
    price = 156.0
    for i in range(count):
        entries.append(
            _entry(
                START + timedelta(hours=i),
                "1h",
                1.0,
                "long",
                price,
                rsi_1h=70.0,
                adx_1h=15.0,
            )
        )
        price -= 0.05
    return entries


def _winning_4h(count: int = 12) -> list[dict]:
    """4h の long 判断を記録し、実際は上昇し続ける(全 hit)履歴。"""
    entries = []
    price = 156.0
    for i in range(count):
        entries.append(
            _entry(
                START + timedelta(hours=i * 4),
                "4h",
                4.0,
                "long",
                price,
                atr=0.30,
                rsi_1h=55.0,
                adx_1h=30.0,
            )
        )
        price += 0.20
    return entries


def _winning_1h(count: int = 25) -> list[dict]:
    """1h の long 判断を記録し、実際は上昇し続ける(全 hit)履歴。"""
    entries = []
    price = 156.0
    for i in range(count):
        entries.append(
            _entry(
                START + timedelta(hours=i),
                "1h",
                1.0,
                "long",
                price,
                rsi_1h=55.0,
                adx_1h=30.0,
            )
        )
        price += 0.05
    return entries


# ------------------------------------------------- 切り出し・採点


def test_entries_for_timeframe_filters_and_ignores_legacy() -> None:
    mixed = _losing_1h(3) + [
        {"ts": START.isoformat(), "symbol": "USDJPY", "close": 156.0}  # 旧スキーマ(timeframe無し)
    ]
    only_1h = entries_for_timeframe(mixed, "1h")
    assert len(only_1h) == 3
    assert all(e["timeframe"] == "1h" for e in only_1h)


def test_each_timeframe_scored_at_its_own_horizon() -> None:
    entries = _losing_1h(20) + _winning_4h(12)
    calls_1h = [
        c for c in evaluate_timeframe_history(entries, "1h") if c.outcome in ("hit", "miss")
    ]
    calls_4h = [
        c for c in evaluate_timeframe_history(entries, "4h") if c.outcome in ("hit", "miss")
    ]
    # 1h は下落continuation で全 miss、4h は上昇で全 hit
    assert calls_1h and all(c.outcome == "miss" for c in calls_1h)
    assert calls_4h and all(c.outcome == "hit" for c in calls_4h)


def test_15m_uses_short_horizon_not_24h() -> None:
    # 15m 判断: 15分後に上昇 → hit。24h固定採点だと拾えないが主ホライズンなら拾える
    entries = [
        _entry(START, "15m", 0.25, "long", 150.00, atr=0.05),
        _entry(START + timedelta(minutes=15), "15m", 0.25, "long", 150.10, atr=0.05),
    ]
    calls = [c for c in evaluate_timeframe_history(entries, "15m") if c.outcome in ("hit", "miss")]
    assert len(calls) == 1
    assert calls[0].outcome == "hit"


# ------------------------------------------------- 学習プロファイル


def test_derive_produces_per_symbol_timeframe_cells() -> None:
    learning = derive_timeframe_learning(
        _losing_1h(20) + _winning_4h(12), now=START + timedelta(days=3)
    )
    assert ("USDJPY", "1h") in learning.profiles
    assert ("USDJPY", "4h") in learning.profiles
    assert "1h" in learning.per_timeframe
    assert "4h" in learning.per_timeframe


def test_weak_cell_decays_conviction() -> None:
    learning = derive_timeframe_learning(_losing_1h(20), now=START + timedelta(days=3))
    tech_w, news_w, conviction_factor, adjuster = learning.profile_lookup("USDJPY", "1h")
    assert conviction_factor < 1.0  # 全 miss なのでペア別減衰が発動
    assert adjuster is not None


def test_strong_cell_does_not_inflate() -> None:
    learning = derive_timeframe_learning(_winning_4h(12), now=START + timedelta(days=3))
    _, _, conviction_factor, _ = learning.profile_lookup("USDJPY", "4h")
    assert conviction_factor == 1.0  # 好成績でも増幅しない(減衰のみ)


def test_unseen_cell_returns_defaults() -> None:
    learning = derive_timeframe_learning(_losing_1h(20), now=START + timedelta(days=3))
    assert learning.profile_lookup("EURUSD", "15m") == (TECH_WEIGHT, NEWS_WEIGHT, 1.0, None)


def test_condition_adjuster_decays_matching_state() -> None:
    learning = derive_timeframe_learning(_losing_1h(20), now=START + timedelta(days=3))
    _, _, _, adjuster = learning.profile_lookup("USDJPY", "1h")
    assert adjuster is not None
    factor, reason = adjuster({"rsi_1h": 70.0, "adx_1h": 15.0}, "long")
    assert factor < 1.0
    assert reason  # 減衰理由の文が付く


def test_horizon_label_in_notes_matches_timeframe() -> None:
    learning = derive_timeframe_learning(_losing_1h(20), now=START + timedelta(days=3))
    notes = learning.per_timeframe["1h"].notes_ja
    # 24h ではなく 1時間後 と表示される
    assert any("1時間後" in note for note in notes)
    assert not any("24時間後" in note for note in notes)


# ------------------------------------------------- 補助ホライズン(観測)


def test_auxiliary_report_labeled_not_for_learning() -> None:
    report = auxiliary_horizon_report_ja(_losing_1h(20), "1h")
    assert report == "" or "学習には不使用" in report


def test_auxiliary_report_empty_when_no_entries() -> None:
    assert auxiliary_horizon_report_ja([], "1h") == ""


# ------------------------------------------------- 表示・保存


def test_summary_ja_has_timeframe_headers() -> None:
    learning = derive_timeframe_learning(
        _losing_1h(20) + _winning_4h(12), now=START + timedelta(days=3)
    )
    summary = learning.summary_ja()
    assert "1時間足" in summary
    assert "4時間足" in summary


def test_empty_learning_summary_is_placeholder() -> None:
    learning = TimeframeLearning()
    assert "蓄積中" in learning.summary_ja()


def test_save_timeframe_learning_roundtrip(tmp_path) -> None:
    learning = derive_timeframe_learning(_losing_1h(20), now=START + timedelta(days=3))
    path = tmp_path / "tf_learning.json"
    save_timeframe_learning(learning, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "profiles" in payload
    assert "USDJPY|1h" in payload["profiles"]
    cell = payload["profiles"]["USDJPY|1h"]
    assert cell["evaluated"] > 0
    assert "symbol_factors" in cell
    assert "condition_stats" in cell


def test_load_timeframe_learning_roundtrip(tmp_path) -> None:
    learning = derive_timeframe_learning(_losing_1h(20), now=START + timedelta(days=3))
    path = tmp_path / "tf_learning.json"
    save_timeframe_learning(learning, path)

    loaded = load_timeframe_learning(path)

    assert ("USDJPY", "1h") in loaded.profiles
    _, _, conviction_factor, adjuster = loaded.profile_lookup("USDJPY", "1h")
    assert conviction_factor < 1.0
    assert adjuster is not None


def test_baseline_used_until_live_cell_has_enough_samples() -> None:
    baseline = derive_timeframe_learning(_losing_1h(20), now=START + timedelta(days=3))

    merged = merge_timeframe_learning(
        TimeframeLearning(),
        baseline,
        min_live_evaluated=20,
    )
    _, _, baseline_factor, _ = merged.profile_lookup("USDJPY", "1h")
    assert baseline_factor < 1.0
    assert "履歴ベースライン" in merged.per_timeframe["1h"].notes_ja[0]

    live = derive_timeframe_learning(_winning_1h(25), now=START + timedelta(days=3))
    merged = merge_timeframe_learning(live, baseline, min_live_evaluated=20)
    _, _, live_factor, _ = merged.profile_lookup("USDJPY", "1h")
    assert live_factor == 1.0
    assert not merged.per_timeframe["1h"].notes_ja[0].startswith("履歴ベースライン")
