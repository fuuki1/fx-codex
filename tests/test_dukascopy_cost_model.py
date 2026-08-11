"""dukascopy historical cost modelの契約テスト。

このcost modelの存在意義は「実測spreadでnet-Rを測れるが、執行可能とは名乗らない」
という区別を型と契約で固定することにある。したがってテストの主眼は
「net-Rが計算できること」と「executableに昇格しないこと」の両立である。
"""

from __future__ import annotations

from fx_intel.evaluation_labels import (
    COST_MODEL_QUOTE_SOURCES,
    DEFAULT_COMMISSION_MODEL_ID,
    DEFAULT_COST_MODEL_VERSION,
    DEFAULT_COST_STATUS,
    DEFAULT_SLIPPAGE_MODEL_ID,
    DUKASCOPY_HISTORICAL_COST_MODEL_ID,
    DUKASCOPY_HISTORICAL_COST_STATUS,
    DUKASCOPY_HISTORICAL_QUOTE_SOURCE,
    FIXTURE_COST_MODEL_ID,
    KNOWN_EXECUTABLE_COST_MODEL_IDS,
    KNOWN_MEASURED_NONEXECUTABLE_COST_MODEL_IDS,
    CostModelResult,
    cost_model_contract_flags,
    executable_cost_model_id_for_source,
    measured_cost_model_id_for_source,
)


def _dukascopy_cost(**overrides: object) -> CostModelResult:
    payload: dict[str, object] = {
        "cost_model_id": DUKASCOPY_HISTORICAL_COST_MODEL_ID,
        "cost_model_version": DEFAULT_COST_MODEL_VERSION,
        "entry_quote_source": DUKASCOPY_HISTORICAL_QUOTE_SOURCE,
        "spread_source": DUKASCOPY_HISTORICAL_QUOTE_SOURCE,
        "slippage_model_id": DEFAULT_SLIPPAGE_MODEL_ID,
        "commission_model_id": DEFAULT_COMMISSION_MODEL_ID,
        "cost_status": DUKASCOPY_HISTORICAL_COST_STATUS,
        "quality_flags": ("historical_download_not_tradable",),
    }
    payload.update(overrides)
    return CostModelResult(**payload)  # type: ignore[arg-type]


def test_dukascopy_model_is_never_treated_as_executable() -> None:
    """執行可能セットに混入しないことが、このモデルの最重要の性質。"""

    assert DUKASCOPY_HISTORICAL_COST_MODEL_ID not in KNOWN_EXECUTABLE_COST_MODEL_IDS
    assert DUKASCOPY_HISTORICAL_COST_MODEL_ID in KNOWN_MEASURED_NONEXECUTABLE_COST_MODEL_IDS
    assert not (KNOWN_EXECUTABLE_COST_MODEL_IDS & KNOWN_MEASURED_NONEXECUTABLE_COST_MODEL_IDS)


def test_executable_resolver_refuses_dukascopy_source() -> None:
    """executable resolverはhistorical sourceを解決してはいけない。"""

    assert executable_cost_model_id_for_source(DUKASCOPY_HISTORICAL_QUOTE_SOURCE) is None
    # 一方でexecutableなsourceは従来どおり解決できる(退行防止)。
    assert executable_cost_model_id_for_source("fixture") == FIXTURE_COST_MODEL_ID


def test_measured_resolver_accepts_both_tiers() -> None:
    """measured resolverはhistoricalもexecutableも解決する。"""

    assert (
        measured_cost_model_id_for_source(DUKASCOPY_HISTORICAL_QUOTE_SOURCE)
        == DUKASCOPY_HISTORICAL_COST_MODEL_ID
    )
    assert measured_cost_model_id_for_source("fixture") == FIXTURE_COST_MODEL_ID
    assert measured_cost_model_id_for_source("unknown_feed") is None


def test_dukascopy_cost_passes_contract_with_its_own_status_and_flag() -> None:
    """historical固有のstatusと品質フラグは契約違反にしない=net-Rを計算できる。"""

    assert cost_model_contract_flags(_dukascopy_cost()) == ()


def test_dukascopy_cost_rejects_executable_status() -> None:
    """executableのstatusを騙ると契約違反になる(昇格の抜け道を塞ぐ)。"""

    flags = cost_model_contract_flags(_dukascopy_cost(cost_status=DEFAULT_COST_STATUS))
    assert "cost_status_mismatch" in flags


def test_unexpected_quality_flag_still_violates_contract() -> None:
    """想定済みフラグ以外は従来どおり違反として検出する(緩めすぎ防止)。"""

    flags = cost_model_contract_flags(
        _dukascopy_cost(quality_flags=("historical_download_not_tradable", "stale_quote"))
    )
    assert "declared_cost_quality_flag" in flags


def test_executable_models_still_reject_any_quality_flag() -> None:
    """既存のexecutableモデルの厳しさは一切緩めない(退行防止)。"""

    cost = CostModelResult(
        cost_model_id=FIXTURE_COST_MODEL_ID,
        cost_model_version=DEFAULT_COST_MODEL_VERSION,
        entry_quote_source="fixture",
        spread_source="fixture",
        slippage_model_id=DEFAULT_SLIPPAGE_MODEL_ID,
        commission_model_id=DEFAULT_COMMISSION_MODEL_ID,
        cost_status=DEFAULT_COST_STATUS,
        quality_flags=("historical_download_not_tradable",),
    )
    assert "declared_cost_quality_flag" in cost_model_contract_flags(cost)


def test_every_registered_source_set_is_disjoint() -> None:
    """source→cost modelの解決が一意であることを保証する。"""

    seen: set[str] = set()
    for sources in COST_MODEL_QUOTE_SOURCES.values():
        assert not (seen & set(sources))
        seen |= set(sources)
