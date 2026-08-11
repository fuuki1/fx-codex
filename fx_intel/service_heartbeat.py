"""Fail-closed liveness gate for services required by canonical shadow fills."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path


@dataclass(frozen=True)
class ServiceHeartbeatResult:
    service_id: str
    path: str
    ok: bool
    age_seconds: float | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ServiceHeartbeatGate:
    allow_new_risk: bool
    checked_at: str
    services: tuple[ServiceHeartbeatResult, ...]

    @property
    def rejection_reasons(self) -> list[str]:
        return [
            f"required service heartbeat unavailable:{row.service_id}:{row.reason}"
            for row in self.services
            if not row.ok
        ]

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": "required-service-heartbeat-gate-v1",
            "allow_new_risk": self.allow_new_risk,
            "checked_at": self.checked_at,
            "services": [row.to_dict() for row in self.services],
            "rejection_reasons": self.rejection_reasons,
        }


def evaluate_required_service_heartbeats(
    config_path: str | Path,
    *,
    root: str | Path,
    now: datetime | None = None,
) -> ServiceHeartbeatGate:
    """Require every configured service output heartbeat to be recent and healthy."""

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("heartbeat now must be timezone-aware")
    moment = moment.astimezone(UTC)
    config = Path(config_path)
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        missing = ServiceHeartbeatResult(
            service_id="heartbeat-config",
            path=str(config),
            ok=False,
            age_seconds=None,
            reason=f"config_unavailable:{type(error).__name__}",
        )
        return ServiceHeartbeatGate(False, moment.isoformat(), (missing,))
    raw_services = payload.get("services") if isinstance(payload, Mapping) else None
    if not isinstance(raw_services, list) or not raw_services:
        invalid = ServiceHeartbeatResult(
            service_id="heartbeat-config",
            path=str(config),
            ok=False,
            age_seconds=None,
            reason="no_required_services",
        )
        return ServiceHeartbeatGate(False, moment.isoformat(), (invalid,))

    base = Path(root)
    results = tuple(_evaluate_service(row, base=base, now=moment) for row in raw_services)
    return ServiceHeartbeatGate(
        all(row.ok for row in results),
        moment.isoformat(),
        results,
    )


def _evaluate_service(
    raw: object,
    *,
    base: Path,
    now: datetime,
) -> ServiceHeartbeatResult:
    if not isinstance(raw, Mapping):
        return ServiceHeartbeatResult("unknown", "", False, None, "invalid_config_row")
    service_id = str(raw.get("service_id") or "").strip() or "unknown"
    relative = str(raw.get("path") or "").strip()
    path = base / relative
    raw_maximum_age = raw.get("max_age_seconds")
    try:
        maximum_age = float(raw_maximum_age) if raw_maximum_age is not None else -1.0
    except (TypeError, ValueError):
        return ServiceHeartbeatResult(service_id, relative, False, None, "invalid_max_age")
    if maximum_age <= 0 or not path.is_file():
        reason = "file_missing" if not path.is_file() else "invalid_max_age"
        return ServiceHeartbeatResult(service_id, relative, False, None, reason)
    age = (now - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)).total_seconds()
    if age < -60:
        return ServiceHeartbeatResult(service_id, relative, False, round(age, 3), "future_mtime")
    if age > maximum_age:
        return ServiceHeartbeatResult(service_id, relative, False, round(age, 3), "stale")
    expected_field = raw.get("status_field")
    expected_value = raw.get("status_value")
    if expected_field is not None:
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ServiceHeartbeatResult(
                service_id, relative, False, round(age, 3), "invalid_status_json"
            )
        actual = content.get(str(expected_field)) if isinstance(content, Mapping) else None
        if actual != expected_value:
            return ServiceHeartbeatResult(
                service_id,
                relative,
                False,
                round(age, 3),
                f"status_mismatch:{actual}",
            )
    return ServiceHeartbeatResult(service_id, relative, True, round(age, 3), "ok")


__all__ = [
    "ServiceHeartbeatGate",
    "ServiceHeartbeatResult",
    "evaluate_required_service_heartbeats",
]
