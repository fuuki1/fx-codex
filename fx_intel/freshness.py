"""Fail-closed interpretation of the operational freshness monitor report."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from collections.abc import Mapping


@dataclass(frozen=True)
class FreshnessGate:
    allow_new_risk: bool
    status: str
    reason: str
    monitor_timestamp: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_freshness_report(
    path: str | Path,
    *,
    now: datetime | None = None,
    max_report_age_seconds: float = 600.0,
    max_future_skew_seconds: float = 60.0,
) -> FreshnessGate:
    """Allow new risk only when a recent monitor report is explicitly ``ok``.

    A missing, malformed, future-dated, stale, warning, critical, or unknown report
    is a hard veto. This keeps absence of evidence from being interpreted as health.
    """

    if max_report_age_seconds <= 0 or max_future_skew_seconds < 0:
        raise ValueError("freshness age/skew thresholds are invalid")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    report_path = Path(path)
    if not report_path.is_file():
        return FreshnessGate(False, "missing", f"鮮度レポートが存在しない: {report_path}")
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return FreshnessGate(False, "invalid", f"鮮度レポートを検証できない: {error}")
    if not isinstance(payload, Mapping):
        return FreshnessGate(False, "invalid", "鮮度レポートがJSON objectではない")

    raw_timestamp = payload.get("monitor_timestamp")
    try:
        monitored_at = datetime.fromisoformat(str(raw_timestamp))
    except (TypeError, ValueError):
        return FreshnessGate(False, "invalid", "monitor_timestampが不正")
    if monitored_at.tzinfo is None:
        return FreshnessGate(False, "invalid", "monitor_timestampにUTC offsetがない")
    monitored_at = monitored_at.astimezone(UTC)
    age_seconds = (now - monitored_at).total_seconds()
    if age_seconds < -max_future_skew_seconds:
        return FreshnessGate(
            False,
            "future",
            f"鮮度レポートが未来時刻({-age_seconds:.0f}秒先)",
            monitored_at.isoformat(),
        )
    if age_seconds > max_report_age_seconds:
        return FreshnessGate(
            False,
            "stale",
            f"鮮度レポート自体が古い({age_seconds:.0f}秒)",
            monitored_at.isoformat(),
        )

    overall = str(payload.get("overall") or "").strip().lower()
    if overall != "ok":
        status = overall if overall in {"warning", "critical"} else "invalid"
        return FreshnessGate(
            False,
            status,
            f"データ鮮度の総合状態が{overall or 'unknown'}",
            monitored_at.isoformat(),
        )
    return FreshnessGate(True, "ok", "データ鮮度レポート正常", monitored_at.isoformat())
