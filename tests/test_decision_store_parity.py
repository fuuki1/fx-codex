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
