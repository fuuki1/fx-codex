"""Additive V2 accounting and synchronized valuation for the virtual portfolio.

The module has no market-data collector, broker adapter, order endpoint, or
account mutation capability.  It only appends research-ledger evidence to an
already-open SQLite connection.  Callers own the surrounding SQLite
transaction so legacy projections and V2 records can commit atomically.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import re
import sqlite3
from typing import Any, cast

V2_SCHEMA_VERSION = 1
EVENT_CONTRACT = "virtual-portfolio-event-v2"
VALUATION_CONTRACT = "virtual-portfolio-valuation-v2"
POSITION_VALUATION_CONTRACT = "virtual-position-valuation-v2"
GENESIS_EVENT_SHA256 = "0" * 64
_EVIDENCE_KINDS = frozenset({"observed", "contracted", "modeled"})
_CONVERSION_RECORD_RE = re.compile(r"^(dukascopy:USDJPY:(?P<event_time>.+):[^:;]+);")
V2_AUDIT_IDENTITY_SCOPE = (
    "event_chain_and_payload_hashes",
    "journal_balance_and_component_postings",
    "legacy_cash_projection",
    "position_cost_reserves_and_liquidation_pnl",
    "portfolio_mid_executable_liquidation_equity",
    "cash_and_realized_pnl_as_observed_at_state_head",
    "liquidation_hwm_and_drawdown",
    "source_age_and_cross_position_skew",
    "position_mark_record_and_quote_provenance",
    "valuation_state_head_predecessor",
)


class VirtualPortfolioV2Error(RuntimeError):
    """Base error for V2 portfolio accounting and valuation."""


class InvalidV2Record(VirtualPortfolioV2Error):
    """A V2 record is incomplete, inconsistent, or temporally invalid."""


class ImmutableV2Conflict(VirtualPortfolioV2Error):
    """An immutable V2 identity was reused with different content."""


@dataclass(frozen=True)
class JournalPosting:
    """One signed JPY posting; a complete transaction must sum to zero."""

    account: str
    amount_jpy: int
    detail: Mapping[str, object] | None = None


@dataclass(frozen=True)
class CanonicalEventRequest:
    """Immutable input to one hash-chained canonical event."""

    event_id: str
    event_type: str
    source_record_id: str
    effective_time: datetime
    observed_at: datetime
    recorded_at: datetime
    payload: Mapping[str, object]
    policy_sha256: str
    writer_id: str
    correlation_id: str = ""
    causation_id: str = ""
    schema_version: int = V2_SCHEMA_VERSION


@dataclass(frozen=True)
class PositionLiquidationValuation:
    """Cost-reserved liquidation value for one open simulated position."""

    trade_id: str
    valuation_cutoff: datetime
    observed_at: datetime
    source_event_time: datetime
    mid_unrealized_pnl_jpy: int
    executable_unrealized_pnl_jpy: int
    exit_cost_reserve_base_jpy: int
    exit_cost_reserve_low_jpy: int
    exit_cost_reserve_high_jpy: int
    evidence_kind: str
    quote_ids: tuple[str, ...]
    valuation_model_sha256: str
    max_source_age_seconds: int
    detail: Mapping[str, object] | None = None


@dataclass(frozen=True)
class PortfolioValuationRequest:
    """One portfolio-wide valuation at a single market-data cutoff."""

    valuation_id: str
    valuation_cutoff: datetime
    observed_at: datetime
    recorded_at: datetime
    positions: tuple[PositionLiquidationValuation, ...]
    policy_sha256: str
    valuation_model_sha256: str
    writer_id: str
    detail: Mapping[str, object] | None = None
    quality_complete: bool = True


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS portfolio_event_log (
    event_seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    source_record_id TEXT NOT NULL UNIQUE,
    effective_time_ns INTEGER NOT NULL,
    observed_at_ns INTEGER NOT NULL,
    recorded_at_ns INTEGER NOT NULL,
    correlation_id TEXT NOT NULL,
    causation_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    prev_event_sha256 TEXT NOT NULL CHECK (length(prev_event_sha256) = 64),
    event_sha256 TEXT NOT NULL UNIQUE CHECK (length(event_sha256) = 64),
    policy_sha256 TEXT NOT NULL CHECK (length(policy_sha256) = 64),
    writer_id TEXT NOT NULL,
    CHECK (effective_time_ns <= observed_at_ns),
    CHECK (observed_at_ns <= recorded_at_ns)
) STRICT;

CREATE INDEX IF NOT EXISTS portfolio_event_log_time_idx
ON portfolio_event_log(effective_time_ns, event_seq);

CREATE TABLE IF NOT EXISTS journal_postings (
    posting_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL,
    line_no INTEGER NOT NULL CHECK (line_no >= 0),
    account TEXT NOT NULL,
    amount_jpy INTEGER NOT NULL,
    currency TEXT NOT NULL CHECK (currency = 'JPY'),
    detail_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
    writer_id TEXT NOT NULL,
    UNIQUE(transaction_id, line_no),
    FOREIGN KEY(transaction_id) REFERENCES journal_transactions(transaction_id)
        DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE IF NOT EXISTS journal_transactions (
    transaction_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    transaction_type TEXT NOT NULL,
    effective_time_ns INTEGER NOT NULL,
    description TEXT NOT NULL,
    posting_count INTEGER NOT NULL CHECK (posting_count >= 0),
    posting_sum_jpy INTEGER NOT NULL CHECK (posting_sum_jpy = 0),
    record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
    writer_id TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES portfolio_event_log(event_id)
) STRICT;

CREATE INDEX IF NOT EXISTS journal_postings_transaction_idx
ON journal_postings(transaction_id, line_no);

CREATE TABLE IF NOT EXISTS portfolio_liquidation_valuations (
    valuation_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    valuation_cutoff_ns INTEGER NOT NULL UNIQUE,
    observed_at_ns INTEGER NOT NULL,
    recorded_at_ns INTEGER NOT NULL,
    state_event_head_seq INTEGER NOT NULL CHECK (state_event_head_seq >= 0),
    state_event_head_sha256 TEXT NOT NULL CHECK (length(state_event_head_sha256) = 64),
    position_count INTEGER NOT NULL CHECK (position_count >= 0),
    marked_position_count INTEGER NOT NULL CHECK (marked_position_count >= 0),
    cash_jpy INTEGER NOT NULL,
    realized_net_pnl_jpy INTEGER NOT NULL,
    mid_equity_jpy INTEGER,
    executable_equity_jpy INTEGER,
    liquidation_equity_base_jpy INTEGER,
    liquidation_equity_low_jpy INTEGER,
    liquidation_equity_high_jpy INTEGER,
    exit_cost_reserve_base_jpy INTEGER,
    exit_cost_reserve_low_jpy INTEGER,
    exit_cost_reserve_high_jpy INTEGER,
    liquidation_hwm_base_jpy INTEGER,
    liquidation_hwm_low_jpy INTEGER,
    liquidation_drawdown_base_jpy INTEGER,
    liquidation_drawdown_low_jpy INTEGER,
    valuation_status TEXT NOT NULL CHECK (
        valuation_status IN ('complete', 'incomplete_synchronized_valuation')
    ),
    cross_position_skew_seconds INTEGER,
    max_source_age_seconds INTEGER,
    valuation_model_sha256 TEXT NOT NULL CHECK (length(valuation_model_sha256) = 64),
    detail_json TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
    writer_id TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES portfolio_event_log(event_id),
    CHECK (valuation_cutoff_ns <= observed_at_ns),
    CHECK (observed_at_ns <= recorded_at_ns),
    CHECK (marked_position_count <= position_count)
) STRICT;

CREATE TABLE IF NOT EXISTS position_liquidation_valuations (
    position_valuation_id TEXT PRIMARY KEY,
    portfolio_valuation_id TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    valuation_cutoff_ns INTEGER NOT NULL,
    observed_at_ns INTEGER NOT NULL,
    mid_unrealized_pnl_jpy INTEGER NOT NULL,
    executable_unrealized_pnl_jpy INTEGER NOT NULL,
    exit_cost_reserve_base_jpy INTEGER NOT NULL
        CHECK (exit_cost_reserve_base_jpy >= 0),
    exit_cost_reserve_low_jpy INTEGER NOT NULL
        CHECK (exit_cost_reserve_low_jpy >= 0),
    exit_cost_reserve_high_jpy INTEGER NOT NULL
        CHECK (exit_cost_reserve_high_jpy >= 0),
    liquidation_net_pnl_base_jpy INTEGER NOT NULL,
    liquidation_net_pnl_low_jpy INTEGER NOT NULL,
    liquidation_net_pnl_high_jpy INTEGER NOT NULL,
    evidence_kind TEXT NOT NULL CHECK (
        evidence_kind IN ('observed', 'contracted', 'modeled')
    ),
    quote_ids_json TEXT NOT NULL,
    valuation_model_sha256 TEXT NOT NULL CHECK (length(valuation_model_sha256) = 64),
    max_source_age_seconds INTEGER NOT NULL CHECK (max_source_age_seconds >= 0),
    detail_json TEXT NOT NULL,
    record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
    writer_id TEXT NOT NULL,
    UNIQUE(portfolio_valuation_id, trade_id),
    FOREIGN KEY(portfolio_valuation_id)
        REFERENCES portfolio_liquidation_valuations(valuation_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (valuation_cutoff_ns <= observed_at_ns),
    CHECK (exit_cost_reserve_low_jpy >= exit_cost_reserve_base_jpy),
    CHECK (exit_cost_reserve_base_jpy >= exit_cost_reserve_high_jpy),
    CHECK (liquidation_net_pnl_low_jpy <= liquidation_net_pnl_base_jpy),
    CHECK (liquidation_net_pnl_base_jpy <= liquidation_net_pnl_high_jpy)
) STRICT;
"""

