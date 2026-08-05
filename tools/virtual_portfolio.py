#!/usr/bin/env python3
"""Operate the analysis-only virtual FX portfolio.

The CLI records offline simulated decisions and fills only.  It intentionally
contains no broker URL, credentials, account identifier, or order API.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.virtual_portfolio import (  # noqa: E402
    QuoteSnapshot,
    TradeCloseRequest,
    VirtualPortfolioPolicy,
    open_virtual_portfolio_writer,
    portfolio_snapshot,
)
from fx_intel.market import is_market_open  # noqa: E402
from fx_intel.virtual_portfolio_replay import run_replay_cycle  # noqa: E402
from fx_intel.virtual_portfolio_valuation import (  # noqa: E402
    append_synchronized_valuation_from_marks,
    evaluate_shadow_risk_gate_v2,
)

DEFAULT_DB = REPO_ROOT / "logs" / "fx_virtual_portfolio.sqlite3"
DEFAULT_DECISIONS = REPO_ROOT / "logs" / "briefing_decisions_latest.json"
DEFAULT_CLOSES = REPO_ROOT / "logs" / "virtual_trade_closes_latest.json"
DEFAULT_QUOTES = REPO_ROOT / "collect" / "log" / "quotes.jsonl"
DEFAULT_AUDIT_RUN_ROOT = REPO_ROOT / "runs" / "virtual_portfolio"
TOKYO = ZoneInfo("Asia/Tokyo")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"JSON file does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON file: {path}: {error}") from error


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_cycle_audit(run_root: Path, payload: Mapping[str, object]) -> Path:
    """Create an immutable audit artifact for one writer cycle."""

    generated = datetime.now(UTC)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    session_date = generated.astimezone(TOKYO).date().isoformat()
    run_id = f"{generated.strftime('%Y%m%dT%H%M%S%fZ')}-{digest[:12]}"
    run_dir = run_root.resolve() / session_date / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    target = run_dir / "cycle.json"
    envelope = {
        "contract": "virtual-portfolio-cycle-audit-v1",
        "generated_at": generated.isoformat(),
        "payload_sha256": digest,
        "payload": dict(payload),
    }
    target.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def _parse_aware(value: object, name: str) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return moment.astimezone(UTC)


def _parse_cli_aware(value: str) -> datetime:
    return _parse_aware(value, "timestamp")


def _quote_from_mapping(payload: Mapping[str, object]) -> QuoteSnapshot:
    return QuoteSnapshot(
        source_record_id=str(payload.get("source_record_id") or ""),
        source=str(payload.get("source") or ""),
        revision_id=str(payload.get("revision_id") or ""),
        event_time=_parse_aware(payload.get("event_time"), "quote.event_time"),
        available_time=_parse_aware(payload.get("available_time"), "quote.available_time"),
        ingested_time=_parse_aware(payload.get("ingested_time"), "quote.ingested_time"),
        bid=float(str(payload.get("bid") or 0)),
        ask=float(str(payload.get("ask") or 0)),
    )


def _canonical_decision(event: Mapping[str, object]) -> dict[str, object]:
    """Flatten one briefing event without inventing missing provenance."""

    nested = event.get("decision")
    decision = dict(nested) if isinstance(nested, Mapping) else {}
    merged = {
        **decision,
        **{
            key: event[key]
            for key in (
                "decision_id",
                "ts",
                "prediction_time",
                "source_cutoff",
                "max_feature_available_time",
                "input_context_id",
                "source_record_ids",
                "pit_eligible",
                "pit_contract",
                "producer",
                "producer_version",
                "mode",
                "symbol",
                "timeframe",
            )
            if key in event
        },
    }
    if "features" not in merged and isinstance(decision.get("features"), Mapping):
        merged["features"] = dict(decision["features"])
    if "components" not in merged and isinstance(decision.get("components"), Mapping):
        merged["components"] = dict(decision["components"])
    return merged


def _latest_events(path: Path) -> tuple[list[dict[str, object]], str]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise ValueError("decision file must contain an object")
    rows = payload.get("events")
    if not isinstance(rows, list):
        raise ValueError("decision file must contain an events list")
    events = [dict(row) for row in rows if isinstance(row, Mapping)]
    identity = {
        "generated_at": payload.get("generated_at"),
        "run_ids": payload.get("run_ids"),
        "decision_ids": [str(row.get("decision_id") or "") for row in events],
    }
    batch_hash = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return events, f"decision-batch-sha256:{batch_hash}"


def _execution_quote(
    event: Mapping[str, object],
    decision: Mapping[str, object],
) -> tuple[QuoteSnapshot | None, float | None, str | None]:
    execution = decision.get("execution")
    nested_quote = execution.get("quote") if isinstance(execution, Mapping) else None
    candidates = (
        event.get("execution_quote"),
        decision.get("execution_quote"),
        nested_quote,
    )
    payload = next((value for value in candidates if isinstance(value, Mapping)), None)
    if payload is None:
        return None, None, None
    try:
        quote = _quote_from_mapping(payload)
        rate_raw = payload.get("quote_to_jpy_rate")
        rate = float(str(rate_raw)) if rate_raw is not None else None
        source = str(payload.get("quote_conversion_source") or "") or None
    except (TypeError, ValueError):
        return None, None, None
    return quote, rate, source


def _trade_close_request(payload: Mapping[str, object]) -> TradeCloseRequest:
    quote_payload = payload.get("quote")
    if not isinstance(quote_payload, Mapping):
        raise ValueError("close request requires a quote object")
    replay_evidence_raw = payload.get("replay_evidence")
    return TradeCloseRequest(
        trade_id=str(payload.get("trade_id") or ""),
        close_time=_parse_aware(payload.get("close_time"), "close_time"),
        quote=_quote_from_mapping(quote_payload),
        quote_to_jpy_rate=float(str(payload.get("quote_to_jpy_rate") or 0)),
        quote_conversion_source=str(payload.get("quote_conversion_source") or ""),
        close_reason=str(payload.get("close_reason") or ""),
        cost_model_id=str(payload.get("cost_model_id") or ""),
        cost_model_version=str(payload.get("cost_model_version") or ""),
        cost_source=str(payload.get("cost_source") or ""),
        costs_complete=payload.get("costs_complete") is True,
        slippage_cost_jpy=int(str(payload.get("slippage_cost_jpy") or 0)),
        commission_jpy=int(str(payload.get("commission_jpy") or 0)),
        financing_jpy=int(str(payload.get("financing_jpy") or 0)),
        conversion_cost_jpy=int(str(payload.get("conversion_cost_jpy") or 0)),
        maximum_favorable_excursion_r=(
            float(str(payload["maximum_favorable_excursion_r"]))
            if payload.get("maximum_favorable_excursion_r") is not None
            else None
        ),
        maximum_adverse_excursion_r=(
            float(str(payload["maximum_adverse_excursion_r"]))
            if payload.get("maximum_adverse_excursion_r") is not None
            else None
        ),
        historical_replay=payload.get("historical_replay") is True,
        observed_at=(
            _parse_aware(payload["observed_at"], "observed_at")
            if payload.get("observed_at") is not None
            else None
        ),
        replay_evidence=(
            dict(replay_evidence_raw) if isinstance(replay_evidence_raw, Mapping) else None
        ),
    )


def _close_requests(path: Path) -> list[TradeCloseRequest]:
    if not path.is_file():
        return []
    payload = _read_json(path)
    raw_rows = payload.get("closes") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_rows, list):
        raise ValueError("close file must be a list or contain a closes list")
    return [_trade_close_request(row) for row in raw_rows if isinstance(row, Mapping)]


def _initialize(db: Path, writer_id: str, now: datetime) -> bool:
    with open_virtual_portfolio_writer(db, writer_id=writer_id) as store:
        return store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=now)


def _set_five_x_limits(db: Path, writer_id: str) -> dict[str, object]:
    changed_at = datetime.now(UTC)
    with open_virtual_portfolio_writer(db, writer_id=writer_id) as store:
        initialized = store.initialize_portfolio(
            policy=VirtualPortfolioPolicy(),
            created_at=changed_at - timedelta(microseconds=1),
        )
        before = store.policy(as_of=changed_at, known_at=changed_at)
        transition = store.activate_five_x_limits(changed_at=changed_at)
        after = store.policy(as_of=changed_at, known_at=changed_at)
        return {
            "ok": True,
            "mode": "offline_simulation",
            "broker_connected": False,
            "initialized": initialized,
            "changed_at": changed_at.isoformat(),
            "before": asdict(before),
            "transition": transition,
            "after": asdict(after),
        }


def _cycle(args: argparse.Namespace) -> dict[str, object]:
    now = datetime.now(UTC)
    events, batch_id = _latest_events(args.decisions)
    close_requests = _close_requests(args.closes)
    market_open = is_market_open(now)
    inserted = 0
    opened = 0
    abstained = 0
    closed = 0
    queued = 0
    conflicts: list[dict[str, str]] = []
    with open_virtual_portfolio_writer(args.db, writer_id=args.writer_id) as store:
        store.initialize_portfolio(policy=VirtualPortfolioPolicy(), created_at=now)
        session_date = now.astimezone(TOKYO).date().isoformat()
        store.record_session_event(
            session_date_jst=session_date,
            event_type="opened",
            event_time=now,
            detail={
                "source": str(args.decisions.resolve()),
                "event_count": len(events),
                "market_open": market_open,
                "mode": "offline_simulation",
            },
        )
        existing_pending_close = store.pending_session_close_request(as_of=now)
        requested_now = bool(args.close_session or (not market_open and store.open_trade_details()))
        if (
            requested_now
            and existing_pending_close is None
            and store.session_close_request(session_date_jst=session_date) is None
        ):
            store.record_session_close_request(
                session_date_jst=session_date,
                requested_at=now,
                cutoff_time=now,
                detail={
                    "source": (
                        "scheduled_session_close"
                        if args.close_session
                        else "market_closed_safety_close"
                    ),
                    "market_open": market_open,
                    "mode": "offline_simulation",
                    "broker_connected": False,
                },
            )
        for request in close_requests:
            try:
                close_result = store.close_trade(request)
            except Exception as error:  # preserve the rest of the immutable batch
                conflicts.append(
                    {
                        "trade_id": request.trade_id or "unknown",
                        "error": str(error),
                    }
                )
                continue
            if close_result["inserted"]:
                closed += 1
        for event in events:
            decision = _canonical_decision(event)
            try:
                queue_result = store.queue_decision(
                    decision,
                    observed_at=now,
                    source=str(args.decisions.resolve()),
                    batch_id=batch_id,
                )
            except Exception as error:  # preserve the rest of the immutable batch
                conflicts.append(
                    {
                        "decision_id": str(decision.get("decision_id") or "unknown"),
                        "error": str(error),
                    }
                )
                continue
            if queue_result["inserted"]:
                queued += 1
        try:
            replay = run_replay_cycle(
                store,
                quote_log=args.quotes,
                quote_index=args.quote_index,
                as_of=now,
            )
        except Exception as error:
            replay = {
                "status": "failed_closed",
                "opened": 0,
                "abstained": 0,
                "closed": 0,
                "waiting": len(store.pending_decisions()),
                "conflicts": [{"record_id": "replay", "error": str(error)}],
            }
        inserted += int(str(replay.get("opened", 0))) + int(str(replay.get("abstained", 0)))
        opened += int(str(replay.get("opened", 0)))
        abstained += int(str(replay.get("abstained", 0)))
        closed += int(str(replay.get("closed", 0)))
        replay_conflicts = replay.get("conflicts")
        if not isinstance(replay_conflicts, list):
            replay_conflicts = []
        conflicts.extend(
            {
                "decision_id": str(row.get("record_id") or row.get("trade_id") or "replay"),
                "error": str(row.get("error") or "unknown replay error"),
            }
            for row in replay_conflicts
            if isinstance(row, Mapping)
        )
        snapshot = store.snapshot(now=now)
        pending_close = store.pending_session_close_request(as_of=now)
        session_request = store.session_close_request(session_date_jst=session_date)
        lifecycle = snapshot.get("session_close_lifecycle")
        lifecycle_status = (
            str(lifecycle.get("status") or "") if isinstance(lifecycle, Mapping) else ""
        )
        if lifecycle_status.startswith("inconsistent"):
            session_close_status = "failed_closed_inconsistent_request_state"
            conflicts.append(
                {
                    "decision_id": str(
                        lifecycle.get("request", {}).get("request_id", "session-close")
                        if isinstance(lifecycle, Mapping)
                        and isinstance(lifecycle.get("request"), Mapping)
                        else "session-close"
                    ),
                    "error": (
                        "session close completion exists while same-session "
                        "simulated positions remain open"
                    ),
                }
            )
        elif pending_close is not None and int(snapshot["open_position_count"]) > 0:
            session_close_status = "deferred_waiting_for_executable_quote"
        elif pending_close is not None and (
            not isinstance(snapshot.get("reconciliation"), Mapping)
            or snapshot["reconciliation"].get("passed") is not True
        ):
            session_close_status = "failed_closed_reconciliation"
            conflicts.append(
                {
                    "decision_id": str(pending_close["request_id"]),
                    "error": (
                        "session close request remains pending because canonical "
                        "ledger reconciliation failed"
                    ),
                }
            )
        elif pending_close is not None:
            completed_at = datetime.now(UTC)
            completion_detail = {
                "inserted_decisions": inserted,
                "queued_intakes": queued,
                "opened": opened,
                "abstained": abstained,
                "closed": closed,
                "market_open": market_open,
                "conflict_count": len(conflicts),
                "reconciliation": snapshot["reconciliation"],
                "open_positions_remaining": 0,
            }
            store.record_session_close_completion(
                request_id=str(pending_close["request_id"]),
                completed_at=completed_at,
                detail=completion_detail,
            )
            # Compatibility projection only. Request-scoped completion above is
            # authoritative; an older session event must never satisfy a later request.
            store.record_session_event(
                session_date_jst=str(pending_close["session_date_jst"]),
                event_type="closed",
                event_time=completed_at,
                detail={
                    **completion_detail,
                    "session_close_request": pending_close,
                },
            )
            session_close_status = "closed_reconciled"
            snapshot = store.snapshot(now=completed_at)
        elif session_request is None:
            session_close_status = "not_requested"
        else:
            completion = store.session_close_completion(
                request_id=str(session_request["request_id"])
            )
            if completion is not None:
                session_close_status = "closed_reconciled"
            else:
                session_close_status = "failed_closed_inconsistent_request_state"
                conflicts.append(
                    {
                        "decision_id": str(session_request["request_id"]),
                        "error": (
                            "session close request exists but is neither pending " "nor completed"
                        ),
                    }
                )
        session_close_request = pending_close or session_request
    result: dict[str, object] = {
        "ok": not conflicts,
        "mode": "offline_simulation",
        "broker_connected": False,
        "market_open": market_open,
        "source_events": len(events),
        "queued_intakes": queued,
        "close_requests": len(close_requests),
        "inserted_decisions": inserted,
        "opened": opened,
        "abstained": abstained,
        "closed": closed,
        "session_close_status": session_close_status,
        "session_close_request": session_close_request,
        "replay": replay,
        "conflicts": conflicts,
        "portfolio": snapshot,
    }
    if not args.no_audit_artifact:
        result["audit_artifact"] = str(_write_cycle_audit(args.audit_run_root, result))
    return result


def _close(args: argparse.Namespace) -> dict[str, object]:
    payload = _read_json(args.request_json)
    if not isinstance(payload, Mapping):
        raise ValueError("close request must be a JSON object")
    request = _trade_close_request(payload)
    with open_virtual_portfolio_writer(args.db, writer_id=args.writer_id) as store:
        result = store.close_trade(request)
        result["portfolio"] = store.snapshot()
    return result


def _export(args: argparse.Namespace) -> dict[str, object]:
    with open_virtual_portfolio_writer(args.db, writer_id=args.writer_id) as store:
        payload = store.export_learning_rows(record_export=True)
    if args.output:
        _write_json(args.output.resolve(), payload)
    return {
        **{key: value for key, value in payload.items() if key != "rows"},
        "output": str(args.output.resolve()) if args.output else None,
    }


def _value(args: argparse.Namespace) -> dict[str, object]:
    cutoff = args.cutoff or datetime.now(UTC)
    observed_at = args.observed_at or cutoff
    with open_virtual_portfolio_writer(args.db, writer_id=args.writer_id) as store:
        valuation = append_synchronized_valuation_from_marks(
            store,
            valuation_cutoff=cutoff,
            observed_at=observed_at,
        )
        risk = evaluate_shadow_risk_gate_v2(
            store,
            as_of=observed_at,
            known_at=datetime.now(UTC),
        )
        audit = store.audit_virtual_portfolio_v2()
    return {
        "valuation": valuation,
        "risk_gate_v2_shadow": risk,
        "audit": audit,
        "broker_connected": False,
        "execution_mode": "offline_simulation",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analysis-only, append-only virtual FX portfolio")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--writer-id", default="virtual-portfolio-cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the fixed JPY 1,000,000 portfolio")
    commands.add_parser(
        "set-five-x-limits",
        help="append the approved five-times loss, drawdown, and capacity limits",
    )
    cycle = commands.add_parser("cycle", help="record the latest decision batch fail-closed")
    cycle.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    cycle.add_argument("--closes", type=Path, default=DEFAULT_CLOSES)
    cycle.add_argument(
        "--quotes",
        type=Path,
        default=DEFAULT_QUOTES,
        help="append-only Dukascopy historical bid/ask JSONL",
    )
    cycle.add_argument(
        "--quote-index",
        type=Path,
        help="incremental SQLite byte-offset index for the quote JSONL",
    )
    cycle.add_argument("--close-session", action="store_true")
    cycle.add_argument(
        "--audit-run-root",
        type=Path,
        default=DEFAULT_AUDIT_RUN_ROOT,
        help="create-only directory for immutable cycle audit artifacts",
    )
    cycle.add_argument(
        "--no-audit-artifact",
        action="store_true",
        help="skip artifact creation for isolated functional tests only",
    )
    close = commands.add_parser("close", help="append one simulated close from validated JSON")
    close.add_argument("--request-json", type=Path, required=True)
    commands.add_parser("summary", help="print the canonical read-only snapshot")
    value = commands.add_parser(
        "value",
        help="append one synchronized liquidation valuation from persisted marks",
    )
    value.add_argument("--cutoff", type=_parse_cli_aware)
    value.add_argument("--observed-at", type=_parse_cli_aware)
    export = commands.add_parser(
        "export-learning",
        help="export mature PIT rows for offline challenger validation",
    )
    export.add_argument("--output", type=Path)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        if args.command == "init":
            now = datetime.now(UTC)
            result: object = {
                "initialized": _initialize(args.db, args.writer_id, now),
                "portfolio": portfolio_snapshot(args.db, now=now),
            }
        elif args.command == "set-five-x-limits":
            result = _set_five_x_limits(args.db, args.writer_id)
        elif args.command == "cycle":
            result = _cycle(args)
        elif args.command == "close":
            result = _close(args)
        elif args.command == "export-learning":
            result = _export(args)
        elif args.command == "value":
            result = _value(args)
        else:
            result = portfolio_snapshot(args.db)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if args.command == "cycle" and (
        not isinstance(result, Mapping) or result.get("ok") is not True
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
