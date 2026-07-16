"""PITRecord and per-source contracts: point-in-time invariants, fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from data_platform.contracts.economic_event import EconomicEvent
from data_platform.contracts.execution_event import ExecutionIntent
from data_platform.contracts.macro_release import MacroRelease
from data_platform.contracts.market_quote import MarketQuote
from data_platform.contracts.news_event import NewsEvent
from data_platform.contracts.pit_record import (
    PITContractError,
    PITRecord,
    canonical_json_sha256,
    filter_available_at,
)

HEX = "a" * 64


def _t(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 13, hour, minute, tzinfo=UTC)


def _record(**overrides: object) -> PITRecord:
    base: dict[str, object] = {
        "source_id": "broker_primary",
        "instrument": "USDJPY",
        "event_time": _t(9),
        "published_at": None,
        "first_seen_at": _t(9),
        "ingested_at": _t(9),
        "available_at": _t(9),
        "revision_id": None,
        "raw_sha256": HEX,
        "writer_id": "collector-1",
        "schema_version": 1,
    }
    base.update(overrides)
    return PITRecord(**base)  # type: ignore[arg-type]


class TestPITRecord:
    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(PITContractError, match="timezone-aware"):
            _record(event_time=datetime(2026, 7, 13, 9, 0))

    def test_available_at_floored_to_first_seen(self) -> None:
        # Source claims availability earlier than we first saw it; the record is
        # not usable before first_seen_at.
        record = _record(available_at=_t(8), first_seen_at=_t(10), ingested_at=_t(10))
        assert record.available_at == _t(10)

    def test_available_at_raised_by_publication(self) -> None:
        record = _record(published_at=_t(12), available_at=_t(9))
        assert record.available_at == _t(12)

    def test_bad_hash_rejected(self) -> None:
        with pytest.raises(PITContractError, match="SHA-256"):
            _record(raw_sha256="not-a-hash")

    def test_schema_version_must_be_positive_int(self) -> None:
        with pytest.raises(PITContractError):
            _record(schema_version=0)

    def test_available_at_or_before_requires_aware_t(self) -> None:
        record = _record()
        with pytest.raises(PITContractError):
            record.available_at_or_before(datetime(2026, 7, 13, 9, 0))

    def test_filter_available_at_slices_at_research_time(self) -> None:
        early = _record(available_at=_t(9), raw_sha256="b" * 64)
        late = _record(available_at=_t(15), raw_sha256="c" * 64)
        visible = filter_available_at([early, late], _t(12))
        assert visible == [early]

    def test_maps_onto_point_in_time_fields(self) -> None:
        # The mapping must be constructible by the backtester envelope.
        from fx_backtester.point_in_time import PointInTimeRecord

        record = _record(published_at=_t(12))
        pit = PointInTimeRecord(**record.to_point_in_time_fields())
        assert pit.available_time == _t(12)
        assert pit.source == "broker_primary"

    def test_payload_nan_becomes_null_not_zero(self) -> None:
        record = _record(payload={"x": float("nan")})
        assert record.payload["x"] is None

    def test_canonical_hash_is_stable(self) -> None:
        assert canonical_json_sha256({"a": 1, "b": 2}) == canonical_json_sha256({"b": 2, "a": 1})


class TestMarketQuote:
    def _quote(self, **overrides: object) -> MarketQuote:
        base: dict[str, object] = {
            "source_id": "broker_primary",
            "instrument": "USDJPY",
            "bid": 145.10,
            "ask": 145.13,
            "source_timestamp": _t(9),
            "received_timestamp": _t(9, 0),
            "available_at": _t(9),
            "sequence_id": 1,
            "writer_id": "collector-1",
            "tradable": True,
        }
        base.update(overrides)
        return MarketQuote(**base)  # type: ignore[arg-type]

    def test_bid_ge_ask_rejected(self) -> None:
        with pytest.raises(PITContractError, match="ask must strictly exceed bid"):
            self._quote(bid=145.13, ask=145.13)

    def test_spread_is_measured(self) -> None:
        quote = self._quote(bid=145.10, ask=145.14)
        assert quote.spread == pytest.approx(0.04)
        assert quote.mid == pytest.approx(145.12)

    def test_to_pit_record_carries_spread_and_hash(self) -> None:
        record = self._quote().to_pit_record()
        assert record.payload["spread"] == pytest.approx(0.03)
        assert len(record.raw_sha256) == 64
        assert record.instrument == "USDJPY"

    def test_negative_sequence_rejected(self) -> None:
        with pytest.raises(PITContractError):
            self._quote(sequence_id=-1)


class TestMacroRelease:
    def _release(self, revision: int) -> MacroRelease:
        return MacroRelease(
            source_id="fred_vintage",
            series_id="CPIAUCSL",
            observation_date=_t(0),
            value=310.2 + revision,
            release_time=_t(13),
            vintage_date=_t(13) + timedelta(days=revision),
            revision_number=revision,
            first_seen_at=_t(13),
            ingested_at=_t(13),
            available_at=_t(13),
            writer_id="collector-1",
        )

    def test_revisions_are_distinct_records(self) -> None:
        first = self._release(0).to_pit_record()
        revised = self._release(1).to_pit_record()
        assert first.revision_id != revised.revision_id
        assert first.raw_sha256 != revised.raw_sha256

    def test_negative_revision_rejected(self) -> None:
        with pytest.raises(PITContractError):
            self._release(-1)


class TestEconomicEvent:
    def _event(self, **overrides: object) -> EconomicEvent:
        base: dict[str, object] = {
            "source_id": "economic_calendar",
            "event_id": "US-NFP-2026-07",
            "country": "US",
            "currency": "USD",
            "indicator": "Nonfarm Payrolls",
            "reference_period": "2026-06",
            "scheduled_at": _t(12, 30),
            "first_seen_at": _t(12, 30),
            "ingested_at": _t(12, 30),
            "available_at": _t(12, 30),
            "importance": "high",
            "writer_id": "collector-1",
            "forecast": 180.0,
            "actual_first_release": 206.0,
        }
        base.update(overrides)
        return EconomicEvent(**base)  # type: ignore[arg-type]

    def test_surprise_z_uses_first_release(self) -> None:
        event = self._event()
        assert event.surprise_z(20.0) == pytest.approx((206.0 - 180.0) / 20.0)

    def test_surprise_none_when_actual_missing(self) -> None:
        event = self._event(actual_first_release=None)
        assert event.surprise_z(20.0) is None

    def test_surprise_requires_positive_std(self) -> None:
        with pytest.raises(PITContractError):
            self._event().surprise_z(0.0)

    def test_revised_actual_requires_revision_of(self) -> None:
        with pytest.raises(PITContractError, match="revises"):
            self._event(actual_revised=210.0)


class TestNewsEvent:
    def _news(self, **overrides: object) -> NewsEvent:
        base: dict[str, object] = {
            "source_id": "news",
            "article_id": "art-1",
            "source": "reuters",
            "original_url_hash": "u" * 64,
            "first_seen_at": _t(10),
            "ingested_at": _t(10),
            "available_at": _t(10),
            "headline_original": "BOJ holds policy rate",
            "writer_id": "collector-1",
            "currency_tags": ("JPY",),
            "event_types": ("central_bank_stance",),
        }
        base.update(overrides)
        return NewsEvent(**base)  # type: ignore[arg-type]

    def test_unknown_event_type_rejected(self) -> None:
        with pytest.raises(PITContractError, match="unknown news event_types"):
            self._news(event_types=("made_up",))

    def test_edited_earlier_publish_cannot_move_window(self) -> None:
        # Publisher back-dates publish time to before we saw it: availability
        # stays floored at first_seen_at.
        record = self._news(published_at=_t(8)).to_pit_record()
        assert record.available_at == _t(10)

    def test_instrument_defaults_to_first_currency_tag(self) -> None:
        assert self._news().to_pit_record().instrument == "JPY"


class TestExecutionIntent:
    def _intent(self, **overrides: object) -> ExecutionIntent:
        base: dict[str, object] = {
            "source_id": "shadow",
            "instrument": "USDJPY",
            "decision_time": _t(9),
            "intent_time": _t(9, 0),
            "quote_at_intent_bid": 145.10,
            "quote_at_intent_ask": 145.13,
            "intended_side": "long",
            "intended_size": 1.0,
            "available_at": _t(9),
            "writer_id": "shadow-1",
        }
        base.update(overrides)
        return ExecutionIntent(**base)  # type: ignore[arg-type]

    def test_flat_intent_pays_no_spread(self) -> None:
        assert self._intent(intended_side="flat").spread_cost == 0.0

    def test_spread_cost_none_until_fill_known(self) -> None:
        assert self._intent().spread_cost is None

    def test_spread_cost_measured_from_fill(self) -> None:
        intent = self._intent(hypothetical_fill=145.13)
        assert intent.spread_cost == pytest.approx(0.015)

    def test_bad_side_rejected(self) -> None:
        with pytest.raises(PITContractError):
            self._intent(intended_side="buy")