_APPEND_ONLY_TABLES = (
    "portfolio_event_log",
    "journal_transactions",
    "journal_postings",
    "portfolio_liquidation_valuations",
    "position_liquidation_valuations",
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise InvalidV2Record(f"{name} is required")
    return normalized


def _sha256(value: object, name: str) -> str:
    normalized = _text(value, name).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise InvalidV2Record(f"{name} must be a lowercase SHA-256")
    return normalized


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidV2Record(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _to_ns(value: datetime) -> int:
    return int(_utc(value, "timestamp").timestamp() * 1_000_000_000)


def _from_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


def _ceil_nonnegative_seconds(value: timedelta, name: str) -> int:
    """Conservatively convert a non-negative duration to whole seconds."""

    microseconds = value.days * 86_400 * 1_000_000 + value.seconds * 1_000_000 + value.microseconds
    if microseconds < 0:
        raise InvalidV2Record(f"{name} cannot be negative")
    return (microseconds + 999_999) // 1_000_000


def _event_identity(request: CanonicalEventRequest) -> dict[str, object]:
    effective = _utc(request.effective_time, "event effective_time")
    observed = _utc(request.observed_at, "event observed_at")
    recorded = _utc(request.recorded_at, "event recorded_at")
    if not effective <= observed <= recorded:
        raise InvalidV2Record("event timestamps must satisfy effective <= observed <= recorded")
    if request.schema_version <= 0:
        raise InvalidV2Record("event schema_version must be positive")
    return {
        "contract": EVENT_CONTRACT,
        "event_id": _text(request.event_id, "event_id"),
        "event_type": _text(request.event_type, "event_type"),
        "source_record_id": _text(request.source_record_id, "source_record_id"),
        "effective_time_ns": _to_ns(effective),
        "observed_at_ns": _to_ns(observed),
        "correlation_id": str(request.correlation_id or ""),
        "causation_id": str(request.causation_id or ""),
        "schema_version": request.schema_version,
        "payload_sha256": _sha256_json(dict(request.payload)),
        "policy_sha256": _sha256(request.policy_sha256, "policy_sha256"),
    }


def install_v2_schema(connection: sqlite3.Connection) -> None:
    """Install only additive V2 tables and immutable guards."""

    pending: list[str] = []
    for line in _SCHEMA_SQL.splitlines():
        pending.append(line)
        candidate = "\n".join(pending)
        if not sqlite3.complete_statement(candidate):
            continue
        statement = candidate.strip()
        if statement:
            connection.execute(statement)
        pending = []
    if "\n".join(pending).strip():
        raise InvalidV2Record("V2 schema contains incomplete SQL")
    for table in _APPEND_ONLY_TABLES:
        connection.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is append-only');
            END
            """)
        connection.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} is append-only');
            END
            """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS journal_postings_no_after_seal
        BEFORE INSERT ON journal_postings
        WHEN EXISTS (
            SELECT 1 FROM journal_transactions
            WHERE transaction_id = NEW.transaction_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'journal transaction is sealed');
        END
        """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS position_valuations_no_after_seal
        BEFORE INSERT ON position_liquidation_valuations
        WHEN EXISTS (
            SELECT 1 FROM portfolio_liquidation_valuations
            WHERE valuation_id = NEW.portfolio_valuation_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'portfolio valuation is sealed');
        END
        """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS journal_transactions_require_balance
        BEFORE INSERT ON journal_transactions
        WHEN
            (SELECT COUNT(*) FROM journal_postings
             WHERE transaction_id = NEW.transaction_id) != NEW.posting_count
            OR
            COALESCE(
                (SELECT SUM(amount_jpy) FROM journal_postings
                 WHERE transaction_id = NEW.transaction_id),
                0
            ) != 0
        BEGIN
            SELECT RAISE(ABORT, 'journal transaction is not balanced');
        END
        """)


def event_head(connection: sqlite3.Connection) -> tuple[int, str]:
    row = connection.execute("""
        SELECT event_seq, event_sha256
        FROM portfolio_event_log
        ORDER BY event_seq DESC
        LIMIT 1
        """).fetchone()
    if row is None:
        return 0, GENESIS_EVENT_SHA256
    return int(row["event_seq"]), str(row["event_sha256"])


def append_canonical_event(
    connection: sqlite3.Connection,
    request: CanonicalEventRequest,
) -> dict[str, object]:
    """Append one event without committing the caller's transaction."""

    identity = _event_identity(request)
    existing = connection.execute(
        "SELECT * FROM portfolio_event_log WHERE event_id = ?",
        (identity["event_id"],),
    ).fetchone()
    if existing is not None:
        comparable = {
            "contract": EVENT_CONTRACT,
            "event_id": str(existing["event_id"]),
            "event_type": str(existing["event_type"]),
            "source_record_id": str(existing["source_record_id"]),
            "effective_time_ns": int(existing["effective_time_ns"]),
            "observed_at_ns": int(existing["observed_at_ns"]),
            "correlation_id": str(existing["correlation_id"]),
            "causation_id": str(existing["causation_id"]),
            "schema_version": int(existing["schema_version"]),
            "payload_sha256": str(existing["payload_sha256"]),
            "policy_sha256": str(existing["policy_sha256"]),
        }
        if comparable != identity:
            raise ImmutableV2Conflict(
                f"canonical event already exists with different content: {request.event_id}"
            )
        return {
            "inserted": False,
            "event_seq": int(existing["event_seq"]),
            "event_id": str(existing["event_id"]),
            "event_sha256": str(existing["event_sha256"]),
        }

    recorded = _utc(request.recorded_at, "event recorded_at")
    previous_seq, previous_sha = event_head(connection)
    event_seq = previous_seq + 1
    event_header = {
        **identity,
        "event_seq": event_seq,
        "recorded_at_ns": _to_ns(recorded),
        "prev_event_sha256": previous_sha,
        "writer_id": _text(request.writer_id, "writer_id"),
    }
    event_sha = _sha256_json(event_header)
    connection.execute(
        """
        INSERT INTO portfolio_event_log(
            event_seq, event_id, event_type, source_record_id,
            effective_time_ns, observed_at_ns, recorded_at_ns,
            correlation_id, causation_id, schema_version,
            payload_json, payload_sha256, prev_event_sha256, event_sha256,
            policy_sha256, writer_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_seq,
            identity["event_id"],
            identity["event_type"],
            identity["source_record_id"],
            identity["effective_time_ns"],
            identity["observed_at_ns"],
            event_header["recorded_at_ns"],
            identity["correlation_id"],
            identity["causation_id"],
            identity["schema_version"],
            _json(dict(request.payload)),
            identity["payload_sha256"],
            previous_sha,
            event_sha,
            identity["policy_sha256"],
            event_header["writer_id"],
        ),
    )
    return {
        "inserted": True,
        "event_seq": event_seq,
        "event_id": identity["event_id"],
        "event_sha256": event_sha,
    }


def append_journal_transaction(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    transaction_id: str,
    transaction_type: str,
    effective_time: datetime,
    description: str,
    postings: Sequence[JournalPosting],
    writer_id: str,
) -> dict[str, object]:
    """Append and seal one structurally balanced JPY transaction."""

    event = connection.execute(
        "SELECT effective_time_ns FROM portfolio_event_log WHERE event_id = ?",
        (_text(event_id, "event_id"),),
    ).fetchone()
    if event is None:
        raise InvalidV2Record("journal transaction requires an existing canonical event")
    effective_ns = _to_ns(_utc(effective_time, "journal effective_time"))
    if effective_ns != int(event["effective_time_ns"]):
        raise InvalidV2Record("journal and canonical event effective times differ")
    normalized: list[dict[str, object]] = []
    for line_no, posting in enumerate(postings):
        if isinstance(posting.amount_jpy, bool) or not isinstance(posting.amount_jpy, int):
            raise InvalidV2Record("journal amount_jpy must be an integer")
        normalized.append(
            {
                "line_no": line_no,
                "account": _text(posting.account, "journal account"),
                "amount_jpy": posting.amount_jpy,
                "detail": dict(posting.detail or {}),
            }
        )
    if sum(posting.amount_jpy for posting in postings) != 0:
        raise InvalidV2Record("journal postings must sum to zero JPY")
    identity = {
        "event_id": event_id,
        "transaction_id": _text(transaction_id, "transaction_id"),
        "transaction_type": _text(transaction_type, "transaction_type"),
        "effective_time_ns": effective_ns,
        "description": _text(description, "description"),
        "postings": normalized,
    }
    record_sha = _sha256_json(identity)
    existing = connection.execute(
        "SELECT record_sha256 FROM journal_transactions WHERE transaction_id = ?",
        (identity["transaction_id"],),
    ).fetchone()
    if existing is not None:
        if str(existing["record_sha256"]) != record_sha:
            raise ImmutableV2Conflict(
                "journal transaction already exists with different content: " f"{transaction_id}"
            )
        return {"inserted": False, "transaction_id": transaction_id}

    normalized_writer = _text(writer_id, "writer_id")
    for row in normalized:
        posting_payload = {
            "transaction_id": identity["transaction_id"],
            **row,
        }
        posting_sha = _sha256_json(posting_payload)
        connection.execute(
            """
            INSERT INTO journal_postings(
                posting_id, transaction_id, line_no, account, amount_jpy,
                currency, detail_json, record_sha256, writer_id
            ) VALUES (?, ?, ?, ?, ?, 'JPY', ?, ?, ?)
            """,
            (
                f"post-{posting_sha[:24]}",
                identity["transaction_id"],
                row["line_no"],
                row["account"],
                row["amount_jpy"],
                _json(row["detail"]),
                posting_sha,
                normalized_writer,
            ),
        )
    connection.execute(
        """
        INSERT INTO journal_transactions(
            transaction_id, event_id, transaction_type, effective_time_ns,
            description, posting_count, posting_sum_jpy, record_sha256, writer_id
        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            identity["transaction_id"],
            event_id,
            identity["transaction_type"],
            effective_ns,
            identity["description"],
            len(normalized),
            record_sha,
            normalized_writer,
        ),
    )
    return {"inserted": True, "transaction_id": transaction_id}


def append_accounting_event(
    connection: sqlite3.Connection,
    request: CanonicalEventRequest,
    *,
    transaction_id: str,
    transaction_type: str,
    description: str,
    postings: Sequence[JournalPosting],
) -> dict[str, object]:
    """Append an event and its balanced journal in the caller's transaction."""

    event_result = append_canonical_event(connection, request)
    journal_result = append_journal_transaction(
        connection,
        event_id=request.event_id,
        transaction_id=transaction_id,
        transaction_type=transaction_type,
        effective_time=request.effective_time,
        description=description,
        postings=postings,
        writer_id=request.writer_id,
    )
    return {"event": event_result, "journal": journal_result}


def assert_legacy_accounting_projected(connection: sqlite3.Connection) -> None:
    """Fail closed if a legacy cash event has not entered the V2 chain."""

    missing = connection.execute("""
        SELECT l.event_id
        FROM ledger_events AS l
        LEFT JOIN portfolio_event_log AS e
          ON e.source_record_id = l.event_id
        WHERE e.event_id IS NULL
        ORDER BY l.effective_time_ns, l.event_id
        LIMIT 1
        """).fetchone()
    if missing is not None:
        raise InvalidV2Record(
            "V2 accounting migration is required before further ledger writes; "
            f"missing legacy event {missing['event_id']}"
        )


def migrate_legacy_accounting(
    connection: sqlite3.Connection,
    *,
    writer_id: str,
    recorded_at: datetime,
) -> dict[str, object]:
    """Append missing legacy cash events with explicit backfill limitations.

    The migration never rewrites legacy rows.  A close event's original writer
    observation time was not stored by V1, so the migration uses the preserved
    quote ingestion time as a lower-bound observation timestamp and labels that
    limitation in the canonical payload.
    """

    migration_time = _utc(recorded_at, "migration recorded_at")
    rows = connection.execute("""
        SELECT l.*, e.event_id AS projected_event_id
        FROM ledger_events AS l
        LEFT JOIN portfolio_event_log AS e ON e.source_record_id = l.event_id
        WHERE e.event_id IS NULL
        ORDER BY l.effective_time_ns, l.event_id
        """).fetchall()
    inserted = 0
    for row in rows:
        postings: tuple[JournalPosting, ...]
        legacy_event_id = str(row["event_id"])
        event_type = str(row["event_type"])
        effective = _from_ns(int(row["effective_time_ns"]))
        amount = int(row["amount_jpy"])
        if event_type == "portfolio_created":
            config = connection.execute("""
                SELECT portfolio_id, policy_sha256, initial_capital_jpy
                FROM portfolio_config
                ORDER BY created_at_ns
                LIMIT 1
                """).fetchone()
            if config is None:
                raise InvalidV2Record("legacy portfolio configuration is missing")
            if amount != int(config["initial_capital_jpy"]):
                raise InvalidV2Record("legacy initial capital does not reconcile")
            observed = effective
            policy_sha = str(config["policy_sha256"])
            correlation_id = str(config["portfolio_id"])
            postings = (
                JournalPosting(account="asset.cash_jpy", amount_jpy=amount),
                JournalPosting(account="equity.initial_capital", amount_jpy=-amount),
            )
            description = "V1固定仮想研究資本をV2へ追記移行"
            payload: dict[str, object] = {
                "legacy_event_id": legacy_event_id,
                "legacy_event_type": event_type,
                "amount_jpy": amount,
                "migration": {
                    "backfilled": True,
                    "observed_at_basis": "legacy effective_time",
                    "original_recorded_at_available": False,
                },
            }
        elif event_type == "trade_closed":
            close = connection.execute(
                """
                SELECT c.*, o.policy_sha256, o.decision_id
                FROM simulated_trade_closes AS c
                JOIN simulated_trade_opens AS o ON o.trade_id = c.trade_id
                WHERE c.trade_id = ?
                """,
                (row["trade_id"],),
            ).fetchone()
            if close is None:
                raise InvalidV2Record(f"legacy close detail is missing for {row['trade_id']}")
            if amount != int(close["net_pnl_jpy"]):
                raise InvalidV2Record(
                    f"legacy close amount does not reconcile for {row['trade_id']}"
                )
            quote = json.loads(str(close["quote_json"]))
            ingested_raw = quote.get("ingested_time")
            if not ingested_raw:
                raise InvalidV2Record(
                    f"legacy close quote ingestion time is missing for {row['trade_id']}"
                )
            try:
                ingested = datetime.fromisoformat(str(ingested_raw))
            except ValueError as error:
                raise InvalidV2Record(
                    f"legacy close quote ingestion time is invalid for {row['trade_id']}"
                ) from error
            observed = max(effective, _utc(ingested, "legacy quote ingested_time"))
            policy_sha = str(close["policy_sha256"])
            correlation_id = str(row["trade_id"])
            gross = int(close["gross_market_pnl_jpy"])
            postings = (
                JournalPosting(account="asset.cash_jpy", amount_jpy=amount),
                JournalPosting(
                    account="pnl.market_move_unseparated_fx",
                    amount_jpy=-gross,
                    detail={
                        "limitation": (
                            "legacy V1 close does not separate market move " "from FX translation"
                        )
                    },
                ),
                JournalPosting(
                    account="expense.spread",
                    amount_jpy=int(close["spread_quote_cost_jpy"]),
                ),
                JournalPosting(
                    account="expense.slippage",
                    amount_jpy=int(close["slippage_cost_jpy"]),
                ),
                JournalPosting(
                    account="expense.commission",
                    amount_jpy=int(close["commission_jpy"]),
                ),
                JournalPosting(
                    account="expense.financing",
                    amount_jpy=int(close["financing_jpy"]),
                ),
                JournalPosting(
                    account="expense.conversion_spread",
                    amount_jpy=int(close["conversion_cost_jpy"]),
                ),
            )
            description = f"V1仮想取引{row['trade_id']}をV2へ追記移行"
            payload = {
                "legacy_event_id": legacy_event_id,
                "legacy_event_type": event_type,
                "trade_id": str(row["trade_id"]),
                "amount_jpy": amount,
                "legacy_close_record_sha256": str(close["record_sha256"]),
                "migration": {
                    "backfilled": True,
                    "observed_at_basis": "legacy quote ingested_time lower bound",
                    "original_recorded_at_available": False,
                },
            }
        else:
            raise InvalidV2Record(f"unsupported legacy event type: {event_type}")
        result = append_accounting_event(
            connection,
            CanonicalEventRequest(
                event_id=f"canonical-{legacy_event_id}",
                event_type=event_type,
                source_record_id=legacy_event_id,
                effective_time=effective,
                observed_at=observed,
                recorded_at=max(migration_time, observed),
                payload=payload,
                policy_sha256=policy_sha,
                writer_id=writer_id,
                correlation_id=correlation_id,
            ),
            transaction_id=f"journal-{legacy_event_id}",
            transaction_type=event_type,
            description=description,
            postings=postings,
        )
        event_result = cast(Mapping[str, object], result["event"])
        inserted += int(bool(event_result["inserted"]))
    assert_legacy_accounting_projected(connection)
    return {
        "contract": "virtual-portfolio-v2-accounting-migration",
        "inserted": inserted,
        "remaining": 0,
        "original_recorded_at_available": False,
    }


def _position_payload(position: PositionLiquidationValuation) -> dict[str, object]:
    cutoff = _utc(position.valuation_cutoff, "position valuation_cutoff")
    observed = _utc(position.observed_at, "position observed_at")
    source_event = _utc(position.source_event_time, "position source_event_time")
    if source_event > cutoff or cutoff > observed:
        raise InvalidV2Record(
            "position timestamps must satisfy source_event <= cutoff <= observed_at"
        )
    if position.evidence_kind not in _EVIDENCE_KINDS:
        raise InvalidV2Record("complete position valuation requires cost evidence")
    if position.max_source_age_seconds < 0:
        raise InvalidV2Record("max_source_age_seconds cannot be negative")
    price_age = _ceil_nonnegative_seconds(
        cutoff - source_event,
        "position price source age",
    )
    if position.max_source_age_seconds < price_age:
        raise InvalidV2Record("max_source_age_seconds cannot be below the price source age")
    quote_ids = tuple(_text(value, "quote_id") for value in position.quote_ids)
    if not quote_ids:
        raise InvalidV2Record("open position valuation requires quote_ids")
    if len(set(quote_ids)) != len(quote_ids):
        raise InvalidV2Record("position quote_ids must be unique")
    base = position.exit_cost_reserve_base_jpy
    low = position.exit_cost_reserve_low_jpy
    high = position.exit_cost_reserve_high_jpy
    pnl_values = (
        position.mid_unrealized_pnl_jpy,
        position.executable_unrealized_pnl_jpy,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (base, low, high, *pnl_values)
    ):
        raise InvalidV2Record("P&L and exit cost reserves must be integer JPY")
    if not low >= base >= high >= 0:
        raise InvalidV2Record(
            "cost reserves must satisfy low-scenario >= base >= high-scenario >= 0"
        )
    if position.executable_unrealized_pnl_jpy > position.mid_unrealized_pnl_jpy:
        raise InvalidV2Record("executable unrealized P&L cannot exceed mid P&L")
    return {
        "contract": POSITION_VALUATION_CONTRACT,
        "trade_id": _text(position.trade_id, "trade_id"),
        "valuation_cutoff": cutoff.isoformat(),
        "observed_at": observed.isoformat(),
        "source_event_time": source_event.isoformat(),
        "mid_unrealized_pnl_jpy": position.mid_unrealized_pnl_jpy,
        "executable_unrealized_pnl_jpy": position.executable_unrealized_pnl_jpy,
        "exit_cost_reserve_base_jpy": base,
        "exit_cost_reserve_low_jpy": low,
        "exit_cost_reserve_high_jpy": high,
        "liquidation_net_pnl_base_jpy": (position.executable_unrealized_pnl_jpy - base),
        "liquidation_net_pnl_low_jpy": position.executable_unrealized_pnl_jpy - low,
        "liquidation_net_pnl_high_jpy": position.executable_unrealized_pnl_jpy - high,
        "evidence_kind": position.evidence_kind,
        "quote_ids": list(quote_ids),
        "valuation_model_sha256": _sha256(
            position.valuation_model_sha256,
            "position valuation_model_sha256",
        ),
        "max_source_age_seconds": position.max_source_age_seconds,
        "detail": dict(position.detail or {}),
    }


def record_synchronized_valuation(
    connection: sqlite3.Connection,
    request: PortfolioValuationRequest,
    *,
    expected_open_trade_ids: Sequence[str],
    cash_jpy: int,
    cash_high_water_jpy: int,
    realized_net_pnl_jpy: int,
) -> dict[str, object]:
    """Append one all-or-null synchronized liquidation valuation."""

    assert_legacy_accounting_projected(connection)
    cutoff = _utc(request.valuation_cutoff, "portfolio valuation_cutoff")
    observed = _utc(request.observed_at, "portfolio observed_at")
    recorded = _utc(request.recorded_at, "portfolio recorded_at")
    if not cutoff <= observed <= recorded:
        raise InvalidV2Record("portfolio timestamps must satisfy cutoff <= observed <= recorded")
    expected = tuple(sorted(_text(value, "expected trade_id") for value in expected_open_trade_ids))
    if len(set(expected)) != len(expected):
        raise InvalidV2Record("expected open trade IDs must be unique")
    position_payloads = [_position_payload(position) for position in request.positions]
    supplied = tuple(sorted(str(row["trade_id"]) for row in position_payloads))
    if len(set(supplied)) != len(supplied):
        raise InvalidV2Record("position valuation trade IDs must be unique")
    portfolio_model_sha = _sha256(
        request.valuation_model_sha256,
        "portfolio valuation_model_sha256",
    )
    if any(str(row["valuation_model_sha256"]) != portfolio_model_sha for row in position_payloads):
        raise InvalidV2Record(
            "position valuation_model_sha256 must match the portfolio valuation model"
        )
    unexpected = sorted(set(supplied) - set(expected))
    if unexpected:
        raise InvalidV2Record(
            "position valuations contain trades that are not open at cutoff: "
            + ",".join(unexpected)
        )
    for row in position_payloads:
        if row["valuation_cutoff"] != cutoff.isoformat():
            raise InvalidV2Record("all positions must use the portfolio valuation cutoff")
        if datetime.fromisoformat(str(row["observed_at"])).astimezone(UTC) > observed:
            raise InvalidV2Record("position observed_at cannot follow portfolio observed_at")

    if isinstance(cash_jpy, bool) or not isinstance(cash_jpy, int):
        raise InvalidV2Record("cash_jpy must be integer JPY")
    if isinstance(cash_high_water_jpy, bool) or not isinstance(cash_high_water_jpy, int):
        raise InvalidV2Record("cash_high_water_jpy must be integer JPY")
    if cash_high_water_jpy < cash_jpy:
        raise InvalidV2Record("cash high-water mark cannot be below current cash")
    request_identity = {
        "valuation_id": _text(request.valuation_id, "valuation_id"),
        "valuation_cutoff": cutoff.isoformat(),
        "observed_at": observed.isoformat(),
        "recorded_at": recorded.isoformat(),
        "positions": position_payloads,
        "expected_open_trade_ids": list(expected),
        "cash_jpy": cash_jpy,
        "cash_high_water_jpy": cash_high_water_jpy,
        "realized_net_pnl_jpy": realized_net_pnl_jpy,
        "policy_sha256": _sha256(request.policy_sha256, "policy_sha256"),
        "valuation_model_sha256": portfolio_model_sha,
        "writer_id": _text(request.writer_id, "writer_id"),
        "detail": dict(request.detail or {}),
        "quality_complete": request.quality_complete,
    }
    request_sha = _sha256_json(request_identity)
    existing = connection.execute(
        """
        SELECT request_sha256, valuation_status
        FROM portfolio_liquidation_valuations
        WHERE valuation_id = ?
        """,
        (request.valuation_id,),
    ).fetchone()
    if existing is not None:
        if str(existing["request_sha256"]) != request_sha:
            raise ImmutableV2Conflict(
                f"portfolio valuation already exists with different content: {request.valuation_id}"
            )
        return {
            "inserted": False,
            "valuation_id": request.valuation_id,
            "valuation_status": str(existing["valuation_status"]),
        }
    latest = connection.execute("""
        SELECT valuation_id, valuation_cutoff_ns
        FROM portfolio_liquidation_valuations
        ORDER BY valuation_cutoff_ns DESC
        LIMIT 1
        """).fetchone()
    if latest is not None and _to_ns(cutoff) <= int(latest["valuation_cutoff_ns"]):
        raise InvalidV2Record("valuation cutoffs must be strictly increasing")

    complete = supplied == expected and request.quality_complete
    status = "complete" if complete else "incomplete_synchronized_valuation"
    head_seq, head_sha = event_head(connection)
    base_detail: dict[str, Any] = {
        "contract": VALUATION_CONTRACT,
        "valuation_id": _text(request.valuation_id, "valuation_id"),
        "valuation_cutoff": cutoff.isoformat(),
        "observed_at": observed.isoformat(),
        "state_event_head_seq": head_seq,
        "state_event_head_sha256": head_sha,
        "position_count": len(expected),
        "marked_position_count": len(position_payloads),
        "expected_open_trade_ids": list(expected),
        "supplied_trade_ids": list(supplied),
        "cash_jpy": cash_jpy,
        "realized_net_pnl_jpy": realized_net_pnl_jpy,
        "valuation_status": status,
        "valuation_model_sha256": _sha256(
            request.valuation_model_sha256,
            "portfolio valuation_model_sha256",
        ),
        "detail": dict(request.detail or {}),
    }

    numeric: dict[str, int | None]
    if complete:
        mid_pnl = sum(cast(int, row["mid_unrealized_pnl_jpy"]) for row in position_payloads)
        executable_pnl = sum(
            cast(int, row["executable_unrealized_pnl_jpy"]) for row in position_payloads
        )
        reserve_base = sum(
            cast(int, row["exit_cost_reserve_base_jpy"]) for row in position_payloads
        )
        reserve_low = sum(cast(int, row["exit_cost_reserve_low_jpy"]) for row in position_payloads)
        reserve_high = sum(
            cast(int, row["exit_cost_reserve_high_jpy"]) for row in position_payloads
        )
        liquidation_base = cash_jpy + executable_pnl - reserve_base
        liquidation_low = cash_jpy + executable_pnl - reserve_low
        liquidation_high = cash_jpy + executable_pnl - reserve_high
        previous = connection.execute("""
            SELECT liquidation_hwm_base_jpy, liquidation_hwm_low_jpy
            FROM portfolio_liquidation_valuations
            WHERE valuation_status = 'complete'
            ORDER BY valuation_cutoff_ns DESC
            LIMIT 1
            """).fetchone()
        previous_base_hwm = (
            int(previous["liquidation_hwm_base_jpy"]) if previous else cash_high_water_jpy
        )
        previous_low_hwm = (
            int(previous["liquidation_hwm_low_jpy"]) if previous else cash_high_water_jpy
        )
        previous_base_hwm = max(previous_base_hwm, cash_high_water_jpy)
        previous_low_hwm = max(previous_low_hwm, cash_high_water_jpy)
        base_hwm = max(previous_base_hwm, liquidation_base)
        low_hwm = max(previous_low_hwm, liquidation_low)
        numeric = {
            "mid_equity_jpy": cash_jpy + mid_pnl,
            "executable_equity_jpy": cash_jpy + executable_pnl,
            "liquidation_equity_base_jpy": liquidation_base,
            "liquidation_equity_low_jpy": liquidation_low,
            "liquidation_equity_high_jpy": liquidation_high,
            "exit_cost_reserve_base_jpy": reserve_base,
            "exit_cost_reserve_low_jpy": reserve_low,
            "exit_cost_reserve_high_jpy": reserve_high,
            "liquidation_hwm_base_jpy": base_hwm,
            "liquidation_hwm_low_jpy": low_hwm,
            "liquidation_drawdown_base_jpy": base_hwm - liquidation_base,
            "liquidation_drawdown_low_jpy": low_hwm - liquidation_low,
        }
    else:
        numeric = {
            "mid_equity_jpy": None,
            "executable_equity_jpy": None,
            "liquidation_equity_base_jpy": None,
            "liquidation_equity_low_jpy": None,
            "liquidation_equity_high_jpy": None,
            "exit_cost_reserve_base_jpy": None,
            "exit_cost_reserve_low_jpy": None,
            "exit_cost_reserve_high_jpy": None,
            "liquidation_hwm_base_jpy": None,
            "liquidation_hwm_low_jpy": None,
            "liquidation_drawdown_base_jpy": None,
            "liquidation_drawdown_low_jpy": None,
        }
    max_age = (
        max(cast(int, row["max_source_age_seconds"]) for row in position_payloads)
        if position_payloads
        else 0
    )
    source_event_times = [
        datetime.fromisoformat(str(row["source_event_time"])).astimezone(UTC)
        for row in position_payloads
    ]
    skew = (
        _ceil_nonnegative_seconds(
            max(source_event_times) - min(source_event_times),
            "cross-position source skew",
        )
        if source_event_times
        else 0
    )
    payload = {**base_detail, **numeric, "positions": position_payloads}
    policy_sha = str(request_identity["policy_sha256"])
    event_id = f"valuation-event-{_sha256_json(payload)[:24]}"
    source_record_id = f"valuation-{request.valuation_id}"
    event = append_canonical_event(
        connection,
        CanonicalEventRequest(
            event_id=event_id,
            event_type="portfolio_valuation",
            source_record_id=source_record_id,
            effective_time=cutoff,
            observed_at=observed,
            recorded_at=recorded,
            payload=payload,
            policy_sha256=policy_sha,
            writer_id=request.writer_id,
            correlation_id=request.valuation_id,
            causation_id=head_sha,
        ),
    )
    record_sha = _sha256_json(payload)
    for row in position_payloads:
        position_sha = _sha256_json(row)
        connection.execute(
            """
            INSERT INTO position_liquidation_valuations(
                position_valuation_id, portfolio_valuation_id, trade_id,
                valuation_cutoff_ns, observed_at_ns,
                mid_unrealized_pnl_jpy, executable_unrealized_pnl_jpy,
                exit_cost_reserve_base_jpy, exit_cost_reserve_low_jpy,
                exit_cost_reserve_high_jpy, liquidation_net_pnl_base_jpy,
                liquidation_net_pnl_low_jpy, liquidation_net_pnl_high_jpy,
                evidence_kind, quote_ids_json, valuation_model_sha256,
                max_source_age_seconds, detail_json, record_sha256, writer_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"position-valuation-{position_sha[:24]}",
                request.valuation_id,
                row["trade_id"],
                _to_ns(cutoff),
                _to_ns(datetime.fromisoformat(str(row["observed_at"]))),
                row["mid_unrealized_pnl_jpy"],
                row["executable_unrealized_pnl_jpy"],
                row["exit_cost_reserve_base_jpy"],
                row["exit_cost_reserve_low_jpy"],
                row["exit_cost_reserve_high_jpy"],
                row["liquidation_net_pnl_base_jpy"],
                row["liquidation_net_pnl_low_jpy"],
                row["liquidation_net_pnl_high_jpy"],
                row["evidence_kind"],
                _json(row["quote_ids"]),
                row["valuation_model_sha256"],
                row["max_source_age_seconds"],
                _json(row["detail"]),
                position_sha,
                request.writer_id,
            ),
        )
    connection.execute(
        """
        INSERT INTO portfolio_liquidation_valuations(
            valuation_id, event_id, valuation_cutoff_ns, observed_at_ns, recorded_at_ns,
            state_event_head_seq, state_event_head_sha256,
            position_count, marked_position_count, cash_jpy, realized_net_pnl_jpy,
            mid_equity_jpy, executable_equity_jpy,
            liquidation_equity_base_jpy, liquidation_equity_low_jpy,
            liquidation_equity_high_jpy, exit_cost_reserve_base_jpy,
            exit_cost_reserve_low_jpy, exit_cost_reserve_high_jpy,
            liquidation_hwm_base_jpy, liquidation_hwm_low_jpy,
            liquidation_drawdown_base_jpy, liquidation_drawdown_low_jpy,
            valuation_status, cross_position_skew_seconds, max_source_age_seconds,
            valuation_model_sha256, detail_json, request_sha256, record_sha256, writer_id
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            request.valuation_id,
            event["event_id"],
            _to_ns(cutoff),
            _to_ns(observed),
            _to_ns(recorded),
            head_seq,
            head_sha,
            len(expected),
            len(position_payloads),
            cash_jpy,
            realized_net_pnl_jpy,
            numeric["mid_equity_jpy"],
            numeric["executable_equity_jpy"],
            numeric["liquidation_equity_base_jpy"],
            numeric["liquidation_equity_low_jpy"],
            numeric["liquidation_equity_high_jpy"],
            numeric["exit_cost_reserve_base_jpy"],
            numeric["exit_cost_reserve_low_jpy"],
            numeric["exit_cost_reserve_high_jpy"],
            numeric["liquidation_hwm_base_jpy"],
            numeric["liquidation_hwm_low_jpy"],
            numeric["liquidation_drawdown_base_jpy"],
            numeric["liquidation_drawdown_low_jpy"],
            status,
            skew,
            max_age,
            base_detail["valuation_model_sha256"],
            _json(base_detail["detail"]),
            request_sha,
            record_sha,
            _text(request.writer_id, "writer_id"),
        ),
    )
    return {
        "inserted": True,
        "valuation_id": request.valuation_id,
        "valuation_status": status,
        **numeric,
        "cross_position_skew_seconds": skew,
        "max_source_age_seconds": max_age,
    }


def latest_v2_snapshot(
    connection: sqlite3.Connection,
    *,
    as_of: datetime | None = None,
    known_at: datetime | None = None,
) -> dict[str, object]:
    """Return the latest PIT-visible V2 state without inventing missing values."""

    effective_cutoff = _to_ns(as_of) if as_of is not None else 2**63 - 1
    knowledge_cutoff = _to_ns(known_at) if known_at is not None else 2**63 - 1
    if as_of is not None and known_at is not None and known_at < as_of:
        raise InvalidV2Record("V2 snapshot known_at cannot precede as_of")

    schema_exists = connection.execute("""
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'portfolio_event_log'
        """).fetchone()
    if schema_exists is None:
        return {
            "contract": VALUATION_CONTRACT,
            "schema_version": V2_SCHEMA_VERSION,
            "accounting_status": "v2_schema_migration_required",
            "legacy_accounting_event_count": None,
            "projected_accounting_event_count": 0,
            "event_count": 0,
            "event_head_sha256": GENESIS_EVENT_SHA256,
            "valuation_status": "unavailable_without_v2_schema",
            "valuation_cutoff": None,
            "cash_jpy": None,
            "realized_net_pnl_jpy": None,
            "mid_equity_jpy": None,
            "executable_equity_jpy": None,
            "liquidation_equity_base_jpy": None,
            "liquidation_equity_low_jpy": None,
            "liquidation_equity_high_jpy": None,
        }
    visible_events = connection.execute(
        """
        SELECT event_seq, event_sha256
        FROM portfolio_event_log
        WHERE effective_time_ns <= ? AND observed_at_ns <= ? AND recorded_at_ns <= ?
        ORDER BY event_seq
        """,
        (effective_cutoff, knowledge_cutoff, knowledge_cutoff),
    ).fetchall()
    event_count = len(visible_events)
    head_sha = str(visible_events[-1]["event_sha256"]) if visible_events else GENESIS_EVENT_SHA256
    close_observations_exist = connection.execute("""
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'simulated_trade_close_observations'
        """).fetchone()
    if close_observations_exist is None:
        legacy_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM ledger_events WHERE effective_time_ns <= ?",
                (effective_cutoff,),
            ).fetchone()[0]
        )
    else:
        legacy_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM ledger_events l
                LEFT JOIN simulated_trade_close_observations ck
                  ON ck.trade_id = l.trade_id
                WHERE l.effective_time_ns <= ?
                  AND (l.event_type <> 'trade_closed' OR l.trade_id IS NULL
                       OR ck.close_known_at_ns <= ?)
                """,
                (effective_cutoff, knowledge_cutoff),
            ).fetchone()[0]
        )
    projected_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM ledger_events AS l
            JOIN portfolio_event_log AS e ON e.source_record_id = l.event_id
            WHERE e.effective_time_ns <= ? AND e.observed_at_ns <= ?
              AND e.recorded_at_ns <= ?
            """,
            (effective_cutoff, knowledge_cutoff, knowledge_cutoff),
        ).fetchone()[0]
    )
    row = connection.execute(
        """
        SELECT *
        FROM portfolio_liquidation_valuations
        WHERE valuation_cutoff_ns <= ? AND observed_at_ns <= ? AND recorded_at_ns <= ?
        ORDER BY valuation_cutoff_ns DESC
        LIMIT 1
        """,
        (effective_cutoff, knowledge_cutoff, knowledge_cutoff),
    ).fetchone()
    accounting_status = "ready" if legacy_count == projected_count else "migration_required"
    if row is None:
        return {
            "contract": VALUATION_CONTRACT,
            "schema_version": V2_SCHEMA_VERSION,
            "accounting_status": accounting_status,
            "legacy_accounting_event_count": legacy_count,
            "projected_accounting_event_count": projected_count,
            "event_count": event_count,
            "event_head_sha256": head_sha,
            "valuation_status": "unavailable_without_synchronized_valuation",
            "valuation_cutoff": None,
            "cash_jpy": None,
            "realized_net_pnl_jpy": None,
            "mid_equity_jpy": None,
            "executable_equity_jpy": None,
            "liquidation_equity_base_jpy": None,
            "liquidation_equity_low_jpy": None,
            "liquidation_equity_high_jpy": None,
        }
    result: dict[str, object] = {
        "contract": VALUATION_CONTRACT,
        "schema_version": V2_SCHEMA_VERSION,
        "accounting_status": accounting_status,
        "legacy_accounting_event_count": legacy_count,
        "projected_accounting_event_count": projected_count,
        "event_count": event_count,
        "event_head_sha256": head_sha,
        "valuation_id": str(row["valuation_id"]),
        "valuation_status": str(row["valuation_status"]),
        "valuation_cutoff": _from_ns(int(row["valuation_cutoff_ns"])).isoformat(),
        "observed_at": _from_ns(int(row["observed_at_ns"])).isoformat(),
        "position_count": int(row["position_count"]),
        "marked_position_count": int(row["marked_position_count"]),
        "cash_jpy": int(row["cash_jpy"]),
        "realized_net_pnl_jpy": int(row["realized_net_pnl_jpy"]),
        "cross_position_skew_seconds": row["cross_position_skew_seconds"],
        "max_source_age_seconds": row["max_source_age_seconds"],
    }
    for name in (
        "mid_equity_jpy",
        "executable_equity_jpy",
        "liquidation_equity_base_jpy",
        "liquidation_equity_low_jpy",
        "liquidation_equity_high_jpy",
        "exit_cost_reserve_base_jpy",
        "exit_cost_reserve_low_jpy",
        "exit_cost_reserve_high_jpy",
        "liquidation_hwm_base_jpy",
        "liquidation_hwm_low_jpy",
        "liquidation_drawdown_base_jpy",
        "liquidation_drawdown_low_jpy",
    ):
        result[name] = int(row[name]) if row[name] is not None else None
    return result


