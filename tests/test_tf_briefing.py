"""時間足別Discordペイロード(fx_intel.tf_briefing)のテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import datetime, UTC

from fx_intel.sentiment import CurrencySentiment, MarketAnalysis
from fx_intel.tf_briefing import (
    MAX_EMBED_CHARS,
    MAX_EMBEDS,
    _embed_chars,
    build_timeframe_discord_payload,
    split_timeframe_payloads,
)
from fx_intel.timeframe import TimeframePlan

NOW = datetime(2026, 6, 29, 9, 0, tzinfo=UTC)


def _embed(name: str, chars: int) -> dict:
    return {"title": name, "fields": [{"name": "f", "value": "x" * max(0, chars - 1)}]}


def test_split_keeps_single_message_when_within_limit() -> None:
    payload = {
        "username": "u",
        "content": "c",
        "embeds": [_embed("a", 500), _embed("b", 500)],
    }
    messages = split_timeframe_payloads(payload)
    assert len(messages) == 1  # 上限内は1通のまま
    assert messages[0]["content"] == "c"
    assert len(messages[0]["embeds"]) == 2


def test_split_divides_when_char_budget_exceeded() -> None:
    # 各3000文字のembed3個 = 9000文字 > MAX_EMBED_CHARS(5800) → 複数通に分割
    payload = {
        "username": "fx-codex 統合デスク",
        "content": "見出し",
        "embeds": [_embed("a", 3000), _embed("b", 3000), _embed("c", 3000)],
    }
    messages = split_timeframe_payloads(payload)

    assert len(messages) >= 2  # 分割された
    # 各メッセージはDiscordの合計文字数上限内
    for message in messages:
        total = sum(_embed_chars(e) for e in message["embeds"])
        assert total <= MAX_EMBED_CHARS
    # 内容(embed総数)は保持される
    assert sum(len(m["embeds"]) for m in messages) == 3
    # contentは先頭メッセージにだけ、usernameは全メッセージに付く
    assert messages[0]["content"] == "見出し"
    assert all("content" not in m for m in messages[1:])
    assert all(m["username"] == "fx-codex 統合デスク" for m in messages)


def test_split_caps_embed_count_per_message() -> None:
    payload = {"embeds": [_embed(str(i), 50) for i in range(MAX_EMBEDS + 3)]}
    messages = split_timeframe_payloads(payload)
    assert len(messages) >= 2
    for message in messages:
        assert len(message["embeds"]) <= MAX_EMBEDS


def test_split_shrinks_single_oversized_embed() -> None:
    payload = {"embeds": [_embed("huge", MAX_EMBED_CHARS + 4000)]}
    messages = split_timeframe_payloads(payload)
    assert len(messages) == 1
    # 単体で上限超過のembedはshrinkで上限内へ抑えられる
    assert _embed_chars(messages[0]["embeds"][0]) <= MAX_EMBED_CHARS


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
