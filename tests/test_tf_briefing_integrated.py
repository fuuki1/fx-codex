"""結論先行のFX統合ブリーフィング表示に対する回帰テスト。"""

from __future__ import annotations

from datetime import datetime, UTC

from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.tf_briefing import build_timeframe_discord_payload
from fx_intel.timeframe import PRIMARY_HORIZON_HOURS, TimeframePlan

NOW = datetime(2026, 7, 15, 7, 10, tzinfo=UTC)


def _plan(symbol: str, timeframe: str, score: float, direction: str, conviction: int) -> TimeframePlan:
    close = 162.367 if symbol.endswith("JPY") else 1.13860
    distance = 0.130 if symbol.endswith("JPY") else 0.00130
    side = 1.0 if score >= 0 else -1.0
    return TimeframePlan(
        symbol=symbol,
        timeframe=timeframe,
        horizon_hours=PRIMARY_HORIZON_HOURS[timeframe],
        direction=direction,
        conviction=conviction,
        tf_score=score,
        news_score=0.1,
        composite=score * 0.55,
        close=close,
        atr=distance * 0.4,
        rsi=55.0 if score >= 0 else 44.0,
        adx=26.0,
        stop=close - side * distance if direction in ("long", "short") else None,
        target1=close + side * distance if direction in ("long", "short") else None,
        target2=close + side * distance * 2 if direction in ("long", "short") else None,
        data_quality=0.72,
        reason=f"{timeframe}レーティング {_direction_label(score)}",
    )


def _direction_label(score: float) -> str:
    return "買い" if score >= 0 else "売り"


def _plans(symbol: str, score: float, directions: tuple[str, ...], convictions: tuple[int, ...]):
    return [
        _plan(symbol, timeframe, score, direction, conviction)
        for timeframe, direction, conviction in zip(
            ("15m", "1h", "4h", "1d"), directions, convictions, strict=True
        )
    ]


def _analysis() -> MarketAnalysis:
    return MarketAnalysis(
        engine="analyst",
        regime="neutral",
        currencies={
            "EUR": CurrencySentiment(
                currency="EUR", score=0.40, headline_count=22, confidence=0.62
            ),
            "USD": CurrencySentiment(
                currency="USD", score=0.12, headline_count=49, confidence=0.21
            ),
            "JPY": CurrencySentiment(
                currency="JPY", score=-0.11, headline_count=18, confidence=0.34
            ),
        },
        summary="直近ニュースではEURが最も買われやすく、地合いは中立です。",
    )


def _total_embed_chars(payload: dict) -> int:
    total = 0
    for embed in payload["embeds"]:
        total += len(str(embed.get("title", "")))
        total += len(str(embed.get("description", "")))
        total += len(str(embed.get("footer", {}).get("text", "")))
        for field in embed.get("fields", []):
            total += len(str(field.get("name", "")))
            total += len(str(field.get("value", "")))
    return total


def test_integrated_briefing_matches_conclusion_first_structure() -> None:
    payload = build_timeframe_discord_payload(
        {
            "USDJPY": _plans(
                "USDJPY", 0.55, ("long", "neutral", "neutral", "long"), (38, 0, 0, 28)
            ),
            "EURUSD": _plans(
                "EURUSD", -0.60, ("neutral", "neutral", "neutral", "neutral"), (0, 0, 0, 10)
            ),
        },
        _analysis(),
        [],
        ["EUR", "USD", "JPY"],
        learning_note="15分足の方向的中率51%。Brierスコア0.319、単純基準0.250。",
        now=NOW,
    )

    assert "新規エントリーは見送り優先" in payload["content"]
    assert "USDJPY：買い目線維持" in payload["content"]
    assert "EURUSD：売り目線／マクロと矛盾" in payload["content"]

    macro = " ".join(
        f"{field['name']} {field['value']}" for field in payload["embeds"][0]["fields"]
    )
    assert "総合判断" in macro
    assert "相対的な通貨強弱" in macro
    assert "EUR ＞ USD ＞ JPY" in macro
    assert "自動学習の信頼性" in macro
    assert "確信度は実確率ではなく" in macro
    assert "データ品質・運用制限" in macro
    assert "実売買シグナルではなく" in macro
    assert "最終アクション" in macro


def test_discord_payload_stays_within_platform_limits() -> None:
    plans = _plans("USDJPY", 0.5, ("long", "long", "long", "long"), (55, 55, 55, 55))
    payload = build_timeframe_discord_payload(
        {f"PAIR{i}": plans for i in range(12)},
        _analysis(),
        [],
        ["EUR", "USD", "JPY"],
        learning_note="学習メモ" * 500,
        fetch_warnings=["データ取得警告" * 100],
        now=NOW,
    )

    assert len(payload["content"]) <= 2000
    assert len(payload["embeds"]) <= 10
    assert _total_embed_chars(payload) <= 6000
    for embed in payload["embeds"]:
        assert len(embed.get("fields", [])) <= 25
        for field in embed.get("fields", []):
            assert len(field["value"]) <= 1024
