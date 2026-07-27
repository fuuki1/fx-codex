"""Reliable and secret-safe Discord webhook delivery."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
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
OUTBOX_SCHEMA = 1
MAX_PENDING_BATCHES = 100


class DiscordDeliveryError(RuntimeError):
    """Webhook delivery failed after bounded retries."""


def _batch_id(payloads: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        payloads,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _label_parts(payloads: list[dict[str, Any]], batch_id: str) -> list[dict[str, Any]]:
    if len(payloads) <= 1:
        return payloads
    labeled: list[dict[str, Any]] = []
    for index, raw in enumerate(payloads, start=1):
        marker = f"fx-codex batch {batch_id[:12]} part {index}/{len(payloads)}"
        # Keep the application payload byte-for-byte equivalent. Prepending the
        # marker to a 2000-character content field would silently truncate it.
        labeled.append({"content": marker})
        labeled.append(dict(raw))
    return labeled


def _load_outbox(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": OUTBOX_SCHEMA, "batches": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DiscordDeliveryError(f"Discord outboxを読めません: {type(error).__name__}") from error
    if not isinstance(payload, dict) or payload.get("schema") != OUTBOX_SCHEMA:
        raise DiscordDeliveryError("Discord outboxのschemaが不正です")
    batches = payload.get("batches")
    if not isinstance(batches, list):
        raise DiscordDeliveryError("Discord outboxのbatchesが不正です")
    return payload


def _save_outbox(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, indent=2, allow_nan=False)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def send_webhook_batch(
    webhook_url: str,
    payloads: list[dict[str, Any]],
    *,
    outbox_path: str | Path,
) -> None:
    """Durably resume a multi-message webhook batch after partial delivery.

    Discord has no transactional multi-message webhook operation. This outbox
    therefore provides ordered at-least-once delivery: completed parts are
    checkpointed, failed parts remain pending, and each multi-part message is
    visibly labeled with a stable batch/part identifier.
    """

    if not payloads:
        return
    outbox = Path(outbox_path)
    outbox.parent.mkdir(parents=True, exist_ok=True)
    lock_path = outbox.with_suffix(outbox.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _load_outbox(outbox)
        batches = state["batches"]
        assert isinstance(batches, list)
        identifier = _batch_id(payloads)
        if not any(
            isinstance(batch, Mapping) and batch.get("batch_id") == identifier for batch in batches
        ):
            if len(batches) >= MAX_PENDING_BATCHES:
                raise DiscordDeliveryError("Discord outboxが上限に達しました")
            batches.append(
                {
                    "batch_id": identifier,
                    "payloads": _label_parts(payloads, identifier),
                    "next_part": 0,
                }
            )
            _save_outbox(outbox, state)

        while batches:
            batch = batches[0]
            if not isinstance(batch, dict):
                raise DiscordDeliveryError("Discord outbox batchの形式が不正です")
            stored_payloads = batch.get("payloads")
            next_part = batch.get("next_part")
            if not isinstance(stored_payloads, list) or not isinstance(next_part, int):
                raise DiscordDeliveryError("Discord outbox checkpointの形式が不正です")
            while next_part < len(stored_payloads):
                payload = stored_payloads[next_part]
                if not isinstance(payload, Mapping):
                    raise DiscordDeliveryError("Discord outbox payloadの形式が不正です")
                send_webhook(webhook_url, payload)
                next_part += 1
                batch["next_part"] = next_part
                _save_outbox(outbox, state)
            batches.pop(0)
            _save_outbox(outbox, state)


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
