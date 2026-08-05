#!/usr/bin/env python3
"""Loopback-only query API for the analysis-only virtual portfolio."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.virtual_portfolio import portfolio_snapshot  # noqa: E402

DEFAULT_DB = REPO_ROOT / "logs" / "fx_virtual_portfolio.sqlite3"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class VirtualPortfolioReadHandler(BaseHTTPRequestHandler):
    server_version = "FxVirtualPortfolioRead/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.query:
            self._send_error(HTTPStatus.BAD_REQUEST, "query parameters are not accepted")
            return
        if parsed.path == "/v1/snapshot":
            self._send_json(
                portfolio_snapshot(
                    self.server.portfolio_db,  # type: ignore[attr-defined]
                )
            )
            return
        if parsed.path == "/health":
            snapshot = portfolio_snapshot(
                self.server.portfolio_db,  # type: ignore[attr-defined]
            )
            self._send_json(
                {
                    "ok": snapshot.get("configured") is True,
                    "read_only": True,
                    "broker_connected": False,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "reconciliation": snapshot.get("reconciliation"),
                }
            )
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not found")

    def log_message(self, fmt: str, *args: object) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {fmt % args}")

    def _send_json(self, payload: dict[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        data = json.dumps(
            {"ok": False, "status": int(status), "error": message},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Loopback query-only virtual portfolio API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    if args.host not in LOOPBACK_HOSTS:
        parser.error("virtual portfolio read API must bind to loopback")
    server = ThreadingHTTPServer((args.host, args.port), VirtualPortfolioReadHandler)
    server.portfolio_db = args.db.expanduser().resolve()  # type: ignore[attr-defined]
    print(f"Virtual portfolio query-only API: http://{args.host}:{args.port}/")
    print(f"Reading: {server.portfolio_db}")  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
