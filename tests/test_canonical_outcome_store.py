"""正準outcome永続層の一意性・破損検出・export hash反証テスト。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, UTC
import json

import pytest

from fx_intel.canonical_outcome_store import (
    CanonicalOutcomeStoreCorruption,
    CanonicalOutcomeStoreUnavailable,
    append_canonical_outcomes,
    export_canonical_outcomes,
    load_canonical_outcomes,
    verify_canonical_outcome_store,
)
from fx_intel.evaluation_labels import (
    DEFAULT_COST_MODEL_ID,
    DEFAULT_COST_STATUS,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
)
from fx_intel.trade_outcome import CanonicalOutcomeConflict, CanonicalTradeOutcome

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


def _outcome(index: int = 1) -> CanonicalTradeOutcome:
    prediction = NOW + timedelta(hours=index)
    return CanonicalTradeOutcome(
        decision_id=f"decision-{index}",
        label_version=NET_LABEL_VERSION,
        label_provenance=NET_LABEL_PROVENANCE,
        symbol="USDJPY",
        mode="per_timeframe",
        timeframe="1h",
        horizon_hours=1.0,
        direction="long",
        prediction_time=prediction.isoformat(),
        holding_end_time=(prediction + timedelta(hours=1)).isoformat(),
        first_touch="terminal",
        first_touch_ts=(prediction + timedelta(hours=1)).isoformat(),
        gross_realized_r=0.5,
        quote_realized_r=0.4,
        planned_payoff_r=0.4,
        slippage_r=0.0,
        commission_r=0.0,
        financing_r=0.0,
        entry_spread_r=0.2,
        additional_cost_r=0.0,
        execution_cost_r=0.1,
        realized_net_r=0.4,
        cost_model_id=DEFAULT_COST_MODEL_ID,
        cost_model_version="1",
        cost_status=DEFAULT_COST_STATUS,
        net_label_eligible=True,
        path_quality=1.0,
    )


def test_identical_replay_is_idempotent_and_loads_once(tmp_path) -> None:
    path = tmp_path / "canonical.jsonl"
    outcome = _outcome()

    assert append_canonical_outcomes(path, [outcome]) == 1
    assert append_canonical_outcomes(path, [outcome]) == 0

    assert load_canonical_outcomes(path) == [outcome]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_missing_store_is_unavailable_not_verified_empty(tmp_path) -> None:
    with pytest.raises(CanonicalOutcomeStoreUnavailable, match="missing"):
        verify_canonical_outcome_store(tmp_path / "missing.jsonl")


def test_conflicting_same_key_is_hard_error(tmp_path) -> None:
    path = tmp_path / "canonical.jsonl"
    outcome = _outcome()
    append_canonical_outcomes(path, [outcome])

    with pytest.raises(CanonicalOutcomeConflict, match="decision-1.*net-r-v1"):
        append_canonical_outcomes(path, [replace(outcome, realized_net_r=-9.0)])

    assert load_canonical_outcomes(path) == [outcome]


def test_eligible_outcome_with_naive_time_is_rejected_at_store_boundary(tmp_path) -> None:
    path = tmp_path / "canonical.jsonl"

    with pytest.raises(CanonicalOutcomeStoreCorruption, match="incomplete"):
        append_canonical_outcomes(
            path,
            [replace(_outcome(), prediction_time="2026-07-23T09:00:00")],
        )

    assert path.read_text(encoding="utf-8") == ""


def test_eligible_outcome_with_broken_accounting_identity_is_rejected(tmp_path) -> None:
    path = tmp_path / "canonical.jsonl"

    with pytest.raises(CanonicalOutcomeStoreCorruption, match="incomplete"):
        append_canonical_outcomes(
            path,
            [replace(_outcome(), realized_net_r=99.0)],
        )


@pytest.mark.parametrize(
    "contents",
    [
        '{"partial":',
        "[]\n",
        (
            '{"schema_version":1,"outcome_key":["decision-1","net-r-v1"],'
            '"outcome":{},"outcome_sha256":"tampered"}\n'
        ),
    ],
)
def test_malformed_partial_or_tampered_store_is_rejected(tmp_path, contents: str) -> None:
    path = tmp_path / "canonical.jsonl"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(CanonicalOutcomeStoreCorruption):
        verify_canonical_outcome_store(path)


def test_existing_duplicate_key_is_rejected_even_when_identical(tmp_path) -> None:
    path = tmp_path / "canonical.jsonl"
    append_canonical_outcomes(path, [_outcome()])
    line = path.read_text(encoding="utf-8")
    path.write_text(line + line, encoding="utf-8")

    with pytest.raises(CanonicalOutcomeStoreCorruption, match="duplicate"):
        load_canonical_outcomes(path)


def test_verified_export_hash_matches_store_audit(tmp_path) -> None:
    path = tmp_path / "canonical.jsonl"
    export_path = tmp_path / "canonical_export.json"
    append_canonical_outcomes(path, [_outcome(2), _outcome(1)])

    audit = verify_canonical_outcome_store(path)
    exported = export_canonical_outcomes(path, export_path)
    payload = json.loads(export_path.read_text(encoding="utf-8"))

    assert exported == audit
    assert payload["canonical_outcomes_sha256"] == audit.canonical_export_sha256
    assert payload["entry_count"] == 2
    assert [row["decision_id"] for row in payload["outcomes"]] == [
        "decision-1",
        "decision-2",
    ]


def test_concurrent_append_preserves_every_unique_key(tmp_path) -> None:
    path = tmp_path / "canonical.jsonl"
    outcomes = [_outcome(index) for index in range(1, 9)]

    with ThreadPoolExecutor(max_workers=4) as executor:
        appended = list(
            executor.map(
                lambda outcome: append_canonical_outcomes(path, [outcome]),
                outcomes,
            )
        )

    assert sum(appended) == 8
    assert verify_canonical_outcome_store(path).entry_count == 8
    assert {outcome.decision_id for outcome in load_canonical_outcomes(path)} == {
        f"decision-{index}" for index in range(1, 9)
    }
