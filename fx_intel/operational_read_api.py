"""Loopback HTTP API for the compact operational read model."""

from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from .operational_read_model import (
    ReadModelError,
    ReadModelStaleError,
    canonical_response_bytes,
    chart_snapshot,
    current_source_snapshot,
    decision_page,
    price_page,
    read_model_metadata,
    response_etag,
)
from .operational_store import OperationalStoreError, open_operational_reader

_CACHE_CONTROL = "private, max-age=0, must-revalidate"
_ServerRequest = socket.socket | tuple[bytes, socket.socket]


class OperationalReadHTTPServer(ThreadingHTTPServer):
    """Bounded loopback server whose workers open kernel-enforced read-only DBs."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        bind_and_activate: bool = True,
        *,
        max_workers: int = 16,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self.max_workers = max_workers
        self.request_timeout_seconds = request_timeout_seconds
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(
            server_address,
            request_handler_class,
            bind_and_activate=bind_and_activate,
        )

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout_seconds)
        return request, client_address

    def process_request(self, request: _ServerRequest, client_address: Any) -> None:
        tcp_request = cast(socket.socket, request)
        if not self._worker_slots.acquire(blocking=False):
            try:
                tcp_request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\n"
                    b"Cache-Control: no-store\r\n"
                    b"Connection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(tcp_request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(
        self,
        request: _ServerRequest,
        client_address: Any,
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


def make_handler(database_path: str | Path) -> type[BaseHTTPRequestHandler]:
    """Bind one immutable database path into a stdlib request handler."""

    database = Path(database_path).resolve()

    class OperationalReadHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "FXOperationalRead/1"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            try:
                query = _query_parameters(parsed.query)
                if parsed.path == "/v1/decisions":
                    _require_only(
                        query,
                        {"limit", "cursor", "symbol", "timeframe", "pit_eligible"},
                    )
                    with open_operational_reader(database) as store:
                        with current_source_snapshot(store):
                            payload = decision_page(
                                store,
                                limit=_limit(query),
                                cursor=_one(query, "cursor"),
                                symbol=_one(query, "symbol"),
                                timeframe=_one(query, "timeframe"),
                                pit_eligible=_optional_bool(query, "pit_eligible"),
                            )
                elif parsed.path == "/v1/prices":
                    _require_only(
                        query,
                        {"limit", "cursor", "symbol", "timeframe"},
                    )
                    with open_operational_reader(database) as store:
                        with current_source_snapshot(store):
                            payload = price_page(
                                store,
                                limit=_limit(query),
                                cursor=_one(query, "cursor"),
                                symbol=_one(query, "symbol"),
                                timeframe=_one(query, "timeframe"),
                            )
                elif parsed.path == "/v1/meta":
                    _require_only(query, set())
                    with open_operational_reader(database) as store:
                        with current_source_snapshot(store):
                            payload = {
                                "schema_version": 1,
                                "kind": "metadata",
                                "metadata": read_model_metadata(store),
                            }
                elif parsed.path == "/v1/chart":
                    _require_only(
                        query,
                        {"symbol", "timeframe", "start", "end", "as_of"},
                    )
                    with open_operational_reader(database) as store:
                        # The chart is a bounded, timestamped projection of the
                        # last canonical DB snapshot. Its own 15-minute freshness
                        # gate fails closed without exposing unsynced raw rows.
                        payload = chart_snapshot(
                            store,
                            symbol=_required(query, "symbol"),
                            timeframe=_required(query, "timeframe"),
                            start=_datetime(query, "start"),
                            end=_datetime(query, "end"),
                            as_of=_datetime(query, "as_of"),
                        )
                elif parsed.path == "/healthz":
                    _require_only(query, set())
                    with open_operational_reader(database) as store:
                        with current_source_snapshot(store):
                            metadata = read_model_metadata(store)
                    payload = {
                        "ok": True,
                        "schema_version": metadata["schema_version"],
                        "query_only": metadata["query_only"],
                    }
                else:
                    self._json_error(HTTPStatus.NOT_FOUND, "route not found")
                    return
                self._json_payload(payload)
            except ReadModelError as error:
                self._json_error(HTTPStatus.BAD_REQUEST, str(error))
            except (ReadModelStaleError, OperationalStoreError, OSError) as error:
                self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, str(error))

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def log_message(self, format: str, *args: Any) -> None:
            """Leave access-log policy to the supervising process."""

        def _json_payload(self, payload: object) -> None:
            self.close_connection = True
            body = canonical_response_bytes(payload)
            etag = response_etag(body)
            if _etag_matches(self.headers.get("If-None-Match"), etag):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", _CACHE_CONTROL)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", _CACHE_CONTROL)
            self.send_header("ETag", etag)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            snapshot = payload.get("snapshot_token") if isinstance(payload, dict) else None
            if isinstance(snapshot, str):
                self.send_header("X-Read-Model-Snapshot", snapshot)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json_error(self, status: HTTPStatus, message: str) -> None:
            self.close_connection = True
            body = canonical_response_bytes(
                {
                    "ok": False,
                    "status": int(status),
                    "error": message,
                }
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    return OperationalReadHandler


def _query_parameters(query: str) -> dict[str, list[str]]:
    try:
        return parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=10,
        )
    except ValueError as error:
        raise ReadModelError("invalid query string") from error


def _require_only(query: dict[str, list[str]], allowed: set[str]) -> None:
    unexpected = set(query) - allowed
    if unexpected:
        raise ReadModelError(f"unsupported query parameter: {sorted(unexpected)[0]}")
    for key, values in query.items():
        if len(values) != 1:
            raise ReadModelError(f"query parameter must appear once: {key}")


def _one(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if values is None:
        return None
    value = values[0].strip()
    if not value:
        raise ReadModelError(f"query parameter cannot be blank: {key}")
    return value


def _required(query: dict[str, list[str]], key: str) -> str:
    value = _one(query, key)
    if value is None:
        raise ReadModelError(f"missing query parameter: {key}")
    return value


def _datetime(query: dict[str, list[str]], key: str) -> datetime:
    value = _required(query, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReadModelError(f"{key} must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReadModelError(f"{key} must be timezone-aware")
    return parsed


def _limit(query: dict[str, list[str]]) -> int:
    value = _one(query, "limit")
    if value is None:
        return 50
    try:
        return int(value)
    except ValueError as error:
        raise ReadModelError("limit must be an integer") from error


def _optional_bool(
    query: dict[str, list[str]],
    key: str,
) -> bool | None:
    value = _one(query, key)
    if value is None or value == "all":
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise ReadModelError(f"{key} must be true, false, or all")


def _etag_matches(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    current = _weak_etag_value(etag)
    return any(
        token.strip() == "*" or _weak_etag_value(token) == current for token in header.split(",")
    )


def _weak_etag_value(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("W/"):
        return normalized[2:].lstrip()
    return normalized


__all__ = [
    "OperationalReadHTTPServer",
    "make_handler",
]
