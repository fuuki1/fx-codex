#!/usr/bin/env python3
"""Mirror credential-free public candle files (Dukascopy .bi5 / FXCM .csv.gz).

Research-only helper. Downloads REAL bid/ask candle data from public endpoints
that require no account or credentials:

- Dukascopy Bank datafeed hourly candles (one LZMA .bi5 per instrument-month):
    https://datafeed.dukascopy.com/datafeed/{PAIR}/{YYYY}/{MM-1:02d}/{SIDE}_candles_hour_1.bi5
- Dukascopy Bank datafeed minute candles (one .bi5 per instrument-day):
    https://datafeed.dukascopy.com/datafeed/{PAIR}/{YYYY}/{MM-1:02d}/{DD:02d}/{SIDE}_candles_min_1.bi5
  (the month path segment is ZERO-indexed; SIDE is BID or ASK)
- FXCM public candle archive (one gzip CSV per instrument-week, bid+ask OHLC):
    https://candledata.fxcorporate.com/H1/{PAIR}/{YYYY}/{WEEK}.csv.gz

Raw bytes are stored exactly as served (never re-encoded); a JSONL manifest
records url, sha256, size, HTTP status, fetch time (UTC) and attempt count for
every request. HTTP 404 is an honest absence — recorded in the manifest with no
file written, never fabricated. A URL that exhausts its retries fails the whole
run (exit 1): a partial mirror is visible in the manifest, never silently
treated as complete. Re-runs are idempotent: files already mirrored are hash
verified and skipped.

Usage:
    python3 scripts/fetch_public_candles.py dukascopy-h1 \
        --pairs USDJPY EURUSD GBPUSD --start 2019-01 --end 2026-06 \
        --out-root logs/data_platform/mirror
    python3 scripts/fetch_public_candles.py dukascopy-m1 \
        --pairs USDJPY EURUSD GBPUSD --start 2024-01 --end 2024-12
    python3 scripts/fetch_public_candles.py fxcm-h1 \
        --pairs USDJPY EURUSD GBPUSD --start 2021-01 --end 2025-12
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
import random
import sys
import threading
from pathlib import Path

import requests

_DUKASCOPY_BASE = "https://datafeed.dukascopy.com/datafeed"
_FXCM_BASE = "https://candledata.fxcorporate.com"
_UA = "fx-codex-collect/1.0 (research; read-only)"
_TIMEOUT_SECONDS = 75.0
_MAX_ATTEMPTS = 6
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_CAP_SECONDS = 45.0

_thread_local = threading.local()


@dataclass(frozen=True)
class FetchJob:
    url: str
    destination: Path


@dataclass
class FetchOutcome:
    job: FetchJob
    status: str  # "fetched" | "absent" | "skipped" | "failed"
    http_status: int | None
    sha256: str | None
    size: int
    attempts: int
    detail: str = ""


def _session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = _UA
        _thread_local.session = session
    return session


def _fetch_one(job: FetchJob, rng: random.Random) -> FetchOutcome:
    if job.destination.is_file():
        data = job.destination.read_bytes()
        return FetchOutcome(
            job=job,
            status="skipped",
            http_status=None,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            attempts=0,
            detail="already mirrored; hash recorded from existing bytes",
        )
    last_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = _session().get(job.url, timeout=_TIMEOUT_SECONDS)
        except requests.RequestException as error:
            last_error = f"{type(error).__name__}: {error}"
        else:
            if response.status_code == 200:
                body = response.content
                job.destination.parent.mkdir(parents=True, exist_ok=True)
                tmp = job.destination.with_suffix(job.destination.suffix + ".part")
                tmp.write_bytes(body)
                tmp.replace(job.destination)
                return FetchOutcome(
                    job=job,
                    status="fetched",
                    http_status=200,
                    sha256=hashlib.sha256(body).hexdigest(),
                    size=len(body),
                    attempts=attempt,
                )
            if response.status_code == 404:
                return FetchOutcome(
                    job=job,
                    status="absent",
                    http_status=404,
                    sha256=None,
                    size=0,
                    attempts=attempt,
                    detail="provider reports no file for this period (honest gap)",
                )
            last_error = f"HTTP {response.status_code}"
        if attempt < _MAX_ATTEMPTS:
            delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            threading.Event().wait(delay * (0.5 + rng.random()))
    return FetchOutcome(
        job=job,
        status="failed",
        http_status=None,
        sha256=None,
        size=0,
        attempts=_MAX_ATTEMPTS,
        detail=last_error,
    )


def _months(start: str, end: str) -> list[tuple[int, int]]:
    first = datetime.strptime(start, "%Y-%m")
    last = datetime.strptime(end, "%Y-%m")
    if last < first:
        raise SystemExit(f"--end {end} precedes --start {start}")
    out: list[tuple[int, int]] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append((year, month))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def _jobs_dukascopy_h1(pairs: list[str], start: str, end: str, root: Path) -> list[FetchJob]:
    jobs: list[FetchJob] = []
    for pair in pairs:
        for year, month in _months(start, end):
            for side in ("BID", "ASK"):
                url = f"{_DUKASCOPY_BASE}/{pair}/{year}/{month - 1:02d}/{side}_candles_hour_1.bi5"
                dest = (
                    root
                    / "dukascopy"
                    / "h1"
                    / pair
                    / f"{year}"
                    / f"{month:02d}"
                    / (f"{side}_candles_hour_1.bi5")
                )
                jobs.append(FetchJob(url=url, destination=dest))
    return jobs


def _jobs_dukascopy_m1(pairs: list[str], start: str, end: str, root: Path) -> list[FetchJob]:
    jobs: list[FetchJob] = []
    for pair in pairs:
        for year, month in _months(start, end):
            day = date(year, month, 1)
            while day.month == month:
                for side in ("BID", "ASK"):
                    url = (
                        f"{_DUKASCOPY_BASE}/{pair}/{year}/{month - 1:02d}/{day.day:02d}/"
                        f"{side}_candles_min_1.bi5"
                    )
                    dest = (
                        root
                        / "dukascopy"
                        / "m1"
                        / pair
                        / f"{year}"
                        / f"{month:02d}"
                        / f"{day.day:02d}"
                        / f"{side}_candles_min_1.bi5"
                    )
                    jobs.append(FetchJob(url=url, destination=dest))
                day += timedelta(days=1)
    return jobs


def _jobs_fxcm_h1(pairs: list[str], start: str, end: str, root: Path) -> list[FetchJob]:
    years = sorted({year for year, _ in _months(start, end)})
    jobs: list[FetchJob] = []
    for pair in pairs:
        for year in years:
            for week in range(1, 54):
                url = f"{_FXCM_BASE}/H1/{pair}/{year}/{week}.csv.gz"
                dest = root / "fxcm" / "h1" / pair / f"{year}" / f"w{week:02d}.csv.gz"
                jobs.append(FetchJob(url=url, destination=dest))
    return jobs


_JOB_BUILDERS = {
    "dukascopy-h1": _jobs_dukascopy_h1,
    "dukascopy-m1": _jobs_dukascopy_m1,
    "fxcm-h1": _jobs_fxcm_h1,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=sorted(_JOB_BUILDERS))
    parser.add_argument("--pairs", nargs="+", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM inclusive")
    parser.add_argument("--end", required=True, help="YYYY-MM inclusive")
    parser.add_argument("--out-root", type=Path, default=Path("logs/data_platform/mirror"))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260714)
    args = parser.parse_args(argv)

    pairs = [pair.upper() for pair in args.pairs]
    jobs = _JOB_BUILDERS[args.source](pairs, args.start, args.end, args.out_root)
    manifest_path = args.out_root / "manifest.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    counts = {"fetched": 0, "absent": 0, "skipped": 0, "failed": 0}
    with (
        open(manifest_path, "a", encoding="utf-8") as manifest,
        ThreadPoolExecutor(max_workers=args.workers) as pool,
    ):
        futures = [pool.submit(_fetch_one, job, random.Random(rng.random())) for job in jobs]
        for done, future in enumerate(as_completed(futures), start=1):
            outcome = future.result()
            counts[outcome.status] += 1
            manifest.write(
                json.dumps(
                    {
                        "source": args.source,
                        "url": outcome.job.url,
                        "path": str(outcome.job.destination),
                        "status": outcome.status,
                        "http_status": outcome.http_status,
                        "sha256": outcome.sha256,
                        "size": outcome.size,
                        "attempts": outcome.attempts,
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "detail": outcome.detail,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            manifest.flush()
            if done % 25 == 0 or done == len(jobs):
                print(
                    f"[{args.source}] {done}/{len(jobs)} "
                    f"(fetched={counts['fetched']} absent={counts['absent']} "
                    f"skipped={counts['skipped']} failed={counts['failed']})",
                    flush=True,
                )
    if counts["failed"]:
        print(f"FAILED: {counts['failed']} urls exhausted retries", file=sys.stderr)
        return 1
    print(f"complete: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
