"""ALFRED (FRED archival) macro PIT capture — keyless, vintage-correct.

Primary source: ``https://alfred.stlouisfed.org/graph/alfredgraph.csv`` with a
``vintage_date`` parameter. We verified empirically (2026-07-14) that this
endpoint returns TRUE vintages keylessly — e.g. GDPC1 2023Q4 at
``vintage_date=2024-02-01`` returns the advance-estimate 22672.859, not the
later-revised 23033.780, and the column header is vintage-stamped
(``GDPC1_20240201``). The plain ``fred.stlouisfed.org/graph/fredgraph.csv``
endpoint SILENTLY IGNORES ``vintage_date`` (it returned post-vintage values in
our probe) and is therefore FORBIDDEN here.

PIT discipline:
- each (series, vintage_date) capture is one immutable raw CSV, hash-addressed
- ``available_at`` is the capture completion time (when WE first held the
  bytes), never the observation period and never the vintage date
- initial vs revised values are separate records (one per vintage) — a later
  vintage never overwrites an earlier one
- ALFRED supplies no intraday release timestamp: ``provider_released_at`` is
  ``None`` with flag ``provider_does_not_supply_release_time``; the vintage
  date bounds the release date from above. ``scheduled_at`` is likewise not
  supplied and never fabricated.
- ``as_of`` never returns a record captured after the prediction time, and
  never a vintage dated after the prediction date.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
import json
import os
from pathlib import Path
import socket
from typing import Any

from data_platform.raw.immutable_store import ImmutableRawStore

MACRO_SCHEMA_VERSION = 1
ALFRED_BASE = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"
FLAG_NO_RELEASE_TIME = "provider_does_not_supply_release_time"
FLAG_NO_SCHEDULE = "provider_does_not_supply_scheduled_time"
FLAG_AVAILABILITY_CAPTURE = "availability_normalized_to_capture_time"
FLAG_VINTAGE_SOURCE = "vintage_from_alfredgraph_keyless"

# Series routed through this collector, with static registry metadata. The
# registry never invents provider values; it only labels what the series IS.
SERIES_REGISTRY: dict[str, dict[str, str]] = {
    "CPIAUCSL": {"country": "US", "currency": "USD", "event_name": "CPI (All Urban, SA)"},
    "GDPC1": {"country": "US", "currency": "USD", "event_name": "Real GDP (chained 2017$)"},
    "UNRATE": {"country": "US", "currency": "USD", "event_name": "Unemployment rate"},
    "FEDFUNDS": {"country": "US", "currency": "USD", "event_name": "Effective Fed Funds rate"},
}


class MacroPITError(RuntimeError):
    """Malformed macro capture — fail closed, keep the raw for forensics."""


@dataclass(frozen=True)
class MacroObservation:
    """One (series, period, vintage) value as known at capture time."""

    provider: str
    event_id: str
    series_id: str
    country: str
    currency: str
    event_name: str
    scheduled_at: None  # ALFRED does not supply schedules; never fabricated
    provider_released_at: None  # ALFRED supplies no intraday release stamp
    received_at: datetime
    available_at: datetime
    period: date
    value: float | None  # None = provider marked the cell '.', kept as missing
    vintage_date: date
    source_uri: str
    raw_payload_sha256: str
    writer_id: str
    schema_version: int = MACRO_SCHEMA_VERSION
    quality_flags: tuple[str, ...] = (
        FLAG_NO_RELEASE_TIME,
        FLAG_NO_SCHEDULE,
        FLAG_AVAILABILITY_CAPTURE,
        FLAG_VINTAGE_SOURCE,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "event_id": self.event_id,
            "series_id": self.series_id,
            "country": self.country,
            "currency": self.currency,
            "event_name": self.event_name,
            "scheduled_at": None,
            "provider_released_at": None,
            "received_at": self.received_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "period": self.period.isoformat(),
            "value": self.value,
            "vintage_date": self.vintage_date.isoformat(),
            "source_uri": self.source_uri,
            "raw_payload_sha256": self.raw_payload_sha256,
            "writer_id": self.writer_id,
            "quality_flags": list(self.quality_flags),
        }


def vintage_url(series_id: str, vintage: date, start: date, end: date) -> str:
    if end < start:
        raise MacroPITError("end must not precede start")
    return (
        f"{ALFRED_BASE}?id={series_id}&cosd={start.isoformat()}"
        f"&coed={end.isoformat()}&vintage_date={vintage.isoformat()}"
    )


Fetcher = Callable[[str], tuple[int, bytes]]


def requests_fetcher(url: str) -> tuple[int, bytes]:  # pragma: no cover - network
    import requests

    response = requests.get(
        url,
        headers={"User-Agent": "fx-codex-collect/1.0 (research; read-only)"},
        timeout=45,
    )
    return response.status_code, response.content


def parse_vintage_csv(
    raw: bytes,
    *,
    series_id: str,
    vintage: date,
    source_uri: str,
    received_at: datetime,
) -> list[MacroObservation]:
    """Parse one alfredgraph vintage CSV. The vintage-stamped column header
    (``SERIES_YYYYMMDD``) is REQUIRED — it proves the endpoint honoured the
    vintage instead of silently returning current data."""

    registry = SERIES_REGISTRY.get(series_id)
    if registry is None:
        raise MacroPITError(f"series {series_id!r} is not in the registry")
    text = raw.decode("utf-8-sig", "strict")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise MacroPITError(f"vintage CSV for {series_id} has no observations")
    header = [cell.strip() for cell in lines[0].split(",")]
    expected_column = f"{series_id}_{vintage.strftime('%Y%m%d')}"
    if header[:1] != ["observation_date"] or expected_column not in header:
        raise MacroPITError(
            f"vintage column {expected_column!r} missing from header {header!r} — "
            "endpoint may have ignored vintage_date; refusing non-PIT data"
        )
    value_index = header.index(expected_column)
    raw_sha = hashlib.sha256(raw).hexdigest()
    writer = f"{socket.gethostname()}:{os.getpid()}"
    observations: list[MacroObservation] = []
    received = received_at.astimezone(UTC)
    for line in lines[1:]:
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) <= value_index:
            raise MacroPITError(f"malformed row in {series_id} vintage CSV: {line!r}")
        period = date.fromisoformat(cells[0])
        cell = cells[value_index]
        value = None if cell in (".", "") else float(cell)
        observations.append(
            MacroObservation(
                provider="alfred",
                event_id=f"{series_id}:{period.isoformat()}:{vintage.isoformat()}",
                series_id=series_id,
                country=registry["country"],
                currency=registry["currency"],
                event_name=registry["event_name"],
                scheduled_at=None,
                provider_released_at=None,
                received_at=received,
                available_at=received,
                period=period,
                value=value,
                vintage_date=vintage,
                source_uri=source_uri,
                raw_payload_sha256=raw_sha,
                writer_id=writer,
            )
        )
    return observations


class MacroPITLog:
    """Append-only JSONL store of vintage observations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, observations: Sequence[MacroObservation]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            for observation in observations:
                handle.write(json.dumps(observation.to_dict(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return len(observations)

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


def capture_vintage(
    series_id: str,
    vintage: date,
    start: date,
    end: date,
    *,
    fetcher: Fetcher,
    store: ImmutableRawStore,
    log: MacroPITLog,
    now: Callable[[], datetime] | None = None,
) -> list[MacroObservation]:
    """Fetch one vintage, raw-first, parse, append. Fail closed on any error."""

    clock = now or (lambda: datetime.now(UTC))
    url = vintage_url(series_id, vintage, start, end)
    status, body = fetcher(url)
    if status != 200 or not body:
        raise MacroPITError(f"{url} -> HTTP {status}; refusing to substitute a default")
    ref = store.put(body)
    stored = store.get(ref.sha256)
    if hashlib.sha256(stored).hexdigest() != ref.sha256:
        raise MacroPITError("raw hash verification failed for macro capture")
    observations = parse_vintage_csv(
        body, series_id=series_id, vintage=vintage, source_uri=url, received_at=clock()
    )
    log.append(observations)
    return observations


def as_of(
    log: MacroPITLog,
    series_id: str,
    prediction_time: datetime,
) -> list[dict[str, Any]]:
    """Values knowable at ``prediction_time``: captured before it AND from a
    vintage dated on/before its date. Latest vintage per period wins; later
    revisions never leak backwards."""

    if prediction_time.tzinfo is None:
        raise MacroPITError("prediction_time must be timezone-aware")
    cutoff = prediction_time.astimezone(UTC)
    best: dict[str, dict[str, Any]] = {}
    for row in log.rows():
        if row["series_id"] != series_id:
            continue
        available = datetime.fromisoformat(row["available_at"])
        vintage = date.fromisoformat(row["vintage_date"])
        if available > cutoff or vintage > cutoff.date():
            continue
        period = str(row["period"])
        current = best.get(period)
        if current is None or vintage > date.fromisoformat(current["vintage_date"]):
            best[period] = row
    return [best[period] for period in sorted(best)]
