"""期待値ガード反実仮想採点(学習飢餓デッドロック解消)のテスト。ネットワーク不要。

再現する欠陥: 期待値ガードがblockした判断は direction=neutral で記録され、
採点対象から消える。ガード根拠のサンプルが永遠に増えず、blockが恒久化する
(2026-07-17〜の実機で観測: 根拠n=28のまま全新規判断が見送りに固定)。

修正の設計: expectancy_guard「単独」で見送りになった行を、判断時に凍結記録した
シャドー計画(shadow_predictionsのfusion_raw)と分析方向(analysis_direction)から
復元し、実績と同じ採点エンジンでガード根拠に加える。推奨(direction)は
neutralのまま変えない。合成に事後計算は使わない(PIT安全・fail-closed)。
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

from fx_intel import journal, learning, trade_outcome as to

MONDAY = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
# briefing.pyの標準シャドー計画: risk = ATR×2.5, TP1 = +1R, TP2 = +2R
SHADOW_RISK = 2.5


def _row(ts: datetime, symbol: str = "USDJPY", close: float = 100.0, **overrides: object) -> dict:
    row = {
        "ts": ts.isoformat(),
        "symbol": symbol,
        "direction": "neutral",
        "conviction": 0,
        "composite": 0.0,
        "tech_score": 0.0,
        "news_score": 0.0,
        "close": close,
        "atr": 1.0,
        "data_quality": 0.9,
    }
    row.update(overrides)
    return row


def _guard_blocked_row(
    ts: datetime,
    *,
    symbol: str = "USDJPY",
    close: float = 100.0,
    direction: str = "long",
    conviction: int = 55,
    atr: float = 1.0,
    extra_blocked_gate: str | None = None,
    **overrides: object,
) -> dict:
    sign = 1.0 if direction == "long" else -1.0
    risk = atr * SHADOW_RISK
    gate_trace: list[dict[str, object]] = [{"gate": "expectancy_guard", "status": "blocked"}]
    if extra_blocked_gate:
        gate_trace.insert(0, {"gate": extra_blocked_gate, "status": "blocked"})
    # observed(非ブロック)のトレースは反実仮想の適格性に影響しないこと
    gate_trace.append({"gate": "liquidity", "status": "observed"})
    row = _row(ts, symbol=symbol, close=close, atr=atr)
    row.update(
        {
            "direction": "neutral",
            "conviction": 0,
            "analysis_direction": direction,
            "analysis_conviction": conviction,
            "gate_trace": gate_trace,
            "shadow_predictions": [
                {
                    "producer": "fusion_raw",
                    "direction": direction,
                    "eligible_for_scoring": True,
                    "stop": close - sign * risk,
                    "target1": close + sign * risk,
                    "target2": close + sign * risk * 2.0,
                    "target_policy": {"policy_id": "shadow-default-atr-v1"},
                }
            ],
        }
    )
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# journal.counterfactual_guard_entries: 合成の適格条件


def test_counterfactual_synthesis_restores_frozen_plan() -> None:
    entry = _guard_blocked_row(MONDAY + 8 * HOUR)
    synthesized = journal.counterfactual_guard_entries([entry])

    assert len(synthesized) == 1
    synth = synthesized[0]
    assert synth["direction"] == "long"
    assert synth["conviction"] == 55
    assert synth["stop"] == 97.5
    assert synth["target1"] == 102.5
    assert synth["target2"] == 105.0
    assert synth["target_policy"] == {"policy_id": "shadow-default-atr-v1"}
    assert synth[journal.COUNTERFACTUAL_ENTRY_KEY] is True
    # 元の行は破壊しない(推奨はneutralのまま)
    assert entry["direction"] == "neutral"
    assert entry["conviction"] == 0
    assert journal.COUNTERFACTUAL_ENTRY_KEY not in entry


def _tf_guard_blocked_row(
    ts: datetime,
    *,
    symbol: str = "USDJPY",
    close: float = 100.0,
    direction: str = "long",
    conviction: int = 55,
    atr: float = 1.0,
    timeframe: str = "1h",
    producer: str = "timeframe_raw",
    **overrides: object,
) -> dict:
    """時間足別判断のガード見送り行。shadow producer は timeframe_raw。"""
    sign = 1.0 if direction == "long" else -1.0
    risk = atr * SHADOW_RISK
    row = _row(ts, symbol=symbol, close=close, atr=atr)
    row.update(
        {
            "timeframe": timeframe,
            "direction": "neutral",
            "conviction": 0,
            "analysis_direction": direction,
            "analysis_conviction": conviction,
            "gate_trace": [
                {"gate": "expectancy_guard", "status": "blocked"},
                {"gate": "liquidity", "status": "observed"},
            ],
            "shadow_predictions": [
                {
                    "producer": producer,
                    "direction": direction,
                    "eligible_for_scoring": True,
                    "stop": close - sign * risk,
                    "target1": close + sign * risk,
                    "target2": close + sign * risk * 2.0,
                    "target_policy": {"policy_id": "shadow-default-atr-v1"},
                }
            ],
        }
    )
    row.update(overrides)
    return row


def test_counterfactual_synthesis_restores_timeframe_plan() -> None:
    # 時間足別判断(timeframe あり)は timeframe_raw シャドーから再構成する。
    # 融合路(fusion_raw)しか見ていなかった欠陥の回帰テスト。
    entry = _tf_guard_blocked_row(MONDAY + 8 * HOUR, timeframe="1h")
    synthesized = journal.counterfactual_guard_entries([entry])

    assert len(synthesized) == 1
    synth = synthesized[0]
    assert synth["direction"] == "long"
    assert synth["conviction"] == 55
    assert synth["timeframe"] == "1h"
    assert synth["stop"] == 97.5
    assert synth["target1"] == 102.5
    assert synth["target2"] == 105.0
    assert synth[journal.COUNTERFACTUAL_ENTRY_KEY] is True
    # 元の行は破壊しない
    assert entry["direction"] == "neutral"
    assert journal.COUNTERFACTUAL_ENTRY_KEY not in entry


def test_counterfactual_uses_path_matching_producer() -> None:
    # 各行は自分の経路の producer からのみ再構成する(別経路のシャドーは使わない)。
    # 時間足別行に fusion_raw しか無い/融合行に timeframe_raw しか無い場合は
    # fail-closed で対象外になる。
    tf_wrong = _tf_guard_blocked_row(MONDAY + 8 * HOUR, timeframe="1h", producer="fusion_raw")
    assert journal.counterfactual_guard_entries([tf_wrong]) == []

    fusion_wrong = _guard_blocked_row(MONDAY + 8 * HOUR)
    fusion_wrong["shadow_predictions"][0]["producer"] = "timeframe_raw"
    assert journal.counterfactual_guard_entries([fusion_wrong]) == []


def test_counterfactual_requires_guard_only_block() -> None:
    # event_window等のデータ・リスク由来ゲートが併発した行は、ガードが無くても
    # 見送っていた行なので反実仮想に含めない
    with_event = _guard_blocked_row(MONDAY + 8 * HOUR, extra_blocked_gate="event_window")
    assert journal.counterfactual_guard_entries([with_event]) == []
    # ガードにブロックされていない行(素のneutral)は対象外
    plain = _row(MONDAY + 8 * HOUR)
    assert journal.counterfactual_guard_entries([plain]) == []


def test_counterfactual_fails_closed_on_missing_records() -> None:
    base_ts = MONDAY + 8 * HOUR
    no_analysis = _guard_blocked_row(base_ts)
    no_analysis["analysis_direction"] = "neutral"
    no_predictions = _guard_blocked_row(base_ts)
    no_predictions["shadow_predictions"] = []
    not_eligible = _guard_blocked_row(base_ts)
    not_eligible["shadow_predictions"][0]["eligible_for_scoring"] = False
    mismatched = _guard_blocked_row(base_ts)
    mismatched["shadow_predictions"][0]["direction"] = "short"
    missing_level = _guard_blocked_row(base_ts)
    missing_level["shadow_predictions"][0]["stop"] = None

    rows = [no_analysis, no_predictions, not_eligible, mismatched, missing_level]
    assert journal.counterfactual_guard_entries(rows) == []


# ---------------------------------------------------------------------------
# trade_outcome: 反実仮想を実績と同じエンジンで採点し、ガード根拠を更新する


def _price_rows(ts: datetime, closes: list[float], symbol: str = "USDJPY") -> list[dict]:
    return [
        _row(ts + (i + 1) * timedelta(minutes=30), symbol=symbol, close=close)
        for i, close in enumerate(closes)
    ]


def test_trade_outcomes_score_counterfactual_with_frozen_levels() -> None:
    ts = MONDAY + 8 * HOUR
    rows = [
        _guard_blocked_row(ts),
        # +30分で100.2(タッチなし)、+1hで105.1(TP2到達)
        *_price_rows(ts, [100.2, 105.1, 105.1, 105.1, 105.1]),
    ]

    assert to.evaluate_trade_outcomes(rows, horizon_hours=2.0, tolerance_hours=0.5) == []

    outcomes = to.evaluate_trade_outcomes(
        rows, horizon_hours=2.0, tolerance_hours=0.5, include_guard_counterfactuals=True
    )
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.direction == "long"
    assert outcome.conviction == 55
    assert outcome.first_touch == "tp2"
    assert outcome.realized_r == 2.0
    assert to.COUNTERFACTUAL_QUALITY_FLAG in outcome.quality_flags
    # シャドー計画のpolicy_idはcandidate_id(承認済みTP/SL)ではないため、
    # by_target_policy集計には載せない(反実仮想を承認ポリシー実績に混ぜない)
    assert outcome.target_policy_id is None


def test_real_outcomes_identical_with_and_without_counterfactual_flag() -> None:
    ts = MONDAY + 8 * HOUR
    real = _row(
        ts,
        direction="long",
        conviction=60,
        stop=97.5,
        target1=102.5,
        target2=105.0,
    )
    rows = [real, *_price_rows(ts, [101.0, 102.6, 102.6, 102.6, 102.6])]

    baseline = to.evaluate_trade_outcomes(rows, horizon_hours=2.0, tolerance_hours=0.5)
    with_flag = to.evaluate_trade_outcomes(
        rows, horizon_hours=2.0, tolerance_hours=0.5, include_guard_counterfactuals=True
    )
    assert [outcome.to_dict() for outcome in baseline] == [
        outcome.to_dict() for outcome in with_flag
    ]
    assert baseline[0].first_touch == "tp1"
    assert to.COUNTERFACTUAL_QUALITY_FLAG not in baseline[0].quality_flags


def _losing_real_row(ts: datetime) -> list[dict]:
    decision = _row(
        ts,
        direction="long",
        conviction=55,
        stop=97.5,
        target1=102.5,
        target2=105.0,
    )
    return [decision, *_price_rows(ts, [99.0, 97.4, 97.4, 97.4, 97.4])]


def _winning_counterfactual_row(ts: datetime) -> list[dict]:
    return [
        _guard_blocked_row(ts),
        *_price_rows(ts, [100.2, 105.1, 105.1, 105.1, 105.1]),
    ]


def _losing_counterfactual_row(ts: datetime) -> list[dict]:
    return [
        _guard_blocked_row(ts),
        *_price_rows(ts, [99.0, 97.4, 97.4, 97.4, 97.4]),
    ]


def test_guard_evidence_releases_block_only_when_counterfactuals_turn_positive() -> None:
    """デッドロック回帰テスト: 実績のみだと根拠が凍結してblockが恒久化する。

    反実仮想を根拠に加えると、シャドー計画の期待Rが正に転じたときだけ
    blockが解除され、負のままなら見送りが続く(fail-closed維持)。
    """
    rows: list[dict] = []
    for i in range(8):
        rows.extend(_losing_real_row(MONDAY + i * 4 * HOUR))
    winning_start = MONDAY + 32 * HOUR
    for i in range(8):
        rows.extend(_winning_counterfactual_row(winning_start + i * 4 * HOUR))

    real_only = to.summarize_expectancy(
        to.evaluate_trade_outcomes(rows, horizon_hours=2.0, tolerance_hours=0.5),
        min_samples=6,
        group_min_samples=4,
    )
    real_adjustment = to.decision_adjustment(real_only, "USDJPY", "long", 55)
    assert real_adjustment.block is True

    with_counterfactuals = to.summarize_expectancy(
        to.evaluate_trade_outcomes(
            rows, horizon_hours=2.0, tolerance_hours=0.5, include_guard_counterfactuals=True
        ),
        min_samples=6,
        group_min_samples=4,
    )
    overall = with_counterfactuals["overall"]
    assert overall["tradable"] == 16
    assert overall["expectancy_r"] > 0
    assert to.counterfactual_outcome_count(with_counterfactuals) == 8
    released = to.decision_adjustment(with_counterfactuals, "USDJPY", "long", 55)
    assert released.block is False


def test_guard_evidence_stays_blocked_while_counterfactuals_lose() -> None:
    rows: list[dict] = []
    for i in range(8):
        rows.extend(_losing_real_row(MONDAY + i * 4 * HOUR))
    losing_start = MONDAY + 32 * HOUR
    for i in range(8):
        rows.extend(_losing_counterfactual_row(losing_start + i * 4 * HOUR))

    summary = to.summarize_expectancy(
        to.evaluate_trade_outcomes(
            rows, horizon_hours=2.0, tolerance_hours=0.5, include_guard_counterfactuals=True
        ),
        min_samples=6,
        group_min_samples=4,
    )
    adjustment = to.decision_adjustment(summary, "USDJPY", "long", 55)
    assert adjustment.block is True


def test_format_guard_evidence_note_only_mentions_counterfactuals_when_present() -> None:
    ts = MONDAY + 8 * HOUR
    rows = _winning_counterfactual_row(ts)
    summary = to.summarize_expectancy(
        to.evaluate_trade_outcomes(
            rows, horizon_hours=2.0, tolerance_hours=0.5, include_guard_counterfactuals=True
        ),
        min_samples=6,
        group_min_samples=4,
    )
    note = to.format_guard_evidence_note_ja(summary)
    assert "反実仮想" in note
    assert "1件" in note

    real_only = to.summarize_expectancy(
        to.evaluate_trade_outcomes(rows, horizon_hours=2.0, tolerance_hours=0.5),
        min_samples=6,
        group_min_samples=4,
    )
    assert to.format_guard_evidence_note_ja(real_only) == ""


# ---------------------------------------------------------------------------
# learning: 反実仮想の並走採点とプロファイル注記


def test_learning_scores_counterfactual_and_labels_profile() -> None:
    ts = MONDAY + 8 * HOUR
    entries = [
        _guard_blocked_row(ts, features={"rsi_1h": 50.0}),
        # 24h±2h後(火曜06:00〜10:00)に+1ATRの順行 → hit
        _row(ts + 23 * HOUR, close=101.0),
    ]

    assert learning.evaluate_history(entries) == []

    calls = learning.evaluate_history(entries, include_guard_counterfactuals=True)
    assert len(calls) == 1
    call = calls[0]
    assert call.outcome == "hit"
    assert call.direction == "long"
    assert call.conviction == 55
    assert call.counterfactual is True

    profile = learning.derive_profile(calls, now=ts + 30 * HOUR)
    assert profile.evaluated == 1
    assert profile.counterfactual_evaluated == 1
    assert any("反実仮想" in note for note in profile.notes_ja)


def test_profile_counterfactual_count_survives_save_and_load(tmp_path) -> None:
    profile = learning.LearnedProfile(
        generated_at=MONDAY.isoformat(),
        evaluated=5,
        hits=3,
        counterfactual_evaluated=2,
    )
    path = tmp_path / "profile.json"
    learning.save_profile(profile, path)
    loaded = learning.load_profile(path)
    assert loaded.counterfactual_evaluated == 2
