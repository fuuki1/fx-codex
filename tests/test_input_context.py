"""Shared point-in-time macro/input context contracts."""

from __future__ import annotations

from datetime import date, datetime, timedelta, UTC

from fx_intel import input_context, liquidity
from fx_intel.macro import CotReport, MacroSeries, MacroSnapshot, SeriesPoint
from fx_intel.technicals import PairTechnicals, build_interval_view

OPEN_NOW = datetime(2026, 6, 29, 9, 0, tzinfo=UTC)


def _view(interval: str, recommendation: str, close: float, atr: float):
    return build_interval_view(
        interval,
        {"RECOMMENDATION": recommendation, "BUY": 10, "SELL": 3, "NEUTRAL": 5},
        {
            "close": close,
            "RSI": 55.0,
            "ADX": 25.0,
            "ATR": atr,
            "SMA20": close * 1.001,
            "SMA100": close,
        },
        20,
        100,
    )


def _all_up_tech() -> PairTechnicals:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {
        "15m": _view("15m", "BUY", 156.20, 0.08),
        "1h": _view("1h", "BUY", 156.25, 0.15),
        "4h": _view("4h", "STRONG_BUY", 156.30, 0.30),
        "1d": _view("1d", "BUY", 156.10, 0.80),
    }
    return tech


def _macro_snapshot(fetched_at: datetime) -> MacroSnapshot:
    points = [SeriesPoint(date(2026, 6, 20 + index), 18.0 + index) for index in range(6)]
    snapshot = MacroSnapshot(
        fetched_at=fetched_at,
        series={
            "vix": MacroSeries("vix", "VIX", points),
            "us10y": MacroSeries(
                "us10y",
                "米10年",
                [SeriesPoint(point.when, 4.0 + i * 0.01) for i, point in enumerate(points)],
            ),
            "us2y": MacroSeries(
                "us2y",
                "米2年",
                [SeriesPoint(point.when, 3.8 + i * 0.01) for i, point in enumerate(points)],
            ),
            "usd_index": MacroSeries(
                "usd_index",
                "ドル",
                [SeriesPoint(point.when, 100.0 + i) for i, point in enumerate(points)],
            ),
        },
        cot={
            # fresh_cot のPIT証跡要件(available_time・出典ID・sha256ハッシュ・
            # 公表時刻の自己検証フラグ)を満たす監査済みレポートにする
            "USD": CotReport(
                "USD",
                date(2026, 6, 23),
                net_position=20,
                open_interest=100,
                available_time=fetched_at - timedelta(hours=1),
                source_record_id="source-USD",
                content_hash="a" * 64,
                dataset_id="b" * 64,
                data_quality_flags=("publication_time_attested_locally",),
            ),
            "JPY": CotReport(
                "JPY",
                date(2026, 6, 23),
                net_position=-10,
                open_interest=100,
                available_time=fetched_at - timedelta(hours=1),
                source_record_id="source-JPY",
                content_hash="c" * 64,
                dataset_id="b" * 64,
                data_quality_flags=("publication_time_attested_locally",),
            ),
        },
    )
    snapshot.cot_evidence = {
        "status": "ok",
        "usable": True,
        "dataset_id": "b" * 64,
        "prediction_time": fetched_at.isoformat(),
        "record_hashes": ["a" * 64, "c" * 64],
    }
    for key in ("vix", "us10y", "us2y", "usd_index"):
        snapshot.provenance[key] = {
            "source": "fred",
            "fetched_at": fetched_at.isoformat(),
            "first_seen_time": fetched_at.isoformat(),
            "content_hash": f"hash-{key}",
        }
    snapshot.provenance["cot"] = {
        "source": "cftc",
        "fetched_at": fetched_at.isoformat(),
        "first_seen_time": fetched_at.isoformat(),
        "content_hash": "hash-cot",
    }
    return snapshot


def _unknown_liquidity(now: datetime) -> input_context.LiquiditySnapshot:
    return liquidity.build_liquidity_snapshot(
        "USDJPY",
        decision_time=now,
        quote=None,
        price_rows=[],
        session_bucket="london",
        policy=liquidity.LiquidityPolicy(min_baseline_samples=2),
    )


def test_macro_features_preserve_values_masks_and_provenance() -> None:
    snapshot = input_context.build_macro_feature_snapshot(
        _macro_snapshot(OPEN_NOW - timedelta(minutes=1)),
        "USDJPY",
        decision_time=OPEN_NOW,
    )

    assert snapshot.features["vix_level"] == 23.0
    assert snapshot.features["vix_change_5d_pct"] is not None
    assert snapshot.features["curve_2s10s_bp"] == 20.0
    assert snapshot.features["cot_pair_diff"] == 0.3
    assert snapshot.feature_masks["macro_pair_score"] == 1
    assert snapshot.values["vix_level"].source == "fred"
    assert snapshot.values["vix_level"].available_time < OPEN_NOW.isoformat()


def test_future_macro_provenance_is_not_used() -> None:
    snapshot = input_context.build_macro_feature_snapshot(
        _macro_snapshot(OPEN_NOW + timedelta(minutes=1)),
        "USDJPY",
        decision_time=OPEN_NOW,
    )

    assert snapshot.quality_status == "invalid"
    assert all(value is None for value in snapshot.features.values())
    assert all(mask == 0 for mask in snapshot.feature_masks.values())
