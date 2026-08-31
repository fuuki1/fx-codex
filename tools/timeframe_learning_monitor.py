#!/usr/bin/env python3
"""Refresh per-timeframe learning caches outside the decision publication lock."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unicodedata

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel import (  # noqa: E402
    decision_commit,
    journal,
    maximization,
    price_history,
    stage_timing,
    tf_learning,
    tp_sl_learning,
)
from fx_intel.state_io import advisory_file_lock, atomic_write_json  # noqa: E402

DEFAULT_JOURNAL = REPO_ROOT / "logs" / "briefing_tf_journal.jsonl"
DEFAULT_PRICES = REPO_ROOT / "logs" / "briefing_tf_prices.jsonl"
DEFAULT_TF_LEARNING = REPO_ROOT / "logs" / "briefing_tf_learning.json"
DEFAULT_TP_SL_LEARNING = REPO_ROOT / "logs" / "briefing_tp_sl_learning.json"
DEFAULT_MAXIMIZATION = REPO_ROOT / "logs" / "briefing_maximization.json"
DEFAULT_MONITOR = REPO_ROOT / "logs" / "timeframe_learning_monitor.json"
MONITOR_CONTRACT = "fx-codex-timeframe-learning-monitor-v1"
BUNDLE_LOCK_TIMEOUT_SECONDS = 5.0
SOURCE_LOCK_TIMEOUT_SECONDS = 5.0


def run(args: argparse.Namespace) -> dict[str, object]:
    (
        journal_path,
        prices_path,
        tf_learning_path,
        tp_sl_learning_path,
        maximization_path,
        monitor_path,
        stage_timing_path,
    ) = _validate_output_bundle(args)
    timing = stage_timing.StageTimingRecorder(
        stage_timing_path,
        pipeline="timeframe_learning_monitor",
    )
    previous_sources = _previous_source_evidence(monitor_path)
    decision_audit: dict[str, object] = {}
    with timing.stage("timeframe_learning_journal_read"):
        decision_source, decision_full_source = _source_snapshot(
            journal_path,
            previous_source=previous_sources.get("decisions"),
            previous_full_source=previous_sources.get("cross_log_commit"),
        )
        decisions = list(
            journal.read_entries(
                journal_path,
                strict=True,
                audit=decision_audit,
                end_offset=int(decision_source["read_offset_bytes"]),
                full_log_end_offset=(
                    int(decision_full_source["read_offset_bytes"])
                    if decision_full_source is not None
                    else None
                ),
            )
        )
        _assert_snapshot_prefix_available(
            journal_path,
            decision_source,
            observed_sha256=_audit_digest(decision_audit),
        )
        if decision_full_source is not None:
            _assert_snapshot_prefix_available(
                decision_commit.commit_path_for(journal_path),
                decision_full_source,
            )
            _assert_previous_prefix_unchanged(
                decision_full_source,
                previous_sources.get("cross_log_commit"),
                label="decision cross-log commit source",
            )
        _assert_previous_prefix_unchanged(
            decision_source,
            previous_sources.get("decisions"),
            label="decision learning source",
        )
        decision_source.pop("_previous_prefix_sha256", None)
        if decision_full_source is not None:
            decision_full_source.pop("_previous_prefix_sha256", None)
    price_audit: dict[str, object] = {}
    with timing.stage("timeframe_learning_price_read"):
        price_source, _ = _source_snapshot(
            prices_path,
            previous_source=previous_sources.get("prices"),
        )
        raw_prices = list(
            journal.read_entries(
                prices_path,
                strict=True,
                audit=price_audit,
                end_offset=int(price_source["read_offset_bytes"]),
            )
        )
        _assert_snapshot_prefix_available(
            prices_path,
            price_source,
            observed_sha256=_audit_digest(price_audit),
        )
        _assert_previous_prefix_unchanged(
            price_source,
            previous_sources.get("prices"),
            label="price learning source",
        )
        price_source.pop("_previous_prefix_sha256", None)
    generated_at = datetime.now(UTC)
    with timing.stage("timeframe_learning_pit_filter"):
        pit_decisions = [
            row
            for row in decisions
            if journal.is_pit_eligible_entry(row)
            and _not_future(row.get("prediction_time"), generated_at)
        ]
        prices = [
            row
            for row in raw_prices
            if price_history.is_pit_eligible_snapshot_entry(row, as_of=generated_at)
        ]
    scoring_entries = [*pit_decisions, *prices]

    with timing.stage("timeframe_direction_learning_refresh"):
        direction = tf_learning.derive_timeframe_learning(scoring_entries, now=generated_at)
    with timing.stage("timeframe_tp_sl_learning_refresh"):
        tp_sl = tp_sl_learning.derive_timeframe_tp_sl_learning(scoring_entries, now=generated_at)
    with timing.stage("timeframe_maximization_learning_refresh"):
        maximum = maximization.derive_timeframe_maximization(scoring_entries, now=generated_at)

    # Hash the large input populations before taking the publication lock. The
    # lock protects only the small cache/manifest swap, not minutes of JSON
    # serialization that would delay the five-minute decision reader.
    with timing.stage("timeframe_learning_manifest_prepare"):
        input_watermarks = {
            "decisions": {
                **decision_source,
                "sha256": _rows_sha256(pit_decisions),
                "source_sha256": _audit_digest(decision_audit),
                "physical_rows": _audit_count(decision_audit, "physical_rows"),
                "parsed_object_rows": _audit_count(decision_audit, "parsed_object_rows"),
                "blank_rows": _audit_count(decision_audit, "blank_rows"),
                "raw_rows": len(decisions),
                "eligible_rows": len(pit_decisions),
                "max_prediction_time": _max_timestamp(pit_decisions, "prediction_time"),
            },
            "prices": {
                **price_source,
                "sha256": _rows_sha256(prices),
                "source_sha256": _audit_digest(price_audit),
                "physical_rows": _audit_count(price_audit, "physical_rows"),
                "parsed_object_rows": _audit_count(price_audit, "parsed_object_rows"),
                "blank_rows": _audit_count(price_audit, "blank_rows"),
                "raw_rows": len(raw_prices),
                "eligible_rows": len(prices),
                "max_available_time": _max_timestamp(prices, "available_time"),
            },
        }

    # Derive every member before publishing any one of them. During a successful
    # publication, briefing holds the shared side of this lock while loading all
    # three caches and the manifest, so it observes one complete generation.
    # A process crash between file replacements is detected by the existing
    # generation/hash contract and fails closed until the next successful run.
    with ExitStack() as bundle_lock:
        with timing.stage("timeframe_learning_bundle_lock_wait"):
            bundle_lock.enter_context(
                advisory_file_lock(
                    _bundle_lock_path(monitor_path),
                    exclusive=True,
                    timeout_seconds=BUNDLE_LOCK_TIMEOUT_SECONDS,
                )
            )
        with timing.stage("timeframe_learning_previous_generation_check"):
            _assert_not_source_rollback(
                monitor_path,
                generated_at=generated_at,
                input_watermarks=input_watermarks,
                cross_log_commit_snapshot=decision_full_source,
            )
        with timing.stage("timeframe_learning_bundle_publish"):
            tf_learning.save_timeframe_learning(direction, tf_learning_path)
            tp_sl_learning.save_timeframe_tp_sl_learning(tp_sl, tp_sl_learning_path)
            maximization.save_timeframe_maximization(maximum, maximization_path)

        with timing.stage("timeframe_learning_manifest_publish"):
            payload: dict[str, object] = {
                "schema": 1,
                "monitor_contract": MONITOR_CONTRACT,
                "generated_at": generated_at.isoformat(),
                "status": "ok",
                "decision_rows": len(decisions),
                "pit_eligible_decision_rows": len(pit_decisions),
                "price_rows": len(raw_prices),
                "pit_eligible_price_rows": len(prices),
                "excluded_future_or_invalid_price_rows": len(raw_prices) - len(prices),
                "direction_profiles": len(direction.profiles),
                "tp_sl_profiles": len(tp_sl.profiles),
                "maximization_cells": len(maximum.cells),
                "input_watermarks": input_watermarks,
                "cache_artifacts": {
                    "timeframe_direction": _artifact(tf_learning_path),
                    "timeframe_tp_sl": _artifact(tp_sl_learning_path),
                    "timeframe_maximization": _artifact(maximization_path),
                },
                "stage_timing": stage_timing.summarize(timing.records),
                "cross_log_commit_snapshot": decision_full_source,
            }
            atomic_write_json(monitor_path, payload)
    return payload


def _bundle_lock_path(manifest_path: Path) -> Path:
    resolved = manifest_path.resolve()
    return resolved.with_suffix(f"{resolved.suffix}.lock")


def _validate_output_bundle(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path, Path, Path, Path, Path | None]:
    """Return stable output targets and keep canonical lock identity inseparable."""

    outputs = (
        Path(args.tf_learning).resolve(),
        Path(args.tp_sl_learning).resolve(),
        Path(args.maximization).resolve(),
        Path(args.monitor_json).resolve(),
    )
    canonical = (
        DEFAULT_TF_LEARNING.resolve(),
        DEFAULT_TP_SL_LEARNING.resolve(),
        DEFAULT_MAXIMIZATION.resolve(),
        DEFAULT_MONITOR.resolve(),
    )
    touches_canonical = any(
        _paths_alias(output, canonical_path) for output in outputs for canonical_path in canonical
    )
    if touches_canonical and outputs != canonical:
        raise ValueError(
            "canonical timeframe learning cache outputs and monitor must be used together"
        )
    if any(
        _paths_alias(path, prior) for index, path in enumerate(outputs) for prior in outputs[:index]
    ):
        raise ValueError("timeframe learning bundle outputs must be distinct")
    sources = (Path(args.journal).resolve(), Path(args.prices).resolve())
    canonical_sources = (DEFAULT_JOURNAL.resolve(), DEFAULT_PRICES.resolve())
    if outputs == canonical and not all(
        _paths_alias(source, expected)
        for source, expected in zip(sources, canonical_sources, strict=True)
    ):
        raise ValueError("canonical learning bundle requires canonical journal and price inputs")
    if _paths_alias(*sources):
        raise ValueError("timeframe learning decision and price sources must be distinct")
    if any(_paths_alias(output, source) for output in outputs for source in sources):
        raise ValueError("timeframe learning bundle outputs must not alias an input source")
    lock_path = _bundle_lock_path(outputs[3]).resolve()
    if any(_paths_alias(lock_path, path) for path in (*outputs, *sources)):
        raise ValueError("timeframe learning bundle lock must not alias data or output")
    timing_path = (
        Path(args.stage_timing_log).resolve() if args.stage_timing_log is not None else None
    )
    if timing_path is not None and any(
        _paths_alias(timing_path, path) for path in (*outputs, lock_path, *sources)
    ):
        raise ValueError("stage timing log must not alias learning data, output, or lock")
    return (*sources, *outputs, timing_path)


def _paths_alias(left: Path, right: Path) -> bool:
    if _normalized_path_key(left) == _normalized_path_key(right):
        return True
    try:
        return left.samefile(right)
    except OSError:
        return False


def _normalized_path_key(path: Path) -> str:
    # APFS deployments may be both case- and Unicode-normalization-insensitive.
    # Conservatively reserve case/normalization variants on every platform so a
    # not-yet-created output cannot later replace the held lock pathname.
    return unicodedata.normalize("NFC", str(path)).casefold()


def _assert_not_source_rollback(
    manifest_path: Path,
    *,
    generated_at: datetime,
    input_watermarks: dict[str, dict[str, object]],
    cross_log_commit_snapshot: dict[str, object] | None,
) -> None:
    """Reject a fresh-looking generation built from truncated/replaced inputs."""

    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if (
        not isinstance(previous, dict)
        or previous.get("monitor_contract") != MONITOR_CONTRACT
        or previous.get("status") != "ok"
    ):
        return
    previous_generated_at = _aware_timestamp(previous.get("generated_at"))
    if previous_generated_at is not None and generated_at <= previous_generated_at:
        raise RuntimeError("timeframe learning generation time moved backwards")
    previous_watermarks = previous.get("input_watermarks")
    if not isinstance(previous_watermarks, dict):
        return
    for scope in ("decisions", "prices"):
        old = previous_watermarks.get(scope)
        new = input_watermarks.get(scope)
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        if old.get("source_path") != new.get("source_path"):
            raise RuntimeError(f"{scope} learning source path changed")
        for key in ("device", "inode"):
            if type(old.get(key)) is int and old.get(key) != new.get(key):  # noqa: E721
                raise RuntimeError(f"{scope} learning source identity changed")
        for key in (
            "read_offset_bytes",
            "mtime_ns",
            "physical_rows",
            "parsed_object_rows",
            "raw_rows",
            "eligible_rows",
        ):
            old_value = old.get(key)
            new_value = new.get(key)
            if (
                type(old_value) is int  # noqa: E721
                and type(new_value) is int  # noqa: E721
                and new_value < old_value
            ):
                raise RuntimeError(f"{scope} learning source regressed at {key}")
        if old.get("read_offset_bytes") == new.get("read_offset_bytes"):
            old_source_hash = old.get("source_sha256")
            if isinstance(old_source_hash, str) and old_source_hash != new.get("source_sha256"):
                raise RuntimeError(f"{scope} learning source changed without append")
        if old.get("eligible_rows") == new.get("eligible_rows"):
            old_eligible_hash = old.get("sha256")
            if isinstance(old_eligible_hash, str) and old_eligible_hash != new.get("sha256"):
                raise RuntimeError(f"{scope} eligible learning population changed")
    previous_commit_source = previous.get("cross_log_commit_snapshot")
    if isinstance(previous_commit_source, dict) and cross_log_commit_snapshot is not None:
        if previous_commit_source.get("source_path") != cross_log_commit_snapshot.get(
            "source_path"
        ):
            raise RuntimeError("decision cross-log commit source path changed")
        for key in ("device", "inode"):
            if type(
                previous_commit_source.get(key)
            ) is int and previous_commit_source.get(  # noqa: E721
                key
            ) != cross_log_commit_snapshot.get(
                key
            ):
                raise RuntimeError("decision cross-log commit source identity changed")
        for key in ("read_offset_bytes", "mtime_ns"):
            old_value = previous_commit_source.get(key)
            new_value = cross_log_commit_snapshot.get(key)
            if (
                type(old_value) is int  # noqa: E721
                and type(new_value) is int  # noqa: E721
                and new_value < old_value
            ):
                raise RuntimeError(f"decision cross-log commit source regressed at {key}")


def _aware_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _not_future(value: object, now: datetime) -> bool:
    parsed = _aware_timestamp(value)
    return parsed is not None and parsed <= now


def _rows_sha256(rows: Sequence[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _audit_count(audit: dict[str, object], key: str) -> int:
    value = audit.get(key)
    if type(value) is not int:  # noqa: E721
        raise RuntimeError(f"missing strict source audit counter: {key}")
    return value


def _audit_digest(audit: dict[str, object]) -> str:
    value = audit.get("source_sha256")
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimeError("missing strict source audit digest")
    return value


def _max_timestamp(rows: Sequence[dict[str, object]], key: str) -> str | None:
    values: list[datetime] = []
    for row in rows:
        try:
            parsed = datetime.fromisoformat(str(row.get(key)).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            values.append(parsed.astimezone(UTC))
    return max(values).isoformat() if values else None


def _artifact(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _source_identity(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "source_path": str(path.resolve()),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "read_offset_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _source_snapshot(
    path: Path,
    *,
    previous_source: dict[str, object] | None = None,
    previous_full_source: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Pin complete append-only source boundaries under the writers' file locks."""

    source = path.resolve()
    full_source = decision_commit.commit_path_for(source).resolve()
    candidates = sorted({source, full_source})
    handles = []
    try:
        deadline = time.monotonic() + SOURCE_LOCK_TIMEOUT_SECONDS
        for candidate in candidates:
            handle = candidate.open("rb")
            try:
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                f"timed out waiting for learning source lock: {candidate}"
                            )
                        time.sleep(min(0.01, remaining))
            except BaseException:
                handle.close()
                raise
            handles.append((candidate, handle))
        identities = {
            candidate: _handle_identity(candidate, handle.fileno()) for candidate, handle in handles
        }
        for candidate, handle in handles:
            boundary = int(identities[candidate]["read_offset_bytes"])
            if boundary:
                handle.seek(boundary - 1)
                if handle.read(1) != b"\n":
                    raise journal.JournalIntegrityError(
                        f"incomplete JSONL snapshot boundary at {candidate}"
                    )
        for candidate, handle in handles:
            previous = previous_source if candidate == source else previous_full_source
            previous_end_offset = _evidence_offset(previous)
            snapshot_sha256, previous_prefix_sha256 = _handle_prefix_hashes(
                handle.fileno(),
                int(identities[candidate]["read_offset_bytes"]),
                previous_end_offset=previous_end_offset,
            )
            identities[candidate]["source_sha256"] = snapshot_sha256
            if previous_end_offset is not None:
                identities[candidate]["_previous_prefix_sha256"] = previous_prefix_sha256
        return identities[source], identities.get(full_source) if full_source != source else None
    finally:
        for _candidate, handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _handle_identity(path: Path, file_descriptor: int) -> dict[str, object]:
    stat = os.fstat(file_descriptor)
    return {
        "source_path": str(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "read_offset_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _handle_prefix_hashes(
    file_descriptor: int,
    end_offset: int,
    *,
    previous_end_offset: int | None,
) -> tuple[str, str | None]:
    if previous_end_offset is not None and previous_end_offset > end_offset:
        raise RuntimeError("learning source regressed before previous snapshot boundary")
    snapshot_digest = hashlib.sha256()
    previous_digest = hashlib.sha256() if previous_end_offset is not None else None
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    remaining = end_offset
    position = 0
    while remaining:
        chunk = os.read(file_descriptor, min(1024 * 1024, remaining))
        if not chunk:
            raise journal.JournalIntegrityError("source is shorter than snapshot boundary")
        snapshot_digest.update(chunk)
        if previous_digest is not None and position < int(previous_end_offset):
            previous_digest.update(chunk[: int(previous_end_offset) - position])
        position += len(chunk)
        remaining -= len(chunk)
    return (
        snapshot_digest.hexdigest(),
        previous_digest.hexdigest() if previous_digest is not None else None,
    )


def _assert_snapshot_prefix_available(
    path: Path,
    snapshot: dict[str, object],
    *,
    observed_sha256: str | None = None,
) -> None:
    current = _source_identity(path.resolve())
    if current["source_path"] != snapshot["source_path"]:
        raise RuntimeError("learning source path changed during snapshot read")
    if any(current[key] != snapshot[key] for key in ("device", "inode")):
        raise RuntimeError("learning source identity changed during snapshot read")
    if int(current["read_offset_bytes"]) < int(snapshot["read_offset_bytes"]):
        raise RuntimeError("learning source truncated during snapshot read")
    expected_sha256 = snapshot.get("source_sha256")
    if observed_sha256 is not None and observed_sha256 != expected_sha256:
        raise RuntimeError("learning source prefix changed during snapshot read")


def _previous_source_evidence(manifest_path: Path) -> dict[str, dict[str, object]]:
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(previous, dict)
        or previous.get("monitor_contract") != MONITOR_CONTRACT
        or previous.get("status") != "ok"
    ):
        return {}
    output: dict[str, dict[str, object]] = {}
    watermarks = previous.get("input_watermarks")
    if isinstance(watermarks, dict):
        for scope in ("decisions", "prices"):
            evidence = watermarks.get(scope)
            if isinstance(evidence, dict):
                output[scope] = evidence
    commit_source = previous.get("cross_log_commit_snapshot")
    if isinstance(commit_source, dict):
        output["cross_log_commit"] = commit_source
    return output


