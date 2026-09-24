#!/usr/bin/env python3
"""Operational runner for complete decision expected-R monitoring.

This command scores the complete decision log by TP/SL first-touch, MFE, MAE,
and realized R, derives the next-decision feedback profile, then writes a
monitor JSON suitable for cron, CI, and the read-only dashboard.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel import (  # noqa: E402
    decision_commit,
    decision_feedback,
    decision_log,
    journal,
    market,
    price_path_adapter,
    price_history,
    stage_timing,
    timeframe as timeframe_config,
    trade_outcome,
)

DEFAULT_DECISION_LOG_PATH = REPO_ROOT / "logs" / "briefing_decisions.jsonl"
DEFAULT_DECISION_JOURNAL_PATHS = (
    REPO_ROOT / "logs" / "briefing_journal.jsonl",
    REPO_ROOT / "logs" / "briefing_tf_journal.jsonl",
)
DEFAULT_TF_PRICES_PATH = REPO_ROOT / "logs" / "briefing_tf_prices.jsonl"
DEFAULT_OUTCOME_REPORT_PATH = REPO_ROOT / "logs" / "briefing_decision_outcomes.json"
DEFAULT_FEEDBACK_PATH = REPO_ROOT / "logs" / "briefing_decision_feedback.json"
DEFAULT_MONITOR_PATH = REPO_ROOT / "logs" / "decision_expectancy_monitor.json"
DEFAULT_FEEDBACK_REFRESH_MONITOR_PATH = (
    REPO_ROOT / "logs" / "decision_feedback_refresh_monitor.json"
)
DEFAULT_PRICE_STALE_MINUTES = 15.0
# The authentic datafeed is intentionally delayed by two hours and refreshed hourly
# at :20.  A closed-bar timestamp can therefore be almost 200 minutes old just
# before the next healthy adapter run; 210 minutes preserves a small run-time
# margin while still rejecting a missed hourly refresh on the following cycle.
DEFAULT_SHARED_PRICE_PATH_STALE_MINUTES = 210.0
# Writers hold LOCK_EX while re-reading the whole append-only file; under I/O
# contention that exceeds a few seconds. Readers poll with LOCK_NB, so a longer
# bounded wait never delays a writer; it only avoids discarding the whole run.
SOURCE_LOCK_TIMEOUT_SECONDS = 120.0
DEFAULT_ACTIVE_SYMBOLS = ("USDJPY", "EURUSD")
DEFAULT_ACTIVE_TIMEFRAMES = timeframe_config.DEFAULT_TIMEFRAMES


def run_decision_expectancy_monitor(
    *,
    decision_log_path: Path | None = DEFAULT_DECISION_LOG_PATH,
    decision_journal_paths: Sequence[Path] | None = None,
    prices_path: Path | None = DEFAULT_TF_PRICES_PATH,
    outcome_json_path: Path | None = DEFAULT_OUTCOME_REPORT_PATH,
    feedback_json_path: Path | None = None,
    monitor_json_path: Path = DEFAULT_MONITOR_PATH,
    require_sample_ok: bool = False,
    price_stale_minutes: float | None = DEFAULT_PRICE_STALE_MINUTES,
    stage_timing_log: Path | None = None,
    now: datetime | None = None,
    feedback_refresh_only: bool = False,
    allow_continuity_bootstrap: bool = False,
    active_symbols: Sequence[str] = DEFAULT_ACTIVE_SYMBOLS,
    active_timeframes: Sequence[str] = DEFAULT_ACTIVE_TIMEFRAMES,
    require_shared_price_path: bool = False,
    shared_price_path_stale_minutes: float = DEFAULT_SHARED_PRICE_PATH_STALE_MINUTES,
) -> dict[str, Any]:
    """Run the full complete-decision expectancy monitoring pass."""

    generated_at = _utc(now or datetime.now(UTC))
    sources = _decision_sources(decision_log_path, decision_journal_paths)
    timing = stage_timing.StageTimingRecorder(
        stage_timing_log,
        pipeline="decision_expectancy_monitor",
        metadata={
            "decision_log_paths": sources,
            "prices_path": prices_path,
            "generated_at": generated_at,
        },
    )
    with timing.stage("decision_log_reader_open"):
        events, decision_snapshots = _decision_event_stream(sources)
    with timing.stage("price_journal_read"):
        raw_price_rows, price_snapshot = _price_snapshot_rows(prices_path)
        price_rows, price_quality = _pit_price_rows(raw_price_rows, as_of=generated_at)
    with timing.stage("price_health_build"):
        price_health = _price_file_health(
            prices_path,
            price_rows,
            now=generated_at,
            stale_minutes=price_stale_minutes,
            quality=price_quality,
        )
    with timing.stage("decision_log_read_and_outcome_score", price_row_count=len(price_rows)):
        outcome_report = decision_log.score_decision_events(
            events,
            price_entries=price_rows,
            now=generated_at,
            require_pit=True,
            include_shadow=not feedback_refresh_only,
        )
    source_continuity: dict[str, object] = {}
    prior_continuity_anchor: list[dict[str, object]] = []
    source_snapshots: list[dict[str, object]] = []
    if feedback_refresh_only:
        source_snapshots = _complete_source_snapshots(
            decision_snapshots,
            price_snapshot,
        )
        source_continuity, prior_continuity_anchor = _source_continuity(
            source_snapshots,
            previous_monitor_path=monitor_json_path,
            allow_bootstrap=allow_continuity_bootstrap,
        )
    else:
        # Complete audits publish immutable provenance for every decision
        # source too.  In particular, never pair a live/unbounded full-log
        # reader with an empty snapshot list in the audit artifact.
        decision_snapshots = _complete_source_snapshots(decision_snapshots, None)
    price_health = _price_file_health(
        prices_path,
        price_rows,
        now=generated_at,
        stale_minutes=price_stale_minutes,
        quality=price_quality,
        required_contexts=_required_price_contexts(
            outcome_report,
            active_symbols=active_symbols,
            active_timeframes=active_timeframes,
        ),
        required_shared_path_symbols=(
            tuple(sorted({str(symbol).upper() for symbol in active_symbols}))
            if require_shared_price_path
            else ()
        ),
        shared_path_stale_minutes=shared_price_path_stale_minutes,
    )
    input_event_count = outcome_report.get("input_decision_events")
    decision_event_count = (
        int(input_event_count) if isinstance(input_event_count, (int, float)) else 0
    )
    with timing.stage("decision_feedback_derive"):
        profile = decision_feedback.derive_decision_feedback(outcome_report, now=generated_at)
    with timing.stage("monitor_snapshot_build"):
        monitor = (
            _feedback_refresh_snapshot(
                profile,
                outcome_report,
                now=generated_at,
                price_health=price_health,
                source_continuity=source_continuity,
            )
            if feedback_refresh_only
            else decision_feedback.build_monitoring_snapshot(
                profile,
                outcome_report,
                now=generated_at,
                require_sample_ok=require_sample_ok,
                price_health=price_health,
            )
        )
    monitor["runner"] = {
        "schema": 1,
        "decision_log_path": str(sources[0]) if len(sources) == 1 else None,
        "decision_log_paths": [str(path) for path in sources],
        "decision_source_snapshots": decision_snapshots,
        "price_source_snapshot": price_snapshot,
        "prices_path": str(prices_path) if prices_path is not None else None,
        "outcome_json_path": str(outcome_json_path) if outcome_json_path else None,
        "feedback_json_path": str(feedback_json_path) if feedback_json_path else None,
        "monitor_json_path": str(monitor_json_path),
        "decision_event_count": decision_event_count,
        "price_row_count": len(price_rows),
        "raw_price_row_count": len(raw_price_rows),
        "require_sample_ok": require_sample_ok,
        "price_stale_minutes": price_stale_minutes,
        "scope": (
            "actual_decision_feedback_refresh" if feedback_refresh_only else "complete_audit"
        ),
        "shadow_scoring": "not_requested" if feedback_refresh_only else "complete_audit",
        "continuity_bootstrap_authorized": allow_continuity_bootstrap,
        "active_consumer_manifest": {
            "symbols": sorted({str(symbol).upper() for symbol in active_symbols}),
            "timeframes": sorted({str(value) for value in active_timeframes}),
            "mode": "per_timeframe",
        },
        "shared_price_path_required": require_shared_price_path,
        "shared_price_path_stale_minutes": shared_price_path_stale_minutes,
    }
    if feedback_refresh_only:
        monitor["source_snapshots"] = source_snapshots
        monitor["continuity_anchor_snapshots"] = (
            source_snapshots
            if source_continuity.get("status") == "pass"
            else prior_continuity_anchor
        )

    if feedback_refresh_only and outcome_json_path is not None:
        raise ValueError("feedback-only refresh cannot publish a complete outcome report")
    if outcome_json_path is not None:
        with timing.stage("outcome_report_save", path=outcome_json_path):
            decision_log.save_outcome_report(outcome_report, outcome_json_path)
    feedback_publishable = (
        price_health.get("status") == "pass"
        and decision_event_count > 0
        and _int_or_zero(outcome_report.get("scored_outcomes")) > 0
        and bool(profile.cells)
        and (not feedback_refresh_only or monitor.get("status") == "ok")
    )
    if feedback_json_path is not None and feedback_publishable:
        with timing.stage("decision_feedback_save", path=feedback_json_path):
            decision_feedback.save_decision_feedback(profile, feedback_json_path)
        monitor["feedback_artifact"] = _feedback_artifact(
            feedback_json_path,
            profile=profile,
        )
    elif feedback_json_path is not None:
        monitor["feedback_artifact"] = None
    with timing.stage("monitor_save", path=monitor_json_path):
        _write_json_atomic(monitor_json_path, monitor)

    return {
        "exit_code": _int_or_zero(monitor.get("exit_code")),
        "monitor": monitor,
        "outcome_report": outcome_report,
        "profile": profile,
        "outcome_report_path": outcome_json_path,
        "feedback_json_path": feedback_json_path,
        "monitor_json_path": monitor_json_path,
        "timing": stage_timing.summarize(timing.records),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="完全判断ログのTP/SL/MFE/MAE期待R監視を一括実行する"
    )
    parser.add_argument(
        "--decision-log",
        type=Path,
        action="append",
        default=None,
        help=(
            "判断ソース。省略時は完全監査ログ。" "複数指定可。legacy完全ログも1件指定なら互換読込"
        ),
    )
    parser.add_argument(
        "--compact-feedback-refresh",
        action="store_true",
        help=(
            "fusion/時間足compact journalから実判断feedbackだけを更新する。"
            "shadow/完全監査reportは評価・上書きしない"
        ),
    )
    parser.add_argument(
        "--initialize-continuity-anchor",
        action="store_true",
        help="初回配備時だけsource continuity anchorの新規作成を許可する",
    )
    parser.add_argument(
        "--prices",
        type=Path,
        default=DEFAULT_TF_PRICES_PATH,
        help="時間足別の価格系列JSONL。none で無効化",
    )
    parser.add_argument(
        "--outcome-json",
        type=Path,
        default=DEFAULT_OUTCOME_REPORT_PATH,
        help="完全判断ログのTP/SL/MFE/MAE採点JSON。none で無効化",
    )
    parser.add_argument(
        "--feedback-json",
        type=Path,
        default=None,
        help=(
            "次回判断に反映する期待RフィードバックJSON。"
            "compact更新では省略時にcanonical、完全監査では省略時に無効。"
            "none で明示的に無効化"
        ),
    )
    parser.add_argument("--monitor-json", type=Path, default=DEFAULT_MONITOR_PATH)
    parser.add_argument(
        "--stage-timing-log",
        type=Path,
        default=None,
        help="工程別UTC時刻・monotonic経過時間を追記するJSONLパス",
    )
    parser.add_argument(
        "--health-require-sample",
        action="store_true",
        help="最低サンプル数未満をヘルスチェック失敗扱いにする",
    )
    parser.add_argument(
        "--price-stale-minutes",
        default=str(DEFAULT_PRICE_STALE_MINUTES),
        help="価格系列のstale判定分数。none で無効化",
    )
    parser.add_argument(
        "--shared-price-path-stale-minutes",
        default=str(DEFAULT_SHARED_PRICE_PATH_STALE_MINUTES),
        help="共有5分確定足のsource-role別stale判定分数",
    )
    parser.add_argument(
        "--active-symbols",
        nargs="+",
        default=list(DEFAULT_ACTIVE_SYMBOLS),
        help="公開feedbackを選択できるscheduled consumerのsymbol集合",
    )
    parser.add_argument(
        "--active-timeframes",
        nargs="+",
        default=list(DEFAULT_ACTIVE_TIMEFRAMES),
        help="公開feedbackを選択できるscheduled consumerのtimeframe集合",
    )
    parser.add_argument("--quiet", action="store_true", help="標準出力の要約を抑制する")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.compact_feedback_refresh and args.decision_log:
        parser.error("--compact-feedback-refresh cannot be combined with --decision-log")
    if args.initialize_continuity_anchor and not args.compact_feedback_refresh:
        parser.error("--initialize-continuity-anchor requires --compact-feedback-refresh")
    compact_sources = DEFAULT_DECISION_JOURNAL_PATHS if args.compact_feedback_refresh else None
    result = run_decision_expectancy_monitor(
        decision_log_path=(
            None
            if compact_sources is not None
            else (
                args.decision_log[0]
                if args.decision_log and len(args.decision_log) == 1
                else DEFAULT_DECISION_LOG_PATH if not args.decision_log else None
            )
        ),
        decision_journal_paths=(
            compact_sources
            or (args.decision_log if args.decision_log and len(args.decision_log) > 1 else None)
        ),
        prices_path=_optional_path(args.prices),
        outcome_json_path=(
            None if args.compact_feedback_refresh else _optional_path(args.outcome_json)
        ),
        feedback_json_path=(
            DEFAULT_FEEDBACK_PATH
            if args.compact_feedback_refresh and args.feedback_json is None
            else _optional_path(args.feedback_json)
        ),
        monitor_json_path=(
            DEFAULT_FEEDBACK_REFRESH_MONITOR_PATH
            if args.compact_feedback_refresh and args.monitor_json == DEFAULT_MONITOR_PATH
            else args.monitor_json
        ),
        require_sample_ok=args.health_require_sample,
        price_stale_minutes=_optional_float(args.price_stale_minutes),
        stage_timing_log=args.stage_timing_log,
        feedback_refresh_only=args.compact_feedback_refresh,
        allow_continuity_bootstrap=args.initialize_continuity_anchor,
        active_symbols=args.active_symbols,
        active_timeframes=args.active_timeframes,
        require_shared_price_path=True,
        shared_price_path_stale_minutes=float(args.shared_price_path_stale_minutes),
    )
    if not args.quiet:
        _print_summary(result)
    return int(result["exit_code"])


def _decision_sources(
    decision_log_path: Path | None,
    decision_journal_paths: Sequence[Path] | None,
) -> tuple[Path, ...]:
    if decision_log_path is not None and decision_journal_paths is not None:
        raise ValueError("decision_log_path and decision_journal_paths are mutually exclusive")
    raw = (
        tuple(decision_journal_paths)
        if decision_journal_paths is not None
        else (
            (decision_log_path,)
            if decision_log_path is not None
            else DEFAULT_DECISION_JOURNAL_PATHS
        )
    )
    sources = tuple(Path(path).resolve() for path in raw)
    if not sources:
        raise ValueError("at least one decision source is required")
    if len(set(sources)) != len(sources):
        raise ValueError("decision sources must be distinct")
    return sources


def _decision_event_stream(
    sources: Sequence[Path],
) -> tuple[Iterable[Mapping[str, object]], list[dict[str, object]]]:
    compact = [
        path
        for path in sources
        if decision_commit.commit_path_for(path).resolve() != path.resolve()
    ]
    if compact and len(compact) != len(sources):
        raise ValueError("compact journals and legacy full logs cannot be mixed")
    if not compact:
        if len(sources) != 1:
            raise ValueError("only one legacy full decision log can be read")
        return _full_decision_event_stream(sources[0])

    boundaries = _snapshot_boundaries(sources)
    snapshots: list[dict[str, object]] = []

    def _entries() -> Iterable[Mapping[str, object]]:
        for source in sources:
            full_path = decision_commit.commit_path_for(source).resolve()
            source_snapshot = boundaries[source.resolve()]
            full_snapshot = boundaries[full_path]
            audit: dict[str, object] = {}
            for entry in journal.read_entries(
                source,
                strict=True,
                audit=audit,
                end_offset=_required_int(
                    source_snapshot.get("read_offset_bytes"), "compact read offset"
                ),
                full_log_end_offset=_required_int(
                    full_snapshot.get("read_offset_bytes"), "full-log read offset"
                ),
            ):
                # The complete-log reader withholds standalone PIT rows that
                # predate the cross-log transaction contract.  Compact journals
                # retain those rows for backward compatibility, so explicitly
                # hide them here to keep the operational feedback population
                # identical to the committed complete-log population.
                if journal.is_pit_eligible_entry(entry) and not str(
                    entry.get("decision_transaction_id") or ""
                ):
                    continue
                yield entry
            source_snapshot.update(
                {
                    "source_sha256": audit.get("source_sha256"),
                    "physical_rows": audit.get("physical_rows"),
                    "parsed_object_rows": audit.get("parsed_object_rows"),
                }
            )
            _assert_snapshot_identity(source, source_snapshot)
            _assert_snapshot_identity(full_path, full_snapshot)

    for source in sources:
        snapshots.append(boundaries[source.resolve()])
    full_paths = {decision_commit.commit_path_for(source).resolve() for source in sources}
    snapshots.extend(boundaries[path] for path in sorted(full_paths))
    return _entries(), snapshots


def _full_decision_event_stream(
    source: Path,
) -> tuple[Iterable[Mapping[str, object]], list[dict[str, object]]]:
    """Read one complete log at a strict, receipt-proven byte boundary."""

    full_path = source.resolve()
    compact_paths = tuple(
        path.resolve()
        for mode in sorted(decision_commit.COMPACT_FILENAMES)
        if (path := decision_commit.compact_path_for_mode(full_path, mode)).exists()
    )
    boundaries = _snapshot_paths((full_path, *compact_paths))
    full_snapshot = boundaries[full_path]
    full_end_offset = _required_int(full_snapshot.get("read_offset_bytes"), "full-log read offset")
    snapshots = [boundaries[path] for path in (full_path, *compact_paths)]

    def _entries() -> Iterable[Mapping[str, object]]:
        receipts: dict[str, dict[str, object]] = {}
        for compact_path in compact_paths:
            compact_snapshot = boundaries[compact_path]
            compact_end_offset = _required_int(
                compact_snapshot.get("read_offset_bytes"), "compact read offset"
            )
            compact_audit: dict[str, object] = {}
            # Validate every physical compact row strictly, even though the
            # complete audit yields the richer events from the full log.
            for _entry in journal.read_entries(
                compact_path,
                strict=True,
                audit=compact_audit,
                end_offset=compact_end_offset,
                full_log_end_offset=full_end_offset,
            ):
                pass
            compact_snapshot.update(
                {
                    "source_sha256": compact_audit.get("source_sha256"),
                    "physical_rows": compact_audit.get("physical_rows"),
                    "parsed_object_rows": compact_audit.get("parsed_object_rows"),
                }
            )
            verified = decision_commit.load_compact_receipts(
                compact_path,
                full_path=full_path,
                compact_end_offset=compact_end_offset,
                full_end_offset=full_end_offset,
            )
            for transaction_id, receipt in verified.items():
                previous = receipts.get(transaction_id)
                if previous is not None and previous != receipt:
                    raise journal.JournalIntegrityError(
                        "conflicting compact receipts for decision transaction " f"{transaction_id}"
                    )
                receipts[transaction_id] = receipt
            _assert_snapshot_identity(compact_path, compact_snapshot)

        physical_rows = 0
        parsed_object_rows = 0
        digest = hashlib.sha256()
        with full_path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (
                full_snapshot.get("device"),
                full_snapshot.get("inode"),
            ):
                raise RuntimeError(f"decision source identity changed before read: {full_path}")
            while handle.tell() < full_end_offset:
                line_start_offset = handle.tell()
                remaining = full_end_offset - line_start_offset
                raw_line = handle.readline(remaining + 1)
                if not raw_line or len(raw_line) > remaining or not raw_line.endswith(b"\n"):
                    raise journal.JournalIntegrityError(
                        f"full decision log has an incomplete bounded row at byte "
                        f"{line_start_offset}: {full_path}"
                    )
                physical_rows += 1
                digest.update(raw_line)
                if not raw_line.strip():
                    raise journal.JournalIntegrityError(
                        f"blank row {physical_rows} in full decision log: {full_path}"
                    )
                try:
                    raw: Any = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise journal.JournalIntegrityError(
                        f"malformed JSON row {physical_rows} in full decision log: " f"{full_path}"
                    ) from error
                if not isinstance(raw, dict):
                    raise journal.JournalIntegrityError(
                        f"non-object row {physical_rows} in full decision log: {full_path}"
                    )
                parsed_object_rows += 1
                event_type = raw.get("event_type")
                if event_type == decision_commit.DECISION_BATCH_EVENT_TYPE:
                    yield from _receipt_published_full_batch(
                        raw,
                        raw_line=raw_line,
                        line_start_offset=line_start_offset,
                        receipts=receipts,
                    )
                    continue
                if event_type in {
                    decision_commit.COMMIT_EVENT_TYPE,
                    decision_commit.RECEIPT_EVENT_TYPE,
                }:
                    continue
                if journal.is_pit_eligible_entry(raw):
                    # Standalone PIT rows predate the cross-log durability
                    # contract and are intentionally withheld.
                    continue
                yield raw

        full_digest = digest.hexdigest()
        full_snapshot.update(
            {
                "source_sha256": full_digest,
                "physical_rows": physical_rows,
                "parsed_object_rows": parsed_object_rows,
            }
        )
        _assert_snapshot_identity(full_path, full_snapshot)
        if _sha256_snapshot_prefix(full_snapshot) != full_digest:
            raise RuntimeError(f"decision source prefix changed during audit: {full_path}")

    return _entries(), snapshots


def _receipt_published_full_batch(
    batch: Mapping[str, object],
    *,
    raw_line: bytes,
    line_start_offset: int,
    receipts: Mapping[str, Mapping[str, object]],
) -> Iterable[Mapping[str, object]]:
    """Yield a full batch only when one bounded compact receipt proves it."""

    if (
        batch.get("schema_version") != decision_commit.SCHEMA_VERSION
        or batch.get("event_type") != decision_commit.DECISION_BATCH_EVENT_TYPE
    ):
        return ()
    raw_events = batch.get("events")
    if (
        not isinstance(raw_events, Sequence)
        or isinstance(raw_events, (str, bytes))
        or not raw_events
        or not all(isinstance(event, Mapping) for event in raw_events)
    ):
        return ()
    events = [dict(event) for event in raw_events]
    try:
        decision_ids = decision_commit.normalize_decision_ids(
            [event.get("decision_id") for event in events]
        )
        decision_ids_sha256 = decision_commit.decision_ids_sha256(decision_ids)
        transaction_id = decision_commit.transaction_id_for(decision_ids)
        batch_sha256 = decision_commit.canonical_sha256(events)
    except (decision_commit.DecisionCommitError, TypeError, ValueError):
        return ()
    modes = {str(event.get("mode") or "") for event in events}
    if len(modes) != 1:
        return ()
    mode = next(iter(modes))
    receipt = receipts.get(transaction_id)
    if (
        batch.get("decision_transaction_id") != transaction_id
        or batch.get("decision_ids") != decision_ids
        or batch.get("decision_ids_sha256") != decision_ids_sha256
        or batch.get("decision_batch_sha256") != batch_sha256
        or receipt is None
        or receipt.get("decision_transaction_id") != transaction_id
        or receipt.get("decision_ids") != decision_ids
        or receipt.get("decision_ids_sha256") != decision_ids_sha256
        or receipt.get("full_batch_sha256") != batch_sha256
        or receipt.get("mode") != mode
        or receipt.get("full_batch_line_start_offset") != line_start_offset
        or receipt.get("full_batch_line_sha256") != hashlib.sha256(raw_line).hexdigest()
    ):
        return ()
    return events


def _snapshot_boundaries(sources: Sequence[Path]) -> dict[Path, dict[str, object]]:
    paths = {
        path.resolve()
        for source in sources
        for path in (source, decision_commit.commit_path_for(source))
    }
    return _snapshot_paths(paths)


def _snapshot_paths(paths: Iterable[Path]) -> dict[Path, dict[str, object]]:
    resolved_paths = {Path(path).resolve() for path in paths}
    handles: dict[Path, Any] = {}
    deadline = time.monotonic() + SOURCE_LOCK_TIMEOUT_SECONDS
    try:
        for path in sorted(resolved_paths):
            handle = path.open("rb")
            try:
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                        break
                    except OSError as error:
                        if error.errno not in (errno.EACCES, errno.EAGAIN):
                            raise
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"decision source lock timeout: {path}") from error
                        time.sleep(0.01)
            except BaseException:
                handle.close()
                raise
            handles[path] = handle
        snapshots: dict[Path, dict[str, object]] = {}
        for path, handle in handles.items():
            stat = os.fstat(handle.fileno())
            if stat.st_size:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    raise RuntimeError(f"decision source snapshot ends inside a row: {path}")
            snapshots[path] = {
                "source_path": str(path),
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "read_offset_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        return snapshots
    finally:
        for handle in reversed(list(handles.values())):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _price_snapshot_rows(
    path: Path | None,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    if path is None:
        return [], None
    resolved = path.resolve()
    if not resolved.exists():
        return [], None
    snapshot = _snapshot_paths((resolved,))[resolved]
    audit: dict[str, object] = {}
    rows = list(
        journal.read_entries(
            resolved,
            strict=True,
            audit=audit,
            end_offset=_required_int(snapshot.get("read_offset_bytes"), "price read offset"),
        )
    )
    snapshot.update(
        {
            "source_sha256": audit.get("source_sha256"),
            "physical_rows": audit.get("physical_rows"),
            "parsed_object_rows": audit.get("parsed_object_rows"),
        }
    )
    _assert_snapshot_identity(resolved, snapshot)
    return rows, snapshot


def _assert_snapshot_identity(path: Path, snapshot: Mapping[str, object]) -> None:
    stat = path.stat()
    if (stat.st_dev, stat.st_ino) != (snapshot.get("device"), snapshot.get("inode")):
        raise RuntimeError(f"decision source identity changed after snapshot: {path}")
    offset = snapshot.get("read_offset_bytes")
    if not isinstance(offset, int) or stat.st_size < offset:
        raise RuntimeError(f"decision source truncated after snapshot: {path}")


def _required_int(value: object, name: str) -> int:
    if type(value) is not int:  # noqa: E721 - bool is not a valid byte offset
        raise RuntimeError(f"{name} is not an integer")
    return value


def _int_or_zero(value: object) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def _pit_price_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    as_of: datetime,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Keep only authentic price rows that were available by ``as_of``.

    Operational feedback is fail-closed for malformed timestamps and invalid
    current-schema rows.  Legacy rows remain measurable but excluded: they do
    not carry enough provenance to support a point-in-time cache refresh.
    """

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("price snapshot as_of must be timezone-aware")
    cutoff = as_of.astimezone(UTC)
    eligible: list[dict[str, object]] = []
    invalid_timestamp_rows = 0
    future_timestamp_rows = 0
    invalid_canonical_rows = 0
    legacy_excluded_rows = 0
    for raw in rows:
        row = dict(raw)
        recorded = _parse_aware_ts(row.get("ts"))
        if recorded is None:
            invalid_timestamp_rows += 1
            continue
        if recorded > cutoff:
            future_timestamp_rows += 1
            continue
        if price_history.is_pit_eligible_snapshot_entry(row, as_of=cutoff):
            eligible.append(row)
            continue
        if row.get("schema_version") == price_history.SNAPSHOT_SCHEMA_VERSION:
            invalid_canonical_rows += 1
        else:
            legacy_excluded_rows += 1
    return eligible, {
        "strict_pit": True,
        "raw_row_count": len(rows),
        "pit_eligible_rows": len(eligible),
        "invalid_timestamp_rows": invalid_timestamp_rows,
        "future_timestamp_rows": future_timestamp_rows,
        "invalid_canonical_rows": invalid_canonical_rows,
        "legacy_excluded_rows": legacy_excluded_rows,
    }


