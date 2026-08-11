from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from fx_intel.canonical_label_readiness import (
    assess_canonical_label_readiness,
    canonical_label_projection,
)
from fx_intel.canonical_label_evidence import (
    InvalidCanonicalLabelEvidence,
    canonical_close_payload_from_virtual_row,
    canonical_label_evidence_from_virtual_row,
    validate_canonical_label_evidence,
    validate_evidence_binding,
)
from fx_intel.canonical_label_evidence_store import (
    CanonicalLabelEvidenceConflict,
    CanonicalLabelEvidenceStoreCorruption,
    append_canonical_label_evidence,
    load_canonical_label_evidence,
    verify_canonical_label_evidence_store,
)
from fx_intel.canonical_outcome_store import append_canonical_outcomes
from fx_intel.evaluation_labels import (
    DEFAULT_COMMISSION_MODEL_ID,
    DEFAULT_COST_MODEL_VERSION,
    DEFAULT_COST_STATUS,
    DEFAULT_SLIPPAGE_MODEL_ID,
    FIXTURE_COST_MODEL_ID,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
)
from fx_intel.trade_outcome import CanonicalTradeOutcome

NOW = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "decision_id": "decision-1",
        "label_version": NET_LABEL_VERSION,
        "label_provenance": NET_LABEL_PROVENANCE,
        "symbol": "USDJPY",
        "mode": "fixture",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "direction": "long",
        "prediction_time": NOW.isoformat(),
        "holding_end_time": (NOW + timedelta(hours=1)).isoformat(),
        "first_touch": "terminal",
        "first_touch_ts": (NOW + timedelta(hours=1)).isoformat(),
        "entry_bid": 100.0,
        "entry_ask": 100.2,
        "entry_executable": 100.2,
        "exit_bid": 100.5,
        "exit_ask": 100.7,
        "exit_executable": 100.5,
        "exit_mid": 100.6,
        "planned_risk_distance": 1.0,
        "gross_realized_r": 0.5,
        "quote_realized_r": 0.3,
        "realized_spread_cost_r": 0.2,
        "slippage_r": 0.0,
        "commission_r": 0.0,
        "financing_r": 0.0,
        "additional_cost_r": 0.0,
        "execution_cost_r": 0.2,
        "realized_net_r": 0.3,
        "entry_spread_r": 0.2,
        "cost_model_id": FIXTURE_COST_MODEL_ID,
        "cost_model_version": DEFAULT_COST_MODEL_VERSION,
        "cost_status": DEFAULT_COST_STATUS,
        "entry_quote_source": "fixture_quotes",
        "spread_source": "fixture_quotes",
        "slippage_model_id": DEFAULT_SLIPPAGE_MODEL_ID,
        "commission_model_id": DEFAULT_COMMISSION_MODEL_ID,
        "cost_quality_flags": [],
        "net_label_eligible": True,
        "path_quality": 1.0,
        "quality_flags": [],
        "mae_r": 0.1,
        "mfe_r": 0.6,
    }
    row.update(overrides)
    return row


def _stored_outcome(**overrides: object) -> CanonicalTradeOutcome:
    row = _canonical_row(**overrides)
    allowed = {field.name for field in fields(CanonicalTradeOutcome)}
    prepared = {key: value for key, value in row.items() if key in allowed}
    prepared["quality_flags"] = tuple(prepared.get("quality_flags", ()))
    prepared["cost_quality_flags"] = tuple(prepared.get("cost_quality_flags", ()))
    return CanonicalTradeOutcome(**prepared)


