from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class TradingViewWebhookConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/webhook/tradingview"
    output_path: Path = Path("runs/tradingview_alerts.jsonl")
    secret: str | None = None
    max_body_bytes: int = 65_536


def parse_tradingview_alert(
    body: bytes,
    content_type: str = "application/json",
    *,
    secret: str | None = None,
    received_at_utc: str | None = None,
) -> dict[str, Any]:
    text = body.decode("utf-8")
    raw = _parse_body(text, content_type)
    if secret is not None:
        provided = raw.get("secret")
        if provided != secret:
            raise PermissionError("invalid TradingView webhook secret")
    raw.pop("secret", None)
    return normalize_tradingview_alert(raw, received_at_utc)


def normalize_tradingview_alert(
    raw: dict[str, Any],
    received_at_utc: str | None = None,
) -> dict[str, Any]:
    timestamp = received_at_utc or datetime.now(UTC).isoformat()
    ticker = _optional_string(raw.get("ticker") or raw.get("symbol"))
    exchange = _optional_string(raw.get("exchange"))
    symbol = _normalize_symbol(ticker)
    action = _optional_string(
        raw.get("action")
        or raw.get("side")
        or raw.get("strategy_order_action")
        or raw.get("strategy.order.action")
    )

    return {
        "received_at_utc": timestamp,
        "source": "tradingview",
        "exchange": exchange,
        "ticker": ticker,
        "symbol": symbol,
        "time": _optional_string(raw.get("time") or raw.get("timestamp")),
        "timeframe": _optional_string(raw.get("timeframe") or raw.get("interval")),
        "action": action,
        "side": _side_from_action(action),
        "price": _optional_float(raw.get("price") or raw.get("close")),
        "quantity": _optional_float(raw.get("quantity") or raw.get("qty") or raw.get("contracts")),
        "order_id": _optional_string(raw.get("order_id") or raw.get("id")),
        "message": _optional_string(raw.get("message")),
        "raw": raw,
    }


def append_tradingview_alert(path: str | Path, alert: dict[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(alert, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
    return destination


def run_tradingview_webhook_server(config: TradingViewWebhookConfig) -> int:
    server = ThreadingHTTPServer((config.host, config.port), _handler_class(config))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def _handler_class(config: TradingViewWebhookConfig) -> type[BaseHTTPRequestHandler]:
    class TradingViewWebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if urlparse(self.path).path != config.path:
                self._write_json(404, {"status": "error", "error": "not_found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._write_json(400, {"status": "error", "error": "invalid_content_length"})
                return
            if length <= 0:
                self._write_json(400, {"status": "error", "error": "empty_body"})
                return
            if length > config.max_body_bytes:
                self._write_json(413, {"status": "error", "error": "body_too_large"})
                return

            body = self.rfile.read(length)
            try:
                alert = parse_tradingview_alert(
                    body,
                    self.headers.get("Content-Type", ""),
                    secret=config.secret,
                )
                append_tradingview_alert(config.output_path, alert)
            except PermissionError as error:
                self._write_json(403, {"status": "error", "error": str(error)})
                return
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
                self._write_json(400, {"status": "error", "error": str(error)})
                return

            self._write_json(
                200,
                {
                    "status": "ok",
                    "output": str(config.output_path),
                    "received_at_utc": alert["received_at_utc"],
                },
            )

        def do_GET(self) -> None:
            if urlparse(self.path).path == "/health":
                self._write_json(200, {"status": "ok"})
                return
            self._write_json(405, {"status": "error", "error": "method_not_allowed"})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return TradingViewWebhookHandler


def _parse_body(text: str, content_type: str) -> dict[str, Any]:
    content_type = content_type.split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("TradingView webhook JSON body must be an object")
        return value
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"message": text}
    if not isinstance(value, dict):
        raise ValueError("TradingView webhook JSON body must be an object")
    return value


def _normalize_symbol(value: str | None) -> str | None:
    if value is None:
        return None
    symbol = value.split(":", 1)[-1]
    return symbol.upper().replace("/", "").replace("-", "").strip()


def _side_from_action(action: str | None) -> str | None:
    if action is None:
        return None
    normalized = action.strip().lower()
    if normalized in {"buy", "long"}:
        return "buy"
    if normalized in {"sell", "short"}:
        return "sell"
    if normalized in {"close", "exit", "flat"}:
        return "close"
    return normalized or None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
