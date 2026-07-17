"""マルチホライズン予測トラック(設計A)の生成器・定数・ジャーナル検証。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, UTC

import pytest

from fx_intel import horizon_forecast, horizon_journal, horizons
from fx_intel.calendar import EconomicEvent, RiskWindow
from fx_intel.sentiment import CurrencySentiment
from fx_intel.technicals import PairTechnicals, build_interval_view

# 2026-07-14(火) 12:00 UTC — 平日・イベントなし
TUESDAY_NOON = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _view(interval, rec, close, rsi=55.0, adx=25.0, atr=0.15):
    summary = {"RECOMMENDATION": rec, "BUY": 10, "SELL": 3, "NEUTRAL": 5}
    indicators = {
        "close": close,
        "RSI": rsi,
        "ADX": adx,
        "ATR": atr,
        "SMA20": close * 1.001,
        "SMA100": close,
    }
    return build_interval_view(interval, summary, indicators, 20, 100)


def _tech(symbol="USDJPY", rec="BUY"):
    tech = PairTechnicals(symbol=symbol)
    tech.views = {
        "15m": _view("15m", rec, 156.20, atr=0.08),
        "1h": _view("1h", rec, 156.25, atr=0.15),
        "4h": _view("4h", rec, 156.30, atr=0.30),
        "1d": _view("1d", rec, 156.10, atr=0.80),
    }
    return tech


def _currencies(usd=0.3, jpy=-0.3):
    return {
        "USD": CurrencySentiment("USD", usd, 3),
        "JPY": CurrencySentiment("JPY", jpy, 3),
    }


def _build(**kwargs):
    defaults = dict(
        symbol="USDJPY",
        tech=_tech(),
        currency_scores=_currencies(),
        windows=[],
        news_items=[],
        now=TUESDAY_NOON,
    )
    defaults.update(kwargs)
    return horizon_forecast.build_horizon_forecasts(**defaults)


# ---------------------------------------------------------------------------
# 定数層


def test_horizon_specs_are_nine_with_5m_permanent_shadow() -> None:
    labels = [spec.label for spec in horizons.HORIZON_SPECS]
    assert labels == ["5m", "15m", "30m", "1h", "3h", "6h", "12h", "24h", "3d"]
    assert "9h" not in labels  # ユーザー決定: 9hは入れない
    shadow = {spec.label for spec in horizons.HORIZON_SPECS if spec.shadow_only}
    assert shadow == {"5m"}
    assert horizons.PRODUCTION_HORIZON_LABELS == (
        "15m",
        "30m",
        "1h",
        "3h",
        "6h",
        "12h",
        "24h",
        "3d",
    )


def test_prior_weights_sum_to_one_for_all_horizons() -> None:
    for label, weights in horizons.PRIOR_WEIGHTS.items():
        assert abs(sum(weights.values()) - 1.0) < 1e-9, label
        assert set(weights) == {"15m", "1h", "4h", "1d", "news"}


def test_thin_gaps_follow_half_horizon_rule() -> None:
    for spec in horizons.HORIZON_SPECS:
        assert spec.learn_thin_gap_hours >= min(spec.hours / 2, spec.hours)
        assert spec.ml_thin_gap_hours > spec.learn_thin_gap_hours


def test_flat_threshold_takes_max_of_atr_and_spread() -> None:
    # ユーザー決定: max(ATR×0.1, 実測スプレッド×2)
    assert horizons.flat_threshold(atr_h=0.5, spread=0.01) == pytest.approx(0.05)  # ATR側
    assert horizons.flat_threshold(atr_h=0.1, spread=0.02) == pytest.approx(0.04)  # スプレッド側
    assert horizons.flat_threshold(atr_h=None, spread=0.02) == pytest.approx(0.04)
    assert horizons.flat_threshold(atr_h=0.5, spread=None) == pytest.approx(0.05)
    assert horizons.flat_threshold(None, None) == 0.0


def test_atr_for_horizon_scales_by_sqrt_time() -> None:
    atrs = {"1h": 0.20}
    scaled = horizons.atr_for_horizon(atrs, 4.0)
    assert scaled == pytest.approx(0.20 * 2.0)  # √4
    assert horizons.atr_for_horizon({}, 4.0) is None


def test_session_label_bands() -> None:
    assert horizons.session_label(datetime(2026, 7, 14, 2, 0, tzinfo=UTC)) == "tokyo"
    assert horizons.session_label(datetime(2026, 7, 14, 9, 0, tzinfo=UTC)) == "london"
    assert horizons.session_label(datetime(2026, 7, 14, 14, 0, tzinfo=UTC)) == "ldn_ny"
    assert horizons.session_label(datetime(2026, 7, 14, 19, 0, tzinfo=UTC)) == "ny"
    assert horizons.session_label(datetime(2026, 7, 14, 23, 0, tzinfo=UTC)) == "off"


# ---------------------------------------------------------------------------
# 生成器


def test_build_generates_nine_forecasts_with_direction_and_probabilities() -> None:
    forecasts = _build()
    assert len(forecasts) == 9
    by_label = {forecast.horizon: forecast for forecast in forecasts}
    assert by_label["5m"].shadow_only is True
    assert by_label["24h"].shadow_only is False
    for forecast in forecasts:
        # 強い買いシグナル+順風ニュースなのでlong
        assert forecast.direction == "long"
        assert forecast.p_up + forecast.p_down + forecast.p_flat == pytest.approx(1.0, abs=1e-6)
        assert forecast.calibrated is False  # v1は較正前
        assert forecast.p_up > forecast.p_down  # 買い方向に傾く
        assert forecast.band_source == "atr_default"
        assert forecast.expected_range is not None and forecast.expected_range > 0
        assert forecast.weights  # 使用重みを記録
        assert forecast.features["session"] == "ldn_ny"


def test_horizon_weights_differ_between_short_and_long() -> None:
    forecasts = {f.horizon: f for f in _build()}
    # 短期は15m足の重みが最大、長期は4h/1dが重い(prior表の反映)
    assert forecasts["15m"].weights["w_15m"] > forecasts["24h"].weights["w_15m"]
    assert forecasts["24h"].weights["w_1d"] > forecasts["15m"].weights["w_1d"]


def test_weak_signal_gives_neutral() -> None:
    tech = _tech(rec="NEUTRAL")
    forecasts = _build(tech=tech, currency_scores=_currencies(usd=0.0, jpy=0.0))
    assert all(f.direction == "neutral" for f in forecasts)


def test_event_window_forces_standby_with_cap() -> None:
    event = EconomicEvent(
        when=TUESDAY_NOON + timedelta(minutes=30),
        currency="USD",
        title="CPI",
        impact="high",
    )
    window = RiskWindow(
        event=event,
        start=TUESDAY_NOON - timedelta(hours=1),
        end=TUESDAY_NOON + timedelta(hours=1),
    )
    forecasts = _build(windows=[window])
    assert all(f.direction == "standby" for f in forecasts)
    assert all(f.conviction <= 25 for f in forecasts)


def test_weekend_forces_closed() -> None:
    saturday = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    forecasts = _build(now=saturday)
    assert all(f.direction == "closed" for f in forecasts)
    assert all(f.conviction == 0 for f in forecasts)


def test_freshness_gate_forces_neutral_zero() -> None:
    forecasts = _build(operational_data_ok=False, operational_data_reason="stale")
    assert all(f.direction == "neutral" and f.conviction == 0 for f in forecasts)
    assert all(any("鮮度ゲート" in w for w in f.warnings) for f in forecasts)


def test_missing_all_views_yields_neutral_without_band() -> None:
    tech = PairTechnicals(symbol="USDJPY")
    tech.views = {}
    forecasts = _build(tech=tech)
    for forecast in forecasts:
        assert forecast.direction == "neutral"
        assert forecast.band_source == "unavailable"
        assert forecast.atr_h is None


def test_spread_recorded_and_widens_flat_threshold() -> None:
    narrow = {f.horizon: f for f in _build(spread=None)}
    wide = {f.horizon: f for f in _build(spread=0.5)}
    assert wide["15m"].flat_threshold > narrow["15m"].flat_threshold
    assert wide["15m"].spread == 0.5


def test_calibration_provider_overrides_probabilities() -> None:
    def provider(symbol, horizon, composite):
        if horizon == "1h":
            return (0.6, 0.2, 0.2)
        return None

    forecasts = {f.horizon: f for f in _build(calibration_provider=provider)}
    assert forecasts["1h"].calibrated is True
    assert forecasts["1h"].p_up == 0.6
    assert forecasts["15m"].calibrated is False


def test_band_provider_overrides_default_band() -> None:
    def provider(symbol, horizon, bucket, session):
        return (-0.3, 0.01, 0.35, "vol_session")

    forecasts = {f.horizon: f for f in _build(band_provider=provider)}
    assert forecasts["3h"].band_source == "vol_session"
    assert forecasts["3h"].band_p90 == pytest.approx(0.35)
    assert forecasts["3h"].expected_range == pytest.approx(0.65)


def test_uncalibrated_probability_simplex_at_extremes() -> None:
    for composite in (-1.0, -0.5, 0.0, 0.5, 1.0):
        p_up, p_down, p_flat = horizon_forecast._uncalibrated_probabilities(composite)
        assert p_up + p_down + p_flat == pytest.approx(1.0, abs=1e-6)
        assert min(p_up, p_down, p_flat) >= 0.05


# ---------------------------------------------------------------------------
# ジャーナル(horizon-pit-v1)


def test_journal_appends_rows_with_pit_provenance(tmp_path) -> None:
    path = tmp_path / "horizon.jsonl"
    forecasts = _build()
    source = TUESDAY_NOON - timedelta(seconds=40)
    feature = TUESDAY_NOON - timedelta(seconds=5)
    written = horizon_journal.append_horizon_forecasts(
        path,
        forecasts,
        prediction_time=TUESDAY_NOON,
        source_cutoff=source,
        max_feature_available_time=feature,
    )
    assert written == 9
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {row["horizon"] for row in rows} == {
        "5m",
        "15m",
        "30m",
        "1h",
        "3h",
        "6h",
        "12h",
        "24h",
        "3d",
    }
    for row in rows:
        assert row["contract"] == "horizon-pit-v1"
        assert horizon_journal.is_pit_eligible_horizon_entry(row)
        assert row["shadow_only"] == (row["horizon"] == "5m")


def test_journal_rejects_pit_order_violation(tmp_path) -> None:
    path = tmp_path / "horizon.jsonl"
    forecasts = _build()
    with pytest.raises(horizon_journal.HorizonPointInTimeError):
        horizon_journal.append_horizon_forecasts(
            path,
            forecasts,
            prediction_time=TUESDAY_NOON,
            source_cutoff=TUESDAY_NOON + timedelta(seconds=1),  # 未来のcutoff=違反
            max_feature_available_time=TUESDAY_NOON,
        )
    assert not path.exists()  # 1行も書かない


def test_journal_rejects_naive_datetime(tmp_path) -> None:
    with pytest.raises(horizon_journal.HorizonPointInTimeError):
        horizon_journal.append_horizon_forecasts(
            tmp_path / "horizon.jsonl",
            _build(),
            prediction_time=datetime(2026, 7, 14, 12, 0),  # naive
            source_cutoff=TUESDAY_NOON,
            max_feature_available_time=TUESDAY_NOON,
        )


def test_pit_eligibility_rejects_tampered_rows(tmp_path) -> None:
    path = tmp_path / "horizon.jsonl"
    horizon_journal.append_horizon_forecasts(
        path,
        _build()[:1],
        prediction_time=TUESDAY_NOON,
        source_cutoff=TUESDAY_NOON - timedelta(seconds=30),
        max_feature_available_time=TUESDAY_NOON - timedelta(seconds=5),
    )
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    tampered = dict(row)
    tampered["source_cutoff"] = (TUESDAY_NOON + timedelta(seconds=9)).isoformat()
    assert not horizon_journal.is_pit_eligible_horizon_entry(tampered)
    legacy = {key: value for key, value in row.items() if key != "contract"}
    assert not horizon_journal.is_pit_eligible_horizon_entry(legacy)
