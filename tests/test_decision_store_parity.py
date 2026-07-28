"""backfill前後の可逆性(parity)検証のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from tools import decision_store_admin as admin
from tools import decision_store_parity as parity


def _news(title: str) -> dict[str, object]:
    return {
        "title": title,
        "source": "Reuters",
        "link": f"https://example.test/{title}",
        "published": "2026-07-24T00:00:00+00:00",
        "summary": "y" * 120,
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


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    """backfillを適用し、(正規化後path, backup path) を返す。"""
    path = tmp_path / "briefing_decisions.jsonl"
    shared = _news("cpi")
    _write(path, [_event(f"d-{i}", [shared, _news("unrate")]) for i in range(4)])
    report = admin.backfill_store(path, apply=True).to_dict()
    return path, Path(report["backup_path"])


def test_parity_verified_on_writer_first_mixed_store(tmp_path: Path) -> None:
    """writerが先に畳んだ行を含む混在ログでも、無損失なら verified になる。

    backup側にも正規化済み行が含まれるため、片側だけ復元して比較すると
    無損失なbackfillを parity_failed と誤判定する。
    """

    from fx_intel import decision_log

    path = tmp_path / "briefing_decisions.jsonl"
    decision_log.append_decision_events(path, [_event("by-writer", [_news("live")])])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_event("legacy", [_news("old")]), ensure_ascii=False) + "\n")

    report = admin.backfill_store(path, apply=True).to_dict()
    verdict = parity.verify_parity(path, Path(report["backup_path"]))

    assert verdict.verdict == parity.VERDICT_VERIFIED
    assert verdict.matched == 2
    assert verdict.mismatched == 0


def test_parity_detects_dropped_duplicate_events(tmp_path: Path) -> None:
    """同一decision_idの重複をdictで潰すと、欠落を見逃す。"""

    event = {
        "decision_id": "dup",
        "news_items_normalized": True,
        "market_context": {"news_count": 0, "news_item_refs": []},
    }
    backup = tmp_path / "backup.jsonl"
    normalized = tmp_path / "briefing_decisions.jsonl"
    _write(backup, [event, event])  # backupには2件
    _write(normalized, [event])  # 正規化ログには1件しか無い

    verdict = parity.verify_parity(normalized, backup)

    assert verdict.verdict == parity.VERDICT_FAILED
    assert verdict.backup_events == 2
    assert verdict.missing_in_normalized == 1


def test_restore_event_is_inverse_of_normalize(tmp_path: Path) -> None:
    path = tmp_path / "briefing_decisions.jsonl"
    original = _event("d-1", [_news("cpi"), _news("unrate")])
    _write(path, [original])
    admin.backfill_store(path, apply=True)

    normalized = [json.loads(line) for line in path.read_text().splitlines() if line.strip()][0]
    sidecar = admin._load_news_sidecar(path.with_name("briefing_decisions_news.jsonl"))
    restored = admin.restore_event(normalized, sidecar)

    assert restored == original  # 完全復元


def test_parity_verified_after_clean_backfill(tmp_path: Path) -> None:
    path, backup = _prepare(tmp_path)

    report = parity.verify_parity(path, backup).to_dict()

    assert report["verdict"] == "parity_verified"
    assert report["backup_events"] == 4
    assert report["matched"] == 4
    assert report["mismatched"] == 0
    assert report["missing_in_normalized"] == 0
    assert report["restore_failures"] == 0


def test_parity_fails_when_sidecar_row_missing(tmp_path: Path) -> None:
    path, backup = _prepare(tmp_path)
    # サイドカーから1行落とす → 復元が引けなくなる
    sidecar = path.with_name("briefing_decisions_news.jsonl")
    rows = sidecar.read_text().splitlines()
    sidecar.write_text("\n".join(rows[1:]) + "\n", encoding="utf-8")

    report = parity.verify_parity(path, backup).to_dict()

    assert report["verdict"] == "parity_failed"
    assert report["restore_failures"] >= 1


def test_parity_fails_when_content_diverges(tmp_path: Path) -> None:
    path, backup = _prepare(tmp_path)
    # 正規化ログ側の非news情報を書き換える → 復元してもbackupと一致しない
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    events[0]["label_version"] = "tampered"
    _write(path, events)

    report = parity.verify_parity(path, backup).to_dict()

    assert report["verdict"] == "parity_failed"
    assert report["mismatched"] >= 1


def test_parity_cli_exit_codes(tmp_path: Path) -> None:
    path, backup = _prepare(tmp_path)
    assert parity.main(["--path", str(path), "--backup", str(backup)]) == 0

    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    events[0]["symbol"] = "EURUSD"
    _write(path, events)
    assert parity.main(["--path", str(path), "--backup", str(backup)]) == 1


def test_parity_cli_autodetects_latest_backup(tmp_path: Path) -> None:
    path, _backup = _prepare(tmp_path)
    # --backup 省略でも decision_store_admin-* の最新originalを拾う
    assert parity.main(["--path", str(path)]) == 0
