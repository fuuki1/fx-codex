"""時間足別Discordペイロード(fx_intel.tf_briefing)のテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import datetime, UTC

from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.tf_briefing import build_timeframe_discord_payload
from fx_intel.timeframe import TimeframePlan

NOW = datetime(2026, 6, 29, 9, 0, tzinfo=UTC)


def _plan(timeframe, horizon, direction, conviction, close=156.0):
    stop = close - 0.5 if direction in ("long", "short") else None
    return TimeframePlan(
        symbol="USDJPY",
        timeframe=timeframe,
        horizon_hours=horizon,
        direction=direction,
        conviction=conviction,
        tf_score=0.5,
        news_score=0.1,
        composite=0.3,
        close=close,
        atr=0.15,
        rsi=55.0,
        adx=28.0,
        stop=stop,
        target1=close + 0.5 if stop else None,
        target2=close + 1.0 if stop else None,
        reason=f"{timeframe}レーティング 買い",
    )


def _analysis():
    return MarketAnalysis(
        engine="lexicon",
        regime="neutral",
        currencies={
            "USD": CurrencySentiment("USD", 0.1, 1),
            "JPY": CurrencySentiment("JPY", -0.1, 1),
        },
        summary="",
    )


def _plans():
    return [
        _plan("15m", 0.25, "neutral", 5),
        _plan("1h", 1.0, "long", 40),
        _plan("4h", 4.0, "long", 55),
        _plan("1d", 24.0, "long", 30),
    ]


def test_payload_has_header_and_one_embed_per_symbol() -> None:
    payload = build_timeframe_discord_payload(
        {"USDJPY": _plans()}, _analysis(), [], ["JPY", "USD"], now=NOW
    )
    # ヘッダ概況 embed + USDJPY の1 embed
    assert len(payload["embeds"]) == 2
    assert payload["embeds"][0]["title"] == "マクロ・センチメント概況"
    assert payload["embeds"][1]["title"].startswith("USDJPY")


def test_symbol_embed_has_field_per_timeframe() -> None:
    payload = build_timeframe_discord_payload(
        {"USDJPY": _plans()}, _analysis(), [], ["USD"], now=NOW
    )
    fields = payload["embeds"][1]["fields"]
    names = " ".join(f["name"] for f in fields)
    assert "15分足" in names
    assert "1時間足" in names
    assert "4時間足" in names
    assert "日足" in names


def test_each_field_shows_its_own_horizon() -> None:
    payload = build_timeframe_discord_payload(
        {"USDJPY": _plans()}, _analysis(), [], ["USD"], now=NOW
    )
    fields = {f["name"]: f["value"] for f in payload["embeds"][1]["fields"]}
    # 15分足フィールドは「15分後」、日足フィールドは「24時間後」で採点と明記
    fifteen = next(v for k, v in fields.items() if k.startswith("15分足"))
    daily = next(v for k, v in fields.items() if k.startswith("日足"))
    assert "15分後" in fifteen
    assert "24時間後" in daily


def test_content_mentions_integrated_conclusion() -> None:
    payload = build_timeframe_discord_payload(
        {"USDJPY": _plans()}, _analysis(), [], ["USD"], now=NOW
    )
    assert "FX統合ブリーフィング" in payload["content"]
    assert "結論" in payload["content"]
    assert "即時エントリーは避ける" in payload["content"]


def test_learning_note_appears_in_header() -> None:
    payload = build_timeframe_discord_payload(
        {"USDJPY": _plans()},
        _analysis(),
        [],
        ["USD"],
        learning_note="【1時間足】的中率 60%",
        now=NOW,
    )
    header_fields = " ".join(str(f.get("value", "")) for f in payload["embeds"][0]["fields"])
    assert "60%" in header_fields


def test_embeds_capped_at_ten() -> None:
    # 12ペア分渡しても embed は10で頭打ち(Discord上限)
    many = {f"PAIR{i}": _plans() for i in range(12)}
    payload = build_timeframe_discord_payload(many, _analysis(), [], ["USD"], now=NOW)
    assert len(payload["embeds"]) == 10


def test_directional_field_shows_sl_tp() -> None:
    payload = build_timeframe_discord_payload(
        {"USDJPY": _plans()}, _analysis(), [], ["USD"], now=NOW
    )
    fields = {f["name"]: f["value"] for f in payload["embeds"][1]["fields"]}
    one_hour = next(v for k, v in fields.items() if k.startswith("1時間足"))
    assert "SL" in one_hour and "T1" in one_hour


def test_aux_report_rendered_when_provided() -> None:
    payload = build_timeframe_discord_payload(
        {"USDJPY": _plans()},
        _analysis(),
        [],
        ["USD"],
        aux_reports_by_symbol={"USDJPY": {"1h": "1時間足 補助ホライズン: 4時間後 55%"}},
        now=NOW,
    )
    fields = " ".join(str(f.get("value", "")) for f in payload["embeds"][1]["fields"])
    assert "補助ホライズン" in fields
