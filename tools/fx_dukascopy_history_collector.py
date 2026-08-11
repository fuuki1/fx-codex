#!/usr/bin/env python3
"""Collect credential-free delayed Dukascopy Bid/Ask for later reconciliation.

Only completed historical hours are downloaded.  The tool has no broker
account, order endpoint, or trading capability.  A two-hour publication lag is
the default so an hour is not permanently skipped while the provider is still
publishing it.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import socket
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.dukascopy import (  # noqa: E402
    DukascopyFetchError,
    ingest_hour,
    requests_fetcher,
)
from data_platform.collect.raw_first import QuoteLog  # noqa: E402
from data_platform.raw.immutable_store import ImmutableRawStore  # noqa: E402
from fx_intel.market import is_market_open  # noqa: E402

DEFAULT_INSTRUMENTS = ("USDJPY", "EURUSD", "GBPUSD")


def _hour(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _read_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"schema_version": 1, "completed_hours": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("completed_hours"), dict):
        raise ValueError("Dukascopy collector state is invalid")
    return payload


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _latest_logged_hours(path: Path, instruments: list[str]) -> dict[str, datetime]:
    if not path.is_file():
        return {}
    wanted = set(instruments)
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - 64_000_000)
        handle.seek(start)
        payload = handle.read()
    lines = payload.decode("utf-8").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    latest: dict[str, datetime] = {}
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("provider") != "dukascopy":
                continue
            symbol = str(row.get("instrument") or "").upper().replace("_", "")
            if symbol not in wanted:
                continue
            event = datetime.fromisoformat(str(row.get("provider_event_time")))
            if event.tzinfo is None or event.utcoffset() is None:
                continue
            event_hour = _hour(event)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if symbol not in latest or event_hour > latest[symbol]:
            latest[symbol] = event_hour
    return latest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--instruments", nargs="+", default=list(DEFAULT_INSTRUMENTS))
    parser.add_argument("--lag-hours", type=int, default=2)
    parser.add_argument("--initial-lookback-hours", type=int, default=4)
    parser.add_argument("--max-hours-per-run", type=int, default=6)
    parser.add_argument("--now", type=datetime.fromisoformat)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    now = args.now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise SystemExit("--now must be timezone-aware")
    if args.lag_hours < 1 or args.initial_lookback_hours < 1 or args.max_hours_per_run < 1:
        raise SystemExit("lag/lookback/max-hours must be positive")
    output_root = args.output_root.resolve()
    state_path = output_root / "state" / "dukascopy_history.json"
    eligible = _hour(now) - timedelta(hours=args.lag_hours)
    state = _read_state(state_path)
    raw_completed = state["completed_hours"]
    assert isinstance(raw_completed, dict)
    symbols = [str(value).upper().replace("/", "").replace("_", "") for value in args.instruments]
    logged_hours = (
        {}
        if all(symbol in raw_completed for symbol in symbols)
        else _latest_logged_hours(output_root / "log" / "quotes.jsonl", symbols)
    )
    plan: dict[str, list[str]] = {}
    for symbol in symbols:
        previous = raw_completed.get(symbol)
        if previous is None and symbol in logged_hours:
            previous = logged_hours[symbol].isoformat()
        if previous is None:
            next_hour = eligible - timedelta(hours=args.initial_lookback_hours - 1)
        else:
            parsed = datetime.fromisoformat(str(previous))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise SystemExit(f"state hour for {symbol} is timezone-naive")
            next_hour = _hour(parsed) + timedelta(hours=1)
        hours: list[str] = []
        while next_hour <= eligible and len(hours) < args.max_hours_per_run:
            hours.append(next_hour.isoformat())
            next_hour += timedelta(hours=1)
        plan[symbol] = hours
    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "broker_connected": False,
                    "collection_mode": "historical_download",
                    "eligible_through": eligible.isoformat(),
                    "plan": plan,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if not any(plan.values()):
        state.update(
            {
                "schema_version": 1,
                "completed_hours": raw_completed,
                "heartbeat_at": datetime.now(UTC).isoformat(),
                "heartbeat_ok": True,
                "broker_connected": False,
                "collection_mode": "historical_download",
            }
        )
        _write_state(state_path, state)
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "idle_no_new_completed_hour",
                    "broker_connected": False,
                    "collection_mode": "historical_download",
                    "eligible_through": eligible.isoformat(),
                    "completed_hours_this_run": 0,
                    "accepted_quotes": 0,
                    "quarantined_quotes": 0,
                    "errors": [],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    raw_store = ImmutableRawStore(output_root / "raw")
    quote_log = QuoteLog(output_root / "log")
    accepted = 0
    quarantined = 0
    completed = 0
    errors: list[dict[str, str]] = []
    connection_id = f"dukascopy-history:{socket.gethostname()}:{os.getpid()}:{now.isoformat()}"
    for symbol, hours in plan.items():
        for raw_hour in hours:
            hour_start = datetime.fromisoformat(raw_hour)
            try:
                result = ingest_hour(
                    symbol,
                    hour_start,
                    fetcher=requests_fetcher,
                    store=raw_store,
                    log=quote_log,
                    connection_id=connection_id,
                )
            except (DukascopyFetchError, OSError, ValueError) as error:
                errors.append({"instrument": symbol, "hour": raw_hour, "error": str(error)[:500]})
                break
            if result is not None:
                accepted += result.accepted_count
                quarantined += len(result.quarantined)
            elif is_market_open(hour_start + timedelta(minutes=30)):
                errors.append(
                    {
                        "instrument": symbol,
                        "hour": raw_hour,
                        "error": "market-open hour is not published yet; retrying later",
                    }
                )
                break
            raw_completed[symbol] = hour_start.isoformat()
            completed += 1
            state.update(
                {
                    "schema_version": 1,
                    "completed_hours": raw_completed,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "broker_connected": False,
                    "collection_mode": "historical_download",
                }
            )
            _write_state(state_path, state)
    state.update(
        {
            "schema_version": 1,
            "completed_hours": raw_completed,
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "heartbeat_ok": not errors,
            "broker_connected": False,
            "collection_mode": "historical_download",
        }
    )
    _write_state(state_path, state)
    print(
        json.dumps(
            {
                "ok": not errors,
                "broker_connected": False,
                "collection_mode": "historical_download",
                "eligible_through": eligible.isoformat(),
                "completed_hours_this_run": completed,
                "accepted_quotes": accepted,
                "quarantined_quotes": quarantined,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
