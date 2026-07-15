"""時間足別価格履歴の重複衝突修復ツールを検証する。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

from fx_intel import price_history


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "repair_tf_price_history.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("repair_tf_price_history", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(ts: str, close: float, writer: str) -> dict[str, object]:
    return {
        "ts": ts,
        "event_time": ts,
        "available_time": ts,
        "ingested_time": ts,
        "capture_slot": "2026-07-06T09:25:00+00:00",
        "symbol": "USDJPY",
        "timeframe": "15m",
        "close": close,
        "source": "tradingview_ta_scanner",
        "schema_version": 2,
        "ohlc_scope": "quote_snapshot",
        "writer_id": writer,
    }


def test_audit_keeps_earliest_available_conflicting_snapshot(tmp_path: Path) -> None:
    tool = _load_tool()
    path = tmp_path / "prices.jsonl"
    later = _row("2026-07-06T09:29:00+00:00", 160.2, "writer-a")
    earlier = _row("2026-07-06T09:25:10+00:00", 160.1, "writer-b")
    exact_duplicate = _row("2026-07-06T09:25:20+00:00", 160.1, "writer-b")
    path.write_text(
        "\n".join(json.dumps(row) for row in (later, earlier, exact_duplicate)) + "\n",
        encoding="utf-8",
    )

    rows, quarantine, counts = tool.audit(path)

    assert len(rows) == 1
    assert rows[0].row["close"] == 160.1
    assert counts["conflicting_duplicates"] == 1
    assert counts["exact_duplicates"] == 1
    assert len(quarantine) == 2


def test_apply_repair_backs_up_quarantines_and_restores_writable_file(
    tmp_path: Path,
) -> None:
    tool = _load_tool()
    path = tmp_path / "prices.jsonl"
    path.write_text(
        json.dumps(_row("2026-07-06T09:29:00+00:00", 160.2, "writer-a"))
        + "\n"
        + json.dumps(_row("2026-07-06T09:25:10+00:00", 160.1, "writer-b"))
        + "\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    rows, quarantine, counts = tool.audit(path)
    backup_dir = tmp_path / "backup"

    report = tool.apply_repair(path, rows, quarantine, counts, backup_dir)

    assert (backup_dir / "briefing_tf_prices.original.jsonl").read_bytes() == before
    assert (backup_dir / "briefing_tf_prices.quarantine.jsonl").is_file()
    assert report["applied"] is True
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert price_history.append_snapshot_entries(path, []) == 0


def test_dry_run_main_does_not_modify_file(tmp_path: Path, capsys) -> None:
    tool = _load_tool()
    path = tmp_path / "prices.jsonl"
    path.write_text(
        json.dumps(_row("2026-07-06T09:29:00+00:00", 160.2, "writer-a")) + "\n",
        encoding="utf-8",
    )
    before = path.read_bytes()

    assert tool.main(["--path", str(path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert path.read_bytes() == before
