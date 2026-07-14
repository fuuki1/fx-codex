"""Reconnect / heartbeat policy for streaming collectors.

Semantics required by the collection protocol:
- exponential backoff with bounded jitter and a hard max delay
- heartbeat timeout marks the connection dead and OPENS A GAP — the gap is
  recorded explicitly and is never back-filled with the previous quote
- every (re)connect gets a fresh ``connection_id`` and bumps ``reconnect_count``
- while disconnected the source is NOT tradable
- an expired/revoked token STOPS the collector (fail-closed); it never retries
  its way past an authorization failure
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import random
from typing import Any
import uuid


class TokenExpiredError(RuntimeError):
    """Authorization failed. The collector must stop, not retry."""


@dataclass(frozen=True)
class BackoffPolicy:
    initial_seconds: float = 1.0
    factor: float = 2.0
    max_seconds: float = 60.0
    jitter_fraction: float = 0.25

    def __post_init__(self) -> None:
        if self.initial_seconds <= 0 or self.factor < 1.0 or self.max_seconds <= 0:
            raise ValueError("backoff parameters must be positive (factor >= 1)")
        if not 0.0 <= self.jitter_fraction < 1.0:
            raise ValueError("jitter_fraction must be in [0, 1)")

    def delay(self, attempt: int, rng: random.Random) -> float:
        """Delay before reconnect ``attempt`` (0-based), jittered, capped."""

        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        base = min(self.initial_seconds * (self.factor**attempt), self.max_seconds)
        jitter = base * self.jitter_fraction
        return max(0.0, base - jitter + rng.random() * 2.0 * jitter)


@dataclass
class Gap:
    started_at: datetime
    ended_at: datetime | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "reason": self.reason,
        }


@dataclass
class ConnectionState:
    """Tracks liveness for exactly one streaming source."""

    heartbeat_timeout_seconds: float
    connection_id: str = ""
    connected: bool = False
    reconnect_count: int = 0
    last_heartbeat_at: datetime | None = None
    gaps: list[Gap] = field(default_factory=list)
    stopped_reason: str | None = None

    def mark_connected(self, now: datetime) -> str:
        """New connection: fresh id, close any open gap at ``now``."""

        self.connection_id = uuid.uuid4().hex
        if self.connected is False and self.gaps and self.gaps[-1].ended_at is None:
            self.gaps[-1].ended_at = now
        self.connected = True
        self.last_heartbeat_at = now.astimezone(UTC)
        return self.connection_id

    def heartbeat(self, now: datetime) -> None:
        if not self.connected:
            return
        self.last_heartbeat_at = now.astimezone(UTC)

    def check_alive(self, now: datetime) -> bool:
        """Heartbeat overdue -> disconnect and open a gap. Returns liveness."""

        if not self.connected:
            return False
        assert self.last_heartbeat_at is not None
        overdue = (now.astimezone(UTC) - self.last_heartbeat_at).total_seconds()
        if overdue > self.heartbeat_timeout_seconds:
            self.mark_disconnected(now, reason="heartbeat_timeout")
            return False
        return True

    def mark_disconnected(self, now: datetime, *, reason: str) -> None:
        if self.connected:
            self.reconnect_count += 1
        self.connected = False
        if not self.gaps or self.gaps[-1].ended_at is not None:
            self.gaps.append(Gap(started_at=now.astimezone(UTC), reason=reason))

    def stop(self, reason: str) -> None:
        """Terminal fail-closed stop (e.g. token expiry). No further retries."""

        self.stopped_reason = reason
        self.connected = False

    @property
    def tradable(self) -> bool:
        """A quote may only be tradable while the stream is demonstrably live."""

        return self.connected and self.stopped_reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "connected": self.connected,
            "reconnect_count": self.reconnect_count,
            "last_heartbeat_at": (
                self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None
            ),
            "gaps": [gap.to_dict() for gap in self.gaps],
            "stopped_reason": self.stopped_reason,
        }
