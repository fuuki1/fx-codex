"""Independent raw-first verification for delayed historical label paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path

from data_platform.collect.dukascopy import HourContext, parse_hour_payload
from data_platform.raw.immutable_store import ImmutableRawStore, RawStoreError

from .virtual_portfolio_replay import (
    LABEL_PATH_EVIDENCE_CONTRACT,
    ReplayDataError,
    ReplayTick,
    horizon_for_timeframe,
    load_quote_tape,
    quote_log_prefix_sha256,
)

RAW_PATH_VERIFICATION_CONTRACT = "canonical-label-raw-path-verification-v1"
RAW_REPLAYED_LABEL_QUALITY = "raw_replayed_research_path"
_SHA256_LENGTH = 64
_TOLERANCE = 1e-8


class CanonicalPathVerificationError(ValueError):
    """The immutable raw inputs do not reproduce the stored label path."""


@dataclass(frozen=True)
class CanonicalPathVerification:
    """Deterministic attestation emitted only after raw-first replay succeeds."""

    contract: str
    quote_log_prefix_size_bytes: int
    quote_log_prefix_sha256: str
    path_tick_count: int
    path_source_ids_sha256: str
    path_raw_payload_hashes_sha256: str
    raw_blob_count: int
    raw_blob_hashes_sha256: str
    exit_trigger: str
    exit_time: str
    exit_bid: float
    exit_ask: float
    exit_executable: float
    mae_r: float
    mfe_r: float
    research_only: bool = True
    promotion_eligible: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "quote_log_prefix_size_bytes": self.quote_log_prefix_size_bytes,
            "quote_log_prefix_sha256": self.quote_log_prefix_sha256,
            "path_tick_count": self.path_tick_count,
            "path_source_ids_sha256": self.path_source_ids_sha256,
            "path_raw_payload_hashes_sha256": self.path_raw_payload_hashes_sha256,
            "raw_blob_count": self.raw_blob_count,
            "raw_blob_hashes_sha256": self.raw_blob_hashes_sha256,
            "exit_trigger": self.exit_trigger,
            "exit_time": self.exit_time,
            "exit_bid": self.exit_bid,
            "exit_ask": self.exit_ask,
            "exit_executable": self.exit_executable,
            "mae_r": self.mae_r,
            "mfe_r": self.mfe_r,
            "research_only": self.research_only,
            "promotion_eligible": self.promotion_eligible,
        }


def verify_virtual_label_path(
    row: Mapping[str, object],
    *,
    quote_log: str | Path,
    raw_store_root: str | Path,
) -> CanonicalPathVerification:
    """Reparse immutable Dukascopy blobs and independently reproduce one close."""

    attribution = _mapping(row.get("attribution"), "attribution")
    replay = _mapping(attribution.get("replay_evidence"), "replay_evidence")
    if replay.get("label_path_evidence_contract") != LABEL_PATH_EVIDENCE_CONTRACT:
        raise CanonicalPathVerificationError("raw path evidence contract is unavailable")
    prefix_size = _positive_integer(replay.get("source_size_bytes"), "source_size_bytes")
    expected_prefix_hash = _sha256(
        replay.get("source_prefix_sha256"),
        "source_prefix_sha256",
    )
    actual_prefix_hash = quote_log_prefix_sha256(quote_log, prefix_size)
    if actual_prefix_hash != expected_prefix_hash:
        raise CanonicalPathVerificationError("quote log prefix SHA-256 mismatch")

    symbol = _text(row.get("symbol"), "symbol").upper()
    direction = _text(row.get("direction"), "direction")
    if direction not in {"long", "short"}:
        raise CanonicalPathVerificationError("direction must be long or short")
    prediction = _aware(row.get("prediction_time"), "prediction_time")
    open_time = _aware(row.get("simulated_entry_time"), "simulated_entry_time")
    close_time = _aware(row.get("label_end_time"), "label_end_time")
    as_of = _aware(row.get("label_available_time"), "label_available_time")
    horizon_end = prediction + horizon_for_timeframe(row.get("timeframe"))
    selection_end = max(close_time, horizon_end)
    try:
        tape = load_quote_tape(
            quote_log,
            symbols=(symbol,),
            start=open_time,
            end=selection_end,
            as_of=as_of,
            source_size_bytes=prefix_size,
        )
    except ReplayDataError as error:
        raise CanonicalPathVerificationError(str(error)) from error
    if tape.source_prefix_sha256 != expected_prefix_hash:
        raise CanonicalPathVerificationError("loaded quote prefix identity changed")
    ticks = tape.ticks.get(symbol, ())
    path = tuple(tick for tick in ticks if open_time < tick.event_time <= close_time)
    if not path:
        raise CanonicalPathVerificationError("stored close has no executable quote path")

    _verify_raw_preimages(
        path,
        raw_store_root=raw_store_root,
        open_time=open_time,
        close_time=close_time,
    )
    entry = _number(row.get("entry_executable"), "entry_executable")
    stop = _number(row.get("stop_price"), "stop_price")
    target = _number(row.get("target_price"), "target_price")
    risk_distance = abs(entry - stop)
    if risk_distance <= 0:
        raise CanonicalPathVerificationError("risk distance must be positive")
    close_reason = _text(row.get("close_reason"), "close_reason")
    expected_exit = _independent_exit(
        path,
        all_ticks=ticks,
        watermark=tape.watermark.get(symbol),
        direction=direction,
        stop=stop,
        target=target,
        close_reason=close_reason,
        horizon_end=horizon_end,
        replay=replay,
    )
    if expected_exit.event_time != close_time or expected_exit != path[-1]:
        raise CanonicalPathVerificationError("independent replay selected a different exit tick")

    executable_prices = [_executable(tick, direction) for tick in path]
    if direction == "long":
        mfe = (max(executable_prices) - entry) / risk_distance
        mae = (entry - min(executable_prices)) / risk_distance
    else:
        mfe = (entry - min(executable_prices)) / risk_distance
        mae = (max(executable_prices) - entry) / risk_distance
    mfe = max(0.0, mfe)
    mae = max(0.0, mae)
    _same_required(row.get("maximum_favorable_excursion_r"), mfe, "mfe_r")
    _same_required(row.get("maximum_adverse_excursion_r"), mae, "mae_r")
    _same_required(row.get("exit_bid"), expected_exit.bid, "exit_bid")
    _same_required(row.get("exit_ask"), expected_exit.ask, "exit_ask")
    exit_executable = _executable(expected_exit, direction)
    _same_required(row.get("exit_executable"), exit_executable, "exit_executable")

    source_ids_hash = _sha256_json([tick.source_record_id for tick in path])
    raw_path_hash = _sha256_json([tick.raw_payload_sha256 for tick in path])
    expected_count = _positive_integer(
        replay.get("path_tick_count_to_exit"),
        "path_tick_count_to_exit",
    )
    if expected_count != len(path):
        raise CanonicalPathVerificationError("path tick count mismatch")
    if _sha256(replay.get("path_source_ids_sha256"), "path_source_ids_sha256") != source_ids_hash:
        raise CanonicalPathVerificationError("ordered path source-id hash mismatch")
    if (
        _sha256(
            replay.get("path_raw_payload_hashes_sha256"),
            "path_raw_payload_hashes_sha256",
        )
        != raw_path_hash
    ):
        raise CanonicalPathVerificationError("ordered raw-payload path hash mismatch")
    if replay.get("exit_trigger") != close_reason:
        raise CanonicalPathVerificationError("stored replay trigger does not match close reason")
    ordered_blobs = tuple(dict.fromkeys(tick.raw_payload_sha256 for tick in path))
    return CanonicalPathVerification(
        contract=RAW_PATH_VERIFICATION_CONTRACT,
        quote_log_prefix_size_bytes=prefix_size,
        quote_log_prefix_sha256=expected_prefix_hash,
        path_tick_count=len(path),
        path_source_ids_sha256=source_ids_hash,
        path_raw_payload_hashes_sha256=raw_path_hash,
        raw_blob_count=len(ordered_blobs),
        raw_blob_hashes_sha256=_sha256_json(ordered_blobs),
        exit_trigger=close_reason,
        exit_time=expected_exit.event_time.isoformat(),
        exit_bid=expected_exit.bid,
        exit_ask=expected_exit.ask,
        exit_executable=exit_executable,
        mae_r=mae,
        mfe_r=mfe,
    )


def _verify_raw_preimages(
    path: tuple[ReplayTick, ...],
    *,
    raw_store_root: str | Path,
    open_time: datetime,
    close_time: datetime,
) -> None:
    store = ImmutableRawStore(raw_store_root)
    grouped: dict[str, list[ReplayTick]] = {}
    for tick in path:
        grouped.setdefault(tick.raw_payload_sha256, []).append(tick)
    for raw_hash, ticks in grouped.items():
        try:
            payload = store.get(raw_hash)
        except (OSError, RawStoreError) as error:
            raise CanonicalPathVerificationError(
                f"immutable raw blob is unavailable: {raw_hash}"
            ) from error
        available_times = {tick.available_time for tick in ticks}
        if len(available_times) != 1:
            raise CanonicalPathVerificationError("one raw blob has conflicting availability times")
        first = ticks[0]
        hour_start = first.event_time.replace(minute=0, second=0, microsecond=0)
        try:
            reparsed = parse_hour_payload(
                payload,
                HourContext(
                    instrument=first.symbol,
                    hour_start_utc=hour_start,
                    received_at=first.available_time,
                    connection_id="canonical-path-verifier",
                ),
            )
        except (OSError, ValueError) as error:
            raise CanonicalPathVerificationError(
                f"immutable raw blob cannot be parsed: {raw_hash}"
            ) from error
        raw_quotes = [
            (
                quote.provider_event_time,
                quote.bid,
                quote.ask,
                quote.raw_payload_sha256,
            )
            for quote in reparsed
            if quote.provider_event_time is not None
            and open_time < quote.provider_event_time <= close_time
        ]
        normalized = [
            (tick.event_time, tick.bid, tick.ask, tick.raw_payload_sha256) for tick in ticks
        ]
        # Dukascopy may contain more than one record at the same millisecond.
        # Timestamp-only deduplication would allow an omitted same-ms stop or
        # target tick to pass verification.  Compare cardinality, order, and
        # values against every raw record in the selected slice instead.
        if normalized != raw_quotes:
            raise CanonicalPathVerificationError(
                "normalized path does not exactly reproduce its immutable raw blob slice"
            )


def _independent_exit(
    path: tuple[ReplayTick, ...],
    *,
    all_ticks: tuple[ReplayTick, ...],
    watermark: datetime | None,
    direction: str,
    stop: float,
    target: float,
    close_reason: str,
    horizon_end: datetime,
    replay: Mapping[str, object],
) -> ReplayTick:
    for tick in path:
        executable = _executable(tick, direction)
        stop_hit = executable <= stop if direction == "long" else executable >= stop
        target_hit = executable >= target if direction == "long" else executable <= target
        if stop_hit or target_hit:
            expected_reason = "stop_loss" if stop_hit else "target1"
            if close_reason != expected_reason:
                raise CanonicalPathVerificationError(
                    "an earlier stop/target contradicts close reason"
                )
            return tick
    if close_reason == "time_exit":
        if watermark is None or watermark < horizon_end:
            raise CanonicalPathVerificationError(
                "time exit was not mature in the pinned quote prefix"
            )
        candidates = tuple(tick for tick in all_ticks if tick.event_time <= horizon_end)
        if not candidates:
            raise CanonicalPathVerificationError("time exit has no horizon quote")
        return candidates[-1]
    if close_reason == "session_end":
        cutoff = _aware(replay.get("session_cutoff"), "session_cutoff")
        candidates = tuple(tick for tick in all_ticks if tick.event_time >= cutoff)
        if not candidates:
            raise CanonicalPathVerificationError("session exit has no quote at-or-after cutoff")
        if candidates[0].event_time > cutoff + timedelta(minutes=5):
            raise CanonicalPathVerificationError(
                "session exit quote is more than five minutes after cutoff"
            )
        return candidates[0]
    raise CanonicalPathVerificationError("stored close reason is not independently reproduced")


def _executable(tick: ReplayTick, direction: str) -> float:
    return tick.bid if direction == "long" else tick.ask


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CanonicalPathVerificationError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CanonicalPathVerificationError(f"{name} is required")
    return text


def _aware(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise CanonicalPathVerificationError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CanonicalPathVerificationError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanonicalPathVerificationError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise CanonicalPathVerificationError(f"{name} must be finite")
    return number


def _positive_integer(value: object, name: str) -> int:
    number = _number(value, name)
    integer = int(number)
    if integer <= 0 or number != integer:
        raise CanonicalPathVerificationError(f"{name} must be a positive integer")
    return integer


def _sha256(value: object, name: str) -> str:
    digest = _text(value, name).lower()
    if len(digest) != _SHA256_LENGTH or any(c not in "0123456789abcdef" for c in digest):
        raise CanonicalPathVerificationError(f"{name} must be a SHA-256 digest")
    return digest


def _same_required(value: object, expected: float, name: str) -> None:
    actual = _number(value, name)
    if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=_TOLERANCE):
        raise CanonicalPathVerificationError(f"{name} does not match independent raw replay")


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CanonicalPathVerification",
    "CanonicalPathVerificationError",
    "RAW_PATH_VERIFICATION_CONTRACT",
    "RAW_REPLAYED_LABEL_QUALITY",
    "verify_virtual_label_path",
]
