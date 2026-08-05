"""Append-only offline FX portfolio ledger and learning evidence.

This module is deliberately limited to simulated decisions and fills.  It has
no broker adapter, order endpoint, or account mutation capability.  A decision
can enter the simulated portfolio only when its point-in-time provenance,
executable quote, costs, and governing artifact hashes are all explicit.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sqlite3
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from fx_intel.market import is_market_open
from fx_intel.operational_contracts import is_pit_eligible_entry
from fx_intel.virtual_portfolio_net_labels import (
    VIRTUAL_LEARNING_CONTRACT,
    canonical_label_summary,
    canonical_outcome_from_virtual_row,
    canonical_outcome_sha256,
    canonical_store_connection_summary,
)
from fx_intel.virtual_portfolio_v2 import (
    CanonicalEventRequest,
    InvalidV2Record,
    JournalPosting,
    PortfolioValuationRequest,
    append_accounting_event,
    assert_legacy_accounting_projected,
    audit_v2,
    install_v2_schema,
    latest_v2_snapshot,
    migrate_legacy_accounting,
    record_synchronized_valuation,
)

SCHEMA_VERSION = 7
APPLICATION_ID = 0x46585650  # "FXVP"
DEFAULT_PORTFOLIO_ID = "virtual-jpy-1m"
DEFAULT_WRITER_ID = "virtual-portfolio-daily"
FAILURE_CLASSIFIER_VERSION = "virtual-failure-rules-v2"
COUNTERFACTUAL_CONTRACT = "virtual-trade-counterfactual-v1"
TOKYO = ZoneInfo("Asia/Tokyo")


class VirtualPortfolioError(RuntimeError):
    """Base error for the offline virtual portfolio."""


class VirtualPortfolioWriterBusy(VirtualPortfolioError):
    """Another process already owns the portfolio writer lock."""


class InvalidVirtualRecord(VirtualPortfolioError):
    """A portfolio record violates a fail-closed contract."""


class ImmutableVirtualRecordConflict(VirtualPortfolioError):
    """A natural key was reused with different immutable content."""


@dataclass(frozen=True)
class VirtualPortfolioPolicy:
    """Versioned limits for the fixed-capital offline simulation."""

    policy_id: str = "virtual-portfolio-policy-v1"
    initial_capital_jpy: int = 1_000_000
    daily_target_jpy: int = 70_000
    risk_per_trade_bps: int = 50
    daily_loss_limit_bps: int = 150
    weekly_loss_limit_bps: int = 300
    monthly_loss_limit_bps: int = 600
    hard_drawdown_bps: int = 1_000
    max_open_positions: int = 2
    decision_max_age_seconds: int = 300
    quote_max_age_seconds: int = 300
    minimum_learning_rows: int = 150

    def validate(self) -> None:
        if not self.policy_id.strip():
            raise InvalidVirtualRecord("policy_id is required")
        if self.initial_capital_jpy <= 0:
            raise InvalidVirtualRecord("initial capital must be positive")
        if self.daily_target_jpy <= 0:
            raise InvalidVirtualRecord("daily target must be positive")
        for name in (
            "risk_per_trade_bps",
            "daily_loss_limit_bps",
            "weekly_loss_limit_bps",
            "monthly_loss_limit_bps",
            "hard_drawdown_bps",
        ):
            value = getattr(self, name)
            if value <= 0 or value > 10_000:
                raise InvalidVirtualRecord(f"{name} must be within 1..10000")
        if self.max_open_positions <= 0:
            raise InvalidVirtualRecord("max_open_positions must be positive")
        if self.decision_max_age_seconds <= 0:
            raise InvalidVirtualRecord("decision_max_age_seconds must be positive")
        if self.quote_max_age_seconds <= 0:
            raise InvalidVirtualRecord("quote_max_age_seconds must be positive")
        if self.minimum_learning_rows <= 1:
            raise InvalidVirtualRecord("minimum_learning_rows must be greater than one")

    @property
    def policy_sha256(self) -> str:
        return _sha256_json(asdict(self))


def five_x_virtual_portfolio_policy() -> VirtualPortfolioPolicy:
    """Return the approved five-times-capacity policy without increasing per-trade risk."""

    return VirtualPortfolioPolicy(
        policy_id="virtual-portfolio-policy-v2-five-x-limits",
        daily_loss_limit_bps=750,
        weekly_loss_limit_bps=1_500,
        monthly_loss_limit_bps=3_000,
        hard_drawdown_bps=5_000,
        max_open_positions=10,
    )


@dataclass(frozen=True)
class VirtualSafetyLearningPolicy:
    """Fast-loop loss memory that may only veto, never increase risk."""

    contract: str = "virtual-portfolio-safety-learning-v1"
    consecutive_loss_limit: int = 3
    rolling_window: int = 5
    minimum_window_mean_net_r: float = -0.25

    @property
    def policy_sha256(self) -> str:
        return _sha256_json(asdict(self))


@dataclass(frozen=True)
class VirtualHorizonAllocationPolicy:
    """Separate short-horizon capacity from long-horizon observation."""

    policy_id: str = "virtual-horizon-allocation-v1"
    capacity_timeframes: tuple[str, ...] = ("15m", "1h")
    observation_timeframes: tuple[str, ...] = ("4h", "1d")

    @property
    def policy_sha256(self) -> str:
        return _sha256_json(asdict(self))


def classify_virtual_horizon(
    timeframe: object,
    *,
    policy: VirtualHorizonAllocationPolicy | None = None,
) -> dict[str, object]:
    """Return the fail-closed capacity class for one decision horizon."""

    allocation_policy = policy or VirtualHorizonAllocationPolicy()
    normalized = str(timeframe or "unknown").strip().lower() or "unknown"
    if normalized in allocation_policy.capacity_timeframes:
        allocation_class = "intraday_capacity"
        capacity_eligible = True
        reason = "15m/1hは既存上限内の仮想取引評価枠"
    elif normalized in allocation_policy.observation_timeframes:
        allocation_class = "observation_only"
        capacity_eligible = False
        reason = "4h/1dは観測専用で仮想ポジション枠を使用しない"
    else:
        allocation_class = "unsupported"
        capacity_eligible = False
        reason = "未対応時間軸はfail-closedで仮想ポジション対象外"
    return {
        "timeframe": normalized,
        "allocation_class": allocation_class,
        "capacity_eligible": capacity_eligible,
        "reason": reason,
        "policy_id": allocation_policy.policy_id,
        "policy_sha256": allocation_policy.policy_sha256,
    }


@dataclass(frozen=True)
class QuoteSnapshot:
    """Executable bid/ask observation with availability provenance."""

    source_record_id: str
    source: str
    revision_id: str
    event_time: datetime
    available_time: datetime
    ingested_time: datetime
    bid: float
    ask: float

    def validate(self, *, as_of: datetime, max_age_seconds: int) -> None:
        event_time = _as_utc(self.event_time, "quote event_time")
        available_time = _as_utc(self.available_time, "quote available_time")
        ingested_time = _as_utc(self.ingested_time, "quote ingested_time")
        as_of_utc = _as_utc(as_of, "quote as_of")
        if not self.source_record_id.strip() or not self.source.strip():
            raise InvalidVirtualRecord("quote source identity is required")
        if not self.revision_id.strip():
            raise InvalidVirtualRecord("quote revision_id is required")
        if not event_time <= available_time <= ingested_time <= as_of_utc:
            raise InvalidVirtualRecord("quote timestamps violate point-in-time ordering")
        if (as_of_utc - event_time).total_seconds() > max_age_seconds:
            raise InvalidVirtualRecord("quote is stale")
        bid = _positive_decimal(self.bid, "quote bid")
        ask = _positive_decimal(self.ask, "quote ask")
        if bid > ask:
            raise InvalidVirtualRecord("quote bid exceeds ask")

    def validate_replay(self, *, as_of: datetime) -> None:
        """Validate a delayed historical quote without calling it real-time.

        Historical downloads can become available well after their market event
        time.  The normal ``validate`` contract therefore cannot be weakened for
        them: replay instead proves event <= availability <= ingestion <= run
        as-of, while entry-window checks are performed against the decision time.
        """

        event_time = _as_utc(self.event_time, "quote event_time")
        available_time = _as_utc(self.available_time, "quote available_time")
        ingested_time = _as_utc(self.ingested_time, "quote ingested_time")
        as_of_utc = _as_utc(as_of, "quote as_of")
        if not self.source_record_id.strip() or not self.source.strip():
            raise InvalidVirtualRecord("quote source identity is required")
        if not self.revision_id.strip():
            raise InvalidVirtualRecord("quote revision_id is required")
        if not event_time <= available_time <= ingested_time <= as_of_utc:
            raise InvalidVirtualRecord("historical quote timestamps violate PIT ordering")
        bid = _positive_decimal(self.bid, "quote bid")
        ask = _positive_decimal(self.ask, "quote ask")
        if bid > ask:
            raise InvalidVirtualRecord("quote bid exceeds ask")


@dataclass(frozen=True)
class TradeCloseRequest:
    """Inputs needed to close one already-open simulated trade."""

    trade_id: str
    close_time: datetime
    quote: QuoteSnapshot
    quote_to_jpy_rate: float
    quote_conversion_source: str
    close_reason: str
    cost_model_id: str = ""
    cost_model_version: str = ""
    cost_source: str = ""
    costs_complete: bool = False
    slippage_cost_jpy: int = 0
    commission_jpy: int = 0
    financing_jpy: int = 0
    conversion_cost_jpy: int = 0
    maximum_favorable_excursion_r: float | None = None
    maximum_adverse_excursion_r: float | None = None
    historical_replay: bool = False
    observed_at: datetime | None = None
    replay_evidence: Mapping[str, object] | None = None


@dataclass(frozen=True)
class PositionMarkRequest:
    """One delayed executable valuation mark for an open simulated trade."""

    trade_id: str
    mark_time: datetime
    quote: QuoteSnapshot
    quote_to_jpy_rate: float
    quote_conversion_source: str
    historical_replay: bool = False
    observed_at: datetime | None = None


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS portfolio_config (
    portfolio_id TEXT PRIMARY KEY,
    created_at_ns INTEGER NOT NULL,
    currency TEXT NOT NULL CHECK (currency = 'JPY'),
    policy_id TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    initial_capital_jpy INTEGER NOT NULL CHECK (initial_capital_jpy > 0),
    writer_id TEXT NOT NULL,
    record_sha256 TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS portfolio_policy_updates (
    update_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    effective_time_ns INTEGER NOT NULL,
    known_at_ns INTEGER NOT NULL,
    policy_id TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    previous_policy_sha256 TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    FOREIGN KEY(portfolio_id) REFERENCES portfolio_config(portfolio_id),
    CHECK (effective_time_ns <= known_at_ns)
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS portfolio_policy_updates_policy_idx
ON portfolio_policy_updates(portfolio_id, policy_sha256);

CREATE INDEX IF NOT EXISTS portfolio_policy_updates_time_idx
ON portfolio_policy_updates(portfolio_id, effective_time_ns, known_at_ns, update_id);

CREATE TABLE IF NOT EXISTS session_events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_date_jst TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('opened', 'closed')),
    event_time_ns INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    UNIQUE(session_id, event_type)
) STRICT;

CREATE TABLE IF NOT EXISTS decision_records (
    decision_id TEXT PRIMARY KEY,
    decision_time_ns INTEGER NOT NULL,
    observed_at_ns INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT NOT NULL
        CHECK (direction IN ('long', 'short', 'neutral', 'standby', 'unknown')),
    disposition TEXT NOT NULL CHECK (disposition IN ('opened', 'abstained')),
    pit_eligible INTEGER NOT NULL CHECK (pit_eligible IN (0, 1)),
    reason_text TEXT NOT NULL,
    rejection_reasons_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    data_sha256 TEXT NOT NULL,
    model_sha256 TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    CHECK (decision_time_ns <= observed_at_ns)
) STRICT;

CREATE INDEX IF NOT EXISTS decision_records_time_idx
ON decision_records(decision_time_ns DESC, decision_id);

CREATE TABLE IF NOT EXISTS decision_horizon_assignments (
    decision_id TEXT PRIMARY KEY,
    timeframe TEXT NOT NULL,
    allocation_class TEXT NOT NULL
        CHECK (allocation_class IN ('intraday_capacity', 'observation_only', 'unsupported')),
    capacity_eligible INTEGER NOT NULL CHECK (capacity_eligible IN (0, 1)),
    policy_id TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    assigned_at_ns INTEGER NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES decision_records(decision_id)
) STRICT;

CREATE INDEX IF NOT EXISTS decision_horizon_assignments_class_idx
ON decision_horizon_assignments(allocation_class, assigned_at_ns, decision_id);

CREATE TABLE IF NOT EXISTS decision_intakes (
    decision_id TEXT PRIMARY KEY,
    decision_time_ns INTEGER NOT NULL,
    observed_at_ns INTEGER NOT NULL,
    source TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    CHECK (decision_time_ns <= observed_at_ns)
) STRICT;

CREATE INDEX IF NOT EXISTS decision_intakes_time_idx
ON decision_intakes(decision_time_ns, decision_id);

CREATE TABLE IF NOT EXISTS simulated_trade_opens (
    trade_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    open_time_ns INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('long', 'short')),
    units REAL NOT NULL CHECK (units > 0),
    entry_bid REAL NOT NULL CHECK (entry_bid > 0),
    entry_ask REAL NOT NULL CHECK (entry_ask > 0),
    entry_mid REAL NOT NULL CHECK (entry_mid > 0),
    entry_executable REAL NOT NULL CHECK (entry_executable > 0),
    stop_price REAL NOT NULL CHECK (stop_price > 0),
    target_price REAL NOT NULL CHECK (target_price > 0),
    quote_to_jpy_rate REAL NOT NULL CHECK (quote_to_jpy_rate > 0),
    quote_conversion_source TEXT NOT NULL,
    planned_risk_jpy INTEGER NOT NULL CHECK (planned_risk_jpy > 0),
    quote_json TEXT NOT NULL,
    rationale_json TEXT NOT NULL,
    features_json TEXT NOT NULL,
    components_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    data_sha256 TEXT NOT NULL,
    model_sha256 TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES decision_records(decision_id),
    CHECK (entry_bid <= entry_ask)
) STRICT;

CREATE INDEX IF NOT EXISTS simulated_trade_opens_time_idx
ON simulated_trade_opens(open_time_ns DESC, trade_id);

CREATE TABLE IF NOT EXISTS simulated_trade_open_observations (
    trade_id TEXT PRIMARY KEY,
    open_known_at_ns INTEGER NOT NULL,
    known_at_basis TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    FOREIGN KEY(trade_id) REFERENCES simulated_trade_opens(trade_id)
) STRICT;

CREATE INDEX IF NOT EXISTS simulated_trade_open_observations_known_idx
ON simulated_trade_open_observations(open_known_at_ns, trade_id);

CREATE TABLE IF NOT EXISTS simulated_trade_closes (
    trade_id TEXT PRIMARY KEY,
    close_time_ns INTEGER NOT NULL,
    close_reason TEXT NOT NULL,
    exit_bid REAL NOT NULL CHECK (exit_bid > 0),
    exit_ask REAL NOT NULL CHECK (exit_ask > 0),
    exit_mid REAL NOT NULL CHECK (exit_mid > 0),
    exit_executable REAL NOT NULL CHECK (exit_executable > 0),
    quote_to_jpy_rate REAL NOT NULL CHECK (quote_to_jpy_rate > 0),
    quote_conversion_source TEXT NOT NULL,
    gross_market_pnl_jpy INTEGER NOT NULL,
    executable_pnl_jpy INTEGER NOT NULL,
    spread_quote_cost_jpy INTEGER NOT NULL CHECK (spread_quote_cost_jpy >= 0),
    slippage_cost_jpy INTEGER NOT NULL CHECK (slippage_cost_jpy >= 0),
    commission_jpy INTEGER NOT NULL CHECK (commission_jpy >= 0),
    financing_jpy INTEGER NOT NULL CHECK (financing_jpy >= 0),
    conversion_cost_jpy INTEGER NOT NULL CHECK (conversion_cost_jpy >= 0),
    net_pnl_jpy INTEGER NOT NULL,
    realized_net_r REAL NOT NULL,
    maximum_favorable_excursion_r REAL,
    maximum_adverse_excursion_r REAL,
    quote_json TEXT NOT NULL,
    attribution_json TEXT NOT NULL,
    failure_json TEXT NOT NULL,
    learning_eligible INTEGER NOT NULL CHECK (learning_eligible IN (0, 1)),
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    FOREIGN KEY(trade_id) REFERENCES simulated_trade_opens(trade_id)
) STRICT;

CREATE INDEX IF NOT EXISTS simulated_trade_closes_time_idx
ON simulated_trade_closes(close_time_ns DESC, trade_id);

CREATE TABLE IF NOT EXISTS simulated_trade_close_observations (
    trade_id TEXT PRIMARY KEY,
    close_known_at_ns INTEGER NOT NULL,
    known_at_basis TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    FOREIGN KEY(trade_id) REFERENCES simulated_trade_closes(trade_id)
) STRICT;

CREATE INDEX IF NOT EXISTS simulated_trade_close_observations_known_idx
ON simulated_trade_close_observations(close_known_at_ns, trade_id);

CREATE TABLE IF NOT EXISTS session_close_requests (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    session_date_jst TEXT NOT NULL,
    requested_at_ns INTEGER NOT NULL,
    cutoff_time_ns INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    CHECK (cutoff_time_ns <= requested_at_ns)
) STRICT;

CREATE INDEX IF NOT EXISTS session_close_requests_time_idx
ON session_close_requests(cutoff_time_ns, request_id);

CREATE TABLE IF NOT EXISTS session_close_completions (
    completion_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    session_date_jst TEXT NOT NULL,
    completed_at_ns INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    FOREIGN KEY(request_id) REFERENCES session_close_requests(request_id)
) STRICT;

CREATE INDEX IF NOT EXISTS session_close_completions_time_idx
ON session_close_completions(completed_at_ns, completion_id);

CREATE TABLE IF NOT EXISTS simulated_position_marks (
    mark_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL,
    mark_time_ns INTEGER NOT NULL,
    observed_at_ns INTEGER NOT NULL,
    bid REAL NOT NULL CHECK (bid > 0),
    ask REAL NOT NULL CHECK (ask > 0),
    mid REAL NOT NULL CHECK (mid > 0),
    executable REAL NOT NULL CHECK (executable > 0),
    quote_to_jpy_rate REAL NOT NULL CHECK (quote_to_jpy_rate > 0),
    quote_conversion_source TEXT NOT NULL,
    gross_unrealized_pnl_jpy INTEGER NOT NULL,
    executable_unrealized_pnl_jpy INTEGER NOT NULL,
    spread_quote_cost_jpy INTEGER NOT NULL CHECK (spread_quote_cost_jpy >= 0),
    quote_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    FOREIGN KEY(trade_id) REFERENCES simulated_trade_opens(trade_id),
    CHECK (mark_time_ns <= observed_at_ns),
    UNIQUE(trade_id, mark_time_ns)
) STRICT;

CREATE INDEX IF NOT EXISTS simulated_position_marks_trade_time_idx
ON simulated_position_marks(trade_id, mark_time_ns DESC, mark_id);

CREATE TABLE IF NOT EXISTS ledger_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ('portfolio_created', 'trade_closed')),
    effective_time_ns INTEGER NOT NULL,
    amount_jpy INTEGER NOT NULL,
    trade_id TEXT UNIQUE,
    detail_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    FOREIGN KEY(trade_id) REFERENCES simulated_trade_opens(trade_id)
) STRICT;

CREATE INDEX IF NOT EXISTS ledger_events_time_idx
ON ledger_events(effective_time_ns, event_id);

CREATE TABLE IF NOT EXISTS portfolio_marks (
    mark_id TEXT PRIMARY KEY,
    effective_time_ns INTEGER NOT NULL,
    cash_balance_jpy INTEGER NOT NULL,
    peak_balance_jpy INTEGER NOT NULL,
    drawdown_jpy INTEGER NOT NULL CHECK (drawdown_jpy >= 0),
    source_event_id TEXT NOT NULL UNIQUE,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS portfolio_marks_time_idx
ON portfolio_marks(effective_time_ns, mark_id);

CREATE TABLE IF NOT EXISTS learning_exports (
    export_id TEXT PRIMARY KEY,
    created_at_ns INTEGER NOT NULL,
    as_of_ns INTEGER NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    dataset_sha256 TEXT NOT NULL,
    contract TEXT NOT NULL,
    promotion_eligible INTEGER NOT NULL CHECK (promotion_eligible = 0),
    detail_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    writer_id TEXT NOT NULL
) STRICT;
"""

_APPEND_ONLY_TABLES = (
    "portfolio_config",
    "portfolio_policy_updates",
    "session_events",
    "decision_intakes",
    "decision_records",
    "decision_horizon_assignments",
    "simulated_trade_opens",
    "simulated_trade_open_observations",
    "simulated_trade_closes",
    "simulated_trade_close_observations",
    "session_close_requests",
    "session_close_completions",
    "simulated_position_marks",
    "ledger_events",
    "portfolio_marks",
    "learning_exports",
)


def _immutable_triggers() -> tuple[str, ...]:
    statements: list[str] = []
    for table in _APPEND_ONLY_TABLES:
        statements.extend(
            (
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END;
                """,
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END;
                """,
            )
        )
    return tuple(statements)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _record_hash(payload: Mapping[str, object]) -> str:
    return _sha256_json(payload)


def _source_decision_payload(decision: Mapping[str, object]) -> dict[str, object]:
    """Remove locally-derived artifact identities for source-event comparison."""

    result = {str(key): value for key, value in decision.items()}
    result.pop("portfolio_risk_snapshot", None)
    provenance = result.pop("artifact_provenance", None)
    if not isinstance(provenance, Mapping):
        return result
    data_basis = str(provenance.get("data_sha256_basis") or "")
    model_basis = str(provenance.get("model_sha256_basis") or "")
    if data_basis != "upstream explicit data_sha256":
        result.pop("data_sha256", None)
    if model_basis != "upstream explicit model_sha256":
        result.pop("model_sha256", None)
    result.pop("policy_sha256", None)
    return result


