"""MFE/MAE/TP/SL期待値監査のテスト。ネットワーク不要。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json

import pytest

from fx_briefing import (
    approve_trade_candidate_cli,
    check_trade_outcome_health_cli,
    retest_trade_variants_cli,
    score_trade_outcomes_cli,
)
from fx_intel import briefing, journal, trade_outcome as to
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
) -> to.TradeOutcome:
    return to.TradeOutcome(
        symbol=symbol,
        direction=direction,
        ts=NOW.isoformat(),
        horizon_hours=24.0,
        conviction=conviction,
        data_quality=0.90,
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
        path_points=6,
        path_start=(NOW + timedelta(hours=4)).isoformat(),
        path_end=(NOW + DAY).isoformat(),
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
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


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
    outcomes = [_outcome(-1.0) for _ in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)

    findings = to.expectancy_findings(summary)

    assert any(
        finding["label"] == "全体" and finding["severity"] == "block" for finding in findings
    )
    assert summary["overall"]["sample_ok"] is True
    assert summary["overall"]["expectancy_r"] == -1.0


def test_expectancy_findings_mark_sample_guard() -> None:
    outcomes = [_outcome(1.0) for _ in range(3)]
    summary = to.summarize_expectancy(outcomes, min_samples=5, group_min_samples=5)

    findings = to.expectancy_findings(summary)

    assert any(finding["severity"] == "sample_guard" for finding in findings)
    assert summary["overall"]["sample_ok"] is False


def test_decision_adjustment_blocks_matching_symbol_direction() -> None:
    outcomes = [_outcome(-1.0, symbol="USDJPY", direction="long") for _ in range(12)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)

    adjustment = to.decision_adjustment(summary, "USDJPY", "long", 60)

    assert adjustment.action == "block"
    assert adjustment.block is True
    assert adjustment.factor == to.EXPECTANCY_BLOCK_FACTOR
    assert adjustment.matched_scope == "通貨ペア×方向"


def test_decision_adjustment_keeps_sample_guard_non_blocking() -> None:
    outcomes = [_outcome(1.0, symbol="USDJPY", direction="long") for _ in range(3)]
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
    )

    assert plan.direction == "neutral"
    assert plan.conviction == 0
    assert plan.stop is None
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
    outcomes = [_outcome(-1.0) for _ in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)
    candidate = next(
        candidate
        for candidate in to.improvement_candidates(summary)
        if candidate.priority == "high"
    )

    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    record = registry["candidates"][candidate.candidate_id]
    assert record["status"] == "active"
    assert record["stage"] == "watch"
    assert record["seen_count"] == 1
    assert registry["events"][-1]["event_type"] == "detected"

    registry = to.update_improvement_registry(
        registry,
        [candidate],
        now=NOW + timedelta(hours=1),
    )
    record = registry["candidates"][candidate.candidate_id]
    assert record["stage"] == "paper_ready"
    assert record["seen_count"] == 2
    assert registry["events"][-1]["event_type"] == "stage_changed"

    registry = to.update_improvement_registry(
        registry,
        [],
        now=NOW + timedelta(hours=2),
    )
    record = registry["candidates"][candidate.candidate_id]
    assert record["status"] == "resolved"
    assert record["stage"] == "resolved"
    assert registry["resolved_count"] == 1
    assert registry["events"][-1]["event_type"] == "resolved"


def test_improvement_registry_preserves_unmanaged_candidate_types() -> None:
    outcomes = [_outcome(-1.0) for _ in range(20)]
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
    variant_candidate = to.variant_improvement_candidates(variant_report)[0]
    registry = to.update_improvement_registry(
        registry,
        [variant_candidate],
        now=NOW + timedelta(hours=1),
        managed_action_types=to.VARIANT_CANDIDATE_ACTION_TYPES,
    )

    assert registry["candidates"][expectancy_candidate.candidate_id]["status"] == "active"
    assert registry["candidates"][variant_candidate.candidate_id]["status"] == "active"


def test_improvement_candidate_approval_requires_paper_ready_and_is_preserved() -> None:
    outcomes = [_outcome(-1.0) for _ in range(20)]
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
    assert unchanged["candidates"][candidate.candidate_id]["stage"] == "watch"

    registry = to.update_improvement_registry(registry, [candidate], now=NOW + timedelta(hours=1))
    approved, result = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        actor="tester",
        note="paper検証OK",
        now=NOW + timedelta(hours=2),
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
        now=NOW + timedelta(hours=3),
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
        {"target1_r": 0.75, "target2_r": 1.5, "scope": "overall", "key": ""},
        "paper",
        "approval",
    )
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    registry = to.update_improvement_registry(registry, [candidate], now=NOW + timedelta(hours=1))

    unchanged, result = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        actor="tester",
        now=NOW + timedelta(hours=2),
    )

    assert result["status"] == "not_improving"
    assert unchanged["candidates"][candidate.candidate_id]["stage"] == "paper_ready"


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
            "baseline_expectancy_r": -1.0,
            "candidate_expectancy_r": 0.3,
            "delta_expectancy_r": 1.3,
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
            "baseline_expectancy_r": -1.0,
            "candidate_expectancy_r": 0.4,
            "delta_expectancy_r": 1.4,
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
    registry = to.update_improvement_registry(
        registry,
        [overall_candidate, cell_candidate],
        now=NOW + timedelta(hours=1),
    )
    registry, _ = to.set_improvement_candidate_approval(
        registry,
        overall_candidate.candidate_id,
        "approved",
        now=NOW + timedelta(hours=2),
    )
    registry, _ = to.set_improvement_candidate_approval(
        registry,
        cell_candidate.candidate_id,
        "approved",
        now=NOW + timedelta(hours=3),
    )

    policy = to.select_approved_target_policy(registry, "USDJPY", "long", 60)
    fallback = to.select_approved_target_policy(registry, "EURUSD", "long", 60)

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
            "baseline_expectancy_r": -1.0,
            "candidate_expectancy_r": 0.75,
            "delta_expectancy_r": 1.75,
            "min_expected_improvement_r": to.MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
        },
        "paper",
        "approval",
    )
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    registry = to.update_improvement_registry(registry, [candidate], now=NOW + timedelta(hours=1))
    registry, result = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        now=NOW + timedelta(hours=2),
    )
    outcomes = [
        _outcome(-1.0, target_policy_id=candidate.candidate_id)
        for _ in range(to.MIN_GROUP_EXPECTANCY_SAMPLES)
    ]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)

    paused_registry, paused = to.auto_pause_underperforming_approved_policies(
        registry,
        summary,
        now=NOW + timedelta(hours=3),
    )

    assert result["status"] == "approved"
    assert paused[0]["candidate_id"] == candidate.candidate_id
    record = paused_registry["candidates"][candidate.candidate_id]
    assert record["stage"] == "auto_paused"
    assert paused_registry["auto_paused_count"] == 1
    assert paused_registry["events"][-1]["event_type"] == "auto_paused"
    assert paused_registry["events"][-1]["details"]["tradable"] == to.MIN_GROUP_EXPECTANCY_SAMPLES
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
            "baseline_expectancy_r": -1.0,
            "candidate_expectancy_r": 0.75,
            "delta_expectancy_r": 1.75,
            "min_expected_improvement_r": to.MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
        },
        "paper",
        "approval",
    )
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    registry = to.update_improvement_registry(registry, [candidate], now=NOW + timedelta(hours=1))
    registry, _ = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        now=NOW + timedelta(hours=2),
    )
    summary = to.summarize_expectancy(
        [
            _outcome(-1.0, target_policy_id=candidate.candidate_id)
            for _ in range(to.MIN_GROUP_EXPECTANCY_SAMPLES)
        ],
        min_samples=20,
        group_min_samples=12,
    )
    paused_registry, _ = to.auto_pause_underperforming_approved_policies(
        registry,
        summary,
        now=NOW + timedelta(hours=3),
    )

    resumed_registry, result = to.set_improvement_candidate_approval(
        paused_registry,
        candidate.candidate_id,
        "resumed",
        actor="ops",
        note="手動で再開",
        now=NOW + timedelta(hours=4),
    )
    policy = to.select_approved_target_policy(resumed_registry, "USDJPY", "long", 60)

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
        now=NOW + timedelta(hours=5),
    )
    assert invalid["status"] == "not_paused"


def test_monitoring_snapshot_includes_health_and_ready_candidates() -> None:
    outcomes = [_outcome(-1.0) for _ in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)
    candidate = to.improvement_candidates(summary)[0]
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    registry = to.update_improvement_registry(registry, [candidate], now=NOW + timedelta(hours=1))

    snapshot = to.build_monitoring_snapshot(summary, registry=registry, now=NOW)

    assert snapshot["schema"] == 1
    assert snapshot["status"] == to.STATUS_FAIL
    assert snapshot["registry"]["paper_ready_count"] == 1
    assert snapshot["registry"]["paper_ready"][0]["candidate_id"] == candidate.candidate_id
    assert snapshot["recent_events"][-1]["event_type"] == "stage_changed"
    assert any(alert["type"] == "paper_ready" for alert in snapshot["alerts"])


def test_approve_trade_candidate_cli_updates_registry(tmp_path) -> None:
    outcomes = [_outcome(-1.0) for _ in range(20)]
    summary = to.summarize_expectancy(outcomes, min_samples=20, group_min_samples=12)
    candidate = to.improvement_candidates(summary)[0]
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    registry = to.update_improvement_registry(registry, [candidate], now=NOW + timedelta(hours=1))
    registry_path = tmp_path / "registry.json"
    to.save_improvement_registry(registry, registry_path)

    exit_code = approve_trade_candidate_cli(
        registry_path,
        candidate.candidate_id,
        decision="approved",
        actor="tester",
        note="paper OK",
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
            "baseline_expectancy_r": -1.0,
            "candidate_expectancy_r": 0.75,
            "delta_expectancy_r": 1.75,
            "min_expected_improvement_r": to.MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
        },
        "paper",
        "approval",
    )
    registry = to.update_improvement_registry(None, [candidate], now=NOW)
    registry = to.update_improvement_registry(registry, [candidate], now=NOW + timedelta(hours=1))
    registry, _ = to.set_improvement_candidate_approval(
        registry,
        candidate.candidate_id,
        "approved",
        now=NOW + timedelta(hours=2),
    )
    summary = to.summarize_expectancy(
        [
            _outcome(-1.0, target_policy_id=candidate.candidate_id)
            for _ in range(to.MIN_GROUP_EXPECTANCY_SAMPLES)
        ],
        min_samples=20,
        group_min_samples=12,
    )
    registry, _ = to.auto_pause_underperforming_approved_policies(
        registry,
        summary,
        now=NOW + timedelta(hours=3),
    )
    registry_path = tmp_path / "registry.json"
    to.save_improvement_registry(registry, registry_path)

    exit_code = approve_trade_candidate_cli(
        registry_path,
        candidate.candidate_id,
        decision="resumed",
        actor="ops",
        note="再開",
    )
    payload = json.loads(registry_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["approved_count"] == 1
    assert payload["auto_paused_count"] == 0
    assert payload["candidates"][candidate.candidate_id]["stage"] == "approved"
    assert payload["candidates"][candidate.candidate_id]["resumed_by"] == "ops"


def test_expectancy_health_warns_for_sample_guard() -> None:
    outcomes = [_outcome(1.0) for _ in range(3)]
    summary = to.summarize_expectancy(outcomes, min_samples=5, group_min_samples=5)

    report = to.check_expectancy_health(summary)

    assert report.status == to.STATUS_WARN
    assert report.exit_code == 0


def test_expectancy_health_fails_for_negative_expectancy() -> None:
    outcomes = [_outcome(-1.0) for _ in range(20)]
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
    assert monitor["schema"] == 1
    assert monitor["health"]["status"] in {to.STATUS_OK, to.STATUS_WARN, to.STATUS_FAIL}
    assert "alerts" in monitor


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
        _entry(NOW + timedelta(hours=8), "USDJPY", 100.2, high=101.2, low=100.1),
        _entry(NOW + timedelta(hours=16), "USDJPY", 100.4, high=100.6, low=100.2),
        _entry(NOW + DAY, "USDJPY", 100.5, high=100.7, low=100.3),
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
        _entry(NOW + timedelta(hours=8), "USDJPY", 100.0, high=101.2, low=98.8),
        _entry(NOW + timedelta(hours=16), "USDJPY", 100.2, high=100.4, low=100.0),
        _entry(NOW + DAY, "USDJPY", 100.3, high=100.5, low=100.1),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    assert outcome.first_touch == "ambiguous_sl_tp"
    assert outcome.realized_r == -1.0
    assert outcome.tp1_hit is True
    assert outcome.sl_hit is True
    assert "ambiguous_intrabar_touch" in outcome.quality_flags


def test_evaluate_trade_outcomes_uses_bid_for_long_exit_path() -> None:
    rows = [
        _entry(
            NOW,
            "EURUSD",
            100.0,
            direction="long",
            stop=99.0,
            target1=101.0,
            target2=102.0,
        ),
        _entry(
            NOW + timedelta(hours=8),
            "EURUSD",
            101.0,
            high=101.1,
            low=100.0,
            bid_close=100.8,
            bid_high=100.95,
            bid_low=99.9,
            ask_close=101.0,
            ask_high=101.15,
            ask_low=100.1,
        ),
        _entry(
            NOW + timedelta(hours=16),
            "EURUSD",
            100.5,
            high=100.7,
            low=100.3,
            bid_close=100.4,
            bid_high=100.6,
            bid_low=100.2,
            ask_close=100.6,
            ask_high=100.8,
            ask_low=100.4,
        ),
        _entry(
            NOW + DAY,
            "EURUSD",
            100.4,
            high=100.6,
            low=100.2,
            bid_close=100.3,
            bid_high=100.5,
            bid_low=100.1,
            ask_close=100.5,
            ask_high=100.7,
            ask_low=100.3,
        ),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    # mid highはTP1へ触れているが、longで売れるbid highは未達なのでTP扱いしない。
    assert outcome.first_touch == "none"
    assert outcome.terminal_price == 100.3
    assert outcome.terminal_r == pytest.approx(0.3)
    assert outcome.path_source == "bid_ask_ohlc"


def test_evaluate_trade_outcomes_uses_ask_for_short_exit_path() -> None:
    rows = [
        _entry(
            NOW,
            "EURUSD",
            100.0,
            direction="short",
            stop=101.0,
            target1=99.0,
            target2=98.0,
        ),
        _entry(
            NOW + timedelta(hours=8),
            "EURUSD",
            99.0,
            high=100.0,
            low=98.9,
            bid_close=98.9,
            bid_high=99.9,
            bid_low=98.8,
            ask_close=99.1,
            ask_high=100.1,
            ask_low=99.05,
        ),
        _entry(
            NOW + timedelta(hours=16),
            "EURUSD",
            99.5,
            high=99.7,
            low=99.3,
            bid_close=99.4,
            bid_high=99.6,
            bid_low=99.2,
            ask_close=99.6,
            ask_high=99.8,
            ask_low=99.4,
        ),
        _entry(
            NOW + DAY,
            "EURUSD",
            99.4,
            high=99.6,
            low=99.2,
            bid_close=99.3,
            bid_high=99.5,
            bid_low=99.1,
            ask_close=99.5,
            ask_high=99.7,
            ask_low=99.3,
        ),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    # mid lowはTP1へ触れているが、shortを買い戻すask lowは未達。
    assert outcome.first_touch == "none"
    assert outcome.terminal_price == 99.5
    assert outcome.terminal_r == pytest.approx(0.5)


def test_realized_net_r_uses_executable_quotes_without_double_counting_spread() -> None:
    rows = [
        _entry(
            NOW,
            "EURUSD",
            100.0,
            direction="long",
            stop=99.0,
            target1=102.0,
            target2=103.0,
            entry_bid=99.9,
            entry_ask=100.1,
            quote_observed_at=NOW.isoformat(),
            cost_model_id="test-quotes-v1",
            slippage_r=0.0,
            commission_r=0.0,
            decision_id="decision-net-r",
        ),
        _entry(
            NOW + timedelta(hours=8),
            "EURUSD",
            100.3,
            high=100.5,
            low=100.1,
            bid_close=100.2,
            bid_high=100.4,
            bid_low=100.0,
            ask_close=100.4,
            ask_high=100.6,
            ask_low=100.2,
        ),
        _entry(
            NOW + timedelta(hours=16),
            "EURUSD",
            100.6,
            high=100.8,
            low=100.4,
            bid_close=100.5,
            bid_high=100.7,
            bid_low=100.3,
            ask_close=100.7,
            ask_high=100.9,
            ask_low=100.5,
        ),
        _entry(
            NOW + DAY,
            "EURUSD",
            100.9,
            high=101.1,
            low=100.7,
            bid_close=100.8,
            bid_high=101.0,
            bid_low=100.6,
            ask_close=101.0,
            ask_high=101.2,
            ask_low=100.8,
        ),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    assert outcome.decision_id == "decision-net-r"
    assert outcome.gross_realized_r == pytest.approx(0.9)
    assert outcome.quote_realized_r == pytest.approx(0.7)
    assert outcome.execution_cost_r == pytest.approx(0.2)
    assert outcome.realized_net_r == pytest.approx(0.7)
    assert outcome.net_label_eligible is True
    assert outcome.cost_status == "quote_measured_modelled_execution"


def test_realized_net_r_is_missing_without_decision_time_quote() -> None:
    rows = [
        _entry(
            NOW,
            "EURUSD",
            100.0,
            direction="long",
            stop=99.0,
            target1=102.0,
            target2=103.0,
            cost_model_id="test-quotes-v1",
            slippage_r=0.0,
            commission_r=0.0,
        )
    ]
    for hours, close in ((8, 100.2), (16, 100.4), (24, 100.6)):
        rows.append(
            _entry(
                NOW + timedelta(hours=hours),
                "EURUSD",
                close,
                high=close + 0.1,
                low=close - 0.1,
                bid_close=close - 0.05,
                bid_high=close + 0.05,
                bid_low=close - 0.15,
                ask_close=close + 0.05,
                ask_high=close + 0.15,
                ask_low=close - 0.05,
            )
        )

    outcome = to.evaluate_trade_outcomes(rows)[0]

    assert outcome.realized_r is not None
    assert outcome.realized_net_r is None
    assert outcome.net_label_eligible is False
    assert "missing_net_label_entry_quote" in outcome.quality_flags


def test_realized_net_r_uses_adverse_open_for_stop_gap() -> None:
    rows = [
        _entry(
            NOW,
            "EURUSD",
            100.0,
            direction="long",
            stop=99.0,
            target1=101.0,
            target2=102.0,
            entry_bid=99.9,
            entry_ask=100.1,
            quote_observed_at=NOW.isoformat(),
            cost_model_id="test-quotes-v1",
            slippage_r=0.0,
            commission_r=0.0,
        ),
        _entry(
            NOW + timedelta(hours=8),
            "EURUSD",
            99.0,
            high=99.2,
            low=98.7,
            bid_open=98.8,
            bid_close=98.9,
            bid_high=99.1,
            bid_low=98.6,
            ask_open=99.0,
            ask_close=99.1,
            ask_high=99.3,
            ask_low=98.8,
        ),
        _entry(
            NOW + timedelta(hours=16),
            "EURUSD",
            99.1,
            high=99.3,
            low=98.9,
            bid_close=99.0,
            bid_high=99.2,
            bid_low=98.8,
            ask_close=99.2,
            ask_high=99.4,
            ask_low=99.0,
        ),
        _entry(
            NOW + DAY,
            "EURUSD",
            99.2,
            high=99.4,
            low=99.0,
            bid_close=99.1,
            bid_high=99.3,
            bid_low=98.9,
            ask_close=99.3,
            ask_high=99.5,
            ask_low=99.1,
        ),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    assert outcome.first_touch == "sl"
    assert outcome.executable_exit == pytest.approx(98.8)
    assert outcome.realized_net_r == pytest.approx(-1.3)


def test_completed_bar_starting_before_decision_is_excluded() -> None:
    decision_time = NOW + timedelta(minutes=2)
    rows = [
        _entry(
            decision_time,
            "EURUSD",
            100.0,
            direction="long",
            stop=99.0,
            target1=101.0,
            target2=102.0,
        ),
        # 終了は判断後だが開始は判断前。high=TP到達を採点へ使ってはいけない。
        _entry(
            NOW + timedelta(minutes=5),
            "EURUSD",
            101.1,
            high=101.2,
            low=99.8,
            bar_start=NOW.isoformat(),
        ),
        _entry(
            NOW + timedelta(hours=8),
            "EURUSD",
            100.2,
            high=100.4,
            low=100.0,
            bar_start=(NOW + timedelta(hours=7, minutes=55)).isoformat(),
        ),
        _entry(
            NOW + timedelta(hours=16),
            "EURUSD",
            100.3,
            high=100.5,
            low=100.1,
            bar_start=(NOW + timedelta(hours=15, minutes=55)).isoformat(),
        ),
        _entry(
            NOW + DAY,
            "EURUSD",
            100.4,
            high=100.6,
            low=100.2,
            bar_start=(NOW + timedelta(hours=23, minutes=55)).isoformat(),
        ),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]
    assert outcome.first_touch == "none"


def test_direction_only_and_forming_bar_ranges_do_not_score_tp() -> None:
    rows = [
        _entry(
            NOW,
            "EURUSD",
            100.0,
            direction="long",
            stop=99.0,
            target1=101.0,
            target2=102.0,
        ),
        _entry(
            NOW + timedelta(hours=4),
            "EURUSD",
            101.2,
            high=101.4,
            low=100.0,
            price_usage="direction_only",
        ),
        _entry(
            NOW + timedelta(hours=8),
            "EURUSD",
            100.2,
            high=101.3,
            low=99.8,
            ohlc_scope="forming_bar_snapshot",
        ),
        _entry(NOW + timedelta(hours=16), "EURUSD", 100.3),
        _entry(NOW + DAY, "EURUSD", 100.4),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    assert outcome.first_touch == "none"
    assert outcome.path_points == 3
    assert outcome.path_source == "close"


def test_duplicate_timestamp_prefers_bid_ask_ohlc_without_path_inflation() -> None:
    rows = [
        _entry(
            NOW,
            "EURUSD",
            100.0,
            direction="long",
            stop=99.0,
            target1=101.0,
            target2=102.0,
        ),
        _entry(NOW + timedelta(hours=8), "EURUSD", 100.2),
        _entry(
            NOW + timedelta(hours=8),
            "EURUSD",
            100.25,
            high=100.4,
            low=100.0,
            bid_close=100.2,
            bid_high=100.35,
            bid_low=99.95,
            ask_close=100.3,
            ask_high=100.45,
            ask_low=100.05,
        ),
        _entry(NOW + timedelta(hours=16), "EURUSD", 100.3),
        _entry(NOW + DAY, "EURUSD", 100.4),
    ]

    outcome = to.evaluate_trade_outcomes(rows)[0]

    assert outcome.path_points == 3
    assert outcome.terminal_price == 100.4


def test_retest_trade_variants_cli_writes_paper_candidate(tmp_path) -> None:
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
    assert payload["baseline"]["overall"]["expectancy_r"] == -1.0
    assert payload["best"]["recommendation"] == "paper_test"
    assert payload["best"]["target1_r"] == 0.75
    assert payload["best"]["expectancy_r"] == 0.75
    assert payload["improvement_candidates"][0]["action_type"] == "tp_sl_variant_paper_test"
    assert registry["active_count"] == len(payload["improvement_candidates"])


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
    assert cell["baseline"]["sample_ok"] is True
    assert cell["best"]["recommendation"] == "paper_test"
    assert cell["best"]["target1_r"] == 0.75
    assert any(
        candidate.action_type == "tp_sl_variant_paper_test"
        and candidate.proposed_change["scope"] == "by_symbol_direction"
        and candidate.proposed_change["key"] == "USDJPY:long"
        for candidate in candidates
    )


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
