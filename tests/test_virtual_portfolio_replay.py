from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import lzma
import struct

import pytest

from data_platform.collect.contract import CollectedQuote
from data_platform.collect.dukascopy import HourContext, parse_hour_payload
from data_platform.raw.immutable_store import ImmutableRawStore
from fx_intel import decision_log
from fx_intel.canonical_label_evidence import canonical_label_evidence_from_virtual_row
from fx_intel.canonical_label_evidence_store import (
    append_canonical_label_evidence,
    load_canonical_label_evidence,
)
from fx_intel.canonical_label_readiness import assess_canonical_label_readiness
from fx_intel.canonical_label_path_verifier import (
    CanonicalPathVerificationError,
    _independent_exit,
    verify_virtual_label_path,
)
from fx_intel.operational_contracts import (
    DECISION_JOURNAL_PIT_CONTRACT,
    FUSION_PRODUCER,
    FUSION_PRODUCER_VERSION,
    TIMEFRAME_PRODUCER,
    TIMEFRAME_PRODUCER_VERSION,
)
from fx_intel.sentiment import MarketAnalysis
from fx_intel.technicals import PairTechnicals, build_interval_view
from fx_intel.timeframe import build_timeframe_plan
from fx_intel.virtual_portfolio import (
    VirtualPortfolioPolicy,
    five_x_virtual_portfolio_policy,
    open_virtual_portfolio_writer,
)
from fx_intel.virtual_portfolio_replay import (
    REPLAY_CONTRACT,
    ReplayTick,
    enrich_decision_artifacts,
    run_replay_cycle,
)
from fx_intel.virtual_portfolio_net_labels import canonical_outcome_from_virtual_row

START = datetime(2026, 7, 29, 0, 0, tzinfo=UTC)


def _decision(*, symbol: str = "USDJPY", timeframe: str = "1h") -> dict[str, object]:
    context_hash = hashlib.sha256(b"point-in-time-context").hexdigest()
    stop = 149.52 if symbol == "USDJPY" else 1.095
    target = 151.02 if symbol == "USDJPY" else 1.115
    reference_atr = 0.2 if symbol == "USDJPY" else 0.0052
    target_rr = 2.0 if symbol == "USDJPY" else (1.115 - 1.1002) / 0.0052
    return {
        "decision_id": "replay-decision-1",
        "ts": START.isoformat(),
        "prediction_time": START.isoformat(),
        "source_cutoff": (START - timedelta(seconds=2)).isoformat(),
        "max_feature_available_time": (START - timedelta(seconds=1)).isoformat(),
        "input_context_id": f"context:sha256:{context_hash}",
        "source_record_ids": ["context-row-1"],
        "pit_eligible": True,
        "pit_contract": DECISION_JOURNAL_PIT_CONTRACT,
        "producer": FUSION_PRODUCER,
        "producer_version": FUSION_PRODUCER_VERSION,
        "mode": "fusion",
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": "long",
        "conviction": 80,
        "stop": stop,
        "target1": target,
        "price_frame": {
            "stop_atr_multiple": 2.5 if symbol == "USDJPY" else 1.0,
            "target_rr": target_rr,
            "reference_atr": reference_atr,
        },
        "reason": "PIT replay fixture",
        "features": {"rsi": 55.0, "regime": "trend"},
        "components": {"technical": 0.7},
        "warnings": [],
    }


