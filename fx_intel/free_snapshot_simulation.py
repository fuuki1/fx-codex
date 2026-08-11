"""Free five-minute snapshot fills with delayed measured-quote reconciliation.

The source snapshot contains a midpoint proxy, not executable Bid/Ask.  This
module therefore applies a versioned conservative spread, labels every fill as
modeled, permanently excludes it from learning, and later appends a comparison
against already-collected Dukascopy historical Bid/Ask.  It has no network or
broker dependency and cannot place, modify, cancel, or close a broker order.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path

from .virtual_portfolio import (
    MODELED_SNAPSHOT_DELIVERY_GRACE_SECONDS,
    MODELED_SNAPSHOT_EXECUTION_MODE,
    PositionMarkRequest,
    QuoteSnapshot,
    TradeCloseRequest,
    VirtualPortfolioStore,
    classify_virtual_horizon,
)
from .virtual_portfolio_replay import (
    ReplayDataError,
    ReplayTick,
    VirtualReplayCostPolicy,
    enrich_decision_artifacts,
    horizon_for_timeframe,
    load_quote_tape,
)

SNAPSHOT_SIMULATION_CONTRACT = "free-five-minute-snapshot-simulation-v1"
SNAPSHOT_QUOTE_SOURCE = "free_five_minute_snapshot_spread_model"
DEFAULT_TAIL_BYTES = 64_000_000


class SnapshotSimulationError(RuntimeError):
    """A snapshot input cannot support an honest modeled fill."""


@dataclass(frozen=True)
class ConservativeSnapshotSpreadPolicy:
    """Pre-registered spread floors for research-only midpoint snapshots."""

    policy_id: str = "free-snapshot-conservative-spread-v1"
    usd_jpy_pips: float = 2.0
    eur_usd_pips: float = 2.0
    gbp_usd_pips: float = 2.5

    @property
    def sha256(self) -> str:
        return _sha256(asdict(self))

    def spread(self, symbol: str) -> float:
        normalized = _symbol(symbol)
        pips = {
            "USDJPY": self.usd_jpy_pips,
            "EURUSD": self.eur_usd_pips,
            "GBPUSD": self.gbp_usd_pips,
        }.get(normalized)
        if pips is None or pips <= 0:
            raise SnapshotSimulationError(f"spread policy has no value for {normalized}")
        pip_size = 0.01 if normalized.endswith("JPY") else 0.0001
        return pips * pip_size


@dataclass(frozen=True)
class SnapshotPoint:
    symbol: str
    timeframe: str
    event_time: datetime
    capture_slot: datetime
    mid: float
    source_record_id: str
    content_hash: str

    def quote(self, policy: ConservativeSnapshotSpreadPolicy) -> QuoteSnapshot:
        spread = policy.spread(self.symbol)
        bid = self.mid - spread / 2
        ask = self.mid + spread / 2
        if bid <= 0 or bid >= ask:
            raise SnapshotSimulationError("modeled snapshot Bid/Ask is invalid")
        identity = {
            "contract": SNAPSHOT_SIMULATION_CONTRACT,
            "snapshot_source_record_id": self.source_record_id,
            "snapshot_content_hash": self.content_hash,
            "spread_policy_sha256": policy.sha256,
            "bid": bid,
            "ask": ask,
        }
        revision = _sha256(identity)
        return QuoteSnapshot(
            source_record_id=(
                f"modeled:{self.source_record_id}:spread-policy-sha256:{policy.sha256}"
            ),
            source=SNAPSHOT_QUOTE_SOURCE,
            revision_id=revision,
            event_time=self.event_time,
            available_time=self.event_time,
            ingested_time=self.event_time,
            bid=bid,
            ask=ask,
        )


@dataclass(frozen=True)
class SnapshotTape:
    points: Mapping[tuple[str, str], tuple[SnapshotPoint, ...]]
    watermark: Mapping[tuple[str, str], datetime]
    source_path: str
    source_size_bytes: int
    source_prefix_sha256: str
    rejected_rows: int


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _content_hash(row: Mapping[str, object]) -> str:
    return _sha256({key: value for key, value in row.items() if key != "content_hash"})


def _aware(value: object, name: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise SnapshotSimulationError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotSimulationError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _number(value: object, name: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError) as error:
        raise SnapshotSimulationError(f"{name} must be numeric") from error
    if not math.isfinite(number) or number <= 0:
        raise SnapshotSimulationError(f"{name} must be positive and finite")
    return number


def _symbol(value: object) -> str:
    normalized = str(value or "").upper()
    for separator in ("/", "_", "-"):
        normalized = normalized.replace(separator, "")
    return normalized


def _tail_lines(path: Path, tail_bytes: int) -> Iterable[str]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - tail_bytes)
        handle.seek(start)
        payload = handle.read()
    lines = payload.decode("utf-8").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def load_snapshot_tape(
    path: str | Path,
    *,
    as_of: datetime,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> SnapshotTape:
    target = Path(path).resolve()
    if not target.is_file():
        raise SnapshotSimulationError(f"snapshot log does not exist: {target}")
    moment = _aware(as_of, "snapshot as_of")
    source_size = target.stat().st_size
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    selected: dict[tuple[str, str], list[SnapshotPoint]] = {}
    rejected = 0
    for line in _tail_lines(target, tail_bytes):
        try:
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                raise TypeError("snapshot row is not an object")
            if raw.get("source") != "tradingview_ta_scanner":
                continue
            claimed_hash = str(raw.get("content_hash") or "")
            if len(claimed_hash) != 64 or claimed_hash != _content_hash(raw):
                raise ValueError("snapshot content hash mismatch")
            flags = raw.get("data_quality_flags")
            if not isinstance(flags, list) or "bid_ask_unavailable" not in flags:
                raise ValueError("snapshot must explicitly declare missing Bid/Ask")
            if raw.get("ohlc_scope") != "forming_bar_snapshot":
                raise ValueError("snapshot OHLC scope is not forming_bar_snapshot")
            available = _aware(raw.get("available_time") or raw.get("ts"), "available_time")
            ingested = _aware(raw.get("ingested_time") or raw.get("ts"), "ingested_time")
            capture_slot = _aware(raw.get("capture_slot"), "capture_slot")
            if not capture_slot <= available <= ingested <= moment:
                raise ValueError("snapshot timestamps violate PIT ordering")
            if available - capture_slot >= timedelta(minutes=5):
                raise ValueError("snapshot availability is outside its five-minute slot")
            symbol = _symbol(raw.get("symbol"))
            timeframe = str(raw.get("timeframe") or "").strip().lower()
            if not symbol or not timeframe:
                raise ValueError("snapshot symbol/timeframe is missing")
            point = SnapshotPoint(
                symbol=symbol,
                timeframe=timeframe,
                event_time=available,
                capture_slot=capture_slot,
                mid=_number(raw.get("close"), "snapshot close"),
                source_record_id=str(
                    raw.get("local_record_id")
                    or raw.get("source_record_id")
                    or f"{symbol}:{timeframe}:{available.isoformat()}"
                ),
                content_hash=claimed_hash,
            )
        except (TypeError, ValueError, SnapshotSimulationError, json.JSONDecodeError):
            rejected += 1
            continue
        selected.setdefault((point.symbol, point.timeframe), []).append(point)
    ordered: dict[tuple[str, str], tuple[SnapshotPoint, ...]] = {}
    watermark: dict[tuple[str, str], datetime] = {}
    for key, rows in selected.items():
        rows.sort(key=lambda row: (row.event_time, row.source_record_id))
        deduplicated: list[SnapshotPoint] = []
        seen_times: set[datetime] = set()
        for row in rows:
            if row.event_time in seen_times:
                previous = deduplicated[-1]
                if previous.content_hash != row.content_hash:
                    raise SnapshotSimulationError(
                        "conflicting snapshot writers reused the same natural key"
                    )
                continue
            seen_times.add(row.event_time)
            deduplicated.append(row)
        ordered[key] = tuple(deduplicated)
        if deduplicated:
            watermark[key] = deduplicated[-1].event_time
    return SnapshotTape(
        points=ordered,
        watermark=watermark,
        source_path=str(target),
        source_size_bytes=source_size,
        source_prefix_sha256=digest.hexdigest(),
        rejected_rows=rejected,
    )


def _first_at_or_after(
    tape: SnapshotTape,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> SnapshotPoint | None:
    for point in tape.points.get((_symbol(symbol), timeframe.lower()), ()):
        if start <= point.event_time <= end:
            return point
    return None


def _points_between(
    tape: SnapshotTape,
    *,
    symbol: str,
    timeframe: str,
    after: datetime,
    through: datetime,
) -> list[SnapshotPoint]:
    return [
        point
        for point in tape.points.get((_symbol(symbol), timeframe.lower()), ())
        if after < point.event_time <= through
    ]


def _conversion(
    tape: SnapshotTape,
    *,
    symbol: str,
    timeframe: str,
    moment: datetime,
) -> tuple[float | None, str | None]:
    normalized = _symbol(symbol)
    if normalized.endswith("JPY"):
        return 1.0, "JPY quote currency"
    if normalized[3:] != "USD":
        return None, None
    candidates = [
        point
        for point in tape.points.get(("USDJPY", timeframe.lower()), ())
        if timedelta(0) <= moment - point.event_time <= timedelta(minutes=5)
    ]
    if not candidates and timeframe.lower() != "15m":
        candidates = [
            point
            for point in tape.points.get(("USDJPY", "15m"), ())
            if timedelta(0) <= moment - point.event_time <= timedelta(minutes=5)
        ]
    if not candidates:
        return None, None
    point = candidates[-1]
    return point.mid, f"free_snapshot_usdjpy:{point.source_record_id}"


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
        "conversion_cost_jpy": (
            0 if _symbol(symbol).endswith("JPY") else amount(policy.conversion_r_bps)
        ),
    }


def _modeled_open_trades(store: VirtualPortfolioStore) -> list[dict[str, object]]:
    modeled: list[dict[str, object]] = []
    for trade in store.open_trade_details():
        assessment = trade.get("execution_assessment")
        if (
            isinstance(assessment, Mapping)
            and assessment.get("execution_mode") == MODELED_SNAPSHOT_EXECUTION_MODE
        ):
            modeled.append(trade)
    return modeled


def run_free_snapshot_cycle(
    store: VirtualPortfolioStore,
    *,
    snapshot_log: str | Path,
    as_of: datetime,
    spread_policy: ConservativeSnapshotSpreadPolicy | None = None,
    cost_policy: VirtualReplayCostPolicy | None = None,
) -> dict[str, object]:
    """Open, mark, and close free modeled fills from immutable 5-minute snapshots."""

    moment = _aware(as_of, "simulation as_of")
    spreads = spread_policy or ConservativeSnapshotSpreadPolicy()
    costs = cost_policy or VirtualReplayCostPolicy()
    costs.validate()
    tape = load_snapshot_tape(snapshot_log, as_of=moment)
    opened = 0
    marked = 0
    closed = 0
    abstained = 0
    waiting = 0
    conflicts: list[dict[str, str]] = []

    for intake in store.pending_decisions():
        try:
            raw = intake["decision"]
            if not isinstance(raw, Mapping):
                raise SnapshotSimulationError("pending decision is not an object")
            decision_time = _aware(intake["decision_time"], "decision_time")
            observed_at = _aware(intake["observed_at"], "decision observed_at")
            policy = store.policy(as_of=decision_time, known_at=decision_time)
            decision = enrich_decision_artifacts(raw, portfolio_policy=policy)
            assignment = classify_virtual_horizon(decision.get("timeframe"))
            direction = str(decision.get("direction") or "").lower()
            if assignment["capacity_eligible"] is not True or direction not in {"long", "short"}:
                result = store.process_decision(decision, observed_at=observed_at)
                if result["inserted"] and result["disposition"] == "abstained":
                    abstained += 1
                continue
            timeframe = str(decision.get("timeframe") or "").lower()
            deadline = decision_time + timedelta(
                seconds=(policy.quote_max_age_seconds + MODELED_SNAPSHOT_DELIVERY_GRACE_SECONDS)
            )
            point = _first_at_or_after(
                tape,
                symbol=str(decision.get("symbol") or ""),
                timeframe=timeframe,
                start=decision_time,
                end=deadline,
            )
            key = (_symbol(decision.get("symbol")), timeframe)
            if point is None:
                if tape.watermark.get(key, datetime.min.replace(tzinfo=UTC)) >= deadline:
                    result = store.process_decision(
                        decision,
                        observed_at=observed_at,
                        additional_rejection_reasons=[
                            "次の5分スロット（配信猶予90秒）に検証可能な無料スナップショットが無い"
                        ],
                    )
                    if result["inserted"] and result["disposition"] == "abstained":
                        abstained += 1
                else:
                    waiting += 1
                continue
            rate, conversion_source = _conversion(
                tape,
                symbol=str(decision.get("symbol") or ""),
                timeframe=timeframe,
                moment=point.event_time,
            )
            if rate is None or conversion_source is None:
                waiting += 1
                continue
            result = store.process_decision(
                decision,
                quote=point.quote(spreads),
                observed_at=observed_at,
                quote_to_jpy_rate=rate,
                quote_conversion_source=conversion_source,
                modeled_snapshot=True,
                replay_as_of=moment,
            )
            if result["inserted"] and result["disposition"] == "opened":
                opened += 1
            elif result["inserted"]:
                abstained += 1
        except Exception as error:
            conflicts.append(
                {
                    "record_id": str(intake.get("decision_id") or "unknown"),
                    "error": str(error),
                }
            )

    for trade in _modeled_open_trades(store):
        try:
            open_time = _aware(trade["open_time"], "trade open_time")
            decision_time = _aware(trade["decision_time"], "trade decision_time")
            horizon_end = decision_time + horizon_for_timeframe(trade["timeframe"])
            timeframe = str(trade["timeframe"]).lower()
            key = (_symbol(trade["symbol"]), timeframe)
            watermark = tape.watermark.get(key)
            if watermark is None:
                waiting += 1
                continue
            points = _points_between(
                tape,
                symbol=str(trade["symbol"]),
                timeframe=timeframe,
                after=open_time,
                through=min(moment, horizon_end),
            )
            if not points:
                waiting += 1
                continue
            direction = str(trade["direction"])
            entry = float(str(trade["entry_executable"]))
            stop = float(str(trade["stop_price"]))
            target = float(str(trade["target_price"]))
            risk_distance = abs(entry - stop)
            if risk_distance <= 0:
                raise SnapshotSimulationError("modeled trade has zero risk distance")
            exit_point: SnapshotPoint | None = None
            close_reason: str | None = None
            path: list[SnapshotPoint] = []
            for point in points:
                path.append(point)
                quote = point.quote(spreads)
                rate, source = _conversion(
                    tape,
                    symbol=str(trade["symbol"]),
                    timeframe=timeframe,
                    moment=point.event_time,
                )
                if rate is None or source is None:
                    raise SnapshotSimulationError("modeled mark has no JPY conversion snapshot")
                mark_result = store.record_position_mark(
                    PositionMarkRequest(
                        trade_id=str(trade["trade_id"]),
                        mark_time=point.event_time,
                        quote=quote,
                        quote_to_jpy_rate=rate,
                        quote_conversion_source=source,
                        modeled_snapshot=True,
                        observed_at=moment,
                    )
                )
                if mark_result["inserted"]:
                    marked += 1
                executable = quote.bid if direction == "long" else quote.ask
                stop_hit = executable <= stop if direction == "long" else executable >= stop
                target_hit = executable >= target if direction == "long" else executable <= target
                if stop_hit or target_hit:
                    exit_point = point
                    close_reason = "stop_loss" if stop_hit else "target1"
                    break
            if exit_point is None and watermark >= horizon_end:
                exit_point = path[-1]
                close_reason = "time_exit"
            if exit_point is None or close_reason is None:
                waiting += 1
                continue
            quote = exit_point.quote(spreads)
            rate, source = _conversion(
                tape,
                symbol=str(trade["symbol"]),
                timeframe=timeframe,
                moment=exit_point.event_time,
            )
            if rate is None or source is None:
                waiting += 1
                continue
            executable_prices = [
                point.quote(spreads).bid if direction == "long" else point.quote(spreads).ask
                for point in path
            ]
            if direction == "long":
                mfe = max(0.0, (max(executable_prices) - entry) / risk_distance)
                mae = max(0.0, (entry - min(executable_prices)) / risk_distance)
            else:
                mfe = max(0.0, (entry - min(executable_prices)) / risk_distance)
                mae = max(0.0, (max(executable_prices) - entry) / risk_distance)
            cost_values = _modeled_costs(
                planned_risk_jpy=int(str(trade["planned_risk_jpy"])),
                symbol=str(trade["symbol"]),
                open_time=open_time,
                close_time=exit_point.event_time,
                policy=costs,
            )
            close_result = store.close_trade(
                TradeCloseRequest(
                    trade_id=str(trade["trade_id"]),
                    close_time=exit_point.event_time,
                    quote=quote,
                    quote_to_jpy_rate=rate,
                    quote_conversion_source=source,
                    close_reason=close_reason,
                    cost_model_id=costs.cost_model_id,
                    cost_model_version=costs.version,
                    cost_source=(
                        "versioned conservative modeled spread and deterministic "
                        "research-only R-based cost assumptions"
                    ),
                    costs_complete=True,
                    maximum_favorable_excursion_r=mfe,
                    maximum_adverse_excursion_r=mae,
                    modeled_snapshot=True,
                    observed_at=moment,
                    replay_evidence={
                        "contract": SNAPSHOT_SIMULATION_CONTRACT,
                        "snapshot_source_path": tape.source_path,
                        "snapshot_source_size_bytes": tape.source_size_bytes,
                        "snapshot_source_prefix_sha256": tape.source_prefix_sha256,
                        "spread_policy": asdict(spreads),
                        "spread_policy_sha256": spreads.sha256,
                        "cost_policy_sha256": costs.sha256,
                        "path_snapshot_count": len(path),
                        "forming_bar_high_low_used": False,
                        "broker_connected": False,
                    },
                    slippage_cost_jpy=cost_values["slippage_cost_jpy"],
                    commission_jpy=cost_values["commission_jpy"],
                    financing_jpy=cost_values["financing_jpy"],
                    conversion_cost_jpy=cost_values["conversion_cost_jpy"],
                )
            )
            if close_result["inserted"]:
                closed += 1
        except Exception as error:
            conflicts.append(
                {"record_id": str(trade.get("trade_id") or "unknown"), "error": str(error)}
            )

    return {
        "contract": SNAPSHOT_SIMULATION_CONTRACT,
        "status": "ok" if not conflicts else "partial_failure",
        "execution_mode": MODELED_SNAPSHOT_EXECUTION_MODE,
        "broker_connected": False,
        "realtime": False,
        "cadence_seconds": 300,
        "opened": opened,
        "marked": marked,
        "closed": closed,
        "abstained": abstained,
        "waiting": waiting,
        "spread_policy": asdict(spreads),
        "spread_policy_sha256": spreads.sha256,
        "source": {
            "path": tape.source_path,
            "size_bytes": tape.source_size_bytes,
            "prefix_sha256": tape.source_prefix_sha256,
            "rejected_rows": tape.rejected_rows,
            "source_kind": "community_scanner_forming_bar_snapshot",
            "bid_ask": "modeled_not_observed",
        },
        "conflicts": conflicts,
    }


def reconcile_modeled_snapshot_quotes(
    store: VirtualPortfolioStore,
    *,
    quote_log: str | Path,
    as_of: datetime,
    quote_index: str | Path | None = None,
) -> dict[str, object]:
    """Append delayed Dukascopy comparisons without rewriting modeled fills."""

    moment = _aware(as_of, "reconciliation as_of")
    candidates = store.modeled_snapshot_reconciliation_candidates()
    if not candidates:
        return {
            "contract": "modeled-fill-delayed-bid-ask-reconciliation-v1",
            "status": "idle",
            "candidates": 0,
            "reconciled": 0,
            "waiting": 0,
            "broker_connected": False,
        }
    start = min(_aware(row["modeled_event_time"], "modeled time") for row in candidates)
    end = max(_aware(row["modeled_event_time"], "modeled time") for row in candidates)
    symbols = {_symbol(row["symbol"]) for row in candidates}
    try:
        tape = load_quote_tape(
            quote_log,
            symbols=symbols,
            start=start,
            end=end + timedelta(minutes=5),
            as_of=moment,
            index_path=quote_index,
        )
    except (OSError, ReplayDataError) as error:
        return {
            "contract": "modeled-fill-delayed-bid-ask-reconciliation-v1",
            "status": "waiting_for_delayed_bid_ask",
            "candidates": len(candidates),
            "reconciled": 0,
            "waiting": len(candidates),
            "error": str(error),
            "broker_connected": False,
        }
    reconciled = 0
    waiting = 0
    conflicts: list[dict[str, str]] = []
    for candidate in candidates:
        modeled_time = _aware(candidate["modeled_event_time"], "modeled time")
        deadline = modeled_time + timedelta(minutes=5)
        actual: ReplayTick | None = next(
            (
                tick
                for tick in tape.ticks.get(_symbol(candidate["symbol"]), ())
                if modeled_time <= tick.event_time <= deadline
            ),
            None,
        )
        if actual is None:
            if (
                tape.watermark.get(_symbol(candidate["symbol"]), datetime.min.replace(tzinfo=UTC))
                < deadline
            ):
                waiting += 1
            else:
                conflicts.append(
                    {
                        "record_id": f"{candidate['trade_id']}:{candidate['leg']}",
                        "error": "delayed Bid/Ask tape passed the window without a tick",
                    }
                )
            continue
        try:
            result = store.record_modeled_quote_reconciliation(
                trade_id=str(candidate["trade_id"]),
                leg=str(candidate["leg"]),
                actual_quote=actual.snapshot(),
                observed_at=moment,
                evidence={
                    "contract": "modeled-fill-delayed-bid-ask-reconciliation-v1",
                    "actual_source": "dukascopy_historical_datafeed",
                    "actual_source_record_id": actual.source_record_id,
                    "actual_raw_payload_sha256": actual.raw_payload_sha256,
                    "quote_log_path": tape.source_path,
                    "quote_log_size_bytes": tape.source_size_bytes,
                    "quote_log_prefix_sha256": tape.source_prefix_sha256,
                    "modeled_fill_rewritten": False,
                    "learning_eligible": False,
                },
            )
            if result["inserted"]:
                reconciled += 1
        except Exception as error:
            conflicts.append(
                {
                    "record_id": f"{candidate['trade_id']}:{candidate['leg']}",
                    "error": str(error),
                }
            )
    return {
        "contract": "modeled-fill-delayed-bid-ask-reconciliation-v1",
        "status": "ok" if not conflicts else "partial_failure",
        "candidates": len(candidates),
        "reconciled": reconciled,
        "waiting": waiting,
        "conflicts": conflicts,
        "broker_connected": False,
    }


__all__ = [
    "ConservativeSnapshotSpreadPolicy",
    "SNAPSHOT_QUOTE_SOURCE",
    "SNAPSHOT_SIMULATION_CONTRACT",
    "SnapshotPoint",
    "SnapshotSimulationError",
    "SnapshotTape",
    "load_snapshot_tape",
    "reconcile_modeled_snapshot_quotes",
    "run_free_snapshot_cycle",
]
