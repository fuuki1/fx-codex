"""Point-in-time market session and regime dimensions for learning.

Session membership is derived from local exchange-centre hours with IANA
timezones, so London and New York daylight-saving changes are handled by the
standard library instead of a fixed UTC table.  The returned categorical
values are persisted at decision time and reused by every learning path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, UTC
from zoneinfo import ZoneInfo

from .market import is_market_open

SESSION_SCHEMA_VERSION = "fx-major-sessions-v1"
REGIME_SCHEMA_VERSION = "risk-regime-v1"

SESSION_TOKYO = "tokyo"
SESSION_LONDON = "london"
SESSION_NEW_YORK = "new_york"
SESSION_TOKYO_LONDON = "tokyo_london_overlap"
SESSION_LONDON_NEW_YORK = "london_new_york_overlap"
SESSION_OTHER_OVERLAP = "other_overlap"
SESSION_OFF = "off_session"
SESSION_CLOSED = "closed"
SESSION_UNKNOWN = "unknown"

SESSION_BUCKETS = (
    SESSION_TOKYO,
    SESSION_TOKYO_LONDON,
    SESSION_LONDON,
    SESSION_LONDON_NEW_YORK,
    SESSION_NEW_YORK,
    SESSION_OTHER_OVERLAP,
    SESSION_OFF,
    SESSION_CLOSED,
    SESSION_UNKNOWN,
)
REGIME_BUCKETS = ("risk_on", "risk_off", "neutral", "unknown")

_SESSION_WINDOWS = {
    SESSION_TOKYO: (ZoneInfo("Asia/Tokyo"), time(9), time(18)),
    SESSION_LONDON: (ZoneInfo("Europe/London"), time(8), time(17)),
    SESSION_NEW_YORK: (ZoneInfo("America/New_York"), time(8), time(17)),
}


@dataclass(frozen=True)
class LearningDimensions:
    observed_at: str
    session_bucket: str
    active_sessions: tuple[str, ...]
    regime: str
    regime_source: str
    schema: int = 1
    session_schema_version: str = SESSION_SCHEMA_VERSION
    regime_schema_version: str = REGIME_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "observed_at": self.observed_at,
            "session_bucket": self.session_bucket,
            "active_sessions": list(self.active_sessions),
            "session_schema_version": self.session_schema_version,
            "regime": self.regime,
            "regime_source": self.regime_source,
            "regime_schema_version": self.regime_schema_version,
        }


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _active(moment: datetime, zone: ZoneInfo, opens: time, closes: time) -> bool:
    local = _aware(moment).astimezone(zone)
    return local.weekday() < 5 and opens <= local.time().replace(tzinfo=None) < closes


def classify_market_session(moment: datetime) -> tuple[str, tuple[str, ...]]:
    """Return one categorical bucket and the underlying active session set."""

    checked = _aware(moment)
    if not is_market_open(checked):
        return SESSION_CLOSED, ()
    active = tuple(
        name
        for name, (zone, opens, closes) in _SESSION_WINDOWS.items()
        if _active(checked, zone, opens, closes)
    )
    active_set = frozenset(active)
    if active_set == {SESSION_TOKYO, SESSION_LONDON}:
        bucket = SESSION_TOKYO_LONDON
    elif active_set == {SESSION_LONDON, SESSION_NEW_YORK}:
        bucket = SESSION_LONDON_NEW_YORK
    elif len(active) > 1:
        bucket = SESSION_OTHER_OVERLAP
    elif len(active) == 1:
        bucket = active[0]
    else:
        bucket = SESSION_OFF
    return bucket, active


def normalize_regime(value: object) -> str:
    regime = str(value or "").strip().lower()
    return regime if regime in REGIME_BUCKETS[:-1] else "unknown"


def regime_source(*, analysis_engine: str, macro_available: bool) -> str:
    engine = analysis_engine.strip().lower()
    if engine == "claude":
        return "llm"
    if macro_available:
        return "macro_real_data"
    if engine in {"analyst", "lexicon"}:
        return "lexicon"
    return "unknown"


def build_learning_dimensions(
    moment: datetime,
    *,
    regime: object,
    analysis_engine: str = "",
    macro_available: bool = False,
) -> LearningDimensions:
    checked = _aware(moment)
    bucket, active = classify_market_session(checked)
    return LearningDimensions(
        observed_at=checked.astimezone(UTC).isoformat(),
        session_bucket=bucket,
        active_sessions=active,
        regime=normalize_regime(regime),
        regime_source=regime_source(
            analysis_engine=analysis_engine,
            macro_available=macro_available,
        ),
    )


def dimensions_from_mapping(
    value: object,
    *,
    fallback_ts: datetime | None = None,
) -> dict[str, object]:
    """Normalize stored dimensions; only session may be derived from an old timestamp."""

    raw = value if isinstance(value, Mapping) else {}
    session = str(raw.get("session_bucket", ""))
    active_raw = raw.get("active_sessions")
    active = (
        [str(item) for item in active_raw if str(item) in _SESSION_WINDOWS]
        if isinstance(active_raw, (list, tuple))
        else []
    )
    session_version = str(raw.get("session_schema_version", ""))
    observed_at = str(raw.get("observed_at", ""))
    if session not in SESSION_BUCKETS and fallback_ts is not None:
        session, derived_active = classify_market_session(fallback_ts)
        active = list(derived_active)
        session_version = SESSION_SCHEMA_VERSION
        observed_at = _aware(fallback_ts).astimezone(UTC).isoformat()
    elif session not in SESSION_BUCKETS:
        session = SESSION_UNKNOWN
    regime = normalize_regime(raw.get("regime"))
    return {
        "schema": 1,
        "observed_at": observed_at,
        "session_bucket": session,
        "active_sessions": active,
        "session_schema_version": session_version or SESSION_SCHEMA_VERSION,
        "regime": regime,
        "regime_source": str(raw.get("regime_source", "unknown")) or "unknown",
        "regime_schema_version": str(raw.get("regime_schema_version", REGIME_SCHEMA_VERSION)),
    }
