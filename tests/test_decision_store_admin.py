"""完全判断ログのnews_items正規化backfillのテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from tools import decision_store_admin as admin


def _news(title: str) -> dict[str, object]:
    return {
        "title": title,
        "source": "Reuters",
        "link": f"https://example.test/{title}",
        "published": "2026-07-24T00:00:00+00:00",
        "summary": "x" * 200,
        "currencies": ["USD", "JPY"],
    }


def _event(decision_id: str, news: list[dict[str, object]]) -> dict[str, object]:
    return {
        "ts": "2026-07-24T00:00:00+00:00",
        "symbol": "USDJPY",
        "timeframe": "fusion",
        "decision_id": decision_id,
        "label_version": "net-r-v2",
        "market_context": {
            "regime": "risk_off",
            "news_count": len(news),
            "news_items": news,
        },
    }


def _write(path: Path, events: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(f"{json.dumps(event, ensure_ascii=False)}\n" for event in events),
        encoding="utf-8",
    )


def test_backfill_keeps_sidecar_rows_written_by_the_live_writer(tmp_path: Path) -> None:
    """writer投入後のログは新旧混在になる。既存サイドカーを消してはいけない。

    writerが先に畳んだニュースは、判断行に参照しか残らずログ本体には全文で
    現れない。backfillが既存サイドカーを読まずに書き直すと、その行が消えて
    参照だけが残る(dangling ref)。
    """

    from fx_intel import decision_log

    path = tmp_path / "briefing_decisions.jsonl"
    sidecar = decision_log.news_sidecar_path(path)

    # writer が正規化済み行 + サイドカーを既に作っている状態。
    decision_log.append_decision_events(path, [_event("written-by-writer", [_news("live")])])
    assert len(sidecar.read_text(encoding="utf-8").splitlines()) == 1

    # そこへ旧形式(全文)の行が残っている。
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_event("legacy", [_news("old")]), ensure_ascii=False) + "\n")

    report = admin.backfill_store(path, apply=True)

    known = {
        json.loads(line)["news_item_hash"]
        for line in sidecar.read_text(encoding="utf-8").splitlines()
        if line
    }
    # writer 由来の行が残り、legacy 分が加わって2件になる。
    assert len(known) == 2
    # 新たに畳んだのは legacy の1件だけで、既存分は二重計上しない。
    assert report.news_items_deduplicated == 1

    # 判断ログは 1行 = 1バッチなので、行をそのままイベントとして読まない。
    for line in path.read_text(encoding="utf-8").splitlines():
        for event in admin._events_of_line(json.loads(line)) or []:  # noqa: SLF001
            context = event["market_context"]
            assert "news_items" not in context, f"{event['decision_id']} が正規化されていない"
            for ref in context["news_item_refs"]:
                assert ref in known, "参照がサイドカーで解決できない"


def test_reconcile_fails_closed_on_unavailable_log(tmp_path: Path) -> None:
    """検証対象が1件も無い状態を「無損失」と認証してはいけない。"""

    missing = admin.reconcile_store(tmp_path / "does_not_exist.jsonl")
    assert missing.drift > 0

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert admin.reconcile_store(empty).drift > 0

    assert admin.main(["--path", str(tmp_path / "does_not_exist.jsonl"), "reconcile"]) == 1


def test_reconcile_counts_corrupt_lines_as_drift(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    path.write_text("{ not json\n", encoding="utf-8")

    assert admin.reconcile_store(path).drift > 0


def test_backfill_backs_up_the_sidecar_before_replacing_it(tmp_path: Path) -> None:
    """サイドカーも置換対象。退避しないと読めない行が復元不能になる。"""

    from fx_intel import decision_log

    path = tmp_path / "briefing_decisions.jsonl"
    decision_log.append_decision_events(path, [_event("d-1", [_news("cpi")])])
    sidecar = decision_log.news_sidecar_path(path)
    # writerが書いた正常な行に続けて、壊れた行が紛れている状態。
    with sidecar.open("a", encoding="utf-8") as handle:
        handle.write("{ corrupt sidecar row\n")

    report = admin.backfill_store(path, apply=True).to_dict()
    backup_dir = Path(report["backup_path"]).parent
    sidecar_backup = backup_dir / f"{path.stem}{admin.NEWS_SIDECAR_SUFFIX}.original"

    assert sidecar_backup.is_file(), "サイドカーが退避されていない"
    assert "corrupt sidecar row" in sidecar_backup.read_text(encoding="utf-8")


def test_tool_shares_the_writer_reference_contract() -> None:
    """契約はwriterが唯一の定義元。再定義するとdangling refを静かに生む。"""

    from fx_intel import decision_log

    assert admin.NEWS_SIDECAR_SUFFIX is decision_log.NEWS_SIDECAR_SUFFIX
    assert admin.NEWS_NORMALIZED_KEY is decision_log.NEWS_NORMALIZED_KEY
    assert admin.news_content_hash is decision_log.news_content_hash


def test_audit_reports_duplication(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    shared = _news("cpi")
    _write(path, [_event(f"d-{i}", [shared, _news("unrate")]) for i in range(5)])

    report = admin.audit_store(path).to_dict()

    assert report["valid_events"] == 5
    assert report["news_item_rows"] == 10  # 2件 × 5判断
    assert report["unique_news_items"] == 2  # cpi と unrate だけ
    assert report["news_duplication_factor"] == 5.0
    assert report["news_items_byte_ratio"] > 0.0


def test_backfill_dry_run_does_not_touch_files(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    original = [_event(f"d-{i}", [_news("cpi")]) for i in range(3)]
    _write(path, original)
    before = path.read_bytes()

    report = admin.backfill_store(path, apply=False).to_dict()

    assert report["applied"] is False
    assert path.read_bytes() == before  # 元ファイル不変
    assert not (path.with_name("briefing_decisions_news.jsonl")).exists()
    assert report["news_items_deduplicated"] == 1
    assert report["estimated_byte_reduction"] > 0.0


def test_backfill_apply_normalizes_and_preserves_pit(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    shared = _news("cpi")
    _write(path, [_event(f"d-{i}", [shared, _news("unrate")]) for i in range(4)])

    report = admin.backfill_store(path, apply=True).to_dict()

    assert report["applied"] is True
    assert report["events_rewritten"] == 4
    assert report["news_items_deduplicated"] == 2  # cpi + unrate を1回ずつ
    assert report["rewritten_bytes"] < report["original_bytes"]

    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(events) == 4
    for event in events:
        # 会計/PIT情報は不変
        assert event["decision_id"].startswith("d-")
        assert event["label_version"] == "net-r-v2"
        context = event["market_context"]
        assert "news_items" not in context  # 全文は消える
        assert len(context["news_item_refs"]) == 2  # ハッシュ参照に置換
        assert context["news_count"] == 2
        assert event["news_items_normalized"] is True

    sidecar = path.with_name("briefing_decisions_news.jsonl")
    news_rows = [json.loads(line) for line in sidecar.read_text().splitlines() if line.strip()]
    assert len(news_rows) == 2  # 各ニュース1回だけ
    hashes = {row["news_item_hash"] for row in news_rows}
    assert set(events[0]["market_context"]["news_item_refs"]) <= hashes


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    _write(path, [_event(f"d-{i}", [_news("cpi")]) for i in range(3)])

    admin.backfill_store(path, apply=True)
    first = path.read_bytes()
    admin.backfill_store(path, apply=True)
    second = path.read_bytes()

    assert first == second  # 二度目のbackfillは何も変えない


def test_backfill_quarantines_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    good = json.dumps(_event("d-1", [_news("cpi")]), ensure_ascii=False)
    path.write_text(f"{good}\n{{not valid json\n", encoding="utf-8")

    report = admin.backfill_store(path, apply=True).to_dict()

    assert report["corrupt_lines_quarantined"] == 1
    quarantine = Path(report["quarantine_path"])
    assert quarantine.is_file()
    assert "not valid json" in quarantine.read_text()
    # 有効行は残る
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(events) == 1


def test_audit_missing_file_is_empty(tmp_path: Path) -> None:
    report = admin.audit_store(tmp_path / "missing.jsonl").to_dict()
    assert report["total_lines"] == 0
    assert report["valid_events"] == 0


def test_reconcile_zero_drift_after_backfill(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    shared = _news("cpi")
    _write(path, [_event(f"d-{i}", [shared, _news("unrate")]) for i in range(4)])
    admin.backfill_store(path, apply=True)

    report = admin.reconcile_store(path).to_dict()

    assert report["drift"] == 0
    assert report["normalized_events"] == 4
    assert report["missing_news_refs"] == 0
    assert report["tampered_news_rows"] == 0


def test_reconcile_detects_unnormalized_events(tmp_path: Path) -> None:
    # backfillしていない生ログ(news_items全文が残る)はドリフト。
    path = tmp_path / "briefing_decisions.jsonl"
    _write(path, [_event(f"d-{i}", [_news("cpi")]) for i in range(3)])

    report = admin.reconcile_store(path).to_dict()

    assert report["unnormalized_events"] == 3
    assert report["drift"] == 3


def test_reconcile_detects_missing_sidecar_ref(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    _write(path, [_event("d-1", [_news("cpi")])])
    admin.backfill_store(path, apply=True)
    # サイドカーを空にする → 参照が引けなくなる
    path.with_name("briefing_decisions_news.jsonl").write_text("", encoding="utf-8")

    report = admin.reconcile_store(path).to_dict()

    assert report["missing_news_refs"] == 1
    assert report["drift"] >= 1


def test_reconcile_detects_tampered_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    _write(path, [_event("d-1", [_news("cpi")])])
    admin.backfill_store(path, apply=True)
    sidecar = path.with_name("briefing_decisions_news.jsonl")
    rows = [json.loads(line) for line in sidecar.read_text().splitlines() if line.strip()]
    rows[0]["title"] = "改ざんされた見出し"  # hashは据え置きで本文だけ書き換える
    sidecar.write_text(
        "".join(f"{json.dumps(r, ensure_ascii=False)}\n" for r in rows), encoding="utf-8"
    )

    report = admin.reconcile_store(path).to_dict()

    assert report["tampered_news_rows"] == 1
    assert report["drift"] >= 1


def test_reconcile_cli_exit_codes(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    _write(path, [_event("d-1", [_news("cpi")])])
    admin.backfill_store(path, apply=True)

    assert admin.main(["--path", str(path), "reconcile"]) == 0  # drift=0

    # 全文を残した未正規化ログはdrift>0でexit 1
    dirty = tmp_path / "dirty.jsonl"
    _write(dirty, [_event("d-2", [_news("cpi")])])
    assert admin.main(["--path", str(dirty), "reconcile"]) == 1


def test_audit_sees_events_inside_batch_lines(tmp_path: Path) -> None:
    """判断ログは 1行 = 1バッチ。行をイベントとして読むと検出0になる。"""

    from fx_intel import decision_log

    path = tmp_path / "briefing_decisions.jsonl"
    decision_log.append_decision_events(
        path,
        [_event(f"d-{index}", [_news("cpi")]) for index in range(3)],
        normalize_news=False,
    )

    report = admin.audit_store(path)

    assert report.total_lines == 1, "1バッチ=1行"
    assert report.valid_events == 3, "バッチ内の3イベントを数えること"
    assert report.news_item_rows == 3


def test_backfill_rewrites_batch_hash_so_readers_keep_seeing_events(tmp_path: Path) -> None:
    """バッチ行を畳んだら decision_batch_sha256 を再計算しなければならない。

    再計算しないと reader の再導出と食い違い、バッチ全体が恒久的に不可視になる。
    """

    from fx_intel import decision_commit, decision_log

    path = tmp_path / "briefing_decisions.jsonl"
    decision_log.append_decision_events(
        path,
        [_event(f"d-{index}", [_news("cpi"), _news("nfp")]) for index in range(4)],
        normalize_news=False,
    )

    admin.backfill_store(path, apply=True)

    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert line["decision_batch_sha256"] == decision_commit.canonical_sha256(line["events"])
    assert len(list(decision_log.read_decision_events(path))) == 4