def audit_v2(connection: sqlite3.Connection) -> dict[str, object]:
    """Recompute chain, journal, projection, and valuation identities."""

    schema_exists = connection.execute("""
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'portfolio_event_log'
        """).fetchone()
    if schema_exists is None:
        return {
            "contract": "virtual-portfolio-v2-audit",
            "schema_version": V2_SCHEMA_VERSION,
            "passed": False,
            "errors": ["v2_schema_migration_required"],
            "event_count": 0,
            "event_head_sha256": GENESIS_EVENT_SHA256,
            "journal_transaction_count": 0,
            "valuation_count": 0,
            "identity_scope": list(V2_AUDIT_IDENTITY_SCOPE),
        }
    errors: list[str] = []
    required_triggers = {
        *(f"{table}_no_update" for table in _APPEND_ONLY_TABLES),
        *(f"{table}_no_delete" for table in _APPEND_ONLY_TABLES),
        "journal_postings_no_after_seal",
        "journal_transactions_require_balance",
        "position_valuations_no_after_seal",
    }
    installed_triggers = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    for trigger in sorted(required_triggers - installed_triggers):
        errors.append(f"immutable_trigger_missing:{trigger}")
    previous_hash = GENESIS_EVENT_SHA256
    expected_seq = 1
    for row in connection.execute(
        "SELECT * FROM portfolio_event_log ORDER BY event_seq"
    ).fetchall():
        seq = int(row["event_seq"])
        if seq != expected_seq:
            errors.append(f"event_seq_gap:{expected_seq}->{seq}")
        try:
            payload = json.loads(str(row["payload_json"]))
        except (json.JSONDecodeError, TypeError):
            payload = {"invalid_payload_json": str(row["payload_json"])}
            errors.append(f"payload_json_invalid:{row['event_id']}")
        payload_sha = _sha256_json(payload)
        if payload_sha != str(row["payload_sha256"]):
            errors.append(f"payload_hash_mismatch:{row['event_id']}")
        if str(row["prev_event_sha256"]) != previous_hash:
            errors.append(f"previous_hash_mismatch:{row['event_id']}")
        identity = {
            "contract": EVENT_CONTRACT,
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "source_record_id": str(row["source_record_id"]),
            "effective_time_ns": int(row["effective_time_ns"]),
            "observed_at_ns": int(row["observed_at_ns"]),
            "correlation_id": str(row["correlation_id"]),
            "causation_id": str(row["causation_id"]),
            "schema_version": int(row["schema_version"]),
            "payload_sha256": str(row["payload_sha256"]),
            "policy_sha256": str(row["policy_sha256"]),
            "event_seq": seq,
            "recorded_at_ns": int(row["recorded_at_ns"]),
            "prev_event_sha256": str(row["prev_event_sha256"]),
            "writer_id": str(row["writer_id"]),
        }
        computed_event_sha = _sha256_json(identity)
        if computed_event_sha != str(row["event_sha256"]):
            errors.append(f"event_hash_mismatch:{row['event_id']}")
        previous_hash = str(row["event_sha256"])
        expected_seq = seq + 1

    for row in connection.execute("SELECT * FROM journal_transactions").fetchall():
        postings = connection.execute(
            """
            SELECT *
            FROM journal_postings
            WHERE transaction_id = ?
            ORDER BY line_no
            """,
            (row["transaction_id"],),
        ).fetchall()
        posting_sum = sum(int(posting["amount_jpy"]) for posting in postings)
        if len(postings) != int(row["posting_count"]):
            errors.append(f"journal_count_mismatch:{row['transaction_id']}")
        if posting_sum != 0:
            errors.append(f"journal_unbalanced:{row['transaction_id']}:{posting_sum}")
        normalized_postings: list[dict[str, object]] = []
        for posting in postings:
            try:
                detail = json.loads(str(posting["detail_json"]))
            except (json.JSONDecodeError, TypeError):
                detail = {"invalid_detail_json": str(posting["detail_json"])}
                errors.append(f"posting_detail_json:{posting['posting_id']}")
            posting_payload = {
                "transaction_id": str(posting["transaction_id"]),
                "line_no": int(posting["line_no"]),
                "account": str(posting["account"]),
                "amount_jpy": int(posting["amount_jpy"]),
                "detail": detail,
            }
            if _sha256_json(posting_payload) != str(posting["record_sha256"]):
                errors.append(f"posting_hash_mismatch:{posting['posting_id']}")
            normalized_postings.append(
                {
                    "line_no": int(posting["line_no"]),
                    "account": str(posting["account"]),
                    "amount_jpy": int(posting["amount_jpy"]),
                    "detail": detail,
                }
            )
        transaction_payload = {
            "event_id": str(row["event_id"]),
            "transaction_id": str(row["transaction_id"]),
            "transaction_type": str(row["transaction_type"]),
            "effective_time_ns": int(row["effective_time_ns"]),
            "description": str(row["description"]),
            "postings": normalized_postings,
        }
        if _sha256_json(transaction_payload) != str(row["record_sha256"]):
            errors.append(f"transaction_hash_mismatch:{row['transaction_id']}")

    try:
        assert_legacy_accounting_projected(connection)
    except InvalidV2Record as error:
        errors.append(f"legacy_projection_incomplete:{error}")
    missing_journal = connection.execute("""
        SELECT l.event_id
        FROM ledger_events AS l
        JOIN portfolio_event_log AS e ON e.source_record_id = l.event_id
        LEFT JOIN journal_transactions AS t ON t.event_id = e.event_id
        WHERE t.transaction_id IS NULL
        ORDER BY l.effective_time_ns, l.event_id
        LIMIT 1
        """).fetchone()
    if missing_journal is not None:
        errors.append(f"legacy_projection_missing_journal:{missing_journal['event_id']}")
    legacy_cash_jpy = int(
        connection.execute("SELECT COALESCE(SUM(amount_jpy), 0) FROM ledger_events").fetchone()[0]
    )
    journal_cash_jpy = int(connection.execute("""
            SELECT COALESCE(SUM(amount_jpy), 0)
            FROM journal_postings
            WHERE account = 'asset.cash_jpy'
            """).fetchone()[0])
    if journal_cash_jpy != legacy_cash_jpy:
        errors.append(
            f"cash_projection_mismatch:legacy={legacy_cash_jpy}:journal={journal_cash_jpy}"
        )

    for legacy in connection.execute("""
        SELECT l.*, e.event_id AS canonical_event_id
        FROM ledger_events AS l
        LEFT JOIN portfolio_event_log AS e ON e.source_record_id = l.event_id
        ORDER BY l.effective_time_ns, l.event_id
        """).fetchall():
        canonical_event_id = legacy["canonical_event_id"]
        if canonical_event_id is None:
            continue
        transaction = connection.execute(
            """
            SELECT *
            FROM journal_transactions
            WHERE event_id = ?
            """,
            (canonical_event_id,),
        ).fetchone()
        if transaction is None:
            continue
        posting_rows = connection.execute(
            """
            SELECT account, amount_jpy
            FROM journal_postings
            WHERE transaction_id = ?
            ORDER BY line_no
            """,
            (transaction["transaction_id"],),
        ).fetchall()
        actual_postings = [
            (str(posting["account"]), int(posting["amount_jpy"])) for posting in posting_rows
        ]
        actual_accounts = [account for account, _ in actual_postings]
        if len(actual_accounts) != len(set(actual_accounts)):
            errors.append(f"journal_duplicate_account:{legacy['event_id']}")
        event_type = str(legacy["event_type"])
        amount = int(legacy["amount_jpy"])
        expected_postings: list[tuple[str, int]]
        if event_type == "portfolio_created":
            expected_postings = [
                ("asset.cash_jpy", amount),
                ("equity.initial_capital", -amount),
            ]
        elif event_type == "trade_closed":
            close = connection.execute(
                """
                SELECT *
                FROM simulated_trade_closes
                WHERE trade_id = ?
                """,
                (legacy["trade_id"],),
            ).fetchone()
            if close is None:
                errors.append(f"close_component_source_missing:{legacy['trade_id']}")
                continue
            gross = int(close["gross_market_pnl_jpy"])
            component_cost = (
                int(close["spread_quote_cost_jpy"])
                + int(close["slippage_cost_jpy"])
                + int(close["commission_jpy"])
                + int(close["financing_jpy"])
                + int(close["conversion_cost_jpy"])
            )
            expected_net = gross - component_cost
            if expected_net != int(close["net_pnl_jpy"]) or expected_net != amount:
                errors.append(f"close_cost_identity:{legacy['trade_id']}")
            expected_postings = [
                ("asset.cash_jpy", amount),
                ("pnl.market_move_unseparated_fx", -gross),
                ("expense.spread", int(close["spread_quote_cost_jpy"])),
                ("expense.slippage", int(close["slippage_cost_jpy"])),
                ("expense.commission", int(close["commission_jpy"])),
                ("expense.financing", int(close["financing_jpy"])),
                ("expense.conversion_spread", int(close["conversion_cost_jpy"])),
            ]
        else:
            errors.append(f"legacy_event_type_unsupported:{legacy['event_id']}")
            continue
        if str(transaction["transaction_type"]) != event_type:
            errors.append(f"journal_transaction_type:{legacy['event_id']}")
        if actual_postings != expected_postings:
            errors.append(f"journal_component_identity:{legacy['event_id']}")

    config = connection.execute("""
        SELECT initial_capital_jpy
        FROM portfolio_config
        ORDER BY created_at_ns
        LIMIT 1
        """).fetchone()
    initial_capital = int(config["initial_capital_jpy"]) if config is not None else 0
    previous_base_hwm: int | None = None
    previous_low_hwm: int | None = None
    valuation_rows = connection.execute("""
        SELECT *
        FROM portfolio_liquidation_valuations
        ORDER BY valuation_cutoff_ns, valuation_id
        """).fetchall()
    # 各valuationごとに現金postingを全走査すると N+1 になる(実機で1,581回×同一join)。
    # 対象は account='asset.cash_jpy' の全件でも数十行しかないため、一度だけ読み出して
    # 以降は Python 側で (event_seq, effective_time_ns) の上限で絞り込む。
    # 走査順は元のSQLと同じ (effective_time_ns, event_id) を維持し、cash_hwm の
    # 逐次計算結果が変わらないようにする。
    all_cash_rows = connection.execute("""
        SELECT t.effective_time_ns, e.event_id, p.amount_jpy, e.event_seq
        FROM portfolio_event_log AS e
        JOIN journal_transactions AS t ON t.event_id = e.event_id
        JOIN journal_postings AS p ON p.transaction_id = t.transaction_id
        WHERE p.account = 'asset.cash_jpy'
        ORDER BY t.effective_time_ns, e.event_id
        """).fetchall()
    cash_ledger = [
        (
            int(cash_event["event_seq"]),
            int(cash_event["effective_time_ns"]),
            int(cash_event["amount_jpy"]),
        )
        for cash_event in all_cash_rows
    ]
    for row in valuation_rows:
        valuation_id = str(row["valuation_id"])
        canonical = connection.execute(
            """
            SELECT *
            FROM portfolio_event_log
            WHERE event_id = ?
            """,
            (row["event_id"],),
        ).fetchone()
        if canonical is None or str(canonical["payload_sha256"]) != str(row["record_sha256"]):
            errors.append(f"valuation_record_hash:{valuation_id}")
        try:
            payload = json.loads(str(canonical["payload_json"])) if canonical is not None else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
            errors.append(f"valuation_payload_json:{valuation_id}")
        if not isinstance(payload, dict):
            payload = {}
            errors.append(f"valuation_payload_type:{valuation_id}")
        if canonical is not None:
            timeline_fields = (
                ("valuation_cutoff_ns", "effective_time_ns"),
                ("observed_at_ns", "observed_at_ns"),
                ("recorded_at_ns", "recorded_at_ns"),
            )
            for row_name, event_name in timeline_fields:
                if int(row[row_name]) != int(canonical[event_name]):
                    errors.append(f"valuation_timeline_identity:{valuation_id}:{row_name}")
            expected_cutoff = _from_ns(int(row["valuation_cutoff_ns"])).isoformat()
            expected_observed = _from_ns(int(row["observed_at_ns"])).isoformat()
            if payload.get("valuation_cutoff") != expected_cutoff:
                errors.append(f"valuation_payload_timeline:{valuation_id}:valuation_cutoff")
            if payload.get("observed_at") != expected_observed:
                errors.append(f"valuation_payload_timeline:{valuation_id}:observed_at")
        positions = connection.execute(
            """
            SELECT *
            FROM position_liquidation_valuations
            WHERE portfolio_valuation_id = ?
            ORDER BY trade_id
            """,
            (valuation_id,),
        ).fetchall()
        if len(positions) != int(row["marked_position_count"]):
            errors.append(f"valuation_marked_position_count:{valuation_id}")

        canonical_positions_raw = payload.get("positions")
        canonical_positions = (
            canonical_positions_raw if isinstance(canonical_positions_raw, list) else []
        )
        if len(canonical_positions) != len(positions):
            errors.append(f"valuation_payload_position_count:{valuation_id}")
        canonical_by_trade = {
            str(position.get("trade_id")): position
            for position in canonical_positions
            if isinstance(position, dict) and position.get("trade_id")
        }
        if len(canonical_by_trade) != len(canonical_positions):
            errors.append(f"valuation_payload_position_identity:{valuation_id}")
        source_times: list[datetime] = []
        aggregate_mid = 0
        aggregate_executable = 0
        aggregate_base_reserve = 0
        aggregate_low_reserve = 0
        aggregate_high_reserve = 0
        aggregate_planned_risk = 0
        aggregate_gap_buffer = 0
        cutoff = _from_ns(int(row["valuation_cutoff_ns"]))
        portfolio_detail = payload.get("detail")
        portfolio_detail = portfolio_detail if isinstance(portfolio_detail, dict) else {}
        try:
            stored_portfolio_detail = json.loads(str(row["detail_json"]))
        except (json.JSONDecodeError, TypeError):
            stored_portfolio_detail = {}
            errors.append(f"valuation_detail_json:{valuation_id}")
        if stored_portfolio_detail != portfolio_detail:
            errors.append(f"valuation_detail_identity:{valuation_id}")
        reserve_policy = portfolio_detail.get("reserve_policy")
        reserve_policy = reserve_policy if isinstance(reserve_policy, dict) else None
        if reserve_policy is not None:
            reserve_policy_sha = _sha256_json(reserve_policy)
            if reserve_policy_sha != str(row["valuation_model_sha256"]):
                errors.append(f"valuation_reserve_policy_hash:{valuation_id}")
            if portfolio_detail.get("reserve_policy_sha256") != reserve_policy_sha:
                errors.append(f"valuation_reserve_policy_identity:{valuation_id}")

        for position in positions:
            trade_id = str(position["trade_id"])
            canonical_position = canonical_by_trade.get(trade_id)
            if canonical_position is None:
                errors.append(f"position_payload_missing:{valuation_id}:{trade_id}")
                continue
            if _sha256_json(canonical_position) != str(position["record_sha256"]):
                errors.append(f"position_record_hash:{valuation_id}:{trade_id}")
            try:
                stored_quote_ids = json.loads(str(position["quote_ids_json"]))
            except (json.JSONDecodeError, TypeError):
                stored_quote_ids = []
                errors.append(f"position_quote_ids_json:{valuation_id}:{trade_id}")
            try:
                stored_detail = json.loads(str(position["detail_json"]))
            except (json.JSONDecodeError, TypeError):
                stored_detail = {}
                errors.append(f"position_detail_json:{valuation_id}:{trade_id}")
            stored_fields = {
                "trade_id": trade_id,
                "valuation_cutoff": _from_ns(int(position["valuation_cutoff_ns"])).isoformat(),
                "observed_at": _from_ns(int(position["observed_at_ns"])).isoformat(),
                "mid_unrealized_pnl_jpy": int(position["mid_unrealized_pnl_jpy"]),
                "executable_unrealized_pnl_jpy": int(position["executable_unrealized_pnl_jpy"]),
                "exit_cost_reserve_base_jpy": int(position["exit_cost_reserve_base_jpy"]),
                "exit_cost_reserve_low_jpy": int(position["exit_cost_reserve_low_jpy"]),
                "exit_cost_reserve_high_jpy": int(position["exit_cost_reserve_high_jpy"]),
                "liquidation_net_pnl_base_jpy": int(position["liquidation_net_pnl_base_jpy"]),
                "liquidation_net_pnl_low_jpy": int(position["liquidation_net_pnl_low_jpy"]),
                "liquidation_net_pnl_high_jpy": int(position["liquidation_net_pnl_high_jpy"]),
                "evidence_kind": str(position["evidence_kind"]),
                "quote_ids": stored_quote_ids,
                "valuation_model_sha256": str(position["valuation_model_sha256"]),
                "max_source_age_seconds": int(position["max_source_age_seconds"]),
                "detail": stored_detail,
            }
            for name, actual in stored_fields.items():
                if canonical_position.get(name) != actual:
                    errors.append(f"position_payload_identity:{valuation_id}:{trade_id}:{name}")
            mid = int(position["mid_unrealized_pnl_jpy"])
            executable_pnl = int(position["executable_unrealized_pnl_jpy"])
            base_reserve = int(position["exit_cost_reserve_base_jpy"])
            low_reserve = int(position["exit_cost_reserve_low_jpy"])
            high_reserve = int(position["exit_cost_reserve_high_jpy"])
            if executable_pnl - base_reserve != int(position["liquidation_net_pnl_base_jpy"]):
                errors.append(f"position_base_identity:{valuation_id}:{trade_id}")
            if executable_pnl - low_reserve != int(position["liquidation_net_pnl_low_jpy"]):
                errors.append(f"position_low_identity:{valuation_id}:{trade_id}")
            if executable_pnl - high_reserve != int(position["liquidation_net_pnl_high_jpy"]):
                errors.append(f"position_high_identity:{valuation_id}:{trade_id}")
            aggregate_mid += mid
            aggregate_executable += executable_pnl
            aggregate_base_reserve += base_reserve
            aggregate_low_reserve += low_reserve
            aggregate_high_reserve += high_reserve
            try:
                source_time = _utc(
                    datetime.fromisoformat(str(canonical_position["source_event_time"])),
                    "audited position source_event_time",
                )
                source_times.append(source_time)
                recomputed_age = _ceil_nonnegative_seconds(
                    cutoff - source_time,
                    "audited source age",
                )
                if recomputed_age != int(position["max_source_age_seconds"]):
                    errors.append(f"position_source_age:{valuation_id}:{trade_id}")
            except (KeyError, ValueError, InvalidV2Record):
                errors.append(f"position_source_time:{valuation_id}:{trade_id}")

            position_detail = canonical_position.get("detail")
            position_detail = position_detail if isinstance(position_detail, dict) else {}
            mark_id = position_detail.get("mark_id")
            mark = (
                connection.execute(
                    """
                    SELECT *
                    FROM simulated_position_marks
                    WHERE mark_id = ? AND trade_id = ?
                    """,
                    (mark_id, trade_id),
                ).fetchone()
                if isinstance(mark_id, str) and mark_id.strip()
                else None
            )
            opened = connection.execute(
                """
                SELECT symbol, open_time_ns, planned_risk_jpy
                FROM simulated_trade_opens
                WHERE trade_id = ?
                """,
                (trade_id,),
            ).fetchone()
            if mark is None:
                errors.append(f"position_mark_missing:{valuation_id}:{trade_id}")
            else:
                try:
                    mark_quote = json.loads(str(mark["quote_json"]))
                except (json.JSONDecodeError, TypeError):
                    mark_quote = {}
                    errors.append(f"position_mark_quote_json:{valuation_id}:{trade_id}")
                if not isinstance(mark_quote, dict):
                    mark_quote = {}
                    errors.append(f"position_mark_quote_type:{valuation_id}:{trade_id}")
                mark_payload_base: dict[str, object] = {
                    "trade_id": trade_id,
                    "mark_time": _from_ns(int(mark["mark_time_ns"])).isoformat(),
                    "observed_at": _from_ns(int(mark["observed_at_ns"])).isoformat(),
                    "bid": float(mark["bid"]),
                    "ask": float(mark["ask"]),
                    "mid": float(mark["mid"]),
                    "executable": float(mark["executable"]),
                    "quote_to_jpy_rate": float(mark["quote_to_jpy_rate"]),
                    "quote_conversion_source": str(mark["quote_conversion_source"]),
                    "gross_unrealized_pnl_jpy": int(mark["gross_unrealized_pnl_jpy"]),
                    "executable_unrealized_pnl_jpy": int(mark["executable_unrealized_pnl_jpy"]),
                    "spread_quote_cost_jpy": int(mark["spread_quote_cost_jpy"]),
                    "quote": mark_quote,
                }
                mark_hashes = {
                    _sha256_json(
                        {
                            **mark_payload_base,
                            "valuation_mode": valuation_mode,
                        }
                    )
                    for valuation_mode in (
                        "contemporaneous_quote",
                        "delayed_historical_bid_ask_replay",
                    )
                }
                mark_sha = str(mark["record_sha256"])
                if mark_sha not in mark_hashes:
                    errors.append(f"position_mark_hash:{valuation_id}:{trade_id}")
                if str(mark["mark_id"]) != f"position-mark-{mark_sha[:24]}":
                    errors.append(f"position_mark_identity:{valuation_id}:{trade_id}")
                if int(mark["mark_time_ns"]) > int(row["valuation_cutoff_ns"]):
                    errors.append(f"position_mark_after_cutoff:{valuation_id}:{trade_id}")
                if int(mark["observed_at_ns"]) > int(row["observed_at_ns"]):
                    errors.append(f"position_mark_observed_late:{valuation_id}:{trade_id}")
                if (
                    int(mark["gross_unrealized_pnl_jpy"]) != mid
                    or int(mark["executable_unrealized_pnl_jpy"]) != executable_pnl
                    or int(mark["gross_unrealized_pnl_jpy"])
                    - int(mark["executable_unrealized_pnl_jpy"])
                    != int(mark["spread_quote_cost_jpy"])
                ):
                    errors.append(f"position_mark_pnl_identity:{valuation_id}:{trade_id}")
                mark_observed = _from_ns(int(mark["observed_at_ns"])).isoformat()
                if position_detail.get("mark_observed_at") != mark_observed:
                    errors.append(f"position_mark_observation_identity:{valuation_id}:{trade_id}")
                conversion_source = str(mark["quote_conversion_source"])
                if position_detail.get("quote_conversion_source") != conversion_source:
                    errors.append(f"position_mark_conversion_identity:{valuation_id}:{trade_id}")
                try:
                    price_event_time = _utc(
                        datetime.fromisoformat(str(mark_quote["event_time"])),
                        "audited mark price event_time",
                    )
                    if price_event_time > _from_ns(int(mark["mark_time_ns"])):
                        errors.append(f"position_mark_future_quote:{valuation_id}:{trade_id}")
                    price_record_id = str(mark_quote["source_record_id"]).strip()
                    expected_quote_ids = [price_record_id]
                    expected_source_time = price_event_time
                    symbol = str(opened["symbol"]) if opened is not None else ""
                    if symbol[3:] != "JPY":
                        conversion = _CONVERSION_RECORD_RE.match(conversion_source)
                        if conversion is None:
                            raise ValueError("conversion evidence")
                        conversion_record_id = conversion.group(1)
                        conversion_time = _utc(
                            datetime.fromisoformat(conversion.group("event_time")),
                            "audited conversion event_time",
                        )
                        if conversion_time > cutoff or conversion_time > _from_ns(
                            int(mark["observed_at_ns"])
                        ):
                            errors.append(
                                f"position_mark_future_conversion:{valuation_id}:{trade_id}"
                            )
                        expected_quote_ids.append(conversion_record_id)
                        expected_source_time = min(expected_source_time, conversion_time)
                    if stored_quote_ids != expected_quote_ids:
                        errors.append(f"position_mark_quote_ids:{valuation_id}:{trade_id}")
                    if (
                        canonical_position.get("source_event_time")
                        != expected_source_time.isoformat()
                    ):
                        errors.append(f"position_mark_source_time:{valuation_id}:{trade_id}")
                except (KeyError, ValueError, InvalidV2Record):
                    errors.append(f"position_mark_source_evidence:{valuation_id}:{trade_id}")
            reserve = position_detail.get("reserve")
            reserve = reserve if isinstance(reserve, dict) else None
            if reserve_policy is None or reserve is None or opened is None:
                errors.append(f"position_cost_evidence:{valuation_id}:{trade_id}")
                continue
            try:
                planned_risk = int(opened["planned_risk_jpy"])
                open_time = _from_ns(int(opened["open_time_ns"]))
                started_days = max(
                    1,
                    math.ceil(
                        _ceil_nonnegative_seconds(
                            cutoff - open_time,
                            "audited financing duration",
                        )
                        / 86_400
                    ),
                )

                def reserve_amount(policy_key: str, multiplier: int = 1) -> int:
                    value = reserve_policy.get(policy_key)
                    if isinstance(value, bool) or not isinstance(value, int):
                        raise ValueError(policy_key)
                    return math.ceil(planned_risk * value * multiplier / 10_000)

                expected_components = {
                    "slippage_cost_jpy": reserve_amount("slippage_r_bps"),
                    "commission_jpy": reserve_amount("commission_r_bps"),
                    "financing_jpy": reserve_amount(
                        "financing_r_bps_per_started_day",
                        started_days,
                    ),
                    "conversion_cost_jpy": (
                        0
                        if str(opened["symbol"])[3:] == "JPY"
                        else reserve_amount("conversion_r_bps")
                    ),
                }
                expected_base = sum(expected_components.values())
                low_multiplier = reserve_policy.get("low_cost_multiplier_bps")
                high_multiplier = reserve_policy.get("high_cost_multiplier_bps")
                if (
                    isinstance(low_multiplier, bool)
                    or not isinstance(low_multiplier, int)
                    or isinstance(high_multiplier, bool)
                    or not isinstance(high_multiplier, int)
                ):
                    raise ValueError("reserve multiplier")
                expected_low = math.ceil(expected_base * low_multiplier / 10_000)
                expected_high = math.ceil(expected_base * high_multiplier / 10_000)
                expected_gap = reserve_amount("gap_buffer_r_bps")
                if reserve.get("components_base_jpy") != expected_components:
                    errors.append(f"position_cost_components:{valuation_id}:{trade_id}")
                if reserve.get("started_financing_days") != started_days:
                    errors.append(f"position_financing_days:{valuation_id}:{trade_id}")
                if (
                    reserve.get("reserve_base_jpy") != expected_base
                    or reserve.get("reserve_low_jpy") != expected_low
                    or reserve.get("reserve_high_jpy") != expected_high
                    or reserve.get("gap_buffer_jpy") != expected_gap
                    or base_reserve != expected_base
                    or low_reserve != expected_low
                    or high_reserve != expected_high
                ):
                    errors.append(f"position_reserve_identity:{valuation_id}:{trade_id}")
                aggregate_planned_risk += planned_risk
                aggregate_gap_buffer += expected_gap
            except (InvalidV2Record, TypeError, ValueError):
                errors.append(f"position_cost_contract:{valuation_id}:{trade_id}")

        recomputed_skew = (
            _ceil_nonnegative_seconds(
                max(source_times) - min(source_times),
                "audited cross-position skew",
            )
            if source_times
            else 0
        )
        recomputed_max_age = (
            max(int(position["max_source_age_seconds"]) for position in positions)
            if positions
            else 0
        )
        if int(row["cross_position_skew_seconds"] or 0) != recomputed_skew:
            errors.append(f"valuation_source_skew:{valuation_id}")
        if int(row["max_source_age_seconds"] or 0) != recomputed_max_age:
            errors.append(f"valuation_source_age:{valuation_id}")

        cutoff_ns = int(row["valuation_cutoff_ns"])
        state_head_seq = int(row["state_event_head_seq"])
        cash = 0
        cash_hwm = 0
        for event_seq, effective_time_ns, amount_jpy in cash_ledger:
            if event_seq > state_head_seq or effective_time_ns > cutoff_ns:
                continue
            cash += amount_jpy
            cash_hwm = max(cash_hwm, cash)
        realized_net = cash - initial_capital
        if int(row["cash_jpy"]) != cash:
            errors.append(f"valuation_cash_identity:{valuation_id}")
        if int(row["realized_net_pnl_jpy"]) != realized_net:
            errors.append(f"valuation_realized_identity:{valuation_id}")

        if canonical is not None:
            state_hash = str(row["state_event_head_sha256"])
            state = (
                connection.execute(
                    """
                    SELECT event_sha256
                    FROM portfolio_event_log
                    WHERE event_seq = ?
                    """,
                    (state_head_seq,),
                ).fetchone()
                if state_head_seq > 0
                else None
            )
            expected_state_hash = (
                str(state["event_sha256"]) if state is not None else GENESIS_EVENT_SHA256
            )
            if expected_state_hash != state_hash:
                errors.append(f"valuation_state_head_identity:{valuation_id}")
            if (
                int(canonical["event_seq"]) != state_head_seq + 1
                or str(canonical["prev_event_sha256"]) != state_hash
            ):
                errors.append(f"valuation_event_predecessor:{valuation_id}")

        valuation_status = str(row["valuation_status"])
        numeric_names = (
            "mid_equity_jpy",
            "executable_equity_jpy",
            "liquidation_equity_base_jpy",
            "liquidation_equity_low_jpy",
            "liquidation_equity_high_jpy",
            "exit_cost_reserve_base_jpy",
            "exit_cost_reserve_low_jpy",
            "exit_cost_reserve_high_jpy",
            "liquidation_hwm_base_jpy",
            "liquidation_hwm_low_jpy",
            "liquidation_drawdown_base_jpy",
            "liquidation_drawdown_low_jpy",
        )
        if valuation_status != "complete":
            if any(row[name] is not None for name in numeric_names):
                errors.append(f"incomplete_valuation_has_numeric_value:{valuation_id}")
            continue
        if len(positions) != int(row["position_count"]):
            errors.append(f"valuation_position_count:{valuation_id}")
            continue
        expected_mid = cash + aggregate_mid
        expected_executable = cash + aggregate_executable
        expected_base = expected_executable - aggregate_base_reserve
        expected_low = expected_executable - aggregate_low_reserve
        expected_high = expected_executable - aggregate_high_reserve
        expected_values = {
            "mid_equity_jpy": expected_mid,
            "executable_equity_jpy": expected_executable,
            "liquidation_equity_base_jpy": expected_base,
            "liquidation_equity_low_jpy": expected_low,
            "liquidation_equity_high_jpy": expected_high,
            "exit_cost_reserve_base_jpy": aggregate_base_reserve,
            "exit_cost_reserve_low_jpy": aggregate_low_reserve,
            "exit_cost_reserve_high_jpy": aggregate_high_reserve,
        }
        for name, expected in expected_values.items():
            if row[name] is None or int(row[name]) != expected:
                errors.append(f"valuation_{name}_identity:{valuation_id}")
        expected_base_hwm = max(previous_base_hwm or cash_hwm, cash_hwm, expected_base)
        expected_low_hwm = max(previous_low_hwm or cash_hwm, cash_hwm, expected_low)
        hwm_values = {
            "liquidation_hwm_base_jpy": expected_base_hwm,
            "liquidation_hwm_low_jpy": expected_low_hwm,
            "liquidation_drawdown_base_jpy": expected_base_hwm - expected_base,
            "liquidation_drawdown_low_jpy": expected_low_hwm - expected_low,
        }
        for name, expected in hwm_values.items():
            if row[name] is None or int(row[name]) != expected:
                errors.append(f"valuation_{name}_identity:{valuation_id}")
        previous_base_hwm = expected_base_hwm
        previous_low_hwm = expected_low_hwm
        if reserve_policy is not None:
            expected_heat = aggregate_planned_risk + aggregate_gap_buffer + aggregate_low_reserve
            if portfolio_detail.get("planned_stop_loss_jpy") != aggregate_planned_risk:
                errors.append(f"valuation_planned_stop_identity:{valuation_id}")
            if portfolio_detail.get("gap_buffer_jpy") != aggregate_gap_buffer:
                errors.append(f"valuation_gap_buffer_identity:{valuation_id}")
            if portfolio_detail.get("low_exit_reserve_jpy") != aggregate_low_reserve:
                errors.append(f"valuation_low_reserve_detail_identity:{valuation_id}")
            if portfolio_detail.get("portfolio_heat_jpy") != expected_heat:
                errors.append(f"valuation_heat_identity:{valuation_id}")

    head_seq, head_sha = event_head(connection)
    return {
        "contract": "virtual-portfolio-v2-audit",
        "schema_version": V2_SCHEMA_VERSION,
        "passed": not errors,
        "errors": errors,
        "event_count": head_seq,
        "event_head_sha256": head_sha,
        "journal_transaction_count": int(
            connection.execute("SELECT COUNT(*) FROM journal_transactions").fetchone()[0]
        ),
        "legacy_cash_jpy": legacy_cash_jpy,
        "journal_cash_jpy": journal_cash_jpy,
        "cash_parity": legacy_cash_jpy == journal_cash_jpy,
        "valuation_count": int(
            connection.execute("SELECT COUNT(*) FROM portfolio_liquidation_valuations").fetchone()[
                0
            ]
        ),
        "identity_scope": list(V2_AUDIT_IDENTITY_SCOPE),
    }
