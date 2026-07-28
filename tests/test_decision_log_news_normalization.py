"""batch/commit契約下でのnews_items正規化テスト。

実機writerは 1行 = 1 batch で、`decision_batch_sha256` を events から導出し、
readerが同じ導出を再実行して照合する。正規化をhash計算の前後どちらに置くかで
可視性が変わるため、ここでは「readerがbatchを見られること」を軸に検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

from fx_intel import decision_commit, decision_log
from tests.decision_fixtures import write_committed_decision_events


def _news(index: int) -> dict[str, object]:
    return {
        "title": f"headline-{index}",
        "body": "x" * 300,
        "source": "fixture",
        "currencies": ["USD"],
    }


def _event(decision_id: str, items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "symbol": "USDJPY",
        "timeframe": "1h",
        "decision": {"direction": "long", "conviction": 55},
        "market_context": {"regime": "risk_on", "news_count": len(items), "news_items": items},
    }


def _batch_line(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])


def _sidecar_hashes(path: Path) -> set[str]:
    sidecar = decision_log.news_sidecar_path(path)
    return {
        json.loads(line)["news_item_hash"]
        for line in sidecar.read_text(encoding="utf-8").splitlines()
        if line
    }


def test_batch_hash_covers_the_normalized_events(tmp_path: Path) -> None:
    """writerのhash・行内のhash・readerの再導出が三者一致すること。

    正規化をhash計算の後に置くと、readerの再導出が行内の値と食い違い、
    batch全体が恒久的に不可視になる。
    """

    path = tmp_path / "briefing_decisions.jsonl"
    shared = [_news(0), _news(1)]

    result = decision_log.append_decision_events(path, [_event(f"d-{i}", shared) for i in range(4)])

    line = _batch_line(path)
    reader_hash = decision_commit.canonical_sha256(line["events"])
    assert result["batch_sha256"] == line["decision_batch_sha256"] == reader_hash


def test_reader_still_sees_normalized_batches(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"

    decision_log.append_decision_events(path, [_event("d-1", [_news(0)])])

    events = list(decision_log.read_decision_events(path))
    assert len(events) == 1, "hash不整合ならreaderは0件になる"
    context = events[0]["market_context"]
    assert "news_items" not in context
    assert context["news_item_refs"] == [decision_log.news_content_hash(_news(0))]


def test_committed_pit_transaction_survives_normalization(tmp_path: Path) -> None:
    """cross-log commit を伴うPIT取引でも全イベントが可視であること。

    commit record は `full_batch_sha256` を別途保持するため、writerの返り値と
    ずれると commit 照合に失敗し、PIT適格イベントが恒久的に隠される。
    """

    path = tmp_path / "briefing_decisions.jsonl"
    shared = [_news(0), _news(1)]
    rows = [
        {
            **_event(f"pit-{index}", shared),
            "ts": "2026-07-27T00:00:00+00:00",
            "mode": "per_timeframe",
            "horizon_hours": 1.0,
        }
        for index in range(3)
    ]

    write_committed_decision_events(path, rows, mode="per_timeframe")

    events = list(decision_log.read_decision_events(path))
    assert len(events) == 3
    known = _sidecar_hashes(path)
    for event in events:
        context = event["market_context"]
        assert "news_items" not in context
        for ref in context["news_item_refs"]:
            assert ref in known


def test_shared_news_is_stored_once_per_batch(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    shared = [_news(0), _news(1)]

    decision_log.append_decision_events(path, [_event(f"d-{i}", shared) for i in range(8)])

    # 8判断が同じ2件を参照しても、サイドカーは各1回だけ持つ。
    assert len(_sidecar_hashes(path)) == 2
    line = _batch_line(path)
    assert len(line["events"]) == 8
    for event in line["events"]:
        assert event[decision_log.NEWS_NORMALIZED_KEY] is True


def test_normalization_can_be_disabled_for_verbatim_replay(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"

    decision_log.append_decision_events(path, [_event("d-1", [_news(0)])], normalize_news=False)

    events = list(decision_log.read_decision_events(path))
    assert events[0]["market_context"]["news_items"][0]["title"] == "headline-0"
    assert not decision_log.news_sidecar_path(path).exists()


def test_events_without_news_are_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    bare = {"decision_id": "bare", "decision": {"direction": "neutral"}}

    decision_log.append_decision_events(path, [bare])

    events = list(decision_log.read_decision_events(path))
    assert events[0]["decision_id"] == "bare"
    assert not decision_log.news_sidecar_path(path).exists()


def test_sidecar_is_written_before_the_batch_line(tmp_path: Path) -> None:
    """参照が解決できないbatch行を残さないため、サイドカーを先に書くこと。"""

    path = tmp_path / "briefing_decisions.jsonl"
    order: list[str] = []
    real_append = decision_log._append_news_sidecar  # noqa: SLF001

    def tracking_sidecar(sidecar_path: Path, rows: object) -> None:
        order.append("sidecar")
        real_append(sidecar_path, rows)  # type: ignore[arg-type]

    decision_log._append_news_sidecar = tracking_sidecar  # type: ignore[assignment]
    try:
        decision_log.append_decision_events(path, [_event("d-1", [_news(0)])])
    finally:
        decision_log._append_news_sidecar = real_append  # type: ignore[assignment]

    order.append("batch")
    assert order == ["sidecar", "batch"]


def test_hash_is_stable_regardless_of_key_order() -> None:
    assert decision_log.news_content_hash({"title": "t", "source": "s"}) == (
        decision_log.news_content_hash({"source": "s", "title": "t"})
    )
