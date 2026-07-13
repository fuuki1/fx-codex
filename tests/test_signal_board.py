"""5分ごとのFXシグナルボード生成をネットワークなしで検証する。"""

from __future__ import annotations

from datetime import date, datetime, UTC

from fx_intel.macro import CotReport, MacroSeries, MacroSnapshot, SeriesPoint
from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.signal_board import (
    DataQuality,
    assess_data_quality,
    build_signal_board_payload,
    rank_candidates,
)
from fx_intel.technicals import IntervalView, PairTechnicals
from fx_intel.timeframe import TimeframePlan

NOW = datetime(2026, 7, 10, 3, 10, tzinfo=UTC)  # 12:10 JST


def _plan(
    symbol: str,
    timeframe: str,
    direction: str,
    conviction: int,
    *,
    close: float,
    rsi: float,
    adx: float,
) -> TimeframePlan:
    sign = 1 if direction == "long" else -1
    risk = 0.0064 if not symbol.endswith("JPY") else 0.64
    return TimeframePlan(
        symbol=symbol,
        timeframe=timeframe,
        horizon_hours={"15m": 0.25, "4h": 4.0}[timeframe],
        direction=direction,
        conviction=conviction,
        tf_score=0.7 * sign,
        news_score=0.3 * sign,
        composite=conviction / 100 * sign,
        close=close,
        atr=risk,
        rsi=rsi,
        adx=adx,
        stop=close - sign * risk,
        target1=close + sign * risk,
        target2=close + sign * risk * 2,
        reason=f"{timeframe}レーティング",
    )


def _view(interval: str, direction: str, close: float) -> IntervalView:
    recommendation = "BUY" if direction == "long" else "SELL"
    return IntervalView(interval, recommendation, 10, 3, 5, close=close)


def _fixtures():
    plans = {
        "GBPUSD": [_plan("GBPUSD", "4h", "long", 78, close=1.345, rsi=71, adx=32)],
        "EURUSD": [_plan("EURUSD", "15m", "long", 54, close=1.172, rsi=80, adx=21)],
        "USDJPY": [_plan("USDJPY", "15m", "short", 33, close=146.2, rsi=20, adx=31)],
    }
    tech_map = {
        "GBPUSD": PairTechnicals(
            "GBPUSD",
            views={
                "4h": _view("4h", "long", 1.345),
                "1d": _view("1d", "long", 1.345),
            },
        ),
        "EURUSD": PairTechnicals(
            "EURUSD",
            views={
                "15m": _view("15m", "long", 1.172),
                "1h": _view("1h", "long", 1.172),
                "4h": _view("4h", "long", 1.172),
            },
        ),
        "USDJPY": PairTechnicals(
            "USDJPY",
            views={
                "15m": _view("15m", "short", 146.2),
                "1h": _view("1h", "short", 146.2),
                "4h": _view("4h", "short", 146.2),
            },
        ),
    }
    analysis = MarketAnalysis(
        engine="analyst",
        regime="neutral",
        currencies={
            "GBP": CurrencySentiment("GBP", 0.55),
            "EUR": CurrencySentiment("EUR", 0.20),
            "USD": CurrencySentiment("USD", -0.05),
            "JPY": CurrencySentiment("JPY", 0.10),
        },
    )
    return plans, tech_map, analysis


def test_board_matches_requested_single_message_shape() -> None:
    plans, tech_map, analysis = _fixtures()
    payload = build_signal_board_payload(
        plans,
        analysis,
        tech_map,
        DataQuality("正常", "正常", "正常", "注意｜広義ドル指数の最終観測 07/02"),
        now=NOW,
    )

    assert set(payload) == {"username", "content", "allowed_mentions"}
    assert "embeds" not in payload
    content = payload["content"]
    assert "📊 FXシグナルボード｜07/10 12:10 JST" in content
    # 自動売買は行わないため発注経路の死活監視は表示しない
    assert "システム状態" not in content
    assert "自動執行" not in content
    assert "新規エントリー候補：なし" in content
    assert content.index("1位 GBPUSD 4h") < content.index("2位 EURUSD 15m")
    assert content.index("2位 EURUSD 15m") < content.index("3位 USDJPY 15m")
    assert "⚪ 最終判断：見送り(取引しない)" in content
    assert "分析方向：🟢 買い｜見送り" in content
    assert "分析方向：🔴 売り｜見送り" in content
    assert "較正・コスト・サイズ・risk veto" in content
    assert "次回通知：5分後" in content
    assert "※シグナル強度は的中確率ではありません。" in content
    assert len(content) <= 2000


def test_ranking_uses_only_one_timeframe_per_symbol() -> None:
    plans, _tech_map, _analysis = _fixtures()
    plans["GBPUSD"].append(_plan("GBPUSD", "15m", "long", 60, close=1.345, rsi=55, adx=25))
    ranked = rank_candidates(plans)
    assert [item.plan.symbol for item in ranked] == ["GBPUSD", "EURUSD", "USDJPY"]
    assert ranked[0].plan.timeframe == "4h"
    assert all(not item.assessment.is_candidate for item in ranked)


def test_macro_staleness_is_shown_with_last_observation() -> None:
    plans, _tech_map, _analysis = _fixtures()
    series = {
        key: MacroSeries(
            key,
            label,
            [SeriesPoint(date(2026, 7, 2) if key == "usd_index" else date(2026, 7, 9), 1.0)],
        )
        for _series_id, (key, label) in {
            "VIXCLS": ("vix", "VIX(恐怖指数)"),
            "DGS10": ("us10y", "米10年金利"),
            "DGS2": ("us2y", "米2年金利"),
            "DTWEXBGS": ("usd_index", "広義ドル指数"),
        }.items()
    }
    snapshot = MacroSnapshot(
        fetched_at=NOW,
        series=series,
        cot={"USD": CotReport("USD", date(2026, 7, 7), 1, 10)},
    )
    quality = assess_data_quality(
        plans,
        calendar_ok=True,
        macro_snapshot=snapshot,
        now=NOW,
    )
    assert quality.macro == "注意｜広義ドル指数の最終観測 07/02"
    assert quality.has_warning