def _virtual_v3_row() -> dict[str, object]:
    holding_end = NOW + timedelta(hours=1)
    return {
        "trade_id": "virtual-trade-1",
        "decision_id": "virtual-decision-1",
        "prediction_time": NOW.isoformat(),
        "simulated_entry_time": NOW.isoformat(),
        "simulated_entry_available_time": NOW.isoformat(),
        "simulated_entry_knowledge_basis": "test_fixture",
        "label_end_time": holding_end.isoformat(),
        "label_available_time": holding_end.isoformat(),
        "label_ingested_time": holding_end.isoformat(),
        "label_knowledge_basis": "test_fixture",
        "symbol": "USDJPY",
        "timeframe": "1h",
        "direction": "long",
        "planned_risk_jpy": 5_000,
        "entry_bid": 100.0,
        "entry_ask": 100.001,
        "entry_mid": 100.0005,
        "entry_executable": 100.001,
        "stop_price": 99.501,
        "target_price": 101.001,
        "units": 10_000.0,
        "entry_quote_to_jpy_rate": 1.0,
        "entry_quote_conversion_source": "JPY quote currency",
        "exit_bid": 100.012,
        "exit_ask": 100.013,
        "exit_mid": 100.0125,
        "exit_executable": 100.012,
        "exit_quote_to_jpy_rate": 1.0,
        "exit_quote_conversion_source": "JPY quote currency",
        "entry_quote": {
            "source": "fixture_quotes",
            "source_record_id": "entry-quote-1",
            "revision_id": "entry-revision-1",
            "event_time": NOW.isoformat(),
            "available_time": NOW.isoformat(),
            "ingested_time": NOW.isoformat(),
            "bid": 100.0,
            "ask": 100.001,
        },
        "exit_quote": {
            "source": "fixture_quotes",
            "source_record_id": "exit-quote-1",
            "revision_id": "exit-revision-1",
            "event_time": holding_end.isoformat(),
            "available_time": holding_end.isoformat(),
            "ingested_time": holding_end.isoformat(),
            "bid": 100.012,
            "ask": 100.013,
        },
        "decision_record_sha256": "d" * 64,
        "open_record_sha256": "e" * 64,
        "open_observation_sha256": "1" * 64,
        "close_record_sha256": "f" * 64,
        "close_observation_sha256": "0" * 64,
        "net_pnl_jpy": 100,
        "realized_net_r": 0.02,
        "attribution": {
            "gross_market_pnl_jpy": 120,
            "spread_quote_cost_jpy": 10,
            "slippage_cost_jpy": 5,
            "commission_jpy": 5,
            "financing_jpy": 0,
            "conversion_cost_jpy": 0,
            "net_pnl_jpy": 100,
            "cost_contract": {
                "cost_model_id": "explicit-test-costs-v1",
                "cost_model_version": "1",
                "cost_source": "test-fixture",
                "costs_complete": True,
            },
            "execution_observation_mode": "contemporaneous_quote",
            "replay_evidence": {},
        },
        "close_reason": "time_exit",
        "failures": [],
    }


def _virtual_v3_evidence(
    *,
    hash_bound_path: bool,
    raw_payload_hash: bool = True,
) -> tuple[dict[str, object], object, object]:
    from fx_intel.virtual_portfolio_net_labels import (
        canonical_outcome_from_virtual_row,
        canonical_outcome_sha256,
    )

    row = _virtual_v3_row()
    row["maximum_adverse_excursion_r"] = 0.15
    row["maximum_favorable_excursion_r"] = 0.35
    if hash_bound_path:
        attribution = dict(row["attribution"])
        attribution["execution_observation_mode"] = "delayed_historical_bid_ask_replay"
        replay_evidence = {
            "contract": "virtual-portfolio-delayed-bid-ask-replay-v1",
            "source_size_bytes": 123,
            "source_prefix_sha256": "c" * 64,
            "path_tick_count_to_exit": 4,
            "path_source_ids_sha256": "a" * 64,
        }
        if raw_payload_hash:
            replay_evidence.update(
                {
                    "label_path_evidence_contract": "canonical-label-path-evidence-v1",
                    "path_raw_payload_hashes_sha256": "b" * 64,
                }
            )
        attribution["replay_evidence"] = replay_evidence
        row["attribution"] = attribution
    preliminary_outcome = canonical_outcome_from_virtual_row(row)
    row["close_record_sha256"] = _sha256(
        canonical_close_payload_from_virtual_row(row, preliminary_outcome)
    )
    outcome = canonical_outcome_from_virtual_row(row)
    payload = outcome.to_dict()
    row["canonical_outcome"] = payload
    row["canonical_outcome_sha256"] = canonical_outcome_sha256(outcome)
    evidence = canonical_label_evidence_from_virtual_row(row, outcome)
    return row, outcome, evidence


def _v2_attestation(evidence):
    return replace(
        evidence,
        evidence_version="canonical-label-evidence-v2",
        label_quality="raw_replayed_research_path",
        path_verification_contract="canonical-label-raw-path-verification-v1",
        quote_log_prefix_size_bytes=123,
        quote_log_prefix_sha256="c" * 64,
        raw_blob_count=1,
        raw_blob_hashes_sha256="d" * 64,
    )