def _required_text(value: object, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise InvalidVirtualRecord(f"{name} is required")
    return normalized


def _as_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidVirtualRecord(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_aware(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise InvalidVirtualRecord(f"{name} must be an ISO timestamp") from error
    return _as_utc(parsed, name)


def _to_ns(value: datetime) -> int:
    return int(_as_utc(value, "timestamp").timestamp() * 1_000_000_000)


def _from_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


def _positive_decimal(value: object, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise InvalidVirtualRecord(f"{name} must be numeric") from error
    if not number.is_finite() or number <= 0:
        raise InvalidVirtualRecord(f"{name} must be positive and finite")
    return number


def _optional_finite(value: object, name: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError) as error:
        raise InvalidVirtualRecord(f"{name} must be numeric") from error
    if not math.isfinite(number):
        raise InvalidVirtualRecord(f"{name} must be finite")
    return number


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise InvalidVirtualRecord(f"{name} must be an integer")
    try:
        number = int(str(value))
    except (TypeError, ValueError) as error:
        raise InvalidVirtualRecord(f"{name} must be an integer") from error
    if number < 0:
        raise InvalidVirtualRecord(f"{name} cannot be negative")
    return number


def _round_jpy(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _stable_id(prefix: str, payload: Mapping[str, object]) -> str:
    return f"{prefix}-{_record_hash(payload)[:24]}"


def _writer_lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.writer.lock")


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    timeout_seconds = 5.0 if read_only else 30.0
    if read_only:
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    else:
        connection = sqlite3.connect(path, timeout=timeout_seconds)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={int(timeout_seconds * 1_000)}")
    if not read_only:
        # This store intentionally avoids WAL because the bundled SQLite runtime
        # may predate the March 2026 WAL-reset fix.  A process lock plus FULL
        # synchronous rollback journaling is slower but safe for this periodic job.
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def _execute_schema_statements(connection: sqlite3.Connection, script: str) -> None:
    """Execute DDL inside the caller's transaction without executescript commits."""

    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        candidate = "\n".join(pending)
        if not sqlite3.complete_statement(candidate):
            continue
        statement = candidate.strip()
        if statement:
            connection.execute(statement)
        pending = []
    if "\n".join(pending).strip():
        raise VirtualPortfolioError("virtual portfolio schema contains incomplete SQL")


def _install_open_observation_schema(
    connection: sqlite3.Connection,
    *,
    writer_id: str,
) -> None:
    """Backfill the additive schema-v6 knowledge clock from immutable provenance."""

    rows = connection.execute("""
        SELECT o.trade_id, o.open_time_ns, o.quote_json, d.observed_at_ns
        FROM simulated_trade_opens o
        JOIN decision_records d ON d.decision_id = o.decision_id
        LEFT JOIN simulated_trade_open_observations k ON k.trade_id = o.trade_id
        WHERE k.trade_id IS NULL
        ORDER BY o.open_time_ns, o.trade_id
        """).fetchall()
    for row in rows:
        try:
            quote = json.loads(str(row["quote_json"]))
            if not isinstance(quote, Mapping):
                raise TypeError("quote payload is not an object")
            quote_ingested_at = _parse_aware(
                quote.get("ingested_time"),
                "legacy open quote ingested_time",
            )
        except (json.JSONDecodeError, TypeError, InvalidVirtualRecord) as error:
            raise VirtualPortfolioError(
                f"cannot derive schema-v6 open knowledge time: {row['trade_id']}"
            ) from error
        known_at_ns = max(
            int(row["open_time_ns"]),
            int(row["observed_at_ns"]),
            _to_ns(quote_ingested_at),
        )
        detail = {
            "migration": "virtual-portfolio-schema-v4-to-v6",
            "derived_from": [
                "open_time_ns",
                "decision_records.observed_at_ns",
                "quote_json.ingested_time",
            ],
        }
        payload = {
            "trade_id": str(row["trade_id"]),
            "open_known_at": _from_ns(known_at_ns).isoformat(),
            "known_at_basis": "legacy_provenance_backfill",
            "detail": detail,
        }
        connection.execute(
            """
            INSERT INTO simulated_trade_open_observations(
                trade_id, open_known_at_ns, known_at_basis, detail_json,
                record_sha256, writer_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(row["trade_id"]),
                known_at_ns,
                "legacy_provenance_backfill",
                _json(detail),
                _record_hash(payload),
                writer_id,
            ),
        )


def _install_close_observation_schema(
    connection: sqlite3.Connection,
    *,
    writer_id: str,
) -> None:
    """Backfill close knowledge from quote and canonical-event provenance."""

    rows = connection.execute("""
        SELECT c.trade_id, c.close_time_ns, c.quote_json, ok.open_known_at_ns,
               e.observed_at_ns AS canonical_observed_at_ns
        FROM simulated_trade_closes c
        JOIN simulated_trade_open_observations ok ON ok.trade_id = c.trade_id
        LEFT JOIN ledger_events l
          ON l.trade_id = c.trade_id AND l.event_type = 'trade_closed'
        LEFT JOIN portfolio_event_log e ON e.source_record_id = l.event_id
        LEFT JOIN simulated_trade_close_observations k ON k.trade_id = c.trade_id
        WHERE k.trade_id IS NULL
        ORDER BY c.close_time_ns, c.trade_id
        """).fetchall()
    for row in rows:
        try:
            quote = json.loads(str(row["quote_json"]))
            if not isinstance(quote, Mapping):
                raise TypeError("quote payload is not an object")
            quote_ingested_at = _parse_aware(
                quote.get("ingested_time"),
                "legacy close quote ingested_time",
            )
        except (json.JSONDecodeError, TypeError, InvalidVirtualRecord) as error:
            raise VirtualPortfolioError(
                f"cannot derive schema-v6 close knowledge time: {row['trade_id']}"
            ) from error
        provenance_times = [
            int(row["close_time_ns"]),
            int(row["open_known_at_ns"]),
            _to_ns(quote_ingested_at),
        ]
        if row["canonical_observed_at_ns"] is not None:
            provenance_times.append(int(row["canonical_observed_at_ns"]))
        known_at_ns = max(provenance_times)
        detail = {
            "migration": "virtual-portfolio-schema-v4-to-v6",
            "derived_from": [
                "close_time_ns",
                "simulated_trade_open_observations.open_known_at_ns",
                "quote_json.ingested_time",
                "portfolio_event_log.observed_at_ns_when_available",
            ],
        }
        payload = {
            "trade_id": str(row["trade_id"]),
            "close_known_at": _from_ns(known_at_ns).isoformat(),
            "known_at_basis": "legacy_provenance_backfill",
            "detail": detail,
        }
        connection.execute(
            """
            INSERT INTO simulated_trade_close_observations(
                trade_id, close_known_at_ns, known_at_basis, detail_json,
                record_sha256, writer_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(row["trade_id"]),
                known_at_ns,
                "legacy_provenance_backfill",
                _json(detail),
                _record_hash(payload),
                writer_id,
            ),
        )


def _initialize(
    connection: sqlite3.Connection,
    *,
    writer_id: str = "virtual-portfolio-schema-migration",
) -> None:
    current_application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if (current_version == 0 and current_application_id not in {0, APPLICATION_ID}) or (
        current_version != 0 and current_application_id != APPLICATION_ID
    ):
        raise VirtualPortfolioError(
            "refusing to initialize a database owned by another application: "
            f"app={current_application_id}, version={current_version}"
        )
    if current_version == SCHEMA_VERSION:
        _verify_schema(connection)
    elif current_version not in {0, 1, 3, 4, 5, 6}:
        raise VirtualPortfolioError(
            f"unsupported virtual portfolio schema: "
            f"app={current_application_id}, version={current_version}"
        )
    connection.execute("BEGIN IMMEDIATE")
    try:
        _execute_schema_statements(connection, _SCHEMA_SQL)
        install_v2_schema(connection)
        _install_open_observation_schema(connection, writer_id=writer_id)
        _install_close_observation_schema(connection, writer_id=writer_id)
        for trigger in _immutable_triggers():
            connection.execute(trigger)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        _verify_schema(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _verify_schema(connection: sqlite3.Connection) -> None:
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if application_id != APPLICATION_ID or user_version != SCHEMA_VERSION:
        raise VirtualPortfolioError(
            f"unsupported virtual portfolio schema: app={application_id}, version={user_version}"
        )
    missing_open_observations = int(connection.execute("""
            SELECT COUNT(*)
            FROM simulated_trade_opens o
            LEFT JOIN simulated_trade_open_observations k ON k.trade_id = o.trade_id
            WHERE k.trade_id IS NULL OR k.open_known_at_ns < o.open_time_ns
            """).fetchone()[0])
    if missing_open_observations:
        raise VirtualPortfolioError(
            "virtual portfolio has missing or invalid open knowledge timestamps"
        )
    missing_close_observations = int(connection.execute("""
            SELECT COUNT(*)
            FROM simulated_trade_closes c
            JOIN simulated_trade_open_observations ok ON ok.trade_id = c.trade_id
            LEFT JOIN simulated_trade_close_observations k ON k.trade_id = c.trade_id
            WHERE k.trade_id IS NULL OR k.close_known_at_ns < c.close_time_ns
               OR k.close_known_at_ns < ok.open_known_at_ns
            """).fetchone()[0])
    if missing_close_observations:
        raise VirtualPortfolioError(
            "virtual portfolio has missing or invalid close knowledge timestamps"
        )
    _verify_policy_updates(connection)
    observation_contracts = (
        (
            "simulated_trade_open_observations",
            "open_known_at_ns",
            "open_known_at",
            {
                "legacy_provenance_backfill",
                "historical_replay_as_of",
                "decision_observed_at",
            },
        ),
        (
            "simulated_trade_close_observations",
            "close_known_at_ns",
            "close_known_at",
            {
                "legacy_provenance_backfill",
                "historical_close_observed_at",
                "contemporaneous_close_observed_at",
            },
        ),
    )
    for table, timestamp_column, payload_key, allowed_bases in observation_contracts:
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY trade_id"):
            try:
                detail = json.loads(str(row["detail_json"]))
            except (json.JSONDecodeError, TypeError) as error:
                raise VirtualPortfolioError(
                    f"invalid observation detail JSON: {table}:{row['trade_id']}"
                ) from error
            basis = str(row["known_at_basis"])
            if basis not in allowed_bases or not isinstance(detail, Mapping):
                raise VirtualPortfolioError(
                    f"invalid observation provenance: {table}:{row['trade_id']}"
                )
            payload = {
                "trade_id": str(row["trade_id"]),
                payload_key: _from_ns(int(row[timestamp_column])).isoformat(),
                "known_at_basis": basis,
                "detail": dict(detail),
            }
            if _record_hash(payload) != str(row["record_sha256"]):
                raise VirtualPortfolioError(
                    f"observation record hash mismatch: {table}:{row['trade_id']}"
                )
        for suffix in ("no_update", "no_delete"):
            trigger_name = f"{table}_{suffix}"
            trigger = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'trigger' AND name = ?
                """,
                (trigger_name,),
            ).fetchone()
            if trigger is None:
                raise VirtualPortfolioError(
                    f"append-only observation trigger is missing: {trigger_name}"
                )


_Decoded = TypeVar("_Decoded")


def _decoded_json(value: object, name: str, expected: type[_Decoded]) -> _Decoded:
    try:
        decoded = json.loads(str(value))
    except (json.JSONDecodeError, TypeError) as error:
        raise VirtualPortfolioError(f"invalid {name} JSON") from error
    if not isinstance(decoded, expected):
        raise VirtualPortfolioError(f"invalid {name} JSON type")
    return decoded


def _label_time_basis(quote: Mapping[str, object]) -> str:
    """ラベル時刻の availability/ingestion が別時計かを自己申告する。

    `label_available_time` と `label_ingested_time` は現在どちらも
    close observation の1値から生成される。値が同じでも「取込ラグが0」なのか
    「2つ目の時計が無い」のかは下流から区別できないため、根拠を明示する。

    OANDA 経路は `oanda_raw_shadow.py` が receive_time と ingest_time を
    別事象として打刻するので将来 `dual_clock` になり得る。Dukascopy 経路は
    `dukascopy_quotes.py` が received を両方へ代入する単一時計のままなので
    `single_clock` を申告する。判定は収集器名ではなく quote が実際に持つ
    2値の一致で行う。名前で決め打ちすると配線が変わった時に嘘になる。
    """

    available = quote.get("available_time")
    ingested = quote.get("ingested_time")
    if not isinstance(available, str) or not isinstance(ingested, str):
        return "unknown"
    return "single_clock" if available == ingested else "dual_clock"


def _verify_canonical_source_record_hashes(
    connection: sqlite3.Connection,
    *,
    trade_id: str,
) -> None:
    """Recompute every mutable source record before sealing a v3 label."""

    opened = connection.execute(
        "SELECT * FROM simulated_trade_opens WHERE trade_id = ?",
        (trade_id,),
    ).fetchone()
    closed = connection.execute(
        "SELECT * FROM simulated_trade_closes WHERE trade_id = ?",
        (trade_id,),
    ).fetchone()
    if opened is None or closed is None:
        raise VirtualPortfolioError(f"canonical source trade is incomplete: {trade_id}")
    decision_id = str(opened["decision_id"])
    decision = connection.execute(
        "SELECT * FROM decision_records WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    assignment = connection.execute(
        "SELECT * FROM decision_horizon_assignments WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    open_observation = connection.execute(
        "SELECT * FROM simulated_trade_open_observations WHERE trade_id = ?",
        (trade_id,),
    ).fetchone()
    if decision is None or assignment is None or open_observation is None:
        raise VirtualPortfolioError(f"canonical source provenance is incomplete: {trade_id}")

    horizon_assignment = {
        "timeframe": str(assignment["timeframe"]),
        "allocation_class": str(assignment["allocation_class"]),
        "capacity_eligible": bool(assignment["capacity_eligible"]),
        "reason": str(assignment["reason_text"]),
        "policy_id": str(assignment["policy_id"]),
        "policy_sha256": str(assignment["policy_sha256"]),
    }
    assignment_payload = {
        "decision_id": decision_id,
        **horizon_assignment,
        "assigned_at": _from_ns(int(assignment["assigned_at_ns"])).isoformat(),
    }
    if _record_hash(assignment_payload) != str(assignment["record_sha256"]):
        raise VirtualPortfolioError(f"horizon assignment hash mismatch: {decision_id}")

    observation_detail = _decoded_json(
        open_observation["detail_json"],
        "open observation detail",
        dict,
    )
    recorded_decision = _decoded_json(decision["decision_json"], "decision", dict)
    rejection_reasons = _decoded_json(
        decision["rejection_reasons_json"],
        "decision rejection reasons",
        list,
    )
    decision_payload: dict[str, object] = {
        "decision_id": decision_id,
        "decision_time": _from_ns(int(decision["decision_time_ns"])).isoformat(),
        "observed_at": _from_ns(int(decision["observed_at_ns"])).isoformat(),
        "symbol": str(decision["symbol"]),
        "timeframe": str(decision["timeframe"]),
        "direction": str(decision["direction"]),
        "disposition": str(decision["disposition"]),
        "pit_eligible": bool(decision["pit_eligible"]),
        "reason_text": str(decision["reason_text"]),
        "rejection_reasons": rejection_reasons,
        "decision": recorded_decision,
        "data_sha256": str(decision["data_sha256"]),
        "model_sha256": str(decision["model_sha256"]),
        "policy_sha256": str(decision["policy_sha256"]),
        "execution_observation_mode": str(
            observation_detail.get("execution_observation_mode") or ""
        ),
        "safety_learning_policy_sha256": VirtualSafetyLearningPolicy().policy_sha256,
        "horizon_allocation": horizon_assignment,
    }
    if _record_hash(decision_payload) != str(decision["record_sha256"]):
        raise VirtualPortfolioError(f"decision record hash mismatch: {decision_id}")

    open_payload = {key: opened[key] for key in _OPEN_INSERT_KEYS if key != "record_sha256"}
    if _record_hash(open_payload) != str(opened["record_sha256"]):
        raise VirtualPortfolioError(f"open record hash mismatch: {trade_id}")

    close_payload: dict[str, object] = {
        "trade_id": trade_id,
        "close_time": _from_ns(int(closed["close_time_ns"])).isoformat(),
        "close_reason": str(closed["close_reason"]),
        "exit_bid": float(closed["exit_bid"]),
        "exit_ask": float(closed["exit_ask"]),
        "exit_mid": float(closed["exit_mid"]),
        "exit_executable": float(closed["exit_executable"]),
        "quote_to_jpy_rate": float(closed["quote_to_jpy_rate"]),
        "quote_conversion_source": str(closed["quote_conversion_source"]),
        "gross_market_pnl_jpy": int(closed["gross_market_pnl_jpy"]),
        "executable_pnl_jpy": int(closed["executable_pnl_jpy"]),
        "spread_quote_cost_jpy": int(closed["spread_quote_cost_jpy"]),
        "slippage_cost_jpy": int(closed["slippage_cost_jpy"]),
        "commission_jpy": int(closed["commission_jpy"]),
        "financing_jpy": int(closed["financing_jpy"]),
        "conversion_cost_jpy": int(closed["conversion_cost_jpy"]),
        "net_pnl_jpy": int(closed["net_pnl_jpy"]),
        "realized_net_r": float(closed["realized_net_r"]),
        "maximum_favorable_excursion_r": (
            float(closed["maximum_favorable_excursion_r"])
            if closed["maximum_favorable_excursion_r"] is not None
            else None
        ),
        "maximum_adverse_excursion_r": (
            float(closed["maximum_adverse_excursion_r"])
            if closed["maximum_adverse_excursion_r"] is not None
            else None
        ),
        "quote": _decoded_json(closed["quote_json"], "close quote", dict),
        "attribution": _decoded_json(closed["attribution_json"], "close attribution", dict),
        "failures": _decoded_json(closed["failure_json"], "close failures", list),
        "learning_eligible": bool(closed["learning_eligible"]),
    }
    if _record_hash(close_payload) != str(closed["record_sha256"]):
        raise VirtualPortfolioError(f"close record hash mismatch: {trade_id}")


def _verify_policy_updates(connection: sqlite3.Connection) -> None:
    configs = connection.execute("""
        SELECT portfolio_id, created_at_ns, policy_sha256, policy_json,
               initial_capital_jpy
        FROM portfolio_config
        ORDER BY portfolio_id
        """).fetchall()
    for config in configs:
        try:
            base_policy = VirtualPortfolioPolicy(**json.loads(str(config["policy_json"])))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise VirtualPortfolioError(
                f"invalid base policy JSON: {config['portfolio_id']}"
            ) from error
        base_policy.validate()
        if base_policy.policy_sha256 != str(
            config["policy_sha256"]
        ) or base_policy.initial_capital_jpy != int(config["initial_capital_jpy"]):
            raise VirtualPortfolioError(
                f"base policy hash does not reconcile: {config['portfolio_id']}"
            )
        previous_sha = base_policy.policy_sha256
        previous_effective_ns = int(config["created_at_ns"])
        previous_known_ns = int(config["created_at_ns"])
        for row in connection.execute(
            """
            SELECT * FROM portfolio_policy_updates
            WHERE portfolio_id = ?
            ORDER BY known_at_ns, effective_time_ns, update_id
            """,
            (str(config["portfolio_id"]),),
        ):
            effective_time_ns = int(row["effective_time_ns"])
            known_at_ns = int(row["known_at_ns"])
            if (
                effective_time_ns > known_at_ns
                or effective_time_ns <= previous_effective_ns
                or known_at_ns <= previous_known_ns
            ):
                raise VirtualPortfolioError(
                    f"invalid policy update time ordering: {row['update_id']}"
                )
            try:
                policy = VirtualPortfolioPolicy(**json.loads(str(row["policy_json"])))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise VirtualPortfolioError(
                    f"invalid policy update JSON: {row['update_id']}"
                ) from error
            policy.validate()
            if (
                policy.policy_sha256 != str(row["policy_sha256"])
                or policy.policy_id != str(row["policy_id"])
                or policy.initial_capital_jpy != base_policy.initial_capital_jpy
                or str(row["previous_policy_sha256"]) != previous_sha
                or not str(row["reason_text"]).strip()
                or not str(row["writer_id"]).strip()
            ):
                raise VirtualPortfolioError(
                    f"policy update chain does not reconcile: {row['update_id']}"
                )
            payload = {
                "portfolio_id": str(config["portfolio_id"]),
                "effective_time": _from_ns(effective_time_ns).isoformat(),
                "known_at": _from_ns(known_at_ns).isoformat(),
                "policy": asdict(policy),
                "previous_policy_sha256": previous_sha,
                "reason": str(row["reason_text"]),
            }
            expected_update_id = _stable_id("portfolio-policy-update", payload)
            expected_record_sha = _record_hash(
                {
                    "update_id": expected_update_id,
                    **payload,
                }
            )
            if (
                str(row["update_id"]) != expected_update_id
                or str(row["record_sha256"]) != expected_record_sha
            ):
                raise VirtualPortfolioError(
                    f"policy update record hash mismatch: {row['update_id']}"
                )
            previous_sha = policy.policy_sha256
            previous_effective_ns = effective_time_ns
            previous_known_ns = known_at_ns


def _period_start(now: datetime, period: str) -> datetime:
    local = _as_utc(now, "period now").astimezone(TOKYO)
    if period == "day":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif period == "month":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"unknown period: {period}")
    return start.astimezone(UTC)


def _moving_block_mean_ci(
    values: list[float],
    *,
    seed_basis: object,
    samples: int = 2_000,
) -> dict[str, object]:
    """Deterministic descriptive CI; never an out-of-sample performance claim."""

    if len(values) < 20:
        return {
            "status": "insufficient_data",
            "lower": None,
            "upper": None,
            "confidence": 0.95,
            "method": "moving_block_bootstrap_descriptive_v1",
            "minimum_rows": 20,
        }
    block_size = max(1, int(math.sqrt(len(values))))
    seed = int(_sha256_json(seed_basis)[:16], 16)
    generator = random.Random(seed)
    possible_starts = range(max(1, len(values) - block_size + 1))
    means: list[float] = []
    for _ in range(samples):
        sampled: list[float] = []
        while len(sampled) < len(values):
            start = generator.choice(possible_starts)
            sampled.extend(values[start : start + block_size])
        means.append(sum(sampled[: len(values)]) / len(values))
    means.sort()
    return {
        "status": "available",
        "lower": means[int(0.025 * (samples - 1))],
        "upper": means[int(0.975 * (samples - 1))],
        "confidence": 0.95,
        "method": "moving_block_bootstrap_descriptive_v1",
        "minimum_rows": 20,
        "samples": samples,
        "block_size": block_size,
    }


def _decision_pit_reasons(decision: Mapping[str, object]) -> list[str]:
    reasons: list[str] = []
    if not is_pit_eligible_entry(decision):
        reasons.append("PIT判断契約を証明できない")
    for field, label in (
        ("data_sha256", "データハッシュ"),
        ("model_sha256", "モデルハッシュ"),
        ("policy_sha256", "方針ハッシュ"),
    ):
        value = str(decision.get(field) or "").strip()
        if not value:
            reasons.append(f"{label}が無い")
        elif len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            reasons.append(f"{label}がSHA-256形式ではない")
    return reasons


def _classification(
    *,
    net_pnl_jpy: int,
    gross_market_pnl_jpy: int,
    conviction: float | None,
    close_reason: str,
    maximum_favorable_excursion_r: float | None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if net_pnl_jpy >= 0:
        return rows

    def finding(
        *,
        category: str,
        code: str,
        evidence: str,
        confidence: str,
        confidence_score: float,
    ) -> dict[str, object]:
        return {
            "category": category,
            "code": code,
            "evidence": evidence,
            "confidence": confidence,
            "confidence_score": confidence_score,
            "classifier_version": FAILURE_CLASSIFIER_VERSION,
        }

    if gross_market_pnl_jpy < 0:
        rows.append(
            finding(
                category="decision_failure",
                code="wrong_direction",
                evidence="mid価格ベースの市場損益が負",
                confidence="observed",
                confidence_score=1.0,
            )
        )
    elif gross_market_pnl_jpy > 0:
        rows.append(
            finding(
                category="execution_cost",
                code="cost_drag",
                evidence="市場損益は正だが全コスト後の純損益が負",
                confidence="observed",
                confidence_score=1.0,
            )
        )
    if "stop" in close_reason.lower():
        rows.append(
            finding(
                category="exit_failure",
                code="stop_loss",
                evidence=f"終了理由={close_reason}",
                confidence="observed",
                confidence_score=1.0,
            )
        )
    if conviction is not None and conviction >= 75:
        rows.append(
            finding(
                category="decision_failure",
                code="high_conviction_loss",
                evidence=f"確信度{conviction:.1f}で純損失",
                confidence="inferred",
                confidence_score=0.65,
            )
        )
    if maximum_favorable_excursion_r is not None and maximum_favorable_excursion_r >= 1:
        rows.append(
            finding(
                category="exit_failure",
                code="gave_back_favorable_move",
                evidence=f"MFE={maximum_favorable_excursion_r:.2f}R後に純損失",
                confidence="observed",
                confidence_score=1.0,
            )
        )
    return rows


@dataclass
class VirtualPortfolioStore:
    """Mutation/query facade; writer presence determines allowed operations."""

    path: Path
    connection: sqlite3.Connection
    writer_id: str | None

    def initialize_portfolio(
        self,
        *,
        policy: VirtualPortfolioPolicy,
        portfolio_id: str = DEFAULT_PORTFOLIO_ID,
        created_at: datetime | None = None,
    ) -> bool:
        writer_id = self._require_writer()
        policy.validate()
        created = _as_utc(created_at or datetime.now(UTC), "created_at")
        payload: dict[str, object] = {
            "portfolio_id": _required_text(portfolio_id, "portfolio_id"),
            "created_at": created.isoformat(),
            "currency": "JPY",
            "policy": asdict(policy),
            "initial_capital_jpy": policy.initial_capital_jpy,
        }
        record_sha = _record_hash(payload)
        existing = self.connection.execute(
            """
            SELECT policy_sha256, initial_capital_jpy
            FROM portfolio_config WHERE portfolio_id = ?
            """,
            (portfolio_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["policy_sha256"]) != policy.policy_sha256
                or int(existing["initial_capital_jpy"]) != policy.initial_capital_jpy
            ):
                raise ImmutableVirtualRecordConflict(
                    f"portfolio identity already exists with different policy: {portfolio_id}"
                )
            return False
        ledger_payload: dict[str, object] = {
            "event_type": "portfolio_created",
            "effective_time": created.isoformat(),
            "amount_jpy": policy.initial_capital_jpy,
            "portfolio_id": portfolio_id,
        }
        event_id = _stable_id("ledger", ledger_payload)
        mark_payload: dict[str, object] = {
            "effective_time": created.isoformat(),
            "cash_balance_jpy": policy.initial_capital_jpy,
            "peak_balance_jpy": policy.initial_capital_jpy,
            "drawdown_jpy": 0,
            "source_event_id": event_id,
        }
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO portfolio_config(
                    portfolio_id, created_at_ns, currency, policy_id, policy_sha256,
                    policy_json, initial_capital_jpy, writer_id, record_sha256
                ) VALUES (?, ?, 'JPY', ?, ?, ?, ?, ?, ?)
                """,
                (
                    portfolio_id,
                    _to_ns(created),
                    policy.policy_id,
                    policy.policy_sha256,
                    _json(asdict(policy)),
                    policy.initial_capital_jpy,
                    writer_id,
                    record_sha,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO ledger_events(
                    event_id, event_type, effective_time_ns, amount_jpy, trade_id,
                    detail_json, record_sha256, writer_id
                ) VALUES (?, 'portfolio_created', ?, ?, NULL, ?, ?, ?)
                """,
                (
                    event_id,
                    _to_ns(created),
                    policy.initial_capital_jpy,
                    _json(ledger_payload),
                    _record_hash(ledger_payload),
                    writer_id,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO portfolio_marks(
                    mark_id, effective_time_ns, cash_balance_jpy, peak_balance_jpy,
                    drawdown_jpy, source_event_id, record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    _stable_id("mark", mark_payload),
                    _to_ns(created),
                    policy.initial_capital_jpy,
                    policy.initial_capital_jpy,
                    event_id,
                    _record_hash(mark_payload),
                    writer_id,
                ),
            )
            append_accounting_event(
                self.connection,
                CanonicalEventRequest(
                    event_id=f"canonical-{event_id}",
                    event_type="portfolio_created",
                    source_record_id=event_id,
                    effective_time=created,
                    observed_at=created,
                    recorded_at=created,
                    payload={
                        **ledger_payload,
                        "legacy_event_id": event_id,
                        "legacy_record_sha256": _record_hash(ledger_payload),
                    },
                    policy_sha256=policy.policy_sha256,
                    writer_id=writer_id,
                    correlation_id=portfolio_id,
                ),
                transaction_id=f"journal-{event_id}",
                transaction_type="portfolio_created",
                description="固定100万円の仮想研究資本を開始",
                postings=(
                    JournalPosting(
                        account="asset.cash_jpy",
                        amount_jpy=policy.initial_capital_jpy,
                    ),
                    JournalPosting(
                        account="equity.initial_capital",
                        amount_jpy=-policy.initial_capital_jpy,
                    ),
                ),
            )
        return True

    def record_session_event(
        self,
        *,
        session_date_jst: str,
        event_type: str,
        event_time: datetime,
        detail: Mapping[str, object] | None = None,
    ) -> bool:
        writer_id = self._require_writer()
        if event_type not in {"opened", "closed"}:
            raise InvalidVirtualRecord("session event_type must be opened or closed")
        try:
            datetime.strptime(session_date_jst, "%Y-%m-%d")
        except ValueError as error:
            raise InvalidVirtualRecord("session_date_jst must be YYYY-MM-DD") from error
        moment = _as_utc(event_time, "session event_time")
        session_id = f"jst-{session_date_jst}"
        payload: dict[str, object] = {
            "session_id": session_id,
            "session_date_jst": session_date_jst,
            "event_type": event_type,
            "event_time": moment.isoformat(),
            "detail": dict(detail or {}),
        }
        event_id = _stable_id("session", payload)
        record_sha = _record_hash(payload)
        existing = self.connection.execute(
            """
            SELECT record_sha256 FROM session_events
            WHERE session_id = ? AND event_type = ?
            """,
            (session_id, event_type),
        ).fetchone()
        if existing is not None:
            return False
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO session_events(
                    event_id, session_id, session_date_jst, event_type, event_time_ns,
                    detail_json, writer_id, record_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    session_id,
                    session_date_jst,
                    event_type,
                    _to_ns(moment),
                    _json(dict(detail or {})),
                    writer_id,
                    record_sha,
                ),
            )
        return True

    def record_session_close_request(
        self,
        *,
        session_date_jst: str,
        requested_at: datetime,
        cutoff_time: datetime,
        detail: Mapping[str, object] | None = None,
    ) -> bool:
        """Persist a close request until delayed executable quotes arrive."""

        writer_id = self._require_writer()
        try:
            datetime.strptime(session_date_jst, "%Y-%m-%d")
        except ValueError as error:
            raise InvalidVirtualRecord("session_date_jst must be YYYY-MM-DD") from error
        requested = _as_utc(requested_at, "session close requested_at")
        cutoff = _as_utc(cutoff_time, "session close cutoff_time")
        if cutoff > requested:
            raise InvalidVirtualRecord("session close cutoff cannot be after request time")
        session_id = f"jst-{session_date_jst}"
        payload: dict[str, object] = {
            "session_id": session_id,
            "session_date_jst": session_date_jst,
            "requested_at": requested.isoformat(),
            "cutoff_time": cutoff.isoformat(),
            "detail": dict(detail or {}),
        }
        record_sha = _record_hash(payload)
        existing = self.connection.execute(
            """
            SELECT record_sha256 FROM session_close_requests
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["record_sha256"]) != record_sha:
                raise ImmutableVirtualRecordConflict(
                    f"session close request already exists with different content: {session_id}"
                )
            return False
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO session_close_requests(
                    request_id, session_id, session_date_jst, requested_at_ns,
                    cutoff_time_ns, detail_json, record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _stable_id("session-close", payload),
                    session_id,
                    session_date_jst,
                    _to_ns(requested),
                    _to_ns(cutoff),
                    _json(dict(detail or {})),
                    record_sha,
                    writer_id,
                ),
            )
        return True

    def pending_session_close_request(
        self,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, object] | None:
        """Return the latest request without its own immutable completion."""

        moment = _as_utc(as_of or datetime.now(UTC), "session close as_of")
        row = self.connection.execute(
            """
            SELECT r.*
            FROM session_close_requests r
            LEFT JOIN session_close_completions c
              ON c.request_id = r.request_id AND c.completed_at_ns <= ?
            WHERE c.completion_id IS NULL
              AND r.cutoff_time_ns <= ?
              AND r.requested_at_ns <= ?
            ORDER BY r.cutoff_time_ns DESC, r.request_id DESC
            LIMIT 1
            """,
            (_to_ns(moment), _to_ns(moment), _to_ns(moment)),
        ).fetchone()
        if row is None:
            return None
        return self._session_close_request_row(row)

    def record_session_close_completion(
        self,
        *,
        request_id: str,
        completed_at: datetime,
        detail: Mapping[str, object] | None = None,
    ) -> bool:
        """Complete exactly one persisted request after every position is flat."""

        writer_id = self._require_writer()
        request_identity = _required_text(request_id, "session close request_id")
        completed = _as_utc(completed_at, "session close completed_at")
        request = self.connection.execute(
            """
            SELECT *
            FROM session_close_requests
            WHERE request_id = ?
            """,
            (request_identity,),
        ).fetchone()
        if request is None:
            raise InvalidVirtualRecord(f"session close request does not exist: {request_identity}")
        if completed < _from_ns(int(request["cutoff_time_ns"])):
            raise InvalidVirtualRecord("session close completion cannot precede cutoff")
        if completed < _from_ns(int(request["requested_at_ns"])):
            raise InvalidVirtualRecord("session close completion cannot precede request")
        open_count = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM simulated_trade_opens o
                LEFT JOIN simulated_trade_close_observations ck
                  ON ck.trade_id = o.trade_id AND ck.close_known_at_ns <= ?
                LEFT JOIN simulated_trade_closes c
                  ON c.trade_id = o.trade_id AND c.close_time_ns <= ?
                 AND ck.trade_id IS NOT NULL
                WHERE o.open_time_ns <= ?
                  AND c.trade_id IS NULL
                """,
                (
                    _to_ns(completed),
                    _to_ns(completed),
                    _to_ns(completed),
                ),
            ).fetchone()[0]
        )
        if open_count:
            raise InvalidVirtualRecord(
                "session close completion requires zero open simulated positions"
            )
        reconciliation = self.reconciliation(as_of=completed)
        if reconciliation.get("passed") is not True:
            raise InvalidVirtualRecord(
                "session close completion requires reconciled canonical ledgers"
            )
        completion_detail = dict(detail or {})
        payload: dict[str, object] = {
            "request_id": request_identity,
            "session_id": str(request["session_id"]),
            "session_date_jst": str(request["session_date_jst"]),
            "completed_at": completed.isoformat(),
            "detail": completion_detail,
        }
        record_sha = _record_hash(payload)
        existing = self.connection.execute(
            """
            SELECT record_sha256
            FROM session_close_completions
            WHERE request_id = ?
            """,
            (request_identity,),
        ).fetchone()
        if existing is not None:
            if str(existing["record_sha256"]) != record_sha:
                raise ImmutableVirtualRecordConflict(
                    "session close request already has a different completion: "
                    f"{request_identity}"
                )
            return False
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO session_close_completions(
                    completion_id, request_id, session_id, session_date_jst,
                    completed_at_ns, detail_json, record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _stable_id("session-close-complete", payload),
                    request_identity,
                    str(request["session_id"]),
                    str(request["session_date_jst"]),
                    _to_ns(completed),
                    _json(completion_detail),
                    record_sha,
                    writer_id,
                ),
            )
        return True

    def session_close_completion(
        self,
        *,
        request_id: str | None = None,
        session_date_jst: str | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, object] | None:
        """Return an immutable request-scoped completion."""

        if (request_id is None) == (session_date_jst is None):
            raise InvalidVirtualRecord("provide exactly one of request_id or session_date_jst")
        if request_id is not None:
            where = "c.request_id = ?"
            parameter = _required_text(request_id, "session close request_id")
        else:
            assert session_date_jst is not None
            try:
                datetime.strptime(session_date_jst, "%Y-%m-%d")
            except ValueError as error:
                raise InvalidVirtualRecord("session_date_jst must be YYYY-MM-DD") from error
            where = "c.session_id = ?"
            parameter = f"jst-{session_date_jst}"
        parameters: list[object] = [parameter]
        as_of_clause = ""
        if as_of is not None:
            as_of_clause = " AND c.completed_at_ns <= ?"
            parameters.append(_to_ns(_as_utc(as_of, "session close completion as_of")))
        row = self.connection.execute(
            f"""
            SELECT c.*
            FROM session_close_completions c
            WHERE {where}{as_of_clause}
            """,
            tuple(parameters),
        ).fetchone()
        if row is None:
            return None
        return {
            "completion_id": str(row["completion_id"]),
            "request_id": str(row["request_id"]),
            "session_id": str(row["session_id"]),
            "session_date_jst": str(row["session_date_jst"]),
            "completed_at": _from_ns(int(row["completed_at_ns"])).isoformat(),
            "detail": json.loads(str(row["detail_json"])),
        }

    def session_close_request(
        self,
        *,
        session_date_jst: str,
    ) -> dict[str, object] | None:
        """Return the immutable close request for one JST session, if present."""

        try:
            datetime.strptime(session_date_jst, "%Y-%m-%d")
        except ValueError as error:
            raise InvalidVirtualRecord("session_date_jst must be YYYY-MM-DD") from error
        row = self.connection.execute(
            """
            SELECT *
            FROM session_close_requests
            WHERE session_id = ?
            """,
            (f"jst-{session_date_jst}",),
        ).fetchone()
        if row is None:
            return None
        return self._session_close_request_row(row)

    @staticmethod
    def _session_close_request_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "request_id": str(row["request_id"]),
            "session_id": str(row["session_id"]),
            "session_date_jst": str(row["session_date_jst"]),
            "requested_at": _from_ns(int(row["requested_at_ns"])).isoformat(),
            "cutoff_time": _from_ns(int(row["cutoff_time_ns"])).isoformat(),
            "detail": json.loads(str(row["detail_json"])),
        }

    def queue_decision(
        self,
        decision: Mapping[str, object],
        *,
        observed_at: datetime,
        source: str,
        batch_id: str,
    ) -> dict[str, object]:
        """Append a decision before any later price-path outcome is inspected."""

        writer_id = self._require_writer()
        decision_id = _required_text(decision.get("decision_id"), "decision_id")
        observed = _as_utc(observed_at, "observed_at")
        decision_time = _parse_aware(
            decision.get("prediction_time") or decision.get("ts"),
            "decision prediction_time",
        )
        if decision_time > observed:
            raise InvalidVirtualRecord("decision time cannot be after first observation")
        canonical = _json(dict(decision))
        terminal = self.connection.execute(
            "SELECT decision_json FROM decision_records WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if terminal is not None:
            terminal_decision = json.loads(str(terminal["decision_json"]))
            if _json(_source_decision_payload(terminal_decision)) != _json(
                _source_decision_payload(decision)
            ):
                raise ImmutableVirtualRecordConflict(
                    f"terminal decision_id has different content: {decision_id}"
                )
            return {"inserted": False, "decision_id": decision_id, "status": "terminal"}
        existing = self.connection.execute(
            "SELECT decision_json FROM decision_intakes WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["decision_json"]) != canonical:
                raise ImmutableVirtualRecordConflict(
                    f"decision intake already exists with different content: {decision_id}"
                )
            return {"inserted": False, "decision_id": decision_id, "status": "pending"}
        payload: dict[str, object] = {
            "decision_id": decision_id,
            "decision_time": decision_time.isoformat(),
            "observed_at": observed.isoformat(),
            "source": _required_text(source, "decision source"),
            "batch_id": _required_text(batch_id, "decision batch_id"),
            "decision": dict(decision),
        }
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO decision_intakes(
                    decision_id, decision_time_ns, observed_at_ns, source, batch_id,
                    decision_json, record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    _to_ns(decision_time),
                    _to_ns(observed),
                    str(payload["source"]),
                    str(payload["batch_id"]),
                    canonical,
                    _record_hash(payload),
                    writer_id,
                ),
            )
        return {"inserted": True, "decision_id": decision_id, "status": "pending"}

    def pending_decisions(self) -> list[dict[str, object]]:
        """Return immutable intakes that do not yet have a terminal disposition."""

        rows = self.connection.execute("""
            SELECT i.*
            FROM decision_intakes i
            LEFT JOIN decision_records d ON d.decision_id = i.decision_id
            WHERE d.decision_id IS NULL
            ORDER BY i.decision_time_ns, i.decision_id
            """).fetchall()
        return [
            {
                "decision_id": str(row["decision_id"]),
                "decision_time": _from_ns(int(row["decision_time_ns"])).isoformat(),
                "observed_at": _from_ns(int(row["observed_at_ns"])).isoformat(),
                "source": str(row["source"]),
                "batch_id": str(row["batch_id"]),
                "decision": json.loads(str(row["decision_json"])),
            }
            for row in rows
        ]

    def process_decision(
        self,
        decision: Mapping[str, object],
        *,
        quote: QuoteSnapshot | None = None,
        observed_at: datetime | None = None,
        quote_to_jpy_rate: float | None = None,
        quote_conversion_source: str | None = None,
        historical_replay: bool = False,
        replay_as_of: datetime | None = None,
        additional_rejection_reasons: list[str] | None = None,
    ) -> dict[str, object]:
        """Record one decision and optionally open an offline simulated trade."""

        writer_id = self._require_writer()
        observed = _as_utc(observed_at or datetime.now(UTC), "observed_at")
        decision_id = _required_text(decision.get("decision_id"), "decision_id")
        existing = self.connection.execute(
            """
            SELECT decision_json, disposition, rejection_reasons_json
            FROM decision_records WHERE decision_id = ?
            """,
            (decision_id,),
        ).fetchone()
        if existing is not None:
            existing_decision = json.loads(str(existing["decision_json"]))
            if _json(_source_decision_payload(existing_decision)) != _json(
                _source_decision_payload(decision)
            ):
                raise ImmutableVirtualRecordConflict(
                    f"decision_id already exists with different content: {decision_id}"
                )
            return {
                "inserted": False,
                "decision_id": decision_id,
                "disposition": str(existing["disposition"]),
                "rejection_reasons": json.loads(str(existing["rejection_reasons_json"])),
            }
        decision_time = _parse_aware(decision.get("prediction_time") or decision.get("ts"), "ts")
        if decision_time > observed:
            raise InvalidVirtualRecord("decision time cannot be after observation")
        policy = self.policy(as_of=decision_time, known_at=decision_time)
        recorded_decision = dict(decision)
        authoritative_risk_snapshot = self.prediction_portfolio_risk_snapshot(as_of=decision_time)
        supplied_risk_snapshot = recorded_decision.get("portfolio_risk_snapshot")
        if isinstance(supplied_risk_snapshot, Mapping) and _json(supplied_risk_snapshot) != _json(
            authoritative_risk_snapshot
        ):
            raise ImmutableVirtualRecordConflict(
                "portfolio_risk_snapshot does not match the authoritative ledger snapshot"
            )
        recorded_decision["portfolio_risk_snapshot"] = authoritative_risk_snapshot
        direction = str(decision.get("direction") or "unknown").lower()
        if direction not in {"long", "short", "neutral", "standby"}:
            direction = "unknown"
        symbol = _required_text(decision.get("symbol"), "symbol").upper()
        timeframe = str(decision.get("timeframe") or "unknown").strip() or "unknown"
        horizon_assignment = classify_virtual_horizon(timeframe)
        capacity_eligible = horizon_assignment["capacity_eligible"] is True
        reason_text = str(decision.get("reason") or decision.get("reason_text") or "").strip()
        pit_reasons = _decision_pit_reasons(decision)
        if (
            str(decision.get("policy_sha256") or "").strip()
            and str(decision.get("policy_sha256")) != policy.policy_sha256
        ):
            pit_reasons.append("判断時の方針ハッシュが仮想口座方針と一致しない")
        rejection_reasons = [*pit_reasons, *(additional_rejection_reasons or [])]
        try:
            assert_legacy_accounting_projected(self.connection)
        except InvalidV2Record:
            rejection_reasons.append("V2会計台帳の明示移行が未完了")
        if not is_market_open(decision_time):
            rejection_reasons.append("市場休場中")
        conversion_rate: Decimal | None = None
        stop: Decimal | None = None
        target: Decimal | None = None
        open_time = decision_time
        if not capacity_eligible:
            rejection_reasons.append(str(horizon_assignment["reason"]))
        else:
            if (observed - decision_time).total_seconds() > policy.decision_max_age_seconds:
                rejection_reasons.append("判断の初回取込が遅く、実時間シミュレーション対象外")
            if direction not in {"long", "short"}:
                rejection_reasons.append("方向判断がlong/shortではない")
            if quote is None:
                rejection_reasons.append("実行可能bid/askが無い")
            else:
                try:
                    if historical_replay:
                        replay_moment = _as_utc(
                            replay_as_of or observed,
                            "historical replay as_of",
                        )
                        quote.validate_replay(as_of=replay_moment)
                        quote_time = _as_utc(quote.event_time, "quote event_time")
                        delay = (quote_time - decision_time).total_seconds()
                        if delay < 0 or delay > policy.quote_max_age_seconds:
                            raise InvalidVirtualRecord(
                                "historical entry quote is outside the decision entry window"
                            )
                    else:
                        quote.validate(
                            as_of=decision_time,
                            max_age_seconds=policy.quote_max_age_seconds,
                        )
                except InvalidVirtualRecord as error:
                    rejection_reasons.append(str(error))
            try:
                conversion_rate, quote_conversion_source = self._conversion_contract(
                    symbol,
                    quote_to_jpy_rate,
                    quote_conversion_source,
                )
            except InvalidVirtualRecord as error:
                rejection_reasons.append(str(error))
            try:
                stop = _positive_decimal(decision.get("stop"), "stop")
                target = _positive_decimal(
                    decision.get("target1") or decision.get("target"), "target"
                )
            except InvalidVirtualRecord as error:
                rejection_reasons.append(str(error))
            open_time = (
                _as_utc(quote.event_time, "historical open_time")
                if historical_replay and quote is not None
                else decision_time
            )
            cutoff_reason = self._session_close_gate_reason(
                open_time,
                as_of=_as_utc(replay_as_of or observed, "session close gate as_of"),
            )
            if cutoff_reason:
                rejection_reasons.append(cutoff_reason)
            risk_reason = self._risk_gate_reason(
                open_time,
                policy,
                known_at=_as_utc(
                    replay_as_of or observed,
                    "risk gate known_at",
                ),
            )
            if risk_reason:
                rejection_reasons.append(risk_reason)
            safety_reason = self._safety_learning_gate_reason(
                recorded_decision,
                as_of=open_time,
                known_at=_as_utc(
                    replay_as_of or observed,
                    "safety gate known_at",
                ),
            )
            if safety_reason:
                rejection_reasons.append(safety_reason)

        open_values: dict[str, object] | None = None
        open_known_at: datetime | None = None
        open_known_at_basis: str | None = None
        if not rejection_reasons:
            assert quote is not None
            assert conversion_rate is not None
            assert stop is not None
            assert target is not None
            try:
                open_values = self._prepare_open(
                    decision=recorded_decision,
                    quote=quote,
                    conversion_rate=conversion_rate,
                    conversion_source=_required_text(
                        quote_conversion_source, "quote conversion source"
                    ),
                    stop=stop,
                    target=target,
                    observed=open_time,
                    policy=policy,
                )
                if historical_replay:
                    open_known_at = _as_utc(
                        replay_as_of or observed,
                        "historical open known_at",
                    )
                    open_known_at_basis = "historical_replay_as_of"
                else:
                    open_known_at = observed
                    open_known_at_basis = "decision_observed_at"
            except InvalidVirtualRecord as error:
                rejection_reasons.append(str(error))
        disposition = "opened" if not rejection_reasons else "abstained"
        decision_payload: dict[str, object] = {
            "decision_id": decision_id,
            "decision_time": decision_time.isoformat(),
            "observed_at": observed.isoformat(),
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "disposition": disposition,
            "pit_eligible": not pit_reasons,
            "reason_text": reason_text,
            "rejection_reasons": rejection_reasons,
            "decision": recorded_decision,
            "data_sha256": str(decision.get("data_sha256") or ""),
            "model_sha256": str(decision.get("model_sha256") or ""),
            "policy_sha256": str(decision.get("policy_sha256") or ""),
            "execution_observation_mode": (
                "delayed_historical_bid_ask_replay"
                if historical_replay
                else "contemporaneous_quote"
            ),
            "safety_learning_policy_sha256": VirtualSafetyLearningPolicy().policy_sha256,
            "horizon_allocation": horizon_assignment,
        }
        record_sha = _record_hash(decision_payload)
        assignment_payload = {
            "decision_id": decision_id,
            **horizon_assignment,
            "assigned_at": observed.isoformat(),
        }
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO decision_records(
                    decision_id, decision_time_ns, observed_at_ns, symbol, timeframe,
                    direction, disposition, pit_eligible, reason_text,
                    rejection_reasons_json, decision_json, data_sha256, model_sha256,
                    policy_sha256, record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    _to_ns(decision_time),
                    _to_ns(observed),
                    symbol,
                    timeframe,
                    direction,
                    disposition,
                    int(not pit_reasons),
                    reason_text,
                    _json(rejection_reasons),
                    _json(recorded_decision),
                    str(decision.get("data_sha256") or ""),
                    str(decision.get("model_sha256") or ""),
                    str(decision.get("policy_sha256") or ""),
                    record_sha,
                    writer_id,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO decision_horizon_assignments(
                    decision_id, timeframe, allocation_class, capacity_eligible,
                    policy_id, policy_sha256, reason_text, assigned_at_ns,
                    record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    str(horizon_assignment["timeframe"]),
                    str(horizon_assignment["allocation_class"]),
                    int(capacity_eligible),
                    str(horizon_assignment["policy_id"]),
                    str(horizon_assignment["policy_sha256"]),
                    str(horizon_assignment["reason"]),
                    _to_ns(observed),
                    _record_hash(assignment_payload),
                    writer_id,
                ),
            )
            if open_values is not None:
                assert quote is not None
                assert open_known_at is not None
                assert open_known_at_basis is not None
                self.connection.execute(
                    """
                    INSERT INTO simulated_trade_opens(
                        trade_id, decision_id, open_time_ns, symbol, timeframe, direction,
                        units, entry_bid, entry_ask, entry_mid, entry_executable,
                        stop_price, target_price, quote_to_jpy_rate,
                        quote_conversion_source, planned_risk_jpy, quote_json,
                        rationale_json, features_json, components_json, warnings_json,
                        data_sha256, model_sha256, policy_sha256, record_sha256, writer_id
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?
                    )
                    """,
                    tuple(open_values[key] for key in _OPEN_INSERT_KEYS) + (writer_id,),
                )
                observation_detail = {
                    "execution_observation_mode": decision_payload["execution_observation_mode"],
                    "quote_ingested_time": _as_utc(
                        quote.ingested_time,
                        "open quote ingested_time",
                    ).isoformat(),
                }
                observation_payload = {
                    "trade_id": str(open_values["trade_id"]),
                    "open_known_at": open_known_at.isoformat(),
                    "known_at_basis": open_known_at_basis,
                    "detail": observation_detail,
                }
                self.connection.execute(
                    """
                    INSERT INTO simulated_trade_open_observations(
                        trade_id, open_known_at_ns, known_at_basis, detail_json,
                        record_sha256, writer_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(open_values["trade_id"]),
                        _to_ns(open_known_at),
                        open_known_at_basis,
                        _json(observation_detail),
                        _record_hash(observation_payload),
                        writer_id,
                    ),
                )
        return {
            "inserted": True,
            "decision_id": decision_id,
            "disposition": disposition,
            "trade_id": open_values["trade_id"] if open_values else None,
            "rejection_reasons": rejection_reasons,
            "horizon_allocation": horizon_assignment,
        }

    def record_position_mark(self, request: PositionMarkRequest) -> dict[str, object]:
        """Append one quote-backed unrealized P&L mark without changing cash."""

        writer_id = self._require_writer()
        trade_id = _required_text(request.trade_id, "trade_id")
        opened = self.connection.execute(
            """
            SELECT o.*
            FROM simulated_trade_opens o
            WHERE o.trade_id = ?
            """,
            (trade_id,),
        ).fetchone()
        if opened is None:
            raise InvalidVirtualRecord(f"open simulated trade not found: {trade_id}")
        mark_time = _as_utc(request.mark_time, "position mark_time")
        open_time = _from_ns(int(opened["open_time_ns"]))
        if mark_time < open_time:
            raise InvalidVirtualRecord("position mark cannot precede open time")
        closed = self.connection.execute(
            "SELECT close_time_ns FROM simulated_trade_closes WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        if closed is not None and mark_time > _from_ns(int(closed["close_time_ns"])):
            raise InvalidVirtualRecord("position mark cannot follow close time")
        observed = _as_utc(request.observed_at or mark_time, "position mark observed_at")
        if request.historical_replay:
            request.quote.validate_replay(as_of=observed)
            if _as_utc(request.quote.event_time, "mark quote event_time") != mark_time:
                raise InvalidVirtualRecord(
                    "historical mark_time must equal executable quote event_time"
                )
        else:
            request.quote.validate(
                as_of=mark_time,
                max_age_seconds=self.policy(
                    as_of=open_time,
                    known_at=open_time,
                ).quote_max_age_seconds,
            )
        conversion_rate, conversion_source = self._conversion_contract(
            str(opened["symbol"]),
            request.quote_to_jpy_rate,
            request.quote_conversion_source,
        )
        bid = _positive_decimal(request.quote.bid, "mark bid")
        ask = _positive_decimal(request.quote.ask, "mark ask")
        mid = (bid + ask) / Decimal("2")
        direction = str(opened["direction"])
        executable = bid if direction == "long" else ask
        sign = Decimal("1") if direction == "long" else Decimal("-1")
        units = _positive_decimal(opened["units"], "mark units")
        entry_mid = Decimal(str(opened["entry_mid"]))
        entry_executable = Decimal(str(opened["entry_executable"]))
        gross = _round_jpy((mid - entry_mid) * sign * units * conversion_rate)
        executable_pnl = _round_jpy(
            (executable - entry_executable) * sign * units * conversion_rate
        )
        spread_cost = gross - executable_pnl
        if spread_cost < 0:
            raise InvalidVirtualRecord("mark executable P&L exceeds mid P&L")
        quote_payload = _quote_payload(request.quote)
        payload: dict[str, object] = {
            "trade_id": trade_id,
            "mark_time": mark_time.isoformat(),
            "observed_at": observed.isoformat(),
            "bid": float(bid),
            "ask": float(ask),
            "mid": float(mid),
            "executable": float(executable),
            "quote_to_jpy_rate": float(conversion_rate),
            "quote_conversion_source": conversion_source,
            "gross_unrealized_pnl_jpy": gross,
            "executable_unrealized_pnl_jpy": executable_pnl,
            "spread_quote_cost_jpy": spread_cost,
            "quote": quote_payload,
            "valuation_mode": (
                "delayed_historical_bid_ask_replay"
                if request.historical_replay
                else "contemporaneous_quote"
            ),
        }
        record_sha = _record_hash(payload)
        existing = self.connection.execute(
            """
            SELECT observed_at_ns, record_sha256 FROM simulated_position_marks
            WHERE trade_id = ? AND mark_time_ns = ?
            """,
            (trade_id, _to_ns(mark_time)),
        ).fetchone()
        if existing is not None:
            existing_sha = str(existing["record_sha256"])
            if existing_sha != record_sha:
                stored_observation_payload = {
                    **payload,
                    "observed_at": _from_ns(int(existing["observed_at_ns"])).isoformat(),
                }
                observation_retry_matches = _record_hash(stored_observation_payload) == existing_sha
            else:
                observation_retry_matches = True
            if not observation_retry_matches:
                raise ImmutableVirtualRecordConflict(
                    f"position mark already exists with different content: {trade_id}"
                )
            return {
                "inserted": False,
                "trade_id": trade_id,
                "mark_time": mark_time.isoformat(),
                "executable_unrealized_pnl_jpy": executable_pnl,
            }
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO simulated_position_marks(
                    mark_id, trade_id, mark_time_ns, observed_at_ns, bid, ask, mid,
                    executable, quote_to_jpy_rate, quote_conversion_source,
                    gross_unrealized_pnl_jpy, executable_unrealized_pnl_jpy,
                    spread_quote_cost_jpy, quote_json, record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _stable_id("position-mark", payload),
                    trade_id,
                    _to_ns(mark_time),
                    _to_ns(observed),
                    float(bid),
                    float(ask),
                    float(mid),
                    float(executable),
                    float(conversion_rate),
                    conversion_source,
                    gross,
                    executable_pnl,
                    spread_cost,
                    _json(quote_payload),
                    record_sha,
                    writer_id,
                ),
            )
        return {
            "inserted": True,
            "trade_id": trade_id,
            "mark_time": mark_time.isoformat(),
            "gross_unrealized_pnl_jpy": gross,
            "executable_unrealized_pnl_jpy": executable_pnl,
            "spread_quote_cost_jpy": spread_cost,
        }

    def close_trade(self, request: TradeCloseRequest) -> dict[str, object]:
        """Close a simulated trade and atomically append canonical P&L."""

        writer_id = self._require_writer()
        trade_id = _required_text(request.trade_id, "trade_id")
        opened = self.connection.execute(
            """
            SELECT o.*, ok.open_known_at_ns, d.decision_json
            FROM simulated_trade_opens o
            JOIN simulated_trade_open_observations ok ON ok.trade_id = o.trade_id
            JOIN decision_records d ON d.decision_id = o.decision_id
            WHERE o.trade_id = ?
            """,
            (trade_id,),
        ).fetchone()
        if opened is None:
            raise InvalidVirtualRecord(f"open simulated trade not found: {trade_id}")
        close_time = _as_utc(request.close_time, "close_time")
        open_time = _from_ns(int(opened["open_time_ns"]))
        if close_time <= open_time:
            raise InvalidVirtualRecord("close_time must be after open_time")
        close_known_at = (
            _as_utc(request.observed_at, "close observed_at")
            if request.observed_at is not None
            else close_time
        )
        if close_known_at < close_time:
            raise InvalidVirtualRecord("close observed_at cannot precede close_time")
        if close_known_at < _from_ns(int(opened["open_known_at_ns"])):
            raise InvalidVirtualRecord("close observed_at cannot precede open known_at")
        close_known_at_basis = (
            "historical_close_observed_at"
            if request.historical_replay
            else "contemporaneous_close_observed_at"
        )
        policy = self.policy(as_of=open_time, known_at=open_time)
        if request.historical_replay:
            if request.observed_at is None:
                raise InvalidVirtualRecord(
                    "historical close requires the replay observation timestamp"
                )
            request.quote.validate_replay(as_of=request.observed_at)
            if _as_utc(request.quote.event_time, "exit quote event_time") != close_time:
                raise InvalidVirtualRecord(
                    "historical close_time must equal the executable quote event_time"
                )
        else:
            request.quote.validate(
                as_of=close_time,
                max_age_seconds=policy.quote_max_age_seconds,
            )
        conversion_rate, conversion_source = self._conversion_contract(
            str(opened["symbol"]),
            request.quote_to_jpy_rate,
            request.quote_conversion_source,
        )
        entry_mid = Decimal(str(opened["entry_mid"]))
        entry_executable = Decimal(str(opened["entry_executable"]))
        exit_bid = _positive_decimal(request.quote.bid, "exit bid")
        exit_ask = _positive_decimal(request.quote.ask, "exit ask")
        exit_mid = (exit_bid + exit_ask) / Decimal("2")
        direction = str(opened["direction"])
        exit_executable = exit_bid if direction == "long" else exit_ask
        units = _positive_decimal(opened["units"], "units")
        sign = Decimal("1") if direction == "long" else Decimal("-1")
        gross = _round_jpy((exit_mid - entry_mid) * sign * units * conversion_rate)
        executable = _round_jpy(
            (exit_executable - entry_executable) * sign * units * conversion_rate
        )
        spread_quote_cost = gross - executable
        if spread_quote_cost < 0:
            raise InvalidVirtualRecord("executable P&L exceeds mid P&L; quote contract invalid")
        if request.costs_complete is not True:
            raise InvalidVirtualRecord("explicit complete cost evidence is required")
        cost_model_id = _required_text(request.cost_model_id, "cost_model_id")
        cost_model_version = _required_text(
            request.cost_model_version,
            "cost_model_version",
        )
        cost_source = _required_text(request.cost_source, "cost_source")
        slippage = _nonnegative_int(request.slippage_cost_jpy, "slippage_cost_jpy")
        commission = _nonnegative_int(request.commission_jpy, "commission_jpy")
        financing = _nonnegative_int(request.financing_jpy, "financing_jpy")
        conversion_cost = _nonnegative_int(request.conversion_cost_jpy, "conversion_cost_jpy")
        net = executable - slippage - commission - financing - conversion_cost
        if net != (gross - spread_quote_cost - slippage - commission - financing - conversion_cost):
            raise InvalidVirtualRecord("P&L attribution identity does not reconcile")
        planned_risk = int(opened["planned_risk_jpy"])
        realized_net_r = net / planned_risk
        close_reason = _required_text(request.close_reason, "close_reason")
        opposite_sign = -sign
        opposite_entry = (
            Decimal(str(opened["entry_bid"]))
            if direction == "long"
            else Decimal(str(opened["entry_ask"]))
        )
        opposite_exit = exit_ask if direction == "long" else exit_bid
        opposite_executable = _round_jpy(
            (opposite_exit - opposite_entry) * opposite_sign * units * conversion_rate
        )
        fixed_costs = slippage + commission + financing + conversion_cost
        counterfactuals: dict[str, object] = {
            "contract": COUNTERFACTUAL_CONTRACT,
            "causal_claim": False,
            "interpretation": "同一観測価格と固定コスト仮定による機械的比較であり、因果効果ではない",
            "no_trade": {
                "status": "available",
                "net_pnl_jpy": 0,
            },
            "opposite_direction": {
                "status": "available",
                "direction": "short" if direction == "long" else "long",
                "gross_market_pnl_jpy": -gross,
                "executable_pnl_jpy": opposite_executable,
                "net_pnl_jpy": opposite_executable - fixed_costs,
                "assumption": "同一units・同一時刻・同一追加コスト",
            },
            "decision_mid_fill": {
                "status": "available",
                "net_pnl_jpy": gross - fixed_costs,
                "spread_quote_cost_jpy": 0,
                "assumption": "entry/exitをmidで約定し追加コストは実績値と同じ",
            },
            "fixed_one_r": {
                "status": "available",
                "net_r": realized_net_r,
                "normalized_net_pnl_per_1r_jpy": round(realized_net_r * planned_risk),
                "assumption": "1Rを当該取引の計画リスク額として正規化",
            },
            "fixed_time_exit": {
                "status": ("available" if close_reason == "time_exit" else "unavailable"),
                "net_pnl_jpy": net if close_reason == "time_exit" else None,
                "reason": (
                    "実際の決済が固定時間決済"
                    if close_reason == "time_exit"
                    else "固定時間時点の完全なbid/ask・JPY換算・コスト証拠が未保存"
                ),
            },
            "alternative_stop_target": {
                "status": "unavailable",
                "net_pnl_jpy": None,
                "reason": "事前登録された代替Stop/Target方針が無いため事後最適化を禁止",
            },
        }
        decision = json.loads(str(opened["decision_json"]))
        conviction = _optional_finite(decision.get("conviction"), "conviction")
        mfe = _optional_finite(
            request.maximum_favorable_excursion_r,
            "maximum_favorable_excursion_r",
        )
        mae = _optional_finite(
            request.maximum_adverse_excursion_r,
            "maximum_adverse_excursion_r",
        )
        failures = _classification(
            net_pnl_jpy=net,
            gross_market_pnl_jpy=gross,
            conviction=conviction,
            close_reason=close_reason,
            maximum_favorable_excursion_r=mfe,
        )
        attribution = {
            "identity": (
                "net_pnl_jpy = gross_market_pnl_jpy - spread_quote_cost_jpy "
                "- slippage_cost_jpy - commission_jpy - financing_jpy "
                "- conversion_cost_jpy"
            ),
            "gross_market_pnl_jpy": gross,
            "spread_quote_cost_jpy": spread_quote_cost,
            "slippage_cost_jpy": slippage,
            "commission_jpy": commission,
            "financing_jpy": financing,
            "conversion_cost_jpy": conversion_cost,
            "net_pnl_jpy": net,
            "cost_contract": {
                "cost_model_id": cost_model_id,
                "cost_model_version": cost_model_version,
                "cost_source": cost_source,
                "costs_complete": True,
            },
            "execution_observation_mode": (
                "delayed_historical_bid_ask_replay"
                if request.historical_replay
                else "contemporaneous_quote"
            ),
            "replay_evidence": dict(request.replay_evidence or {}),
            "counterfactuals": counterfactuals,
        }
        quote_payload = _quote_payload(request.quote)
        close_payload: dict[str, object] = {
            "trade_id": trade_id,
            "close_time": close_time.isoformat(),
            "close_reason": close_reason,
            "exit_bid": float(exit_bid),
            "exit_ask": float(exit_ask),
            "exit_mid": float(exit_mid),
            "exit_executable": float(exit_executable),
            "quote_to_jpy_rate": float(conversion_rate),
            "quote_conversion_source": conversion_source,
            "gross_market_pnl_jpy": gross,
            "executable_pnl_jpy": executable,
            "spread_quote_cost_jpy": spread_quote_cost,
            "slippage_cost_jpy": slippage,
            "commission_jpy": commission,
            "financing_jpy": financing,
            "conversion_cost_jpy": conversion_cost,
            "net_pnl_jpy": net,
            "realized_net_r": realized_net_r,
            "maximum_favorable_excursion_r": mfe,
            "maximum_adverse_excursion_r": mae,
            "quote": quote_payload,
            "attribution": attribution,
            "failures": failures,
            "learning_eligible": True,
        }
        close_hash = _record_hash(close_payload)
        existing_close = self.connection.execute(
            """
            SELECT record_sha256, net_pnl_jpy
            FROM simulated_trade_closes WHERE trade_id = ?
            """,
            (trade_id,),
        ).fetchone()
        if existing_close is not None:
            if str(existing_close["record_sha256"]) != close_hash:
                raise ImmutableVirtualRecordConflict(
                    f"trade close already exists with different content: {trade_id}"
                )
            return {
                "inserted": False,
                "trade_id": trade_id,
                "net_pnl_jpy": int(existing_close["net_pnl_jpy"]),
            }
        ledger_payload: dict[str, object] = {
            "event_type": "trade_closed",
            "effective_time": close_time.isoformat(),
            "amount_jpy": net,
            "trade_id": trade_id,
            "close_record_sha256": close_hash,
        }
        ledger_id = _stable_id("ledger", ledger_payload)
        assert_legacy_accounting_projected(self.connection)
        balance_before = self.cash_balance_jpy(as_of=close_time)
        balance_after = balance_before + net
        peak_before = self.peak_balance_jpy(as_of=close_time)
        peak_after = max(peak_before, balance_after)
        mark_payload: dict[str, object] = {
            "effective_time": close_time.isoformat(),
            "cash_balance_jpy": balance_after,
            "peak_balance_jpy": peak_after,
            "drawdown_jpy": peak_after - balance_after,
            "source_event_id": ledger_id,
        }
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO simulated_trade_closes(
                    trade_id, close_time_ns, close_reason, exit_bid, exit_ask,
                    exit_mid, exit_executable, quote_to_jpy_rate,
                    quote_conversion_source, gross_market_pnl_jpy,
                    executable_pnl_jpy, spread_quote_cost_jpy, slippage_cost_jpy,
                    commission_jpy, financing_jpy, conversion_cost_jpy,
                    net_pnl_jpy, realized_net_r, maximum_favorable_excursion_r,
                    maximum_adverse_excursion_r, quote_json, attribution_json,
                    failure_json, learning_eligible, record_sha256, writer_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    trade_id,
                    _to_ns(close_time),
                    close_reason,
                    float(exit_bid),
                    float(exit_ask),
                    float(exit_mid),
                    float(exit_executable),
                    float(conversion_rate),
                    conversion_source,
                    gross,
                    executable,
                    spread_quote_cost,
                    slippage,
                    commission,
                    financing,
                    conversion_cost,
                    net,
                    realized_net_r,
                    mfe,
                    mae,
                    _json(quote_payload),
                    _json(attribution),
                    _json(failures),
                    1,
                    close_hash,
                    writer_id,
                ),
            )
            close_observation_detail = {
                "execution_observation_mode": attribution["execution_observation_mode"],
                "quote_ingested_time": _as_utc(
                    request.quote.ingested_time,
                    "close quote ingested_time",
                ).isoformat(),
            }
            close_observation_payload = {
                "trade_id": trade_id,
                "close_known_at": close_known_at.isoformat(),
                "known_at_basis": close_known_at_basis,
                "detail": close_observation_detail,
            }
            self.connection.execute(
                """
                INSERT INTO simulated_trade_close_observations(
                    trade_id, close_known_at_ns, known_at_basis, detail_json,
                    record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    _to_ns(close_known_at),
                    close_known_at_basis,
                    _json(close_observation_detail),
                    _record_hash(close_observation_payload),
                    writer_id,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO ledger_events(
                    event_id, event_type, effective_time_ns, amount_jpy, trade_id,
                    detail_json, record_sha256, writer_id
                ) VALUES (?, 'trade_closed', ?, ?, ?, ?, ?, ?)
                """,
                (
                    ledger_id,
                    _to_ns(close_time),
                    net,
                    trade_id,
                    _json(ledger_payload),
                    _record_hash(ledger_payload),
                    writer_id,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO portfolio_marks(
                    mark_id, effective_time_ns, cash_balance_jpy, peak_balance_jpy,
                    drawdown_jpy, source_event_id, record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _stable_id("mark", mark_payload),
                    _to_ns(close_time),
                    balance_after,
                    peak_after,
                    peak_after - balance_after,
                    ledger_id,
                    _record_hash(mark_payload),
                    writer_id,
                ),
            )
            append_accounting_event(
                self.connection,
                CanonicalEventRequest(
                    event_id=f"canonical-{ledger_id}",
                    event_type="trade_closed",
                    source_record_id=ledger_id,
                    effective_time=close_time,
                    observed_at=close_known_at,
                    recorded_at=max(datetime.now(UTC), close_known_at),
                    payload={
                        **close_payload,
                        "legacy_event_id": ledger_id,
                        "legacy_record_sha256": _record_hash(ledger_payload),
                    },
                    policy_sha256=str(opened["policy_sha256"]),
                    writer_id=writer_id,
                    correlation_id=trade_id,
                    causation_id=str(opened["decision_id"]),
                ),
                transaction_id=f"journal-{ledger_id}",
                transaction_type="trade_closed",
                description=f"仮想取引{trade_id}の全費用後損益を確定",
                postings=(
                    JournalPosting(account="asset.cash_jpy", amount_jpy=net),
                    JournalPosting(
                        account="pnl.market_move_unseparated_fx",
                        amount_jpy=-gross,
                        detail={
                            "limitation": (
                                "legacy V1 close does not separate market move "
                                "from FX translation"
                            )
                        },
                    ),
                    JournalPosting(
                        account="expense.spread",
                        amount_jpy=spread_quote_cost,
                    ),
                    JournalPosting(
                        account="expense.slippage",
                        amount_jpy=slippage,
                    ),
                    JournalPosting(
                        account="expense.commission",
                        amount_jpy=commission,
                    ),
                    JournalPosting(
                        account="expense.financing",
                        amount_jpy=financing,
                    ),
                    JournalPosting(
                        account="expense.conversion_spread",
                        amount_jpy=conversion_cost,
                    ),
                ),
            )
        return {
            "inserted": True,
            "trade_id": trade_id,
            "net_pnl_jpy": net,
            "realized_net_r": realized_net_r,
            "attribution": attribution,
            "failures": failures,
        }

    def export_learning_rows(
        self,
        *,
        as_of: datetime | None = None,
        record_export: bool = False,
    ) -> dict[str, object]:
        """Return PIT-safe, mature outcomes for offline challenger research."""

        moment = _as_utc(as_of or datetime.now(UTC), "learning as_of")
        eligible_row = self.connection.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM simulated_trade_opens o
            JOIN simulated_trade_closes c ON c.trade_id = o.trade_id
            JOIN decision_horizon_assignments h ON h.decision_id = o.decision_id
            WHERE c.learning_eligible = 1 AND c.close_time_ns <= ?
              AND h.allocation_class = 'intraday_capacity'
            """,
            (_to_ns(moment),),
        ).fetchone()
        eligible_rows = int(eligible_row["row_count"])
        rows = self.connection.execute(
            """
            SELECT
                o.trade_id, o.decision_id, d.decision_time_ns, o.open_time_ns,
                o.symbol, o.timeframe,
                o.direction, o.planned_risk_jpy, o.features_json,
                o.components_json, d.decision_json,
                o.entry_bid, o.entry_ask, o.entry_mid, o.entry_executable,
                o.units, o.quote_to_jpy_rate AS entry_quote_to_jpy_rate,
                o.quote_conversion_source AS entry_quote_conversion_source,
                o.stop_price, o.target_price, o.quote_json AS entry_quote_json,
                o.data_sha256, o.model_sha256, o.policy_sha256,
                o.record_sha256 AS open_record_sha256,
                d.record_sha256 AS decision_record_sha256,
                ok.open_known_at_ns, ok.known_at_basis AS open_known_at_basis,
                ok.record_sha256 AS open_observation_sha256,
                c.close_time_ns, c.exit_bid, c.exit_ask, c.exit_mid,
                c.exit_executable, c.quote_json AS exit_quote_json,
                c.quote_to_jpy_rate AS exit_quote_to_jpy_rate,
                c.quote_conversion_source AS exit_quote_conversion_source,
                c.net_pnl_jpy, c.realized_net_r,
                c.close_reason, c.maximum_favorable_excursion_r,
                c.maximum_adverse_excursion_r, c.attribution_json, c.failure_json,
                c.record_sha256 AS close_record_sha256,
                ck.close_known_at_ns,
                ck.known_at_basis AS close_known_at_basis,
                ck.record_sha256 AS close_observation_sha256
            FROM simulated_trade_opens o
            JOIN decision_records d ON d.decision_id = o.decision_id
            JOIN decision_horizon_assignments h ON h.decision_id = o.decision_id
            JOIN simulated_trade_open_observations ok ON ok.trade_id = o.trade_id
            JOIN simulated_trade_closes c ON c.trade_id = o.trade_id
            JOIN simulated_trade_close_observations ck ON ck.trade_id = c.trade_id
            WHERE c.learning_eligible = 1 AND c.close_time_ns <= ?
              AND ok.open_known_at_ns <= ? AND ck.close_known_at_ns <= ?
              AND h.allocation_class = 'intraday_capacity'
            ORDER BY c.close_time_ns, o.trade_id
            """,
            (_to_ns(moment), _to_ns(moment), _to_ns(moment)),
        ).fetchall()
        dataset: list[dict[str, object]] = []
        for row in rows:
            _verify_canonical_source_record_hashes(
                self.connection,
                trade_id=str(row["trade_id"]),
            )
            decision = json.loads(str(row["decision_json"]))
            exit_quote = json.loads(str(row["exit_quote_json"]))
            input_context = decision.get("input_context")
            input_features = decision.get("input_features")
            input_feature_masks = decision.get("input_feature_masks")
            learning_dimensions = decision.get("learning_dimensions")
            execution_snapshot = decision.get("execution")
            portfolio_risk_snapshot = decision.get("portfolio_risk_snapshot")
            source_record_ids = decision.get("source_record_ids")
            learning_row: dict[str, object] = {
                "trade_id": str(row["trade_id"]),
                "decision_id": str(row["decision_id"]),
                "prediction_time": _from_ns(int(row["decision_time_ns"])).isoformat(),
                "simulated_entry_time": _from_ns(int(row["open_time_ns"])).isoformat(),
                "label_end_time": _from_ns(int(row["close_time_ns"])).isoformat(),
                "label_available_time": _from_ns(int(row["close_known_at_ns"])).isoformat(),
                "label_ingested_time": _from_ns(int(row["close_known_at_ns"])).isoformat(),
                "label_knowledge_basis": str(row["close_known_at_basis"]),
                "label_time_basis": _label_time_basis(exit_quote),
                "simulated_entry_available_time": _from_ns(
                    int(row["open_known_at_ns"])
                ).isoformat(),
                "simulated_entry_knowledge_basis": str(row["open_known_at_basis"]),
                "symbol": str(row["symbol"]),
                "timeframe": str(row["timeframe"]),
                "direction": str(row["direction"]),
                "planned_risk_jpy": int(row["planned_risk_jpy"]),
                "entry_bid": float(row["entry_bid"]),
                "entry_ask": float(row["entry_ask"]),
                "entry_mid": float(row["entry_mid"]),
                "entry_executable": float(row["entry_executable"]),
                "units": float(row["units"]),
                "entry_quote_to_jpy_rate": float(row["entry_quote_to_jpy_rate"]),
                "entry_quote_conversion_source": str(row["entry_quote_conversion_source"]),
                "stop_price": float(row["stop_price"]),
                "target_price": float(row["target_price"]),
                "exit_bid": float(row["exit_bid"]),
                "exit_ask": float(row["exit_ask"]),
                "exit_mid": float(row["exit_mid"]),
                "exit_executable": float(row["exit_executable"]),
                "exit_quote_to_jpy_rate": float(row["exit_quote_to_jpy_rate"]),
                "exit_quote_conversion_source": str(row["exit_quote_conversion_source"]),
                "entry_quote": json.loads(str(row["entry_quote_json"])),
                "exit_quote": json.loads(str(row["exit_quote_json"])),
                "features": json.loads(str(row["features_json"])),
                "components": json.loads(str(row["components_json"])),
                "learning_dimensions": (
                    dict(learning_dimensions) if isinstance(learning_dimensions, Mapping) else {}
                ),
                "input_context_id": str(decision.get("input_context_id") or ""),
                "input_context": (
                    dict(input_context) if isinstance(input_context, Mapping) else {}
                ),
                "input_features": (
                    dict(input_features) if isinstance(input_features, Mapping) else {}
                ),
                "input_feature_masks": (
                    dict(input_feature_masks) if isinstance(input_feature_masks, Mapping) else {}
                ),
                "execution_snapshot": (
                    dict(execution_snapshot) if isinstance(execution_snapshot, Mapping) else {}
                ),
                "portfolio_risk_snapshot": (
                    dict(portfolio_risk_snapshot)
                    if isinstance(portfolio_risk_snapshot, Mapping)
                    else {}
                ),
                "source_record_ids": (
                    [str(value) for value in source_record_ids]
                    if isinstance(source_record_ids, list | tuple)
                    else []
                ),
                "data_sha256": str(row["data_sha256"]),
                "model_sha256": str(row["model_sha256"]),
                "policy_sha256": str(row["policy_sha256"]),
                "decision_record_sha256": str(row["decision_record_sha256"]),
                "open_record_sha256": str(row["open_record_sha256"]),
                "open_observation_sha256": str(row["open_observation_sha256"]),
                "close_record_sha256": str(row["close_record_sha256"]),
                "close_observation_sha256": str(row["close_observation_sha256"]),
                "net_pnl_jpy": int(row["net_pnl_jpy"]),
                "realized_net_r": float(row["realized_net_r"]),
                "close_reason": str(row["close_reason"]),
                "maximum_favorable_excursion_r": (
                    float(row["maximum_favorable_excursion_r"])
                    if row["maximum_favorable_excursion_r"] is not None
                    else None
                ),
                "maximum_adverse_excursion_r": (
                    float(row["maximum_adverse_excursion_r"])
                    if row["maximum_adverse_excursion_r"] is not None
                    else None
                ),
                "attribution": json.loads(str(row["attribution_json"])),
                "failures": json.loads(str(row["failure_json"])),
                "contract": VIRTUAL_LEARNING_CONTRACT,
                "research_only": True,
                "promotion_eligible": False,
                "allocation_scope": "intraday_capacity",
                "feature_availability_contract": "decision-time-frozen-only",
            }
            canonical = canonical_outcome_from_virtual_row(learning_row)
            canonical_payload = canonical.to_dict()
            learning_row.update(canonical_payload)
            learning_row["canonical_outcome"] = canonical_payload
            learning_row["canonical_outcome_sha256"] = canonical_outcome_sha256(canonical)
            dataset.append(learning_row)
        excluded_row = self.connection.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM simulated_trade_opens o
            JOIN simulated_trade_closes c ON c.trade_id = o.trade_id
            JOIN simulated_trade_close_observations ck ON ck.trade_id = c.trade_id
            LEFT JOIN decision_horizon_assignments h ON h.decision_id = o.decision_id
            WHERE c.learning_eligible = 1 AND c.close_time_ns <= ?
              AND ck.close_known_at_ns <= ?
              AND (h.allocation_class IS NULL
                   OR h.allocation_class <> 'intraday_capacity')
            """,
            (_to_ns(moment), _to_ns(moment)),
        ).fetchone()
        dataset_sha = _sha256_json(dataset)
        result: dict[str, object] = {
            "contract": VIRTUAL_LEARNING_CONTRACT,
            "as_of": moment.isoformat(),
            "row_count": len(dataset),
            "eligible_rows": eligible_rows,
            "dataset_sha256": dataset_sha,
            "research_only": True,
            "promotion_eligible": False,
            "allocation_scope": "intraday_capacity",
            "excluded_legacy_or_observer_rows": int(excluded_row["row_count"]),
            "canonical_net_r": canonical_label_summary(
                dataset,
                eligible_rows=eligible_rows,
            ),
            "rows": dataset,
        }
        if record_export:
            writer_id = self._require_writer()
            export_payload: dict[str, object] = {
                key: value for key, value in result.items() if key != "rows"
            }
            export_id = _stable_id("learning", export_payload)
            with self.connection:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO learning_exports(
                        export_id, created_at_ns, as_of_ns, row_count, dataset_sha256,
                        contract, promotion_eligible, detail_json, record_sha256, writer_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        export_id,
                        _to_ns(datetime.now(UTC)),
                        _to_ns(moment),
                        len(dataset),
                        dataset_sha,
                        VIRTUAL_LEARNING_CONTRACT,
                        _json(export_payload),
                        _record_hash(export_payload),
                        writer_id,
                    ),
                )
            result["export_id"] = export_id
        return result

    def shortfall_attribution(
        self,
        *,
        session_start: datetime,
        session_end: datetime,
        realized_net_pnl_jpy: int,
        total_cost_jpy: int,
        gross_market_pnl_jpy: int,
        trade_count: int,
    ) -> dict[str, object]:
        """日次目標に届かなかった額を、台帳の実数で3段階へ分解する。

        目的は「なぜ届かなかったか」を翌日の判断材料にすること。3段階は同じ
        円単位で、合計は必ず shortfall と一致する(恒等式をテストで固定)。

        - ``cost``: 実際に差し引かれたコスト(台帳の実数)
        - ``execution``: 建った取引が出した市場損益のうち負の分
        - ``opportunity``: 上記で説明できない**残余**

        ⚠️ ``opportunity`` は「建てていれば勝てた額」ではない。測定できない量を
        受け止める残余であり、予測でも反実仮想でもない。混同を防ぐため
        ``opportunity_is_residual_not_forecast`` を契約に明示する。

        目標値は KPI であって学習ラベルではない。この分解結果を特徴量や
        自動調整の入力にしてはならない(``not_training_label`` を明示)。
        """

        policy = self.policy(as_of=session_end, known_at=session_end)
        start_ns = _to_ns(_as_utc(session_start, "session_start"))
        end_ns = _to_ns(_as_utc(session_end, "session_end"))
        target = int(policy.daily_target_jpy)
        shortfall = target - int(realized_net_pnl_jpy)

        directional = int(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM decision_records
                WHERE direction IN ('long', 'short')
                  AND decision_time_ns >= ? AND decision_time_ns <= ?
                """,
                (start_ns, end_ns),
            ).fetchone()[0]
        )
        reason_counts: Counter[str] = Counter()
        abstained = 0
        for row in self.connection.execute(
            """
            SELECT rejection_reasons_json FROM decision_records
            WHERE direction IN ('long', 'short') AND disposition = 'abstained'
              AND decision_time_ns >= ? AND decision_time_ns <= ?
            """,
            (start_ns, end_ns),
        ):
            abstained += 1
            reasons = json.loads(str(row[0]))
            if isinstance(reasons, list) and reasons:
                # 先頭理由を支配的要因として数える(理由は評価順に並ぶ)
                reason_counts[str(reasons[0])] += 1
        dominant_reason, dominant_count = (
            reason_counts.most_common(1)[0] if reason_counts else ("", 0)
        )

        # 枠がどれだけ塞がっていたか。capacity x セッション長が上限。
        occupied_ns = 0
        for row in self.connection.execute(
            """
            SELECT o.open_time_ns, c.close_time_ns
            FROM simulated_trade_opens o
            LEFT JOIN simulated_trade_closes c ON c.trade_id = o.trade_id
            WHERE o.open_time_ns <= ?
              AND (c.close_time_ns IS NULL OR c.close_time_ns >= ?)
            """,
            (end_ns, start_ns),
        ):
            opened = max(int(row[0]), start_ns)
            closed = min(int(row[1]) if row[1] is not None else end_ns, end_ns)
            if closed > opened:
                occupied_ns += closed - opened
        session_ns = max(0, end_ns - start_ns)
        capacity_ns = session_ns * max(1, int(policy.max_open_positions))
        slot_utilization = (occupied_ns / capacity_ns) if capacity_ns else None

        # 3段階を同じ符号規約(正 = 未達への寄与)へ揃える。
        cost_share = max(0, int(total_cost_jpy))
        execution_share = max(0, -int(gross_market_pnl_jpy))
        opportunity_share = shortfall - cost_share - execution_share

        stages = {
            "opportunity": opportunity_share,
            "execution": execution_share,
            "cost": cost_share,
        }
        binding = max(stages, key=lambda key: stages[key]) if shortfall > 0 else "none"

        return {
            "contract": "virtual-portfolio-shortfall-attribution-v1",
            "not_training_label": True,
            "opportunity_is_residual_not_forecast": True,
            "target_jpy": target,
            "realized_net_pnl_jpy": int(realized_net_pnl_jpy),
            "shortfall_jpy": shortfall,
            "shortfall_r": (
                round(
                    shortfall / (policy.initial_capital_jpy * policy.risk_per_trade_bps / 10_000), 4
                )
                if policy.risk_per_trade_bps
                else None
            ),
            "target_met": shortfall <= 0,
            "binding_constraint": binding,
            "stages": stages,
            "opportunity": {
                "directional_decisions": directional,
                "abstained": abstained,
                "abstain_reasons": dict(reason_counts.most_common()),
                "dominant_reason": dominant_reason,
                "dominant_share": (round(dominant_count / abstained, 4) if abstained else None),
            },
            "execution": {
                "trades": int(trade_count),
                "gross_market_pnl_jpy": int(gross_market_pnl_jpy),
                "slot_hours_occupied": round(occupied_ns / 3_600_000_000_000, 3),
                "slot_hours_available": round(capacity_ns / 3_600_000_000_000, 3),
                "slot_utilization": (
                    round(slot_utilization, 4) if slot_utilization is not None else None
                ),
            },
            "cost": {"total_cost_jpy": cost_share},
        }

    def snapshot(self, *, now: datetime | None = None, trade_limit: int = 100) -> dict[str, Any]:
        moment = _as_utc(now or datetime.now(UTC), "snapshot now")
        policy_state = self._policy_state(as_of=moment, known_at=moment)
        policy = policy_state["policy"]
        assert isinstance(policy, VirtualPortfolioPolicy)
        balance = self.cash_balance_jpy(as_of=moment, known_at=moment)
        peak = self.peak_balance_jpy(as_of=moment, known_at=moment)
        today_pnl = self._period_pnl(moment, "day", known_at=moment)
        weekly_pnl = self._period_pnl(moment, "week", known_at=moment)
        monthly_pnl = self._period_pnl(moment, "month", known_at=moment)
        open_rows = self.connection.execute(
            """
            SELECT o.*, k.open_known_at_ns, k.known_at_basis
            FROM simulated_trade_opens o
            JOIN simulated_trade_open_observations k ON k.trade_id = o.trade_id
            LEFT JOIN simulated_trade_close_observations ck
              ON ck.trade_id = o.trade_id AND ck.close_known_at_ns <= ?
            LEFT JOIN simulated_trade_closes c
              ON c.trade_id = o.trade_id AND c.close_time_ns <= ?
             AND ck.trade_id IS NOT NULL
            WHERE o.open_time_ns <= ?
              AND k.open_known_at_ns <= ?
              AND c.trade_id IS NULL
            ORDER BY o.open_time_ns DESC, o.trade_id
            """,
            (
                _to_ns(moment),
                _to_ns(moment),
                _to_ns(moment),
                _to_ns(moment),
            ),
        ).fetchall()
        open_positions: list[dict[str, object]] = []
        marked_unrealized_pnl_jpy = 0
        mark_times: list[datetime] = []
        marks_complete = True
        for row in open_rows:
            position = self._open_row(row)
            mark = self.connection.execute(
                """
                SELECT *
                FROM simulated_position_marks
                WHERE trade_id = ? AND mark_time_ns <= ? AND observed_at_ns <= ?
                ORDER BY mark_time_ns DESC, mark_id DESC
                LIMIT 1
                """,
                (str(row["trade_id"]), _to_ns(moment), _to_ns(moment)),
            ).fetchone()
            if mark is None:
                marks_complete = False
                position["valuation_status"] = "unavailable"
            else:
                mark_time = _from_ns(int(mark["mark_time_ns"]))
                mark_times.append(mark_time)
                unrealized = int(mark["executable_unrealized_pnl_jpy"])
                marked_unrealized_pnl_jpy += unrealized
                position.update(
                    {
                        "valuation_status": "delayed_executable_mark",
                        "mark_time": mark_time.isoformat(),
                        "mark_observed_at": _from_ns(int(mark["observed_at_ns"])).isoformat(),
                        "mark_bid": float(mark["bid"]),
                        "mark_ask": float(mark["ask"]),
                        "mark_mid": float(mark["mid"]),
                        "mark_executable": float(mark["executable"]),
                        "mark_quote_to_jpy_rate": float(mark["quote_to_jpy_rate"]),
                        "mark_quote_conversion_source": str(mark["quote_conversion_source"]),
                        "gross_unrealized_pnl_jpy": int(mark["gross_unrealized_pnl_jpy"]),
                        "unrealized_pnl_jpy": unrealized,
                        "mark_spread_quote_cost_jpy": int(mark["spread_quote_cost_jpy"]),
                        "mark_quote": json.loads(str(mark["quote_json"])),
                    }
                )
            open_positions.append(position)
        trade_rows = self.connection.execute(
            """
            SELECT o.*, k.open_known_at_ns, k.known_at_basis,
                   ck.close_known_at_ns, ck.known_at_basis AS close_known_at_basis,
                   c.*
            FROM simulated_trade_opens o
            JOIN simulated_trade_open_observations k ON k.trade_id = o.trade_id
            LEFT JOIN simulated_trade_close_observations ck
              ON ck.trade_id = o.trade_id AND ck.close_known_at_ns <= ?
            LEFT JOIN simulated_trade_closes c
              ON c.trade_id = o.trade_id AND c.close_time_ns <= ?
             AND ck.trade_id IS NOT NULL
            WHERE o.open_time_ns <= ?
              AND k.open_known_at_ns <= ?
            ORDER BY o.open_time_ns DESC, o.trade_id DESC
            LIMIT ?
            """,
            (
                _to_ns(moment),
                _to_ns(moment),
                _to_ns(moment),
                _to_ns(moment),
                max(1, min(trade_limit, 500)),
            ),
        ).fetchall()
        open_positions_by_trade_id = {
            str(position["trade_id"]): position for position in open_positions
        }
        trades: list[dict[str, object]] = []
        for row in trade_rows:
            trade = self._trade_row(row)
            if trade["status"] == "open":
                marked_position = open_positions_by_trade_id.get(str(trade["trade_id"]))
                if marked_position is not None:
                    trade.update(marked_position)
            trades.append(trade)
        decision_rows = self.connection.execute(
            """
            SELECT d.*, h.allocation_class AS horizon_allocation_class,
                   h.capacity_eligible AS horizon_capacity_eligible,
                   h.policy_id AS horizon_policy_id,
                   h.policy_sha256 AS horizon_policy_sha256,
                   h.reason_text AS horizon_reason_text
            FROM decision_records d
            LEFT JOIN decision_horizon_assignments h ON h.decision_id = d.decision_id
            WHERE d.decision_time_ns <= ? AND d.observed_at_ns <= ?
            ORDER BY d.decision_time_ns DESC, d.decision_id DESC
            LIMIT 50
            """,
            (_to_ns(moment), _to_ns(moment)),
        ).fetchall()
        pending_rows = self.connection.execute(
            """
            SELECT i.*
            FROM decision_intakes i
            LEFT JOIN decision_records d
              ON d.decision_id = i.decision_id AND d.observed_at_ns <= ?
            WHERE i.decision_time_ns <= ? AND i.observed_at_ns <= ?
              AND d.decision_id IS NULL
            ORDER BY i.decision_time_ns DESC, i.decision_id DESC
            LIMIT 50
            """,
            (_to_ns(moment), _to_ns(moment), _to_ns(moment)),
        ).fetchall()
        pending_decision_view: list[dict[str, object]] = []
        for row in pending_rows:
            pending_decision = json.loads(str(row["decision_json"]))
            assignment = classify_virtual_horizon(pending_decision.get("timeframe"))
            pending_decision_view.append(
                {
                    "decision_id": str(row["decision_id"]),
                    "decision_time": _from_ns(int(row["decision_time_ns"])).isoformat(),
                    "observed_at": _from_ns(int(row["observed_at_ns"])).isoformat(),
                    "symbol": str(pending_decision.get("symbol") or ""),
                    "timeframe": str(pending_decision.get("timeframe") or ""),
                    "allocation_class": assignment["allocation_class"],
                    "capacity_eligible": assignment["capacity_eligible"],
                    "status": (
                        "waiting_for_historical_bid_ask"
                        if assignment["capacity_eligible"] is True
                        else "awaiting_terminal_observation_classification"
                    ),
                }
            )
        learning_count = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM simulated_trade_opens o
                JOIN simulated_trade_closes c ON c.trade_id = o.trade_id
                JOIN decision_horizon_assignments h ON h.decision_id = o.decision_id
                WHERE c.learning_eligible = 1 AND c.close_time_ns <= ?
                  AND h.allocation_class = 'intraday_capacity'
                """,
                (_to_ns(moment),),
            ).fetchone()[0]
        )
        closed_count = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM simulated_trade_closes c
                JOIN simulated_trade_close_observations ck ON ck.trade_id = c.trade_id
                WHERE c.close_time_ns <= ? AND ck.close_known_at_ns <= ?
                """,
                (_to_ns(moment), _to_ns(moment)),
            ).fetchone()[0]
        )
        wins = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM simulated_trade_closes c
                JOIN simulated_trade_close_observations ck ON ck.trade_id = c.trade_id
                WHERE c.net_pnl_jpy > 0 AND c.close_time_ns <= ?
                  AND ck.close_known_at_ns <= ?
                """,
                (_to_ns(moment), _to_ns(moment)),
            ).fetchone()[0]
        )
        daily_series: dict[str, int] = {}
        for row in self.connection.execute(
            """
            SELECT l.effective_time_ns, l.amount_jpy
            FROM ledger_events l
            JOIN simulated_trade_close_observations ck ON ck.trade_id = l.trade_id
            WHERE l.event_type = 'trade_closed' AND l.effective_time_ns <= ?
              AND ck.close_known_at_ns <= ?
            ORDER BY l.effective_time_ns
            """,
            (_to_ns(moment), _to_ns(moment)),
        ):
            key = _from_ns(int(row["effective_time_ns"])).astimezone(TOKYO).date().isoformat()
            daily_series[key] = daily_series.get(key, 0) + int(row["amount_jpy"])
        target_progress = today_pnl / policy.daily_target_jpy
        hard_drawdown_limit = policy.initial_capital_jpy * policy.hard_drawdown_bps // 10_000
        risk_state = self._session_close_gate_reason(
            moment, as_of=moment
        ) or self._risk_gate_reason(moment, policy, known_at=moment)
        curve = self._balance_curve(as_of=moment, known_at=moment)
        reconciliation = self.reconciliation(as_of=moment, known_at=moment)
        gross_notional_jpy = 0.0
        for position in open_positions:
            units = _positive_decimal(position["units"], "position units")
            price = _positive_decimal(
                position.get("mark_mid") or position["entry_mid"],
                "position valuation price",
            )
            conversion_rate = _positive_decimal(
                position.get("mark_quote_to_jpy_rate") or position["quote_to_jpy_rate"],
                "position valuation conversion rate",
            )
            gross_notional_jpy += float(units * price * conversion_rate)
        gross_leverage = gross_notional_jpy / balance if balance > 0 else None
        daily_loss_limit = policy.initial_capital_jpy * policy.daily_loss_limit_bps // 10_000
        weekly_loss_limit = policy.initial_capital_jpy * policy.weekly_loss_limit_bps // 10_000
        monthly_loss_limit = policy.initial_capital_jpy * policy.monthly_loss_limit_bps // 10_000
        cumulative_net_pnl = balance - policy.initial_capital_jpy
        equity = balance + marked_unrealized_pnl_jpy if not open_rows or marks_complete else None
        equity_drawdown = max(0, peak - equity) if equity is not None else None
        daily_loss_stop_active = today_pnl <= -daily_loss_limit
        daily_target_reference_met = today_pnl >= policy.daily_target_jpy
        daily_task_day_start = moment.astimezone(TOKYO).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        daily_profit_task = {
            "contract": "virtual-daily-profit-task-v1",
            "required": True,
            "session_date_jst": daily_task_day_start.date().isoformat(),
            "deadline_jst": (daily_task_day_start + timedelta(days=1)).isoformat(),
            "target_jpy": policy.daily_target_jpy,
            "realized_net_pnl_jpy": today_pnl,
            "remaining_jpy": max(0, policy.daily_target_jpy - today_pnl),
            "progress_ratio": target_progress,
            "status": ("completed" if daily_target_reference_met else "in_progress"),
            "completion_basis": "canonical_realized_net_pnl",
            "optimization_input": False,
            "training_label": False,
            "achievement_guaranteed": False,
            "safety_constraints_override_allowed": False,
        }
        latest_session = self.connection.execute(
            """
            SELECT * FROM session_events
            WHERE event_time_ns <= ?
            ORDER BY event_time_ns DESC, event_id DESC
            LIMIT 1
            """,
            (_to_ns(moment),),
        ).fetchone()
        pending_session_close = self.pending_session_close_request(as_of=moment)
        latest_close_request_row = self.connection.execute(
            """
            SELECT *
            FROM session_close_requests
            WHERE cutoff_time_ns <= ? AND requested_at_ns <= ?
            ORDER BY cutoff_time_ns DESC, request_id DESC
            LIMIT 1
            """,
            (_to_ns(moment), _to_ns(moment)),
        ).fetchone()
        latest_close_request = (
            pending_session_close
            if pending_session_close is not None
            else (
                self._session_close_request_row(latest_close_request_row)
                if latest_close_request_row is not None
                else None
            )
        )
        latest_close_completion = (
            self.session_close_completion(
                request_id=str(latest_close_request["request_id"]),
                as_of=moment,
            )
            if latest_close_request is not None
            else None
        )
        close_lifecycle_status = (
            "not_requested"
            if latest_close_request is None
            else ("completed" if latest_close_completion is not None else "pending")
        )
        post_completion_open_position_count = 0
        if latest_close_request is not None and latest_close_completion is not None:
            completion_time = _as_utc(
                datetime.fromisoformat(str(latest_close_completion["completed_at"])),
                "session close completion time",
            )
            pre_completion_open_positions = [
                position
                for position in open_positions
                if _as_utc(
                    datetime.fromisoformat(str(position["open_time"])),
                    "open position time",
                )
                <= completion_time
            ]
            post_completion_open_position_count = sum(
                1
                for position in open_positions
                if _as_utc(
                    datetime.fromisoformat(str(position["open_time"])),
                    "open position time",
                )
                > completion_time
            )
            if pre_completion_open_positions:
                close_lifecycle_status = "inconsistent_completed_with_open_positions"
                risk_state = "日次決済ライフサイクル不整合のためfail-closed"
        horizon_policy = VirtualHorizonAllocationPolicy()
        horizon_policy_payload = {
            **asdict(horizon_policy),
            "capacity_timeframes": list(horizon_policy.capacity_timeframes),
            "observation_timeframes": list(horizon_policy.observation_timeframes),
        }
        horizon_counts = {
            str(row["allocation_class"]): int(row["row_count"])
            for row in self.connection.execute(
                """
                SELECT h.allocation_class, COUNT(*) AS row_count
                FROM decision_horizon_assignments h
                JOIN decision_records d ON d.decision_id = h.decision_id
                WHERE d.observed_at_ns <= ?
                GROUP BY h.allocation_class
                """,
                (_to_ns(moment),),
            ).fetchall()
        }
        for allocation_class in (
            "intraday_capacity",
            "observation_only",
            "unsupported",
        ):
            horizon_counts.setdefault(allocation_class, 0)
        assigned_count = sum(horizon_counts.values())
        terminal_decision_count = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM decision_records
                WHERE observed_at_ns <= ?
                """,
                (_to_ns(moment),),
            ).fetchone()[0]
        )
        horizon_counts["legacy_unassigned"] = max(0, terminal_decision_count - assigned_count)
        intraday_open_count = sum(
            1
            for position in open_positions
            if str(position["timeframe"]).lower() in horizon_policy.capacity_timeframes
        )
        rolling_start_local = moment.astimezone(TOKYO).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=19)
        rolling_rows = self.connection.execute(
            """
            SELECT o.trade_id, c.net_pnl_jpy, c.realized_net_r
            FROM simulated_trade_opens o
            JOIN simulated_trade_closes c ON c.trade_id = o.trade_id
            JOIN simulated_trade_close_observations ck ON ck.trade_id = c.trade_id
            JOIN decision_horizon_assignments h ON h.decision_id = o.decision_id
            WHERE c.close_time_ns >= ? AND c.close_time_ns <= ?
              AND ck.close_known_at_ns <= ?
              AND h.allocation_class = 'intraday_capacity'
            ORDER BY c.close_time_ns, o.trade_id
            """,
            (
                _to_ns(rolling_start_local.astimezone(UTC)),
                _to_ns(moment),
                _to_ns(moment),
            ),
        ).fetchall()
        excluded_rolling_row = self.connection.execute(
            """
            SELECT COUNT(*) AS trade_count,
                   COALESCE(SUM(c.net_pnl_jpy), 0) AS net_pnl_jpy,
                   COALESCE(SUM(c.realized_net_r), 0.0) AS net_r
            FROM simulated_trade_opens o
            JOIN simulated_trade_closes c ON c.trade_id = o.trade_id
            JOIN simulated_trade_close_observations ck ON ck.trade_id = c.trade_id
            LEFT JOIN decision_horizon_assignments h ON h.decision_id = o.decision_id
            WHERE c.close_time_ns >= ? AND c.close_time_ns <= ?
              AND ck.close_known_at_ns <= ?
              AND (h.allocation_class IS NULL
                   OR h.allocation_class <> 'intraday_capacity')
            """,
            (
                _to_ns(rolling_start_local.astimezone(UTC)),
                _to_ns(moment),
                _to_ns(moment),
            ),
        ).fetchone()
        rolling_net_r = [float(row["realized_net_r"]) for row in rolling_rows]
        rolling_net_pnl = sum(int(row["net_pnl_jpy"]) for row in rolling_rows)
        rolling_expectancy = sum(rolling_net_r) / len(rolling_net_r) if rolling_net_r else None
        rolling_ci = _moving_block_mean_ci(
            rolling_net_r,
            seed_basis={
                "contract": "virtual-portfolio-research-kpis-v1",
                "trade_ids": [str(row["trade_id"]) for row in rolling_rows],
                "values": rolling_net_r,
            },
        )
        try:
            from fx_intel.virtual_portfolio_valuation import (
                evaluate_shadow_risk_gate_v2,
            )

            risk_gate_v2_shadow = evaluate_shadow_risk_gate_v2(
                self,
                as_of=moment,
                known_at=moment,
            )
        except Exception as error:
            risk_gate_v2_shadow = {
                "contract": "virtual-portfolio-risk-gate-v2-shadow",
                "enforcement_mode": "shadow_only",
                "can_open": False,
                "reasons": [f"risk_gate_evaluation_failed:{type(error).__name__}"],
                "risk_equity_jpy": None,
                "portfolio_heat_jpy": None,
                "next_trade_risk_jpy": None,
            }
        try:
            learning_payload = self.export_learning_rows(as_of=moment)
            raw_canonical_net_r = learning_payload.get("canonical_net_r")
            if not isinstance(raw_canonical_net_r, Mapping):
                raise InvalidVirtualRecord("canonical label summary is unavailable")
            canonical_net_r = dict(raw_canonical_net_r)
            row_count = learning_payload.get("row_count")
            if isinstance(row_count, bool) or not isinstance(row_count, int):
                raise InvalidVirtualRecord("canonical label row count is invalid")
            if row_count != learning_count:
                raise InvalidVirtualRecord(
                    "canonical label export count does not match mature learning count"
                )
        except Exception as error:
            learning_payload = {"rows": []}
            canonical_net_r = {
                "contract": "virtual-portfolio-canonical-net-r-summary-v1",
                "eligible_rows": learning_count,
                "net_label_samples": 0,
                "net_label_coverage": 0.0 if learning_count else None,
                "status": "invalid_or_unavailable",
                "reason": f"canonical_net_r_failed:{type(error).__name__}:{error}",
                "research_only": True,
                "promotion_eligible": False,
            }
        try:
            from fx_intel.multi_axis_learning import (
                summarize_multi_axis_readiness,
            )

            multi_axis_readiness = summarize_multi_axis_readiness(
                learning_payload,
                as_of=moment,
            )
        except Exception as error:
            multi_axis_readiness = {
                "contract": "virtual-portfolio-multi-axis-shadow-v1",
                "status": "unavailable",
                "reason": f"multi_axis_readiness_failed:{type(error).__name__}",
                "axes": {},
                "unavailable_axes": [
                    "market_regime",
                    "liquidity_proxy",
                    "macro_surprise",
                    "cross_asset_relationship",
                    "portfolio_risk",
                ],
                "research_only": True,
                "model_usable_for_decisions": False,
                "lockbox_access": "unavailable_no_fixed_lockbox_commitment",
            }
        return {
            "configured": True,
            "read_only": True,
            "execution_mode": "offline_simulation",
            "broker_connected": False,
            "portfolio_id": self._portfolio_config()["portfolio_id"],
            "currency": "JPY",
            "policy": asdict(policy),
            "policy_provenance": {
                "source": policy_state["source"],
                "update_id": policy_state["update_id"],
                "effective_at": policy_state["effective_at"],
                "policy_sha256": policy.policy_sha256,
                "append_only": True,
            },
            "horizon_allocation": {
                "contract": horizon_policy.policy_id,
                "policy": horizon_policy_payload,
                "policy_sha256": horizon_policy.policy_sha256,
                "counts": horizon_counts,
                "intraday_open_positions": intraday_open_count,
                "capacity_limit": policy.max_open_positions,
                "capacity_utilization": (intraday_open_count / policy.max_open_positions),
                "legacy_positions_outside_policy": sum(
                    1
                    for position in open_positions
                    if str(position["timeframe"]).lower() not in horizon_policy.capacity_timeframes
                ),
            },
            "generated_at": moment.isoformat(),
            "balance_jpy": balance,
            "cash_jpy": balance,
            "equity_jpy": equity,
            "unrealized_pnl_jpy": (
                marked_unrealized_pnl_jpy if not open_rows or marks_complete else None
            ),
            "equity_valuation_status": (
                "cash_equals_equity_no_open_positions"
                if not open_rows
                else (
                    "delayed_executable_marks_complete"
                    if marks_complete
                    else "unavailable_without_complete_executable_marks"
                )
            ),
            "equity_mark_time": (
                min(mark_times).isoformat() if open_rows and marks_complete and mark_times else None
            ),
            "initial_capital_jpy": policy.initial_capital_jpy,
            "cumulative_net_pnl_jpy": cumulative_net_pnl,
            "peak_balance_jpy": peak,
            "drawdown_jpy": peak - balance,
            "drawdown_pct": (peak - balance) / peak if peak > 0 else None,
            "equity_drawdown_jpy": equity_drawdown,
            "equity_drawdown_pct": (
                equity_drawdown / peak if equity_drawdown is not None and peak > 0 else None
            ),
            "hard_drawdown_limit_jpy": hard_drawdown_limit,
            "today_pnl_jpy": today_pnl,
            "weekly_pnl_jpy": weekly_pnl,
            "monthly_pnl_jpy": monthly_pnl,
            "daily_target_jpy": policy.daily_target_jpy,
            "remaining_daily_target_jpy": max(0, policy.daily_target_jpy - today_pnl),
            "target_progress": target_progress,
            "target_met": today_pnl >= policy.daily_target_jpy,
            "daily_profit_task": daily_profit_task,
            "risk_gate": {
                "can_open": risk_state is None,
                "reason": risk_state,
                "daily_loss_limit_jpy": daily_loss_limit,
                "weekly_loss_limit_jpy": weekly_loss_limit,
                "monthly_loss_limit_jpy": monthly_loss_limit,
                "remaining_daily_loss_budget_jpy": max(0, daily_loss_limit + today_pnl),
                "remaining_weekly_loss_budget_jpy": max(0, weekly_loss_limit + weekly_pnl),
                "remaining_monthly_loss_budget_jpy": max(0, monthly_loss_limit + monthly_pnl),
                "remaining_drawdown_budget_jpy": max(0, hard_drawdown_limit - (peak - balance)),
                "remaining_equity_drawdown_budget_jpy": (
                    max(0, hard_drawdown_limit - equity_drawdown)
                    if equity_drawdown is not None
                    else None
                ),
                "next_trade_risk_jpy": max(
                    0,
                    balance * policy.risk_per_trade_bps // 10_000,
                ),
            },
            "gross_notional_jpy": round(gross_notional_jpy),
            "gross_leverage": gross_leverage,
            "open_position_count": len(open_rows),
            "open_positions": open_positions,
            "closed_trade_count": closed_count,
            "win_rate": wins / closed_count if closed_count else None,
            "trades": trades,
            "recent_decisions": [self._decision_row(row) for row in decision_rows],
            "pending_decision_count": len(pending_rows),
            "pending_decisions": pending_decision_view,
            "daily_series": [
                {"session_date_jst": key, "net_pnl_jpy": value}
                for key, value in sorted(daily_series.items())
            ],
            "balance_curve": curve,
            "portfolio_v2": latest_v2_snapshot(
                self.connection,
                as_of=moment,
                known_at=moment,
            ),
            "risk_gate_v2_shadow": risk_gate_v2_shadow,
            "reconciliation": reconciliation,
            "data_quality_state": {
                "status": (
                    "session_close_lifecycle_error"
                    if close_lifecycle_status.startswith("inconsistent")
                    else (
                        "waiting_for_session_close_quote"
                        if pending_session_close is not None
                        else (
                            "waiting_for_delayed_quotes"
                            if pending_rows
                            else (
                                "waiting_for_position_marks"
                                if not marks_complete
                                else "eligible_inputs_consumed"
                            )
                        )
                    )
                ),
                "pending_decisions": len(pending_rows),
                "position_marks_complete": marks_complete,
                "position_marks_missing": sum(
                    1
                    for position in open_positions
                    if position.get("valuation_status") == "unavailable"
                ),
                "canonical_ledger_reconciled": reconciliation["passed"],
                "market_open": is_market_open(moment),
                "daily_stop_active": daily_loss_stop_active,
                "daily_loss_stop_active": daily_loss_stop_active,
                "daily_target_stop_active": False,
                "daily_target_reference_met": daily_target_reference_met,
                "daily_target_task_completed": daily_target_reference_met,
                "risk_block_active": risk_state is not None,
                "session_close_lifecycle_ok": not close_lifecycle_status.startswith("inconsistent"),
            },
            "learning": {
                "contract": VIRTUAL_LEARNING_CONTRACT,
                "eligible_rows": learning_count,
                "canonical_net_r": canonical_net_r,
                "minimum_rows": policy.minimum_learning_rows,
                "ready_for_validation": learning_count >= policy.minimum_learning_rows,
                "fast_loop": "safety_only",
                "fast_loop_policy": asdict(VirtualSafetyLearningPolicy()),
                "fast_loop_policy_sha256": VirtualSafetyLearningPolicy().policy_sha256,
                "slow_loop": "weekly_offline_challenger",
                "multi_axis": multi_axis_readiness,
                "promotion_eligible": False,
                "daily_target_is_training_label": False,
            },
            "latest_session": (
                {
                    "session_id": str(latest_session["session_id"]),
                    "session_date_jst": str(latest_session["session_date_jst"]),
                    "event_type": str(latest_session["event_type"]),
                    "event_time": _from_ns(int(latest_session["event_time_ns"])).isoformat(),
                    "detail": json.loads(str(latest_session["detail_json"])),
                }
                if latest_session is not None
                else None
            ),
            "pending_session_close_request": pending_session_close,
            "session_close_lifecycle": {
                "status": close_lifecycle_status,
                "request": latest_close_request,
                "completion": latest_close_completion,
                "sealed_through": (
                    latest_close_completion["completed_at"]
                    if latest_close_completion is not None
                    else None
                ),
                "post_completion_open_position_count": (post_completion_open_position_count),
                "authoritative_key": "request_id",
            },
            "research_kpis": {
                "contract": "virtual-portfolio-research-kpis-v1",
                "primary": "rolling_20d_cost_adjusted_expectancy_net_r",
                "rolling_20d": {
                    "calendar_timezone": "Asia/Tokyo",
                    "start": rolling_start_local.astimezone(UTC).isoformat(),
                    "end": moment.isoformat(),
                    "trade_count": len(rolling_rows),
                    "net_pnl_jpy": rolling_net_pnl,
                    "net_r": sum(rolling_net_r),
                    "cost_adjusted_expectancy_net_r": rolling_expectancy,
                    "expectancy_95pct_ci": rolling_ci,
                    "scope": "intraday_capacity_only",
                    "excluded_from_primary": {
                        "trade_count": int(excluded_rolling_row["trade_count"]),
                        "net_pnl_jpy": int(excluded_rolling_row["net_pnl_jpy"]),
                        "net_r": float(excluded_rolling_row["net_r"]),
                        "reason": "legacy_unassigned_or_non_intraday",
                    },
                },
                "drawdown_jpy": peak - balance,
                "mature_learning_rows": learning_count,
                "minimum_learning_rows": policy.minimum_learning_rows,
                "capacity_utilization": (intraday_open_count / policy.max_open_positions),
                "daily_profit_reference": {
                    "value_jpy": policy.daily_target_jpy,
                    "status": "required_operational_task",
                    "required": True,
                    "optimization_input": False,
                    "training_label": False,
                    "achievement_guaranteed": False,
                },
                "daily_profit_task": daily_profit_task,
                "claim_scope": "descriptive_only_evaluation_unavailable",
            },
            "performance_claim_status": "evaluation_unavailable",
            "performance_claim_reason": (
                "1日7万円は必須タスクとして進捗追跡しますが、将来利益や達成確率を"
                "保証する検証証拠ではありません"
            ),
        }

    def activate_five_x_limits(
        self,
        *,
        changed_at: datetime | None = None,
        reason: str = "operator-approved five-times portfolio limits",
    ) -> dict[str, object]:
        """Append the approved five-times limit transition without rewriting history."""

        writer_id = self._require_writer()
        changed = _as_utc(changed_at or datetime.now(UTC), "policy changed_at")
        config = self._portfolio_config()
        portfolio_id = str(config["portfolio_id"])
        requested = five_x_virtual_portfolio_policy()
        requested.validate()
        if requested.initial_capital_jpy != int(config["initial_capital_jpy"]):
            raise InvalidVirtualRecord("policy update cannot change initial capital")
        current_state = self._policy_state(as_of=changed, known_at=changed)
        current = current_state["policy"]
        assert isinstance(current, VirtualPortfolioPolicy)
        if current.policy_sha256 == requested.policy_sha256:
            return {
                "inserted": False,
                "portfolio_id": portfolio_id,
                "policy_id": requested.policy_id,
                "policy_sha256": requested.policy_sha256,
                "effective_at": current_state["effective_at"],
            }
        reason_text = _required_text(reason, "policy update reason")
        latest_time_ns = int(
            self.connection.execute(
                """
                SELECT COALESCE(MAX(effective_time_ns), ?) FROM portfolio_policy_updates
                WHERE portfolio_id = ?
                """,
                (int(config["created_at_ns"]), portfolio_id),
            ).fetchone()[0]
        )
        changed_ns = _to_ns(changed)
        if changed_ns <= latest_time_ns:
            raise InvalidVirtualRecord("policy change time must follow the previous policy")
        payload = {
            "portfolio_id": portfolio_id,
            "effective_time": changed.isoformat(),
            "known_at": changed.isoformat(),
            "policy": asdict(requested),
            "previous_policy_sha256": current.policy_sha256,
            "reason": reason_text,
        }
        update_id = _stable_id("portfolio-policy-update", payload)
        record_payload = {"update_id": update_id, **payload}
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO portfolio_policy_updates(
                    update_id, portfolio_id, effective_time_ns, known_at_ns,
                    policy_id, policy_sha256, policy_json,
                    previous_policy_sha256, reason_text, record_sha256, writer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    update_id,
                    portfolio_id,
                    changed_ns,
                    changed_ns,
                    requested.policy_id,
                    requested.policy_sha256,
                    _json(asdict(requested)),
                    current.policy_sha256,
                    reason_text,
                    _record_hash(record_payload),
                    writer_id,
                ),
            )
            _verify_policy_updates(self.connection)
        return {
            "inserted": True,
            "update_id": update_id,
            "portfolio_id": portfolio_id,
            "previous_policy_sha256": current.policy_sha256,
            "policy_id": requested.policy_id,
            "policy_sha256": requested.policy_sha256,
            "effective_at": changed.isoformat(),
        }

    def _policy_state(
        self,
        *,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> dict[str, object]:
        config = self._portfolio_config()
        effective_cutoff = _to_ns(as_of) if as_of is not None else 2**63 - 1
        knowledge_cutoff = _to_ns(known_at) if known_at is not None else 2**63 - 1
        update = self.connection.execute(
            """
            SELECT * FROM portfolio_policy_updates
            WHERE portfolio_id = ? AND effective_time_ns <= ? AND known_at_ns <= ?
            ORDER BY effective_time_ns DESC, known_at_ns DESC, update_id DESC
            LIMIT 1
            """,
            (str(config["portfolio_id"]), effective_cutoff, knowledge_cutoff),
        ).fetchone()
        row = update if update is not None else config
        raw = json.loads(str(row["policy_json"]))
        policy = VirtualPortfolioPolicy(**raw)
        policy.validate()
        if policy.policy_sha256 != str(row["policy_sha256"]):
            raise VirtualPortfolioError("stored policy hash does not reconcile")
        if update is None:
            effective_at = _from_ns(int(config["created_at_ns"])).isoformat()
            source = "portfolio_config"
            update_id = None
        else:
            effective_at = _from_ns(int(update["effective_time_ns"])).isoformat()
            source = "portfolio_policy_updates"
            update_id = str(update["update_id"])
        return {
            "policy": policy,
            "effective_at": effective_at,
            "source": source,
            "update_id": update_id,
        }

    def policy(
        self,
        *,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> VirtualPortfolioPolicy:
        state = self._policy_state(as_of=as_of, known_at=known_at)
        policy = state["policy"]
        assert isinstance(policy, VirtualPortfolioPolicy)
        return policy

    def cash_balance_jpy(
        self,
        *,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> int:
        effective_cutoff = _to_ns(as_of) if as_of is not None else 2**63 - 1
        knowledge_cutoff = _to_ns(known_at) if known_at is not None else 2**63 - 1
        row = self.connection.execute(
            """
            SELECT COALESCE(SUM(l.amount_jpy), 0) AS balance
            FROM ledger_events l
            LEFT JOIN simulated_trade_close_observations k ON k.trade_id = l.trade_id
            WHERE l.effective_time_ns <= ?
              AND (l.event_type <> 'trade_closed' OR l.trade_id IS NULL
                   OR k.close_known_at_ns <= ?)
            """,
            (effective_cutoff, knowledge_cutoff),
        ).fetchone()
        return int(row["balance"])

    def prediction_portfolio_risk_snapshot(
        self,
        *,
        as_of: datetime,
    ) -> dict[str, object]:
        """Freeze only portfolio state already knowable at a decision timestamp.

        This is a model-input snapshot, not a risk-gate override.  It deliberately
        uses the same timestamp for event and knowledge cutoffs so a delayed
        close/open cannot be backfilled into an earlier prediction.
        """

        moment = _as_utc(as_of, "portfolio risk snapshot as_of")
        policy = self.policy(as_of=moment, known_at=moment)
        cutoff = _to_ns(moment)
        open_row = self.connection.execute(
            """
            SELECT COUNT(*) AS position_count,
                   COALESCE(SUM(o.planned_risk_jpy), 0) AS planned_risk_jpy
            FROM simulated_trade_opens o
            JOIN simulated_trade_open_observations ok ON ok.trade_id = o.trade_id
            LEFT JOIN simulated_trade_close_observations ck
              ON ck.trade_id = o.trade_id AND ck.close_known_at_ns <= ?
            LEFT JOIN simulated_trade_closes c
              ON c.trade_id = o.trade_id AND c.close_time_ns <= ?
             AND ck.trade_id IS NOT NULL
            WHERE o.open_time_ns <= ? AND ok.open_known_at_ns <= ?
              AND c.trade_id IS NULL
            """,
            (cutoff, cutoff, cutoff, cutoff),
        ).fetchone()
        cash = self.cash_balance_jpy(as_of=moment, known_at=moment)
        peak = self.peak_balance_jpy(as_of=moment, known_at=moment)
        drawdown = max(0, peak - cash)
        period_pnl = {
            period: self._period_pnl(moment, period, known_at=moment)
            for period in ("day", "week", "month")
        }
        loss_limits = {
            "day": policy.initial_capital_jpy * policy.daily_loss_limit_bps // 10_000,
            "week": policy.initial_capital_jpy * policy.weekly_loss_limit_bps // 10_000,
            "month": policy.initial_capital_jpy * policy.monthly_loss_limit_bps // 10_000,
        }
        ledger_source_rows = self.connection.execute(
            """
            SELECT l.event_id, l.record_sha256
            FROM ledger_events l
            LEFT JOIN simulated_trade_close_observations ck ON ck.trade_id = l.trade_id
            WHERE l.effective_time_ns <= ?
              AND (l.event_type <> 'trade_closed' OR l.trade_id IS NULL
                   OR ck.close_known_at_ns <= ?)
            ORDER BY l.effective_time_ns, l.event_id
            """,
            (cutoff, cutoff),
        ).fetchall()
        open_source_rows = self.connection.execute(
            """
            SELECT o.trade_id, o.record_sha256 AS open_record_sha256,
                   ok.record_sha256 AS observation_record_sha256
            FROM simulated_trade_opens o
            JOIN simulated_trade_open_observations ok ON ok.trade_id = o.trade_id
            WHERE o.open_time_ns <= ? AND ok.open_known_at_ns <= ?
            ORDER BY o.open_time_ns, o.trade_id
            """,
            (cutoff, cutoff),
        ).fetchall()
        planned_risk = int(open_row["planned_risk_jpy"])
        source_record_ids = [
            f"portfolio-policy:{policy.policy_sha256}",
            *(f"ledger:{row['event_id']}" for row in ledger_source_rows),
            *(f"open:{row['trade_id']}" for row in open_source_rows),
        ]
        source_record_sha256 = [
            policy.policy_sha256,
            *(str(row["record_sha256"]) for row in ledger_source_rows),
            *(
                str(value)
                for row in open_source_rows
                for value in (
                    row["open_record_sha256"],
                    row["observation_record_sha256"],
                )
            ),
        ]
        payload: dict[str, object] = {
            "contract": "virtual-portfolio-risk-snapshot-v1",
            "as_of": moment.isoformat(),
            "knowledge_as_of": moment.isoformat(),
            "currency": "JPY",
            "initial_capital_jpy": policy.initial_capital_jpy,
            "cash_balance_jpy": cash,
            "peak_cash_balance_jpy": peak,
            "cash_drawdown_jpy": drawdown,
            "cash_drawdown_ratio": drawdown / peak if peak > 0 else None,
            "open_position_count": int(open_row["position_count"]),
            "open_planned_risk_jpy": planned_risk,
            "open_planned_risk_ratio": (
                planned_risk / policy.initial_capital_jpy
                if policy.initial_capital_jpy > 0
                else None
            ),
            "period_pnl_jpy": period_pnl,
            "remaining_loss_budget_jpy": {
                period: max(0, limit + int(period_pnl[period]))
                for period, limit in loss_limits.items()
            },
            "hard_drawdown_limit_jpy": (
                policy.initial_capital_jpy * policy.hard_drawdown_bps // 10_000
            ),
            "risk_gate_reason": self._risk_gate_reason(
                moment,
                policy,
                known_at=moment,
            ),
            "policy_sha256": policy.policy_sha256,
            "source_record_ids": source_record_ids,
            "source_record_sha256": source_record_sha256,
            "liquidation_heat_status": "unavailable_in_prediction_snapshot",
            "research_only": True,
        }
        payload["snapshot_sha256"] = _sha256_json(payload)
        return payload

    def reconciliation(
        self,
        *,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> dict[str, object]:
        """Reconcile canonical ledger, closes, and the latest derived balance."""

        moment = _as_utc(as_of or datetime.now(UTC), "reconciliation as_of")
        cutoff = _to_ns(moment)
        initial = int(self._portfolio_config()["initial_capital_jpy"])
        close_row = self.connection.execute(
            """
            SELECT COALESCE(SUM(c.net_pnl_jpy), 0) AS total
            FROM simulated_trade_closes c
            JOIN simulated_trade_close_observations k ON k.trade_id = c.trade_id
            WHERE c.close_time_ns <= ? AND k.close_known_at_ns <= ?
            """,
            (cutoff, _to_ns(known_at or moment)),
        ).fetchone()
        ledger_row = self.connection.execute(
            """
            SELECT COALESCE(SUM(l.amount_jpy), 0) AS total
            FROM ledger_events l
            LEFT JOIN simulated_trade_close_observations k ON k.trade_id = l.trade_id
            WHERE l.event_type = 'trade_closed' AND l.effective_time_ns <= ?
              AND (l.trade_id IS NULL OR k.close_known_at_ns <= ?)
            """,
            (cutoff, _to_ns(known_at or moment)),
        ).fetchone()
        close_total = int(close_row["total"])
        ledger_trade_total = int(ledger_row["total"])
        expected_balance = initial + close_total
        actual_balance = self.cash_balance_jpy(
            as_of=moment,
            known_at=known_at or moment,
        )
        passed = close_total == ledger_trade_total and expected_balance == actual_balance
        return {
            "contract": "virtual-portfolio-ledger-reconciliation-v1",
            "as_of": moment.isoformat(),
            "initial_capital_jpy": initial,
            "canonical_close_net_pnl_jpy": close_total,
            "ledger_trade_net_pnl_jpy": ledger_trade_total,
            "expected_balance_jpy": expected_balance,
            "actual_balance_jpy": actual_balance,
            "difference_jpy": actual_balance - expected_balance,
            "passed": passed,
        }

    def _balance_curve(
        self,
        *,
        as_of: datetime,
        known_at: datetime,
    ) -> list[dict[str, object]]:
        """Rebuild balance and drawdown in event time from the canonical ledger."""

        rows = self.connection.execute(
            """
            SELECT event_id, event_type, effective_time_ns, amount_jpy
            FROM ledger_events l
            LEFT JOIN simulated_trade_close_observations k ON k.trade_id = l.trade_id
            WHERE l.effective_time_ns <= ?
              AND (l.event_type <> 'trade_closed' OR l.trade_id IS NULL
                   OR k.close_known_at_ns <= ?)
            ORDER BY l.effective_time_ns, l.event_id
            """,
            (_to_ns(as_of), _to_ns(known_at)),
        ).fetchall()
        balance = 0
        peak = 0
        result: list[dict[str, object]] = []
        for row in rows:
            balance += int(row["amount_jpy"])
            peak = max(peak, balance)
            result.append(
                {
                    "event_id": str(row["event_id"]),
                    "event_type": str(row["event_type"]),
                    "time": _from_ns(int(row["effective_time_ns"])).isoformat(),
                    "balance_jpy": balance,
                    "peak_balance_jpy": peak,
                    "drawdown_jpy": peak - balance,
                }
            )
        return result

    def peak_balance_jpy(
        self,
        *,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> int:
        """Recompute the event-time peak so delayed backfills cannot stale marks."""

        config = self._portfolio_config()
        initial = int(config["initial_capital_jpy"])
        created_ns = int(config["created_at_ns"])
        cutoff = _to_ns(as_of) if as_of is not None else 2**63 - 1
        balance = initial if created_ns <= cutoff else 0
        peak = balance
        for row in self.connection.execute(
            """
            SELECT l.amount_jpy
            FROM ledger_events l
            LEFT JOIN simulated_trade_close_observations k ON k.trade_id = l.trade_id
            WHERE l.event_type = 'trade_closed' AND l.effective_time_ns <= ?
              AND (l.trade_id IS NULL OR k.close_known_at_ns <= ?)
            ORDER BY l.effective_time_ns, l.event_id
            """,
            (
                cutoff,
                _to_ns(known_at) if known_at is not None else 2**63 - 1,
            ),
        ):
            balance += int(row["amount_jpy"])
            peak = max(peak, balance)
        return peak

    def _portfolio_config(self) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM portfolio_config ORDER BY created_at_ns LIMIT 1"
        ).fetchone()
        if row is None:
            raise VirtualPortfolioError("virtual portfolio is not initialized")
        return row

    def _conversion_contract(
        self,
        symbol: str,
        rate: float | None,
        source: str | None,
    ) -> tuple[Decimal, str]:
        if len(symbol) != 6 or not symbol.isalpha():
            raise InvalidVirtualRecord("symbol must be a six-letter FX pair")
        if symbol[3:] == "JPY":
            if rate is not None and _positive_decimal(rate, "quote_to_jpy_rate") != 1:
                raise InvalidVirtualRecord("JPY quote pair conversion rate must equal 1")
            return Decimal("1"), "JPY quote currency"
        if rate is None:
            raise InvalidVirtualRecord("quote-to-JPY conversion rate is missing")
        return (
            _positive_decimal(rate, "quote_to_jpy_rate"),
            _required_text(source, "quote conversion source"),
        )

    def _prepare_open(
        self,
        *,
        decision: Mapping[str, object],
        quote: QuoteSnapshot,
        conversion_rate: Decimal,
        conversion_source: str,
        stop: Decimal,
        target: Decimal,
        observed: datetime,
        policy: VirtualPortfolioPolicy,
    ) -> dict[str, object]:
        direction = str(decision["direction"])
        bid = _positive_decimal(quote.bid, "entry bid")
        ask = _positive_decimal(quote.ask, "entry ask")
        mid = (bid + ask) / Decimal("2")
        executable = ask if direction == "long" else bid
        if direction == "long" and not stop < executable < target:
            raise InvalidVirtualRecord("long stop/entry/target ordering is invalid")
        if direction == "short" and not target < executable < stop:
            raise InvalidVirtualRecord("short target/entry/stop ordering is invalid")
        risk_distance = abs(executable - stop)
        current_balance = self.cash_balance_jpy(as_of=observed)
        maximum_risk = current_balance * policy.risk_per_trade_bps // 10_000
        if maximum_risk <= 0:
            raise InvalidVirtualRecord("risk budget is exhausted")
        units_decimal = (Decimal(maximum_risk) / (risk_distance * conversion_rate)).quantize(
            Decimal("1"), rounding=ROUND_FLOOR
        )
        if units_decimal <= 0:
            raise InvalidVirtualRecord("calculated units are zero")
        planned_risk = _round_jpy(units_decimal * risk_distance * conversion_rate)
        decision_id = str(decision["decision_id"])
        trade_identity: dict[str, object] = {
            "decision_id": decision_id,
            "open_time": observed.isoformat(),
            "symbol": str(decision["symbol"]).upper(),
            "direction": direction,
        }
        trade_id = _stable_id("sim", trade_identity)
        quote_payload = _quote_payload(quote)
        rationale = {
            "reason": str(decision.get("reason") or decision.get("reason_text") or ""),
            "conviction": decision.get("conviction"),
            "why_now": decision.get("why_now"),
            "counterfactual": decision.get("counterfactual"),
        }
        values: dict[str, object] = {
            "trade_id": trade_id,
            "decision_id": decision_id,
            "open_time_ns": _to_ns(observed),
            "symbol": str(decision["symbol"]).upper(),
            "timeframe": str(decision.get("timeframe") or "unknown"),
            "direction": direction,
            "units": float(units_decimal),
            "entry_bid": float(bid),
            "entry_ask": float(ask),
            "entry_mid": float(mid),
            "entry_executable": float(executable),
            "stop_price": float(stop),
            "target_price": float(target),
            "quote_to_jpy_rate": float(conversion_rate),
            "quote_conversion_source": conversion_source,
            "planned_risk_jpy": planned_risk,
            "quote_json": _json(quote_payload),
            "rationale_json": _json(rationale),
            "features_json": _json(decision.get("features") or {}),
            "components_json": _json(decision.get("components") or {}),
            "warnings_json": _json(decision.get("warnings") or []),
            "data_sha256": str(decision["data_sha256"]),
            "model_sha256": str(decision["model_sha256"]),
            "policy_sha256": str(decision["policy_sha256"]),
        }
        values["record_sha256"] = _record_hash(values)
        return values

    def _session_close_gate_reason(
        self,
        event_time: datetime,
        *,
        as_of: datetime,
    ) -> str | None:
        """Prevent any capacity-eligible decision from reopening a closed session."""

        event = _as_utc(event_time, "session close gate event_time")
        processing_as_of = _as_utc(as_of, "session close gate as_of")
        session_date = event.astimezone(TOKYO).date()
        row = self.connection.execute(
            """
            SELECT r.request_id, r.cutoff_time_ns, c.completion_id,
                   c.completed_at_ns
            FROM session_close_requests r
            LEFT JOIN session_close_completions c ON c.request_id = r.request_id
            WHERE r.session_id = ? AND r.requested_at_ns <= ?
            """,
            (f"jst-{session_date.isoformat()}", _to_ns(processing_as_of)),
        ).fetchone()
        if row is None:
            return None
        if row["completion_id"] is not None and int(row["completed_at_ns"]) <= _to_ns(
            processing_as_of
        ):
            if _to_ns(event) <= int(row["completed_at_ns"]):
                return "日次決済完了済みsessionのため新規仮想建てを停止"
            return None
        if int(row["cutoff_time_ns"]) > _to_ns(event):
            return None
        return "日次決済締切後のため新規仮想建てを停止"

    def _risk_gate_reason(
        self,
        now: datetime,
        policy: VirtualPortfolioPolicy,
        *,
        known_at: datetime,
    ) -> str | None:
        open_count = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM simulated_trade_opens o
                JOIN simulated_trade_open_observations k ON k.trade_id = o.trade_id
                LEFT JOIN simulated_trade_close_observations ck
                  ON ck.trade_id = o.trade_id AND ck.close_known_at_ns <= ?
                LEFT JOIN simulated_trade_closes c
                  ON c.trade_id = o.trade_id AND c.close_time_ns <= ?
                 AND ck.trade_id IS NOT NULL
                WHERE o.open_time_ns <= ?
                  AND k.open_known_at_ns <= ?
                  AND c.trade_id IS NULL
                """,
                (
                    _to_ns(known_at),
                    _to_ns(now),
                    _to_ns(now),
                    _to_ns(known_at),
                ),
            ).fetchone()[0]
        )
        if open_count >= policy.max_open_positions:
            return f"同時保有上限{policy.max_open_positions}件に到達"
        balance = self.cash_balance_jpy(as_of=now, known_at=known_at)
        peak = self.peak_balance_jpy(as_of=now, known_at=known_at)
        hard_limit = policy.initial_capital_jpy * policy.hard_drawdown_bps // 10_000
        if peak - balance >= hard_limit:
            return "ハードドローダウン上限に到達"
        periods = (
            ("day", policy.daily_loss_limit_bps, "日次"),
            ("week", policy.weekly_loss_limit_bps, "週次"),
            ("month", policy.monthly_loss_limit_bps, "月次"),
        )
        for period, bps, label in periods:
            limit = policy.initial_capital_jpy * bps // 10_000
            if self._period_pnl(now, period, known_at=known_at) <= -limit:
                return f"{label}損失上限に到達"
        return None

    def _safety_learning_gate_reason(
        self,
        decision: Mapping[str, object],
        *,
        as_of: datetime,
        known_at: datetime,
    ) -> str | None:
        """Apply only a loss cooldown learned from already-mature outcomes."""

        safety = VirtualSafetyLearningPolicy()
        symbol = str(decision.get("symbol") or "").upper()
        timeframe = str(decision.get("timeframe") or "")
        direction = str(decision.get("direction") or "").lower()
        if not symbol or not timeframe or direction not in {"long", "short"}:
            return None
        rows = self.connection.execute(
            """
            SELECT c.net_pnl_jpy, c.realized_net_r
            FROM simulated_trade_opens o
            JOIN simulated_trade_closes c ON c.trade_id = o.trade_id
            JOIN simulated_trade_close_observations ck ON ck.trade_id = c.trade_id
            WHERE o.symbol = ? AND o.timeframe = ? AND o.direction = ?
              AND c.learning_eligible = 1 AND c.close_time_ns <= ?
              AND ck.close_known_at_ns <= ?
            ORDER BY c.close_time_ns DESC, o.trade_id DESC
            LIMIT ?
            """,
            (
                symbol,
                timeframe,
                direction,
                _to_ns(as_of),
                _to_ns(known_at),
                safety.rolling_window,
            ),
        ).fetchall()
        if len(rows) >= safety.consecutive_loss_limit and all(
            int(row["net_pnl_jpy"]) < 0 for row in rows[: safety.consecutive_loss_limit]
        ):
            return (
                "学習安全ゲート:同一symbol/timeframe/directionで"
                f"{safety.consecutive_loss_limit}連続純損失のため新規仮想建てを停止"
            )
        if len(rows) >= safety.rolling_window:
            mean_net_r = sum(float(row["realized_net_r"]) for row in rows) / len(rows)
            if mean_net_r <= safety.minimum_window_mean_net_r:
                return (
                    "学習安全ゲート:同一区分の直近"
                    f"{safety.rolling_window}件平均が{mean_net_r:.2f}Rのため停止"
                )
        return None

    def _period_pnl(
        self,
        now: datetime,
        period: str,
        *,
        known_at: datetime,
    ) -> int:
        row = self.connection.execute(
            """
            SELECT COALESCE(SUM(l.amount_jpy), 0) AS pnl
            FROM ledger_events l
            JOIN simulated_trade_close_observations k ON k.trade_id = l.trade_id
            WHERE l.event_type = 'trade_closed' AND l.effective_time_ns >= ?
              AND l.effective_time_ns <= ? AND k.close_known_at_ns <= ?
            """,
            (
                _to_ns(_period_start(now, period)),
                _to_ns(now),
                _to_ns(known_at),
            ),
        ).fetchone()
        return int(row["pnl"])

    def _open_row(self, row: sqlite3.Row) -> dict[str, object]:
        notional_jpy = (
            float(row["units"]) * float(row["entry_mid"]) * float(row["quote_to_jpy_rate"])
        )
        result: dict[str, object] = {
            "trade_id": str(row["trade_id"]),
            "decision_id": str(row["decision_id"]),
            "open_time": _from_ns(int(row["open_time_ns"])).isoformat(),
            "symbol": str(row["symbol"]),
            "timeframe": str(row["timeframe"]),
            "direction": str(row["direction"]),
            "units": float(row["units"]),
            "entry_bid": float(row["entry_bid"]),
            "entry_ask": float(row["entry_ask"]),
            "entry_mid": float(row["entry_mid"]),
            "entry_executable": float(row["entry_executable"]),
            "stop_price": float(row["stop_price"]),
            "target_price": float(row["target_price"]),
            "planned_risk_jpy": int(row["planned_risk_jpy"]),
            "notional_jpy": round(notional_jpy),
            "quote_to_jpy_rate": float(row["quote_to_jpy_rate"]),
            "quote_conversion_source": str(row["quote_conversion_source"]),
            "rationale": json.loads(str(row["rationale_json"])),
            "features": json.loads(str(row["features_json"])),
            "components": json.loads(str(row["components_json"])),
            "warnings": json.loads(str(row["warnings_json"])),
            "entry_quote": json.loads(str(row["quote_json"])),
            "data_sha256": str(row["data_sha256"]),
            "model_sha256": str(row["model_sha256"]),
            "policy_sha256": str(row["policy_sha256"]),
        }
        if "open_known_at_ns" in row.keys():
            result["open_known_at"] = _from_ns(int(row["open_known_at_ns"])).isoformat()
            result["open_known_at_basis"] = str(row["known_at_basis"])
        return result

    def open_trade_details(self) -> list[dict[str, object]]:
        """Return complete research-only state needed by the settlement replay."""

        rows = self.connection.execute("""
            SELECT o.*, k.open_known_at_ns, k.known_at_basis,
                   d.decision_time_ns, d.decision_json
            FROM simulated_trade_opens o
            JOIN simulated_trade_open_observations k ON k.trade_id = o.trade_id
            JOIN decision_records d ON d.decision_id = o.decision_id
            LEFT JOIN simulated_trade_closes c ON c.trade_id = o.trade_id
            WHERE c.trade_id IS NULL
            ORDER BY o.open_time_ns, o.trade_id
            """).fetchall()
        return [
            {
                **self._open_row(row),
                "decision_time": _from_ns(int(row["decision_time_ns"])).isoformat(),
                "entry_bid": float(row["entry_bid"]),
                "entry_ask": float(row["entry_ask"]),
                "entry_mid": float(row["entry_mid"]),
                "quote_to_jpy_rate": float(row["quote_to_jpy_rate"]),
                "quote_conversion_source": str(row["quote_conversion_source"]),
                "decision": json.loads(str(row["decision_json"])),
                "entry_quote": json.loads(str(row["quote_json"])),
            }
            for row in rows
        ]

    def _trade_row(self, row: sqlite3.Row) -> dict[str, object]:
        result = self._open_row(row)
        if row["close_time_ns"] is None:
            result["status"] = "open"
            return result
        result.update(
            {
                "status": "closed",
                "close_time": _from_ns(int(row["close_time_ns"])).isoformat(),
                "close_known_at": _from_ns(int(row["close_known_at_ns"])).isoformat(),
                "close_known_at_basis": str(row["close_known_at_basis"]),
                "close_reason": str(row["close_reason"]),
                "exit_bid": float(row["exit_bid"]),
                "exit_ask": float(row["exit_ask"]),
                "exit_mid": float(row["exit_mid"]),
                "exit_executable": float(row["exit_executable"]),
                "gross_market_pnl_jpy": int(row["gross_market_pnl_jpy"]),
                "spread_quote_cost_jpy": int(row["spread_quote_cost_jpy"]),
                "slippage_cost_jpy": int(row["slippage_cost_jpy"]),
                "commission_jpy": int(row["commission_jpy"]),
                "financing_jpy": int(row["financing_jpy"]),
                "conversion_cost_jpy": int(row["conversion_cost_jpy"]),
                "net_pnl_jpy": int(row["net_pnl_jpy"]),
                "realized_net_r": float(row["realized_net_r"]),
                "maximum_favorable_excursion_r": (
                    float(row["maximum_favorable_excursion_r"])
                    if row["maximum_favorable_excursion_r"] is not None
                    else None
                ),
                "maximum_adverse_excursion_r": (
                    float(row["maximum_adverse_excursion_r"])
                    if row["maximum_adverse_excursion_r"] is not None
                    else None
                ),
                "attribution": json.loads(str(row["attribution_json"])),
                "failures": json.loads(str(row["failure_json"])),
            }
        )
        return result

    def _decision_row(self, row: sqlite3.Row) -> dict[str, object]:
        keys = set(row.keys())
        allocation_class = (
            str(row["horizon_allocation_class"])
            if "horizon_allocation_class" in keys and row["horizon_allocation_class"] is not None
            else "legacy_unassigned"
        )
        return {
            "decision_id": str(row["decision_id"]),
            "decision_time": _from_ns(int(row["decision_time_ns"])).isoformat(),
            "symbol": str(row["symbol"]),
            "timeframe": str(row["timeframe"]),
            "direction": str(row["direction"]),
            "disposition": str(row["disposition"]),
            "pit_eligible": bool(row["pit_eligible"]),
            "reason": str(row["reason_text"]),
            "rejection_reasons": json.loads(str(row["rejection_reasons_json"])),
            "horizon_allocation": {
                "allocation_class": allocation_class,
                "capacity_eligible": (
                    bool(row["horizon_capacity_eligible"])
                    if "horizon_capacity_eligible" in keys
                    and row["horizon_capacity_eligible"] is not None
                    else False
                ),
                "policy_id": (
                    str(row["horizon_policy_id"])
                    if "horizon_policy_id" in keys and row["horizon_policy_id"] is not None
                    else None
                ),
                "policy_sha256": (
                    str(row["horizon_policy_sha256"])
                    if "horizon_policy_sha256" in keys and row["horizon_policy_sha256"] is not None
                    else None
                ),
                "reason": (
                    str(row["horizon_reason_text"])
                    if "horizon_reason_text" in keys and row["horizon_reason_text"] is not None
                    else "schema v4以前の判断は容量分類未割当"
                ),
            },
        }

    def record_synchronized_liquidation_valuation(
        self,
        request: PortfolioValuationRequest,
    ) -> dict[str, object]:
        """Append one V2 all-position valuation without changing simulated cash."""

        writer_id = self._require_writer()
        if request.writer_id != writer_id:
            raise InvalidVirtualRecord("valuation writer_id does not own the portfolio lock")
        cutoff = _as_utc(request.valuation_cutoff, "valuation cutoff")
        observed = _as_utc(request.observed_at, "valuation observed_at")
        if request.policy_sha256 != self.policy(as_of=cutoff, known_at=observed).policy_sha256:
            raise InvalidVirtualRecord("valuation policy hash differs from the stored policy")
        expected_rows = self.connection.execute(
            """
            SELECT o.trade_id
            FROM simulated_trade_opens AS o
            JOIN simulated_trade_open_observations AS ok ON ok.trade_id = o.trade_id
            LEFT JOIN simulated_trade_close_observations AS ck
              ON ck.trade_id = o.trade_id AND ck.close_known_at_ns <= ?
            LEFT JOIN simulated_trade_closes AS c
              ON c.trade_id = o.trade_id AND c.close_time_ns <= ?
             AND ck.trade_id IS NOT NULL
            WHERE o.open_time_ns <= ?
              AND ok.open_known_at_ns <= ?
              AND c.trade_id IS NULL
            ORDER BY o.trade_id
            """,
            (
                _to_ns(observed),
                _to_ns(cutoff),
                _to_ns(cutoff),
                _to_ns(observed),
            ),
        ).fetchall()
        expected_trade_ids = tuple(str(row["trade_id"]) for row in expected_rows)
        cash = self.cash_balance_jpy(as_of=cutoff, known_at=observed)
        initial_capital = int(self._portfolio_config()["initial_capital_jpy"])
        with self.connection:
            return record_synchronized_valuation(
                self.connection,
                request,
                expected_open_trade_ids=expected_trade_ids,
                cash_jpy=cash,
                cash_high_water_jpy=self.peak_balance_jpy(
                    as_of=cutoff,
                    known_at=observed,
                ),
                realized_net_pnl_jpy=cash - initial_capital,
            )

    def virtual_portfolio_v2_snapshot(
        self,
        *,
        as_of: datetime | None = None,
        known_at: datetime | None = None,
    ) -> dict[str, object]:
        """Return the additive V2 accounting and liquidation projection."""

        return latest_v2_snapshot(
            self.connection,
            as_of=as_of,
            known_at=known_at,
        )

    def migrate_virtual_portfolio_v2_accounting(
        self,
        *,
        recorded_at: datetime | None = None,
    ) -> dict[str, object]:
        """Append missing V1 accounting rows to V2 without rewriting history."""

        writer_id = self._require_writer()
        with self.connection:
            return migrate_legacy_accounting(
                self.connection,
                writer_id=writer_id,
                recorded_at=recorded_at or datetime.now(UTC),
            )

    def audit_virtual_portfolio_v2(self) -> dict[str, object]:
        """Recompute V2 chain, journal, projection, and valuation identities."""

        _verify_schema(self.connection)
        return audit_v2(self.connection)

    def _require_writer(self) -> str:
        if self.writer_id is None:
            raise VirtualPortfolioError("mutation requires open_virtual_portfolio_writer")
        return self.writer_id


