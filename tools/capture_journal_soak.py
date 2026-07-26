#!/usr/bin/env python3
"""Synthetic capacity probe for the canonical daily capture journal.

This is functional/capacity evidence only.  It does not support an FX
performance claim and does not contact a broker or market-data endpoint.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.oanda import parse_price_line  # noqa: E402
from data_platform.collect.raw_first import QuoteLog, ingest_payload  # noqa: E402

DOCUMENTED_FOUR_PAIR_MESSAGES_PER_SECOND = 16
SECONDS_PER_DAY = 86_400
FULL_DAY_MESSAGES = DOCUMENTED_FOUR_PAIR_MESSAGES_PER_SECOND * SECONDS_PER_DAY
FULL_DAY_MAX_EXPECTED_BYTES = 6 * 1024 * 1024 * 1024
DEFAULT_MIN_FREE_AFTER_BYTES = 10 * 1024 * 1024 * 1024
INSTRUMENTS = ("USD_JPY", "EUR_USD", "GBP_USD", "AUD_USD")


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def _repository_evidence() -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None, None
    return revision, bool(status)


def _representative_payload(index: int, observed: datetime) -> bytes:
    instrument = INSTRUMENTS[index % len(INSTRUMENTS)]
    base = {
        "USD_JPY": 155.0,
        "EUR_USD": 1.17,
        "GBP_USD": 1.35,
        "AUD_USD": 0.66,
    }[instrument]
    bump = (index % 10_000) / 100_000_000
    bid = base + bump
    ask = bid + (0.003 if instrument == "USD_JPY" else 0.00003)
    payload = {
        "type": "PRICE",
        "time": observed.isoformat(),
        "instrument": instrument,
        "tradeable": True,
        "status": "tradeable",
        "bids": [{"price": f"{bid:.8f}", "liquidity": 1_000_000}],
        "asks": [{"price": f"{ask:.8f}", "liquidity": 1_000_000}],
        "closeoutBid": f"{bid - 0.00001:.8f}",
        "closeoutAsk": f"{ask + 0.00001:.8f}",
        "quoteHomeConversionFactors": {
            "positiveUnits": "1.00000000",
            "negativeUnits": "1.00000000",
        },
        "unitsAvailable": {
            "default": {"long": "1000000", "short": "1000000"},
            "openOnly": {"long": "1000000", "short": "1000000"},
            "reduceFirst": {"long": "1000000", "short": "1000000"},
            "reduceOnly": {"long": "1000000", "short": "1000000"},
        },
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")


def _rss_bytes() -> int:
    observed = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(observed if sys.platform == "darwin" else observed * 1024)


def run_soak(
    root: Path,
    *,
    messages: int,
    message_rate: int = DOCUMENTED_FOUR_PAIR_MESSAGES_PER_SECOND,
    progress_every: int = 0,
    progress: Callable[[int, int, float], None] | None = None,
) -> dict[str, Any]:
    if messages < 1:
        raise ValueError("messages must be positive")
    if message_rate < 1:
        raise ValueError("message_rate must be positive")
    if progress_every < 0:
        raise ValueError("progress_every must be non-negative")
    existing = tuple((root / "log" / "capture_journal").glob("capture-*.sqlite3"))
    if existing:
        raise ValueError("soak output root already contains journal shards")
    log = QuoteLog(root / "log", legacy_jsonl=False, verify_journal=False)
    base = datetime(2026, 1, 5, tzinfo=UTC)
    latency_sample_ms: list[float] = []
    latency_sample_stride = max(1, math.ceil(messages / 100_000))
    latency_total_ms = 0.0
    latency_max_ms = 0.0
    payload_total_bytes = 0
    payload_min_bytes: int | None = None
    payload_max_bytes = 0
    free_bytes_before = shutil.disk_usage(root).free
    cpu_started = time.process_time()
    started = time.perf_counter()
    for index in range(messages):
        observed = base + timedelta(microseconds=index * 1_000_000 // message_rate)
        payload = _representative_payload(index, observed)
        payload_size = len(payload)
        payload_total_bytes += payload_size
        payload_min_bytes = (
            payload_size if payload_min_bytes is None else min(payload_min_bytes, payload_size)
        )
        payload_max_bytes = max(payload_max_bytes, payload_size)

        def parser(raw: bytes, *, stamp: datetime = observed):
            return parse_price_line(
                raw,
                environment="practice",
                connection_id="synthetic-soak",
                tradable_allowed=False,
                received_at=stamp,
                source_endpoint_class="replay_fixture",
                collection_mode="replay",
            )

        message_started = time.perf_counter()
        result = ingest_payload(
            payload,
            parser=parser,
            log=log,
            now=lambda: observed,
        )
        if result.accepted_count != 1:
            raise RuntimeError(f"synthetic message {index} was not accepted")
        latency_ms = (time.perf_counter() - message_started) * 1_000
        latency_total_ms += latency_ms
        latency_max_ms = max(latency_max_ms, latency_ms)
        if index % latency_sample_stride == 0:
            latency_sample_ms.append(latency_ms)
        completed = index + 1
        if progress is not None and progress_every and completed % progress_every == 0:
            progress(completed, messages, time.perf_counter() - started)
    ingest_seconds = time.perf_counter() - started
    cpu_seconds = time.process_time() - cpu_started
    logical_end = base + timedelta(microseconds=(messages - 1) * 1_000_000 // message_rate)
    log.journal.checkpoint()
    main_window_files = [
        path for path in (root / "log" / "capture_journal").iterdir() if path.is_file()
    ]
    main_window_bytes = sum(path.stat().st_size for path in main_window_files)
    main_window_shards = len(log.journal.shard_paths())

    # Keep the recovery boundary in the final logical shard so the capacity
    # probe does not silently add a second day outside its stated window.
    crash_time = logical_end
    incomplete = log.journal.begin(
        b"synthetic-crash-boundary",
        occurred_at=crash_time,
    )
    visible_before_recovery = len(
        log.journal.read_quotes(
            start_sequence=incomplete.raw_sequence,
            expected_entry_sha256=incomplete.raw_entry_sha256,
        )
    )
    recovered = log.journal.recover_incomplete(
        occurred_at=crash_time,
    )
    log.journal.checkpoint()
    replay_started = time.perf_counter()
    replayed_count = log.journal.verified_quote_count()
    replay_seconds = time.perf_counter() - replay_started
    files = [path for path in (root / "log" / "capture_journal").iterdir() if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files)
    journal_shards = len(log.journal.shard_paths())
    throughput = messages / ingest_seconds
    bytes_per_message = total_bytes / messages
    repository_revision, worktree_dirty = _repository_evidence()
    free_bytes_after = shutil.disk_usage(root).free
    return {
        "schema_version": 1,
        "evidence_class": "synthetic_functional_capacity_probe",
        "payload_profile": "representative_oanda_price_json_not_provider_captured",
        "parser_profile": "production_parse_price_line_with_replay_provenance",
        "logical_window_start": base.isoformat(),
        "logical_window_end": logical_end.isoformat(),
        "messages": messages,
        "configured_messages_per_second": message_rate,
        "ingest_seconds": round(ingest_seconds, 6),
        "process_cpu_seconds": round(cpu_seconds, 6),
        "max_rss_bytes": _rss_bytes(),
        "throughput_messages_per_second": round(throughput, 3),
        "documented_four_pair_messages_per_second": (DOCUMENTED_FOUR_PAIR_MESSAGES_PER_SECOND),
        "throughput_multiple_of_documented_rate": round(
            throughput / DOCUMENTED_FOUR_PAIR_MESSAGES_PER_SECOND,
            3,
        ),
        "latency_ms": {
            "mean": round(latency_total_ms / messages, 3),
            "p50_sampled": round(_percentile(latency_sample_ms, 0.50), 3),
            "p95_sampled": round(_percentile(latency_sample_ms, 0.95), 3),
            "p99_sampled": round(_percentile(latency_sample_ms, 0.99), 3),
            "max": round(latency_max_ms, 3),
            "sample_size": len(latency_sample_ms),
            "sample_stride": latency_sample_stride,
        },
        "raw_payload_bytes": {
            "mean": round(payload_total_bytes / messages, 3),
            "min": payload_min_bytes,
            "max": payload_max_bytes,
        },
        "journal_file_count": len(files),
        "journal_shard_count": journal_shards,
        "bounded_file_layout": len(files) <= journal_shards * 3,
        "journal_bytes": total_bytes,
        "main_window_journal_file_count": len(main_window_files),
        "main_window_journal_shard_count": main_window_shards,
        "main_window_journal_bytes": main_window_bytes,
        "free_bytes_before": free_bytes_before,
        "free_bytes_after": free_bytes_after,
        "disk_bytes_consumed": max(0, free_bytes_before - free_bytes_after),
        "bytes_per_message": round(bytes_per_message, 3),
        "linear_estimated_bytes_at_documented_daily_max": round(
            bytes_per_message * DOCUMENTED_FOUR_PAIR_MESSAGES_PER_SECOND * SECONDS_PER_DAY
        ),
        "replay_seconds": round(replay_seconds, 6),
        "replayed_quotes": replayed_count,
        "incomplete_visible_before_recovery": visible_before_recovery,
        "recovered_capture_id": incomplete.capture_id,
        "recovery_appended_unavailable": recovered == (incomplete.capture_id,),
        "integrity_verified_after_recovery": replayed_count == messages,
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "repository_revision": repository_revision,
        "worktree_dirty": worktree_dirty,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    volume = parser.add_mutually_exclusive_group()
    volume.add_argument("--messages", type=int)
    volume.add_argument(
        "--full-day",
        action="store_true",
        help=f"run {FULL_DAY_MESSAGES} messages (16/s for one logical UTC day)",
    )
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--progress-every", type=int, default=10_000)
    parser.add_argument("--min-free-after-gib", type=float, default=10.0)
    args = parser.parse_args(argv)
    messages = FULL_DAY_MESSAGES if args.full_day else (args.messages or 10_000)

    if args.report is not None and args.report.exists():
        parser.error(f"report is create-only and already exists: {args.report}")
    if args.min_free_after_gib < 0:
        parser.error("--min-free-after-gib must be non-negative")

    if args.output_root is None:
        with tempfile.TemporaryDirectory(prefix="fx-capture-journal-soak-") as temporary:
            output_root = Path(temporary)
            if args.full_day:
                required = FULL_DAY_MAX_EXPECTED_BYTES + round(args.min_free_after_gib * 1024**3)
                available = shutil.disk_usage(output_root).free
                if available < required:
                    parser.error(
                        "insufficient disk headroom for full-day soak: "
                        f"available={available}, required={required}"
                    )
            report = run_soak(
                output_root,
                messages=messages,
                progress_every=args.progress_every,
                progress=lambda completed, total, elapsed: print(
                    f"progress {completed}/{total} elapsed_seconds={elapsed:.1f}",
                    file=sys.stderr,
                    flush=True,
                ),
            )
    else:
        args.output_root.mkdir(parents=True, exist_ok=True)
        if args.full_day:
            required = FULL_DAY_MAX_EXPECTED_BYTES + round(args.min_free_after_gib * 1024**3)
            available = shutil.disk_usage(args.output_root).free
            if available < required:
                parser.error(
                    "insufficient disk headroom for full-day soak: "
                    f"available={available}, required={required}"
                )
        report = run_soak(
            args.output_root,
            messages=messages,
            progress_every=args.progress_every,
            progress=lambda completed, total, elapsed: print(
                f"progress {completed}/{total} elapsed_seconds={elapsed:.1f}",
                file=sys.stderr,
                flush=True,
            ),
        )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            args.report,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o440,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