def test_projection_uses_target_names_and_derives_elapsed_time() -> None:
    projection, sources = canonical_label_projection(_canonical_row())

    assert projection["gross_r"] == 0.5
    assert projection["spread_cost_r"] == 0.2
    assert projection["net_r"] == 0.3
    assert projection["holding_seconds"] == 3600.0
    assert projection["time_to_tp"] is None
    assert sources["holding_seconds"] == "derived:prediction_time-holding_end_time"
    assert sources["time_to_tp"] == "not_applicable"


def test_projection_derives_spread_without_treating_missing_cost_as_zero() -> None:
    projection, sources = canonical_label_projection(_canonical_row(realized_spread_cost_r=None))
    assert projection["spread_cost_r"] == 0.2
    assert sources["spread_cost_r"] == "derived:gross_realized_r-quote_realized_r"

    unavailable, unavailable_sources = canonical_label_projection(
        _canonical_row(realized_spread_cost_r=None, quote_realized_r=None)
    )
    assert unavailable["spread_cost_r"] is None
    assert unavailable_sources["spread_cost_r"] == "unavailable"


def test_verified_store_shape_stays_incomplete_without_canonical_excursions() -> None:
    report = assess_canonical_label_readiness([_stored_outcome()], source_verified=True)

    assert report.status == "incomplete"
    assert report.phase2_complete is False
    assert report.source_verified is True
    assert report.net_r_rows == 1
    assert report.nonzero_net_r_rows == 1
    assert report.completion_conditions["entry_exit_bid_ask_side_verified"] is True
    assert report.completion_conditions["cost_breakdown_recomputable"] is True
    assert report.completion_conditions["target_label_fields_complete"] is False
    assert report.missing_reasons["field_unavailable:mae_r"] == 1
    assert report.missing_reasons["field_unavailable:mfe_r"] == 1


def test_missing_exit_sides_and_excursions_fail_closed() -> None:
    report = assess_canonical_label_readiness(
        [_stored_outcome(exit_bid=None, exit_ask=None)],
        source_verified=True,
    )

    assert report.status == "incomplete"
    assert report.phase2_complete is False
    assert report.completion_conditions["entry_exit_bid_ask_side_verified"] is False
    assert report.completion_conditions["target_label_fields_complete"] is False
    assert report.missing_reasons["missing_exit_bid_ask"] == 1
    assert report.missing_reasons["field_unavailable:mae_r"] == 1
    assert report.missing_reasons["field_unavailable:mfe_r"] == 1


def test_invalid_contract_is_not_counted_as_canonical_evidence() -> None:
    report = assess_canonical_label_readiness(
        [_stored_outcome(net_label_eligible=False, realized_net_r=None)],
        source_verified=True,
    )

    assert report.status == "evaluation_unavailable"
    assert report.canonical_rows == 0
    assert report.phase2_complete is False
    assert report.missing_reasons["contract:net_label_not_eligible"] == 1


def test_finite_zero_net_r_counts_as_present_but_remains_diagnostic() -> None:
    report = assess_canonical_label_readiness(
        [
            _stored_outcome(
                gross_realized_r=0.2,
                quote_realized_r=0.0,
                realized_net_r=0.0,
                execution_cost_r=0.2,
                exit_bid=100.2,
                exit_ask=100.4,
                exit_executable=100.2,
                exit_mid=100.3,
            )
        ],
        source_verified=True,
    )

    assert report.status == "incomplete"
    assert report.net_r_rows == 1
    assert report.nonzero_net_r_rows == 0
    assert report.completion_conditions["net_r_present"] is True


def test_unverified_mapping_can_never_claim_readiness() -> None:
    report = assess_canonical_label_readiness([_canonical_row()], source_verified=False)

    assert report.status == "evaluation_unavailable"
    assert report.source_verified is False
    assert report.phase2_complete is False
    assert report.missing_reasons["source_not_verified"] == 1


def test_quote_accounting_mismatch_fails_even_when_gross_net_identity_matches() -> None:
    report = assess_canonical_label_readiness(
        [
            _stored_outcome(
                quote_realized_r=0.4,
                realized_spread_cost_r=0.1,
                execution_cost_r=0.1,
                realized_net_r=0.4,
            )
        ],
        source_verified=True,
    )

    assert report.phase2_complete is False
    assert report.completion_conditions["entry_exit_bid_ask_side_verified"] is False


