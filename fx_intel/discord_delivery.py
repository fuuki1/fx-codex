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

# 1通知あたりの実時間上限。attemptsだけでは上限を縛れない:
# SSL/timeout系は1試行で最大30秒(接続10+読取20)かかり、指数バックオフを挟むと
# 既定4試行で優に2分を超える。呼び出し元(鮮度監視)はlaunchdの300秒周期で回るため、
# 通知1件が周期を食い潰すと監視レポートの更新間隔が伸びる。
DEFAULT_BUDGET_SECONDS = 45.0


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
    budget_seconds: float | None = DEFAULT_BUDGET_SECONDS,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Send a payload, retrying rate limits, 5xx, and transport errors.

    ``budget_seconds`` caps the total wall-clock time spent across all attempts,
    so a slow or hanging endpoint cannot stall the caller indefinitely.  ``None``
    disables the cap.  The budget only prevents *starting* further work: an
    attempt already in flight still runs to its own ``timeout``.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if budget_seconds is not None and budget_seconds <= 0:
        raise ValueError("budget_seconds must be positive when set")
    if not webhook_url.startswith("https://"):
        raise DiscordDeliveryError("Discord通知設定が不正です（HTTPS URLが必要）")

    started_at = monotonic_fn()

    def remaining() -> float | None:
        if budget_seconds is None:
            return None
        return budget_seconds - (monotonic_fn() - started_at)

    last_reason = "unknown"
    used_attempts = 0
    for attempt in range(1, attempts + 1):
        used_attempts = attempt
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
            delay = min(float(2 ** (attempt - 1)), MAX_RETRY_DELAY_SECONDS)
            left = remaining()
            if left is not None and left <= delay:
                last_reason = f"{last_reason}, budget exhausted"
                break
            sleep_fn(delay)
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
        left = remaining()
        if left is not None and left <= delay:
            last_reason = f"{last_reason}, budget exhausted"
            break
        sleep_fn(delay)

    raise DiscordDeliveryError(
        f"Discord通知に失敗しました（{last_reason}, attempts={used_attempts}）"
    )
