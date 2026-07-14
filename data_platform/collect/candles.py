"""Collected-candle contract + raw-first ingest for public candle archives.

Some credential-free REAL data sources (Dukascopy datafeed, FXCM public candle
archive) publish *bar/candle* files, not tick quotes. A candle is a lossy
summary made by the provider — it must never be turned into fabricated ticks,
so it gets its own contract instead of being shoehorned into
:class:`~data_platform.collect.contract.CollectedQuote`.

``CollectedCandle`` is one provider-supplied OHLC observation for ONE side of
the book (bid or ask). Honesty rules enforced at construction (fail-closed,
never repaired):

- prices must be finite and positive, ``high >= max(open, close)``,
  ``low <= min(open, close)`` — an incoherent candle is quarantined, not fixed
- naive datetimes are rejected; an ``open_time`` in the future relative to
  ``received_at`` (beyond the declared clock-skew allowance) is rejected
- ``volume`` the provider does not supply stays ``None`` and is flagged
  ``provider_does_not_supply_volume`` — never zero-filled

Ingest follows the same mandatory raw-first order as
:mod:`data_platform.collect.raw_first`: raw bytes into the immutable store
BEFORE parsing, hash re-verified, then parse -> classify -> append-only log.
Provider padding rows (zero-volume flat candles emitted for closed-market
periods) are excluded by the provider parser with an explicit *counted* reason
recorded per payload — exclusion is visible, never silent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from data_platform.collect.contract import MAX_PROVIDER_CLOCK_AHEAD_SECONDS
from data_platform.quality.state import QualityState
from data_platform.raw.immutable_store import ImmutableRawStore

CANDLE_SCHEMA_VERSION = 1
CANDLE_INGEST_VERSION = "collect_candles_v1"

FLAG_NO_VOLUME = "provider_does_not_supply_volume"
FLAG_DUPLICATE_CANDLE = "duplicate_candle"
FLAG_OUT_OF_ORDER_CANDLE = "out_of_order_candle"

CANDLE_SIDES = ("bid", "ask")
# Source intervals this contract accepts (what providers actually publish).
CANDLE_INTERVALS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "1h": timedelta(hours=1),
}


class CandleContractError(ValueError):
    """A candle violates the contract. The raw payload must be quarantined."""


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CandleContractError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _finite_positive(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CandleContractError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise CandleContractError(f"{field_name} must be finite and positive, got {value!r}")
    return number


@dataclass(frozen=True)
class CollectedCandle:
    """One provider-supplied OHLC candle for one side of the book."""

    provider: str
    account_environment: str  # same domain as CollectedQuote
    instrument: str
    side: str  # "bid" | "ask"
    interval: str  # "1m" | "1h" (what the provider published)
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None  # provider-reported units; None when not supplied
    received_at: datetime
    connection_id: str
    writer_id: str
    raw_payload_sha256: str
    source_endpoint_class: str
    collection_mode: str
    quality_state: QualityState = QualityState.USABLE
    quality_flags: tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = CANDLE_SCHEMA_VERSION
    ingest_version: str = CANDLE_INGEST_VERSION

    def __post_init__(self) -> None:
        for name in ("provider", "instrument", "connection_id", "writer_id"):
            if not str(getattr(self, name)).strip():
                raise CandleContractError(f"{name} must be non-empty")
        if self.account_environment not in ("live", "practice", "datafeed"):
            raise CandleContractError(
                f"account_environment must be live/practice/datafeed, got "
                f"{self.account_environment!r}"
            )
        if self.side not in CANDLE_SIDES:
            raise CandleContractError(f"side must be one of {CANDLE_SIDES}, got {self.side!r}")
        if self.interval not in CANDLE_INTERVALS:
            raise CandleContractError(
                f"interval must be one of {sorted(CANDLE_INTERVALS)}, got {self.interval!r}"
            )
        if len(self.raw_payload_sha256) != 64:
            raise CandleContractError("raw_payload_sha256 must be a hex sha256")
        opened = _finite_positive(self.open, "open")
        high = _finite_positive(self.high, "high")
        low = _finite_positive(self.low, "low")
        closed = _finite_positive(self.close, "close")
        if high < max(opened, closed) or low > min(opened, closed) or high < low:
            raise CandleContractError(
                f"incoherent OHLC rejected: o={opened} h={high} l={low} c={closed}"
            )
        received = _aware(self.received_at, "received_at")
        object.__setattr__(self, "received_at", received)
        start = _aware(self.open_time, "open_time")
        ahead = (start - received).total_seconds()
        if ahead > MAX_PROVIDER_CLOCK_AHEAD_SECONDS:
            raise CandleContractError(
                f"future open_time rejected ({ahead:.3f}s ahead of received_at)"
            )
        object.__setattr__(self, "open_time", start)
        flags = list(self.quality_flags)
        if self.volume is not None:
            if isinstance(self.volume, bool) or not isinstance(self.volume, (int, float)):
                raise CandleContractError("volume must be a number or None")
            number = float(self.volume)
            if not math.isfinite(number) or number < 0.0:
                raise CandleContractError("volume must be finite and non-negative")
            object.__setattr__(self, "volume", number)
        elif FLAG_NO_VOLUME not in flags:
            flags.append(FLAG_NO_VOLUME)
        object.__setattr__(self, "quality_flags", tuple(flags))

    @property
    def close_time(self) -> datetime:
        return self.open_time + CANDLE_INTERVALS[self.interval]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ingest_version": self.ingest_version,
            "provider": self.provider,
            "account_environment": self.account_environment,
            "instrument": self.instrument,
            "side": self.side,
            "interval": self.interval,
            "open_time": self.open_time.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "received_at": self.received_at.isoformat(),
            "connection_id": self.connection_id,
            "writer_id": self.writer_id,
            "raw_payload_sha256": self.raw_payload_sha256,
            "source_endpoint_class": self.source_endpoint_class,
            "collection_mode": self.collection_mode,
            "quality_state": str(self.quality_state),
            "quality_flags": list(self.quality_flags),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CollectedCandle:
        return cls(
            provider=str(payload["provider"]),
            account_environment=str(payload["account_environment"]),
            instrument=str(payload["instrument"]),
            side=str(payload["side"]),
            interval=str(payload["interval"]),
            open_time=datetime.fromisoformat(str(payload["open_time"])),
            open=float(payload["open"]),
            high=float(payload["high"]),
            low=float(payload["low"]),
            close=float(payload["close"]),
            volume=None if payload.get("volume") is None else float(payload["volume"]),
            received_at=datetime.fromisoformat(str(payload["received_at"])),
            connection_id=str(payload["connection_id"]),
            writer_id=str(payload["writer_id"]),
            raw_payload_sha256=str(payload["raw_payload_sha256"]),
            source_endpoint_class=str(payload["source_endpoint_class"]),
            collection_mode=str(payload["collection_mode"]),
            quality_state=QualityState(str(payload.get("quality_state", "usable"))),
            quality_flags=tuple(payload.get("quality_flags", ())),
        )

    def with_quality(self, state: QualityState, *extra_flags: str) -> CollectedCandle:
        merged = tuple(dict.fromkeys((*self.quality_flags, *extra_flags)))
        return CollectedCandle(
            provider=self.provider,
            account_environment=self.account_environment,
            instrument=self.instrument,
            side=self.side,
            interval=self.interval,
            open_time=self.open_time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            received_at=self.received_at,
            connection_id=self.connection_id,
            writer_id=self.writer_id,
            raw_payload_sha256=self.raw_payload_sha256,
            source_endpoint_class=self.source_endpoint_class,
            collection_mode=self.collection_mode,
            quality_state=state,
            quality_flags=merged,
        )


@dataclass(frozen=True)
class ParsedCandles:
    """A provider parser's output: candles plus counted exclusions.

    ``padding_excluded`` is the number of provider padding rows (zero-volume,
    flat OHLC emitted for closed-market periods) the parser dropped;
    ``zero_width_excluded`` counts records dropped because the book had zero
    width (bid == ask) at a boundary and ``crossed_excluded`` counts records
    dropped for a strictly crossed boundary book (bid > ask) — both observed
    in real broker archives at point precision, and excluded rather than
    repaired or silently kept. The counts make every exclusion auditable:
    ``total_records`` must equal ``len(candles) + padding_excluded +
    zero_width_excluded + crossed_excluded``.
    """

    candles: tuple[CollectedCandle, ...]
    padding_excluded: int
    total_records: int
    zero_width_excluded: int = 0
    crossed_excluded: int = 0

    def __post_init__(self) -> None:
        accounted = (
            len(self.candles)
            + self.padding_excluded
            + self.zero_width_excluded
            + self.crossed_excluded
        )
        if accounted != self.total_records:
            raise CandleContractError(
                "parser accounting error: candles + padding_excluded + "
                "zero_width_excluded + crossed_excluded != total_records"
            )


def _append_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if not payload:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


class CandleLog:
    """Append-only JSONL logs (accepted + quarantine + padding notes) with
    duplicate/ordering detection scoped per (provider, instrument, side,
    interval)."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.accepted_path = self.directory / "candles.jsonl"
        self.quarantine_path = self.directory / "candle_quarantine.jsonl"
        self.padding_path = self.directory / "candle_padding.jsonl"
        self._seen: set[tuple[str, str, str, str, str]] = set()
        self._last_open: dict[tuple[str, str, str, str], datetime] = {}
        self._bootstrap()

    def _bootstrap(self) -> None:
        if not self.accepted_path.is_file():
            return
        for line in self.accepted_path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            self._seen.add(self._identity_from_dict(row))
            key = (
                str(row["provider"]),
                str(row["instrument"]),
                str(row["side"]),
                str(row["interval"]),
            )
            stamp = datetime.fromisoformat(str(row["open_time"]))
            previous = self._last_open.get(key)
            if previous is None or stamp > previous:
                self._last_open[key] = stamp

    @staticmethod
    def _identity_from_dict(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
        return (
            str(row["provider"]),
            str(row["instrument"]),
            str(row["side"]),
            str(row["interval"]),
            str(row["open_time"]),
        )

    @staticmethod
    def _identity(candle: CollectedCandle) -> tuple[str, str, str, str, str]:
        return (
            candle.provider,
            candle.instrument,
            candle.side,
            candle.interval,
            candle.open_time.isoformat(),
        )

    def classify(self, candle: CollectedCandle) -> CollectedCandle:
        identity = self._identity(candle)
        if identity in self._seen:
            return candle.with_quality(QualityState.QUARANTINED, FLAG_DUPLICATE_CANDLE)
        key = (candle.provider, candle.instrument, candle.side, candle.interval)
        last = self._last_open.get(key)
        if last is not None and candle.open_time < last:
            return candle.with_quality(QualityState.DEGRADED, FLAG_OUT_OF_ORDER_CANDLE)
        return candle

    def note_accepted(self, candle: CollectedCandle) -> None:
        """Register an accepted identity for duplicate/ordering detection."""

        self._seen.add(self._identity(candle))
        key = (candle.provider, candle.instrument, candle.side, candle.interval)
        last = self._last_open.get(key)
        if last is None or candle.open_time > last:
            self._last_open[key] = candle.open_time

    def record(self, candle: CollectedCandle) -> None:
        if candle.quality_state in (QualityState.USABLE, QualityState.DEGRADED):
            self.note_accepted(candle)
            _append_jsonl(self.accepted_path, [candle.to_dict()])
        else:
            _append_jsonl(self.quarantine_path, [candle.to_dict()])

    def append_batch(self, candles: Sequence[CollectedCandle]) -> None:
        """Durably append many already-classified-and-noted candles with ONE
        write per log file — a payload of thousands of candles costs two
        fsyncs instead of thousands. Callers must have run :meth:`classify`
        and :meth:`note_accepted` first (as :func:`ingest_candle_payload`
        does)."""

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for candle in candles:
            if candle.quality_state in (QualityState.USABLE, QualityState.DEGRADED):
                accepted.append(candle.to_dict())
            else:
                rejected.append(candle.to_dict())
        _append_jsonl(self.accepted_path, accepted)
        _append_jsonl(self.quarantine_path, rejected)

    def record_padding(self, note: dict[str, Any]) -> None:
        _append_jsonl(self.padding_path, [note])


@dataclass
class CandleIngestResult:
    raw_sha256: str
    accepted: list[CollectedCandle] = field(default_factory=list)
    quarantined: list[dict[str, Any]] = field(default_factory=list)
    padding_excluded: int = 0

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)


