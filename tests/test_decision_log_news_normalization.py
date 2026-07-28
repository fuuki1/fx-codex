"""判断ログ書込時のnews_items正規化(参照化+サイドカー)テスト。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
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


def _append_shared_batch(log_path: str) -> None:
    """別プロセスから同一ニュースを書く(サイドカー排他の反証用)。"""

    shared = [_news(0), _news(1)]
    decision_log.append_decision_events(Path(log_path), [_event("d", shared)], normalize_news=True)


def test_concurrent_processes_do_not_duplicate_sidecar_rows(tmp_path: Path) -> None:
    """読取り→追記の間にロックが途切れると、同じニュースが二重追記される。"""

    log = tmp_path / "decisions.jsonl"
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_append_shared_batch, args=(str(log),)) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    assert [worker.exitcode for worker in workers] == [0, 0, 0, 0]
    sidecar = _lines(decision_log.news_sidecar_path(log))
    hashes = [row["news_item_hash"] for row in sidecar]
    # 4プロセスが同じ2件を書いても、サイドカーは各1回だけ持つ。
    assert sorted(hashes) == sorted(set(hashes))
    assert len(hashes) == 2


def test_concurrent_threads_do_not_duplicate_sidecar_rows(tmp_path: Path) -> None:
    log = tmp_path / "decisions.jsonl"
    shared = [_news(0), _news(1)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda index: decision_log.append_decision_events(
                    log, [_event(f"d-{index}", shared)]
                ),
                range(4),
            )
        )

    hashes = [row["news_item_hash"] for row in _lines(decision_log.news_sidecar_path(log))]
    assert sorted(hashes) == sorted(set(hashes))
    assert len(hashes) == 2
    # 判断行は4件すべて残り、参照はサイドカーで解決できる。
    known = set(hashes)
    written = _lines(log)
    assert len(written) == 4
    for row in written:
        for ref in row["market_context"]["news_item_refs"]:
            assert ref in known


def test_sidecar_scan_happens_under_the_lock(tmp_path: Path) -> None:
    """既存ハッシュの走査はロック取得後でなければならない。

    読取りを先に済ませてから施錠する実装では、2つのwriterが同じニュースを
    「未登録」と観測してから順に追記でき、exactly-once契約が壊れる。ここでは
    施錠時点を記録して、走査がその後に起きることを直接検証する。
    """

    log = tmp_path / "decisions.jsonl"
    sidecar = decision_log.news_sidecar_path(log)
    sidecar.write_text(
        json.dumps({"news_item_hash": "seed", "title": "seed"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    events: list[str] = []
    real_flock = decision_log.fcntl.flock

    def tracking_flock(fileno: int, operation: int) -> None:
        if operation == decision_log.fcntl.LOCK_EX:
            events.append("lock")
        real_flock(fileno, operation)

    original_loads = decision_log.json.loads

    def tracking_loads(text: str, *args: object, **kwargs: object) -> object:
        # サイドカー既存行の解釈は走査の一部。
        if '"news_item_hash"' in text:
            events.append("scan")
        return original_loads(text, *args, **kwargs)

    decision_log.fcntl.flock = tracking_flock  # type: ignore[assignment]
    decision_log.json.loads = tracking_loads  # type: ignore[assignment]
    try:
        decision_log.append_decision_events(log, [_event("d", [_news(0)])])
    finally:
        decision_log.fcntl.flock = real_flock  # type: ignore[assignment]
        decision_log.json.loads = original_loads  # type: ignore[assignment]

    assert "scan" in events, "既存サイドカー行が走査されていない"
    assert events.index("lock") < events.index("scan")


def test_hash_is_stable_regardless_of_key_order(tmp_path: Path) -> None:
    first = {"title": "t", "source": "s"}
    second = {"source": "s", "title": "t"}

    assert decision_log.news_content_hash(first) == decision_log.news_content_hash(second)
