"""期待値ガード反実仮想採点(学習飢餓デッドロック解消)のテスト。ネットワーク不要。

再現する欠陥: 期待値ガードがblockした判断は direction=neutral で記録され、
採点対象から消える。ガード根拠のサンプルが永遠に増えず、blockが恒久化する
(2026-07-17〜の実機で観測: 根拠n=28のまま全新規判断が見送りに固定)。

修正の設計: expectancy_guard「単独」で見送りになった行を、producerが判断時に
凍結したpre_guard完全プランから復元する。推奨(direction)はneutralのまま
変えず、合成にraw scoreの再計算や別producerのshadowを使わない。
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
    mode: str = "fusion",
    producer: str = "fusion_raw",
    timeframe: str | None = None,
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
            "prediction_time": ts.isoformat(),
            "source_cutoff": (ts - timedelta(minutes=2)).isoformat(),
            "max_feature_available_time": (ts - timedelta(seconds=1)).isoformat(),
            "pit_eligible": True,
            "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
            "decision_id": f"decision:{symbol}:{ts.isoformat()}:{mode}",
            "mode": mode,
            "producer": producer,
            "producer_version": (
                journal.TIMEFRAME_PRODUCER_VERSION
                if mode == "per_timeframe"
                else journal.FUSION_PRODUCER_VERSION
            ),
            "input_context_id": f"context:{symbol}:{ts.isoformat()}",
            "source_record_ids": [f"source:{symbol}:{ts.isoformat()}"],
            **({"timeframe": timeframe} if timeframe is not None else {}),
            "direction": "neutral",
            "conviction": 0,
            "analysis_direction": direction,
            "analysis_conviction": conviction,
            "pre_guard_direction": direction,
            "pre_guard_conviction": conviction,
            "pre_guard_stop": close - sign * risk,
            "pre_guard_target1": close + sign * risk,
            "pre_guard_target2": close + sign * risk * 2.0,
            "pre_guard_target_policy": {
                "policy_id": "default-atr-v1",
                "target1_r": 1.0,
                "target2_r": 2.0,
            },
            "pre_guard_cost_model_id": "scanner-proxy-mid-diagnostic-v1",
            "pre_guard_execution_snapshot": {
                "entry_bid": close - 0.01,
                "entry_ask": close + 0.01,
                "quote_observed_at": ts.isoformat(),
                "quote_available_at": ts.isoformat(),
                "quote_source": "fixture",
                "quote_source_record_id": f"quote:{symbol}:{ts.isoformat()}",
                "planned_risk_distance": risk,
                "entry_spread_r": 0.02 / risk,
                "label_version": "net-r-v2",
                "label_provenance": "paper_quote_model",
                "cost_model_id": "scanner-proxy-mid-diagnostic-v1",
                "cost_model_version": "1",
                "cost_status": "diagnostic_only",
                "slippage_model_id": "zero-slippage-v1",
                "commission_model_id": "zero-commission-v1",
                "slippage_r": 0.0,
                "commission_r": 0.0,
                "financing_r": 0.0,
                "cost_quality_flags": ["diagnostic_only"],
                "canonical_net_label_input_eligible": False,
                "canonical_net_label_status": "ineligible",
            },
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
    # 別producerのshadowに矛盾する値があっても、凍結pre-guard以外を参照しない。
    entry["shadow_predictions"] = [
        {
            "producer": "timeframe_raw",
            "direction": "short",
            "eligible_for_scoring": True,
            "stop": 999.0,
            "target1": 1.0,
            "target2": 0.5,
        }
    ]
    synthesized = journal.counterfactual_guard_entries([entry])

    assert len(synthesized) == 1
    synth = synthesized[0]
    assert synth["direction"] == "long"
    assert synth["conviction"] == 55
    assert synth["stop"] == 97.5
    assert synth["target1"] == 102.5
    assert synth["target2"] == 105.0
    assert synth["target_policy"] == {
        "policy_id": "default-atr-v1",
        "target1_r": 1.0,
        "target2_r": 2.0,
    }
    assert synth["parent_decision_id"] == entry["decision_id"]
    assert synth["decision_id"] == f"{entry['decision_id']}:pre-guard"
    assert synth["execution_cost_r"] is None
    assert synth["canonical_net_label_status"] == "pending_canonical_outcome"
    assert synth[journal.COUNTERFACTUAL_ENTRY_KEY] is True
    # 元の行は破壊しない(推奨はneutralのまま)
    assert entry["direction"] == "neutral"
    assert entry["conviction"] == 0
    assert journal.COUNTERFACTUAL_ENTRY_KEY not in entry


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
    no_direction = _guard_blocked_row(base_ts)
    no_direction["pre_guard_direction"] = "neutral"
    no_policy = _guard_blocked_row(base_ts)
    no_policy["pre_guard_target_policy"] = {}
    no_execution = _guard_blocked_row(base_ts)
    no_execution["pre_guard_execution_snapshot"] = {}
    mismatched_cost = _guard_blocked_row(base_ts)
    mismatched_cost["pre_guard_execution_snapshot"]["cost_model_id"] = "other"
    missing_level = _guard_blocked_row(base_ts)
    missing_level["pre_guard_stop"] = None
    non_finite = _guard_blocked_row(base_ts)
    non_finite["pre_guard_conviction"] = float("nan")

    rows = [
        no_direction,
        no_policy,
        no_execution,
        mismatched_cost,
        missing_level,
        non_finite,
    ]
    assert journal.counterfactual_guard_entries(rows) == []


def test_counterfactual_uses_explicit_mode_and_producer_not_timeframe_presence() -> None:
    fusion_with_timeframe_marker = _guard_blocked_row(
        MONDAY + 8 * HOUR,
        mode="fusion",
        producer="fusion_raw",
        timeframe="fusion",
    )
    assert len(journal.counterfactual_guard_entries([fusion_with_timeframe_marker])) == 1

    timeframe_row = _guard_blocked_row(
        MONDAY + 9 * HOUR,
        mode="per_timeframe",
        producer="timeframe_raw",
        timeframe="1h",
    )
    assert len(journal.counterfactual_guard_entries([timeframe_row])) == 1

    unknown = _guard_blocked_row(
        MONDAY + 10 * HOUR,
        mode="unknown",
        producer="unknown",
        timeframe="1h",
    )
    assert journal.counterfactual_guard_entries([unknown]) == []


def test_counterfactual_restores_approved_target_policy() -> None:
    entry = _guard_blocked_row(MONDAY + 8 * HOUR)
    entry["pre_guard_target1"] = 101.875
    entry["pre_guard_target2"] = 103.75
    entry["pre_guard_target_policy"] = {
        "candidate_id": "approved-overall-tp",
        "target1_r": 0.75,
        "target2_r": 1.5,
    }

    synth = journal.counterfactual_guard_entries([entry])[0]
    assert synth["target1"] == 101.875
    assert synth["target2"] == 103.75
    assert synth["target_policy"]["candidate_id"] == "approved-overall-tp"


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
    assert outcome.realized_net_r is None
    assert to.COUNTERFACTUAL_QUALITY_FLAG in outcome.quality_flags
    # default policy_idはcandidate_id(承認済みTP/SL)ではないため、
    # by_target_policy集計には載せない。
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


def test_legacy_gross_counterfactuals_cannot_drive_guard_release() -> None:
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
    assert real_adjustment.action == "sample_guard"
    assert real_adjustment.block is False

    with_counterfactuals = to.summarize_expectancy(
        to.evaluate_trade_outcomes(
            rows, horizon_hours=2.0, tolerance_hours=0.5, include_guard_counterfactuals=True
        ),
        min_samples=6,
        group_min_samples=4,
    )
    overall = with_counterfactuals["overall"]
    assert overall["tradable"] == 16
    assert overall["gross_expectancy_r"] > 0
    assert overall["net_expectancy_r"] is None
    assert to.counterfactual_outcome_count(with_counterfactuals) == 8
    released = to.decision_adjustment(with_counterfactuals, "USDJPY", "long", 55)
    assert released.action == "sample_guard"
    assert released.block is False


def test_legacy_losing_counterfactuals_remain_evaluation_unavailable() -> None:
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
    assert adjustment.action == "sample_guard"
    assert adjustment.block is False


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
