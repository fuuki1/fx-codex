from datetime import datetime, UTC

from fx_intel.market_session import (
    SESSION_CLOSED,
    SESSION_LONDON,
    SESSION_LONDON_NEW_YORK,
    SESSION_NEW_YORK,
    SESSION_TOKYO,
    SESSION_TOKYO_LONDON,
    build_learning_dimensions,
    classify_market_session,
    dimensions_from_mapping,
)


def test_tokyo_and_london_overlap_follow_local_hours() -> None:
    assert classify_market_session(datetime(2026, 1, 12, 1, tzinfo=UTC))[0] == SESSION_TOKYO
    assert (
        classify_market_session(datetime(2026, 1, 12, 8, 30, tzinfo=UTC))[0] == SESSION_TOKYO_LONDON
    )


def test_london_and_new_york_dst_boundaries() -> None:
    # US DST has started but UK DST has not: 12:30 UTC is both local sessions.
    assert (
        classify_market_session(datetime(2026, 3, 20, 12, 30, tzinfo=UTC))[0]
        == SESSION_LONDON_NEW_YORK
    )
    # Summer London closes at 16 UTC, leaving New York on its own.
    assert classify_market_session(datetime(2026, 7, 17, 16, 30, tzinfo=UTC))[0] == SESSION_NEW_YORK
    # Winter London opens at 08 UTC.
    assert classify_market_session(datetime(2026, 1, 12, 7, 59, tzinfo=UTC))[0] == SESSION_TOKYO
    assert classify_market_session(datetime(2026, 1, 12, 9, 0, tzinfo=UTC))[0] == SESSION_LONDON


def test_weekend_is_closed_before_session_classification() -> None:
    assert classify_market_session(datetime(2026, 7, 18, 8, tzinfo=UTC)) == (
        SESSION_CLOSED,
        (),
    )


def test_dimensions_capture_regime_source_and_old_session_derivation() -> None:
    now = datetime(2026, 7, 17, 9, 15, tzinfo=UTC)
    dimensions = build_learning_dimensions(
        now,
        regime="risk_off",
        analysis_engine="analyst",
        macro_available=True,
    ).to_dict()
    assert dimensions["session_bucket"] == SESSION_LONDON
    assert dimensions["regime"] == "risk_off"
    assert dimensions["regime_source"] == "macro_real_data"

    derived = dimensions_from_mapping({}, fallback_ts=now)
    assert derived["session_bucket"] == SESSION_LONDON
    assert derived["regime"] == "unknown"
