"""Contract: scoring the persisted decision log must not hold the raw corpus.

``read_decision_events`` is a generator, and ``score_decision_events`` consumes
its input in one pass while retaining only compact projections. Both properties
are defeated by a caller that materializes the reader, and neither function can
detect that from the inside — so the lazy hand-off is pinned here.

This is a regression guard for the 2026-07-27 cadence incident: per-cycle
runtime grew with history until it exceeded the five-minute launchd interval,
launchd coalesced the following runs, and 68.6% of one window's decision cycles
were lost. Lost decisions are lost learning samples, so this contract is what
keeps the learning loop fed as the audit log grows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json
from pathlib import Path
from unittest import mock
import weakref

from fx_intel import decision_log, journal
from fx_intel.evaluation_labels import DEFAULT_COST_MODEL_ID
from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.technicals import PairTechnicals, build_interval_view
from fx_intel.timeframe import TimeframePlan

NOW = datetime(2026, 7, 8, 8, 0, tzinfo=UTC)


class _TrackedEvent(dict):
    """A weak-referenceable event, so the test can observe when one is freed."""

    __slots__ = ("__weakref__",)


def _analysis() -> MarketAnalysis:
    return MarketAnalysis(
        currencies={
            "USD": CurrencySentiment("USD", score=0.3, headline_count=2),
            "JPY": CurrencySentiment("JPY", score=-0.2, headline_count=1),
        },
        regime="neutral",
        summary="streaming contract fixture",
        engine="lexicon",
    )


def _tech() -> PairTechnicals:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views["1h"] = build_interval_view(
        "1h",
        {"RECOMMENDATION": "BUY", "BUY": 10, "SELL": 2, "NEUTRAL": 4},
        {"close": 150.0, "RSI": 55.0, "ADX": 28.0, "ATR": 0.2, "SMA20": 150.2, "SMA100": 149.8},
        20,
        100,
    )
    return tech


def _plan(index: int) -> TimeframePlan:
    return TimeframePlan(
        symbol="USDJPY",
        timeframe="1h",
        horizon_hours=1.0,
        direction="long",
        conviction=72,
        tf_score=0.5,
        news_score=0.2,
        composite=0.37,
        close=150.0,
        atr=0.2,
        rsi=55.0,
        adx=28.0,
        stop=149.5,
        target1=150.5,
        target2=151.0,
        entry_bid=149.99,
        entry_ask=150.01,
        quote_observed_at="2026-07-08T07:59:57+00:00",
        quote_available_at="2026-07-08T07:59:59+00:00",
        quote_source="fixture_quotes",
        quote_source_record_id=f"quote-{index}",
        planned_risk_distance=0.5,
        data_quality=0.9,
        features={"rsi_1h": 55.0, "news_count": 2.0},
        components=[{"key": "tech", "score": 0.5, "weight": 0.55}],
        reason="1hレーティング 買い",
        pre_guard_direction="long",
        pre_guard_conviction=72,
        pre_guard_stop=149.5,
        pre_guard_target1=150.5,
        pre_guard_target2=151.0,
        pre_guard_target_policy={
            "policy_id": "default-atr-v1",
            "target1_r": 1.0,
            "target2_r": 2.0,
        },
        pre_guard_execution_snapshot={
            "cost_model_id": DEFAULT_COST_MODEL_ID,
            "canonical_net_label_input_eligible": True,
        },
        pre_guard_cost_model_id=DEFAULT_COST_MODEL_ID,
    )


def _build_events(count: int, *, start: datetime) -> list[dict]:
    """Build ``count`` PIT-eligible per-timeframe decision events."""

    events: list[dict] = []
    for index in range(count):
        moment = start + timedelta(hours=index)
        plan = _plan(index)
        plan.input_context_id = f"context-{index}"
        built = decision_log.build_timeframe_decision_events(
            {"USDJPY": [plan]},
            now=moment,
            analysis=_analysis(),
            tech_map={"USDJPY": _tech()},
        )
        for event in built:
            event.update(
                journal.pit_metadata_for_plan(
                    plan,
                    prediction_time=moment,
                    source_cutoff=moment,
                    max_feature_available_time=moment,
                    decision_id=str(event["decision_id"]),
                    mode="per_timeframe",
                )
            )
        events.extend(built)
    return events


def _write_log(tmp_path: Path, *, count: int) -> Path:
    path = tmp_path / "briefing_decisions.jsonl"
    decision_log.append_decision_events(path, _build_events(count, start=NOW))
    return path


def test_scoring_the_persisted_log_never_retains_the_raw_corpus(tmp_path: Path) -> None:
    path = _write_log(tmp_path, count=60)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 60

    handed_out: list[weakref.ref] = []
    peak = 0

    def tracking_reader(_path):
        nonlocal peak
        for row in rows:
            event = _TrackedEvent(row)
            handed_out.append(weakref.ref(event))
            alive = sum(1 for reference in handed_out if reference() is not None)
            peak = max(peak, alive)
            yield event

    with mock.patch.object(decision_log, "read_decision_events", tracking_reader):
        report = decision_log.score_persisted_decision_log(
            path, [], now=NOW + timedelta(days=3), require_pit=True
        )

    assert report["pit_eligible_decision_events"] == 60
    assert peak <= 4, f"{peak} of {len(rows)} raw events were held at once"


def test_streaming_scorer_matches_materialized_scoring(tmp_path: Path) -> None:
    path = _write_log(tmp_path, count=8)
    new_events = _build_events(2, start=NOW + timedelta(hours=8))
    reference = NOW + timedelta(days=3)

    prior = list(decision_log.read_decision_events(path))
    expected = decision_log.score_decision_events(
        [*prior, *new_events], now=reference, require_pit=True
    )
    actual = decision_log.score_persisted_decision_log(
        path, new_events, now=reference, require_pit=True
    )

    assert actual == expected
    assert expected["pit_eligible_decision_events"] == 10


def test_reported_input_count_matches_the_events_actually_consumed(tmp_path: Path) -> None:
    path = _write_log(tmp_path, count=5)
    report = decision_log.score_persisted_decision_log(path, _build_events(2, start=NOW))

    assert decision_log.scored_input_event_count(report) == 7


def test_audit_log_consumers_never_read_the_corpus_directly() -> None:
    """Both five-minute writers must score through the streaming helper.

    The 2026-07-27 incident had two producers doing the same full-corpus
    rescore: the briefing cycle and the dedicated expectancy monitor. Pinning
    only one of them would leave the other free to reintroduce the contention.
    """

    root = Path(__file__).resolve().parents[1]
    for relative in ("fx_briefing.py", "tools/decision_expectancy_monitor.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "score_persisted_decision_log" in source, relative
        assert "read_decision_events" not in source, (
            f"{relative} must not read the audit log directly; "
            "use decision_log.score_persisted_decision_log so the corpus stays streamed"
        )
