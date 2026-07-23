"""decision eventから正準scorer/storeへ接続するoffline pipelineテスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import hashlib
import json

import pytest

from tools import canonical_outcome_pipeline as pipeline_cli
from fx_intel.canonical_outcome_pipeline import (
    CanonicalOutcomePipelineError,
    build_canonical_trade_request,
    guard_counterfactual_decision_events,
    score_and_store_decision_events,
)
from fx_intel.canonical_outcome_store import load_canonical_outcomes
from fx_intel import journal
from fx_intel.evaluation_labels import (
    DEFAULT_COMMISSION_MODEL_ID,
    DEFAULT_COST_MODEL_ID,
    DEFAULT_COST_MODEL_VERSION,
    DEFAULT_COST_STATUS,
    DEFAULT_SLIPPAGE_MODEL_ID,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
)
from fx_intel.trade_outcome import score_canonical_outcome

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


def _event(**decision_overrides: object) -> dict[str, object]:
    decision = {
        "direction": "long",
        "close": 100.0,
        "stop": 99.1,
        "target1": 101.1,
        "target2": 102.1,
        "entry_bid": 99.9,
        "entry_ask": 100.1,
        "quote_observed_at": NOW.isoformat(),
        "quote_available_at": NOW.isoformat(),
        "quote_source": "fixture_quotes",
        "quote_source_record_id": "entry-1",
        "planned_risk_distance": 1.0,
        "label_version": NET_LABEL_VERSION,
        "label_provenance": NET_LABEL_PROVENANCE,
        "cost_model_id": DEFAULT_COST_MODEL_ID,
        "cost_model_version": DEFAULT_COST_MODEL_VERSION,
        "cost_status": DEFAULT_COST_STATUS,
        "slippage_model_id": DEFAULT_SLIPPAGE_MODEL_ID,
        "commission_model_id": DEFAULT_COMMISSION_MODEL_ID,
        "slippage_r": 0.0,
        "commission_r": 0.0,
        "financing_r": 0.0,
    }
    decision.update(decision_overrides)
    return {
        "decision_id": "decision-1",
        "ts": NOW.isoformat(),
        "symbol": "USDJPY",
        "mode": "per_timeframe",
        "timeframe": "1h",
        "horizon_hours": 1.0,
        "decision": decision,
    }


def _guard_event(**event_overrides: object) -> dict[str, object]:
    event = _event(
        direction="neutral",
        stop=None,
        target1=None,
        target2=None,
        planned_risk_distance=None,
    )
    decision = dict(event["decision"])
    execution = {
        key: value
        for key, value in _event()["decision"].items()
        if key
        in {
            "entry_bid",
            "entry_ask",
            "quote_observed_at",
            "quote_available_at",
            "quote_source",
            "quote_source_record_id",
            "planned_risk_distance",
            "label_version",
            "label_provenance",
            "cost_model_id",
            "cost_model_version",
            "cost_status",
            "slippage_model_id",
            "commission_model_id",
            "slippage_r",
            "commission_r",
            "financing_r",
        }
    }
    execution.update(
        {
            "canonical_net_label_input_eligible": True,
            "canonical_net_label_status": "input_ready",
        }
    )
    decision.update(
        {
            "pre_guard_direction": "long",
            "pre_guard_conviction": 60,
            "pre_guard_stop": 99.1,
            "pre_guard_target1": 101.1,
            "pre_guard_target2": 102.1,
            "pre_guard_target_policy": {
                "candidate_id": "approved-overall-tp",
                "target1_r": 1.0,
                "target2_r": 2.0,
            },
            "pre_guard_execution_snapshot": execution,
            "pre_guard_cost_model_id": DEFAULT_COST_MODEL_ID,
            "gate_trace": [{"gate": "expectancy_guard", "status": "blocked"}],
        }
    )
    event["decision"] = decision
    event.update(
        {
            "prediction_time": NOW.isoformat(),
            "source_cutoff": NOW.isoformat(),
            "max_feature_available_time": NOW.isoformat(),
            "pit_eligible": True,
            "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
            "producer": journal.TIMEFRAME_PRODUCER,
            "producer_version": journal.TIMEFRAME_PRODUCER_VERSION,
            "input_context_id": "context-1",
            "source_record_ids": ["source-1"],
        }
    )
    event.update(event_overrides)
    return event


def _bar(index: int, **overrides: object) -> dict[str, object]:
    start = NOW + timedelta(minutes=index * 5)
    end = start + timedelta(minutes=5)
    row: dict[str, object] = {
        "ts": end.isoformat(),
        "event_time": end.isoformat(),
        "available_time": end.isoformat(),
        "ingested_time": end.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "bar_start": start.isoformat(),
        "bar_end": end.isoformat(),
        "bar_granularity": "M5",
        "complete": True,
        "source": "fixture_quotes",
        "source_record_id": f"bar-{index}",
        "ohlc_scope": "completed_bid_ask_bar",
        "data_quality_flags": [],
        "path_quality": 1.0,
        "bid_open": 100.4,
        "bid_high": 100.6,
        "bid_low": 99.8,
        "bid_close": 100.5,
        "ask_open": 100.6,
        "ask_high": 100.8,
        "ask_low": 100.0,
        "ask_close": 100.7,
    }
    row.update(overrides)
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row["content_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return row


def test_complete_horizon_scores_and_persists_canonical_outcome(tmp_path) -> None:
    store = tmp_path / "canonical.jsonl"
    rows = [_bar(index) for index in range(12)]

    report = score_and_store_decision_events([_event()], rows, store)

    assert report.scored == 1
    assert report.eligible == 1
    assert report.pending == 0
    assert report.persistable == 1
    assert report.appended == 1
    outcome = load_canonical_outcomes(store)[0]
    assert outcome.first_touch == "terminal"
    assert outcome.realized_net_r == 0.4
    assert outcome.prediction_time == NOW.isoformat()
    assert outcome.holding_end_time == (NOW + timedelta(hours=1)).isoformat()

    replay = score_and_store_decision_events([_event()], list(reversed(rows)), store)
    assert replay.appended == 0


def test_guard_counterfactual_uses_canonical_scorer_and_store(tmp_path) -> None:
    event = _guard_event()
    children = guard_counterfactual_decision_events([event])

    assert len(children) == 1
    child = children[0]
    assert child["decision_id"] == f"{event['decision_id']}:pre-guard"
    assert child["parent_decision_id"] == event["decision_id"]
    assert child["decision"]["direction"] == "long"
    assert child["decision"]["target_policy"]["candidate_id"] == "approved-overall-tp"
    assert event["decision"]["direction"] == "neutral"

    store = tmp_path / "canonical.jsonl"
    report = score_and_store_decision_events(
        [event],
        [_bar(index) for index in range(12)],
        store,
    )

    assert report.input_events == 1
    assert report.counterfactual_events == 1
    assert report.events == 2
    counterfactual = next(
        outcome for outcome in report.outcomes if outcome.decision_id.endswith(":pre-guard")
    )
    assert counterfactual.net_label_eligible is True
    assert counterfactual.realized_net_r == 0.4
    assert counterfactual.cost_model_id == DEFAULT_COST_MODEL_ID
    assert {outcome.decision_id for outcome in load_canonical_outcomes(store)} == {
        event["decision_id"],
        f"{event['decision_id']}:pre-guard",
    }


def test_guard_counterfactual_unknown_producer_and_missing_net_inputs_fail_closed() -> None:
    fusion = _guard_event(
        mode="fusion",
        timeframe="fusion",
        producer=journal.FUSION_PRODUCER,
        producer_version=journal.FUSION_PRODUCER_VERSION,
    )
    fusion_child = guard_counterfactual_decision_events([fusion])[0]
    assert fusion_child["mode"] == "fusion"
    assert fusion_child["timeframe"] == "fusion"
    assert fusion_child["decision"]["direction"] == "long"

    unknown = _guard_event(producer="unknown")
    assert guard_counterfactual_decision_events([unknown]) == ()

    ineligible = _guard_event()
    decision = dict(ineligible["decision"])
    execution = dict(decision["pre_guard_execution_snapshot"])
    execution["cost_model_id"] = "scanner-proxy-mid-diagnostic-v1"
    execution["cost_status"] = "diagnostic_only"
    execution["canonical_net_label_input_eligible"] = False
    decision["pre_guard_execution_snapshot"] = execution
    decision["pre_guard_cost_model_id"] = "scanner-proxy-mid-diagnostic-v1"
    ineligible["decision"] = decision

    child = guard_counterfactual_decision_events([ineligible])[0]
    outcome = score_canonical_outcome(
        build_canonical_trade_request(child, [_bar(index) for index in range(12)])
    )

    assert outcome.net_label_eligible is False
    assert outcome.realized_net_r is None
    assert "unknown_cost_model" in outcome.quality_flags


def test_forming_or_unhashed_rows_do_not_become_executable_path(tmp_path) -> None:
    rows = [_bar(index, ohlc_scope="forming_bar_snapshot") for index in range(6)]
    for index in range(6, 12):
        row = _bar(index)
        row["content_hash"] = ""
        rows.append(row)

    report = score_and_store_decision_events(
        [_event()],
        rows,
        tmp_path / "canonical.jsonl",
    )

    assert report.eligible == 0
    assert report.pending == 1
    assert report.appended == 0
    outcome = report.outcomes[0]
    assert outcome.realized_net_r is None
    assert "missing_executable_path" in outcome.quality_flags
    assert "forming_bar_rejected" in outcome.quality_flags
    assert "missing_path_content_hash" in outcome.quality_flags


def test_incomplete_horizon_is_fail_closed_even_with_valid_bars(tmp_path) -> None:
    store = tmp_path / "canonical.jsonl"
    report = score_and_store_decision_events(
        [_event()],
        [_bar(index) for index in range(11)],
        store,
    )

    outcome = report.outcomes[0]
    assert outcome.net_label_eligible is False
    assert "incomplete_horizon_path" in outcome.quality_flags
    assert report.pending == 1
    assert report.appended == 0
    assert not store.exists()

    matured = score_and_store_decision_events(
        [_event()],
        [_bar(index) for index in range(12)],
        store,
    )
    assert matured.eligible == 1
    assert matured.appended == 1


def test_internal_bar_gap_is_pending_not_a_terminal_label(tmp_path) -> None:
    rows = [_bar(index) for index in range(12) if index != 6]

    report = score_and_store_decision_events(
        [_event()],
        rows,
        tmp_path / "canonical.jsonl",
    )

    assert report.pending == 1
    assert report.appended == 0
    assert "path_gap" in report.outcomes[0].quality_flags


def test_conflicting_completed_bars_for_same_start_are_hard_error() -> None:
    rows = [_bar(index) for index in range(12)]
    rows.append(_bar(3, bid_close=100.55))

    with pytest.raises(CanonicalOutcomePipelineError, match="conflicting completed bars"):
        build_canonical_trade_request(_event(), rows)


def test_source_record_id_cannot_claim_multiple_bar_starts() -> None:
    rows = [_bar(index) for index in range(12)]
    rows[4] = _bar(4, source_record_id="bar-3")

    with pytest.raises(CanonicalOutcomePipelineError, match="multiple bar starts"):
        build_canonical_trade_request(_event(), rows)


def test_future_entry_quote_and_path_source_mismatch_are_rejected() -> None:
    future_quote = (NOW + timedelta(seconds=1)).isoformat()
    request = build_canonical_trade_request(
        _event(quote_available_at=future_quote),
        [_bar(index, source="other_fixture") for index in range(12)],
    )

    outcome = score_canonical_outcome(request)

    assert outcome.net_label_eligible is False
    assert "future_entry_quote" in outcome.quality_flags
    assert "path_source_mismatch" in outcome.quality_flags


def test_offline_cli_writes_verified_store_export_and_report(tmp_path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    store = tmp_path / "canonical.jsonl"
    exported = tmp_path / "canonical_export.json"
    report = tmp_path / "canonical_report.json"
    decisions.write_text(json.dumps(_event()) + "\n", encoding="utf-8")
    prices.write_text(
        "".join(json.dumps(_bar(index)) + "\n" for index in range(12)),
        encoding="utf-8",
    )

    assert (
        pipeline_cli.main(
            [
                "--decisions",
                str(decisions),
                "--prices",
                str(prices),
                "--store",
                str(store),
                "--export",
                str(exported),
                "--report",
                str(report),
            ]
        )
        == 0
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["eligible"] == 1
    assert payload["store_audit"]["entry_count"] == 1
    assert exported.exists()


def test_offline_cli_rejects_malformed_jsonl(tmp_path) -> None:
    decisions = tmp_path / "decisions.jsonl"
    prices = tmp_path / "prices.jsonl"
    decisions.write_text('{"partial":\n', encoding="utf-8")
    prices.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="malformed JSONL"):
        pipeline_cli.main(
            [
                "--decisions",
                str(decisions),
                "--prices",
                str(prices),
                "--store",
                str(tmp_path / "canonical.jsonl"),
            ]
        )
