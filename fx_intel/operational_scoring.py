"""Close the operational learning loop by scoring matured predictions.

The store already owns every half of this seam: :meth:`OperationalStore.due_predictions`
reads matured pending predictions, :meth:`OperationalStore.price_window` reads a
point-in-time bounded price path, and :meth:`OperationalStore.record_outcome`
appends an immutable outcome. Nothing joined them, so predictions accumulated and
never closed.

This module is that join and nothing more. Scoring itself stays in
``canonical_outcome_pipeline``/``trade_outcome`` so the store path and the offline
path cannot drift into two different definitions of realized net R.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .canonical_outcome_pipeline import build_canonical_trade_request
from .evaluation_labels import NET_LABEL_VERSION
from .operational_store import OperationalStore, OutcomeRecord
from .trade_outcome import CanonicalTradeOutcome, score_canonical_outcome

# A matured prediction still needs the price path that follows it. Reading only up
# to ``due_time`` would truncate the final bar, so the window is widened by one
# horizon and then bounded again by availability inside ``price_window``.
_PATH_LOOKAHEAD = timedelta(hours=1)

SCORED = "scored"
EVALUATION_UNAVAILABLE = "evaluation_unavailable"


@dataclass(frozen=True)
class ScoringReport:
    """Counts for one scoring sweep, mirroring ``CanonicalPipelineReport``."""

    due: int = 0
    scored: int = 0
    unavailable: int = 0
    deferred: int = 0
    inserted: int = 0
    identical_retries: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "due": self.due,
            "scored": self.scored,
            "unavailable": self.unavailable,
            "deferred": self.deferred,
            "inserted": self.inserted,
            "identical_retries": self.identical_retries,
            "reasons": dict(self.reasons),
        }


def score_due_predictions(
    store: OperationalStore,
    *,
    as_of: datetime,
    source_checkpoint: str,
    limit: int = 1_000,
    label_version: str = NET_LABEL_VERSION,
) -> ScoringReport:
    """Score every matured prediction and append its immutable outcome.

    A prediction whose price path has not arrived yet is *deferred*, not written:
    an outcome is immutable, so recording ``evaluation_unavailable`` for a path
    that is merely late would permanently poison the label. Only a path that is
    present but unusable resolves to ``evaluation_unavailable``.
    """

    due = store.due_predictions(as_of=as_of, limit=limit)
    scored = 0
    unavailable = 0
    deferred = 0
    inserted = 0
    identical = 0
    reasons: dict[str, int] = {}

    for row in due:
        event = _event_payload(row)
        if event is None:
            deferred += 1
            _count(reasons, "missing_event_payload")
            continue

        prediction_time = _time(row.get("prediction_time_ns"))
        due_time = _time(row.get("due_time_ns"))
        if prediction_time is None or due_time is None:
            deferred += 1
            _count(reasons, "missing_prediction_window")
            continue

        price_rows = _price_path(
            store,
            symbol=str(row.get("symbol") or ""),
            timeframe=str(row.get("timeframe") or ""),
            start=prediction_time,
            end=due_time,
        )
        if not price_rows:
            # The horizon matured but the path has not been ingested yet.
            deferred += 1
            _count(reasons, "price_path_not_ingested")
            continue

        outcome = score_canonical_outcome(build_canonical_trade_request(event, price_rows))
        status, realized = _resolve_status(outcome)
        if status is None:
            deferred += 1
            _count(reasons, outcome.first_touch or "path_incomplete")
            continue

        record = OutcomeRecord(
            prediction_id=str(row["prediction_id"]),
            label_version=label_version,
            holding_end_time=due_time,
            scored_time=as_of,
            status=status,
            source_checkpoint=source_checkpoint,
            payload=_outcome_payload(outcome),
            realized_net_r=realized,
        )
        if store.record_outcome(record):
            inserted += 1
        else:
            identical += 1
        if status == SCORED:
            scored += 1
        else:
            unavailable += 1
            _count(reasons, outcome.cost_status or "net_label_ineligible")

    return ScoringReport(
        due=len(due),
        scored=scored,
        unavailable=unavailable,
        deferred=deferred,
        inserted=inserted,
        identical_retries=identical,
        reasons=reasons,
    )


def _resolve_status(outcome: CanonicalTradeOutcome) -> tuple[str | None, float | None]:
    """Map a canonical outcome onto the store's outcome status contract.

    ``None`` means "do not write yet" — the path is still resolving, and an
    immutable row would freeze an answer that is not final.
    """

    if outcome.first_touch == "unavailable":
        return None, None
    if outcome.net_label_eligible and outcome.realized_net_r is not None:
        return SCORED, float(outcome.realized_net_r)
    # The path resolved, but cost provenance is missing, so no net label exists.
    # That is a terminal, auditable answer rather than a retryable one.
    return EVALUATION_UNAVAILABLE, None


def _price_path(
    store: OperationalStore,
    *,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, object]]:
    """Read the PIT price path, flattening each stored payload for the scorer."""

    if not symbol or not timeframe:
        return []
    rows = store.price_window(
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end + _PATH_LOOKAHEAD,
    )
    return [_flattened_price_row(row) for row in rows]


def _flattened_price_row(row: Mapping[str, object]) -> dict[str, object]:
    """Return the original ingest row the canonical scorer expects.

    ``price_points`` keeps scalar ``bid``/``ask`` columns, but executable scoring
    needs the eight bid/ask OHLC fields, which survive only inside the immutable
    payload. The payload is returned verbatim: the scorer re-derives
    ``content_hash`` over the whole row, so merging the projected SQL columns
    back in would change the hash and reject every bar as tampered.
    """

    payload = row.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _event_payload(row: Mapping[str, object]) -> Mapping[str, object] | None:
    payload = row.get("payload")
    return payload if isinstance(payload, Mapping) else None


def _outcome_payload(outcome: CanonicalTradeOutcome) -> dict[str, object]:
    """Persist the provenance a later audit needs to re-derive the label."""

    return {
        "decision_id": outcome.decision_id,
        "label_version": outcome.label_version,
        "label_provenance": outcome.label_provenance,
        "symbol": outcome.symbol,
        "timeframe": outcome.timeframe,
        "mode": outcome.mode,
        "direction": outcome.direction,
        "horizon_hours": outcome.horizon_hours,
        "first_touch": outcome.first_touch,
        "first_touch_ts": outcome.first_touch_ts,
        "gross_realized_r": outcome.gross_realized_r,
        "execution_cost_r": outcome.execution_cost_r,
        "realized_net_r": outcome.realized_net_r,
        "cost_model_id": outcome.cost_model_id,
        "cost_model_version": outcome.cost_model_version,
        "cost_status": outcome.cost_status,
        "entry_quote_source": outcome.entry_quote_source,
        "spread_source": outcome.spread_source,
        "net_label_eligible": outcome.net_label_eligible,
        "path_quality": outcome.path_quality,
        "path_source": outcome.path_source,
        "quality_flags": list(outcome.quality_flags),
        "cost_quality_flags": list(outcome.cost_quality_flags),
    }


def _time(value: object) -> datetime | None:
    if not isinstance(value, int):
        return None
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


def _count(reasons: dict[str, int], key: str) -> None:
    reasons[key] = reasons.get(key, 0) + 1


__all__: Sequence[str] = (
    "EVALUATION_UNAVAILABLE",
    "SCORED",
    "ScoringReport",
    "score_due_predictions",
)