def _generated_timeframe_decision() -> dict[str, object]:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views["1h"] = build_interval_view(
        "1h",
        {"RECOMMENDATION": "STRONG_BUY", "BUY": 20, "SELL": 0, "NEUTRAL": 6},
        {
            "close": 150.0,
            "RSI": 55.0,
            "ADX": 28.0,
            "ATR": 0.2,
            "SMA20": 150.2,
            "SMA100": 149.8,
        },
        20,
        100,
    )
    plan = build_timeframe_plan(
        "USDJPY",
        "1h",
        tech,
        {},
        [],
        [],
        now=START,
    )
    assert plan.direction == "long"
    event = decision_log.build_timeframe_decision_events(
        {"USDJPY": [plan]},
        now=START,
        analysis=MarketAnalysis(
            currencies={},
            regime="neutral",
            summary="fixture",
            engine="fixture",
        ),
        tech_map={"USDJPY": tech},
    )[0]
    nested = event["decision"]
    assert isinstance(nested, dict)
    context_hash = hashlib.sha256(b"generated-timeframe-context").hexdigest()
    return {
        **nested,
        "decision_id": event["decision_id"],
        "ts": START.isoformat(),
        "prediction_time": START.isoformat(),
        "source_cutoff": (START - timedelta(seconds=2)).isoformat(),
        "max_feature_available_time": (START - timedelta(seconds=1)).isoformat(),
        "input_context_id": f"context:sha256:{context_hash}",
        "source_record_ids": ["generated-timeframe-source"],
        "pit_eligible": True,
        "pit_contract": DECISION_JOURNAL_PIT_CONTRACT,
        "producer": TIMEFRAME_PRODUCER,
        "producer_version": TIMEFRAME_PRODUCER_VERSION,
        "mode": "per_timeframe",
        "symbol": "USDJPY",
        "timeframe": "1h",
    }


def _tick(
    moment: datetime,
    bid: float,
    ask: float,
    *,
    received_at: datetime,
    instrument: str = "USDJPY",
) -> dict:
    identity = f"{instrument}:{moment.isoformat()}:{bid}:{ask}".encode()
    quote = CollectedQuote(
        provider="dukascopy",
        account_environment="datafeed",
        instrument=instrument,
        provider_event_time=moment,
        received_at=received_at,
        bid=bid,
        ask=ask,
        bid_size=1.0,
        ask_size=1.0,
        tradable=False,
        sequence_id=None,
        connection_id="historical-fixture",
        writer_id="fixture",
        revision_id=None,
        raw_payload_sha256=hashlib.sha256(identity).hexdigest(),
        source_endpoint_class="historical_datafeed",
        collection_mode="historical_download",
    )
    return quote.to_dict()


