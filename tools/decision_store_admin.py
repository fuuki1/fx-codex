#!/usr/bin/env python3
"""完全判断ログ(briefing_decisions.jsonl)を監査し、news_items全文の複製を安全に畳む。

`market_context.news_items` は判断ごとにニュース本文を丸ごと複製するため、同じ
1,080種のニュースが17,000件超の判断へ重複展開され、ファイルが数百MBまで肥大化する
(1件55KB中の61%がnews_items)。全読込とダッシュボードのレイテンシを悪化させ、
briefing周期を圧迫する。

backfillは各ニュースをコンテンツハッシュで正規化し、サイドカー
`*_news.jsonl`(各ニュース1回だけ)へ退避して、判断側を `news_item_refs`(ハッシュ配列)
へ置き換える。判断ID・会計・PIT・shadowなど他のフィールドは一切変更しない。決定論的で、
同じ入力の再実行は同じ出力とハッシュを生む。

安全境界:
- 既定はdry-run。`--apply`時だけ元ファイルを完全バックアップしてからatomic置換する。
- 壊れた行は破棄せずquarantineへ退避する(監査可能性の維持)。
- audit監査ログには書き込まない。runtime dataの変更はbackfillのみで、実行前に
  `com.fx-codex.briefing`をbootoutし、判断writerを停止すること。
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.decision_log import (  # noqa: E402
    NEWS_NORMALIZED_KEY,
    NEWS_SIDECAR_SUFFIX,
    news_content_hash,
    news_sidecar_path,
)
from fx_intel.state_io import atomic_write_json  # noqa: E402

# 参照契約(ハッシュ・サイドカー名・正規化マーカー)はwriterである
# `fx_intel.decision_log` が唯一の定義元。ここで再定義すると、片方だけが
# 変更されたときにwriterが畳んだ行をこのツールが解決できなくなり、
# dangling refを静かに生む。import して共有する。
DEFAULT_PATH = Path("logs/briefing_decisions.jsonl")
BACKFILL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AuditReport:
    path: str
    total_lines: int
    valid_events: int
    corrupt_lines: int
    total_bytes: int
    news_items_bytes: int
    news_item_rows: int
    unique_news_items: int
    already_normalized: int

    def to_dict(self) -> dict[str, object]:
        news_ratio = self.news_items_bytes / self.total_bytes if self.total_bytes else None
        duplication = (
            self.news_item_rows / self.unique_news_items if self.unique_news_items else None
        )
        return {
            "path": self.path,
            "total_lines": self.total_lines,
            "valid_events": self.valid_events,
            "corrupt_lines": self.corrupt_lines,
            "total_bytes": self.total_bytes,
            "news_items_bytes": self.news_items_bytes,
            "news_items_byte_ratio": _round(news_ratio),
            "news_item_rows": self.news_item_rows,
            "unique_news_items": self.unique_news_items,
            "news_duplication_factor": _round(duplication),
            "already_normalized_events": self.already_normalized,
        }


@dataclass(frozen=True)
class BackfillReport:
    audit: AuditReport
    applied: bool
    events_rewritten: int
    news_items_deduplicated: int
    corrupt_lines_quarantined: int
    original_bytes: int
    rewritten_bytes: int
    backup_path: str
    news_sidecar_path: str
    quarantine_path: str
    report_path: str

    def to_dict(self) -> dict[str, object]:
        reduction = (
            1.0 - (self.rewritten_bytes / self.original_bytes) if self.original_bytes else None
        )
        return {
            "schema_version": BACKFILL_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "applied": self.applied,
            "audit": self.audit.to_dict(),
            "events_rewritten": self.events_rewritten,
            "news_items_deduplicated": self.news_items_deduplicated,
            "corrupt_lines_quarantined": self.corrupt_lines_quarantined,
            "original_bytes": self.original_bytes,
            "rewritten_bytes": self.rewritten_bytes,
            "estimated_byte_reduction": _round(reduction),
            "backup_path": self.backup_path,
            "news_sidecar_path": self.news_sidecar_path,
            "quarantine_path": self.quarantine_path,
            "report_path": self.report_path,
        }


@dataclass(frozen=True)
class ReconcileReport:
    path: str
    news_sidecar_path: str
    checked_events: int
    normalized_events: int
    unnormalized_events: int
    corrupt_lines: int
    missing_news_refs: int
    tampered_news_rows: int
    ref_count_mismatches: int
    drift: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "news_sidecar_path": self.news_sidecar_path,
            "checked_events": self.checked_events,
            "normalized_events": self.normalized_events,
            "unnormalized_events": self.unnormalized_events,
            "corrupt_lines": self.corrupt_lines,
            "missing_news_refs": self.missing_news_refs,
            "tampered_news_rows": self.tampered_news_rows,
            "ref_count_mismatches": self.ref_count_mismatches,
            "drift": self.drift,
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _iter_lines(path: Path) -> Iterator[str]:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                yield stripped


def _news_items_of(event: Mapping[str, object]) -> list[dict[str, object]]:
    context = event.get("market_context")
    if not isinstance(context, Mapping):
        return []
    items = context.get("news_items")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, Mapping)]


def audit_store(path: str | Path) -> AuditReport:
    """判断ログを読み取り専用で走査し、肥大化の内訳を集計する。"""

    target = Path(path)
    total_lines = 0
    valid_events = 0
    corrupt_lines = 0
    total_bytes = 0
    news_items_bytes = 0
    news_item_rows = 0
    already_normalized = 0
    unique: set[str] = set()
    if not target.is_file():
        return AuditReport(str(target), 0, 0, 0, 0, 0, 0, 0, 0)
    for line in _iter_lines(target):
        total_lines += 1
        total_bytes += len(line.encode("utf-8")) + 1  # 改行分
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            corrupt_lines += 1
            continue
        if not isinstance(event, dict):
            corrupt_lines += 1
            continue
        valid_events += 1
        if event.get(NEWS_NORMALIZED_KEY) is True:
            already_normalized += 1
        items = _news_items_of(event)
        if items:
            news_items_bytes += len(json.dumps(items, ensure_ascii=False).encode("utf-8"))
            news_item_rows += len(items)
            for item in items:
                unique.add(news_content_hash(item))
    return AuditReport(
        path=str(target),
        total_lines=total_lines,
        valid_events=valid_events,
        corrupt_lines=corrupt_lines,
        total_bytes=total_bytes,
        news_items_bytes=news_items_bytes,
        news_item_rows=news_item_rows,
        unique_news_items=len(unique),
        already_normalized=already_normalized,
    )


def _normalize_event(
    event: dict[str, object],
    news_index: dict[str, dict[str, object]],
) -> tuple[dict[str, object], int]:
    """1イベントのnews_items全文をハッシュ参照へ畳む。会計/PIT情報は不変。"""

    context = event.get("market_context")
    if not isinstance(context, Mapping):
        return event, 0
    items = _news_items_of(event)
    if not items:
        return event, 0
    refs: list[str] = []
    added = 0
    for item in items:
        digest = news_content_hash(item)
        if digest not in news_index:
            news_index[digest] = {"news_item_hash": digest, **item}
            added += 1
        refs.append(digest)
    new_context = dict(context)
    new_context.pop("news_items", None)
    new_context["news_item_refs"] = refs
    new_context["news_count"] = len(refs)
    rewritten = dict(event)
    rewritten["market_context"] = new_context
    rewritten[NEWS_NORMALIZED_KEY] = True
    return rewritten, added


def backfill_store(
    path: str | Path,
    *,
    apply: bool,
    now: datetime | None = None,
) -> BackfillReport:
    """news_items全文の複製を正規化サイドカーへ畳む。既定はdry-run。"""

    target = Path(path)
    audit = audit_store(target)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = target.parent / f"decision_store_admin-{stamp}"
    # 同一秒に再実行しても既存バックアップを壊さないよう、衝突時は連番で退避する。
    suffix = 1
    while apply and backup_dir.exists():
        suffix += 1
        backup_dir = target.parent / f"decision_store_admin-{stamp}-{suffix}"
    backup_path = backup_dir / f"{target.name}.original"
    quarantine_path = backup_dir / f"{target.name}.quarantine"
    sidecar_backup_path = backup_dir / f"{target.stem}{NEWS_SIDECAR_SUFFIX}.original"
    report_path = backup_dir / "backfill_report.json"
    sidecar_path = news_sidecar_path(target)

    # 既存サイドカーを索引の初期値にする。writerが先に畳んだニュースは、この
    # ログにもう全文で現れないため、読まずに書き直すとその行が消え、判断側の
    # 参照だけが残る(dangling ref)。writer投入後のログは新旧混在になるので、
    # この初期化が無いとbackfillは可逆でなくなる。
    news_index: dict[str, dict[str, object]] = _load_news_sidecar(sidecar_path)
    preexisting_news_items = len(news_index)
    rewritten_lines: list[str] = []
    quarantine_lines: list[str] = []
    events_rewritten = 0
    if target.is_file():
        for line in _iter_lines(target):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                quarantine_lines.append(line)
                continue
            if not isinstance(event, dict):
                quarantine_lines.append(line)
                continue
            normalized, _added = _normalize_event(event, news_index)
            if normalized is not event:
                events_rewritten += 1
            rewritten_lines.append(json.dumps(normalized, ensure_ascii=False, sort_keys=True))

    rewritten_blob = "".join(f"{line}\n" for line in rewritten_lines)
    rewritten_bytes = len(rewritten_blob.encode("utf-8"))

    if apply and target.is_file():
        backup_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(target, backup_path)
        # サイドカーも置換対象なので必ず退避する。読めなかった行は索引に載らず、
        # 置換で消える。判断ログだけを退避していると、参照されたニュースが
        # 復元不能になる runtime evidence の消失になる。
        if sidecar_path.is_file():
            shutil.copy2(sidecar_path, sidecar_backup_path)
        if quarantine_lines:
            _atomic_text(quarantine_path, "".join(f"{q}\n" for q in quarantine_lines))
        _atomic_text(
            sidecar_path,
            "".join(
                f"{json.dumps(row, ensure_ascii=False, sort_keys=True)}\n"
                for row in news_index.values()
            ),
        )
        _atomic_text(target, rewritten_blob)

    report = BackfillReport(
        audit=audit,
        applied=apply,
        events_rewritten=events_rewritten,
        # このbackfillが新たに畳んだ件数。既存サイドカー分は含めない。
        news_items_deduplicated=len(news_index) - preexisting_news_items,
        corrupt_lines_quarantined=len(quarantine_lines),
        original_bytes=audit.total_bytes,
        rewritten_bytes=rewritten_bytes,
        backup_path=str(backup_path),
        news_sidecar_path=str(sidecar_path),
        quarantine_path=str(quarantine_path),
        report_path=str(report_path),
    )
    if apply and target.is_file():
        atomic_write_json(report_path, report.to_dict())
    return report


def _load_news_sidecar(path: Path) -> dict[str, dict[str, object]]:
    """正規化サイドカーを {hash: news_row} で読む。壊れた行はスキップ。"""

    index: dict[str, dict[str, object]] = {}
    if not path.is_file():
        return index
    for line in _iter_lines(path):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            digest = str(row.get("news_item_hash") or "")
            if digest:
                index[digest] = row
    return index


def restore_event(
    event: Mapping[str, object],
    news_index: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """正規化済みイベントを、backfill前の全文 news_items 形式へ復元する。

    参照ハッシュをサイドカーで引き、`news_item_hash` を除いた本文を
    `news_items` へ差し戻し、`news_item_refs` と正規化マーカーを取り除く。
    `_normalize_event` の逆写像であり、可逆性の検証に使う。参照が引けない
    場合はKeyErrorを送出し、黙って欠損を埋めない(fail-closed)。
    """

    restored = dict(event)
    restored.pop(NEWS_NORMALIZED_KEY, None)
    context = event.get("market_context")
    if not isinstance(context, Mapping):
        return restored
    new_context = dict(context)
    refs = new_context.pop("news_item_refs", None)
    if not isinstance(refs, list):
        return restored
    items: list[dict[str, object]] = []
    for digest in refs:
        row = news_index[str(digest)]
        items.append({key: value for key, value in row.items() if key != "news_item_hash"})
    new_context["news_items"] = items
    new_context["news_count"] = len(items)
    restored["market_context"] = new_context
    return restored


def reconcile_store(path: str | Path) -> ReconcileReport:
    """backfill後のドリフトを検証する。読み取り専用。drift=0でexit 0。

    正規化済みイベントの `news_item_refs` について次を確認する。

    - 参照ハッシュがすべてサイドカーに存在する(欠損参照の検出)。
    - サイドカーの本文を再ハッシュすると参照ハッシュと一致する
      (サイドカー改ざん・不整合の検出)。
    - `news_count` が参照件数と一致する。
    - 全文 `news_items` を残したままの未正規化イベントが無い。

    いずれかが崩れた件数を `drift` として返す。0なら正規化は無損失。
    """

    target = Path(path)
    sidecar_path = target.with_name(f"{target.stem}{NEWS_SIDECAR_SUFFIX}")
    news_index = _load_news_sidecar(sidecar_path)

    checked = normalized = unnormalized = corrupt = 0
    missing_refs = tampered = count_mismatch = 0
    if target.is_file():
        for line in _iter_lines(target):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                corrupt += 1
                continue
            if not isinstance(event, dict):
                corrupt += 1
                continue
            checked += 1
            context = event.get("market_context")
            context_map = context if isinstance(context, Mapping) else {}
            if event.get(NEWS_NORMALIZED_KEY) is not True:
                # backfill対象だった(news_items全文を持つ)のに未正規化なら残存ドリフト。
                if _news_items_of(event):
                    unnormalized += 1
                continue
            normalized += 1
            refs = context_map.get("news_item_refs")
            refs = refs if isinstance(refs, list) else []
            if context_map.get("news_count") != len(refs):
                count_mismatch += 1
            for digest in refs:
                row = news_index.get(str(digest))
                if row is None:
                    missing_refs += 1
                    continue
                restored = {key: value for key, value in row.items() if key != "news_item_hash"}
                if news_content_hash(restored) != str(digest):
                    tampered += 1

    # 読めない・空・欠損したログを「無損失」と認証してはいけない。検証対象が
    # 1件も無い状態は成功ではなく、検証不能としてfail closedにする。
    unavailable = 1 if checked == 0 else 0
    drift = unnormalized + missing_refs + tampered + count_mismatch + corrupt + unavailable
    return ReconcileReport(
        path=str(target),
        news_sidecar_path=str(sidecar_path),
        checked_events=checked,
        normalized_events=normalized,
        unnormalized_events=unnormalized,
        corrupt_lines=corrupt,
        missing_news_refs=missing_refs,
        tampered_news_rows=tampered,
        ref_count_mismatches=count_mismatch,
        drift=drift,
    )


def _atomic_text(path: Path, text: str) -> None:
    """tmp→fsync→renameでテキストを原子的に書く(途中クラッシュで壊れた行を残さない)。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("audit", help="読み取り専用で肥大化の内訳を報告する")
    backfill = sub.add_parser("backfill", help="news_items全文の複製を正規化する(既定dry-run)")
    backfill.add_argument(
        "--apply",
        action="store_true",
        help="実際に元ファイルをバックアップして置換する(未指定はdry-run)",
    )
    sub.add_parser(
        "reconcile",
        help="backfill後のドリフトを検証する(読み取り専用・drift>0でexit 1)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.path if args.path.is_absolute() else REPO_ROOT / args.path
    if args.command == "audit":
        audit = audit_store(path)
        print(json.dumps(audit.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "backfill":
        backfill = backfill_store(path, apply=args.apply)
        print(json.dumps(backfill.to_dict(), ensure_ascii=False, indent=2))
        if not args.apply:
            print(
                "dry-run: 変更していません。--apply で置換します"
                "(実行前にcom.fx-codex.briefingを停止すること)",
                file=sys.stderr,
            )
        return 0
    if args.command == "reconcile":
        reconcile = reconcile_store(path)
        print(json.dumps(reconcile.to_dict(), ensure_ascii=False, indent=2))
        if reconcile.drift:
            print(
                f"drift検出: {reconcile.drift}件。正規化は無損失ではありません",
                file=sys.stderr,
            )
            return 1
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
