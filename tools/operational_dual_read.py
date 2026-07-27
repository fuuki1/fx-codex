#!/usr/bin/env python3
"""Compare the active dashboard with the independent operational read API."""

from __future__ import annotations

import argparse
from datetime import datetime, UTC
import json
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.operational_dual_read import (  # noqa: E402
    DualReadError,
    compare_dual_read,
    dashboard_rows,
)
from fx_intel.state_io import atomic_write_json_create_only  # noqa: E402

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_TIMEFRAMES = ("15m", "1h", "4h", "1d")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="現行dashboardと独立read APIのcompact判断行をread-only比較する"
    )
    parser.add_argument("--dashboard-url", required=True)
    parser.add_argument("--read-api-url", required=True)
    parser.add_argument("--symbol", default="USDJPY")
    parser.add_argument("--timeframes", nargs="+", default=list(DEFAULT_TIMEFRAMES))
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def run_comparison(
    *,
    dashboard_url: str,
    read_api_url: str,
    symbol: str,
    timeframes: list[str],
    timeout_seconds: float,
) -> dict[str, object]:
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    dashboard, dashboard_etag = _fetch_json(
        dashboard_url,
        timeout_seconds=timeout_seconds,
    )
    read_meta, read_meta_etag = _fetch_json(
        f"{read_api_url.rstrip('/')}/v1/meta",
        timeout_seconds=timeout_seconds,
    )
    read_rows: dict[str, list[dict[str, object]]] = {}
    read_etags: dict[str, list[str]] = {}
    for timeframe in timeframes:
        expected = len(
            dashboard_rows(
                dashboard,
                symbol=symbol.upper(),
                timeframe=timeframe,
            )
        )
        rows, etags = _read_decisions(
            base_url=read_api_url,
            symbol=symbol,
            timeframe=timeframe,
            count=expected,
            timeout_seconds=timeout_seconds,
        )
        read_rows[timeframe] = rows
        read_etags[timeframe] = etags
    comparison = compare_dual_read(
        dashboard,
        read_rows,
        symbol=symbol,
        timeframes=timeframes,
    )
    return {
        "schema_version": 1,
        "operation": "operational_dual_read",
        "generated_at": datetime.now(UTC).isoformat(),
        "dashboard": {
            "url": dashboard_url,
            "etag": dashboard_etag,
            "generated_at": dashboard.get("generated_at"),
        },
        "read_api": {
            "url": read_api_url,
            "metadata_etag": read_meta_etag,
            "metadata": read_meta.get("metadata"),
            "page_etags": read_etags,
        },
        "comparison": comparison,
        "verdict": comparison["verdict"],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output.exists():
        return _error(f"output already exists: {args.output}")
    try:
        report = run_comparison(
            dashboard_url=args.dashboard_url,
            read_api_url=args.read_api_url,
            symbol=args.symbol,
            timeframes=args.timeframes,
            timeout_seconds=args.timeout_seconds,
        )
        atomic_write_json_create_only(args.output, report)
    except (DualReadError, HTTPError, OSError, URLError, ValueError) as error:
        return _error(str(error))
    print(json.dumps({"ok": report["verdict"] == "parity_verified", **report}))
    return 0 if report["verdict"] == "parity_verified" else 1


def _read_decisions(
    *,
    base_url: str,
    symbol: str,
    timeframe: str,
    count: int,
    timeout_seconds: float,
) -> tuple[list[dict[str, object]], list[str]]:
    rows: list[dict[str, object]] = []
    etags: list[str] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while len(rows) < count:
        query: dict[str, object] = {
            "limit": min(200, count - len(rows)),
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "pit_eligible": "all",
        }
        if cursor is not None:
            query["cursor"] = cursor
        payload, etag = _fetch_json(
            f"{base_url.rstrip('/')}/v1/decisions?{urlencode(query)}",
            timeout_seconds=timeout_seconds,
        )
        items = payload.get("items")
        page = payload.get("page")
        if not isinstance(items, list) or not isinstance(page, dict):
            raise DualReadError("read API decision page has an invalid shape")
        if any(not isinstance(item, dict) for item in items):
            raise DualReadError("read API decision item is not an object")
        if not items and page.get("next_cursor") is not None:
            raise DualReadError("read API returned an empty page with a cursor")
        rows.extend(dict(item) for item in items)
        if etag:
            etags.append(etag)
        cursor = page.get("next_cursor")
        if cursor is None:
            break
        if not isinstance(cursor, str):
            raise DualReadError("read API cursor has an invalid shape")
        if cursor in seen_cursors:
            raise DualReadError("read API repeated a pagination cursor")
        seen_cursors.add(cursor)
    return rows[:count], etags


def _fetch_json(
    url: str,
    *,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str | None]:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_seconds) as response:
        length_header = response.headers.get("Content-Length")
        if length_header is not None and int(length_header) > MAX_RESPONSE_BYTES:
            raise DualReadError("HTTP response exceeds the size limit")
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise DualReadError("HTTP response exceeds the size limit")
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as error:
            raise DualReadError("HTTP response is not valid JSON") from error
        if not isinstance(payload, dict):
            raise DualReadError("HTTP response is not a JSON object")
        return payload, response.headers.get("ETag")


def _error(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
