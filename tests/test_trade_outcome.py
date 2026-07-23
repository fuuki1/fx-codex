"""MFE/MAE/TP/SL期待値監査のテスト。ネットワーク不要。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, UTC
import json

from fx_briefing import (
    approve_trade_candidate_cli,
    check_trade_outcome_health_cli,
    retest_trade_variants_cli,
    score_trade_outcomes_cli,
)
from fx_intel import briefing, journal, trade_outcome as to
from fx_intel.evaluation_labels import (
    DEFAULT_COMMISSION_MODEL_ID,
    DEFAULT_COST_MODEL_ID,
    DEFAULT_COST_MODEL_VERSION,
    DEFAULT_COST_STATUS,
    DEFAULT_SLIPPAGE_MODEL_ID,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
)
from fx_intel.sentiment import CurrencySentiment
from fx_intel.technicals import IntervalView, PairTechnicals

NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)
DAY = timedelta(hours=24)


def _outcome(
    r_multiple: float,
    *,
    symbol: str = "USDJPY",
    direction: str = "long",
    quality: float = 0.70,
    conviction: int = 60,
    target_policy_id: str | None = None,
    index: int = 0,
    net_r: float | None = None,
) -> to.TradeOutcome:
    prediction = NOW + timedelta(days=index * 2)
    realized_net_r = r_multiple - 0.2 if net_r is None else net_r
    return to.TradeOutcome(
        symbol=symbol,
        direction=direction,
        ts=prediction.isoformat(),
        horizon_hours=24.0,
        conviction=conviction,
        data_quality=0.90,
        timeframe="1h",
        decision_id=f"decision-{symbol}-{direction}-{index}",
        entry=100.0,
        stop=99.0 if direction == "long" else 101.0,
        target1=101.0 if direction == "long" else 99.0,
        target2=102.0 if direction == "long" else 98.0,
        target_policy_id=target_policy_id,
        atr=1.0,
        risk_distance=1.0,
        terminal_price=100.0 + r_multiple,
        terminal_r=r_multiple,
        mfe=max(r_multiple, 0.2),
        mae=max(-r_multiple, 0.2),
        mfe_r=max(r_multiple, 0.2),
        mae_r=max(-r_multiple, 0.2),
        tp1_hit=r_multiple >= 1.0,
        tp2_hit=r_multiple >= 2.0,
        sl_hit=r_multiple <= -1.0,
        first_touch=(
            "tp2"
            if r_multiple >= 2.0
            else "tp1" if r_multiple >= 1.0 else "sl" if r_multiple <= -1.0 else "none"
        ),
        realized_r=r_multiple,
        realized_net_r=realized_net_r,
        net_label_eligible=True,
        label_version=NET_LABEL_VERSION,
        label_provenance=NET_LABEL_PROVENANCE,
        entry_bid=99.9,
        entry_ask=100.1,
        quote_realized_r=realized_net_r,
        entry_spread_r=0.2,
        slippage_r=0.0,
        commission_r=0.0,
        financing_r=0.0,
        additional_cost_r=0.0,
        execution_cost_r=r_multiple - realized_net_r,
        cost_model_id=DEFAULT_COST_MODEL_ID,
        cost_model_version=DEFAULT_COST_MODEL_VERSION,
        cost_status=DEFAULT_COST_STATUS,
        entry_quote_source="fixture",
        spread_source="fixture",
        slippage_model_id=DEFAULT_SLIPPAGE_MODEL_ID,
        commission_model_id=DEFAULT_COMMISSION_MODEL_ID,
        cost_quality_flags=(),
        path_points=6,
        path_start=(prediction + timedelta(hours=4)).isoformat(),
        path_end=(prediction + DAY).isoformat(),
        path_coverage=1.0,
        path_quality=quality,
        quality_flags=("close_only_path",),
    )


def _entry(ts: datetime, symbol: str, close: float, **overrides: object) -> dict:
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


def _write_jsonl(path, rows: list[dict]) -> None:
    eligible_rows = []
    for raw in rows:
        row = dict(raw)
        prediction = datetime.fromisoformat(str(row["ts"]))
        row.update(
            {
                "prediction_time": prediction.isoformat(),
                "source_cutoff": (prediction - timedelta(minutes=2)).isoformat(),
                "max_feature_available_time": (prediction - timedelta(seconds=1)).isoformat(),
                "pit_eligible": True,
                "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
                "decision_id": (
                    f"decision:{row['symbol']}:{prediction.isoformat()}:"
                    f"{row.get('direction', 'neutral')}"
                ),
                "mode": "fusion",
                "producer": journal.FUSION_PRODUCER,
                "producer_version": journal.FUSION_PRODUCER_VERSION,
                "input_context_id": f"context:{row['symbol']}:{prediction.isoformat()}",
                "source_record_ids": [f"source:{row['symbol']}:{prediction.isoformat()}"],
            }
        )
        eligible_rows.append(row)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in eligible_rows) + "\n",
        encoding="utf-8",
    )


def _net_tp_evidence(
    *,
    baseline: float,
    candidate: float,
) -> dict[str, object]:
    return {
        "label_version": NET_LABEL_VERSION,
        "label_provenance": NET_LABEL_PROVENANCE,
        "cost_model_id": DEFAULT_COST_MODEL_ID,
        "net_label_samples": to.MIN_EXPECTANCY_SAMPLES,
        "net_label_coverage": 1.0,
        "baseline_net_expectancy_r": baseline,
        "candidate_net_expectancy_r": candidate,
        "delta_net_expectancy_r": candidate - baseline,
        "trial_count": 1,
        "trial_sharpes": [0.0],
    }


def _prospective_outcomes(
    candidate: to.TradeImprovementCandidate,
    *,
    net_values: list[float] | None = None,
    gross_values: list[float] | None = None,
    count: int = to.PROSPECTIVE_MIN_EFFECTIVE_SAMPLES,
) -> list[to.TradeOutcome]:
    values = net_values or [0.2 if index % 2 == 0 else 0.6 for index in range(count)]
    gross = gross_values or [value + 0.2 for value in values]
    assert len(values) == len(gross)
    symbols = ("USDJPY", "EURUSD", "GBPUSD")
    proposed = candidate.proposed_change
    scope = str(proposed.get("scope") or candidate.scope)
    key = str(proposed.get("key") or candidate.key)
    outcomes: list[to.TradeOutcome] = []
    for index, net_r in enumerate(values, start=1):
        symbol = symbols[(index - 1) % len(symbols)]
        direction = "long"
        if scope in {"by_symbol", "通貨ペア"}:
            symbol = key
        elif scope in {"by_symbol_direction", "通貨ペア×方向"}:
            symbol, direction = key.split(":", maxsplit=1)
        elif scope in {"by_direction", "方向"}:
            direction = key
        outcomes.append(
            _outcome(
                gross[index - 1],
                symbol=symbol,
                direction=direction,
                target_policy_id=(
                    candidate.candidate_id
                    if candidate.action_type == "tp_sl_variant_paper_test"
                    else None
                ),
                index=index,
                net_r=net_r,
            )
        )
    return outcomes


def _advance_registry_to_review(
    registry: dict,
    candidates: list[to.TradeImprovementCandidate],
    *,
    outcomes: list[to.TradeOutcome] | None = None,
) -> tuple[dict, list[to.TradeOutcome]]:
    evidence = outcomes or [
        outcome for candidate in candidates for outcome in _prospective_outcomes(candidate)
    ]
    latest = max(datetime.fromisoformat(outcome.path_end) for outcome in evidence)
    updated = to.update_improvement_registry(
        registry,
        candidates,
        now=latest + timedelta(hours=1),
        prospective_outcomes=evidence,
    )
    return updated, evidence


def _bullish_tech() -> PairTechnicals:
    tech = PairTechnicals(symbol="USDJPY", fast_window=20, slow_window=100)
    tech.views["1h"] = IntervalView(
        interval="1h",
        recommendation="BUY",
        buy=10,
        sell=2,
        neutral=2,
        close=150.0,
        rsi=55.0,
        atr=0.5,
        sma_fast=150.2,
        sma_slow=149.5,
    )
    tech.views["4h"] = IntervalView(
        interval="4h",
        recommendation="BUY",
        buy=8,
        sell=1,
        neutral=2,
        close=150.0,
    )
    return tech


def test_expectancy_findings_flag_negative_expectancy_cells() -> None:
    outcomes = [_outcome(-1.0, index=index) for index in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)

    findings = to.expectancy_findings(summary)

    assert any(
        finding["label"] == "全体" and finding["severity"] == "block" for finding in findings
    )
    assert summary["overall"]["sample_ok"] is True
    assert summary["overall"]["gross_expectancy_r"] == -1.0
    assert summary["overall"]["net_expectancy_r"] == -1.2


def test_expectancy_findings_mark_sample_guard() -> None:
    outcomes = [_outcome(1.0, index=index) for index in range(3)]
    summary = to.summarize_expectancy(outcomes, min_samples=5, group_min_samples=5)

    findings = to.expectancy_findings(summary)

    assert any(finding["severity"] == "sample_guard" for finding in findings)
    assert summary["overall"]["sample_ok"] is False


def test_decision_adjustment_blocks_matching_symbol_direction() -> None:
    outcomes = [
        _outcome(-1.0, symbol="USDJPY", direction="long", index=index) for index in range(12)
    ]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)

    adjustment = to.decision_adjustment(summary, "USDJPY", "long", 60)

    assert adjustment.action == "block"
    assert adjustment.block is True
    assert adjustment.factor == to.EXPECTANCY_BLOCK_FACTOR
    assert adjustment.matched_scope == "通貨ペア×方向"


def test_gross_positive_but_canonical_net_negative_is_blocked() -> None:
    outcomes = [
        _outcome(0.1, net_r=-0.1, index=index) for index in range(to.MIN_EXPECTANCY_SAMPLES)
    ]
    summary = to.summarize_expectancy(
        outcomes,
        min_samples=to.MIN_EXPECTANCY_SAMPLES,
        group_min_samples=to.MIN_GROUP_EXPECTANCY_SAMPLES,
    )

    assert summary["overall"]["gross_expectancy_r"] == 0.1
    assert summary["overall"]["net_expectancy_r"] == -0.1
    adjustment = to.decision_adjustment(summary, "USDJPY", "long", 60)
    assert adjustment.block is True
    assert adjustment.expectancy_r == -0.1


def test_decision_adjustment_keeps_sample_guard_non_blocking() -> None:
    outcomes = [_outcome(1.0, symbol="USDJPY", direction="long", index=index) for index in range(3)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)

    adjustment = to.decision_adjustment(summary, "USDJPY", "long", 60)

    assert adjustment.action == "sample_guard"
    assert adjustment.block is False
    assert adjustment.factor == 1.0


def test_build_trade_plan_expectancy_guard_can_block_to_neutral() -> None:
    scores = {
        "USD": CurrencySentiment("USD", score=0.5),
        "JPY": CurrencySentiment("JPY", score=-0.3),
    }
    plan = briefing.build_trade_plan(
        "USDJPY",
        _bullish_tech(),
        scores,
        [],
        [],
        now=NOW,
        expectancy_adjuster=lambda _symbol, _direction, _conviction: (
            to.EXPECTANCY_BLOCK_FACTOR,
            "期待R -1.00Rが非正。新規エントリーは見送り",
            True,
        ),
        target_r_adjuster=lambda _symbol, _direction, _conviction: (
            0.75,
            1.5,
            "承認済み候補",
            {
                "candidate_id": "approved-overall-tp",
                "target1_r": 0.75,
                "target2_r": 1.5,
            },
        ),
    )

    assert plan.direction == "neutral"
    assert plan.conviction == 0
    assert plan.stop is None
    assert plan.pre_guard_direction == "long"
    assert plan.pre_guard_conviction > 0
    assert plan.pre_guard_stop is not None
    assert plan.pre_guard_target_policy["candidate_id"] == "approved-overall-tp"
    assert any("期待値ガード" in warning for warning in plan.warnings)


def test_target_policy_is_written_to_journal_and_scored_by_policy(tmp_path) -> None:
    policy = {
        "candidate_id": "approved-overall-tp",
        "scope": "overall",
        "key": "",
        "target1_r": 0.75,
        "target2_r": 1.5,
    }
    scores = {
        "USD": CurrencySentiment("USD", score=0.5),
        "JPY": CurrencySentiment("JPY", score=-0.3),
    }
    plan = briefing.build_trade_plan(
        "USDJPY",
        _bullish_tech(),
        scores,
        [],
        [],
        now=NOW,
        target_r_adjuster=lambda _symbol, _direction, _conviction: (
            0.75,
            1.5,
            "承認済み候補",
            policy,
        ),
    )
    path = tmp_path / "journal.jsonl"

    journal.append_plans(path, [plan], now=NOW)
    rows = list(journal.read_entries(path))
    rows.extend(
        [
            _entry(NOW + timedelta(hours=8), "USDJPY", 150.2),
            _entry(NOW + timedelta(hours=16), "USDJPY", 150.4),
            _entry(NOW + DAY, "USDJPY", 151.0),
        ]
    )
    outcomes = to.evaluate_trade_outcomes(rows)
    summary = to.summarize_expectancy(outcomes, min_samples=1, group_min_samples=1)

    assert rows[0]["target_policy"]["candidate_id"] == "approved-overall-tp"
    assert outcomes[0].target_policy_id == "approved-overall-tp"
    assert "approved-overall-tp" in summary["by_target_policy"]
    assert summary["by_target_policy"]["approved-overall-tp"]["evaluated"] == 1


def test_improvement_registry_tracks_active_ready_and_resolved_candidates() -> None:
    outcomes = [_outcome(-1.0, index=index) for index in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)
    candidate = next(
        candidate
        for candidate in to.improvement_candidates(summary)
        if candidate.priority == "high"
    )

    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    record = registry["candidates"][candidate.candidate_id]
    assert record["status"] == "active"
    assert record["stage"] == "prospective_collecting"
    assert record["seen_count"] == 1
    assert record["candidate_created_at"] == NOW.isoformat()
    assert record["prospective_start"] == NOW.isoformat()
    assert record["prospective_metrics"]["effective_samples"] == 0
    assert registry["events"][-1]["event_type"] == "detected"

    original_hash = record["prospective_dataset_hash"]
    evidence = _prospective_outcomes(candidate)
    registry = to.update_improvement_registry(
        registry,
        [candidate],
        now=NOW + timedelta(hours=1),
        prospective_outcomes=evidence,
    )
    record = registry["candidates"][candidate.candidate_id]
    assert record["stage"] == "prospective_collecting"
    assert record["seen_count"] == 2
    assert record["prospective_dataset_hash"] == original_hash
    assert record["prospective_metrics"]["effective_samples"] == 0
    assert registry["events"][-1]["event_type"] == "detected"

    registry, evidence = _advance_registry_to_review(
        registry,
        [candidate],
        outcomes=evidence,
    )
    record = registry["candidates"][candidate.candidate_id]
    assert record["stage"] == "ready_for_review"
    assert record["prospective_metrics"]["effective_samples"] == len(evidence)
    assert registry["events"][-1]["event_type"] == "stage_changed"

    evidence_hash = record["prospective_dataset_hash"]
    registry = to.update_improvement_registry(
        registry,
        [candidate],
        now=datetime.fromisoformat(evidence[-1].path_end) + timedelta(hours=2),
        prospective_outcomes=evidence,
    )
    record = registry["candidates"][candidate.candidate_id]
    assert record["stage"] == "ready_for_review"
    assert record["prospective_dataset_hash"] == evidence_hash
    assert record["prospective_metrics"]["effective_samples"] == len(evidence)

    registry = to.update_improvement_registry(
        registry,
        [],
        now=NOW + timedelta(days=50),
    )
    record = registry["candidates"][candidate.candidate_id]
    assert record["status"] == "resolved"
    assert record["stage"] == "resolved"
    assert registry["resolved_count"] == 1
    assert registry["events"][-1]["event_type"] == "resolved"


def test_prospective_registry_rejects_positive_gross_with_negative_net() -> None:
    summary = to.summarize_expectancy(
        [_outcome(-1.0, index=index) for index in range(20)],
        min_samples=20,
        group_min_samples=12,
    )
    candidate = to.improvement_candidates(summary)[0]
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    evidence = _prospective_outcomes(
        candidate,
        net_values=[-0.1 if index % 2 == 0 else -0.3 for index in range(20)],
        gross_values=[0.5 for _ in range(20)],
    )

    registry, _ = _advance_registry_to_review(
        registry,
        [candidate],
        outcomes=evidence,
    )
    record = registry["candidates"][candidate.candidate_id]

    assert record["stage"] == "prospective_collecting"
    assert record["prospective_metrics"]["net_expectancy_r"] < 0
    assert "nonpositive_net_expectancy_lcb" in record["prospective_metrics"]["blocking_reasons"]


def test_prospective_registry_rejects_mixed_label_versions() -> None:
    summary = to.summarize_expectancy(
        [_outcome(-1.0, index=index) for index in range(20)],
        min_samples=20,
        group_min_samples=12,
    )
    candidate = to.improvement_candidates(summary)[0]
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    evidence = _prospective_outcomes(candidate)
    evidence[-1] = replace(evidence[-1], label_version="net-r-mixed")

    registry, _ = _advance_registry_to_review(
        registry,
        [candidate],
        outcomes=evidence,
    )
    metrics = registry["candidates"][candidate.candidate_id]["prospective_metrics"]

    assert registry["candidates"][candidate.candidate_id]["stage"] == "prospective_collecting"
    assert metrics["contract_consistent"] is False
    assert "mixed_label_or_cost_contract" in metrics["blocking_reasons"]
    assert metrics["net_label_coverage"] < 1.0


def test_prospective_registry_rejects_mixed_cost_models() -> None:
    summary = to.summarize_expectancy(
        [_outcome(-1.0, index=index) for index in range(20)],
        min_samples=20,
        group_min_samples=12,
    )
    candidate = to.improvement_candidates(summary)[0]
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    evidence = _prospective_outcomes(candidate)
    evidence[-1] = replace(
        evidence[-1],
        cost_model_id="ibkr-paper-executable-quotes-zero-slippage-v1",
        entry_quote_source="ibkr_paper_snapshot",
        spread_source="ibkr_paper_snapshot",
    )

    registry, _ = _advance_registry_to_review(
        registry,
        [candidate],
        outcomes=evidence,
    )
    metrics = registry["candidates"][candidate.candidate_id]["prospective_metrics"]

    assert registry["candidates"][candidate.candidate_id]["stage"] == "prospective_collecting"
    assert metrics["contract_consistent"] is False
    assert metrics["net_label_coverage"] == 1.0
    assert "mixed_label_or_cost_contract" in metrics["blocking_reasons"]


def test_prospective_registry_expires_without_new_outcomes() -> None:
    summary = to.summarize_expectancy(
        [_outcome(-1.0, index=index) for index in range(20)],
        min_samples=20,
        group_min_samples=12,
    )
    candidate = to.improvement_candidates(summary)[0]
    registry = to.update_improvement_registry(None, [candidate], now=NOW)

    registry = to.update_improvement_registry(
        registry,
        [candidate],
        now=NOW + timedelta(days=to.PROSPECTIVE_VALID_DAYS, seconds=1),
    )
    record = registry["candidates"][candidate.candidate_id]

    assert record["stage"] == "expired"
    assert record["expired_at"]
    assert "insufficient_effective_samples" in record["prospective_metrics"]["blocking_reasons"]


def test_improvement_registry_preserves_unmanaged_candidate_types() -> None:
    outcomes = [_outcome(-1.0, index=index) for index in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)
    expectancy_candidate = to.improvement_candidates(summary)[0]
    registry = to.update_improvement_registry(None, [expectancy_candidate], now=NOW)

    variant_report = {
        "baseline": {"overall": {"expectancy_r": -1.0, "sample_ok": True}},
        "variants": [
            {
                "variant_id": "tp1-0.75-tp2-1.5",
                "target1_r": 0.75,
                "target2_r": 1.5,
                "tradable": 20,
                "sample_ok": True,
                "expectancy_r": 0.75,
                "profit_factor_r": float("inf"),
                "delta_expectancy_r": 1.75,
                "recommendation": "paper_test",
            }
        ],
    }
    variant_candidates = to.variant_improvement_candidates(variant_report)
    registry = to.update_improvement_registry(
        registry,
        variant_candidates,
        now=NOW + timedelta(hours=1),
        managed_action_types=to.VARIANT_CANDIDATE_ACTION_TYPES,
    )

    assert variant_candidates == []
    assert registry["candidates"][expectancy_candidate.candidate_id]["status"] == "active"


def test_variant_candidate_freezes_all_trial_sharpes_for_prospective_dsr() -> None:
    canonical = {
        "tradable": to.MIN_EXPECTANCY_SAMPLES,
        "sample_ok": True,
        "net_label_samples": to.MIN_EXPECTANCY_SAMPLES,
        "net_label_coverage": 1.0,
        "label_version": NET_LABEL_VERSION,
        "label_provenance": NET_LABEL_PROVENANCE,
        "cost_model_id": DEFAULT_COST_MODEL_ID,
    }
    report = {
        "baseline": {"overall": {"net_expectancy_r": -0.2}},
        "variants": [
            {
                **canonical,
                "variant_id": "selected",
                "target1_r": 0.75,
                "target2_r": 1.5,
                "net_expectancy_r": 0.3,
                "delta_net_expectancy_r": 0.5,
                "net_profit_factor_r": 1.4,
                "net_sharpe_per_period": 0.6,
                "recommendation": "paper_test",
            },
            {
                **canonical,
                "variant_id": "rejected",
                "target1_r": 1.0,
                "target2_r": 2.0,
                "net_expectancy_r": -0.1,
                "delta_net_expectancy_r": 0.1,
                "net_profit_factor_r": 0.9,
                "net_sharpe_per_period": -0.2,
                "recommendation": "reject",
            },
        ],
    }

    candidate = to.variant_improvement_candidates(report)[0]

    assert candidate.proposed_change["trial_count"] == 2
    assert candidate.proposed_change["trial_sharpes"] == [0.6, -0.2]


def test_improvement_candidate_approval_requires_prospective_review_and_is_preserved() -> None:
    outcomes = [_outcome(-1.0, index=index) for index in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)
    candidate = to.improvement_candidates(summary)[0]
    registry = to.update_improvement_registry(None, [candidate], now=NOW)

    unchanged, not_ready = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        actor="tester",
        now=NOW,
    )
    assert not_ready["status"] == "not_ready"
    assert unchanged["candidates"][candidate.candidate_id]["stage"] == "prospective_collecting"

    registry, evidence = _advance_registry_to_review(registry, [candidate])
    approval_time = datetime.fromisoformat(evidence[-1].path_end) + timedelta(hours=2)
    approved, result = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        actor="tester",
        note="paper検証OK",
        now=approval_time,
    )
    assert result["status"] == "approved"
    assert approved["approved_count"] == 1
    record = approved["candidates"][candidate.candidate_id]
    assert record["stage"] == "approved"
    assert record["approved_by"] == "tester"
    assert record["approval_note"] == "paper検証OK"
    assert approved["events"][-1]["event_type"] == "approved"
    assert approved["events"][-1]["actor"] == "tester"

    refreshed = to.update_improvement_registry(
        approved,
        [candidate],
        now=approval_time + timedelta(hours=1),
        prospective_outcomes=evidence,
    )
    assert refreshed["candidates"][candidate.candidate_id]["stage"] == "approved"
    assert refreshed["approved_count"] == 1


def test_tp_sl_candidate_approval_requires_expectancy_improvement_evidence() -> None:
    candidate = to.TradeImprovementCandidate(
        "weak-tp-candidate",
        "TP/SL候補",
        "overall",
        "high",
        "tp_sl_variant_paper_test",
        "TP1=0.75R / TP2=1.5Rをpaper検証",
        "改善根拠なし",
        {
            "target1_r": 0.75,
            "target2_r": 1.5,
            "scope": "overall",
            "key": "",
            "trial_count": 1,
            "trial_sharpes": [0.0],
        },
        "paper",
        "approval",
    )
    registry = to.update_improvement_registry(
        None, [candidate], now=NOW, data_contract=journal.FUSION_PIT_DATA_CONTRACT
    )
    registry, evidence = _advance_registry_to_review(registry, [candidate])

    unchanged, result = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        actor="tester",
        now=datetime.fromisoformat(evidence[-1].path_end) + timedelta(hours=2),
    )

    assert result["status"] == "not_improving"
    assert unchanged["candidates"][candidate.candidate_id]["stage"] == "ready_for_review"


def test_select_approved_target_policy_prefers_specific_approved_candidate() -> None:
    overall_candidate = to.TradeImprovementCandidate(
        "overall-tp",
        "TP/SL候補",
        "tp1-0.8-tp2-1.6",
        "high",
        "tp_sl_variant_paper_test",
        "全体TP候補",
        "overall",
        {
            "target1_r": 0.8,
            "target2_r": 1.6,
            "scope": "overall",
            "key": "",
            **_net_tp_evidence(baseline=-1.0, candidate=0.3),
            "min_expected_improvement_r": to.MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
        },
        "paper",
        "approval",
    )
    cell_candidate = to.TradeImprovementCandidate(
        "usdjpy-long-tp",
        "TP/SL候補 通貨ペア×方向",
        "USDJPY:long",
        "high",
        "tp_sl_variant_paper_test",
        "USDJPY long TP候補",
        "cell",
        {
            "target1_r": 0.75,
            "target2_r": 1.5,
            "scope": "by_symbol_direction",
            "key": "USDJPY:long",
            **_net_tp_evidence(baseline=-1.0, candidate=0.4),
            "min_expected_improvement_r": to.MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
        },
        "paper",
        "approval",
    )
    registry = to.update_improvement_registry(
        None,
        [overall_candidate, cell_candidate],
        now=NOW,
    )
    registry, evidence = _advance_registry_to_review(
        registry,
        [overall_candidate, cell_candidate],
    )
    approval_time = datetime.fromisoformat(evidence[-1].path_end) + timedelta(hours=2)
    registry, _ = to.set_improvement_candidate_approval(
        registry,
        overall_candidate.candidate_id,
        "approved",
        now=approval_time,
    )
    registry, _ = to.set_improvement_candidate_approval(
        registry,
        cell_candidate.candidate_id,
        "approved",
        now=approval_time + timedelta(hours=1),
    )

    policy = to.select_approved_target_policy(
        registry,
        "USDJPY",
        "long",
        60,
        now=approval_time + timedelta(hours=1),
    )
    fallback = to.select_approved_target_policy(
        registry,
        "EURUSD",
        "long",
        60,
        now=approval_time + timedelta(hours=1),
    )

    assert policy is not None
    assert policy.candidate_id == cell_candidate.candidate_id
    assert policy.target1_r == 0.75
    assert fallback is not None
    assert fallback.candidate_id == overall_candidate.candidate_id


def test_auto_pause_underperforming_approved_target_policy() -> None:
    candidate = to.TradeImprovementCandidate(
        "approved-overall-tp",
        "TP/SL候補",
        "overall",
        "high",
        "tp_sl_variant_paper_test",
        "TP1=0.75R / TP2=1.5Rをpaper検証",
        "期待R改善",
        {
            "target1_r": 0.75,
            "target2_r": 1.5,
            "scope": "overall",
            "key": "",
            **_net_tp_evidence(baseline=-1.0, candidate=0.75),
            "min_expected_improvement_r": to.MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
        },
        "paper",
        "approval",
    )
    registry = to.update_improvement_registry(
        None, [candidate], now=NOW, data_contract=journal.FUSION_PIT_DATA_CONTRACT
    )
    registry, evidence = _advance_registry_to_review(registry, [candidate])
    approval_time = datetime.fromisoformat(evidence[-1].path_end) + timedelta(hours=2)
    registry, result = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        now=approval_time,
    )
    outcomes = [
        _outcome(-1.0, target_policy_id=candidate.candidate_id, index=index)
        for index in range(to.MIN_GROUP_EXPECTANCY_SAMPLES)
    ]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)

    paused_registry, paused = to.auto_pause_underperforming_approved_policies(
        registry,
        summary,
        now=approval_time + timedelta(hours=1),
    )

    assert result["status"] == "approved"
    assert paused[0]["candidate_id"] == candidate.candidate_id
    record = paused_registry["candidates"][candidate.candidate_id]
    assert record["stage"] == "auto_paused"
    assert paused_registry["auto_paused_count"] == 1
    assert paused_registry["events"][-1]["event_type"] == "auto_paused"
    assert (
        paused_registry["events"][-1]["details"]["net_label_samples"]
        == to.MIN_GROUP_EXPECTANCY_SAMPLES
    )
    assert (
        to.select_approved_target_policy(
            paused_registry,
            "USDJPY",
            "long",
            60,
        )
        is None
    )


def test_resume_auto_paused_candidate_restores_approved_policy() -> None:
    candidate = to.TradeImprovementCandidate(
        "approved-overall-tp",
        "TP/SL候補",
        "overall",
        "high",
        "tp_sl_variant_paper_test",
        "TP1=0.75R / TP2=1.5Rをpaper検証",
        "期待R改善",
        {
            "target1_r": 0.75,
            "target2_r": 1.5,
            "scope": "overall",
            "key": "",
            **_net_tp_evidence(baseline=-1.0, candidate=0.75),
            "min_expected_improvement_r": to.MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
        },
        "paper",
        "approval",
    )
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    registry, evidence = _advance_registry_to_review(registry, [candidate])
    approval_time = datetime.fromisoformat(evidence[-1].path_end) + timedelta(hours=2)
    registry, _ = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        now=approval_time,
    )
    summary = to.summarize_expectancy(
        [
            _outcome(-1.0, target_policy_id=candidate.candidate_id, index=index)
            for index in range(to.MIN_GROUP_EXPECTANCY_SAMPLES)
        ],
        min_samples=20,
        group_min_samples=12,
    )
    paused_registry, _ = to.auto_pause_underperforming_approved_policies(
        registry,
        summary,
        now=approval_time + timedelta(hours=1),
    )

    resumed_registry, result = to.set_improvement_candidate_approval(
        paused_registry,
        candidate.candidate_id,
        "resumed",
        actor="ops",
        note="手動で再開",
        now=approval_time + timedelta(hours=2),
    )
    policy = to.select_approved_target_policy(
        resumed_registry,
        "USDJPY",
        "long",
        60,
        now=approval_time + timedelta(hours=2),
    )

    assert result["status"] == "resumed"
    record = resumed_registry["candidates"][candidate.candidate_id]
    assert record["stage"] == "approved"
    assert record["resumed_by"] == "ops"
    assert resumed_registry["approved_count"] == 1
    assert resumed_registry["events"][-1]["event_type"] == "resumed"
    assert resumed_registry["events"][-1]["actor"] == "ops"
    assert policy is not None
    assert policy.candidate_id == candidate.candidate_id

    _, invalid = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "resumed",
        now=approval_time + timedelta(hours=3),
    )
    assert invalid["status"] == "not_paused"


def test_monitoring_snapshot_includes_health_and_ready_candidates() -> None:
    outcomes = [_outcome(-1.0, index=index) for index in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)
    candidate = to.improvement_candidates(summary)[0]
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    registry, evidence = _advance_registry_to_review(registry, [candidate])

    snapshot = to.build_monitoring_snapshot(
        summary,
        registry=registry,
        now=datetime.fromisoformat(evidence[-1].path_end) + timedelta(hours=1),
    )

    assert snapshot["schema"] == 2
    assert snapshot["status"] == to.STATUS_FAIL
    assert snapshot["registry"]["ready_for_review_count"] == 1
    ready = snapshot["registry"]["ready_for_review"][0]
    assert ready["candidate_id"] == candidate.candidate_id
    assert ready["prospective_metrics"]["effective_samples"] == len(evidence)
    assert ready["prospective_end"]
    assert snapshot["recent_events"][-1]["event_type"] == "stage_changed"
    assert any(alert["type"] == "ready_for_review" for alert in snapshot["alerts"])


def test_approve_trade_candidate_cli_updates_registry(tmp_path) -> None:
    outcomes = [_outcome(-1.0, index=index) for index in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)
    candidate = to.improvement_candidates(summary)[0]
    registry = to.update_improvement_registry(
        None, [candidate], now=NOW, data_contract=journal.FUSION_PIT_DATA_CONTRACT
    )
    registry, evidence = _advance_registry_to_review(registry, [candidate])
    approval_time = datetime.fromisoformat(evidence[-1].path_end) + timedelta(hours=2)
    registry_path = tmp_path / "registry.json"
    to.save_improvement_registry(registry, registry_path)

    exit_code = approve_trade_candidate_cli(
        registry_path,
        candidate.candidate_id,
        decision="approved",
        actor="tester",
        note="paper OK",
        now=approval_time,
    )
    payload = json.loads(registry_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["approved_count"] == 1
    assert payload["candidates"][candidate.candidate_id]["stage"] == "approved"
    assert payload["candidates"][candidate.candidate_id]["approved_by"] == "tester"


def test_resume_trade_candidate_cli_updates_auto_paused_registry(tmp_path) -> None:
    candidate = to.TradeImprovementCandidate(
        "approved-overall-tp",
        "TP/SL候補",
        "overall",
        "high",
        "tp_sl_variant_paper_test",
        "TP1=0.75R / TP2=1.5Rをpaper検証",
        "期待R改善",
        {
            "target1_r": 0.75,
            "target2_r": 1.5,
            "scope": "overall",
            "key": "",
            **_net_tp_evidence(baseline=-1.0, candidate=0.75),
            "min_expected_improvement_r": to.MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
        },
        "paper",
        "approval",
    )
    registry = to.update_improvement_registry(
        None, [candidate], now=NOW, data_contract=journal.FUSION_PIT_DATA_CONTRACT
    )
    registry, evidence = _advance_registry_to_review(registry, [candidate])
    approval_time = datetime.fromisoformat(evidence[-1].path_end) + timedelta(hours=2)
    registry, _ = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        now=approval_time,
    )
    summary = to.summarize_expectancy(
        [
            _outcome(-1.0, target_policy_id=candidate.candidate_id, index=index)
            for index in range(to.MIN_GROUP_EXPECTANCY_SAMPLES)
        ],
        min_samples=20,
        group_min_samples=12,
    )
    registry, _ = to.auto_pause_underperforming_approved_policies(
        registry,
        summary,
        now=approval_time + timedelta(hours=1),
    )
    registry_path = tmp_path / "registry.json"
    to.save_improvement_registry(registry, registry_path)

    exit_code = approve_trade_candidate_cli(
        registry_path,
        candidate.candidate_id,
        decision="resumed",
        actor="ops",
        note="再開",
        now=approval_time + timedelta(hours=2),
    )
    payload = json.loads(registry_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["approved_count"] == 1
    assert payload["auto_paused_count"] == 0
    assert payload["candidates"][candidate.candidate_id]["stage"] == "approved"
    assert payload["candidates"][candidate.candidate_id]["resumed_by"] == "ops"


def test_expectancy_health_warns_for_sample_guard() -> None:
    outcomes = [_outcome(1.0, index=index) for index in range(3)]
    summary = to.summarize_expectancy(outcomes, min_samples=5, group_min_samples=5)

    report = to.check_expectancy_health(summary)

    assert report.status == to.STATUS_WARN
    assert report.exit_code == 0


def test_expectancy_health_fails_for_negative_expectancy() -> None:
    outcomes = [_outcome(-1.0, index=index) for index in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)

    report = to.check_expectancy_health(summary)

    assert report.status == to.STATUS_FAIL
    assert report.exit_code == 1


def test_score_trade_outcomes_cli_writes_json_report(tmp_path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    report_path = tmp_path / "trade_outcomes.json"
    monitor_path = tmp_path / "trade_monitor.json"
    rows = [
        _entry(
            NOW,
            "USDJPY",
            100.0,
            direction="long",
            conviction=65,
            stop=99.0,
            target1=101.0,
            target2=102.0,
        ),
        _entry(NOW + timedelta(hours=8), "USDJPY", 100.2),
        _entry(NOW + timedelta(hours=16), "USDJPY", 100.5),
        _entry(NOW + DAY, "USDJPY", 101.2),
    ]
    _write_jsonl(journal_path, rows)

    exit_code = score_trade_outcomes_cli(
        journal_path,
        json_report_path=report_path,
        monitor_json_path=monitor_path,
    )
    raw_report = report_path.read_text(encoding="utf-8")
    payload = json.loads(raw_report)
    monitor = json.loads(monitor_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert "Infinity" not in raw_report
    assert payload["schema"] == 1
    assert len(payload["outcomes"]) == 1
    assert payload["outcomes"][0]["first_touch"] == "tp1"
    assert payload["summary"]["overall"]["tradable"] == 1
    assert payload["improvement_candidates"]
    assert monitor["schema"] == 2
    assert monitor["health"]["status"] in {to.STATUS_OK, to.STATUS_WARN, to.STATUS_FAIL}
    assert "alerts" in monitor


def test_score_trade_outcomes_cli_excludes_legacy_rows(tmp_path) -> None:
    journal_path = tmp_path / "legacy.jsonl"
    report_path = tmp_path / "report.json"
    rows = [
        _entry(NOW, "USDJPY", 100.0, direction="long", stop=99.0, target1=101.0),
        _entry(NOW + DAY, "USDJPY", 101.2),
    ]
    journal_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    assert score_trade_outcomes_cli(journal_path, json_report_path=report_path) == 0
    assert json.loads(report_path.read_text(encoding="utf-8"))["outcomes"] == []


def test_evaluate_trade_outcomes_can_override_tp_r_targets() -> None:
    rows = [
        _entry(
            NOW,
            "USDJPY",
            100.0,
            direction="long",
            conviction=65,
            stop=99.0,
            target1=101.0,
            target2=102.0,
        ),
        _entry(NOW + timedelta(hours=8), "USDJPY", 100.8),
        _entry(NOW + timedelta(hours=16), "USDJPY", 99.0),
        _entry(NOW + DAY, "USDJPY", 99.0),
    ]

    baseline = to.evaluate_trade_outcomes(rows)
    tighter_tp = to.evaluate_trade_outcomes(rows, target1_r=0.75, target2_r=1.5)

    assert baseline[0].first_touch == "sl"
    assert baseline[0].realized_r == -1.0
    assert tighter_tp[0].first_touch == "tp1"
    assert tighter_tp[0].target1 == 100.75
    assert tighter_tp[0].realized_r == 0.75


def test_evaluate_trade_outcomes_uses_high_low_path_for_touch_and_quality() -> None:
    rows = [
        _entry(
            NOW,
            "USDJPY",
            100.0,
            direction="long",
            conviction=65,
            stop=99.0,
            target1=101.0,
            target2=102.0,
        ),
        _entry(
            NOW + timedelta(hours=8),
            "USDJPY",
            100.2,
            high=101.2,
            low=100.1,
            ohlc_scope="post_prediction_interval",
        ),
        _entry(
            NOW + timedelta(hours=16),
            "USDJPY",
            100.4,
            high=100.6,
            low=100.2,
            ohlc_scope="post_prediction_interval",
        ),
        _entry(
            NOW + DAY,
            "USDJPY",
            100.5,
            high=100.7,
            low=100.3,
            ohlc_scope="post_prediction_interval",
        ),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    assert outcome.first_touch == "tp1"
    assert outcome.realized_r == 1.0
    assert outcome.mfe_r == 1.2
    assert outcome.path_source == "ohlc"
    assert "close_only_path" not in outcome.quality_flags
    assert outcome.path_quality > to.CLOSE_ONLY_QUALITY_CAP


def test_evaluate_trade_outcomes_flags_ambiguous_intrabar_touch() -> None:
    rows = [
        _entry(
            NOW,
            "USDJPY",
            100.0,
            direction="long",
            conviction=65,
            stop=99.0,
            target1=101.0,
            target2=102.0,
        ),
        _entry(
            NOW + timedelta(hours=8),
            "USDJPY",
            100.0,
            high=101.2,
            low=98.8,
            ohlc_scope="closed_bar_after_prediction",
        ),
        _entry(
            NOW + timedelta(hours=16),
            "USDJPY",
            100.2,
            high=100.4,
            low=100.0,
            ohlc_scope="closed_bar_after_prediction",
        ),
        _entry(
            NOW + DAY,
            "USDJPY",
            100.3,
            high=100.5,
            low=100.1,
            ohlc_scope="closed_bar_after_prediction",
        ),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    assert outcome.first_touch == "ambiguous_sl_tp"
    assert outcome.realized_r == -1.0
    assert outcome.tp1_hit is True
    assert outcome.sl_hit is True
    assert "ambiguous_intrabar_touch" in outcome.quality_flags


def test_forming_bar_ohlc_is_ignored_for_post_prediction_touch_labels() -> None:
    rows = [
        _entry(
            NOW,
            "USDJPY",
            100.0,
            direction="long",
            conviction=65,
            stop=99.0,
            target1=101.0,
            target2=102.0,
        ),
        _entry(
            NOW + timedelta(hours=8),
            "USDJPY",
            100.2,
            high=101.2,
            low=100.1,
            ohlc_scope="forming_bar_snapshot",
        ),
        _entry(NOW + timedelta(hours=16), "USDJPY", 100.4),
        _entry(NOW + DAY, "USDJPY", 100.5),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    assert outcome.first_touch == "none"
    assert outcome.path_source == "close"
    assert "untrusted_forming_ohlc_ignored" in outcome.quality_flags


def test_mfe_and_mae_stop_at_the_first_exit_touch() -> None:
    rows = [
        _entry(
            NOW,
            "USDJPY",
            100.0,
            direction="long",
            conviction=65,
            stop=99.0,
            target1=101.0,
            target2=None,
        ),
        _entry(
            NOW + timedelta(hours=8),
            "USDJPY",
            101.0,
            high=101.1,
            low=99.8,
            ohlc_scope="closed_bar_after_prediction",
        ),
        _entry(
            NOW + timedelta(hours=16),
            "USDJPY",
            105.0,
            high=110.0,
            low=98.0,
            ohlc_scope="closed_bar_after_prediction",
        ),
        _entry(NOW + DAY, "USDJPY", 105.0),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    assert outcome.first_touch == "tp1"
    assert outcome.mfe_r == 1.1
    assert outcome.mae_r == 0.2


def test_retest_trade_variants_cli_does_not_promote_legacy_gross_candidate(tmp_path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    report_path = tmp_path / "variants.json"
    registry_path = tmp_path / "registry.json"
    rows: list[dict] = []
    for index in range(20):
        symbol = f"TST{index:02d}"
        rows.extend(
            [
                _entry(
                    NOW,
                    symbol,
                    100.0,
                    direction="long",
                    conviction=65,
                    stop=99.0,
                    target1=101.0,
                    target2=102.0,
                ),
                _entry(NOW + timedelta(hours=8), symbol, 100.8),
                _entry(NOW + timedelta(hours=16), symbol, 99.0),
                _entry(NOW + DAY, symbol, 99.0),
            ]
        )
    _write_jsonl(journal_path, rows)

    exit_code = retest_trade_variants_cli(
        journal_path,
        json_report_path=report_path,
        improvement_registry_path=registry_path,
        target1_r_candidates=[0.75, 1.0],
        target2_r_candidates=[1.5, 2.0],
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["baseline"]["overall"]["gross_expectancy_r"] == -1.0
    assert payload["baseline"]["overall"]["net_expectancy_r"] is None
    assert payload["best"] is None
    assert payload["variants"][0]["recommendation"] == "sample_guard"
    assert payload["variants"][0]["target1_r"] == 0.75
    assert payload["variants"][0]["expectancy_r"] is None
    assert payload["improvement_candidates"] == []
    assert registry["active_count"] == 0


def test_retest_trade_variants_reports_symbol_direction_cell_candidates() -> None:
    rows: list[dict] = []
    for index in range(12):
        ts = NOW + timedelta(minutes=index)
        rows.extend(
            [
                _entry(
                    ts,
                    "USDJPY",
                    100.0,
                    direction="long",
                    conviction=65,
                    stop=99.0,
                    target1=101.0,
                    target2=102.0,
                ),
                _entry(ts + timedelta(hours=8), "USDJPY", 100.8),
                _entry(ts + timedelta(hours=16), "USDJPY", 99.0),
                _entry(ts + DAY, "USDJPY", 99.0),
            ]
        )

    report = to.retest_tp_sl_variants(
        rows,
        target1_r_candidates=[0.75, 1.0],
        target2_r_candidates=[1.5, 2.0],
        min_samples=20,
        group_min_samples=12,
    )
    cell = report["cells"]["by_symbol_direction"]["USDJPY:long"]
    candidates = to.variant_improvement_candidates(report, limit=5)

    assert report["baseline"]["overall"]["sample_ok"] is False
    assert cell["baseline"]["sample_ok"] is False
    assert cell["best"] is None
    assert cell["variants"][0]["recommendation"] == "sample_guard"
    assert cell["variants"][0]["target1_r"] == 0.75
    assert candidates == []


def test_check_trade_outcome_health_cli_returns_failure_for_negative_expectancy(tmp_path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    rows: list[dict] = []
    for index in range(20):
        symbol = f"TST{index:02d}"
        rows.extend(
            [
                _entry(
                    NOW,
                    symbol,
                    100.0,
                    direction="long",
                    conviction=55,
                    stop=99.0,
                    target1=101.0,
                    target2=102.0,
                ),
                _entry(NOW + timedelta(hours=8), symbol, 99.8),
                _entry(NOW + timedelta(hours=16), symbol, 99.4),
                _entry(NOW + DAY, symbol, 98.8),
            ]
        )
    _write_jsonl(journal_path, rows)

    assert check_trade_outcome_health_cli(journal_path) == 1


def test_legacy_cost_estimate_does_not_create_a_net_label() -> None:
    """close-only path and checklist costs remain diagnostic, never canonical net R."""
    rows = [
        _entry(
            NOW,
            "USDJPY",
            100.0,
            direction="long",
            action="long",
            stop=99.0,
            target1=101.0,
            target2=102.0,
            conviction=60,
            horizon_hours=24.0,
            execution_cost_r=0.15,
            net_expected_r=0.30,
        ),
        _entry(NOW + DAY, "USDJPY", 102.0),  # 24h後に+2R到達(tp2先着)
    ]
    scored = [o for o in to.evaluate_trade_outcomes(rows) if o.realized_r is not None]
    assert len(scored) == 1
    outcome = scored[0]
    assert outcome.realized_r == 2.0  # グロスは従来通り
    assert outcome.execution_cost_r is None
    assert outcome.realized_net_r is None
    assert outcome.net_expected_r is None
    assert outcome.diagnostic_execution_cost_r == 0.15
    assert outcome.diagnostic_net_expected_r == 0.30
    assert to.NONCANONICAL_COST_QUALITY_FLAG in outcome.quality_flags


def test_realized_net_r_is_none_without_cost() -> None:
    """execution_cost_r が無い行は realized_net_r=None、realized_r は従来通り(既存挙動不変)。"""
    rows = [
        _entry(
            NOW,
            "USDJPY",
            100.0,
            direction="long",
            action="long",
            stop=99.0,
            target1=101.0,
            target2=102.0,
            conviction=60,
            horizon_hours=24.0,
        ),
        _entry(NOW + DAY, "USDJPY", 102.0),
    ]
    scored = [o for o in to.evaluate_trade_outcomes(rows) if o.realized_r is not None]
    assert len(scored) == 1
    outcome = scored[0]
    assert outcome.realized_r == 2.0
    assert outcome.realized_net_r is None
    assert outcome.execution_cost_r is None
    assert outcome.diagnostic_execution_cost_r is None