def _evidence_offset(evidence: dict[str, object] | None) -> int | None:
    if not isinstance(evidence, dict):
        return None
    value = evidence.get("read_offset_bytes")
    return value if type(value) is int and value >= 0 else None  # noqa: E721


def _assert_previous_prefix_unchanged(
    snapshot: dict[str, object],
    previous: dict[str, object] | None,
    *,
    label: str,
) -> None:
    if previous is None:
        return
    expected = previous.get("source_sha256")
    observed = snapshot.get("_previous_prefix_sha256")
    if isinstance(expected, str) and len(expected) == 64 and observed != expected:
        raise RuntimeError(f"{label} previous prefix changed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="時間足別の全履歴学習cacheをdecision critical path外で更新する"
    )
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--prices", type=Path, default=DEFAULT_PRICES)
    parser.add_argument("--tf-learning", type=Path, default=DEFAULT_TF_LEARNING)
    parser.add_argument("--tp-sl-learning", type=Path, default=DEFAULT_TP_SL_LEARNING)
    parser.add_argument("--maximization", type=Path, default=DEFAULT_MAXIMIZATION)
    parser.add_argument("--monitor-json", type=Path, default=DEFAULT_MONITOR)
    parser.add_argument("--stage-timing-log", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(args)
    if not args.quiet:
        print(
            "timeframe learning monitor: "
            f"decisions={result['decision_rows']} prices={result['price_rows']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
