from __future__ import annotations

from datetime import UTC, datetime, timedelta
import copy
import json

import pytest

from fx_intel.virtual_portfolio import (
    VirtualPortfolioPolicy,
    open_virtual_portfolio_reader,
    open_virtual_portfolio_writer,
)
from tools import virtual_portfolio_daily_report as daily_report_module
from tools.virtual_portfolio_daily_report import (
    DailyReportNotReady,
    DailyReportUnavailable,
    build_daily_report,
    create_or_load_report,
    discord_payload,
)

START = datetime(2026, 7, 29, tzinfo=UTC)
SESSION_DATE = "2026-07-29"


def _database(tmp_path, *, close: bool):
    database = tmp_path / "virtual.sqlite3"
    cutoff = START + timedelta(hours=8)
    with open_virtual_portfolio_writer(database, writer_id="test") as store:
        store.initialize_portfolio(
            policy=VirtualPortfolioPolicy(),
            created_at=START,
        )
        store.record_session_event(
            session_date_jst=SESSION_DATE,
            event_type="opened",
            event_time=START,
        )
        store.record_session_close_request(
            session_date_jst=SESSION_DATE,
            requested_at=cutoff,
            cutoff_time=cutoff,
            detail={"source": "test"},
        )
        if close:
            request = store.session_close_request(session_date_jst=SESSION_DATE)
            assert request is not None
            store.record_session_close_completion(
                request_id=str(request["request_id"]),
                completed_at=cutoff + timedelta(minutes=1),
                detail={"reconciled": True},
            )
    return database


def test_daily_report_waits_for_closed_session(tmp_path) -> None:
    database = _database(tmp_path, close=False)
    with open_virtual_portfolio_reader(database) as store:
        with pytest.raises(DailyReportNotReady, match="not complete"):
            build_daily_report(
                store,
                session_date_jst=SESSION_DATE,
                generated_at=START + timedelta(hours=9),
            )


def test_daily_report_is_reconciled_create_only_and_discord_safe(tmp_path) -> None:
    database = _database(tmp_path, close=True)
    with open_virtual_portfolio_reader(database) as store:
        report = build_daily_report(
            store,
            session_date_jst=SESSION_DATE,
            generated_at=START + timedelta(hours=9),
        )
        retry = build_daily_report(
            store,
            session_date_jst=SESSION_DATE,
            generated_at=START + timedelta(hours=10),
        )

    assert report == retry
    assert report["execution_mode"] == "offline_simulation"
    assert report["broker_connected"] is False
    assert report["trade_count"] == 0
    assert report["pnl_identity_passed"] is True
    assert report["portfolio"]["balance_jpy"] == 1_000_000
    assert report["daily_target_is_kpi_only"] is False
    assert report["daily_target_required_task"] is True
    assert report["daily_target_not_optimization_input"] is True
    assert report["daily_target_reference_status"] == "required_operational_task"
    assert report["daily_profit_task"]["required"] is True
    assert report["daily_profit_task"]["status"] == "in_progress"
    assert report["daily_profit_task"]["achievement_guaranteed"] is False
    assert (
        report["session_close_completion"]["request_id"]
        == report["session_close_request"]["request_id"]
    )
    assert report["research_kpis"]["primary"] == ("rolling_20d_cost_adjusted_expectancy_net_r")

    path, loaded, created = create_or_load_report(tmp_path / "runs", report)
    assert created is True
    assert path.is_file()
    assert loaded == report
    _, loaded_again, created_again = create_or_load_report(tmp_path / "runs", retry)
    assert created_again is False
    assert loaded_again == report

    payload = discord_payload(report)
    rendered = str(payload)
    assert "分析専用・ブローカー未接続" in rendered
    assert "達成保証" in rendered
    assert "webhook" not in rendered.lower()


def test_shortfall_attribution_decomposes_the_gap_without_forecasting(tmp_path) -> None:
    """未達額を cost / execution / opportunity へ分解する。

    実機 2026-07-30 では不足 73,961円(14.8R)のうち損失は 0.8R(5%)だけで、
    76.7% が「同時保有上限で建てられなかった」だった。「なぜ勝てないか」だけを
    見ると原因の 5% しか見ないことになるため、建てられなかった分を必ず併記する。
    """

    database = _database(tmp_path, close=True)
    with open_virtual_portfolio_reader(database) as store:
        report = build_daily_report(
            store,
            session_date_jst=SESSION_DATE,
            generated_at=START + timedelta(hours=9),
        )

    attribution = report["shortfall_attribution"]
    assert attribution["contract"] == "virtual-portfolio-shortfall-attribution-v1"

    # 目標は KPI であって学習ラベルではない(自動調整へ繋がない)
    assert attribution["not_training_label"] is True
    # 残余を「建てれば勝てた額」と読ませない
    assert attribution["opportunity_is_residual_not_forecast"] is True

    # 恒等式: 3段階の合計は必ず不足額に一致する
    stages = attribution["stages"]
    assert (
        stages["opportunity"] + stages["execution"] + stages["cost"] == attribution["shortfall_jpy"]
    )
    assert attribution["shortfall_jpy"] == (
        attribution["target_jpy"] - attribution["realized_net_pnl_jpy"]
    )

    # 取引ゼロの日でも例外にならず、機会側が支配的になる
    assert report["trade_count"] == 0
    assert attribution["target_met"] is False
    assert attribution["binding_constraint"] == "opportunity"
    assert attribution["execution"]["trades"] == 0
    assert attribution["cost"]["total_cost_jpy"] == 0

    # 枠の占有状況(誰が枠を塞いだかを翌日の判断材料にする)
    execution = attribution["execution"]
    assert execution["slot_hours_available"] > 0
    assert execution["slot_hours_occupied"] == 0
    assert execution["slot_utilization"] == 0.0


def test_daily_report_retry_ignores_late_observer_counts_but_not_ledger_changes(
    tmp_path,
) -> None:
    database = _database(tmp_path, close=True)
    with open_virtual_portfolio_reader(database) as store:
        report = build_daily_report(
            store,
            session_date_jst=SESSION_DATE,
            generated_at=START + timedelta(hours=9),
        )
    run_root = tmp_path / "runs"
    create_or_load_report(run_root, report)

    observer_drift = copy.deepcopy(report)
    observer_drift["horizon_allocation"]["counts"]["intraday_capacity"] += 1
    observer_payload = dict(observer_drift)
    observer_payload.pop("report_sha256")
    observer_drift["report_sha256"] = daily_report_module._sha256(observer_payload)
    _, loaded, created = create_or_load_report(run_root, observer_drift)
    assert created is False
    assert loaded == report

    ledger_drift = copy.deepcopy(report)
    ledger_drift["portfolio"]["balance_jpy"] -= 1
    ledger_payload = dict(ledger_drift)
    ledger_payload.pop("report_sha256")
    ledger_drift["report_sha256"] = daily_report_module._sha256(ledger_payload)
    with pytest.raises(DailyReportUnavailable, match="canonical ledger"):
        create_or_load_report(run_root, ledger_drift)

    report_path = run_root / SESSION_DATE / "daily_report.json"
    tampered = json.loads(report_path.read_text(encoding="utf-8"))
    tampered["trade_count"] = 99
    report_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(DailyReportUnavailable, match="cannot be verified"):
        create_or_load_report(run_root, report)
