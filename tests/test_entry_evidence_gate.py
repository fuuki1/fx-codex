from __future__ import annotations

import pytest

from fx_intel.entry_evidence_gate import (
    DEFAULT_ACTION,
    GATE_CONTRACT,
    EntryEvidence,
    evaluate_entry_evidence,
)


def _passing() -> EntryEvidence:
    """全要件を満たす立証(現時点の実データには存在しない仮想例)。"""
    return EntryEvidence(
        label="hypothetical",
        in_sample_net_r=0.05,
        out_of_sample_net_r=0.04,
        out_of_sample_samples=400,
        out_of_sample_z=4.5,
        out_of_sample_block_z=4.2,
        out_of_sample_market_days=30,
        max_block_share=0.10,
        candidates_searched=1,
    )


def test_default_action_is_abstain_when_no_evidence() -> None:
    decision = evaluate_entry_evidence(None)
    assert decision.action == DEFAULT_ACTION
    assert decision.abstained is True
    assert decision.reasons == ["立証が提出されていない"]


def test_complete_evidence_permits_entry() -> None:
    decision = evaluate_entry_evidence(_passing())
    assert decision.action == "enter"
    assert decision.abstained is False
    assert decision.reasons == []


@pytest.mark.parametrize(
    "field",
    [
        "in_sample_net_r",
        "out_of_sample_net_r",
        "out_of_sample_z",
        "out_of_sample_block_z",
        "max_block_share",
    ],
)
def test_missing_measurement_fails_closed(field: str) -> None:
    """欠測は合格にしない。証拠の欠如を健全と読み替えない。"""
    evidence = dataclass_replace(_passing(), **{field: None})
    decision = evaluate_entry_evidence(evidence)
    assert decision.action == DEFAULT_ACTION
    assert any("—" in reason for reason in decision.reasons)


def test_insufficient_out_of_sample_samples_abstains() -> None:
    evidence = dataclass_replace(_passing(), out_of_sample_samples=199)
    decision = evaluate_entry_evidence(evidence)
    assert decision.action == DEFAULT_ACTION
    assert any("サンプル不足" in reason for reason in decision.reasons)


def test_negative_out_of_sample_abstains_even_when_in_sample_positive() -> None:
    """探索側だけ正の候補(単一レジームのエピソード)を弾く。"""
    evidence = dataclass_replace(
        _passing(), in_sample_net_r=0.0387, out_of_sample_net_r=-0.0082, out_of_sample_z=-0.47
    )
    decision = evaluate_entry_evidence(evidence)
    assert decision.action == DEFAULT_ACTION
    assert any("out-of-sample純R期待値が正でない" in reason for reason in decision.reasons)


def test_time_clustered_evidence_abstains_despite_strong_naive_z() -> None:
    """実測: USDJPY 1h short gross は naive z=+3.26 だが日ブロックでは -1.63。

    行単位のzだけでは通ってしまう提案を、ブロックzが止めることを固定する。
    2026-09-04 baseline: 15日中4日のみ正、08-18(n=174)と09-02(n=130)が牽引。
    """
    evidence = EntryEvidence(
        label="USDJPY-1h-short-gross",
        in_sample_net_r=0.0272,
        out_of_sample_net_r=0.0501,
        out_of_sample_samples=813,
        out_of_sample_z=3.26,
        out_of_sample_block_z=-1.63,
        out_of_sample_market_days=15,
        max_block_share=0.611,
        candidates_searched=8,
    )
    decision = evaluate_entry_evidence(evidence)
    assert decision.action == DEFAULT_ACTION
    assert any("ブロック単位の有意性不足" in reason for reason in decision.reasons)
    assert any("独立ブロック不足" in reason for reason in decision.reasons)
    assert any("単一ブロックへの集中" in reason for reason in decision.reasons)


def test_single_week_concentration_abstains() -> None:
    """実測: W34候補は441行中82%が単一週。行単位では正に見えた。"""
    evidence = dataclass_replace(
        _passing(),
        out_of_sample_samples=441,
        max_block_share=0.821,
        out_of_sample_market_days=11,
    )
    decision = evaluate_entry_evidence(evidence)
    assert decision.action == DEFAULT_ACTION
    assert any("単一ブロックへの集中" in reason for reason in decision.reasons)


def test_search_count_raises_the_significance_bar() -> None:
    """探索候補が増えるほど要求|z|が上がる(Bonferroni)。"""
    single = evaluate_entry_evidence(dataclass_replace(_passing(), candidates_searched=1))
    many = evaluate_entry_evidence(dataclass_replace(_passing(), candidates_searched=404))
    assert single.required_abs_z is not None and many.required_abs_z is not None
    assert many.required_abs_z > single.required_abs_z
    assert single.action == "enter"
    # z=4.5 は K=404 の要求(約3.84)を超えるので通過する
    assert many.action == "enter"
    stricter = evaluate_entry_evidence(
        dataclass_replace(_passing(), out_of_sample_z=3.0, candidates_searched=404)
    )
    assert stricter.action == DEFAULT_ACTION


def test_positive_z_required_not_merely_large_magnitude() -> None:
    """|z|が大きくても符号が負なら不合格。"""
    evidence = dataclass_replace(_passing(), out_of_sample_z=-9.0)
    decision = evaluate_entry_evidence(evidence)
    assert decision.action == DEFAULT_ACTION


def test_measured_best_cell_is_rejected() -> None:
    """実測: USDJPY 1h short は gross 正だが net -0.0036 なので建てられない。

    2026-09-04 baseline 20260904T023814Z-23bd824de790 (n=813)。
    grossで通そうとしても、このゲートはnetしか受け取らない。
    """
    evidence = EntryEvidence(
        label="USDJPY-1h-short",
        in_sample_net_r=-0.0036,
        out_of_sample_net_r=-0.0036,
        out_of_sample_samples=813,
        out_of_sample_z=-0.32,
        out_of_sample_block_z=-2.57,
        out_of_sample_market_days=15,
        max_block_share=0.611,
        candidates_searched=8,
    )
    decision = evaluate_entry_evidence(evidence)
    assert decision.action == DEFAULT_ACTION
    assert len(decision.reasons) >= 2


def test_measured_population_baseline_is_rejected() -> None:
    """実測: 母集団全体 net -0.0614 (z=-16.14) は当然ABSTAIN。"""
    evidence = EntryEvidence(
        label="population-baseline",
        in_sample_net_r=-0.0713,
        out_of_sample_net_r=-0.0466,
        out_of_sample_samples=2267,
        out_of_sample_z=-7.47,
        out_of_sample_block_z=-4.45,
        out_of_sample_market_days=18,
        max_block_share=0.586,
        candidates_searched=1,
    )
    decision = evaluate_entry_evidence(evidence)
    assert decision.action == DEFAULT_ACTION


def test_decision_serializes_with_contract() -> None:
    payload = evaluate_entry_evidence(None).to_dict()
    assert payload["contract"] == GATE_CONTRACT
    assert payload["action"] == DEFAULT_ACTION


def dataclass_replace(evidence: EntryEvidence, **changes: object) -> EntryEvidence:
    import dataclasses

    return dataclasses.replace(evidence, **changes)  # type: ignore[arg-type]