_OPEN_INSERT_KEYS = (
    "trade_id",
    "decision_id",
    "open_time_ns",
    "symbol",
    "timeframe",
    "direction",
    "units",
    "entry_bid",
    "entry_ask",
    "entry_mid",
    "entry_executable",
    "stop_price",
    "target_price",
    "quote_to_jpy_rate",
    "quote_conversion_source",
    "planned_risk_jpy",
    "quote_json",
    "rationale_json",
    "features_json",
    "components_json",
    "warnings_json",
    "data_sha256",
    "model_sha256",
    "policy_sha256",
    "record_sha256",
)


def _quote_payload(quote: QuoteSnapshot) -> dict[str, object]:
    return {
        "source_record_id": quote.source_record_id,
        "source": quote.source,
        "revision_id": quote.revision_id,
        "event_time": _as_utc(quote.event_time, "quote event_time").isoformat(),
        "available_time": _as_utc(quote.available_time, "quote available_time").isoformat(),
        "ingested_time": _as_utc(quote.ingested_time, "quote ingested_time").isoformat(),
        "bid": quote.bid,
        "ask": quote.ask,
    }


@contextmanager
def open_virtual_portfolio_writer(
    path: str | Path,
    *,
    writer_id: str = DEFAULT_WRITER_ID,
) -> Iterator[VirtualPortfolioStore]:
    """Open the only mutation-capable connection for one portfolio database."""

    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(_writer_lock_path(target), os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise VirtualPortfolioWriterBusy(
                f"virtual portfolio writer is already active: {target}"
            ) from error
        connection = _connect(target)
        try:
            _initialize(
                connection,
                writer_id=_required_text(writer_id, "writer_id"),
            )
            yield VirtualPortfolioStore(
                path=target,
                connection=connection,
                writer_id=_required_text(writer_id, "writer_id"),
            )
        finally:
            connection.close()
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def open_virtual_portfolio_reader(path: str | Path) -> Iterator[VirtualPortfolioStore]:
    """Open an existing virtual portfolio under SQLite query-only enforcement."""

    target = Path(path).resolve()
    if not target.is_file():
        raise VirtualPortfolioError(f"virtual portfolio does not exist: {target}")
    connection = _connect(target, read_only=True)
    try:
        _verify_schema(connection)
        connection.execute("PRAGMA query_only=ON")
        yield VirtualPortfolioStore(path=target, connection=connection, writer_id=None)
    finally:
        connection.close()


def unavailable_snapshot(path: str | Path, *, now: datetime | None = None) -> dict[str, object]:
    """Return an explicit fail-closed dashboard payload for a missing store."""

    moment = _as_utc(now or datetime.now(UTC), "snapshot now")
    return {
        "configured": False,
        "read_only": True,
        "execution_mode": "offline_simulation",
        "broker_connected": False,
        "database_path": str(Path(path).resolve()),
        "generated_at": moment.isoformat(),
        "balance_jpy": None,
        "cash_jpy": None,
        "equity_jpy": None,
        "unrealized_pnl_jpy": None,
        "equity_drawdown_jpy": None,
        "equity_drawdown_pct": None,
        "equity_valuation_status": "unavailable",
        "equity_mark_time": None,
        "initial_capital_jpy": 1_000_000,
        "cumulative_net_pnl_jpy": None,
        "daily_target_jpy": 70_000,
        "remaining_daily_target_jpy": None,
        "today_pnl_jpy": None,
        "target_progress": 0.0,
        "target_met": False,
        "daily_profit_task": {
            "contract": "virtual-daily-profit-task-v1",
            "required": True,
            "session_date_jst": moment.astimezone(TOKYO).date().isoformat(),
            "deadline_jst": (
                moment.astimezone(TOKYO).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                + timedelta(days=1)
            ).isoformat(),
            "target_jpy": 70_000,
            "realized_net_pnl_jpy": None,
            "remaining_jpy": None,
            "progress_ratio": None,
            "status": "unavailable",
            "completion_basis": "canonical_realized_net_pnl",
            "optimization_input": False,
            "training_label": False,
            "achievement_guaranteed": False,
            "safety_constraints_override_allowed": False,
        },
        "open_position_count": 0,
        "open_positions": [],
        "trades": [],
        "recent_decisions": [],
        "pending_decision_count": 0,
        "pending_decisions": [],
        "pending_session_close_request": None,
        "session_close_lifecycle": {
            "status": "unavailable",
            "request": None,
            "completion": None,
            "authoritative_key": "request_id",
        },
        "horizon_allocation": {
            "contract": "virtual-horizon-allocation-v1",
            "policy": {
                **asdict(VirtualHorizonAllocationPolicy()),
                "capacity_timeframes": list(VirtualHorizonAllocationPolicy().capacity_timeframes),
                "observation_timeframes": list(
                    VirtualHorizonAllocationPolicy().observation_timeframes
                ),
            },
            "policy_sha256": VirtualHorizonAllocationPolicy().policy_sha256,
            "counts": {
                "intraday_capacity": 0,
                "observation_only": 0,
                "unsupported": 0,
                "legacy_unassigned": 0,
            },
            "intraday_open_positions": 0,
            "capacity_limit": 2,
            "capacity_utilization": 0.0,
            "legacy_positions_outside_policy": 0,
        },
        "daily_series": [],
        "balance_curve": [],
        "gross_notional_jpy": 0,
        "gross_leverage": None,
        "reconciliation": {
            "passed": False,
            "difference_jpy": None,
        },
        "data_quality_state": {
            "status": "unavailable",
            "pending_decisions": 0,
            "position_marks_complete": False,
            "position_marks_missing": 0,
            "canonical_ledger_reconciled": False,
            "market_open": is_market_open(moment),
            "daily_stop_active": True,
            "daily_loss_stop_active": True,
            "daily_target_stop_active": False,
            "daily_target_reference_met": False,
            "daily_target_task_completed": False,
            "risk_block_active": True,
            "session_close_lifecycle_ok": False,
        },
        "learning": {
            "contract": VIRTUAL_LEARNING_CONTRACT,
            "eligible_rows": 0,
            "canonical_net_r": {
                "contract": "virtual-portfolio-canonical-net-r-summary-v1",
                "eligible_rows": 0,
                "net_label_samples": 0,
                "net_label_coverage": None,
                "label_version": "net-r-v3",
                "research_only": True,
                "promotion_eligible": False,
            },
            "minimum_rows": 150,
            "ready_for_validation": False,
            "promotion_eligible": False,
            "daily_target_is_training_label": False,
        },
        "risk_gate": {
            "can_open": False,
            "reason": "仮想ポートフォリオDBが未初期化",
        },
        "research_kpis": {
            "contract": "virtual-portfolio-research-kpis-v1",
            "primary": "rolling_20d_cost_adjusted_expectancy_net_r",
            "rolling_20d": {
                "trade_count": 0,
                "net_pnl_jpy": 0,
                "net_r": 0.0,
                "cost_adjusted_expectancy_net_r": None,
                "expectancy_95pct_ci": {
                    "status": "insufficient_data",
                    "lower": None,
                    "upper": None,
                },
                "scope": "intraday_capacity_only",
                "excluded_from_primary": {
                    "trade_count": 0,
                    "net_pnl_jpy": 0,
                    "net_r": 0.0,
                    "reason": "legacy_unassigned_or_non_intraday",
                },
            },
            "minimum_learning_rows": 150,
            "daily_profit_reference": {
                "value_jpy": 70_000,
                "status": "required_operational_task",
                "required": True,
                "optimization_input": False,
                "training_label": False,
                "achievement_guaranteed": False,
            },
            "daily_profit_task": {
                "contract": "virtual-daily-profit-task-v1",
                "required": True,
                "status": "unavailable",
                "target_jpy": 70_000,
                "optimization_input": False,
                "training_label": False,
                "achievement_guaranteed": False,
                "safety_constraints_override_allowed": False,
            },
            "claim_scope": "descriptive_only_evaluation_unavailable",
        },
        "performance_claim_status": "evaluation_unavailable",
        "performance_claim_reason": "仮想台帳が未初期化です",
    }


def portfolio_snapshot(
    path: str | Path,
    *,
    now: datetime | None = None,
    trade_limit: int = 100,
) -> dict[str, Any]:
    """Read a dashboard-safe snapshot or an explicit unavailable payload."""

    target = Path(path).resolve()
    if not target.is_file():
        return unavailable_snapshot(target, now=now)
    try:
        snapshot_as_of = _as_utc(now or datetime.now(UTC), "portfolio snapshot as_of")
        with open_virtual_portfolio_reader(target) as store:
            payload = store.snapshot(now=snapshot_as_of, trade_limit=trade_limit)
            learning_view = payload.get("learning")
            if isinstance(learning_view, dict):
                eligible_rows = int(learning_view.get("eligible_rows") or 0)
                try:
                    learning_export = store.export_learning_rows(as_of=snapshot_as_of)
                    rows = learning_export.get("rows")
                    if not isinstance(rows, list):
                        raise InvalidVirtualRecord("canonical learning rows are unavailable")
                    learning_view["canonical_net_r"] = canonical_store_connection_summary(
                        rows,
                        eligible_rows=eligible_rows,
                        store_path=target.with_name("canonical_outcomes.jsonl"),
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as error:
                    learning_view["canonical_net_r"] = {
                        "contract": "virtual-portfolio-canonical-connection-v1",
                        "eligible_rows": eligible_rows,
                        "connected_rows": 0,
                        "generated_label_samples": 0,
                        "net_label_samples": 0,
                        "net_label_coverage": 0.0 if eligible_rows else None,
                        "status": "invalid_or_unavailable",
                        "reason": (
                            f"canonical_store_connection_failed:" f"{type(error).__name__}:{error}"
                        ),
                        "label_version": "net-r-v3",
                        "research_only": True,
                        "promotion_eligible": False,
                    }
            return payload
    except (sqlite3.Error, VirtualPortfolioError) as error:
        payload = unavailable_snapshot(target, now=now)
        payload["risk_gate"] = {
            "can_open": False,
            "reason": f"仮想台帳を検証できない: {error}",
        }
        payload["performance_claim_reason"] = "仮想台帳の整合性確認に失敗しました"
        return payload


__all__ = [
    "COUNTERFACTUAL_CONTRACT",
    "DEFAULT_PORTFOLIO_ID",
    "DEFAULT_WRITER_ID",
    "FAILURE_CLASSIFIER_VERSION",
    "SCHEMA_VERSION",
    "ImmutableVirtualRecordConflict",
    "InvalidVirtualRecord",
    "PositionMarkRequest",
    "QuoteSnapshot",
    "TradeCloseRequest",
    "VirtualPortfolioError",
    "VirtualPortfolioPolicy",
    "VirtualSafetyLearningPolicy",
    "VirtualPortfolioStore",
    "VirtualPortfolioWriterBusy",
    "five_x_virtual_portfolio_policy",
    "open_virtual_portfolio_reader",
    "open_virtual_portfolio_writer",
    "portfolio_snapshot",
    "unavailable_snapshot",
]
