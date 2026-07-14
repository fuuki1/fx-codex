"""Candle-source tests: contract honesty, Dukascopy/FXCM decoding, TrueFX live
parsing, raw-first candle ingest, bar materialization determinism, bar-level
divergence, and the v1.1 broker/aggregator live distinction of the scorecard.

All payloads here are synthetic FIXTURES that exercise the code paths — they
are never counted as real market data anywhere.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import gzip
import hashlib
import json
import lzma
from pathlib import Path
import struct

import pytest

from data_platform.collect.candles import (
    FLAG_DUPLICATE_CANDLE,
    FLAG_NO_VOLUME,
    CandleContractError,
    CandleLog,
    CollectedCandle,
    ParsedCandles,
    ingest_candle_payload,
)
from data_platform.collect.divergence import DivergenceInputError
from data_platform.collect.divergence_bars import (
    compare_bars_to_close_series,
    compare_candle_bars,
)
from data_platform.collect.dukascopy import DukascopyFetchError
from data_platform.collect.dukascopy_candles import (
    day_m1_url,
    fetch_payload,
    h1_month_context,
    m1_day_context,
    month_h1_url,
    parse_candle_payload,
)
from data_platform.collect.fxcm_candles import FxcmContext, parse_week_h1, week_h1_url
from data_platform.collect.raw_first import QuoteLog
from data_platform.collect.truefx import (
    TruefxContext,
    TruefxFetchError,
    parse_rates_payload,
    poll_once,
    run_poller,
)
from data_platform.materialize.candle_bars import (
    CandleBarError,
    bars_from_csv_bytes,
    bars_sha256,
    bars_to_csv_bytes,
    candle_gap_audit,
    materialize_candle_bars,
)
from data_platform.quality.state import QualityState
from data_platform.raw.immutable_store import ImmutableRawStore
from tools.data_platform_scorecard import Evidence, compute_scorecard

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
DAY = date(2024, 1, 10)
DAY_START = datetime(2024, 1, 10, tzinfo=UTC)

_CANDLE_STRUCT = struct.Struct(">IIIIIf")


def _candle(
    *,
    side: str = "bid",
    interval: str = "1m",
    open_time: datetime = DAY_START,
    o: float = 144.431,
    h: float = 144.465,
    lo: float = 144.416,
    c: float = 144.460,
    volume: float | None = 100.0,
    provider: str = "dukascopy",
) -> CollectedCandle:
    return CollectedCandle(
        provider=provider,
        account_environment="datafeed",
        instrument="USDJPY",
        side=side,
        interval=interval,
        open_time=open_time,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=volume,
        received_at=NOW,
        connection_id="test-conn",
        writer_id="test-writer",
        raw_payload_sha256="ab" * 32,
        source_endpoint_class="historical_datafeed",
        collection_mode="historical_download",
    )


class TestCandleContract:
    def test_incoherent_ohlc_rejected(self) -> None:
        with pytest.raises(CandleContractError, match="incoherent OHLC"):
            _candle(h=144.40)  # high below open/close

    def test_low_above_close_rejected(self) -> None:
        with pytest.raises(CandleContractError, match="incoherent OHLC"):
            _candle(lo=144.455, o=144.46, c=144.45, h=144.47)

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(CandleContractError, match="timezone-aware"):
            _candle(open_time=datetime(2024, 1, 10))  # noqa: DTZ001

    def test_future_open_time_rejected(self) -> None:
        with pytest.raises(CandleContractError, match="future open_time"):
            _candle(open_time=NOW + timedelta(seconds=10))

    def test_missing_volume_flagged_never_zero_filled(self) -> None:
        candle = _candle(volume=None)
        assert candle.volume is None
        assert FLAG_NO_VOLUME in candle.quality_flags

    def test_bad_side_and_interval_rejected(self) -> None:
        with pytest.raises(CandleContractError, match="side"):
            _candle(side="mid")
        with pytest.raises(CandleContractError, match="interval"):
            _candle(interval="7m")


def _m1_day_payload(
    *,
    records: int = 1440,
    padded_indices: set[int] | None = None,
    base_points: int = 144_431,
) -> bytes:
    """Synthetic Dukascopy m1 day file: JPY points, optional padding rows."""

    padded = padded_indices or set()
    rows = bytearray()
    for index in range(records):
        if index in padded:
            rows += _CANDLE_STRUCT.pack(index * 60, 0, 0, 0, 0, 0.0)
            continue
        opened = base_points + index
        rows += _CANDLE_STRUCT.pack(index * 60, opened, opened + 3, opened - 2, opened + 5, 12.5)
    return lzma.compress(bytes(rows))


class TestDukascopyCandles:
    def test_urls_use_zero_indexed_month(self) -> None:
        assert day_m1_url("usdjpy", DAY, "bid").endswith("/USDJPY/2024/00/10/BID_candles_min_1.bi5")
        assert month_h1_url("usdjpy", 2024, 1, "ask").endswith(
            "/USDJPY/2024/00/ASK_candles_hour_1.bi5"
        )

    def test_parse_m1_day_scales_points_and_excludes_padding(self) -> None:
        context = m1_day_context("USDJPY", DAY, "bid", received_at=NOW, connection_id="conn")
        payload = _m1_day_payload(padded_indices={100, 101, 102})
        parsed = parse_candle_payload(payload, context)
        assert parsed.total_records == 1440
        assert parsed.padding_excluded == 3
        assert len(parsed.candles) == 1437
        first = parsed.candles[0]
        assert first.open == pytest.approx(144.431)  # JPY point 1e-3
        assert first.open_time == DAY_START
        assert first.collection_mode == "historical_download"
        # the padded minutes are absent, not zero-priced
        times = {candle.open_time for candle in parsed.candles}
        assert DAY_START + timedelta(minutes=100) not in times

    def test_wrong_record_count_fails_closed(self) -> None:
        context = m1_day_context("USDJPY", DAY, "bid", received_at=NOW, connection_id="conn")
        with pytest.raises(ValueError, match="expected 1440"):
            parse_candle_payload(_m1_day_payload(records=1000), context)

    def test_off_grid_offset_fails_closed(self) -> None:
        context = m1_day_context("USDJPY", DAY, "bid", received_at=NOW, connection_id="conn")
        rows = bytearray()
        rows += _CANDLE_STRUCT.pack(30, 144_431, 144_434, 144_429, 144_436, 1.0)  # 30s offset
        for index in range(1, 1440):
            rows += _CANDLE_STRUCT.pack(index * 60, 144_431, 144_434, 144_429, 144_436, 1.0)
        with pytest.raises(ValueError, match="grid"):
            parse_candle_payload(lzma.compress(bytes(rows)), context)

    def test_h1_month_expected_records_follow_calendar(self) -> None:
        assert (
            h1_month_context(
                "EURUSD", 2024, 2, "bid", received_at=NOW, connection_id="c"
            ).expected_records
            == 29 * 24  # 2024 is a leap year
        )

    def test_fetch_retries_then_succeeds_and_404_is_absence(self) -> None:
        calls = {"n": 0}

        def flaky(url: str) -> tuple[int, bytes]:
            calls["n"] += 1
            return (503, b"") if calls["n"] < 3 else (200, b"payload")

        assert fetch_payload("http://x", fetcher=flaky, max_attempts=4) == b"payload"
        assert fetch_payload("http://x", fetcher=lambda _u: (404, b"")) == b""
        with pytest.raises(DukascopyFetchError, match="HTTP 500"):
            fetch_payload("http://x", fetcher=lambda _u: (500, b""), max_attempts=2)


def _fxcm_week_payload(*, crossed: bool = False) -> bytes:
    header = "DateTime,BidOpen,BidHigh,BidLow,BidClose,AskOpen,AskHigh,AskLow,AskClose"
    rows = [header]
    bid_close = 1.22336 if not crossed else 1.22451
    rows.append(
        f"01/03/2021 22:00:00.000,1.2239,1.22407,1.22279,{bid_close},"
        "1.22411,1.22451,1.22323,1.22351"
    )
    rows.append(
        "01/03/2021 23:00:00.000,1.22336,1.22527,1.22302,1.22499," "1.22351,1.22529,1.22313,1.22500"
    )
    return gzip.compress("\n".join(rows).encode())


class TestFxcmCandles:
    def test_week_url(self) -> None:
        assert week_h1_url("eurusd", 2021, 1).endswith("/H1/EURUSD/2021/1.csv.gz")

    def test_parse_week_emits_bid_and_ask_sides(self) -> None:
        context = FxcmContext(instrument="EURUSD", received_at=NOW, connection_id="c")
        parsed = parse_week_h1(_fxcm_week_payload(), context)
        assert parsed.padding_excluded == 0
        assert len(parsed.candles) == 4  # 2 rows x 2 sides
        bid = parsed.candles[0]
        ask = parsed.candles[1]
        assert (bid.side, ask.side) == ("bid", "ask")
        assert bid.open_time == datetime(2021, 1, 3, 22, tzinfo=UTC)
        assert bid.volume is None and FLAG_NO_VOLUME in bid.quality_flags
        assert ask.open == pytest.approx(1.22411)

    def test_isolated_crossed_row_is_excluded_and_counted(self) -> None:
        context = FxcmContext(instrument="EURUSD", received_at=NOW, connection_id="c")
        header = "DateTime,BidOpen,BidHigh,BidLow,BidClose,AskOpen,AskHigh,AskLow,AskClose"
        crossed_row = (
            "01/11/2021 03:00:00.000,1.04126,1.04180,1.04120,1.04173,"
            "1.04127,1.04181,1.04121,1.04172"  # bid_close > ask_close (real-world case)
        )
        good_rows = [
            f"01/{day:02d}/2021 {hour:02d}:00:00.000,1.22336,1.22527,1.22302,1.22499,"
            "1.22351,1.22529,1.22313,1.22500"
            for day in range(12, 17)
            for hour in range(24)
        ]
        payload = gzip.compress("\n".join([header, crossed_row, *good_rows]).encode())
        parsed = parse_week_h1(payload, context)
        assert parsed.crossed_excluded == 2
        assert len(parsed.candles) == 2 * len(good_rows)

    def test_pervasively_crossed_file_fails_closed(self) -> None:
        context = FxcmContext(instrument="EURUSD", received_at=NOW, connection_id="c")
        with pytest.raises(ValueError, match="crossed boundary book; file untrusted"):
            parse_week_h1(_fxcm_week_payload(crossed=True), context)

    def test_zero_width_boundary_is_excluded_and_counted(self) -> None:
        header = "DateTime,BidOpen,BidHigh,BidLow,BidClose,AskOpen,AskHigh,AskLow,AskClose"
        zero_width_row = (
            "01/04/2021 05:00:00.000,102.996,103.010,102.990,103.006,"
            "102.998,103.012,102.992,103.006"  # bid_close == ask_close (real-world case)
        )
        good_row = (
            "01/04/2021 06:00:00.000,102.996,103.010,102.990,103.000,"
            "102.998,103.012,102.992,103.004"
        )
        payload = gzip.compress("\n".join([header, zero_width_row, good_row]).encode())
        context = FxcmContext(instrument="USDJPY", received_at=NOW, connection_id="c")
        parsed = parse_week_h1(payload, context)
        assert parsed.zero_width_excluded == 2
        assert parsed.total_records == 4
        assert len(parsed.candles) == 2

    def test_bad_header_fails_closed(self) -> None:
        context = FxcmContext(instrument="EURUSD", received_at=NOW, connection_id="c")
        with pytest.raises(ValueError, match="unexpected FXCM header"):
            parse_week_h1(gzip.compress(b"Time,Open\n1,2"), context)

    def test_empty_week_file_is_an_honest_gap(self) -> None:
        context = FxcmContext(instrument="EURUSD", received_at=NOW, connection_id="c")
        parsed = parse_week_h1(gzip.compress(b""), context)
        assert parsed.candles == () and parsed.total_records == 0


def _truefx_payload(*, ms: int | None = None) -> bytes:
    stamp = ms if ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    lines = [
        f"EUR/USD,{stamp},1.13,955,1.13,959,1.13770,1.14061,1.13843",
        f"USD/JPY,{stamp},162.,211,162.,217,162.044,162.507,162.352",
        f"GBP/USD,{stamp},1.33,777,1.33,783,1.33397,1.33811,1.33548",
        f"EUR/GBP,{stamp},0.85,201,0.85,205,0.85067,0.85403,0.85292",
    ]
    return ("\n".join(lines) + "\n").encode()


class TestTruefx:
    def test_parse_concatenates_big_figure_and_points(self) -> None:
        context = TruefxContext(received_at=NOW, connection_id="c")
        payload = _truefx_payload(ms=int(NOW.timestamp() * 1000))
        quotes = parse_rates_payload(payload, context, ["USD_JPY", "EURUSD"])
        by_instrument = {quote.instrument: quote for quote in quotes}
        assert set(by_instrument) == {"USDJPY", "EURUSD"}  # EUR/GBP filtered out
        assert by_instrument["USDJPY"].bid == pytest.approx(162.211)
        assert by_instrument["USDJPY"].ask == pytest.approx(162.217)
        assert by_instrument["EURUSD"].bid == pytest.approx(1.13955)
        quote = by_instrument["USDJPY"]
        assert quote.collection_mode == "live_stream"
        assert quote.account_environment == "datafeed"
        assert quote.tradable is False  # indicative aggregator rate, never tradable
        assert quote.bid_size is None and quote.ask_size is None

    def test_poll_once_ingests_and_second_identical_poll_is_duplicate(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")
        stamp = int((datetime.now(UTC) - timedelta(seconds=1)).timestamp() * 1000)
        payload = _truefx_payload(ms=stamp)
        result = poll_once(
            fetcher=lambda _u: (200, payload),
            store=store,
            log=log,
            instruments=["USD_JPY"],
            connection_id="c",
        )
        assert result.accepted_count == 1
        again = poll_once(
            fetcher=lambda _u: (200, payload),
            store=store,
            log=log,
            instruments=["USD_JPY"],
            connection_id="c",
        )
        assert again.accepted_count == 0  # unchanged snapshot = duplicate, quarantined

    def test_stale_event_time_is_quarantined_not_accepted(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = QuoteLog(tmp_path / "log")
        stale_ms = int((datetime.now(UTC) - timedelta(minutes=5)).timestamp() * 1000)
        result = poll_once(
            fetcher=lambda _u: (200, _truefx_payload(ms=stale_ms)),
            store=store,
            log=log,
            instruments=["USD_JPY"],
            connection_id="c",
        )
        assert result.accepted_count == 0
        assert result.quarantined

    def test_http_error_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(TruefxFetchError, match="HTTP 503"):
            poll_once(
                fetcher=lambda _u: (503, b""),
                store=ImmutableRawStore(tmp_path / "raw"),
                log=QuoteLog(tmp_path / "log"),
                instruments=["USD_JPY"],
                connection_id="c",
            )

    def test_slow_fetch_is_not_future_data(self, tmp_path: Path) -> None:
        """A snapshot produced by the server DURING a slow fetch must be
        accepted: received_at is stamped after the response, not before the
        request (regression: 44% of live polls were rejected as future)."""

        def slow_fetcher(_url: str) -> tuple[int, bytes]:
            # event stamped "now" — later than any pre-fetch clock reading
            return (200, _truefx_payload(ms=int(datetime.now(UTC).timestamp() * 1000)))

        state, results = run_poller(
            fetcher=slow_fetcher,
            store=ImmutableRawStore(tmp_path / "raw"),
            log=QuoteLog(tmp_path / "log"),
            instruments=["USD_JPY"],
            max_polls=1,
            poll_interval_seconds=0.0,
        )
        assert results[0].accepted_count == 1
        assert not results[0].quarantined

    def test_run_poller_opens_gap_on_transport_failure_and_bounds_polls(
        self, tmp_path: Path
    ) -> None:
        calls = {"n": 0}

        def sometimes(url: str) -> tuple[int, bytes]:
            calls["n"] += 1
            if calls["n"] == 2:
                return (503, b"")
            offset = calls["n"]
            stamp = int((datetime.now(UTC).timestamp() - 0.5 + offset / 1000) * 1000)
            return (200, _truefx_payload(ms=stamp))

        state, results = run_poller(
            fetcher=sometimes,
            store=ImmutableRawStore(tmp_path / "raw"),
            log=QuoteLog(tmp_path / "log"),
            instruments=["USD_JPY"],
            max_polls=3,
            poll_interval_seconds=0.0,
        )
        assert len(results) == 3
        assert state.reconnect_count == 1
        assert state.gaps and state.gaps[0].reason.startswith("transport")


class TestCandleIngest:
    def test_raw_first_order_and_dedup(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = CandleLog(tmp_path / "log")
        context = m1_day_context("USDJPY", DAY, "bid", received_at=NOW, connection_id="conn")
        payload = _m1_day_payload(padded_indices={5})
        result = ingest_candle_payload(
            payload,
            parser=lambda raw: parse_candle_payload(raw, context),
            store=store,
            log=log,
        )
        assert result.accepted_count == 1439
        assert result.padding_excluded == 1
        # raw stored content-addressed BEFORE parsing
        assert store.get(result.raw_sha256) == payload
        # padding exclusion is recorded, not silent
        padding_rows = [
            json.loads(line) for line in (tmp_path / "log" / "candle_padding.jsonl").open()
        ]
        assert padding_rows[0]["padding_excluded"] == 1
        # re-ingesting the same payload quarantines every candle as duplicate
        again = ingest_candle_payload(
            payload,
            parser=lambda raw: parse_candle_payload(raw, context),
            store=store,
            log=log,
        )
        assert again.accepted_count == 0
        assert len(again.quarantined) == 1439

    def test_parse_failure_quarantines_but_keeps_raw(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = CandleLog(tmp_path / "log")
        bogus = b"not-a-bi5-payload"
        context = m1_day_context("USDJPY", DAY, "bid", received_at=NOW, connection_id="conn")

        def parser(raw: bytes) -> ParsedCandles:
            return parse_candle_payload(raw, context)  # LZMA failure -> ValueError

        result = ingest_candle_payload(bogus, parser=parser, store=store, log=log)
        assert result.accepted_count == 0
        assert result.quarantined[0]["reason"] == "schema_validation_failed"
        assert store.get(hashlib.sha256(bogus).hexdigest()) == bogus

    def test_candle_log_flags_duplicates(self, tmp_path: Path) -> None:
        log = CandleLog(tmp_path / "log")
        candle = _candle()
        assert log.classify(candle).quality_state is QualityState.USABLE
        log.record(candle)
        flagged = log.classify(candle)
        assert flagged.quality_state is QualityState.QUARANTINED
        assert FLAG_DUPLICATE_CANDLE in flagged.quality_flags


def _paired_m1_candles(minutes: int, *, start: datetime = DAY_START) -> list[CollectedCandle]:
    candles: list[CollectedCandle] = []
    for index in range(minutes):
        stamp = start + timedelta(minutes=index)
        base = 144.431 + index * 0.001
        candles.append(
            _candle(
                side="bid", open_time=stamp, o=base, h=base + 0.005, lo=base - 0.002, c=base + 0.003
            )
        )
        candles.append(
            _candle(
                side="ask",
                open_time=stamp,
                o=base + 0.004,
                h=base + 0.009,
                lo=base + 0.002,
                c=base + 0.007,
            )
        )
    return candles


class TestCandleBars:
    def test_m1_to_5m_aggregation_and_spread_boundaries(self) -> None:
        result = materialize_candle_bars(_paired_m1_candles(10), "5m")
        assert len(result.bars) == 2
        bar = result.bars[0]
        assert bar.candle_count == 5
        assert bar.completeness == 1.0
        assert bar.bid_open == pytest.approx(144.431)
        assert bar.bid_close == pytest.approx(144.431 + 0.004 + 0.003)
        assert bar.ask_high == pytest.approx(144.431 + 0.004 + 0.009)
        assert bar.spread_open == pytest.approx(0.004)
        assert bar.spread_samples == 10  # open+close per constituent minute
        assert bar.spread_basis == "minute_boundaries"
        assert bar.bid_volume_sum == pytest.approx(500.0)

    def test_unpaired_and_crossed_are_counted_not_repaired(self) -> None:
        candles = _paired_m1_candles(3)
        candles.append(_candle(side="bid", open_time=DAY_START + timedelta(minutes=10)))
        crossed_time = DAY_START + timedelta(minutes=4)
        candles.append(
            _candle(side="bid", open_time=crossed_time, o=144.5, h=144.51, lo=144.49, c=144.5)
        )
        candles.append(
            _candle(side="ask", open_time=crossed_time, o=144.45, h=144.46, lo=144.44, c=144.45)
        )
        result = materialize_candle_bars(candles, "5m")
        assert result.unpaired_bid == 1
        assert result.crossed_excluded == 1

    def test_finer_target_than_source_rejected(self) -> None:
        with pytest.raises(CandleBarError, match="coarser"):
            materialize_candle_bars(
                [_candle(interval="1h"), _candle(side="ask", interval="1h")], "5m"
            )

    def test_mixed_providers_rejected(self) -> None:
        with pytest.raises(CandleBarError, match="one instrument, provider"):
            materialize_candle_bars([_candle(), _candle(side="ask", provider="fxcm")], "5m")

    def test_csv_serialization_is_deterministic(self) -> None:
        bars = materialize_candle_bars(_paired_m1_candles(10), "5m").bars
        assert bars_to_csv_bytes(bars) == bars_to_csv_bytes(list(bars))
        assert bars_to_csv_bytes(bars, compress=True) == bars_to_csv_bytes(
            bars, compress=True
        )  # gzip mtime pinned
        assert bars_sha256(bars) == bars_sha256(bars)

    def test_csv_round_trip_preserves_bytes_and_hash(self) -> None:
        bars = list(materialize_candle_bars(_paired_m1_candles(10), "5m").bars)
        serialized = bars_to_csv_bytes(bars)
        reloaded = bars_from_csv_bytes(serialized)
        assert bars_to_csv_bytes(reloaded) == serialized
        assert bars_sha256(reloaded) == bars_sha256(bars)
        gz = bars_to_csv_bytes(bars, compress=True)
        assert bars_to_csv_bytes(bars_from_csv_bytes(gz)) == serialized
        with pytest.raises(CandleBarError, match="canonical"):
            bars_from_csv_bytes(b"nope,nope\n1,2\n")

    def test_gap_audit_reports_missing_buckets(self) -> None:
        candles = _paired_m1_candles(5) + _paired_m1_candles(
            5, start=DAY_START + timedelta(minutes=15)
        )
        result = materialize_candle_bars(candles, "5m")
        audit = candle_gap_audit(result.bars, "5m")
        assert audit.expected_bars == 4
        assert audit.observed_bars == 2
        assert len(audit.missing_open_times) == 2


class TestBarDivergence:
    def _bars(self, provider: str, *, mid_shift: float = 0.0, spread_shift: float = 0.0):
        candles: list[CollectedCandle] = []
        for index in range(6):
            stamp = DAY_START + timedelta(hours=index)
            base = 144.40 + index * 0.01 + mid_shift
            spread = 0.004 + spread_shift
            candles.append(
                _candle(
                    side="bid",
                    interval="1h",
                    open_time=stamp,
                    o=base,
                    h=base + 0.01,
                    lo=base - 0.01,
                    c=base + 0.005,
                    provider=provider,
                )
            )
            candles.append(
                _candle(
                    side="ask",
                    interval="1h",
                    open_time=stamp,
                    o=base + spread,
                    h=base + 0.01 + spread,
                    lo=base - 0.01 + spread,
                    c=base + 0.005 + spread,
                    provider=provider,
                )
            )
        return materialize_candle_bars(candles, "1h").bars

    def test_same_provider_rejected(self) -> None:
        bars = self._bars("dukascopy")
        with pytest.raises(DivergenceInputError, match="independent"):
            compare_candle_bars(bars, bars, pip_size=0.01)

    def test_agreeing_sources_are_usable_with_all_metrics(self) -> None:
        primary = self._bars("dukascopy")
        secondary = self._bars("fxcm", mid_shift=0.005)  # 0.5 pip apart
        received = {bar.open_time: NOW for bar in primary}
        report = compare_candle_bars(
            primary,
            secondary,
            pip_size=0.01,
            primary_received_at=received,
            secondary_received_at={t: NOW + timedelta(seconds=1) for t in received},
        )
        assert report["divergence_state"] == "usable"
        assert report["matched_bars"] == 6
        assert report["metrics"]["mid_diff_pips"]["max"] == pytest.approx(0.5)
        assert report["metrics"]["spread_diff_pips"]["count"] == 6.0
        assert report["metrics"]["receive_time_skew_ms"]["p50"] == pytest.approx(1000.0)

    def test_breach_degrades_then_quarantines_never_averages(self) -> None:
        primary = self._bars("dukascopy")
        degraded = compare_candle_bars(primary, self._bars("fxcm", mid_shift=0.05), pip_size=0.01)
        assert degraded["divergence_state"] == "degraded"
        quarantined = compare_candle_bars(
            primary, self._bars("fxcm", mid_shift=0.15), pip_size=0.01
        )
        assert quarantined["divergence_state"] == "quarantined"
        assert "never averaged" in quarantined["policy"]

    def test_close_only_series_spread_is_honestly_unmeasured(self) -> None:
        bars = self._bars("dukascopy")
        closes = {bar.open_time: bar.mid_close + 0.02 for bar in bars}  # 2 pips off
        report = compare_bars_to_close_series(
            bars,
            closes,
            secondary_provider="histdata",
            pip_size=0.01,
            close_basis_note="HistData M1-derived 1h closes (bid basis per provider docs)",
        )
        assert report["metrics"]["spread_diff_pips"] == {}
        assert report["metrics"]["mid_diff_pips"]["max"] == pytest.approx(2.0)
        with pytest.raises(DivergenceInputError, match="close_basis_note"):
            compare_bars_to_close_series(
                bars,
                closes,
                secondary_provider="histdata",
                pip_size=0.01,
                close_basis_note="  ",
            )


class TestScorecardV11LiveDistinction:
    @staticmethod
    def _bundle(tmp_path: Path, provider_type: str | None) -> dict:
        source = {
            "provider": "truefx",
            "collection_mode": "live_stream",
            "account_environment": "datafeed",
            "has_bid_ask": True,
            "quote_count": 4000,
            "instruments": ["USDJPY", "EURUSD", "GBPUSD"],
            "sizes_flagged_absent": True,
            "raw_first_verified": True,
        }
        if provider_type is not None:
            source["provider_type"] = provider_type
        bundle = tmp_path / "bundle"
        bundle.mkdir(exist_ok=True)
        (bundle / "collection_summary.json").write_text(
            json.dumps({"sources": [source], "synthetic_or_replay_counted_as_real": False})
        )
        evidence = Evidence.load(bundle, None)
        return compute_scorecard(evidence)

    def test_aggregator_live_earns_partial_credit_and_caps_at_80(self, tmp_path: Path) -> None:
        result = self._bundle(tmp_path, "aggregator")
        awards = {a["reason"]: a for a in result["awards"] if a["points"] > 0}
        live = [a for r, a in awards.items() if "NON-BROKER aggregator" in r]
        assert live and live[0]["points"] == 7.0
        limits = {c["limit"] for c in result["hard_cap_reasons"]}
        assert 80 in limits
        assert 75 not in limits  # live data exists; the 75 cap must not fire

    def test_missing_provider_type_is_treated_as_non_broker(self, tmp_path: Path) -> None:
        result = self._bundle(tmp_path, None)
        limits = {c["limit"] for c in result["hard_cap_reasons"]}
        assert 80 in limits

    def test_broker_live_earns_full_credit_without_80_cap(self, tmp_path: Path) -> None:
        result = self._bundle(tmp_path, "broker")
        live = [a for a in result["awards"] if "live non-demo broker stream" in a["reason"]]
        assert live and live[0]["points"] == 15.0
        limits = {c["limit"] for c in result["hard_cap_reasons"]}
        assert 80 not in limits


class TestDaemonTruefxSource:
    def test_dry_run_requires_no_credentials(self, tmp_path: Path, capsys) -> None:
        from tools.fx_quote_collector import EX_OK, main as daemon_main

        code = daemon_main(["--output-root", str(tmp_path), "--source", "truefx", "--dry-run"])
        assert code == EX_OK
        plan = json.loads(capsys.readouterr().out)
        assert plan["credentials_required"] is False
        assert plan["tradable"] is False