def _write_tape(path, *, received_at: datetime) -> None:
    rows = [
        _tick(START + timedelta(seconds=1), 150.00, 150.02, received_at=received_at),
        _tick(START + timedelta(minutes=5), 150.50, 150.52, received_at=received_at),
        _tick(START + timedelta(minutes=10), 151.03, 151.05, received_at=received_at),
        _tick(START + timedelta(hours=1), 151.20, 151.22, received_at=received_at),
    ]
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_rows(path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_raw_first_tape(path, raw_root, *, received_at: datetime) -> None:
    records = [
        (1_000, 150_020, 150_000),
        (300_000, 150_520, 150_500),
        (600_000, 151_050, 151_030),
        (3_600_000, 151_220, 151_200),
    ]
    decompressed = b"".join(
        struct.pack(">IIIff", offset, ask, bid, 1.0, 1.0) for offset, ask, bid in records
    )
    payload = lzma.compress(decompressed)
    ImmutableRawStore(raw_root).put(payload)
    quotes = parse_hour_payload(
        payload,
        HourContext(
            instrument="USDJPY",
            hour_start_utc=START,
            received_at=received_at,
            connection_id="raw-first-fixture",
        ),
    )
    _write_rows(path, [quote.to_dict() for quote in quotes])


def test_artifact_enrichment_uses_context_hash_and_labels_descriptor() -> None:
    policy = VirtualPortfolioPolicy()
    enriched = enrich_decision_artifacts(_decision(), portfolio_policy=policy)
    assert enriched["data_sha256"] == _decision()["input_context_id"].split(":")[-1]
    assert enriched["policy_sha256"] == policy.policy_sha256
    assert len(str(enriched["model_sha256"])) == 64
    provenance = enriched["artifact_provenance"]
    assert isinstance(provenance, dict)
    assert "not a trained-model claim" in str(provenance["model_sha256_basis"])


def test_idle_cycle_still_records_cash_liquidation_baseline(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(
            policy=VirtualPortfolioPolicy(),
            created_at=START,
        )
        result = run_replay_cycle(
            store,
            quote_log=tmp_path / "unused-quotes.jsonl",
            as_of=START,
        )
        snapshot = store.virtual_portfolio_v2_snapshot()

    assert result["status"] == "idle"
    assert result["valuation_v2"]["quality_complete"] is True
    assert snapshot["valuation_status"] == "complete"
    assert snapshot["liquidation_equity_low_jpy"] == 1_000_000


def test_long_horizon_queue_is_terminal_observation_without_quote_tape(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(
            policy=VirtualPortfolioPolicy(),
            created_at=START,
        )
        store.queue_decision(
            _decision(timeframe="1d"),
            observed_at=START,
            source="fixture",
            batch_id="batch-observer",
        )
        result = run_replay_cycle(
            store,
            quote_log=tmp_path / "missing-quotes.jsonl",
            as_of=START,
        )
        snapshot = store.snapshot(now=START)

    assert result["status"] == "ok"
    assert result["queued"] == 1
    assert result["abstained"] == 1
    assert result["waiting"] == 0
    assert snapshot["open_position_count"] == 0
    assert snapshot["pending_decision_count"] == 0
    assert (
        snapshot["recent_decisions"][0]["horizon_allocation"]["allocation_class"]
        == "observation_only"
    )


def test_delayed_ticks_are_not_visible_before_received_time(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    policy = VirtualPortfolioPolicy()
    received = START + timedelta(hours=2)
    _write_tape(quotes, received_at=received)
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=policy, created_at=START)
        store.queue_decision(
            _decision(),
            observed_at=START,
            source="fixture",
            batch_id="batch-1",
        )
        early = run_replay_cycle(
            store,
            quote_log=quotes,
            as_of=START + timedelta(minutes=30),
        )
        assert early["opened"] == 0
        assert early["closed"] == 0
        assert early["waiting"] == 1
        assert store.snapshot(now=START + timedelta(minutes=30))["pending_decision_count"] == 1


def test_delayed_replay_keeps_the_policy_known_at_decision_time(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    base_policy = VirtualPortfolioPolicy()
    five_x_policy = five_x_virtual_portfolio_policy()
    replay_time = START + timedelta(hours=2)
    _write_tape(quotes, received_at=replay_time)

    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=base_policy, created_at=START)
        store.queue_decision(
            _decision(),
            observed_at=START,
            source="fixture",
            batch_id="pre-policy-transition",
        )
        store.activate_five_x_limits(changed_at=START + timedelta(hours=1))
        result = run_replay_cycle(store, quote_log=quotes, as_of=replay_time)
        recorded = store.connection.execute("""
            SELECT decision_json
            FROM decision_records
            WHERE decision_id = 'replay-decision-1'
            """).fetchone()

        assert store.policy().policy_sha256 == five_x_policy.policy_sha256

    assert result["opened"] == 1
    assert recorded is not None
    decision = json.loads(str(recorded["decision_json"]))
    assert decision["policy_sha256"] == base_policy.policy_sha256
    assert decision["policy_sha256"] != five_x_policy.policy_sha256
    assert decision["portfolio_risk_snapshot"]["policy_sha256"] == (base_policy.policy_sha256)


def test_queue_to_executable_entry_to_target_close_to_learning(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    policy = VirtualPortfolioPolicy()
    as_of = START + timedelta(hours=2)
    _write_tape(quotes, received_at=as_of)
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=policy, created_at=START)
        queued = store.queue_decision(
            _decision(),
            observed_at=START,
            source="fixture",
            batch_id="batch-1",
        )
        result = run_replay_cycle(store, quote_log=quotes, as_of=as_of)
        snapshot = store.snapshot(now=as_of)
        learning = store.export_learning_rows(as_of=as_of)

    assert queued["status"] == "pending"
    assert result["contract"] == REPLAY_CONTRACT
    assert result["opened"] == 1
    assert result["closed"] == 1
    assert result["conflicts"] == []
    assert snapshot["pending_decision_count"] == 0
    assert snapshot["open_position_count"] == 0
    trade = snapshot["trades"][0]
    assert trade["close_reason"] == "target1"
    assert trade["attribution"]["execution_observation_mode"] == (
        "delayed_historical_bid_ask_replay"
    )
    assert trade["attribution"]["replay_evidence"]["gap_policy"].startswith("first_observed")
    assert learning["row_count"] == 1
    assert learning["rows"][0]["prediction_time"] == START.isoformat()
    assert learning["rows"][0]["close_reason"] == "target1"
    assert learning["rows"][0]["promotion_eligible"] is False
    assert learning["rows"][0]["feature_availability_contract"] == ("decision-time-frozen-only")
    canonical = canonical_outcome_from_virtual_row(learning["rows"][0])
    evidence = canonical_label_evidence_from_virtual_row(learning["rows"][0], canonical)
    assert evidence.label_quality == "hash_bound_unrecomputed_path"
    assert evidence.path_tick_count is not None
    assert len(evidence.path_source_ids_sha256) == 64
    assert len(evidence.path_raw_payload_hashes_sha256) == 64
    risk_snapshot = learning["rows"][0]["portfolio_risk_snapshot"]
    assert risk_snapshot["contract"] == "virtual-portfolio-risk-snapshot-v1"
    assert risk_snapshot["as_of"] == START.isoformat()
    assert risk_snapshot["knowledge_as_of"] == START.isoformat()
    assert len(risk_snapshot["snapshot_sha256"]) == 64


def test_generated_timeframe_event_anchors_and_replays_without_missing_price_frame(
    tmp_path,
) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    as_of = START + timedelta(hours=2)
    _write_tape(quotes, received_at=as_of)

    decision = _generated_timeframe_decision()
    assert decision["price_frame"]["reference_atr"] == 0.2
    assert decision["price_frame"]["stop_atr_multiple"] == 2.5
    assert decision["price_frame"]["target_rr"] == 1.0

    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(
            decision,
            observed_at=START,
            source="generated-timeframe-fixture",
            batch_id="generated-timeframe-batch",
        )
        result = run_replay_cycle(store, quote_log=quotes, as_of=as_of)
        record = store.connection.execute(
            "SELECT disposition, rejection_reasons_json FROM decision_records WHERE decision_id = ?",
            (decision["decision_id"],),
        ).fetchone()

    assert result["opened"] == 1
    assert result["closed"] == 1
    assert result["conflicts"] == []
    assert record is not None
    assert record["disposition"] == "opened"
    assert "missing_atr_or_multiple" not in str(record["rejection_reasons_json"])


def test_raw_preimages_independently_verify_exit_and_excursions(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    raw_root = tmp_path / "raw"
    as_of = START + timedelta(hours=2)
    _write_raw_first_tape(quotes, raw_root, received_at=as_of)
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(
            _decision(),
            observed_at=START,
            source="fixture",
            batch_id="raw-first",
        )
        result = run_replay_cycle(store, quote_log=quotes, as_of=as_of)
        learning = store.export_learning_rows(as_of=as_of)

    assert result["closed"] == 1
    row = learning["rows"][0]
    verification = verify_virtual_label_path(
        row,
        quote_log=quotes,
        raw_store_root=raw_root,
    )
    outcome = canonical_outcome_from_virtual_row(row)
    legacy_evidence = canonical_label_evidence_from_virtual_row(row, outcome)
    evidence = canonical_label_evidence_from_virtual_row(
        row,
        outcome,
        path_verification=verification,
    )

    assert verification.raw_blob_count == 1
    assert legacy_evidence.evidence_version == "canonical-label-evidence-v1"
    assert evidence.evidence_version == "canonical-label-evidence-v2"
    assert evidence.label_quality == "raw_replayed_research_path"
    assert evidence.quote_log_prefix_sha256 == verification.quote_log_prefix_sha256
    assert evidence.path_tick_count == 2
    evidence_store = tmp_path / "canonical_label_evidence.jsonl"
    assert append_canonical_label_evidence(evidence_store, [legacy_evidence]) == 1
    assert append_canonical_label_evidence(evidence_store, [evidence]) == 1
    assert load_canonical_label_evidence(evidence_store) == [legacy_evidence, evidence]
    readiness = assess_canonical_label_readiness(
        [outcome],
        source_verified=True,
        label_evidence=[legacy_evidence, evidence],
        evidence_source_verified=True,
    )
    assert readiness.status == "incomplete"
    assert readiness.phase2_complete is False
    assert readiness.completion_conditions["label_quality_verified"] is False

    altered = dict(row)
    altered["maximum_favorable_excursion_r"] = 99.0
    with pytest.raises(
        CanonicalPathVerificationError,
        match="mfe_r does not match independent raw replay",
    ):
        verify_virtual_label_path(
            altered,
            quote_log=quotes,
            raw_store_root=raw_root,
        )


def test_raw_verifier_rejects_omitted_same_millisecond_tick(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    raw_root = tmp_path / "raw"
    as_of = START + timedelta(hours=2)
    records = [
        (1_000, 150_020, 150_000),
        (300_000, 150_520, 150_500),
        # Same provider millisecond, but this second record is the true target touch.
        (300_000, 151_050, 151_030),
        (3_600_000, 151_220, 151_200),
    ]
    decompressed = b"".join(
        struct.pack(">IIIff", offset, ask, bid, 1.0, 1.0) for offset, ask, bid in records
    )
    payload = lzma.compress(decompressed)
    ImmutableRawStore(raw_root).put(payload)
    raw_quotes = parse_hour_payload(
        payload,
        HourContext(
            instrument="USDJPY",
            hour_start_utc=START,
            received_at=as_of,
            connection_id="same-ms-fixture",
        ),
    )
    # Model the accepted log losing the second same-ms record.  Without an exact
    # raw comparison the stored path closes one hour late and falsely verifies.
    _write_rows(
        quotes,
        [raw_quotes[0].to_dict(), raw_quotes[1].to_dict(), raw_quotes[3].to_dict()],
    )
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(
            _decision(),
            observed_at=START,
            source="fixture",
            batch_id="same-ms",
        )
        result = run_replay_cycle(store, quote_log=quotes, as_of=as_of)
        row = store.export_learning_rows(
            as_of=as_of,
            include_ineligible_for_audit=True,
        )[
            "rows"
        ][0]

    assert result["closed"] == 1
    assert row["label_end_time"] == (START + timedelta(hours=1)).isoformat()
    with pytest.raises(
        CanonicalPathVerificationError,
        match="does not exactly reproduce",
    ):
        verify_virtual_label_path(
            row,
            quote_log=quotes,
            raw_store_root=raw_root,
        )


def test_independent_terminal_exit_semantics_fail_closed() -> None:
    before_horizon = ReplayTick(
        symbol="USDJPY",
        event_time=START + timedelta(minutes=59),
        available_time=START + timedelta(hours=2),
        bid=150.5,
        ask=150.52,
        raw_payload_sha256="a" * 64,
        revision_id="a" * 64,
        sequence_id=None,
    )
    after_horizon = replace(
        before_horizon,
        event_time=START + timedelta(hours=1, seconds=1),
        raw_payload_sha256="b" * 64,
        revision_id="b" * 64,
    )
    selected = _independent_exit(
        (before_horizon,),
        all_ticks=(before_horizon, after_horizon),
        watermark=after_horizon.event_time,
        direction="long",
        stop=149.0,
        target=151.0,
        close_reason="time_exit",
        horizon_end=START + timedelta(hours=1),
        replay={},
    )
    assert selected == before_horizon

    late_session_tick = replace(
        before_horizon,
        event_time=START + timedelta(minutes=36),
    )
    with pytest.raises(
        CanonicalPathVerificationError,
        match="more than five minutes",
    ):
        _independent_exit(
            (late_session_tick,),
            all_ticks=(late_session_tick,),
            watermark=late_session_tick.event_time,
            direction="long",
            stop=149.0,
            target=151.0,
            close_reason="session_end",
            horizon_end=START + timedelta(hours=1),
            replay={"session_cutoff": (START + timedelta(minutes=30)).isoformat()},
        )


def test_stop_or_target_is_settled_before_full_horizon_maturity(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    as_of = START + timedelta(minutes=10)
    _write_rows(
        quotes,
        [
            _tick(
                START + timedelta(seconds=1),
                150.00,
                150.02,
                received_at=as_of,
            ),
            _tick(
                START + timedelta(minutes=5),
                149.50,
                149.52,
                received_at=as_of,
            ),
        ],
    )
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(
            _decision(),
            observed_at=START,
            source="fixture",
            batch_id="batch-1",
        )
        result = run_replay_cycle(store, quote_log=quotes, as_of=as_of)
        snapshot = store.snapshot(now=as_of)

    assert result["closed"] == 1
    assert snapshot["open_position_count"] == 0
    assert snapshot["trades"][0]["close_reason"] == "stop_loss"
    assert snapshot["trades"][0]["close_time"] == (START + timedelta(minutes=5)).isoformat()


def test_exit_tick_ingested_before_open_is_known_still_settles(tmp_path) -> None:
    """終了tickの取込が建玉の認識より先でも決済できる(スロットを解放する)。

    取込ラグ(実測 中央値97分)がホライズンより長い短期足では、終了tickの
    取込時刻が `open_known_at` をわずかに下回る。以前はここで
    `close observed_at cannot precede open known_at` が発火し、その建玉が
    **永久に決済できずスロットを占有し続けた**(実機: 15m 建玉が horizon の
    33.6倍=8.4時間占有し、当日の見送り 616件中 262件がこれ由来)。

    close_time(市場で決済が成立した時刻)は動かさず、「我々が知り得た時刻」
    だけを open_known_at 以降へ繰り下げて解消する。
    """

    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"

    # 実機で観測された順序を再現する:
    #   entry tick は遅れて取り込まれ、その取込時刻が open_known_at になる
    #   (known_at_basis = historical_replay_as_of)。
    #   一方 exit tick は entry tick より 6 分早く取り込まれている。
    entry_tick_received = START + timedelta(minutes=96)
    exit_tick_received = START + timedelta(minutes=90)
    as_of = START + timedelta(minutes=120)

    _write_rows(
        quotes,
        [
            _tick(START + timedelta(seconds=1), 150.00, 150.02, received_at=entry_tick_received),
            # stop(149.52)へ到達する終了tick。取込は entry より前。
            _tick(START + timedelta(minutes=5), 149.50, 149.52, received_at=exit_tick_received),
        ],
    )

    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(
            _decision(),
            observed_at=START,
            source="fixture",
            batch_id="batch-1",
        )
        result = run_replay_cycle(store, quote_log=quotes, as_of=as_of)
        snapshot = store.snapshot(now=as_of)

    # 決済が成立し、スロットが解放されること
    assert result["conflicts"] == []
    assert result["closed"] == 1
    assert snapshot["open_position_count"] == 0

    trade = snapshot["trades"][0]
    # close_time は市場時刻のまま(損益・PIT整合性は不変)
    assert trade["close_time"] == (START + timedelta(minutes=5)).isoformat()
    assert trade["close_reason"] == "stop_loss"


def test_open_trade_receives_delayed_executable_equity_mark(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    as_of = START + timedelta(minutes=10)
    _write_rows(
        quotes,
        [
            _tick(
                START + timedelta(seconds=1),
                150.00,
                150.02,
                received_at=as_of,
            ),
            _tick(
                START + timedelta(minutes=5),
                150.10,
                150.12,
                received_at=as_of,
            ),
        ],
    )
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(
            _decision(),
            observed_at=START,
            source="fixture",
            batch_id="batch-1",
        )
        result = run_replay_cycle(store, quote_log=quotes, as_of=as_of)
        snapshot = store.snapshot(now=as_of)

    assert result["opened"] == 1
    assert result["closed"] == 0
    assert result["marked"] == 1
    assert result["valuation_v2"]["quality_complete"] is True
    assert result["valuation_v2"]["valuation"]["valuation_status"] == "complete"
    assert snapshot["open_position_count"] == 1
    assert snapshot["equity_jpy"] is not None
    assert snapshot["equity_valuation_status"] == "delayed_executable_marks_complete"
    assert snapshot["open_positions"][0]["mark_time"] == (START + timedelta(minutes=5)).isoformat()


def test_replaying_unchanged_position_mark_at_later_as_of_is_idempotent(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    first_as_of = START + timedelta(minutes=10)
    _write_rows(
        quotes,
        [
            _tick(
                START + timedelta(seconds=1),
                150.00,
                150.02,
                received_at=first_as_of,
            ),
            _tick(
                START + timedelta(minutes=5),
                150.10,
                150.12,
                received_at=first_as_of,
            ),
        ],
    )
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(
            _decision(),
            observed_at=START,
            source="fixture",
            batch_id="batch-1",
        )
        first = run_replay_cycle(store, quote_log=quotes, as_of=first_as_of)
        retry = run_replay_cycle(
            store,
            quote_log=quotes,
            as_of=START + timedelta(minutes=20),
        )
        mark_count = int(
            store.connection.execute("SELECT COUNT(*) FROM simulated_position_marks").fetchone()[0]
        )
        observed_at_ns = int(
            store.connection.execute(
                "SELECT observed_at_ns FROM simulated_position_marks"
            ).fetchone()[0]
        )

    assert first["status"] == "ok"
    assert first["marked"] == 1
    assert retry["status"] == "ok"
    assert retry["marked"] == 0
    assert retry["conflicts"] == []
    assert mark_count == 1
    assert observed_at_ns == int(first_as_of.timestamp() * 1_000_000_000)


def test_mark_observation_waits_for_jpy_conversion_quote_availability(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    as_of = START + timedelta(minutes=20)
    conversion_available = START + timedelta(minutes=15)
    _write_rows(
        quotes,
        [
            _tick(
                START + timedelta(seconds=1),
                1.1000,
                1.1002,
                received_at=START + timedelta(minutes=2),
                instrument="EURUSD",
            ),
            _tick(
                START + timedelta(seconds=2),
                150.00,
                150.02,
                received_at=START + timedelta(minutes=3),
            ),
            _tick(
                START + timedelta(minutes=5),
                1.1010,
                1.1012,
                received_at=START + timedelta(minutes=10),
                instrument="EURUSD",
            ),
            _tick(
                START + timedelta(minutes=5, seconds=1),
                150.10,
                150.12,
                received_at=conversion_available,
            ),
        ],
    )
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(
            _decision(symbol="EURUSD"),
            observed_at=START,
            source="fixture",
            batch_id="batch-1",
        )
        result = run_replay_cycle(store, quote_log=quotes, as_of=as_of)
        observed_at_ns = int(
            store.connection.execute(
                "SELECT observed_at_ns FROM simulated_position_marks"
            ).fetchone()[0]
        )

    assert result["status"] == "ok"
    assert result["marked"] == 1
    assert result["valuation_v2"]["quality_complete"] is False
    assert result["valuation_v2"]["warnings"][0].startswith("stale_position_mark:")
    assert observed_at_ns == int(conversion_available.timestamp() * 1_000_000_000)


def test_persisted_session_close_waits_for_delayed_quote_then_settles(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    quotes = tmp_path / "quotes.jsonl"
    entry_received = START + timedelta(minutes=2)
    close_received = START + timedelta(minutes=30)
    cutoff = START + timedelta(minutes=10)
    _write_rows(
        quotes,
        [
            _tick(
                START + timedelta(seconds=1),
                150.00,
                150.02,
                received_at=entry_received,
            ),
            _tick(
                START + timedelta(minutes=11),
                150.20,
                150.22,
                received_at=close_received,
            ),
        ],
    )
    with open_virtual_portfolio_writer(db, writer_id="test") as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=START)
        store.queue_decision(
            _decision(),
            observed_at=START,
            source="fixture",
            batch_id="batch-1",
        )
        opened = run_replay_cycle(store, quote_log=quotes, as_of=entry_received)
        assert opened["opened"] == 1
        store.record_session_event(
            session_date_jst="2026-07-29",
            event_type="closed",
            event_time=cutoff - timedelta(minutes=1),
            detail={"source": "legacy-pre-request-event"},
        )
        store.record_session_close_request(
            session_date_jst="2026-07-29",
            requested_at=cutoff,
            cutoff_time=cutoff,
            detail={"source": "fixture"},
        )
        waiting = run_replay_cycle(store, quote_log=quotes, as_of=cutoff)
        assert waiting["closed"] == 0
        assert waiting["session_close_request"] is not None
        assert store.pending_session_close_request(as_of=cutoff) is not None
        assert store.snapshot(now=cutoff)["open_position_count"] == 1

        settled = run_replay_cycle(store, quote_log=quotes, as_of=close_received)
        snapshot = store.snapshot(now=close_received)

    assert settled["closed"] == 1
    assert snapshot["open_position_count"] == 0
    assert snapshot["trades"][0]["close_reason"] == "session_end"
    assert snapshot["trades"][0]["close_time"] == (START + timedelta(minutes=11)).isoformat()
