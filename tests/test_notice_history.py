"""Detailed-notice historical OHLC sourcing tests."""

from __future__ import annotations

import lzma
import struct
from datetime import datetime, timedelta, UTC

from fx_intel import notice_history
from fx_intel.briefing import TradePlan
from fx_intel.market_structure import EntryLevels, OhlcBar

_STRUCT = struct.Struct(">IIIff")
BASE = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)


def _make_bi5(records: list[tuple[int, int, int, float, float]]) -> bytes:
    payload = b"".join(_STRUCT.pack(*record) for record in records)
    return lzma.compress(payload)


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, by_hour: dict[int, bytes | None]) -> None:
        self.by_hour = by_hour
        self.calls: list[str] = []

    def get(self, url: str, headers=None, timeout=None) -> _FakeResponse:
        self.calls.append(url)
        hour = int(url.rsplit("/", 1)[1][:2])
        body = self.by_hour.get(hour)
        if body is None:
            return _FakeResponse(b"", status_code=404)
        return _FakeResponse(body)


def _plan() -> TradePlan:
    return TradePlan(
        symbol="USDJPY",
        direction="long",
        conviction=52,
        composite=0.52,
        tech_score=0.55,
        news_score=0.1,
        close=143.55,
        atr=0.08,
        stop=143.35,
        target1=143.75,
        target2=143.95,
    )


def test_dukascopy_notice_bars_uses_only_finalized_bars(tmp_path) -> None:
    # 10:00 bar is finalized by 10:20; 10:15 bar is still forming and must be ignored.
    raw = _make_bi5(
        [
            (0, 143500, 143480, 1.0, 1.0),
            (2 * 60_000, 143520, 143500, 1.0, 1.0),
            (16 * 60_000, 143900, 143880, 1.0, 1.0),
        ]
    )
    session = _FakeSession({9: None, 10: raw})
    result = notice_history.dukascopy_notice_bars(
        ["USDJPY"],
        now=BASE + timedelta(minutes=20),
        cache_dir=tmp_path,
        timeframe="15m",
        hours_back=1,
        session=session,
    )

    assert result.warnings == []
    bars = result.bars_by_symbol["USDJPY"]
    assert len(bars) == 1
    assert bars[0].timestamp == BASE
    assert bars[0].high < 143.9  # incomplete 10:15 spike was not included


def test_entry_levels_from_bars_builds_for_matching_plan() -> None:
    bars = [
        OhlcBar(BASE + timedelta(minutes=15 * i), 143.4, high, low, 143.5)
        for i, (high, low) in enumerate(
            [
                (143.55, 143.42),
                (143.58, 143.40),
                (143.60, 143.38),
                (143.57, 143.41),
                (143.62, 143.43),
                (143.59, 143.44),
            ]
        )
    ]

    levels = notice_history.entry_levels_from_bars([_plan()], {"USDJPY": bars}, lookback_bars=6)

    assert "USDJPY" in levels
    assert levels["USDJPY"].source == "recent_ohlc"


def test_merge_entry_levels_keeps_primary() -> None:
    primary = EntryLevels("USDJPY", "long", 1, 2, 3, 4, 1, 4, 1, 4, "csv", 10)
    fallback = EntryLevels("USDJPY", "long", 5, 6, 7, 8, 5, 8, 5, 8, "dukas", 10)

    merged = notice_history.merge_entry_levels({"USDJPY": primary}, {"USDJPY": fallback})

    assert merged["USDJPY"] is primary
