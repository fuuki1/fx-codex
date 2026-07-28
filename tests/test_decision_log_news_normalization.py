"""判断ログ書込時のnews_items正規化(参照化+サイドカー)テスト。"""

from __future__ import annotations

import json
from pathlib import Path

from fx_intel import decision_log


def _event(decision_id: str, items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": decision_log.SCHEMA_VERSION,
        "decision_id": decision_id,
        "symbol": "USDJPY",
        "timeframe": "1h",
        "decision": {"direction": "long", "conviction": 55},
        "market_context": {
            "regime": "risk_on",
            "news_count": len(items),
            "news_items": items,
        },
    }


def _news(index: int) -> dict[str, object]:
    return {
        "title": f"headline-{index}",
        "body": "x" * 500,
        "source": "fixture",
        "currencies": ["USD"],
    }


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_shared_news_batch_is_stored_once_across_events(tmp_path: Path) -> None:
    log = tmp_path / "decisions.jsonl"
    shared = [_news(0), _news(1)]
    # One run embeds the same market_context into every decision it produces.
    events = [_event(f"decision-{index}", shared) for index in range(8)]

    decision_log.append_decision_events(log, events)

    written = _lines(log)
    assert len(written) == 8
    for row in written:
        context = row["market_context"]
        assert "news_items" not in context
        assert context["news_item_refs"] == [
            decision_log.news_content_hash(item) for item in shared
        ]
        assert context["news_count"] == 2
        assert row[decision_log.NEWS_NORMALIZED_KEY] is True

    sidecar = _lines(decision_log.news_sidecar_path(log))
    # Two distinct items, stored once each despite eight referencing decisions.
    assert len(sidecar) == 2
    assert {row["news_item_hash"] for row in sidecar} == set(
        written[0]["market_context"]["news_item_refs"]
    )
    assert sidecar[0]["title"] == "headline-0"


def test_every_reference_resolves_to_a_sidecar_row(tmp_path: Path) -> None:
    log = tmp_path / "decisions.jsonl"
    decision_log.append_decision_events(log, [_event("a", [_news(0)])])
    decision_log.append_decision_events(log, [_event("b", [_news(0), _news(1)])])

    known = {row["news_item_hash"] for row in _lines(decision_log.news_sidecar_path(log))}
    for row in _lines(log):
        for ref in row["market_context"]["news_item_refs"]:
            assert ref in known
    # The item shared by both batches is not appended twice.
    assert len(known) == 2


def test_normalizing_an_already_normalized_event_is_a_no_op(tmp_path: Path) -> None:
    log = tmp_path / "decisions.jsonl"
    decision_log.append_decision_events(log, [_event("a", [_news(0)])])
    first = _lines(log)[0]

    again, rows = decision_log.normalize_news_items(first)

    assert again == first
    assert rows == []


def test_normalization_can_be_disabled_for_verbatim_replay(tmp_path: Path) -> None:
    log = tmp_path / "decisions.jsonl"
    decision_log.append_decision_events(log, [_event("a", [_news(0)])], normalize_news=False)

    context = _lines(log)[0]["market_context"]
    assert context["news_items"][0]["title"] == "headline-0"
    assert not decision_log.news_sidecar_path(log).exists()


def test_events_without_news_are_written_unchanged(tmp_path: Path) -> None:
    log = tmp_path / "decisions.jsonl"
    bare = {"decision_id": "bare", "decision": {"direction": "neutral"}}

    decision_log.append_decision_events(log, [bare])

    written = _lines(log)[0]
    assert written == bare
    assert not decision_log.news_sidecar_path(log).exists()


def test_hash_is_stable_regardless_of_key_order(tmp_path: Path) -> None:
    first = {"title": "t", "source": "s"}
    second = {"source": "s", "title": "t"}

    assert decision_log.news_content_hash(first) == decision_log.news_content_hash(second)
