from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fx_intel.counterfactual_cost_policy import (
    ACCOUNTING_COST_CONTRACT,
    CounterfactualAccountingCostPolicy,
    accounting_cost_r,
    cost_breakdown,
    rollovers_crossed,
)
from fx_intel.virtual_portfolio_replay import ReplayDataError, VirtualReplayCostPolicy

# baseline artifact 20260904T023814Z-23bd824de790 の manifest が記録している値
BASELINE_POLICY_SHA = "1e094a66ea8083162564cb1c0f4b6d4f93ed8339ed3876f5b93c08dd509cc7b9"


def test_existing_production_policy_hash_is_untouched() -> None:
    """既存policyのsha256を壊さないこと。

    ``VirtualReplayCostPolicy.sha256`` は ``asdict(self)`` を封じるため、
    フィールドを1つ足すだけで既存DB全行の policy_sha256 が無効になる。
    新policyは別classなので、この照合が守られる。
    """
    assert VirtualReplayCostPolicy().sha256 == BASELINE_POLICY_SHA


def test_new_policy_has_distinct_identity() -> None:
    assert CounterfactualAccountingCostPolicy().sha256 != BASELINE_POLICY_SHA


def test_spread_is_not_charged_because_gross_already_crosses_it() -> None:
    """L1: slippage項が存在しないこと。gross_rで控除済みのため。"""
    policy = CounterfactualAccountingCostPolicy()
    assert not hasattr(policy, "slippage_r_bps")
    entry = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    cost = accounting_cost_r(
        policy=policy, symbol="USDJPY", entry_time=entry, exit_time=entry + timedelta(minutes=15)
    )
    # commission のみ。spread も financing も乗らない。
    assert cost == pytest.approx(0.0100)


def test_intraday_holding_charges_no_financing() -> None:
    """L2: 15m/1h はロールオーバーを跨がないので financing 0。

    旧policyは max(1, ...) で必ず1日分課金しており、
    15m で 96.0倍、1h で 24.0倍の過大だった。
    """
    policy = CounterfactualAccountingCostPolicy()
    entry = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    for minutes in (15, 60):
        exit_time = entry + timedelta(minutes=minutes)
        assert rollovers_crossed(entry, exit_time) == 0
        breakdown = cost_breakdown(
            policy=policy, symbol="USDJPY", entry_time=entry, exit_time=exit_time
        )
        derivable = breakdown["derivable_l2_r"]
        assert isinstance(derivable, dict)
        assert derivable["financing_r"] == pytest.approx(0.0)


def test_crossing_ny_rollover_charges_financing_once() -> None:
    """NY 17:00 を跨いだら1回分だけ課金する。"""
    policy = CounterfactualAccountingCostPolicy()
    # 2026-09-01 20:00Z = NY 16:00 EDT (ロールオーバー前)
    entry = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
    # 2026-09-01 22:00Z = NY 18:00 EDT (ロールオーバー後)
    exit_time = datetime(2026, 9, 1, 22, 0, tzinfo=UTC)
    assert rollovers_crossed(entry, exit_time) == 1
    cost = accounting_cost_r(policy=policy, symbol="USDJPY", entry_time=entry, exit_time=exit_time)
    assert cost == pytest.approx(0.0100 + 0.0100)


def test_multi_day_holding_charges_each_rollover() -> None:
    entry = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    exit_time = entry + timedelta(days=3)
    assert rollovers_crossed(entry, exit_time) == 3


def test_execution_cost_is_isolated_and_defaults_to_zero() -> None:
    """L3: 未観測。既定0は『無コスト』ではなく『根拠がない』の意。"""
    policy = CounterfactualAccountingCostPolicy()
    assert policy.execution_r_bps == 0
    entry = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    breakdown = cost_breakdown(
        policy=policy,
        symbol="USDJPY",
        entry_time=entry,
        exit_time=entry + timedelta(minutes=15),
    )
    unobserved = breakdown["unobserved_l3_r"]
    assert isinstance(unobserved, dict)
    assert unobserved["execution_r"] == pytest.approx(0.0)
    assert "not costless" in str(unobserved["basis"])


def test_execution_cost_is_configurable_when_measured() -> None:
    """measured層が用意されたらL3に値を入れられる。"""
    policy = CounterfactualAccountingCostPolicy(execution_r_bps=30)
    entry = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    cost = accounting_cost_r(
        policy=policy,
        symbol="USDJPY",
        entry_time=entry,
        exit_time=entry + timedelta(minutes=15),
    )
    assert cost == pytest.approx(0.0100 + 0.0030)


def test_conversion_applies_only_to_non_jpy_quote() -> None:
    policy = CounterfactualAccountingCostPolicy()
    entry = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    exit_time = entry + timedelta(minutes=15)
    jpy = accounting_cost_r(policy=policy, symbol="USDJPY", entry_time=entry, exit_time=exit_time)
    eur = accounting_cost_r(policy=policy, symbol="EURUSD", entry_time=entry, exit_time=exit_time)
    assert eur - jpy == pytest.approx(0.0050)


def test_naive_datetimes_are_rejected() -> None:
    policy = CounterfactualAccountingCostPolicy()
    naive = datetime(2026, 9, 1, 12, 0)
    with pytest.raises(ReplayDataError):
        accounting_cost_r(policy=policy, symbol="USDJPY", entry_time=naive, exit_time=naive)


def test_non_positive_interval_charges_no_rollover() -> None:
    entry = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert rollovers_crossed(entry, entry) == 0
    assert rollovers_crossed(entry, entry - timedelta(hours=1)) == 0


def test_validate_rejects_negative_bps() -> None:
    with pytest.raises(ReplayDataError):
        CounterfactualAccountingCostPolicy(commission_r_bps=-1).validate()


def test_breakdown_carries_contract_and_layers() -> None:
    policy = CounterfactualAccountingCostPolicy()
    entry = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    breakdown = cost_breakdown(
        policy=policy,
        symbol="USDJPY",
        entry_time=entry,
        exit_time=entry + timedelta(minutes=15),
    )
    assert breakdown["contract"] == ACCOUNTING_COST_CONTRACT
    observed = breakdown["observed_l1_spread_r"]
    assert isinstance(observed, dict)
    assert observed["charged_here"] is False
