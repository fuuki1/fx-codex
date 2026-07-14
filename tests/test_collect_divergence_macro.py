"""Cross-source divergence and ALFRED macro-PIT tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
from pathlib import Path

import pytest

from data_platform.collect.contract import CollectedQuote
from data_platform.collect.divergence import (
    DivergenceInputError,
    DivergenceThresholds,
    compare_sources,
)
from data_platform.collect.fred_macro import (
    MacroPITError,
    MacroPITLog,
    as_of,
    capture_vintage,
    parse_vintage_csv,
    vintage_url,
)
from data_platform.raw.immutable_store import ImmutableRawStore

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
SHA = "1" * 64


def _quote(provider: str, offset_ms: float, bid: float, ask: float) -> CollectedQuote:
    stamp = NOW + timedelta(milliseconds=offset_ms)
    return CollectedQuote(
        provider=provider,
        account_environment="datafeed",
        instrument="USDJPY",
        provider_event_time=stamp,
        received_at=stamp + timedelta(milliseconds=50),
        bid=bid,
        ask=ask,
        bid_size=None,
        ask_size=None,
        tradable=False,
        sequence_id=None,
        connection_id="c",
        writer_id="w",
        revision_id=None,
        raw_payload_sha256=SHA,
        source_endpoint_class="historical_datafeed",
        collection_mode="historical_download",
    )


class TestDivergence:
    def test_same_provider_is_not_independent(self) -> None:
        a = [_quote("dukascopy", 0, 155.000, 155.003)]
        b = [_quote("dukascopy", 0, 155.001, 155.004)]
        with pytest.raises(DivergenceInputError, match="independent"):
            compare_sources(a, b, pip_size=0.01)

    def test_close_sources_are_usable(self) -> None:
        a = [_quote("dukascopy", i * 100, 155.000, 155.003) for i in range(10)]
        b = [_quote("histdata", i * 100 + 20, 155.001, 155.004) for i in range(10)]
        report = compare_sources(a, b, pip_size=0.01)
        assert report["divergence_state"] == "usable"
        assert report["matched_quotes"] == 10
        assert report["metrics"]["mid_diff_pips"]["max"] == pytest.approx(0.1)

    def test_breach_degrades_never_averages(self) -> None:
        a = [_quote("dukascopy", 0, 155.000, 155.003)]
        b = [_quote("histdata", 10, 155.050, 155.053)]  # 5 pips apart
        report = compare_sources(a, b, pip_size=0.01)
        assert report["divergence_state"] == "degraded"
        assert any("mid_diff" in reason for reason in report["divergence_reason"])
        assert "never averaged" in report["policy"]

    def test_extreme_breach_quarantines(self) -> None:
        a = [_quote("dukascopy", 0, 155.000, 155.003)]
        b = [_quote("histdata", 10, 155.200, 155.203)]  # 20 pips apart
        report = compare_sources(a, b, pip_size=0.01)
        assert report["divergence_state"] == "quarantined"

    def test_no_alignment_is_unavailable(self) -> None:
        a = [_quote("dukascopy", 0, 155.000, 155.003)]
        b = [_quote("histdata", 60_000, 155.001, 155.004)]  # 1 min apart
        report = compare_sources(a, b, pip_size=0.01, align_tolerance_seconds=2.0)
        assert report["divergence_state"] == "unavailable"
        assert report["divergence_reason"] == ["no_aligned_quotes"]

    def test_threshold_ordering_enforced(self) -> None:
        with pytest.raises(DivergenceInputError):
            DivergenceThresholds(max_mid_diff_pips=10.0, quarantine_mid_diff_pips=5.0)


VINTAGE_CSV = "observation_date,GDPC1_20240201\n2023-07-01,22780.933\n2023-10-01,22672.859\n"


class TestMacroVintageParsing:
    def test_parse_requires_vintage_stamped_header(self) -> None:
        rows = parse_vintage_csv(
            VINTAGE_CSV.encode(),
            series_id="GDPC1",
            vintage=date(2024, 2, 1),
            source_uri="https://alfred.stlouisfed.org/graph/alfredgraph.csv?...",
            received_at=NOW,
        )
        assert len(rows) == 2
        assert rows[1].value == pytest.approx(22672.859)
        assert rows[1].available_at == NOW  # availability = capture time
        assert rows[1].provider_released_at is None  # never fabricated

    def test_unstamped_header_rejected_as_non_pit(self) -> None:
        current_csv = "observation_date,GDPC1\n2023-10-01,23033.780\n"
        with pytest.raises(MacroPITError, match="ignored vintage_date"):
            parse_vintage_csv(
                current_csv.encode(),
                series_id="GDPC1",
                vintage=date(2024, 2, 1),
                source_uri="u",
                received_at=NOW,
            )

    def test_missing_cell_stays_missing(self) -> None:
        csv = "observation_date,GDPC1_20240201\n2023-10-01,.\n"
        rows = parse_vintage_csv(
            csv.encode(),
            series_id="GDPC1",
            vintage=date(2024, 2, 1),
            source_uri="u",
            received_at=NOW,
        )
        assert rows[0].value is None  # '.' is missing, never 0

    def test_unknown_series_rejected(self) -> None:
        with pytest.raises(MacroPITError, match="registry"):
            parse_vintage_csv(
                VINTAGE_CSV.encode(),
                series_id="NOPE",
                vintage=date(2024, 2, 1),
                source_uri="u",
                received_at=NOW,
            )


class TestMacroCaptureAndAsOf:
    def _capture(self, tmp_path: Path, vintage: date, value: str, when: datetime) -> MacroPITLog:
        store = ImmutableRawStore(tmp_path / "raw")
        log = MacroPITLog(tmp_path / "macro.jsonl")
        stamp = vintage.strftime("%Y%m%d")
        body = f"observation_date,GDPC1_{stamp}\n2023-10-01,{value}\n".encode()
        capture_vintage(
            "GDPC1",
            vintage,
            date(2023, 10, 1),
            date(2023, 10, 1),
            fetcher=lambda _u: (200, body),
            store=store,
            log=log,
            now=lambda: when,
        )
        return log

    def test_as_of_blocks_values_captured_later(self, tmp_path: Path) -> None:
        log = self._capture(tmp_path, date(2024, 2, 1), "22672.859", NOW)
        before = as_of(log, "GDPC1", NOW - timedelta(hours=1))
        assert before == []  # captured after prediction time -> invisible
        after = as_of(log, "GDPC1", NOW + timedelta(hours=1))
        assert len(after) == 1 and after[0]["value"] == pytest.approx(22672.859)

    def test_revisions_do_not_leak_backwards(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = MacroPITLog(tmp_path / "macro.jsonl")
        for vintage, value, when in (
            (date(2024, 2, 1), "22672.859", NOW),
            (date(2024, 4, 5), "23033.780", NOW + timedelta(days=1)),
        ):
            stamp = vintage.strftime("%Y%m%d")
            body = f"observation_date,GDPC1_{stamp}\n2023-10-01,{value}\n".encode()
            capture_vintage(
                "GDPC1",
                vintage,
                date(2023, 10, 1),
                date(2023, 10, 1),
                fetcher=lambda _u, b=body: (200, b),
                store=store,
                log=log,
                now=lambda w=when: w,
            )
        early = as_of(log, "GDPC1", NOW + timedelta(hours=1))
        assert early[0]["value"] == pytest.approx(22672.859)  # initial only
        late = as_of(log, "GDPC1", NOW + timedelta(days=2))
        assert late[0]["value"] == pytest.approx(23033.780)  # revision after its capture
        # both vintages remain stored separately (revision never overwrites)
        assert len(log.rows()) == 2

    def test_http_error_fails_closed(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = MacroPITLog(tmp_path / "macro.jsonl")
        with pytest.raises(MacroPITError, match="HTTP 503"):
            capture_vintage(
                "GDPC1",
                date(2024, 2, 1),
                date(2023, 10, 1),
                date(2023, 10, 1),
                fetcher=lambda _u: (503, b""),
                store=store,
                log=log,
            )
        assert log.rows() == []

    def test_naive_prediction_time_rejected(self, tmp_path: Path) -> None:
        log = MacroPITLog(tmp_path / "macro.jsonl")
        with pytest.raises(MacroPITError, match="timezone-aware"):
            as_of(log, "GDPC1", datetime(2026, 7, 14))

    def test_capture_is_raw_first(self, tmp_path: Path) -> None:
        store = ImmutableRawStore(tmp_path / "raw")
        log = MacroPITLog(tmp_path / "macro.jsonl")
        body = b"observation_date,GDPC1_20240201\n2023-10-01,22672.859\n"
        rows = capture_vintage(
            "GDPC1",
            date(2024, 2, 1),
            date(2023, 10, 1),
            date(2023, 10, 1),
            fetcher=lambda _u: (200, body),
            store=store,
            log=log,
        )
        sha = hashlib.sha256(body).hexdigest()
        assert rows[0].raw_payload_sha256 == sha
        assert store.get(sha) == body  # raw stored and retrievable

    def test_vintage_url_shape(self) -> None:
        url = vintage_url("CPIAUCSL", date(2024, 3, 15), date(2024, 1, 1), date(2024, 6, 1))
        assert "alfredgraph.csv" in url and "vintage_date=2024-03-15" in url