def test_touch_after_holding_end_fails_timeline_contract() -> None:
    report = assess_canonical_label_readiness(
        [
            _stored_outcome(
                first_touch="tp1",
                first_touch_ts=(NOW + timedelta(hours=2)).isoformat(),
            )
        ],
        source_verified=True,
    )

    assert report.phase2_complete is False
    assert report.completion_conditions["label_timeline_valid"] is False
    assert report.missing_reasons["label_timeline_not_recomputable"] == 1
    assert report.missing_reasons["field_unavailable:time_to_tp"] == 1


def test_immediate_touch_has_zero_elapsed_seconds() -> None:
    outcome = _stored_outcome(first_touch="tp1", first_touch_ts=NOW.isoformat())
    projection, sources = canonical_label_projection(outcome)
    report = assess_canonical_label_readiness([outcome], source_verified=True)

    assert projection["time_to_tp"] == 0.0
    assert sources["time_to_tp"] == "derived:prediction_time-first_touch_ts"
    assert report.completion_conditions["label_timeline_valid"] is True


def test_virtual_v3_vocabulary_and_conversion_accounting_are_supported() -> None:
    from fx_intel.virtual_portfolio_net_labels import canonical_outcome_from_virtual_row

    outcome = canonical_outcome_from_virtual_row(_virtual_v3_row())
    report = assess_canonical_label_readiness([outcome], source_verified=True)
    projection, sources = canonical_label_projection(outcome)

    assert outcome.first_touch == "time_exit"
    assert report.source_verified is True
    assert report.completion_conditions["entry_exit_bid_ask_side_verified"] is True
    assert report.completion_conditions["cost_breakdown_recomputable"] is True
    assert report.completion_conditions["label_timeline_valid"] is True
    assert projection["time_to_tp"] is None
    assert projection["time_to_sl"] is None
    assert sources["time_to_tp"] == "not_applicable"
    assert sources["time_to_sl"] == "not_applicable"


def test_cli_rejects_report_store_path_collision_without_mutation(tmp_path) -> None:
    from tools import canonical_label_readiness as readiness_cli

    store = tmp_path / "canonical.jsonl"
    original = b"immutable-store-bytes\n"
    store.write_bytes(original)

    with pytest.raises(SystemExit) as raised:
        readiness_cli.main(["--store", str(store), "--report", str(store)])

    assert raised.value.code == 2
    assert store.read_bytes() == original


def test_versioned_evidence_persists_hash_bound_path_fields() -> None:
    _row, outcome, evidence = _virtual_v3_evidence(hash_bound_path=True)

    assert evidence.evidence_version == "canonical-label-evidence-v1"
    assert evidence.canonical_outcome_sha256
    assert evidence.exit_bid == 100.012
    assert evidence.exit_ask == 100.013
    assert evidence.mae_r == 0.15
    assert evidence.mfe_r == 0.35
    assert evidence.holding_seconds == 3600.0
    assert evidence.time_to_tp is None
    assert evidence.time_to_sl is None
    assert evidence.label_quality == "hash_bound_unrecomputed_path"
    assert evidence.path_tick_count == 4
    assert evidence.path_raw_payload_hashes_sha256 == "b" * 64
    assert "path_verification_contract" not in evidence.to_dict()
    assert "quote_log_prefix_sha256" not in evidence.to_dict()
    validate_evidence_binding(evidence, outcome)


def test_contemporaneous_close_does_not_claim_verified_excursion_path() -> None:
    _row, _outcome, evidence = _virtual_v3_evidence(hash_bound_path=False)

    assert evidence.mae_r == 0.15
    assert evidence.mfe_r == 0.35
    assert evidence.label_quality == "incomplete"
    assert "excursion_path_not_verified" in evidence.quality_flags


def test_legacy_replay_without_raw_payload_hash_chain_stays_incomplete() -> None:
    _row, _outcome, evidence = _virtual_v3_evidence(
        hash_bound_path=True,
        raw_payload_hash=False,
    )

    assert evidence.mae_r == 0.15
    assert evidence.mfe_r == 0.35
    assert evidence.label_quality == "incomplete"
    assert "raw_path_hash_not_available" in evidence.quality_flags


