"""Bid/ask bar materialization, broker adapters, and bar-quality checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from data_platform.adapters.broker import (
    MockQuoteSource,
    QuoteSourceError,
    ReplayQuoteSource,
    UnimplementedQuoteSource,
    collect_quotes,
    normalize_quotes,
)
from data_platform.contracts.market_quote import MarketQuote
from data_platform.contracts.pit_record import PITContractError
from data_platform.materialize.bid_ask_bars import BAR_INTERVALS, materialize_bars
from data_platform.quality.bars import (
    gap_audit,
    max_staleness_seconds,
    source_divergence,
    stale_bars,
)


def _t(second: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 13, 9, minute, second, tzinfo=UTC)


def _quote(
    seq: int, bid: float, ask: float, second: int, minute: int = 0, **kw: object
) -> MarketQuote:
    base: dict[str, object] = {
        "source_id": "broker_primary",
        "instrument": "USDJPY",
        "bid": bid,
        "ask": ask,
        "source_timestamp": _t(second, minute),
        "received_timestamp": _t(second, minute),
        "available_at": _t(second, minute),
        "sequence_id": seq,
        "writer_id": "collector-1",
        "tradable": True,
    }
    base.update(kw)
    return MarketQuote(**base)  # type: ignore[arg-type]


class TestAdapters:
    def test_replay_yields_in_order(self) -> None:
        quotes = [_quote(1, 145.10, 145.13, 0), _quote(2, 145.11, 145.14, 1)]
        source = ReplayQuoteSource("broker_primary", "collector-1", "USDJPY", quotes)
        assert [q.sequence_id for q in source.quotes()] == [1, 2]

    def test_replay_rejects_out_of_order_fixture(self) -> None:
        quotes = [_quote(2, 145.11, 145.14, 1), _quote(1, 145.10, 145.13, 0)]
        with pytest.raises(QuoteSourceError, match="out of order"):
            ReplayQuoteSource("broker_primary", "collector-1", "USDJPY", quotes)

    def test_replay_rejects_multiple_writers(self) -> None:
        quotes = [_quote(1, 145.10, 145.13, 0), _quote(2, 145.11, 145.14, 1, writer_id="other")]
        with pytest.raises(QuoteSourceError, match="one writer_id"):
            ReplayQuoteSource("broker_primary", "collector-1", "USDJPY", quotes)

    def test_unimplemented_source_fails_closed(self) -> None:
        source = UnimplementedQuoteSource("broker_secondary", "USDJPY")
        with pytest.raises(QuoteSourceError, match="unvalidated"):
            list(source.quotes())

    def test_mock_is_usable_as_source(self) -> None:
        source = MockQuoteSource(
            "mock", "w1", "USDJPY", [_quote(1, 145.1, 145.13, 0, writer_id="w1")]
        )
        assert collect_quotes(source)[0].sequence_id == 1

    def test_normalize_drops_identical_duplicate(self) -> None:
        q = _quote(1, 145.10, 145.13, 0)
        assert len(normalize_quotes([q, q])) == 1

    def test_normalize_rejects_conflicting_duplicate(self) -> None:
        a = _quote(1, 145.10, 145.13, 0)
        b = _quote(1, 145.20, 145.23, 0)  # same key, different content
        with pytest.raises(PITContractError, match="conflicting content"):
            normalize_quotes([a, b])


class TestMaterializeBars:
    def test_single_bar_ohlc(self) -> None:
        quotes = [
            _quote(1, 145.10, 145.13, 0),
            _quote(2, 145.15, 145.18, 1),
            _quote(3, 145.08, 145.11, 2),
            _quote(4, 145.12, 145.15, 3),
        ]
        bars = materialize_bars(quotes, "5s")
        assert len(bars) == 1
        bar = bars[0]
        assert bar.bid_open == pytest.approx(145.10)
        assert bar.bid_high == pytest.approx(145.15)
        assert bar.bid_low == pytest.approx(145.08)
        assert bar.bid_close == pytest.approx(145.12)
        assert bar.quote_count == 4
        assert bar.mid_open == pytest.approx((145.10 + 145.13) / 2)

    def test_spread_distribution(self) -> None:
        quotes = [
            _quote(1, 145.10, 145.13, 0),  # spread 0.03
            _quote(2, 145.10, 145.20, 1),  # spread 0.10
            _quote(3, 145.10, 145.15, 2),  # spread 0.05
        ]
        bar = materialize_bars(quotes, "5s")[0]
        assert bar.spread_open == pytest.approx(0.03)
        assert bar.spread_max == pytest.approx(0.10)
        assert bar.spread_median == pytest.approx(0.05)
        assert bar.spread_mean == pytest.approx((0.03 + 0.10 + 0.05) / 3)

    def test_quotes_split_across_buckets(self) -> None:
        # Two quotes 5s apart fall into distinct 5s buckets.
        quotes = [_quote(1, 145.10, 145.13, 0), _quote(2, 145.20, 145.23, 5)]
        bars = materialize_bars(quotes, "5s")
        assert len(bars) == 2
        assert bars[0].bid_close == pytest.approx(145.10)
        assert bars[1].bid_open == pytest.approx(145.20)

    def test_stale_seconds_is_largest_intrabar_gap(self) -> None:
        # Quotes at 0s, 1s, 40s within a 1m bar -> largest gap 39s.
        quotes = [
            _quote(1, 145.10, 145.13, 0),
            _quote(2, 145.11, 145.14, 1),
            _quote(3, 145.12, 145.15, 40),
        ]
        bar = materialize_bars(quotes, "1m")[0]
        assert bar.stale_seconds == pytest.approx(39.0)

    def test_source_coverage_uses_expected_interval(self) -> None:
        # 3 quotes in a 5s bar; expected 1 quote/sec -> coverage 3/5.
        quotes = [
            _quote(1, 145.10, 145.13, 0),
            _quote(2, 145.11, 145.14, 1),
            _quote(3, 145.12, 145.15, 2),
        ]
        bar = materialize_bars(quotes, "5s", expected_quote_interval=timedelta(seconds=1))[0]
        assert bar.source_coverage == pytest.approx(3 / 5)

    def test_empty_quotes_yields_no_bars(self) -> None:
        assert materialize_bars([], "5s") == []

    def test_unknown_interval_rejected(self) -> None:
        with pytest.raises(PITContractError, match="unknown bar interval"):
            materialize_bars([_quote(1, 145.1, 145.13, 0)], "7s")

    def test_reconstruction_is_deterministic(self) -> None:
        # Same quotes + interval -> identical bars (raw -> bar reconstructable).
        quotes = [_quote(1, 145.10, 145.13, 0), _quote(2, 145.15, 145.18, 1)]
        first = [b.to_dict() for b in materialize_bars(quotes, "5s")]
        second = [b.to_dict() for b in materialize_bars(list(reversed(quotes)), "5s")]
        assert first == second  # order-independent aggregation

    def test_all_intervals_materialize(self) -> None:
        quotes = [_quote(1, 145.10, 145.13, 0)]
        for interval in BAR_INTERVALS:
            assert len(materialize_bars(quotes, interval)) == 1


class TestBarQuality:
    def _bars(self, interval: str = "5s", minutes_seconds: list[tuple[int, int]] | None = None):
        pairs = minutes_seconds or [(0, 0), (0, 5), (0, 10)]
        quotes = [
            _quote(i + 1, 145.10, 145.13, sec, minute=mn) for i, (mn, sec) in enumerate(pairs)
        ]
        return materialize_bars(quotes, interval)

    def test_gap_audit_detects_missing_bucket(self) -> None:
        # Buckets at 0s and 10s present, 5s missing.
        bars = self._bars(minutes_seconds=[(0, 0), (0, 10)])
        audit = gap_audit(bars, "5s")
        assert audit.expected_bars == 3
        assert audit.observed_bars == 2
        assert len(audit.missing_open_times) == 1
        assert audit.completeness == pytest.approx(2 / 3)

    def test_gap_audit_contiguous_has_no_gaps(self) -> None:
        bars = self._bars(minutes_seconds=[(0, 0), (0, 5), (0, 10)])
        assert gap_audit(bars, "5s").missing_open_times == ()

    def test_divergence_flags_breach(self) -> None:
        primary = self._bars(minutes_seconds=[(0, 0)])
        # secondary at same close_time but different mid
        secondary_quotes = [
            _quote(1, 146.10, 146.13, 0, source_id="broker_secondary", writer_id="w2")
        ]
        from data_platform.materialize.bid_ask_bars import materialize_bars as mb

        secondary = mb(secondary_quotes, "5s")
        report = source_divergence(primary, secondary, max_abs_mid=0.5)
        assert report.compared_bars == 1
        assert report.has_breach
        assert report.max_abs_mid_divergence == pytest.approx(1.0, abs=1e-6)

    def test_divergence_ignores_non_cotimed_bars(self) -> None:
        primary = self._bars(minutes_seconds=[(0, 0)])
        secondary_quotes = [_quote(1, 145.10, 145.13, 30, source_id="s", writer_id="w2")]
        from data_platform.materialize.bid_ask_bars import materialize_bars as mb

        report = source_divergence(primary, mb(secondary_quotes, "5s"), max_abs_mid=0.001)
        assert report.compared_bars == 0
        assert not report.has_breach

    def test_stale_bars_flagged(self) -> None:
        quotes = [_quote(1, 145.10, 145.13, 0), _quote(2, 145.12, 145.15, 50)]
        bars = materialize_bars(quotes, "1m")
        assert max_staleness_seconds(bars) == pytest.approx(50.0)
        assert len(stale_bars(bars, max_stale_seconds=30.0)) == 1
        assert len(stale_bars(bars, max_stale_seconds=60.0)) == 0
