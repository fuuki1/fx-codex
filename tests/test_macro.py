"""マクロデータ層(macro.py)のテスト(ネットワーク不要)。

キャッシュを事前に仕込むことで fetch_macro_snapshot をオフライン検証する。
"""

from __future__ import annotations

import json
from datetime import date, datetime, UTC

from fx_intel.macro import (
    CotReport,
    MacroSeries,
    MacroSnapshot,
    SeriesPoint,
    cot_pair_score,
    fetch_macro_snapshot,
    macro_pair_view,
    parse_cot_json,
    parse_fred_csv,
    parse_stooq_csv,
    regime_pair_score,
)

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


def test_parse_fred_csv_skips_missing_values() -> None:
    text = "DATE,DGS10\n2026-06-29,4.20\n2026-06-30,.\n2026-07-01,4.25\n"
    points = parse_fred_csv(text)
    assert len(points) == 2
    assert points[0].when == date(2026, 6, 29)
    assert points[-1].value == 4.25


def test_parse_stooq_csv_reads_close() -> None:
    text = "Date,Open,High,Low,Close,Volume\n2026-07-01,155.0,155.5,154.8,155.2,0\n"
    points = parse_stooq_csv(text)
    assert len(points) == 1
    assert points[0].value == 155.2


def test_parse_cot_json_picks_latest_and_prev() -> None:
    payload = [
        {
            "cftc_contract_market_code": "097741",  # JPY
            "report_date_as_yyyy_mm_dd": "2026-06-30",
            "noncomm_positions_long_all": "10000",
            "noncomm_positions_short_all": "40000",
            "open_interest_all": "200000",
        },
        {
            "cftc_contract_market_code": "097741",
            "report_date_as_yyyy_mm_dd": "2026-06-23",
            "noncomm_positions_long_all": "12000",
            "noncomm_positions_short_all": "38000",
            "open_interest_all": "195000",
        },
    ]
    reports = parse_cot_json(payload)
    assert "JPY" in reports
    assert reports["JPY"].report_date == date(2026, 6, 30)
    assert reports["JPY"].net_position == -30000
    assert reports["JPY"].prev_net_position == -26000


def test_series_change_and_staleness() -> None:
    series = MacroSeries(
        key="us10y",
        label_ja="米10年金利",
        points=[
            SeriesPoint(date(2026, 6, 24), 4.5),
            SeriesPoint(date(2026, 6, 25), 4.4),
            SeriesPoint(date(2026, 6, 26), 4.3),
            SeriesPoint(date(2026, 6, 27), 4.2),
            SeriesPoint(date(2026, 6, 28), 4.1),
            SeriesPoint(date(2026, 6, 29), 4.0),
        ],
    )
    assert series.change(5) == -0.5
    assert not series.is_stale(NOW)
    old = MacroSeries(key="vix", label_ja="VIX", points=[SeriesPoint(date(2026, 1, 1), 20.0)])
    assert old.is_stale(NOW)


def test_regime_risk_off_from_vix_spike() -> None:
    snap = MacroSnapshot(fetched_at=NOW)
    snap.series["vix"] = MacroSeries(
        key="vix",
        label_ja="VIX",
        points=[
            SeriesPoint(date(2026, 6, 26), 15.0),
            SeriesPoint(date(2026, 6, 27), 16.0),
            SeriesPoint(date(2026, 6, 28), 18.0),
            SeriesPoint(date(2026, 6, 29), 22.0),
            SeriesPoint(date(2026, 6, 30), 25.0),
            SeriesPoint(date(2026, 7, 1), 28.0),
        ],
    )
    regime, reason = snap.regime()
    assert regime == "risk_off"
    assert "VIX" in reason


def test_regime_neutral_without_data() -> None:
    snap = MacroSnapshot(fetched_at=NOW)
    regime, _ = snap.regime()
    assert regime == "neutral"


def test_cot_pair_score_direction() -> None:
    snap = MacroSnapshot(fetched_at=NOW)
    snap.cot["USD"] = CotReport("USD", date(2026, 6, 30), net_position=60000, open_interest=200000)
    snap.cot["JPY"] = CotReport("JPY", date(2026, 6, 30), net_position=-40000, open_interest=200000)
    result = cot_pair_score("USD", "JPY", snap)
    assert result is not None
    score, _ = result
    assert score > 0  # USDロング偏重 vs JPYショート偏重 → USD/JPYロング寄り


def test_cot_pair_score_none_when_missing() -> None:
    snap = MacroSnapshot(fetched_at=NOW)
    snap.cot["USD"] = CotReport("USD", date(2026, 6, 30), net_position=60000, open_interest=200000)
    assert cot_pair_score("USD", "JPY", snap) is None


def test_regime_pair_score_risk_off_favors_safe_haven() -> None:
    result = regime_pair_score("USD", "JPY", "risk_off")
    assert result is not None
    score, _ = result
    # リスクオフではJPY(感応度-1.0)がUSD(-0.5)より買われる → USD/JPYは下向き
    assert score < 0


def test_regime_pair_score_none_in_neutral() -> None:
    assert regime_pair_score("USD", "JPY", "neutral") is None


def test_macro_pair_view_combines_cot_and_regime() -> None:
    snap = MacroSnapshot(fetched_at=NOW)
    snap.cot["USD"] = CotReport("USD", date(2026, 6, 30), net_position=60000, open_interest=200000)
    snap.cot["JPY"] = CotReport("JPY", date(2026, 6, 30), net_position=-40000, open_interest=200000)
    score, confidence, notes = macro_pair_view("USD", "JPY", snap)
    assert confidence > 0
    assert notes


def test_macro_pair_view_empty_without_data() -> None:
    snap = MacroSnapshot(fetched_at=NOW)
    score, confidence, notes = macro_pair_view("USD", "JPY", snap)
    assert (score, confidence, notes) == (0.0, 0.0, [])


def test_fetch_uses_fresh_cache_without_network(tmp_path) -> None:
    """新鮮なキャッシュがあればネットワークに触れずに読める。"""
    cache_path = tmp_path / "macro_cache.json"
    fred_csv = "DATE,VIXCLS\n2026-07-01,18.0\n2026-07-02,19.0\n"
    cache = {
        f"fred_{sid}": {"fetched_at": NOW.isoformat(), "body": fred_csv}
        for sid in ("VIXCLS", "DGS10", "DGS2", "DTWEXBGS")
    }
    cache["cftc_cot"] = {"fetched_at": NOW.isoformat(), "body": json.dumps([])}
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    snap = fetch_macro_snapshot(cache_path, now=NOW, include_cot=True)
    assert "vix" in snap.series
    vix_last = snap.series["vix"].last()
    assert vix_last is not None
    assert vix_last.value == 19.0
    upgraded = json.loads(cache_path.read_text(encoding="utf-8"))
    assert upgraded["fred_VIXCLS"]["first_seen_time"] == NOW.isoformat()
    assert upgraded["fred_VIXCLS"]["content_hash"]
    assert snap.provenance["vix"]["first_seen_time"] == NOW.isoformat()


def test_coverage_reflects_fresh_data() -> None:
    empty = MacroSnapshot(fetched_at=NOW)
    assert empty.coverage() == 0.0
    snap = MacroSnapshot(fetched_at=NOW)
    for key in ("vix", "us10y", "us2y", "usd_index"):
        snap.series[key] = MacroSeries(
            key=key, label_ja=key, points=[SeriesPoint(date(2026, 7, 1), 1.0)]
        )
    assert snap.coverage() > 0.6  # 系列4本フレッシュで0.7分
