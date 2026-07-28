#!/usr/bin/env python3
"""融合判断ジャーナルを読み取り、次の定期取得が必要か判定する。"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys

DUE_EXIT_CODE = 0
INVALID_EXIT_CODE = 2
NOT_DUE_EXIT_CODE = 3
DEFAULT_MINIMUM_INTERVAL_MINUTES = 55


class FusionCaptureScheduleError(ValueError):
    """安全に取得時刻を判定できない場合。"""


def _aware_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FusionCaptureScheduleError("最終行のtsが空または文字列ではありません")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise FusionCaptureScheduleError("最終行のtsがISO-8601ではありません") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FusionCaptureScheduleError("最終行のtsにtimezoneがありません")
    return parsed.astimezone(UTC)


def _is_decision_row(row: dict[str, object]) -> bool:
    """判断行だけを対象にする(commit receipt等の制御行を除外)。

    decision_commit がバッチ確定時に追記する receipt 行は `ts` を持たず
    `committed_at` を持つ。判断の最終時刻を求める用途では制御行を読み飛ばす。
    """

    if "ts" not in row:
        return False
    return not str(row.get("event_type") or "").endswith("_commit")


def latest_journal_timestamp(journal: Path) -> datetime | None:
    """最終「判断行」のaware UTC時刻を返す。未作成・空ファイルはNone。

    末尾には commit receipt が来るため、後ろから判断行を探す。
    判断行が1件も無い場合は None(=未取得扱い)を返す。
    """
    if not journal.exists():
        return None
    try:
        rows: list[dict[str, object]] = []
        with journal.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as error:
                    raise FusionCaptureScheduleError("journalに不正なJSON行があります") from error
                if not isinstance(parsed, dict):
                    raise FusionCaptureScheduleError("journal行がobjectではありません")
                rows.append(parsed)
    except (OSError, UnicodeError) as error:
        raise FusionCaptureScheduleError(f"journalを読めません: {error}") from error
    for row in reversed(rows):
        if _is_decision_row(row):
            return _aware_timestamp(row.get("ts"))
    return None


def capture_is_due(
    journal: Path,
    *,
    now: datetime | None = None,
    minimum_interval_minutes: int = DEFAULT_MINIMUM_INTERVAL_MINUTES,
) -> bool:
    """最後の融合判断から指定時間以上ならTrue。"""
    if minimum_interval_minutes <= 0:
        raise FusionCaptureScheduleError("minimum intervalは正の整数が必要です")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise FusionCaptureScheduleError("現在時刻にtimezoneがありません")
    current = current.astimezone(UTC)
    latest = latest_journal_timestamp(journal)
    if latest is None:
        return True
    if latest > current:
        raise FusionCaptureScheduleError("journal最終時刻が現在時刻より未来です")
    return current - latest >= timedelta(minutes=minimum_interval_minutes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument(
        "--minimum-interval-minutes",
        type=int,
        default=DEFAULT_MINIMUM_INTERVAL_MINUTES,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        due = capture_is_due(
            args.journal,
            minimum_interval_minutes=args.minimum_interval_minutes,
        )
    except FusionCaptureScheduleError as error:
        print(f"fusion capture schedule invalid: {error}", file=sys.stderr)
        return INVALID_EXIT_CODE
    if due:
        print("fusion capture due")
        return DUE_EXIT_CODE
    print("fusion capture not due")
    return NOT_DUE_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