def test_hash_bound_but_unrecomputed_path_cannot_complete_phase2_gate() -> None:
    _row, outcome, evidence = _virtual_v3_evidence(hash_bound_path=True)

    report = assess_canonical_label_readiness(
        [outcome],
        source_verified=True,
        label_evidence=[evidence],
        evidence_source_verified=True,
    )

    assert report.status == "incomplete"
    assert report.phase2_complete is False
    assert report.evidence_coverage == 0.0
    assert report.missing_reasons["missing_label_evidence"] == 1
    assert report.completion_conditions["label_quality_verified"] is False
    assert report.field_coverage["mae_r"] == 0.0
    assert report.field_coverage["mfe_r"] == 0.0


def test_readiness_selects_current_evidence_version_during_side_by_side_migration() -> None:
    _row, outcome, legacy = _virtual_v3_evidence(hash_bound_path=True)
    evidence = _v2_attestation(legacy)

    report = assess_canonical_label_readiness(
        [outcome],
        source_verified=True,
        label_evidence=[legacy, evidence],
        evidence_source_verified=True,
    )

    assert report.status == "incomplete"
    assert report.phase2_complete is False
    assert report.evidence_rows == 2
    assert report.evidence_versions == ("canonical-label-evidence-v2",)
    assert "duplicate_label_evidence_key" not in report.missing_reasons


def test_mismatched_evidence_hash_fails_closed() -> None:
    _row, outcome, evidence = _virtual_v3_evidence(hash_bound_path=True)
    evidence = _v2_attestation(evidence)
    changed = replace(outcome, quality_flags=(*outcome.quality_flags, "changed"))

    with pytest.raises(InvalidCanonicalLabelEvidence, match="binding hash"):
        validate_evidence_binding(evidence, changed)
    report = assess_canonical_label_readiness(
        [changed],
        source_verified=True,
        label_evidence=[evidence],
        evidence_source_verified=True,
    )
    assert report.status == "evaluation_unavailable"
    assert report.phase2_complete is False
    assert report.missing_reasons["invalid_label_evidence_binding"] == 1


def test_rehashed_sidecar_cannot_forge_excursions_outside_close_record() -> None:
    _row, outcome, evidence = _virtual_v3_evidence(hash_bound_path=True)
    forged = replace(
        _v2_attestation(evidence),
        mae_r=999.0,
        mfe_r=999.0,
    )

    with pytest.raises(InvalidCanonicalLabelEvidence, match="close payload binding"):
        validate_canonical_label_evidence(forged)
    report = assess_canonical_label_readiness(
        [outcome],
        source_verified=True,
        label_evidence=[forged],
        evidence_source_verified=True,
    )
    assert report.status == "evaluation_unavailable"
    assert report.phase2_complete is False
    assert report.missing_reasons["invalid_label_evidence_binding"] == 1


def test_rehashed_close_cannot_claim_recomputed_path_extrema() -> None:
    from fx_intel.virtual_portfolio_net_labels import (
        canonical_outcome_from_virtual_row,
        canonical_outcome_sha256,
    )

    row, _outcome, _evidence = _virtual_v3_evidence(hash_bound_path=True)
    row["maximum_adverse_excursion_r"] = 999.0
    row["maximum_favorable_excursion_r"] = 999.0
    preliminary = canonical_outcome_from_virtual_row(row)
    row["close_record_sha256"] = _sha256(canonical_close_payload_from_virtual_row(row, preliminary))
    outcome = canonical_outcome_from_virtual_row(row)
    row["canonical_outcome"] = outcome.to_dict()
    row["canonical_outcome_sha256"] = canonical_outcome_sha256(outcome)
    evidence = canonical_label_evidence_from_virtual_row(row, outcome)
    report = assess_canonical_label_readiness(
        [outcome],
        source_verified=True,
        label_evidence=[evidence],
        evidence_source_verified=True,
    )

    assert evidence.mae_r == 999.0
    assert evidence.mfe_r == 999.0
    assert evidence.label_quality == "hash_bound_unrecomputed_path"
    assert report.status == "incomplete"
    assert report.phase2_complete is False
    assert report.completion_conditions["label_quality_verified"] is False


