"""自前分析エンジン(analyst.py)のテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

from fx_intel.analyst import (
    analyze_headlines,
    detect_regime_from_headlines,
    score_headlines,
)
from fx_intel.news import NewsItem, tag_currencies

NOW = datetime(2026, 7, 2, 8, 0, tzinfo=UTC)


def make_news(title: str, hours_ago: float = 1.0, source: str = "FXStreet") -> NewsItem:
    return NewsItem(
        title=title,
        source=source,
        link="https://example.com",
        published=NOW - timedelta(hours=hours_ago),
        summary="",
        currencies=tag_currencies(title),
    )


def test_hawkish_headline_lifts_currency() -> None:
    items = [make_news("Fed signals rate hike amid hot inflation")]
    scores = score_headlines(items, ["USD", "JPY"], now=NOW)
    assert scores["USD"].score > 0
    assert "金融政策" in scores["USD"].themes or "インフレ" in scores["USD"].themes


def test_dovish_headline_weighs_currency() -> None:
    items = [make_news("ECB dovish shift, rate cut expected")]
    scores = score_headlines(items, ["EUR", "USD"], now=NOW)
    assert scores["EUR"].score < 0


def test_negation_flips_polarity() -> None:
    """「rules out rate hike」はタカ派ではなくハト派方向に効く。"""
    hawkish = score_headlines([make_news("BOJ signals rate hike")], ["JPY"], now=NOW)
    negated = score_headlines([make_news("BOJ rules out rate hike")], ["JPY"], now=NOW)
    assert hawkish["JPY"].score > 0
    assert negated["JPY"].score < hawkish["JPY"].score


def test_hedge_dampens_confidence() -> None:
    """ヘッジ語(may/speculation)が付くと断定形より寄与が小さい。"""
    firm = score_headlines([make_news("Fed hawkish on rate hike")], ["USD"], now=NOW)
    hedged = score_headlines(
        [make_news("Speculation Fed may turn hawkish on rate hike")], ["USD"], now=NOW
    )
    assert abs(firm["USD"].score) > 0
    assert abs(hedged["USD"].score) < abs(firm["USD"].score)


def test_recency_decay_reduces_old_news() -> None:
    fresh = score_headlines([make_news("USD strong on rate hike", hours_ago=0.5)], ["USD"], now=NOW)
    old = score_headlines([make_news("USD strong on rate hike", hours_ago=48)], ["USD"], now=NOW)
    assert abs(old["USD"].score) < abs(fresh["USD"].score)


def test_future_news_has_zero_feature_weight() -> None:
    future = make_news("Fed signals rate hike", hours_ago=-1.0)
    scores = score_headlines([future], ["USD"], now=NOW)

    assert scores["USD"].score == 0.0


def test_effective_score_is_bias_times_confidence() -> None:
    """薄い材料(1件)は確信度が低く、実効スコアが抑えられる。"""
    scores = score_headlines([make_news("USD resilient")], ["USD"], now=NOW)
    sentiment = scores["USD"]
    assert sentiment.confidence is not None
    assert sentiment.confidence < 0.5  # 1件では確信度が低い
    assert abs(sentiment.score) < 0.5


def test_pair_move_syntax_moves_both_legs() -> None:
    items = [make_news("USD/JPY rises sharply to fresh highs")]
    scores = score_headlines(items, ["USD", "JPY"], now=NOW)
    assert scores["USD"].score > 0
    assert scores["JPY"].score < 0


def test_regime_from_headlines_detects_risk_off() -> None:
    items = [
        make_news("Safe haven demand surges amid war escalation"),
        make_news("Global sell-off as sanctions hit markets"),
    ]
    regime, _ = detect_regime_from_headlines(items)
    assert regime == "risk_off"


def test_analyze_headlines_is_deterministic() -> None:
    items = [make_news("Fed hawkish, USD strong"), make_news("BOJ dovish, yen weakens")]
    first = analyze_headlines(items, ["USD", "JPY"], now=NOW)
    second = analyze_headlines(items, ["USD", "JPY"], now=NOW)
    assert first.engine == "analyst"
    assert {c: s.score for c, s in first.currencies.items()} == {
        c: s.score for c, s in second.currencies.items()
    }


def test_analyze_headlines_uses_macro_regime_when_available() -> None:
    from fx_intel.macro import MacroSeries, MacroSnapshot, SeriesPoint
    from datetime import date

    snap = MacroSnapshot(fetched_at=NOW)
    # VIX急騰でrisk_off
    snap.series["vix"] = MacroSeries(
        key="vix",
        label_ja="VIX",
        points=[
            SeriesPoint(date(2026, 6, 25), 15.0),
            SeriesPoint(date(2026, 6, 26), 16.0),
            SeriesPoint(date(2026, 6, 27), 18.0),
            SeriesPoint(date(2026, 6, 28), 22.0),
            SeriesPoint(date(2026, 6, 29), 26.0),
            SeriesPoint(date(2026, 6, 30), 30.0),
        ],
    )
    analysis = analyze_headlines([make_news("markets quiet")], ["USD", "JPY"], now=NOW, macro=snap)
    assert analysis.regime == "risk_off"
    assert "実データ判定" in analysis.summary
