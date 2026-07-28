#!/usr/bin/env python3
"""backfill前後の完全判断ログが可逆(無損失)であることを検証する。

`decision_store_admin.py backfill --apply` は news_items 全文をサイドカーへ畳むが、
その操作が本当に情報を1ビットも失っていないかは、正規化後のログとサイドカーから
元の形へ復元し、backup(旧形式)とイベント単位で完全一致するかで確かめられる。

このツールは読み取り専用で、次を照合する。

- backup(旧形式)の各イベントと、正規化ログを `restore_event` で復元した
  イベントが、正規化キー(`decision_id`)ごとに完全一致する。
- 件数、順序、欠損、余剰が無い。

すべて一致した場合だけ verdict を ``parity_verified`` とし exit 0 を返す。
1件でも差分・欠損・復元失敗があれば ``parity_failed`` で exit 1。
runtime data には書き込まない。
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.decision_store_admin import (  # noqa: E402
    DEFAULT_PATH,
    NEWS_SIDECAR_SUFFIX,
    _events_of_line,
    _load_news_sidecar,
    restore_event,
)

VERDICT_VERIFIED = "parity_verified"
VERDICT_FAILED = "parity_failed"


@dataclass(frozen=True)
class ParityReport:
    verdict: str
    normalized_path: str
    backup_path: str
    news_sidecar_path: str
    backup_events: int
    normalized_events: int
    matched: int
    mismatched: int
    missing_in_normalized: int
    extra_in_normalized: int
    restore_failures: int
    corrupt_lines: int

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "generated_at": datetime.now(UTC).isoformat(),
            "normalized_path": self.normalized_path,
            "backup_path": self.backup_path,
            "news_sidecar_path": self.news_sidecar_path,
            "backup_events": self.backup_events,
            "normalized_events": self.normalized_events,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "missing_in_normalized": self.missing_in_normalized,
            "extra_in_normalized": self.extra_in_normalized,
            "restore_failures": self.restore_failures,
            "corrupt_lines": self.corrupt_lines,
        }


def _iter_events(path: Path) -> Iterator[dict[str, object]]:
    if not path.is_file():
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                yield {"__corrupt__": True}
                continue
            # 現行の判断ログは 1行 = 1バッチ。バッチを1イベントとして数えると
            # backup と正規化ログの件数が食い違い、無損失なbackfillでも
            # parity_failed になる。
            events = _events_of_line(parsed)
            if events is None:
                yield {"__corrupt__": True}
                continue
            yield from events


def _key(event: Mapping[str, object]) -> str:
    return str(event.get("decision_id") or "")


def _canonical(event: Mapping[str, object]) -> str:
    return json.dumps(event, ensure_ascii=False, sort_keys=True)


def verify_parity(
    normalized_path: str | Path,
    backup_path: str | Path,
) -> ParityReport:
    """正規化ログを復元し、backup(旧形式)とイベント単位で完全一致を検証する。"""

    normalized = Path(normalized_path)
    backup = Path(backup_path)
    sidecar = normalized.with_name(f"{normalized.stem}{NEWS_SIDECAR_SUFFIX}")
    news_index = _load_news_sidecar(sidecar)

    corrupt = 0
    restore_failures = 0
    # 同一 decision_id が複数回現れる場合(writer retry 等)に潰さないよう、
    # キーごとに復元結果を*多重集合*として保持する。dictへ上書きすると
    # 「backupに2行・正規化ログに1行」の欠落を parity_verified と誤判定する。
    restored_by_key: dict[str, list[str]] = {}
    normalized_events = 0
    for event in _iter_events(normalized):
        if event.get("__corrupt__"):
            corrupt += 1
            continue
        normalized_events += 1
        try:
            restored = restore_event(event, news_index)
        except KeyError:
            restore_failures += 1
            continue
        restored_by_key.setdefault(_key(restored), []).append(_canonical(restored))

    backup_events = 0
    matched = 0
    mismatched = 0
    missing = 0
    for event in _iter_events(backup):
        if event.get("__corrupt__"):
            corrupt += 1
            continue
        backup_events += 1
        # backup 側も正規化済みでありうる。writer が先に畳んだ行は backfill 前から
        # 参照形式で、そのまま比較すると無損失な backfill を mismatch と誤判定する。
        # 双方を同じ表現(復元後)へ揃えてから突合する。
        try:
            backup_restored = restore_event(event, news_index)
        except KeyError:
            restore_failures += 1
            continue
        candidates = restored_by_key.get(_key(backup_restored))
        if not candidates:
            missing += 1
            continue
        blob = _canonical(backup_restored)
        if blob in candidates:
            candidates.remove(blob)
            matched += 1
        else:
            # 同じキーで内容が違う行しか残っていない。1件消費して mismatch とする。
            candidates.pop()
            mismatched += 1
    extra = sum(len(blobs) for blobs in restored_by_key.values())

    ok = (
        backup_events > 0
        and mismatched == 0
        and missing == 0
        and extra == 0
        and restore_failures == 0
        and corrupt == 0
        and matched == backup_events
    )
    return ParityReport(
        verdict=VERDICT_VERIFIED if ok else VERDICT_FAILED,
        normalized_path=str(normalized),
        backup_path=str(backup),
        news_sidecar_path=str(sidecar),
        backup_events=backup_events,
        normalized_events=normalized_events,
        matched=matched,
        mismatched=mismatched,
        missing_in_normalized=missing,
        extra_in_normalized=extra,
        restore_failures=restore_failures,
        corrupt_lines=corrupt,
    )


def _latest_backup(target: Path) -> Path | None:
    """decision_store_admin が作った最新バックアップを推定する。"""

    candidates = sorted(target.parent.glob(f"decision_store_admin-*/{target.name}.original"))
    return candidates[-1] if candidates else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument(
        "--backup",
        type=Path,
        default=None,
        help="旧形式backup。省略時はdecision_store_admin-*の最新originalを使う",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.path if args.path.is_absolute() else REPO_ROOT / args.path
    backup = args.backup
    if backup is None:
        backup = _latest_backup(path)
        if backup is None:
            print(
                "backupが見つかりません。--backup で旧形式ファイルを指定してください",
                file=sys.stderr,
            )
            return 1
    elif not backup.is_absolute():
        backup = REPO_ROOT / backup

    report = verify_parity(path, backup)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.verdict == VERDICT_VERIFIED else 1


if __name__ == "__main__":
    raise SystemExit(main())