def test_evidence_store_is_hash_verified_idempotent_and_conflict_safe(tmp_path) -> None:
    _row, _outcome, evidence = _virtual_v3_evidence(hash_bound_path=True)
    path = tmp_path / "canonical_label_evidence.jsonl"

    assert append_canonical_label_evidence(path, [evidence]) == 1
    assert append_canonical_label_evidence(path, [evidence]) == 0
    assert load_canonical_label_evidence(path) == [evidence]
    audit = verify_canonical_label_evidence_store(path)
    assert audit.entry_count == 1
    assert len(audit.store_sha256) == 64
    with pytest.raises(CanonicalLabelEvidenceConflict):
        append_canonical_label_evidence(
            path,
            [replace(evidence, quality_flags=(*evidence.quality_flags, "changed"))],
        )
    with pytest.raises(CanonicalLabelEvidenceStoreCorruption, match="v2-only fields"):
        append_canonical_label_evidence(
            tmp_path / "invalid_legacy.jsonl",
            [replace(evidence, path_verification_contract="forged")],
        )

    record = json.loads(path.read_text(encoding="utf-8"))
    record["evidence"]["mae_r"] = 999.0
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(CanonicalLabelEvidenceStoreCorruption, match="hash mismatch"):
        load_canonical_label_evidence(path)

    invalid_utf8 = tmp_path / "invalid_utf8_evidence.jsonl"
    invalid_utf8.write_bytes(b"\xff\n")
    with pytest.raises(CanonicalLabelEvidenceStoreCorruption, match="invalid UTF-8"):
        load_canonical_label_evidence(invalid_utf8)
    with pytest.raises(CanonicalLabelEvidenceStoreCorruption, match="invalid UTF-8"):
        append_canonical_label_evidence(invalid_utf8, [evidence])


def test_cli_requires_and_verifies_both_append_only_stores(tmp_path, capsys) -> None:
    from tools import canonical_label_readiness as readiness_cli

    _row, outcome, evidence = _virtual_v3_evidence(hash_bound_path=True)
    outcome_store = tmp_path / "canonical_outcomes.jsonl"
    evidence_store = tmp_path / "canonical_label_evidence.jsonl"
    append_canonical_outcomes(outcome_store, [outcome])
    append_canonical_label_evidence(evidence_store, [evidence])

    exit_code = readiness_cli.main(
        [
            "--store",
            str(outcome_store),
            "--label-evidence-store",
            str(evidence_store),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == readiness_cli.EXIT_INCOMPLETE
    assert payload["phase2_complete"] is False
    assert payload["store_verification"]["verified"] is True
    assert payload["evidence_store_verification"]["verified"] is True


def test_dual_store_preflight_rejects_evidence_conflict_before_outcome_mutation(
    tmp_path,
) -> None:
    from fx_intel.virtual_portfolio_net_labels import (
        canonical_outcome_from_virtual_row,
        canonical_outcome_sha256,
    )
    from tools.virtual_portfolio_canonical_outcomes import _preflight_existing_stores

    _row, outcome, evidence = _virtual_v3_evidence(hash_bound_path=True)
    conflict_row, _old_outcome, _old_evidence = _virtual_v3_evidence(hash_bound_path=True)
    conflict_row["maximum_adverse_excursion_r"] = 0.25
    conflict_row["maximum_favorable_excursion_r"] = 0.45
    preliminary = canonical_outcome_from_virtual_row(conflict_row)
    conflict_row["close_record_sha256"] = _sha256(
        canonical_close_payload_from_virtual_row(conflict_row, preliminary)
    )
    conflict_outcome = canonical_outcome_from_virtual_row(conflict_row)
    conflict_row["canonical_outcome"] = conflict_outcome.to_dict()
    conflict_row["canonical_outcome_sha256"] = canonical_outcome_sha256(conflict_outcome)
    conflict_evidence = canonical_label_evidence_from_virtual_row(
        conflict_row,
        conflict_outcome,
    )
    outcome_store = tmp_path / "must_not_be_created.jsonl"
    evidence_store = tmp_path / "preexisting_evidence.jsonl"
    append_canonical_label_evidence(evidence_store, [conflict_evidence])

    with pytest.raises(CanonicalLabelEvidenceConflict):
        _preflight_existing_stores(
            store_path=outcome_store,
            outcomes=[outcome],
            label_evidence_store_path=evidence_store,
            evidence_rows=[evidence],
        )

    assert outcome_store.exists() is False
