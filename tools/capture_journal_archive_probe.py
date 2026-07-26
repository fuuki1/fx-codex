#!/usr/bin/env python3
"""Exercise create-only capture-journal sealing and full-prefix restore."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.capture_journal import CaptureJournal  # noqa: E402
from data_platform.collect.contract import CollectedQuote  # noqa: E402
from data_platform.collect.journal_archive import (  # noqa: E402
    restore_archive_set,
    seal_historical_shard,
    verify_archive,
)
from data_platform.collect.raw_first import QuoteLog, ingest_payload  # noqa: E402

PROBE_START = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _ingest(log: QuoteLog, *, when: datetime, sequence: int) -> None:
    payload = json.dumps(
        {
            "type": "PRICE",
            "time": when.isoformat(),
            "instrument": "USD_JPY",
            "bids": [{"price": f"{155 + sequence / 100:.5f}", "liquidity": 1_000_000}],
            "asks": [{"price": f"{155.01 + sequence / 100:.5f}", "liquidity": 1_000_000}],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")

    def parser(raw: bytes) -> list[CollectedQuote]:
        return [
            CollectedQuote(
                provider="oanda",
                account_environment="practice",
                instrument="USDJPY",
                provider_event_time=when,
                received_at=when,
                bid=155 + sequence / 100,
                ask=155.01 + sequence / 100,
                bid_size=1_000_000,
                ask_size=1_000_000,
                tradable=False,
                sequence_id=sequence,
                connection_id="archive-probe",
                writer_id="archive-probe",
                revision_id=None,
                raw_payload_sha256=hashlib.sha256(raw).hexdigest(),
                source_endpoint_class="replay_fixture",
                collection_mode="replay",
            )
        ]

    result = ingest_payload(payload, parser=parser, log=log, now=lambda: when)
    if result.accepted_count != 1:
        raise RuntimeError(f"probe message {sequence} was not accepted")


def run_probe(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("archive probe output root must be empty")
    log = QuoteLog(root / "source" / "log", legacy_jsonl=False)
    for offset in range(3):
        _ingest(
            log,
            when=PROBE_START + timedelta(days=offset),
            sequence=offset + 1,
        )
    log.journal.checkpoint()
    source_identity = log.journal.identity()
    source_paths = log.journal.shard_paths()
    source_hashes_before = {path.name: _sha256_file(path) for path in source_paths}

    archive_root = root / "archive"
    sealed = [
        seal_historical_shard(
            log.journal.root,
            archive_root,
            shard_date=day,
            sealed_at=PROBE_START + timedelta(days=3),
        )
        for day in ("2026-01-05", "2026-01-06")
    ]
    verified = [verify_archive(item.manifest_path) for item in sealed]
    source_hashes_after = {path.name: _sha256_file(path) for path in source_paths}

    restored_root = root / "restored"
    restore = restore_archive_set(
        [item.manifest_path for item in reversed(sealed)],
        restored_root,
    )
    source_quotes = log.journal.read_quotes()
    restored_quotes = CaptureJournal(restored_root).read_quotes()
    source_payloads = [row.quote.to_dict() for row in source_quotes[:2]]
    restored_payloads = [row.quote.to_dict() for row in restored_quotes]
    manifest_hashes = {item.manifest_path.name: _sha256_file(item.manifest_path) for item in sealed}
    passed = (
        len(verified) == 2
        and source_hashes_before == source_hashes_after
        and all(path.is_file() for path in source_paths)
        and log.journal.identity() == source_identity
        and restore.shard_dates == ("2026-01-05", "2026-01-06")
        and restore.head_sequence == 4
        and source_payloads == restored_payloads
    )
    return {
        "schema_version": 1,
        "evidence_class": "synthetic_archive_restore_probe",
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": passed,
        "source_shards": len(source_paths),
        "source_shards_preserved": source_hashes_before == source_hashes_after,
        "sealed_shards": len(sealed),
        "verified_archives": len(verified),
        "restored_shard_dates": list(restore.shard_dates),
        "restored_head_sequence": restore.head_sequence,
        "restored_quote_count": len(restored_quotes),
        "restored_quotes_match_source_prefix": source_payloads == restored_payloads,
        "manifest_sha256": manifest_hashes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.report is not None and args.report.exists():
        parser.error(f"report is create-only and already exists: {args.report}")

    if args.output_root is None:
        with tempfile.TemporaryDirectory(prefix="fx-capture-journal-archive-probe-") as temporary:
            report = run_probe(Path(temporary))
    else:
        report = run_probe(args.output_root)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