CandleParser = Callable[[bytes], ParsedCandles]


def ingest_candle_payload(
    raw_payload: bytes,
    *,
    parser: CandleParser,
    store: ImmutableRawStore,
    log: CandleLog,
    now: Callable[[], datetime] | None = None,
) -> CandleIngestResult:
    """Run one candle payload through the mandatory raw-first order."""

    clock = now or (lambda: datetime.now(UTC))
    ref = store.put(raw_payload)
    stored = store.get(ref.sha256)
    observed = hashlib.sha256(stored).hexdigest()
    if observed != ref.sha256:
        raise RuntimeError(f"raw hash verification failed: {observed} != {ref.sha256}")
    result = CandleIngestResult(raw_sha256=ref.sha256)
    try:
        parsed = parser(raw_payload)
    except (CandleContractError, ValueError, KeyError, TypeError) as error:
        record = {
            "raw_sha256": ref.sha256,
            "reason": "schema_validation_failed",
            "detail": str(error)[:500],
            "occurred_at": clock().isoformat(),
        }
        log.quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        _append_jsonl(log.quarantine_path, [record])
        result.quarantined.append(record)
        return result
    result.padding_excluded = parsed.padding_excluded
    if parsed.padding_excluded:
        log.record_padding(
            {
                "raw_sha256": ref.sha256,
                "padding_excluded": parsed.padding_excluded,
                "total_records": parsed.total_records,
                "reason": "provider_padded_zero_volume_flat_candles",
                "occurred_at": clock().isoformat(),
            }
        )
    classified_batch: list[CollectedCandle] = []
    for candle in parsed.candles:
        if candle.raw_payload_sha256 != ref.sha256:
            record = {
                "raw_sha256": ref.sha256,
                "reason": "raw_hash_mismatch",
                "detail": (
                    f"candle cites {candle.raw_payload_sha256[:12]}… but payload is "
                    f"{ref.sha256[:12]}…"
                ),
                "occurred_at": clock().isoformat(),
            }
            _append_jsonl(log.quarantine_path, [record])
            result.quarantined.append(record)
            continue
        classified = log.classify(candle)
        classified_batch.append(classified)
        if classified.quality_state in (QualityState.USABLE, QualityState.DEGRADED):
            log.note_accepted(classified)
            result.accepted.append(classified)
        else:
            result.quarantined.append(
                {
                    "raw_sha256": ref.sha256,
                    "reason": ",".join(classified.quality_flags) or str(classified.quality_state),
                    "detail": f"{classified.instrument} {classified.side} candle quarantined",
                    "occurred_at": clock().isoformat(),
                }
            )
    log.append_batch(classified_batch)
    return result
