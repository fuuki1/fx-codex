"""Trade outcome expectancy audit tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, UTC

from fx_briefing import score_trade_outcomes_cli
from fx_intel import trade_outcome as to

NOW = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)


def _outcome(
    r_multiple: float,
    *,
    symbol: str = "USDJPY",
    direction: str = "long",
    quality: float = 0.70,
) -> to.TradeOutcome:
    return to.TradeOutcome(
        symbol=symbol,
        direction=direction,
        ts=NOW.isoformat(),
        horizon_hours=24.0,
        conviction=52,
        data_quality=0.8,
        entry=100.0,
        stop=99.0,
        target1=101.0,
        target2=102.0,
        risk_distance=1.0,
        terminal_price=100.0 + r_multiple,
        terminal_r=r_multiple,
        mfe_r=max(r_multiple, 0.4),
        mae_r=1.0 if r_multiple < 0 else 0.3,
        tp1_hit=r_multiple >= 1.0,
        tp2_hit=r_multiple >= 2.0,
        sl_hit=r_multiple < 0,
        first_touch="sl" if r_multiple < 0 else ("tp1" if r_multiple >= 1.0 else "none"),
        realized_r=r_multiple,
        path_points=12,
        path_quality=quality,
        quality_flags=("close_only_path",),
    )


def test_expectancy_findings_flag_negative_expectancy_cells() -> None:
    outcomes = [_outcome(-1.0) for _ in range(10)] + [_outcome(1.0) for _ in range(2)]
    summary = to.summarize_expectancy(outcomes, min_samples=10, group_min_samples=5)

    findings = to.expectancy_findings(summary, limit=10)
    candidates = to.improvement_candidates(summary, limit=10)
    report = to.format_expectancy_report_ja(summary, limit=10)

    assert any(item["severity"] == "block" and item["scope"] == "全体" for item in findings)
    assert any(item["label"] == "通貨ペア USDJPY" for item in findings)
    assert any(candidate.action_type == "expectancy_guard" for candidate in candidates)
    assert candidates[0].priority == "high"
    assert "改善候補" in report
    assert "期待R" in report
    assert "非正" in report


def test_expectancy_findings_mark_sample_guard() -> None:
    summary = to.summarize_expectancy(
        [_outcome(1.0) for _ in range(4)],
        min_samples=10,
        group_min_samples=5,
    )

    findings = to.expectancy_findings(summary, limit=10)
    candidates = to.improvement_candidates(summary, limit=10)
    report = to.format_expectancy_report_ja(summary, limit=10)

    assert any(item["severity"] == "sample_guard" for item in findings)
    assert any(candidate.action_type == "collect_samples" for candidate in candidates)
    assert all(
        candidate.proposed_change.get("parameter_change") != "optimize_now"
        for candidate in candidates
    )
    assert "サンプル不足" in report


def test_improvement_registry_tracks_active_and_resolved_candidates() -> None:
    weak_summary = to.summarize_expectancy(
        [_outcome(1.0) for _ in range(4)],
        min_samples=10,
        group_min_samples=5,
    )
    healthy_summary = to.summarize_expectancy(
        [_outcome(1.0) for _ in range(10)],
        min_samples=10,
        group_min_samples=5,
    )
    first_seen = NOW
    second_seen = NOW + timedelta(hours=1)
    resolved_at = NOW + timedelta(hours=2)

    first = to.update_improvement_registry(
        {},
        to.improvement_candidates(weak_summary),
        now=first_seen,
    )
    second = to.update_improvement_registry(
        first,
        to.improvement_candidates(weak_summary),
        now=second_seen,
    )
    resolved = to.update_improvement_registry(
        second,
        to.improvement_candidates(healthy_summary),
        now=resolved_at,
    )

    candidate_id, record = next(iter(second["candidates"].items()))
    assert record["status"] == "active"
    assert record["first_seen"] == first_seen.isoformat()
    assert record["last_seen"] == second_seen.isoformat()
    assert record["seen_count"] == 2
    assert resolved["candidates"][candidate_id]["status"] == "resolved"
    assert resolved["candidates"][candidate_id]["resolved_at"] == resolved_at.isoformat()
    assert "active=0" in to.format_improvement_registry_ja(resolved)


def test_expectancy_health_warns_for_sample_guard() -> None:
    summary = to.summarize_expectancy(
        [_outcome(1.0) for _ in range(4)],
        min_samples=10,
        group_min_samples=5,
    )

    report = to.check_expectancy_health(summary)
    strict_report = to.check_expectancy_health(summary, require_sample_ok=True)

    assert report.status == to.STATUS_WARN
    assert report.exit_code == 0
    assert strict_report.status == to.STATUS_FAIL
    assert strict_report.exit_code == 1
    assert "トレード期待値ヘルスチェック: WARN" in to.format_expectancy_health_ja(report)


def test_expectancy_health_fails_for_negative_expectancy() -> None:
    outcomes = [_outcome(-1.0) for _ in range(10)] + [_outcome(1.0) for _ in range(2)]
    summary = to.summarize_expectancy(outcomes, min_samples=10, group_min_samples=5)

    report = to.check_expectancy_health(summary)

    assert report.status == to.STATUS_FAIL
    assert report.exit_code == 1
    assert any(
        check.name == "expectancy" and check.status == to.STATUS_FAIL for check in report.checks
    )
    assert any(
        check.name == "blocked_cells" and check.status == to.STATUS_FAIL for check in report.checks
    )


def test_score_trade_outcomes_cli_writes_json_report(tmp_path, capsys) -> None:
    journal_path = tmp_path / "briefing_journal.jsonl"
    json_path = tmp_path / "trade_outcomes.json"
    registry_path = tmp_path / "trade_improvement_candidates.json"
    rows = [
        {
            "ts": NOW.isoformat(),
            "symbol": "USDJPY",
            "direction": "long",
            "conviction": 52,
            "close": 100.0,
            "stop": 99.0,
            "target1": 101.0,
            "target2": 102.0,
            "atr": 0.4,
            "data_quality": 0.8,
        },
        {
            "ts": (NOW + timedelta(hours=1)).isoformat(),
            "symbol": "USDJPY",
            "direction": "neutral",
            "close": 100.4,
        },
        {
            "ts": (NOW + timedelta(hours=2)).isoformat(),
            "symbol": "USDJPY",
            "direction": "neutral",
            "close": 101.2,
        },
        {
            "ts": (NOW + timedelta(hours=24)).isoformat(),
            "symbol": "USDJPY",
            "direction": "neutral",
            "close": 100.8,
        },
    ]
    journal_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    exit_code = score_trade_outcomes_cli(journal_path, json_path, registry_path)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "トレード期待値監視" in output
    assert "JSONを保存" in output
    assert "改善候補レジストリを保存" in output
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert payload["summary"]["overall"]["evaluated"] == 1
    assert payload["outcomes"][0]["tp1_hit"] is True
    assert payload["findings"][0]["severity"] == "sample_guard"
    assert payload["improvement_candidates"][0]["action_type"] == "collect_samples"
    assert payload["improvement_registry"]["active_count"] >= 1
    assert registry["active_count"] == payload["improvement_registry"]["active_count"]


def test_check_trade_outcome_health_cli_returns_failure_for_negative_expectancy(
    tmp_path,
    capsys,
) -> None:
    from fx_briefing import check_trade_outcome_health_cli

    journal_path = tmp_path / "briefing_journal.jsonl"
    rows = []
    for index in range(22):
        ts = NOW + timedelta(hours=index * 2)
        rows.append(
            {
                "ts": ts.isoformat(),
                "symbol": "USDJPY",
                "direction": "long",
                "conviction": 52,
                "close": 100.0,
                "stop": 99.0,
                "target1": 101.0,
                "target2": 102.0,
                "atr": 0.4,
                "data_quality": 0.8,
            }
        )
        rows.append(
            {
                "ts": (ts + timedelta(hours=1)).isoformat(),
                "symbol": "USDJPY",
                "direction": "neutral",
                "close": 98.8,
            }
        )
    journal_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    exit_code = check_trade_outcome_health_cli(journal_path)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "トレード期待値ヘルスチェック: FAIL" in output
    assert "期待R" in output