def _complete_source_snapshots(
    decision_snapshots: Sequence[dict[str, object]],
    price_snapshot: dict[str, object] | None,
) -> list[dict[str, object]]:
    snapshots = [*decision_snapshots]
    if price_snapshot is not None:
        snapshots.append(price_snapshot)
    output: list[dict[str, object]] = []
    seen: set[str] = set()
    for snapshot in snapshots:
        source_path = str(snapshot.get("source_path") or "")
        if not source_path or source_path in seen:
            continue
        seen.add(source_path)
        digest = snapshot.get("source_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            snapshot["source_sha256"] = _sha256_snapshot_prefix(snapshot)
        output.append(dict(snapshot))
    return sorted(output, key=lambda row: str(row.get("source_path") or ""))


def _source_continuity(
    current: Sequence[Mapping[str, object]],
    *,
    previous_monitor_path: Path,
    allow_bootstrap: bool,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Verify that every successful source generation is append-only."""

    if not previous_monitor_path.exists():
        return {
            "status": "pass" if allow_bootstrap else "fail",
            "bootstrap": allow_bootstrap,
            "checked_sources": 0,
            "failures": [] if allow_bootstrap else ["previous_monitor_missing"],
        }, []
    try:
        previous = json.loads(previous_monitor_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "status": "fail",
            "bootstrap": False,
            "checked_sources": 0,
            "failures": [f"previous_monitor_unreadable:{type(error).__name__}"],
        }, []
    if not isinstance(previous, Mapping) or previous.get("monitor_contract") != (
        "fx-codex-decision-feedback-refresh-v1"
    ):
        return {
            "status": "fail",
            "bootstrap": False,
            "checked_sources": 0,
            "failures": ["previous_monitor_contract_invalid"],
        }, []
    raw_anchor = previous.get("continuity_anchor_snapshots")
    if not isinstance(raw_anchor, list) and previous.get("status") == "ok":
        raw_anchor = previous.get("source_snapshots")
    anchor = [dict(row) for row in raw_anchor or [] if isinstance(row, Mapping)]
    if not anchor:
        if "continuity_anchor_snapshots" in previous:
            return {
                "status": "fail",
                "bootstrap": False,
                "checked_sources": 0,
                "failures": ["previous_success_anchor_unavailable"],
            }, []
        return {
            "status": "pass",
            "bootstrap": True,
            "checked_sources": 0,
            "failures": [],
        }, []

    prior_by_path = {str(row.get("source_path") or ""): row for row in anchor}
    current_by_path = {str(row.get("source_path") or ""): row for row in current}
    pairs = [
        (source_path, prior_by_path[source_path], current_by_path[source_path])
        for source_path in sorted(set(prior_by_path) & set(current_by_path))
    ]
    # A relocated log (e.g. moved to another volume behind a symlink) keeps its
    # file name but resolves to a new path. Pair it with its prior generation
    # only when the name is unambiguous on both sides; the prefix digest below
    # then decides whether the move preserved every previously scored byte.
    unmatched_prior = sorted(set(prior_by_path) - set(current_by_path))
    unmatched_current = sorted(set(current_by_path) - set(prior_by_path))
    for prior_path in list(unmatched_prior):
        name = Path(prior_path).name
        same_prior = [path for path in unmatched_prior if Path(path).name == name]
        same_current = [path for path in unmatched_current if Path(path).name == name]
        if len(same_prior) != 1 or len(same_current) != 1:
            continue
        current_path = same_current[0]
        pairs.append((current_path, prior_by_path[prior_path], current_by_path[current_path]))
        unmatched_prior.remove(prior_path)
        unmatched_current.remove(current_path)
    failures: list[str] = []
    if unmatched_prior or unmatched_current:
        failures.append("source_set_changed")
    checked = 0
    relocated: list[dict[str, object]] = []
    for source_path, prior, present in pairs:
        checked += 1
        moved = (
            prior.get("source_path"),
            prior.get("device"),
            prior.get("inode"),
        ) != (
            present.get("source_path"),
            present.get("device"),
            present.get("inode"),
        )
        if moved and not (
            type(prior.get("read_offset_bytes")) is int  # noqa: E721
            and int(prior["read_offset_bytes"]) > 0
        ):
            # An empty prior range hashes the same for any file: no proof.
            failures.append(f"source_identity_changed:{source_path}")
            continue
        try:
            prior_offset = _required_int(
                prior.get("read_offset_bytes"), "previous source read offset"
            )
            current_offset = _required_int(
                present.get("read_offset_bytes"), "current source read offset"
            )
        except RuntimeError:
            failures.append(f"source_offset_invalid:{source_path}")
            continue
        if current_offset < prior_offset:
            failures.append(f"source_truncated:{source_path}")
            continue
        prior_digest = prior.get("source_sha256")
        if not isinstance(prior_digest, str) or len(prior_digest) != 64:
            failures.append(f"previous_source_hash_invalid:{source_path}")
            continue
        try:
            current_prefix_digest = _sha256_snapshot_prefix(
                present,
                end_offset=prior_offset,
            )
        except (OSError, RuntimeError, TimeoutError) as error:
            failures.append(f"source_recheck_failed:{source_path}:{type(error).__name__}")
            continue
        if current_prefix_digest != prior_digest:
            failures.append(f"source_prefix_changed:{source_path}")
            continue
        if moved:
            relocated.append(
                {
                    "source_path": source_path,
                    "previous_source_path": prior.get("source_path"),
                    "previous_device": prior.get("device"),
                    "previous_inode": prior.get("inode"),
                    "verified_prefix_bytes": prior_offset,
                }
            )
    return {
        "status": "fail" if failures else "pass",
        "bootstrap": False,
        "checked_sources": checked,
        "failures": failures,
        "relocated_sources": relocated,
    }, anchor


def _sha256_snapshot_prefix(
    snapshot: Mapping[str, object],
    *,
    end_offset: int | None = None,
) -> str:
    path = Path(str(snapshot.get("source_path") or ""))
    boundary = (
        _required_int(snapshot.get("read_offset_bytes"), "source read offset")
        if end_offset is None
        else end_offset
    )
    if type(boundary) is not int or boundary < 0:  # noqa: E721
        raise RuntimeError("source hash boundary is invalid")
    deadline = time.monotonic() + SOURCE_LOCK_TIMEOUT_SECONDS
    with path.open("rb") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"source hash lock timeout: {path}") from error
                time.sleep(0.01)
        try:
            stat = os.fstat(handle.fileno())
            if (stat.st_dev, stat.st_ino) != (
                snapshot.get("device"),
                snapshot.get("inode"),
            ):
                raise RuntimeError(f"source identity changed before hashing: {path}")
            if stat.st_size < boundary:
                raise RuntimeError(f"source truncated before hashing: {path}")
            digest = hashlib.sha256()
            remaining = boundary
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError(f"source ended before hash boundary: {path}")
                digest.update(chunk)
                remaining -= len(chunk)
            return digest.hexdigest()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _required_price_contexts(
    report: Mapping[str, object],
    *,
    active_symbols: Sequence[str] = DEFAULT_ACTIVE_SYMBOLS,
    active_timeframes: Sequence[str] = DEFAULT_ACTIVE_TIMEFRAMES,
) -> tuple[tuple[str, str, str], ...]:
    """Return price contexts selectable by the published feedback consumer.

    Historical compact journals retain manual fusion decisions so their mature
    outcomes remain auditable.  The scheduled consumer is explicitly
    per-timeframe and its lookup cannot select fusion cells, so a retired/manual
    fusion price heartbeat must not veto publication of the per-timeframe
    cache.  Avoid recency inference here: wall-clock windows misclassify market
    closures and neutral decision periods.
    """

    raw_outcomes = report.get("outcomes")
    outcomes = raw_outcomes if isinstance(raw_outcomes, list) else []
    allowed_symbols = {str(symbol).upper() for symbol in active_symbols}
    allowed_timeframes = {str(value) for value in active_timeframes}
    contexts = {
        (
            str(row.get("symbol") or "").upper(),
            str(row.get("mode") or "fusion"),
            str(row.get("timeframe") or "fusion"),
        )
        for row in outcomes
        if isinstance(row, Mapping)
        and str(row.get("symbol") or "").strip()
        and str(row.get("timeframe") or "").strip()
        and str(row.get("mode") or "fusion") == "per_timeframe"
        and str(row.get("symbol") or "").upper() in allowed_symbols
        and str(row.get("timeframe") or "fusion") in allowed_timeframes
    }
    return tuple(sorted(contexts))


def _feedback_artifact(
    path: Path,
    *,
    profile: decision_feedback.DecisionFeedbackProfile,
) -> dict[str, object]:
    resolved = path.resolve()
    body = resolved.read_bytes()
    return {
        "path": str(resolved),
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "generated_at": profile.generated_at,
        "profile_semantic_sha256": decision_feedback.feedback_semantic_sha256(profile),
    }


def _feedback_refresh_snapshot(
    profile: decision_feedback.DecisionFeedbackProfile,
    report: Mapping[str, object],
    *,
    now: datetime,
    price_health: Mapping[str, object],
    source_continuity: Mapping[str, object],
) -> dict[str, object]:
    decision_events = report.get("decision_events")
    scored_outcomes = report.get("scored_outcomes")
    input_ok = (
        isinstance(decision_events, int)
        and decision_events > 0
        and isinstance(scored_outcomes, int)
        and scored_outcomes > 0
        and bool(profile.cells)
    )
    price_ok = price_health.get("status") == "pass"
    continuity_ok = source_continuity.get("status") == "pass"
    status = "ok" if input_ok and price_ok and continuity_ok else "fail"
    outcomes = report.get("outcomes")
    outcome_rows = outcomes if isinstance(outcomes, list) else []
    outcome_ids = sorted(
        str(row.get("decision_id") or "")
        for row in outcome_rows
        if isinstance(row, Mapping) and row.get("decision_id")
    )
    return {
        "schema": 1,
        "monitor_contract": "fx-codex-decision-feedback-refresh-v1",
        "generated_at": now.isoformat(),
        "status": status,
        "exit_code": 0 if status == "ok" else 1,
        "scope": "actual_decisions_only",
        "shadow_scoring": "not_requested",
        "complete_outcome_report_written": False,
        "summary": {
            "input_decision_events": report.get("input_decision_events"),
            "pit_eligible_decision_events": report.get("pit_eligible_decision_events"),
            "decision_events": decision_events,
            "scored_outcomes": scored_outcomes,
            "cell_count": len(profile.cells),
            "profile_semantic_sha256": decision_feedback.feedback_semantic_sha256(profile),
            "outcome_decision_ids_sha256": _canonical_sha256(outcome_ids),
            "price_health": dict(price_health),
            "source_continuity": dict(source_continuity),
        },
    }


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _mapping(value: object) -> dict[str, Any]:
    """Return a plain dict when ``value`` is a mapping, else an empty dict.

    Keeps the callers below fail-soft (a missing / malformed section reads as
    empty rather than crashing) while giving mypy a single concrete type instead
    of the ``Any | dict | None`` union that ternary narrowing leaves behind.
    """

    return dict(value) if isinstance(value, Mapping) else {}


def _print_summary(result: Mapping[str, Any]) -> None:
    monitor = result.get("monitor")
    if not isinstance(monitor, Mapping):
        print("decision expectancy monitor: no monitor payload")
        return
    summary = _mapping(monitor.get("summary"))
    overall = _mapping(summary.get("overall"))
    counts = _mapping(summary.get("action_counts"))
    price_health = _mapping(summary.get("price_health"))
    print(
        "decision expectancy monitor: "
        f"status={monitor.get('status')} exit={monitor.get('exit_code')} "
        f"events={summary.get('decision_events', 0)} "
        f"scored={summary.get('scored_outcomes', 0)} "
        f"expectancy={_fmt_r(overall.get('expectancy_r') if overall else None)} "
        f"price={price_health.get('status', 'unknown')} "
        f"avoid={counts.get('avoid', 0) if counts else 0} "
        f"dampen={counts.get('dampen', 0) if counts else 0}"
    )
    print(f"outcome_json={result.get('outcome_report_path')}")
    print(f"feedback_json={result.get('feedback_json_path')}")
    print(f"monitor_json={result.get('monitor_json_path')}")


def _optional_path(path: Path | str | None) -> Path | None:
    if path is None:
        return None
    if str(path).strip().lower() == "none":
        return None
    return Path(path)


def _optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    if str(value).strip().lower() == "none":
        return None
    return float(value)


def _price_file_health(
    path: Path | None,
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    stale_minutes: float | None,
    quality: Mapping[str, object],
    required_contexts: Sequence[tuple[str, str, str]] = (),
    required_shared_path_symbols: Sequence[str] = (),
    shared_path_stale_minutes: float = DEFAULT_SHARED_PRICE_PATH_STALE_MINUTES,
) -> dict[str, object]:
    strict_pit = quality.get("strict_pit") is True
    if path is None:
        return {
            "status": "fail" if strict_pit else "pass",
            "enabled": strict_pit,
            "path": None,
            "quality": dict(quality),
            "message_ja": (
                "運用feedback更新に必要な価格系列が無効です"
                if strict_pit
                else "価格系列のstale監視は無効です"
            ),
        }
    if stale_minutes is None and not strict_pit:
        return {
            "status": "pass",
            "enabled": False,
            "path": str(path),
            "row_count": len(rows),
            "quality": dict(quality),
            "message_ja": "価格系列のstale監視は無効です",
        }
    if not path.exists():
        return {
            "status": "fail",
            "enabled": True,
            "path": str(path),
            "row_count": 0,
            "stale_minutes": stale_minutes,
            "quality": dict(quality),
            "message_ja": "時間足別価格スナップショットが未作成です",
        }
    structural_failures = {
        key: _int_or_zero(quality.get(key))
        for key in (
            "invalid_timestamp_rows",
            "future_timestamp_rows",
            "invalid_canonical_rows",
        )
    }
    latest = _latest_row_ts(rows)
    if not strict_pit and latest is None:
        latest = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    age_minutes = (now - latest).total_seconds() / 60.0 if latest is not None else None

    context_health: list[dict[str, object]] = []
    for symbol, mode, timeframe in required_contexts:
        context_latest = _latest_context_price_ts(
            rows,
            symbol=symbol,
            mode=mode,
            timeframe=timeframe,
        )
        context_age = (
            (now - context_latest).total_seconds() / 60.0 if context_latest is not None else None
        )
        context_ok = context_latest is not None and (
            stale_minutes is None or context_age is not None and context_age <= stale_minutes
        )
        context_health.append(
            {
                "symbol": symbol,
                "mode": mode,
                "timeframe": timeframe,
                "latest_ts": context_latest.isoformat() if context_latest else None,
                "age_minutes": round(context_age, 1) if context_age is not None else None,
                "status": "pass" if context_ok else "fail",
            }
        )

    shared_path_health: list[dict[str, object]] = []
    for symbol in required_shared_path_symbols:
        shared_latest = _latest_shared_price_path_ts(rows, symbol=symbol)
        wall_age = (
            (now - shared_latest).total_seconds() / 60.0 if shared_latest is not None else None
        )
        shared_open_age = (
            market.open_hours_between(shared_latest, now) * 60.0
            if shared_latest is not None
            else None
        )
        shared_ok = (
            shared_latest is not None
            and shared_open_age is not None
            and shared_open_age <= shared_path_stale_minutes
        )
        shared_path_health.append(
            {
                "symbol": symbol,
                "timeframe": decision_log.SHARED_PRICE_PATH_TIMEFRAME,
                "latest_ts": shared_latest.isoformat() if shared_latest else None,
                "age_minutes": (round(shared_open_age, 1) if shared_open_age is not None else None),
                "wall_age_minutes": round(wall_age, 1) if wall_age is not None else None,
                "age_basis": "fx_market_open_time",
                "stale_minutes": shared_path_stale_minutes,
                "status": "pass" if shared_ok else "fail",
            }
        )

    structural_ok = not any(structural_failures.values())
    rows_ok = bool(rows)
    global_fresh = latest is not None and (
        stale_minutes is None or age_minutes is not None and age_minutes <= stale_minutes
    )
    contexts_ok = all(row["status"] == "pass" for row in context_health)
    shared_paths_ok = all(row["status"] == "pass" for row in shared_path_health)
    status = (
        "pass"
        if structural_ok and rows_ok and global_fresh and contexts_ok and shared_paths_ok
        else "fail"
    )
    failed_contexts = [
        f"{row['symbol']}|{row['mode']}|{row['timeframe']}"
        for row in context_health
        if row["status"] == "fail"
    ]
    failed_shared_paths = [
        str(row["symbol"]) for row in shared_path_health if row["status"] == "fail"
    ]
    if any(structural_failures.values()):
        message = "価格系列に未来時刻またはPIT契約違反があります"
    elif not rows:
        message = "PIT適格な時間足別価格スナップショットがありません"
    elif failed_shared_paths:
        message = "採点用の共有5分確定足が欠損またはstaleです"
    elif failed_contexts:
        message = "判断コンテキスト別の価格系列が欠損またはstaleです"
    elif status == "fail":
        message = f"時間足別価格スナップショットがstaleです({age_minutes:.1f}分)"
    else:
        message = "時間足別価格スナップショットはPIT適格かつ更新されています"
    return {
        "status": status,
        "enabled": stale_minutes is not None or strict_pit,
        "path": str(path),
        "row_count": len(rows),
        "latest_ts": latest.isoformat() if latest else None,
        "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "stale_minutes": stale_minutes,
        "quality": dict(quality),
        "required_context_count": len(required_contexts),
        "failed_contexts": failed_contexts,
        "context_health": context_health,
        "required_shared_path_symbol_count": len(required_shared_path_symbols),
        "failed_shared_path_symbols": failed_shared_paths,
        "shared_path_health": shared_path_health,
        "message_ja": message,
    }


def _latest_row_ts(rows: Sequence[Mapping[str, Any]]) -> datetime | None:
    latest: datetime | None = None
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def _parse_aware_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _latest_context_price_ts(
    rows: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    mode: str,
    timeframe: str,
) -> datetime | None:
    latest: datetime | None = None
    for row in rows:
        if str(row.get("symbol") or "").upper() != symbol:
            continue
        row_timeframe = str(row.get("timeframe") or "fusion")
        row_mode = str(row.get("mode") or "")
        if row_timeframe != timeframe or row_mode and row_mode != mode:
            continue
        ts = _parse_aware_ts(row.get("ts"))
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def _latest_shared_price_path_ts(
    rows: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
) -> datetime | None:
    latest: datetime | None = None
    for row in rows:
        if str(row.get("symbol") or "").upper() != symbol:
            continue
        if not _is_trusted_shared_price_path_row(row):
            continue
        ts = _parse_aware_ts(row.get("ts"))
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def _is_trusted_shared_price_path_row(row: Mapping[str, object]) -> bool:
    if (
        str(row.get("timeframe") or "") != price_path_adapter.ADAPTER_TIMEFRAME
        or str(row.get("source") or "") != price_path_adapter.ADAPTER_SOURCE
        or str(row.get("writer_id") or "") != price_path_adapter.ADAPTER_WRITER_ID
        or str(row.get("ohlc_scope") or "") != price_path_adapter.ADAPTER_OHLC_SCOPE
        or str(row.get("direction") or "")
        or row.get("stop") is not None
        or row.get("target1") is not None
    ):
        return False
    high = row.get("high")
    low = row.get("low")
    tick_count = row.get("tick_count")
    return (
        isinstance(high, int | float)
        and not isinstance(high, bool)
        and math.isfinite(float(high))
        and isinstance(low, int | float)
        and not isinstance(low, bool)
        and math.isfinite(float(low))
        and float(high) >= float(low)
        and type(tick_count) is int  # noqa: E721 - bool is not a tick count
        and tick_count > 0
    )


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(
                trade_outcome.json_safe(payload),
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
        tmp_path.replace(target)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _fmt_r(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}R"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
