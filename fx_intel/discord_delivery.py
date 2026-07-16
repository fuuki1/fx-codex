"""Reliable and secret-safe Discord webhook delivery."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import time
from typing import Any

import requests

DEFAULT_ATTEMPTS = 4
DEFAULT_TIMEOUT = (10.0, 20.0)
MAX_RETRY_DELAY_SECONDS = 30.0
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class DiscordDeliveryError(RuntimeError):
    """Webhook delivery failed after bounded retries."""


def _retry_after_seconds(response: requests.Response, attempt: int) -> float:
    values: list[object] = []
    try:
        payload = response.json()
    except (ValueError, requests.RequestException):
        payload = None
    if isinstance(payload, Mapping):
        values.append(payload.get("retry_after"))
    values.append(response.headers.get("Retry-After"))
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        try:
            delay = float(value)
        except (TypeError, ValueError):
            continue
        if delay >= 0:
            return min(delay, MAX_RETRY_DELAY_SECONDS)
    return min(float(2 ** (attempt - 1)), MAX_RETRY_DELAY_SECONDS)


def send_webhook(
    webhook_url: str,
    payload: Mapping[str, Any],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Send a payload, retrying rate limits, 5xx, and transport errors."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if not webhook_url.startswith("https://"):
        raise DiscordDeliveryError("Discord通知設定が不正です（HTTPS URLが必要）")

    last_reason = "unknown"
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                webhook_url,
                json=dict(payload),
                timeout=timeout,
            )
        except requests.RequestException as error:
            last_reason = f"network error ({type(error).__name__})"
            if attempt >= attempts:
                break
            sleep_fn(min(float(2 ** (attempt - 1)), MAX_RETRY_DELAY_SECONDS))
            continue

        delay = 0.0
        try:
            status = response.status_code
            if 200 <= status < 300:
                return
            last_reason = f"HTTP {status}"
            if status not in RETRYABLE_STATUS_CODES or attempt >= attempts:
                break
            delay = _retry_after_seconds(response, attempt)
        finally:
            response.close()
        sleep_fn(delay)

    raise DiscordDeliveryError(f"Discord通知に失敗しました（{last_reason}, attempts={attempts}）")
