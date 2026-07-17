"""ホライズン採点・セル学習・経験帯・較正テーブルの検証(合成データ)。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from fx_intel import horizon_learning as hl
from fx_intel.horizons import HORIZON_BY_LABEL

TUESDAY_NOON = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _entry(
    ts: datetime,
    horizon: str = "1h",
    symbol: str = "USDJPY",
    direction: str = "long",
    close: float = 150.0,
    atr_h: float = 0.2,
    spread: float = 0.01,
    p=(0.55, 0.25, 0.20),
    band=(-0.25, 0.0, 0.25),
    composite: float = 0.4,
    session: str = "london",
    bucket: str = "mid",
    pit: bool = True,
):
    source = ts - timedelta(seconds=40)
    feature = ts - timedelta(seconds=5)
    row = {
        "schema_version": 1,
        "contract": "horizon-pit-v1" if pit else "other",
        "ts": ts.isoformat(),
        "prediction_time": ts.isoformat(),
        "source_cutoff": source.isoformat(),
        "max_feature_available_time": feature.isoformat(),
        "pit_eligible": pit,
        "symbol": symbol,
        "horizon": horizon,
        "horizon_hours": HORIZON_BY_LABEL[horizon].hours,
        "shadow_only": HORIZON_BY_LABEL[horizon].shadow_only,
        "direction": direction,
        "composite": composite,
        "conviction": 40,
        "p_up": p[0],
        "p_down": p[1],
        "p_flat": p[2],
        "calibrated": False,
        "close": close,
        "atr_h": atr_h,
        "spread": spread,
        "flat_threshold": max(0.1 * atr_h, 2 * spread),
        "band_p10": band[0],
        "band_p50": band[1],
        "band_p90": band[2],
        "band_source": "atr_default",
        "expected_range": band[2] - band[0],
        "data_quality": 1.0,
        "features": {"session": session, "vol_bucket": bucket},
        "gates": {},
        "warnings": [],
        "generator_version": "hf-1",
    }
    return row


def _price(ts: datetime, close: float, symbol: str = "USDJPY", timeframe: str = "15m"):
    return {"ts": ts.isoformat(), "symbol": symbol, "timeframe": timeframe, "close": close}


def test_scoring_classifies_up_down_flat_with_spread_threshold() -> None:
    base = TUESDAY_NOON
    now = base + timedelta(hours=3)
    entries = [
        _entry(base, direction="long", close=150.0),  # +0.10 → up, hit
        _entry(base, symbol="EURUSD", direction="short", close=1.10),  # +0.10 → up, miss
        # flat: 閾値 max(0.1*0.2, 2*0.04)=0.08 に対し move=+0.05
        _entry(base, symbol="GBPUSD", direction="long", close=1.30, spread=0.04),
    ]
    prices = [
        _price(base + timedelta(hours=1), 150.10),
        _price(base + timedelta(hours=1), 1.20, symbol="EURUSD"),
        _price(base + timedelta(hours=1), 1.35, symbol="GBPUSD"),
    ]
    # GBPUSDのmoveを+0.05へ調整
    prices[2] = _price(base + timedelta(hours=1), 1.35, symbol="GBPUSD")
    entries[2]["close"] = 1.30
    prices[2]["close"] = 1.35  # move=0.05 < 0.08
    result = hl.score_horizon_history(entries, prices, now)
    assert result.immature == 0 and result.unresolved == 0
    outcomes = {item.symbol: item for item in result.scored}
    assert outcomes["USDJPY"].realized_class == "up"
    assert outcomes["USDJPY"].direction_outcome == "hit"
    assert outcomes["EURUSD"].realized_class == "up"
    assert outcomes["EURUSD"].direction_outcome == "miss"
    assert outcomes["GBPUSD"].realized_class == "flat"
    assert outcomes["GBPUSD"].direction_outcome == "flat"


def test_scoring_neutral_rows_are_scored_as_class_only() -> None:
    """中立行もクラス実現(up/down/flat)は採点される=shadow還流の土台。"""
    base = TUESDAY_NOON
    entries = [_entry(base, direction="neutral")]
    prices = [_price(base + timedelta(hours=1), 150.30)]
    result = hl.score_horizon_history(entries, prices, base + timedelta(hours=2))
    assert len(result.scored) == 1
    item = result.scored[0]
    assert item.realized_class == "up"
    assert item.direction_outcome == "none"
    assert item.net_r is None  # 方向を張っていないので純Rなし


def test_scoring_respects_pit_and_maturity_and_missing_price() -> None:
    base = TUESDAY_NOON
    now = base + timedelta(hours=2)
    entries = [
        _entry(base, pit=False),  # PIT不適格
        _entry(now - timedelta(minutes=10)),  # 未成熟
        _entry(base, symbol="EURUSD", close=1.1),  # 価格なし→unresolved
    ]
    prices = [_price(base + timedelta(hours=1), 150.2)]  # USDJPYのみ
    result = hl.score_horizon_history(entries, prices, now)
    assert result.pit_ineligible == 1
    assert result.immature == 1
    assert result.unresolved == 1
    assert result.scored == []


def test_scoring_maturity_uses_open_hours_across_weekend() -> None:
    friday_20 = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    entries = [_entry(friday_20, horizon="24h", close=150.0)]
    # 月曜20時: 壁時計72hだがオープン23h → 未成熟
    monday_20 = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
    result = hl.score_horizon_history(entries, [], monday_20)
    assert result.immature == 1
    # 火曜23時: オープン26h → 成熟(価格が無いのでunresolved)
    tuesday_23 = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)
    result = hl.score_horizon_history(entries, [], tuesday_23)
    assert result.unresolved == 1


def test_brier_pinball_coverage_and_net_r() -> None:
    base = TUESDAY_NOON
    entries = [_entry(base, p=(0.55, 0.25, 0.20), band=(-0.25, 0.0, 0.25))]
    prices = [_price(base + timedelta(hours=1), 150.10)]  # move=+0.10
    result = hl.score_horizon_history(entries, prices, base + timedelta(hours=2))
    item = result.scored[0]
    # Brier: (0.55-1)^2 + 0.25^2 + 0.2^2 = 0.2025+0.0625+0.04 = 0.305
    assert item.brier == pytest.approx(0.305, abs=1e-6)
    # 帯: -0.25 <= 0.10 <= 0.25 → 包含
    assert item.band_covered is True
    # pinball p50 (pred 0.0, q=0.5): 0.5*0.10 = 0.05
    assert item.pinball_p50 == pytest.approx(0.05, abs=1e-6)
    # net_r: (0.10 - 0.01)/0.2 = 0.45
    assert item.net_r == pytest.approx(0.45, abs=1e-4)


def test_thinning_by_horizon_gap() -> None:
    base = TUESDAY_NOON
    entries = []
    prices = []
    for i in range(13):  # 5分間隔×13 = 0〜60分
        ts = base + timedelta(minutes=5 * i)
        entries.append(_entry(ts))
        prices.append(_price(ts + timedelta(hours=1), 150.2))
    result = hl.score_horizon_history(entries, prices, base + timedelta(hours=3))
    assert len(result.scored) == 13
    thinned = hl.thin_scored(result.scored, HORIZON_BY_LABEL["1h"].learn_thin_gap_hours)
    assert len(thinned) == 2  # 1時間gapで13本→2本(0分と60分)


def test_derive_learning_profiles_and_climatology() -> None:
    # 月曜00:00 UTC起点の2時間刻み×30本 = 週末を跨がず平日内に収まる
    base = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    entries = []
    prices = []
    for i in range(30):
        ts = base + timedelta(hours=2 * i)  # gap 2h > 間引き1h → 全部残る
        up = i % 2 == 0
        entries.append(_entry(ts, direction="long" if up else "short"))
        prices.append(_price(ts + timedelta(hours=1), 150.0 + (0.10 if up else -0.10)))
    result = hl.score_horizon_history(entries, prices, base + timedelta(hours=70))
    state = hl.derive_horizon_learning(result, now=base + timedelta(hours=70))
    profile = state["profiles"]["USDJPY|1h"]
    assert profile["n_scored"] > 0
    assert profile["hits"] == profile["n_directional"]  # 全部hitになる構成
    assert profile["hit_rate"] == 1.0
    assert profile["mean_brier"] is not None
    assert profile["climatology_brier"] is not None
    assert profile["calibrated"] is False  # 50件未満
    assert state["bands"]["USDJPY|1h"]["__horizon__"]["n"] >= 20


def test_calibration_table_requires_50_samples_and_provider_roundtrip() -> None:
    # 月曜00:00起点2時間刻み×56本 = 金曜14:00まで(週末を跨がない)
    base = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)
    entries = []
    prices = []
    for i in range(56):
        ts = base + timedelta(hours=2 * i)
        up = i % 3 != 0  # 約2/3がup
        entries.append(_entry(ts, direction="long", composite=0.4))
        prices.append(_price(ts + timedelta(hours=1), 150.0 + (0.10 if up else -0.10)))
    result = hl.score_horizon_history(entries, prices, base + timedelta(hours=200))
    state = hl.derive_horizon_learning(result, now=TUESDAY_NOON)
    profile = state["profiles"]["USDJPY|1h"]
    assert profile["n_scored"] >= 50
    assert profile["calibrated"] is True
    provider = hl.make_calibration_provider(state)
    calibrated = provider("USDJPY", "1h", 0.4)
    assert calibrated is not None
    p_up, p_down, p_flat = calibrated
    assert p_up == pytest.approx(2 / 3, abs=0.05)
    assert p_up + p_down + p_flat == pytest.approx(1.0, abs=1e-6)
    # 別セルは較正なし
    assert provider("EURUSD", "1h", 0.4) is None


def test_band_provider_fallback_chain(tmp_path) -> None:
    base = TUESDAY_NOON - timedelta(days=5)
    entries = []
    prices = []
    # londonセッション/midバケット40件 + tokyo 5件 → tokyoはバケット帯なし→全体帯へ縮退
    for i in range(45):
        session = "london" if i < 40 else "tokyo"
        ts = base + timedelta(hours=2 * i)
        entries.append(_entry(ts, session=session))
        prices.append(_price(ts + timedelta(hours=1), 150.0 + 0.05 * ((i % 5) - 2)))
    result = hl.score_horizon_history(entries, prices, base + timedelta(days=10))
    state = hl.derive_horizon_learning(result, now=TUESDAY_NOON)
    provider = hl.make_band_provider(state)
    bucket_band = provider("USDJPY", "1h", "mid", "london")
    assert bucket_band is not None and bucket_band[3] == "vol_session"
    fallback_band = provider("USDJPY", "1h", "mid", "tokyo")
    assert fallback_band is not None and fallback_band[3] == "horizon_all"
    assert provider("USDJPY", "3d", "mid", "london") is None  # データなし

    # 永続化ラウンドトリップ
    path = tmp_path / "horizon_learning.json"
    hl.save_horizon_learning(state, path)
    loaded = hl.load_horizon_learning(path)
    assert loaded is not None
    assert loaded["profiles"]["USDJPY|1h"]["n_scored"] == state["profiles"]["USDJPY|1h"]["n_scored"]
