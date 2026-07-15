#!/usr/bin/env python3
"""時間足別価格JSONLの重複キー衝突を監査し、安全に修復する。

既定はdry-run。`--apply`時だけ元ファイルを完全バックアップし、同じ
(capture_slot, symbol, timeframe)に複数行がある場合はPIT上もっとも安全な
「最初に利用可能になった行」を残す。除外行はquarantineへ保存する。

実行前にcom.fx-codex.snapshotをbootoutし、書込みを停止すること。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, UTC
import json
import os
from pathlib import Path
import shutil
import stat
from collections.abc import Mapping

from fx_intel.price_history import PriceHistoryWriteError, _same_snapshot, _snapshot_key

DEFAULT_PATH = Path("logs/briefing_tf_prices.jsonl")


@dataclass(frozen=True)
class Candidate:
    line_number: int
    row: dict[str, object]
    available_at: datetime


def _parse_time(row: Mapping[str, object]) -> datetime:
    for key in ("available_time", "event_time", "ts", "ingested_time"):
        value = row.get(key)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return datetime.max.replace(tzinfo=UTC)


def audit(
    path: Path,
) -> tuple[list[Candidate], list[dict[str, object]], dict[str, int]]:
    kept: dict[tuple[str, str, str], Candidate] = {}
    quarantine: list[dict[str, object]] = []
    counts = {
        "input_rows": 0,
        "kept_rows": 0,
        "exact_duplicates": 0,
        "conflicting_duplicates": 0,
        "invalid_rows": 0,
    }

    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            counts["input_rows"] += 1
            try:
                parsed = json.loads(raw_line)
            except json.JSONDecodeError:
                counts["invalid_rows"] += 1
                quarantine.append(
                    {
                        "reason": "invalid_json",
                        "line_number": line_number,
                        "raw": raw_line.rstrip("\n"),
                    }
                )
                continue
            if not isinstance(parsed, dict):
                counts["invalid_rows"] += 1
                quarantine.append(
                    {
                        "reason": "non_object_json",
                        "line_number": line_number,
                        "row": parsed,
                    }
                )
                continue
            row = dict(parsed)
            try:
                key = _snapshot_key(row)
            except PriceHistoryWriteError as error:
                counts["invalid_rows"] += 1
                quarantine.append(
                    {
                        "reason": "invalid_snapshot_key",
                        "line_number": line_number,
                        "error": str(error),
                        "row": row,
                    }
                )
                continue

            candidate = Candidate(line_number, row, _parse_time(row))
            previous = kept.get(key)
            if previous is None:
                kept[key] = candidate
                continue

            exact = _same_snapshot(previous.row, candidate.row)
            reason = "exact_duplicate" if exact else "conflicting_duplicate"
            counter = "exact_duplicates" if exact else "conflicting_duplicates"
            counts[counter] += 1

            if (candidate.available_at, candidate.line_number) < (
                previous.available_at,
                previous.line_number,
            ):
                quarantine.append(
                    {
                        "reason": reason,
                        "line_number": previous.line_number,
                        "natural_key": list(key),
                        "kept_line_number": candidate.line_number,
                        "row": previous.row,
                    }
                )
                kept[key] = candidate
            else:
                quarantine.append(
                    {
                        "reason": reason,
                        "line_number": candidate.line_number,
                        "natural_key": list(key),
                        "kept_line_number": previous.line_number,
                        "row": candidate.row,
                    }
                )

    ordered = sorted(kept.values(), key=lambda item: (item.available_at, item.line_number))
    counts["kept_rows"] = len(ordered)
    return ordered, quarantine, counts


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def apply_repair(
    path: Path,
    rows: list[Candidate],
    quarantine: list[dict[str, object]],
    counts: dict[str, int],
    backup_dir: Path,
) -> dict[str, object]:
    backup_dir.mkdir(parents=True, exist_ok=False)
    original = backup_dir / "briefing_tf_prices.original.jsonl"
    quarantine_path = backup_dir / "briefing_tf_prices.quarantine.jsonl"
    report_path = backup_dir / "repair_report.json"
    shutil.copy2(path, original)

    with quarantine_path.open("w", encoding="utf-8") as handle:
        for item in quarantine:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    mode = stat.S_IMODE(path.stat().st_mode)
    temp = path.with_name(f".{path.name}.repair-{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        for candidate in rows:
            handle.write(json.dumps(candidate.row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

    report: dict[str, object] = {
        **counts,
        "applied": True,
        "selection_policy": "earliest_available_time_then_line_number",
        "source": str(path),
        "backup": str(original),
        "quarantine": str(quarantine_path),
    }
    _atomic_json(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="時間足別価格JSONLの重複衝突を監査・修復")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--apply", action="store_true", help="バックアップ後に修復を適用")
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.path.is_file():
        parser.error(f"対象ファイルが存在しません: {args.path}")
    rows, quarantine, counts = audit(args.path)
    report: dict[str, object] = {
        **counts,
        "applied": False,
        "selection_policy": "earliest_available_time_then_line_number",
        "source": str(args.path),
    }

    if args.apply:
        backup_dir = args.backup_dir or Path("backups") / (
            "tf-price-repair-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        )
        report = apply_repair(args.path, rows, quarantine, counts, backup_dir)

    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
