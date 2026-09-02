"""Delayed bid/ask replay for the analysis-only virtual portfolio.

This module is the only automatic transaction-to-settlement bridge.  It reads
the repository's already-collected Dukascopy historical ticks, never contacts a
broker, and never presents delayed data as a contemporaneous/paper fill.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import heapq
import json
import math
from pathlib import Path
import re
from typing import Any

from data_platform.collect.contract import CollectedQuote, QualityState, QuoteContractError

from .quote_tape_index import QuoteIndexError, load_indexed_quote_payloads
from .virtual_portfolio import (
    PositionMarkRequest,
    QuoteSnapshot,
    TradeCloseRequest,
    VirtualPortfolioPolicy,
    VirtualPortfolioStore,
    classify_virtual_horizon,
)
from .virtual_portfolio_valuation import append_synchronized_valuation_from_marks

REPLAY_CONTRACT = "virtual-portfolio-delayed-bid-ask-replay-v1"
LABEL_PATH_EVIDENCE_CONTRACT = "canonical-label-path-evidence-v1"
MODEL_DESCRIPTOR_CONTRACT = "virtual-decision-producer-descriptor-v1"
_CONTEXT_SHA_RE = re.compile(r"(?:^|:)sha256:([0-9a-f]{64})$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_HORIZONS = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "8h": timedelta(hours=8),
    "1d": timedelta(days=1),
}


class ReplayDataError(RuntimeError):
    """The quote tape or replay inputs cannot support a fail-closed outcome."""


@dataclass(frozen=True)
class VirtualReplayCostPolicy:
    """Explicit conservative modeled costs in addition to measured spread."""

    cost_model_id: str = "virtual-replay-cost-policy"
    version: str = "v1"
    slippage_r_bps: int = 200
    commission_r_bps: int = 100
    financing_r_bps_per_started_day: int = 100
    conversion_r_bps: int = 50

    def validate(self) -> None:
        if not self.cost_model_id or not self.version:
            raise ReplayDataError("cost policy identity is required")
        for name, value in asdict(self).items():
            if name in {"cost_model_id", "version"}:
                continue
            if not isinstance(value, int) or value < 0:
                raise ReplayDataError(f"{name} must be a non-negative integer")

    @property
    def sha256(self) -> str:
        return _sha256_json(asdict(self))


@dataclass(frozen=True)
class ReplayTick:
    symbol: str
    event_time: datetime
    available_time: datetime
    bid: float
    ask: float
    raw_payload_sha256: str
    revision_id: str
    sequence_id: int | None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def source_record_id(self) -> str:
        suffix = (
            str(self.sequence_id) if self.sequence_id is not None else self.raw_payload_sha256[:16]
        )
        return f"dukascopy:{self.symbol}:{self.event_time.isoformat()}:{suffix}"

    def snapshot(self) -> QuoteSnapshot:
        return QuoteSnapshot(
            source_record_id=self.source_record_id,
            source="dukascopy_historical_datafeed",
            revision_id=self.revision_id,
            event_time=self.event_time,
            available_time=self.available_time,
            ingested_time=self.available_time,
            bid=self.bid,
            ask=self.ask,
        )


@dataclass(frozen=True)
class QuoteTape:
    ticks: Mapping[str, tuple[ReplayTick, ...]]
    watermark: Mapping[str, datetime]
    source_path: str
    source_size_bytes: int
    source_prefix_sha256: str
    rejected_rows: int
    index_path: str | None = None
    index_lag_bytes: int = 0
    indexed_at: datetime | None = None


def _tape_source_evidence(tape: QuoteTape) -> dict[str, object]:
    return {
        "source_path": tape.source_path,
        "source_size_bytes": tape.source_size_bytes,
        "source_prefix_sha256": tape.source_prefix_sha256,
        "index_path": tape.index_path,
        "index_lag_bytes": tape.index_lag_bytes,
        "indexed_at": tape.indexed_at.isoformat() if tape.indexed_at is not None else None,
    }


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def quote_log_prefix_sha256(path: str | Path, size_bytes: int) -> str:
    """Hash exactly one immutable prefix of an append-only quote log."""

    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise ReplayDataError("quote log prefix size must be a non-negative integer")
    digest = hashlib.sha256()
    remaining = size_bytes
    try:
        with Path(path).open("rb") as handle:
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ReplayDataError("quote log is shorter than its recorded prefix")
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError as error:
        raise ReplayDataError(f"historical quote log cannot be hashed: {error}") from error
    return digest.hexdigest()


def _quote_log_prefix_lines(path: Path, size_bytes: int) -> Iterable[str]:
    """Yield complete UTF-8 lines without crossing the pinned prefix boundary."""

    consumed = 0
    try:
        with path.open("rb") as handle:
            while consumed < size_bytes:
                remaining = size_bytes - consumed
                raw_line = handle.readline(remaining + 1)
                if not raw_line:
                    raise ReplayDataError("quote log is shorter than its recorded prefix")
                if len(raw_line) > remaining:
                    raise ReplayDataError("quote log prefix ends inside a JSONL record")
                consumed += len(raw_line)
                try:
                    yield raw_line.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ReplayDataError("historical quote log is not valid UTF-8") from error
    except OSError as error:
        raise ReplayDataError(f"historical quote log cannot be read: {error}") from error


def _aware(value: object, name: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ReplayDataError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayDataError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _number(value: object, name: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError) as error:
        raise ReplayDataError(f"{name} must be numeric") from error
    if not math.isfinite(number):
        raise ReplayDataError(f"{name} must be finite")
    return number


def horizon_for_timeframe(timeframe: object) -> timedelta:
    normalized = str(timeframe or "").strip().lower()
    try:
        return _HORIZONS[normalized]
    except KeyError as error:
        raise ReplayDataError(f"unsupported replay timeframe: {normalized or 'missing'}") from error


def enrich_decision_artifacts(
    decision: Mapping[str, object],
    *,
    portfolio_policy: VirtualPortfolioPolicy,
) -> dict[str, object]:
    """Attach reproducible artifact identities without inventing market data."""

    enriched = dict(decision)
    context_id = str(enriched.get("input_context_id") or "")
    context_match = _CONTEXT_SHA_RE.search(context_id)
    existing_data_hash = str(enriched.get("data_sha256") or "")
    if context_match:
        data_hash = context_match.group(1)
        data_basis = "input_context_id embedded SHA-256"
    elif _SHA_RE.fullmatch(existing_data_hash):
        data_hash = existing_data_hash
        data_basis = "upstream explicit data_sha256"
    else:
        data_hash = ""
        data_basis = "unavailable"

    descriptor = {
        "contract": MODEL_DESCRIPTOR_CONTRACT,
        "producer": str(enriched.get("producer") or ""),
        "producer_version": str(enriched.get("producer_version") or ""),
        "mode": str(enriched.get("mode") or ""),
        "pit_contract": str(enriched.get("pit_contract") or ""),
        "timeframe": str(enriched.get("timeframe") or ""),
        "decision_schema": sorted(
            str(key)
            for key in enriched
            if key
            not in {
                "data_sha256",
                "model_sha256",
                "policy_sha256",
                "artifact_provenance",
            }
        ),
    }
    existing_model_hash = str(enriched.get("model_sha256") or "")
    model_hash = (
        existing_model_hash if _SHA_RE.fullmatch(existing_model_hash) else _sha256_json(descriptor)
    )
    enriched.update(
        {
            "data_sha256": data_hash,
            "model_sha256": model_hash,
            "policy_sha256": portfolio_policy.policy_sha256,
            "artifact_provenance": {
                "data_sha256_basis": data_basis,
                "model_sha256_basis": (
                    "upstream explicit model_sha256"
                    if _SHA_RE.fullmatch(existing_model_hash)
                    else "deterministic producer descriptor; not a trained-model claim"
                ),
                "model_descriptor": descriptor,
                "policy_sha256_basis": "virtual portfolio admission policy",
                "replay_contract": REPLAY_CONTRACT,
            },
        }
    )
    return enriched


def _strict_tick(payload: Mapping[str, Any], *, as_of: datetime) -> ReplayTick | None:
    try:
        quote = CollectedQuote.from_dict(payload)
    except (KeyError, TypeError, ValueError, QuoteContractError):
        return None
    if (
        quote.provider != "dukascopy"
        or quote.account_environment != "datafeed"
        or quote.source_endpoint_class != "historical_datafeed"
        or quote.collection_mode != "historical_download"
        or quote.quality_state is not QualityState.USABLE
        or quote.provider_event_time is None
        or quote.received_at > as_of
        or quote.provider_event_time > quote.received_at
    ):
        return None
    revision = quote.revision_id or quote.raw_payload_sha256
    return ReplayTick(
        symbol=quote.instrument.upper().replace("/", "").replace("_", ""),
        event_time=quote.provider_event_time,
        available_time=quote.received_at,
        bid=quote.bid,
        ask=quote.ask,
        raw_payload_sha256=quote.raw_payload_sha256,
        revision_id=revision,
        sequence_id=quote.sequence_id,
    )


def load_quote_tape(
    path: str | Path,
    *,
    symbols: Iterable[str],
    start: datetime,
    end: datetime,
    as_of: datetime,
    index_path: str | Path | None = None,
    source_size_bytes: int | None = None,
) -> QuoteTape:
    """Stream a bounded, provenance-checked slice from the append-only quote log."""

    target = Path(path).resolve()
    if not target.is_file():
        raise ReplayDataError(f"historical quote log does not exist: {target}")
    start_utc = _aware(start, "tape start")
    end_utc = _aware(end, "tape end")
    as_of_utc = _aware(as_of, "tape as_of")
    if end_utc < start_utc:
        raise ReplayDataError("quote tape window is reversed")
    wanted = {str(symbol).upper() for symbol in symbols}
    selected: dict[str, list[ReplayTick]] = {symbol: [] for symbol in wanted}
    watermark: dict[str, datetime] = {}
    rejected = 0
    indexed_at: datetime | None = None
    resolved_index: str | None = None
    index_lag_bytes = 0
    current_size_bytes = target.stat().st_size
    if source_size_bytes is not None:
        if (
            isinstance(source_size_bytes, bool)
            or not isinstance(source_size_bytes, int)
            or source_size_bytes < 0
            or source_size_bytes > current_size_bytes
        ):
            raise ReplayDataError("recorded quote log prefix is outside the current file")
        if index_path is not None:
            raise ReplayDataError("a pinned quote prefix cannot use the mutable current index")
        indexed_size_bytes = source_size_bytes
    else:
        indexed_size_bytes = current_size_bytes
    prefix_hash_before = (
        quote_log_prefix_sha256(target, indexed_size_bytes) if index_path is None else ""
    )
    if index_path is None:
        for line in _quote_log_prefix_lines(target, indexed_size_bytes):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                rejected += 1
                continue
            if not isinstance(payload, Mapping):
                rejected += 1
                continue
            raw_symbol = str(payload.get("instrument") or "").upper().replace("/", "")
            if raw_symbol not in wanted:
                continue
            tick = _strict_tick(payload, as_of=as_of_utc)
            if tick is None:
                rejected += 1
                continue
            previous = watermark.get(tick.symbol)
            if previous is None or tick.event_time > previous:
                watermark[tick.symbol] = tick.event_time
            if start_utc <= tick.event_time <= end_utc:
                selected.setdefault(tick.symbol, []).append(tick)
    else:
        try:
            indexed = load_indexed_quote_payloads(
                target,
                index_path,
                symbols=wanted,
                start=start_utc,
                end=end_utc,
                as_of=as_of_utc,
            )
        except QuoteIndexError as error:
            raise ReplayDataError(f"historical quote index unavailable: {error}") from error
        rejected = indexed.rejected_rows
        watermark.update(indexed.watermark)
        indexed_at = indexed.indexed_at
        resolved_index = indexed.index_path
        index_lag_bytes = indexed.index_lag_bytes
        indexed_size_bytes = indexed.indexed_size_bytes
        prefix_hash_before = quote_log_prefix_sha256(target, indexed_size_bytes)
        for payload in indexed.payloads:
            tick = _strict_tick(payload, as_of=as_of_utc)
            if tick is None:
                raise ReplayDataError("indexed historical quote failed replay validation")
            selected.setdefault(tick.symbol, []).append(tick)
    normalized: dict[str, tuple[ReplayTick, ...]] = {}
    for symbol, rows in selected.items():
        rows.sort(
            key=lambda tick: (
                tick.event_time,
                tick.sequence_id if tick.sequence_id is not None else 2**63 - 1,
                tick.raw_payload_sha256,
            )
        )
        deduplicated: list[ReplayTick] = []
        identities: set[tuple[datetime, str]] = set()
        for tick in rows:
            identity = (tick.event_time, tick.raw_payload_sha256)
            if identity in identities:
                continue
            identities.add(identity)
            deduplicated.append(tick)
        normalized[symbol] = tuple(deduplicated)
    prefix_hash_after = quote_log_prefix_sha256(target, indexed_size_bytes)
    if prefix_hash_after != prefix_hash_before:
        raise ReplayDataError("quote log prefix changed while it was being loaded")
    return QuoteTape(
        ticks=normalized,
        watermark=watermark,
        source_path=str(target),
        source_size_bytes=indexed_size_bytes,
        source_prefix_sha256=prefix_hash_after,
        rejected_rows=rejected,
        index_path=resolved_index,
        index_lag_bytes=index_lag_bytes,
        indexed_at=indexed_at,
    )


def _first_at_or_after(
    tape: QuoteTape,
    symbol: str,
    start: datetime,
    end: datetime,
) -> ReplayTick | None:
    rows = tape.ticks.get(symbol, ())
    index = bisect_left(rows, start, key=lambda tick: tick.event_time)
    if index >= len(rows) or rows[index].event_time > end:
        return None
    return rows[index]


def _latest_at_or_before(
    tape: QuoteTape,
    symbol: str,
    start: datetime,
    end: datetime,
) -> ReplayTick | None:
    rows = tape.ticks.get(symbol, ())
    first = bisect_left(rows, start, key=lambda tick: tick.event_time)
    after_last = bisect_right(rows, end, key=lambda tick: tick.event_time)
    if after_last <= first:
        return None
    return rows[after_last - 1]


def _ticks_between(
    tape: QuoteTape,
    symbol: str,
    *,
    after: datetime,
    through: datetime,
) -> tuple[ReplayTick, ...]:
    """Return (after, through] without scanning the prefix of a large tape."""

    rows = tape.ticks.get(symbol, ())
    start = bisect_right(rows, after, key=lambda tick: tick.event_time)
    end = bisect_right(rows, through, key=lambda tick: tick.event_time)
    return rows[start:end]


def _conversion_at(
    tape: QuoteTape,
    *,
    symbol: str,
    moment: datetime,
    max_delay: timedelta,
) -> tuple[float | None, str | None, datetime | None]:
    if symbol[3:] == "JPY":
        return 1.0, "JPY quote currency; conversion not required", None
    if symbol[3:] != "USD":
        return None, None, None
    conversion = _first_at_or_after(tape, "USDJPY", moment, moment + max_delay)
    if conversion is None:
        return None, None, None
    return (
        conversion.mid,
        f"{conversion.source_record_id}; executable USDJPY mid replay",
        conversion.available_time,
    )


def _path_outcome(
    trade: Mapping[str, object],
    *,
    tape: QuoteTape,
) -> tuple[ReplayTick, str, float, float, dict[str, object]] | None:
    symbol = str(trade["symbol"])
    direction = str(trade["direction"])
    open_time = _aware(trade["open_time"], "trade open_time")
    decision_time = _aware(trade["decision_time"], "trade decision_time")
    horizon = horizon_for_timeframe(trade["timeframe"])
    horizon_end = decision_time + horizon
    watermark = tape.watermark.get(symbol)
    if watermark is None:
        return None
    available_end = min(watermark, horizon_end)
    path = _ticks_between(tape, symbol, after=open_time, through=available_end)
    if not path:
        if watermark >= horizon_end:
            raise ReplayDataError(f"mature quote path is empty for {trade['trade_id']}")
        return None
    stop = _number(trade["stop_price"], "stop_price")
    target = _number(trade["target_price"], "target_price")
    entry = _number(trade["entry_executable"], "entry_executable")
    risk_distance = abs(entry - stop)
    if risk_distance <= 0:
        raise ReplayDataError(f"zero risk distance for {trade['trade_id']}")
    exit_index: int | None = None
    close_reason: str | None = None
    for index, tick in enumerate(path):
        executable = tick.bid if direction == "long" else tick.ask
        stop_hit = executable <= stop if direction == "long" else executable >= stop
        target_hit = executable >= target if direction == "long" else executable <= target
        if stop_hit or target_hit:
            exit_index = index
            close_reason = "stop_loss" if stop_hit else "target1"
            break
    if exit_index is None:
        if watermark < horizon_end:
            return None
        exit_index = len(path) - 1
        close_reason = "time_exit"
    exit_path = path[: exit_index + 1]
    exit_tick = exit_path[-1]
    executable_prices = [tick.bid if direction == "long" else tick.ask for tick in exit_path]
    if direction == "long":
        mfe = (max(executable_prices) - entry) / risk_distance
        mae = (entry - min(executable_prices)) / risk_distance
    else:
        mfe = (entry - min(executable_prices)) / risk_distance
        mae = (max(executable_prices) - entry) / risk_distance
    assert close_reason is not None
    evidence = {
        "contract": REPLAY_CONTRACT,
        **_tape_source_evidence(tape),
        "path_start": path[0].event_time.isoformat(),
        "path_end": exit_tick.event_time.isoformat(),
        "horizon_end": horizon_end.isoformat(),
        "path_tick_count_to_exit": len(exit_path),
        "path_source_ids_sha256": _sha256_json([tick.source_record_id for tick in exit_path]),
        "label_path_evidence_contract": LABEL_PATH_EVIDENCE_CONTRACT,
        "path_raw_payload_hashes_sha256": _sha256_json(
            [tick.raw_payload_sha256 for tick in exit_path]
        ),
        "exit_trigger": close_reason,
        "gap_policy": "first_observed_executable_tick; no optimistic stop/target cap",
        "same_tick_policy": "exact ticks are strictly ordered; first observed trigger wins",
        "data_mode": "delayed_historical_download",
    }
    return exit_tick, close_reason, max(0.0, mfe), max(0.0, mae), evidence


def _modeled_costs(
    *,
    planned_risk_jpy: int,
    symbol: str,
    open_time: datetime,
    close_time: datetime,
    policy: VirtualReplayCostPolicy,
) -> dict[str, int]:
    def amount(bps: int) -> int:
        return math.ceil(planned_risk_jpy * bps / 10_000)

    started_days = max(1, math.ceil((close_time - open_time).total_seconds() / 86_400))
    return {
        "slippage_cost_jpy": amount(policy.slippage_r_bps),
        "commission_jpy": amount(policy.commission_r_bps),
        "financing_jpy": amount(policy.financing_r_bps_per_started_day * started_days),
        "conversion_cost_jpy": (0 if symbol[3:] == "JPY" else amount(policy.conversion_r_bps)),
    }


def _close_observed_at(
    trade: Mapping[str, object],
    *,
    tick_available_time: datetime,
    conversion_available_time: datetime | None,
) -> datetime:
    """決済を我々が知り得た時刻。

    `close_time`(決済が成立した市場時刻)とは別に、「いつ観測できたか」を返す。
    決済の根拠は終了tickと換算レートなので、その取込時刻より前には知り得ない。

    さらに**建玉を知った時刻(`open_known_at`)より前にはなり得ない**。
    取込ラグ(実測 中央値97分)がホライズンより長い短期足では、終了tickの
    取込がわずかに先行して `close observed_at cannot precede open known_at`
    が発火し、その建玉が**永久に決済できずスロットを占有し続けた**
    (実測: 15m 建玉が horizon の 33.6 倍=8.4時間占有)。

    close_time は動かさないので損益も PIT 整合性も変わらない。動かすのは
    「知り得た時刻」だけで、これは event_time / available_time を分離する
    既存の収集契約と同じ考え方。
    """

    observed = max(
        tick_available_time,
        conversion_available_time or tick_available_time,
    )
    raw_open_known_at = trade.get("open_known_at")
    if raw_open_known_at is None:
        return observed
    return max(observed, _aware(raw_open_known_at, "open_known_at"))


def _close_request(
    trade: Mapping[str, object],
    *,
    tape: QuoteTape,
    cost_policy: VirtualReplayCostPolicy,
) -> TradeCloseRequest | None:
    outcome = _path_outcome(trade, tape=tape)
    if outcome is None:
        return None
    tick, close_reason, mfe, mae, evidence = outcome
    rate, source, conversion_available_time = _conversion_at(
        tape,
        symbol=str(trade["symbol"]),
        moment=tick.event_time,
        max_delay=timedelta(minutes=5),
    )
    if rate is None or source is None:
        return None
    costs = _modeled_costs(
        planned_risk_jpy=int(str(trade["planned_risk_jpy"])),
        symbol=str(trade["symbol"]),
        open_time=_aware(trade["open_time"], "trade open_time"),
        close_time=tick.event_time,
        policy=cost_policy,
    )
    return TradeCloseRequest(
        trade_id=str(trade["trade_id"]),
        close_time=tick.event_time,
        quote=tick.snapshot(),
        quote_to_jpy_rate=rate,
        quote_conversion_source=source,
        close_reason=close_reason,
        cost_model_id=cost_policy.cost_model_id,
        cost_model_version=cost_policy.version,
        cost_source=(
            "measured Dukascopy bid/ask spread plus conservative deterministic "
            "research-only R-based assumptions"
        ),
        costs_complete=True,
        maximum_favorable_excursion_r=mfe,
        maximum_adverse_excursion_r=mae,
        historical_replay=True,
        observed_at=_close_observed_at(
            trade,
            tick_available_time=tick.available_time,
            conversion_available_time=conversion_available_time,
        ),
        replay_evidence={
            **evidence,
            "cost_policy_sha256": cost_policy.sha256,
            "cost_policy": asdict(cost_policy),
        },
        **costs,
    )


def _session_close_request(
    trade: Mapping[str, object],
    *,
    tape: QuoteTape,
    cutoff_time: datetime,
    cost_policy: VirtualReplayCostPolicy,
) -> TradeCloseRequest | None:
    symbol = str(trade["symbol"])
    direction = str(trade["direction"])
    open_time = _aware(trade["open_time"], "trade open_time")
    cutoff = max(_aware(cutoff_time, "session cutoff"), open_time)
    deadline = cutoff + timedelta(minutes=5)
    tick = _first_at_or_after(tape, symbol, cutoff, deadline)
    watermark = tape.watermark.get(symbol)
    if tick is None:
        if watermark is not None and watermark >= deadline:
            raise ReplayDataError(
                f"session close quote is missing within five minutes for {trade['trade_id']}"
            )
        return None
    entry = _number(trade["entry_executable"], "entry_executable")
    stop = _number(trade["stop_price"], "stop_price")
    risk_distance = abs(entry - stop)
    if risk_distance <= 0:
        raise ReplayDataError(f"zero risk distance for {trade['trade_id']}")
    path = _ticks_between(tape, symbol, after=open_time, through=tick.event_time)
    if not path:
        raise ReplayDataError(f"session close path is empty for {trade['trade_id']}")
    prices = [row.bid if direction == "long" else row.ask for row in path]
    if direction == "long":
        mfe = (max(prices) - entry) / risk_distance
        mae = (entry - min(prices)) / risk_distance
    else:
        mfe = (entry - min(prices)) / risk_distance
        mae = (max(prices) - entry) / risk_distance
    rate, source, conversion_available_time = _conversion_at(
        tape,
        symbol=symbol,
        moment=tick.event_time,
        max_delay=timedelta(minutes=5),
    )
    if rate is None or source is None:
        return None
    costs = _modeled_costs(
        planned_risk_jpy=int(str(trade["planned_risk_jpy"])),
        symbol=symbol,
        open_time=open_time,
        close_time=tick.event_time,
        policy=cost_policy,
    )
    evidence = {
        "contract": REPLAY_CONTRACT,
        **_tape_source_evidence(tape),
        "path_start": path[0].event_time.isoformat(),
        "path_end": tick.event_time.isoformat(),
        "session_cutoff": cutoff.isoformat(),
        "path_tick_count_to_exit": len(path),
        "path_source_ids_sha256": _sha256_json([row.source_record_id for row in path]),
        "label_path_evidence_contract": LABEL_PATH_EVIDENCE_CONTRACT,
        "path_raw_payload_hashes_sha256": _sha256_json([row.raw_payload_sha256 for row in path]),
        "exit_trigger": "session_end",
        "gap_policy": "first executable tick at-or-after cutoff within five minutes",
        "data_mode": "delayed_historical_download",
        "cost_policy_sha256": cost_policy.sha256,
        "cost_policy": asdict(cost_policy),
    }
    return TradeCloseRequest(
        trade_id=str(trade["trade_id"]),
        close_time=tick.event_time,
        quote=tick.snapshot(),
        quote_to_jpy_rate=rate,
        quote_conversion_source=source,
        close_reason="session_end",
        cost_model_id=cost_policy.cost_model_id,
        cost_model_version=cost_policy.version,
        cost_source=(
            "measured Dukascopy bid/ask spread plus conservative deterministic "
            "research-only R-based assumptions"
        ),
        costs_complete=True,
        maximum_favorable_excursion_r=max(0.0, mfe),
        maximum_adverse_excursion_r=max(0.0, mae),
        historical_replay=True,
        observed_at=_close_observed_at(
            trade,
            tick_available_time=tick.available_time,
            conversion_available_time=conversion_available_time,
        ),
        replay_evidence=evidence,
        **costs,
    )


def _position_mark_request(
    trade: Mapping[str, object],
    *,
    tape: QuoteTape,
    as_of: datetime,
    cap_time: datetime | None = None,
) -> PositionMarkRequest | None:
    symbol = str(trade["symbol"])
    open_time = _aware(trade["open_time"], "trade open_time")
    watermark = tape.watermark.get(symbol)
    if watermark is None:
        return None
    end = min(_aware(as_of, "mark as_of"), watermark)
    if cap_time is not None:
        end = min(end, _aware(cap_time, "mark cap_time"))
    tick = _latest_at_or_before(tape, symbol, open_time, end)
    if tick is None:
        return None
    rate, source, conversion_available_time = _conversion_at(
        tape,
        symbol=symbol,
        moment=tick.event_time,
        max_delay=timedelta(minutes=5),
    )
    if rate is None or source is None:
        return None
    return PositionMarkRequest(
        trade_id=str(trade["trade_id"]),
        mark_time=tick.event_time,
        quote=tick.snapshot(),
        quote_to_jpy_rate=rate,
        quote_conversion_source=source,
        historical_replay=True,
        observed_at=max(
            tick.available_time,
            conversion_available_time or tick.available_time,
        ),
    )


def _tape_window(
    pending: list[dict[str, object]],
    open_trades: list[dict[str, object]],
    *,
    as_of: datetime,
    session_close_cutoff: datetime | None = None,
) -> tuple[datetime, datetime, set[str]] | None:
    starts: list[datetime] = []
    ends: list[datetime] = []
    symbols: set[str] = set()
    for intake in pending:
        decision = intake["decision"]
        assert isinstance(decision, Mapping)
        moment = _aware(intake["decision_time"], "pending decision_time")
        starts.append(moment)
        symbols.add(str(decision.get("symbol") or "").upper())
        try:
            ends.append(moment + horizon_for_timeframe(decision.get("timeframe")))
        except ReplayDataError:
            ends.append(moment + timedelta(minutes=5))
    for trade in open_trades:
        moment = _aware(trade["open_time"], "open trade time")
        starts.append(moment)
        symbols.add(str(trade["symbol"]).upper())
        prediction = _aware(trade["decision_time"], "open trade decision_time")
        ends.append(prediction + horizon_for_timeframe(trade["timeframe"]))
    if session_close_cutoff is not None and open_trades:
        ends.append(_aware(session_close_cutoff, "session close cutoff") + timedelta(minutes=5))
    if not starts:
        return None
    if any(symbol.endswith("USD") and not symbol.endswith("JPY") for symbol in symbols):
        symbols.add("USDJPY")
    return min(starts), min(as_of, max(ends) + timedelta(minutes=5)), symbols


def run_replay_cycle(
    store: VirtualPortfolioStore,
    *,
    quote_log: str | Path,
    quote_index: str | Path | None = None,
    as_of: datetime,
    cost_policy: VirtualReplayCostPolicy | None = None,
) -> dict[str, object]:
    """Open queued decisions and settle mature trades in event-time order."""

    moment = _aware(as_of, "replay as_of")
    costs = cost_policy or VirtualReplayCostPolicy()
    costs.validate()
    initial_pending = store.pending_decisions()
    pending: list[dict[str, object]] = []
    pre_abstained = 0
    pre_waiting = 0
    pre_conflicts: list[dict[str, str]] = []
    for intake in initial_pending:
        raw_decision = intake["decision"]
        assert isinstance(raw_decision, Mapping)
        decision_time = _aware(intake["decision_time"], "pending decision_time")
        decision_policy = store.policy(
            as_of=decision_time,
            known_at=decision_time,
        )
        decision = enrich_decision_artifacts(
            raw_decision,
            portfolio_policy=decision_policy,
        )
        assignment = classify_virtual_horizon(decision.get("timeframe"))
        if assignment["capacity_eligible"] is True:
            pending.append({**intake, "decision": decision})
            continue
        try:
            result = store.process_decision(
                decision,
                observed_at=_aware(intake["observed_at"], "observed_at"),
            )
            if result["inserted"] and result["disposition"] == "abstained":
                pre_abstained += 1
        except Exception as error:
            pre_waiting += 1
            pre_conflicts.append(
                {
                    "record_id": str(intake["decision_id"]),
                    "error": str(error),
                }
            )
    open_trades = store.open_trade_details()
    session_request = store.pending_session_close_request(as_of=moment)
    session_close_cutoff = (
        _aware(session_request["cutoff_time"], "session close cutoff")
        if session_request is not None
        else None
    )
    window = _tape_window(
        pending,
        open_trades,
        as_of=moment,
        session_close_cutoff=session_close_cutoff,
    )
    if window is None:
        try:
            valuation_v2 = append_synchronized_valuation_from_marks(
                store,
                valuation_cutoff=moment,
                observed_at=moment,
            )
            valuation_conflicts: list[dict[str, str]] = []
        except Exception as error:
            valuation_v2 = {
                "contract": "virtual-portfolio-valuation-consumer-v1",
                "quality_complete": False,
                "error": str(error),
            }
            valuation_conflicts = [
                {
                    "record_id": "portfolio-valuation-v2",
                    "error": str(error),
                }
            ]
        return {
            "contract": REPLAY_CONTRACT,
            "status": (
                "idle"
                if not initial_pending and not valuation_conflicts
                else ("ok" if not pre_conflicts and not valuation_conflicts else "partial_failure")
            ),
            "queued": len(initial_pending),
            "opened": 0,
            "abstained": pre_abstained,
            "closed": 0,
            "marked": 0,
            "waiting": pre_waiting,
            "session_close_request": session_request,
            "valuation_v2": valuation_v2,
            "conflicts": [*pre_conflicts, *valuation_conflicts],
        }
    start, end, symbols = window
    tape = load_quote_tape(
        quote_log,
        symbols=symbols,
        start=start,
        end=end,
        as_of=moment,
        index_path=quote_index,
    )
    events: list[tuple[datetime, int, int, str, dict[str, object]]] = []
    serial = 0
    waiting = pre_waiting
    abstained = pre_abstained
    opened = 0
    closed = 0
    marked = 0
    conflicts: list[dict[str, str]] = list(pre_conflicts)

    for trade in open_trades:
        try:
            request = _close_request(
                trade,
                tape=tape,
                cost_policy=costs,
            )
            if session_close_cutoff is not None:
                forced_request = _session_close_request(
                    trade,
                    tape=tape,
                    cutoff_time=session_close_cutoff,
                    cost_policy=costs,
                )
                if forced_request is not None and (
                    request is None or forced_request.close_time < request.close_time
                ):
                    request = forced_request
            mark_request = _position_mark_request(
                trade,
                tape=tape,
                as_of=moment,
                cap_time=request.close_time if request is not None else None,
            )
        except (ReplayDataError, ValueError) as error:
            conflicts.append({"trade_id": str(trade["trade_id"]), "error": str(error)})
            continue
        if mark_request is not None:
            mark_payload: dict[str, object] = {"request": mark_request}
            heapq.heappush(
                events,
                (
                    mark_request.mark_time,
                    0,
                    serial,
                    "mark",
                    mark_payload,
                ),
            )
            serial += 1
        if request is None:
            waiting += 1
            continue
        close_payload: dict[str, object] = {"request": request}
        heapq.heappush(
            events,
            (
                request.close_time,
                1,
                serial,
                "close",
                close_payload,
            ),
        )
        serial += 1

    for intake in pending:
        raw_decision = intake["decision"]
        assert isinstance(raw_decision, Mapping)
        decision = dict(raw_decision)
        decision_time = _aware(intake["decision_time"], "decision_time")
        observed_at = _aware(intake["observed_at"], "observed_at")
        decision_policy = store.policy(
            as_of=decision_time,
            known_at=decision_time,
        )
        direction = str(decision.get("direction") or "").lower()
        try:
            horizon_for_timeframe(decision.get("timeframe"))
        except ReplayDataError as error:
            payload: dict[str, object] = {
                "decision": dict(decision),
                "observed_at": observed_at,
                "reasons": [str(error)],
            }
            heapq.heappush(events, (decision_time, 1, serial, "abstain", payload))
            serial += 1
            continue
        if direction not in {"long", "short"}:
            payload = {
                "decision": dict(decision),
                "observed_at": observed_at,
                "reasons": [],
            }
            heapq.heappush(events, (decision_time, 1, serial, "abstain", payload))
            serial += 1
            continue
        symbol = str(decision.get("symbol") or "").upper()
        deadline = decision_time + timedelta(seconds=decision_policy.quote_max_age_seconds)
        entry = _first_at_or_after(tape, symbol, decision_time, deadline)
        if entry is None:
            if tape.watermark.get(symbol, datetime.min.replace(tzinfo=UTC)) >= deadline:
                payload = {
                    "decision": dict(decision),
                    "observed_at": observed_at,
                    "reasons": ["判断後5分以内に検証可能なDukascopy bid/ask tickが無い"],
                }
                heapq.heappush(events, (deadline, 1, serial, "abstain", payload))
                serial += 1
            else:
                waiting += 1
            continue
        conversion_rate, conversion_source, _ = _conversion_at(
            tape,
            symbol=symbol,
            moment=entry.event_time,
            max_delay=timedelta(minutes=5),
        )
        if conversion_rate is None or conversion_source is None:
            conversion_watermark = tape.watermark.get("USDJPY")
            if symbol[3:] == "USD" and conversion_watermark is not None:
                payload = {
                    "decision": dict(decision),
                    "observed_at": observed_at,
                    "reasons": ["同時点のUSDJPY換算tickが無くJPYリスクを計算できない"],
                }
                heapq.heappush(events, (deadline, 1, serial, "abstain", payload))
                serial += 1
            else:
                waiting += 1
            continue
        payload = {
            "decision": dict(decision),
            "observed_at": observed_at,
            "entry": entry,
            "conversion_rate": conversion_rate,
            "conversion_source": conversion_source,
        }
        heapq.heappush(events, (entry.event_time, 1, serial, "open", payload))
        serial += 1

    while events:
        _, _, _, event_type, payload = heapq.heappop(events)
        try:
            if event_type == "mark":
                request_value = payload.get("request")
                assert isinstance(request_value, PositionMarkRequest)
                result = store.record_position_mark(request_value)
                if result["inserted"]:
                    marked += 1
                continue
            if event_type == "close":
                request_value = payload.get("request")
                assert isinstance(request_value, TradeCloseRequest)
                result = store.close_trade(request_value)
                if result["inserted"]:
                    closed += 1
                continue
            if event_type == "abstain":
                decision_value = payload.get("decision")
                observed_value = payload.get("observed_at")
                reasons_value = payload.get("reasons")
                assert isinstance(decision_value, Mapping)
                assert isinstance(observed_value, datetime)
                assert isinstance(reasons_value, list)
                result = store.process_decision(
                    decision_value,
                    observed_at=observed_value,
                    additional_rejection_reasons=[str(reason) for reason in reasons_value],
                )
                if result["inserted"] and result["disposition"] == "abstained":
                    abstained += 1
                continue
            decision_value = payload.get("decision")
            observed_value = payload.get("observed_at")
            entry_value = payload.get("entry")
            rate_value = payload.get("conversion_rate")
            source_value = payload.get("conversion_source")
            assert isinstance(decision_value, Mapping)
            assert isinstance(observed_value, datetime)
            assert isinstance(entry_value, ReplayTick)
            assert isinstance(source_value, str)
            result = store.process_decision(
                decision_value,
                quote=entry_value.snapshot(),
                observed_at=observed_value,
                quote_to_jpy_rate=_number(rate_value, "conversion_rate"),
                quote_conversion_source=source_value,
                historical_replay=True,
                replay_as_of=moment,
            )
            if result["inserted"] and result["disposition"] == "opened":
                opened += 1
                trade = next(
                    row
                    for row in store.open_trade_details()
                    if row["trade_id"] == result["trade_id"]
                )
                request = _close_request(
                    trade,
                    tape=tape,
                    cost_policy=costs,
                )
                if session_close_cutoff is not None:
                    forced_request = _session_close_request(
                        trade,
                        tape=tape,
                        cutoff_time=session_close_cutoff,
                        cost_policy=costs,
                    )
                    if forced_request is not None and (
                        request is None or forced_request.close_time < request.close_time
                    ):
                        request = forced_request
                mark_request = _position_mark_request(
                    trade,
                    tape=tape,
                    as_of=moment,
                    cap_time=request.close_time if request is not None else None,
                )
                if mark_request is not None:
                    followup_mark_payload: dict[str, object] = {"request": mark_request}
                    heapq.heappush(
                        events,
                        (
                            mark_request.mark_time,
                            0,
                            serial,
                            "mark",
                            followup_mark_payload,
                        ),
                    )
                    serial += 1
                if request is None:
                    waiting += 1
                else:
                    followup_close_payload: dict[str, object] = {"request": request}
                    heapq.heappush(
                        events,
                        (
                            request.close_time,
                            1,
                            serial,
                            "close",
                            followup_close_payload,
                        ),
                    )
                    serial += 1
            elif result["inserted"]:
                abstained += 1
        except Exception as error:  # one immutable record must not poison the batch
            request_value = payload.get("request")
            decision_value = payload.get("decision")
            identity = (
                request_value.trade_id
                if isinstance(request_value, (TradeCloseRequest, PositionMarkRequest))
                else str(
                    decision_value.get("decision_id", "unknown")
                    if isinstance(decision_value, Mapping)
                    else "unknown"
                )
            )
            conflicts.append({"record_id": identity, "error": str(error)})

    try:
        valuation_v2 = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=moment,
            observed_at=moment,
        )
    except Exception as error:
        conflicts.append(
            {
                "record_id": "portfolio-valuation-v2",
                "error": str(error),
            }
        )
        valuation_v2 = {
            "contract": "virtual-portfolio-valuation-consumer-v1",
            "quality_complete": False,
            "error": str(error),
        }

    return {
        "contract": REPLAY_CONTRACT,
        "status": "ok" if not conflicts else "partial_failure",
        "source": {
            "path": tape.source_path,
            "size_bytes": tape.source_size_bytes,
            "prefix_sha256": tape.source_prefix_sha256,
            "index_path": tape.index_path,
            "index_lag_bytes": tape.index_lag_bytes,
            "indexed_at": (tape.indexed_at.isoformat() if tape.indexed_at is not None else None),
            "rejected_rows": tape.rejected_rows,
            "watermark": {symbol: value.isoformat() for symbol, value in tape.watermark.items()},
            "collection_mode": "historical_download",
            "broker_connected": False,
        },
        "queued": len(initial_pending),
        "opened": opened,
        "abstained": abstained,
        "closed": closed,
        "marked": marked,
        "waiting": waiting,
        "session_close_request": session_request,
        "cost_policy_sha256": costs.sha256,
        "valuation_v2": valuation_v2,
        "conflicts": conflicts,
    }


__all__ = [
    "MODEL_DESCRIPTOR_CONTRACT",
    "QuoteTape",
    "REPLAY_CONTRACT",
    "LABEL_PATH_EVIDENCE_CONTRACT",
    "ReplayDataError",
    "ReplayTick",
    "VirtualReplayCostPolicy",
    "enrich_decision_artifacts",
    "horizon_for_timeframe",
    "load_quote_tape",
    "quote_log_prefix_sha256",
    "run_replay_cycle",
]
