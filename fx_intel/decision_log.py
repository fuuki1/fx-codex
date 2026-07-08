"""Complete audit log for chart-analysis decisions.

The lightweight journals in journal.py are optimized for scoring.  This module
keeps the fuller audit trail: the final decision, the market inputs, the
technical snapshot, applied learning context, TP/SL levels, and maximization
cells that can explain why confidence was boosted, dampened, or blocked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path

from .timeframe import PRIMARY_HORIZON_HOURS, tolerance_for
from .trade_outcome import TradeOutcome, evaluate_trade_outcomes, json_safe, summarize_expectancy

SCHEMA_VERSION = 1
EVENT_TYPE = "chart_decision"
SCORING_METHOD = "tp_sl_mfe_mae_first_touch"
SCORING_METRICS = (
    "first_touch",
    "realized_r",
    "mfe_r",
    "mae_r",
    "tp1_hit",
    "tp2_hit",
    "sl_hit",
    "path_quality",
)


def build_timeframe_decision_events(
    plans_by_symbol: Mapping[str, Sequence[object]],
    *,
    now: datetime,
    analysis: object,
    tech_map: Mapping[str, object],
    news_items: Sequence[object] = (),
    events_48h: Sequence[object] = (),
    fetch_warnings: Sequence[str] = (),
    calendar_ok: bool = True,
    timeframe_learning: object | None = None,
    tp_sl_learning: object | None = None,
    maximization_profile: object | None = None,
    expectancy_summaries: Mapping[str, Mapping[str, object]] | None = None,
    source: str = "fx_briefing",
) -> list[dict[str, object]]:
    """Build append-only audit events for --per-timeframe decisions."""

    now = _utc(now)
    run_id = _run_id(now, "per_timeframe")
    market_context = _market_context(
        analysis,
        news_items=news_items,
        events_48h=events_48h,
        fetch_warnings=fetch_warnings,
        calendar_ok=calendar_ok,
    )
    events: list[dict[str, object]] = []
    for symbol, plans in plans_by_symbol.items():
        technical_context = _technical_context(tech_map.get(symbol))
        for index, plan in enumerate(plans):
            timeframe = str(getattr(plan, "timeframe", ""))
            direction = str(getattr(plan, "direction", ""))
            event = {
                "schema": SCHEMA_VERSION,
                "event_type": EVENT_TYPE,
                "decision_id": _decision_id(
                    run_id,
                    "per_timeframe",
                    str(symbol),
                    timeframe,
                    direction,
                    index,
                ),
                "run_id": run_id,
                "ts": now.isoformat(),
                "source": source,
                "mode": "per_timeframe",
                "symbol": str(symbol),
                "timeframe": timeframe,
                "horizon_hours": _number_or_none(getattr(plan, "horizon_hours", None)),
                "decision": _timeframe_plan_snapshot(plan),
                "market_context": market_context,
                "technical_context": technical_context,
                "learning_context": _timeframe_learning_context(
                    str(symbol),
                    timeframe,
                    direction,
                    timeframe_learning=timeframe_learning,
                    tp_sl_learning=tp_sl_learning,
                    maximization_profile=maximization_profile,
                    expectancy_summaries=expectancy_summaries or {},
                ),
                "audit": _audit_flags(plan),
            }
            events.append(_json_ready_dict(event))
    return events


def build_fusion_decision_events(
    plans: Sequence[object],
    *,
    now: datetime,
    analysis: object,
    tech_map: Mapping[str, object],
    news_items: Sequence[object] = (),
    events_48h: Sequence[object] = (),
    fetch_warnings: Sequence[str] = (),
    calendar_ok: bool = True,
    learning_profile: object | None = None,
    trade_expectancy_summary: Mapping[str, object] | None = None,
    ml_artifact: object | None = None,
    promotion_state: object | None = None,
    source: str = "fx_briefing",
) -> list[dict[str, object]]:
    """Build append-only audit events for the legacy one-decision-per-symbol path."""

    now = _utc(now)
    run_id = _run_id(now, "fusion")
    market_context = _market_context(
        analysis,
        news_items=news_items,
        events_48h=events_48h,
        fetch_warnings=fetch_warnings,
        calendar_ok=calendar_ok,
    )
    events: list[dict[str, object]] = []
    for index, plan in enumerate(plans):
        symbol = str(getattr(plan, "symbol", ""))
        direction = str(getattr(plan, "direction", ""))
        event = {
            "schema": SCHEMA_VERSION,
            "event_type": EVENT_TYPE,
            "decision_id": _decision_id(run_id, "fusion", symbol, "fusion", direction, index),
            "run_id": run_id,
            "ts": now.isoformat(),
            "source": source,
            "mode": "fusion",
            "symbol": symbol,
            "timeframe": "fusion",
            "horizon_hours": 24.0,
            "decision": _fusion_plan_snapshot(plan),
            "market_context": market_context,
            "technical_context": _technical_context(tech_map.get(symbol)),
            "learning_context": {
                "directional_learning": _learned_profile_snapshot(learning_profile, symbol),
                "trade_expectancy_summary": trade_expectancy_summary,
                "ml": _ml_artifact_snapshot(ml_artifact),
                "promotion": _promotion_snapshot(promotion_state),
            },
            "audit": _audit_flags(plan),
        }
        events.append(_json_ready_dict(event))
    return events


def append_decision_events(path: str | Path, events: Iterable[Mapping[str, object]]) -> None:
    """Append audit events as JSONL.  One line is one immutable decision event."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(_json_ready(event), ensure_ascii=False, allow_nan=False) + "\n")


def save_latest_snapshot(
    path: str | Path,
    events: Sequence[Mapping[str, object]],
    *,
    now: datetime | None = None,
) -> None:
    """Save the latest full decision batch for dashboards and quick inspection."""

    generated_at = _utc(now or datetime.now(UTC))
    action_counts: dict[str, int] = {}
    for event in events:
        decision = event.get("decision")
        direction = ""
        if isinstance(decision, Mapping):
            direction = str(decision.get("direction", ""))
        action_counts[direction] = action_counts.get(direction, 0) + 1
    payload = {
        "schema": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "event_count": len(events),
        "run_ids": sorted(
            {str(event.get("run_id", "")) for event in events if event.get("run_id")}
        ),
        "action_counts": dict(sorted(action_counts.items())),
        "events": list(events),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(_json_ready(payload), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def read_decision_events(path: str | Path):
    """Read append-only decision events.  Corrupt lines are skipped."""

    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def decision_event_to_scoring_entry(event: Mapping[str, object]) -> dict[str, object] | None:
    """Flatten one complete decision event into trade_outcome's scoring schema."""

    decision = event.get("decision")
    if not isinstance(decision, Mapping):
        return None
    ts = str(event.get("ts", "")).strip()
    symbol = str(event.get("symbol", decision.get("symbol", ""))).upper()
    if not ts or not symbol:
        return None
    timeframe = str(event.get("timeframe", decision.get("timeframe", "fusion")) or "fusion")
    mode = str(event.get("mode", "fusion") or "fusion")
    horizon = _number_or_none(event.get("horizon_hours"))
    if horizon is None:
        horizon = _number_or_none(decision.get("horizon_hours"))
    if horizon is None:
        horizon = _horizon_for_timeframe(timeframe)
    entry = {
        "ts": ts,
        "symbol": symbol,
        "mode": mode,
        "timeframe": timeframe,
        "horizon_hours": horizon,
        "decision_id": event.get("decision_id"),
        "run_id": event.get("run_id"),
        "direction": decision.get("direction"),
        "conviction": decision.get("conviction"),
        "composite": decision.get("composite"),
        "tech_score": decision.get("tf_score", decision.get("tech_score")),
        "news_score": decision.get("news_score"),
        "close": decision.get("close"),
        "atr": decision.get("atr"),
        "stop": decision.get("stop"),
        "target1": decision.get("target1"),
        "target2": decision.get("target2"),
        "target_policy": decision.get("target_policy", {}),
        "data_quality": decision.get("data_quality"),
        "features": decision.get("features", {}),
        "components": decision.get("components", []),
    }
    return _json_ready_dict(entry)


def score_decision_events(
    events: Iterable[Mapping[str, object]],
    *,
    price_entries: Iterable[Mapping[str, object]] = (),
    now: datetime | None = None,
) -> dict[str, object]:
    """Score complete decision logs by TP/SL first-touch, MFE, MAE, and realized R."""

    generated_at = _utc(now or datetime.now(UTC))
    event_entries = [
        entry for event in events if (entry := decision_event_to_scoring_entry(event)) is not None
    ]
    normalized_prices = [_normalize_price_entry(row) for row in price_entries]
    all_entries = event_entries + normalized_prices
    contexts = sorted(
        {
            (
                str(entry.get("mode", "fusion") or "fusion"),
                str(entry.get("timeframe", "fusion") or "fusion"),
                _entry_horizon(entry),
            )
            for entry in event_entries
            if str(entry.get("direction", "")) in ("long", "short")
        },
        key=lambda item: (item[0], item[1], item[2]),
    )

    all_outcomes: list[TradeOutcome] = []
    enriched_outcomes: list[dict[str, object]] = []
    by_timeframe: dict[str, dict[str, object]] = {}
    by_mode: dict[str, list[TradeOutcome]] = {}
    meta_by_key = {
        _meta_key(entry): entry
        for entry in event_entries
        if str(entry.get("direction", "")) in ("long", "short")
    }

    for mode, timeframe, horizon in contexts:
        group_entries = [
            entry
            for entry in all_entries
            if _entry_matches_context(entry, mode=mode, timeframe=timeframe)
        ]
        outcomes = evaluate_trade_outcomes(
            group_entries,
            horizon_hours=horizon,
            tolerance_hours=tolerance_for(horizon),
        )
        all_outcomes.extend(outcomes)
        by_mode.setdefault(mode, []).extend(outcomes)
        by_timeframe[f"{mode}|{timeframe}"] = summarize_expectancy(outcomes)
        for outcome in outcomes:
            meta = meta_by_key.get(
                (outcome.symbol, outcome.direction, outcome.ts, timeframe, mode), {}
            )
            outcome_dict = outcome.to_dict()
            outcome_dict.update(
                {
                    "decision_id": meta.get("decision_id"),
                    "run_id": meta.get("run_id"),
                    "mode": mode,
                    "timeframe": timeframe,
                    "score_method": SCORING_METHOD,
                    "score_label": _score_label(outcome),
                    "score_hit": outcome.realized_r is not None and outcome.realized_r > 0,
                }
            )
            enriched_outcomes.append(outcome_dict)

    return _json_ready_dict(
        {
            "schema": SCHEMA_VERSION,
            "generated_at": generated_at.isoformat(),
            "scoring_method": SCORING_METHOD,
            "metrics": list(SCORING_METRICS),
            "decision_events": len(event_entries),
            "scored_outcomes": len(enriched_outcomes),
            "summary": summarize_expectancy(all_outcomes),
            "by_timeframe": by_timeframe,
            "by_mode": {
                mode: summarize_expectancy(outcomes) for mode, outcomes in sorted(by_mode.items())
            },
            "outcomes": enriched_outcomes,
        }
    )


def save_outcome_report(report: Mapping[str, object], path: str | Path) -> None:
    """Save the TP/SL/MFE/MAE scoring report for the complete decision log."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(_json_ready(report), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _timeframe_plan_snapshot(plan: object) -> dict[str, object]:
    return {
        "symbol": getattr(plan, "symbol", ""),
        "timeframe": getattr(plan, "timeframe", ""),
        "horizon_hours": _number_or_none(getattr(plan, "horizon_hours", None)),
        "direction": getattr(plan, "direction", ""),
        "direction_ja": getattr(plan, "direction_ja", ""),
        "conviction": _int_or_none(getattr(plan, "conviction", None)),
        "tf_score": _number_or_none(getattr(plan, "tf_score", None)),
        "news_score": _number_or_none(getattr(plan, "news_score", None)),
        "composite": _number_or_none(getattr(plan, "composite", None)),
        "close": _number_or_none(getattr(plan, "close", None)),
        "atr": _number_or_none(getattr(plan, "atr", None)),
        "rsi": _number_or_none(getattr(plan, "rsi", None)),
        "adx": _number_or_none(getattr(plan, "adx", None)),
        "stop": _number_or_none(getattr(plan, "stop", None)),
        "target1": _number_or_none(getattr(plan, "target1", None)),
        "target2": _number_or_none(getattr(plan, "target2", None)),
        "risk_pct": _number_or_none(getattr(plan, "risk_pct", None)),
        "data_quality": _number_or_none(getattr(plan, "data_quality", None)),
        "tech_weight": _number_or_none(getattr(plan, "tech_weight", None)),
        "news_weight": _number_or_none(getattr(plan, "news_weight", None)),
        "features": dict(getattr(plan, "features", {}) or {}),
        "components": list(getattr(plan, "components", []) or []),
        "reason": getattr(plan, "reason", ""),
        "warnings": list(getattr(plan, "warnings", []) or []),
        "target_policy": dict(getattr(plan, "target_policy", {}) or {}),
        "auxiliary_horizons": list(getattr(plan, "auxiliary_horizons", ()) or ()),
    }


def _fusion_plan_snapshot(plan: object) -> dict[str, object]:
    return {
        "symbol": getattr(plan, "symbol", ""),
        "direction": getattr(plan, "direction", ""),
        "direction_ja": getattr(plan, "direction_ja", ""),
        "conviction": _int_or_none(getattr(plan, "conviction", None)),
        "composite": _number_or_none(getattr(plan, "composite", None)),
        "tech_score": _number_or_none(getattr(plan, "tech_score", None)),
        "news_score": _number_or_none(getattr(plan, "news_score", None)),
        "close": _number_or_none(getattr(plan, "close", None)),
        "atr": _number_or_none(getattr(plan, "atr", None)),
        "stop": _number_or_none(getattr(plan, "stop", None)),
        "target1": _number_or_none(getattr(plan, "target1", None)),
        "target2": _number_or_none(getattr(plan, "target2", None)),
        "risk_pct": _number_or_none(getattr(plan, "risk_pct", None)),
        "data_quality": _number_or_none(getattr(plan, "data_quality", None)),
        "tech_weight": _number_or_none(getattr(plan, "tech_weight", None)),
        "news_weight": _number_or_none(getattr(plan, "news_weight", None)),
        "features": dict(getattr(plan, "features", {}) or {}),
        "components": list(getattr(plan, "components", []) or []),
        "committee_notes": list(getattr(plan, "committee_notes", []) or []),
        "warnings": list(getattr(plan, "warnings", []) or []),
        "headlines": [_news_item(item) for item in list(getattr(plan, "headlines", []) or [])],
        "interval_summary": getattr(plan, "interval_summary", ""),
        "ma_note": getattr(plan, "ma_note", ""),
        "target_policy": dict(getattr(plan, "target_policy", {}) or {}),
    }


def _timeframe_learning_context(
    symbol: str,
    timeframe: str,
    direction: str,
    *,
    timeframe_learning: object | None,
    tp_sl_learning: object | None,
    maximization_profile: object | None,
    expectancy_summaries: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    max_cells: dict[str, object] = {}
    if maximization_profile is not None and hasattr(maximization_profile, "cell_for"):
        for cell_direction in ("long", "short"):
            cell = maximization_profile.cell_for(symbol, timeframe, cell_direction)
            max_cells[cell_direction] = cell.to_dict() if cell is not None else None
    active_cell = max_cells.get(direction) if direction in max_cells else None
    tp_sl_profile = None
    if tp_sl_learning is not None and hasattr(tp_sl_learning, "profile_for"):
        profile = tp_sl_learning.profile_for(symbol, timeframe)
        tp_sl_profile = profile.to_dict() if profile is not None else None
    learned_profile = None
    if timeframe_learning is not None and hasattr(timeframe_learning, "profile_for"):
        learned_profile = _learned_profile_snapshot(
            timeframe_learning.profile_for(symbol, timeframe),
            symbol,
        )
    return {
        "timeframe_learning": learned_profile,
        "tp_sl_learning": tp_sl_profile,
        "maximization": {
            "active_cell": active_cell,
            "direction_cells": max_cells,
        },
        "timeframe_expectancy_summary": expectancy_summaries.get(timeframe),
    }


def _learned_profile_snapshot(
    profile: object | None, symbol: str | None = None
) -> dict[str, object] | None:
    if profile is None:
        return None
    bins = []
    for item in list(getattr(profile, "bins", []) or []):
        bins.append(
            {
                "low": getattr(item, "low", None),
                "high": getattr(item, "high", None),
                "evaluated": getattr(item, "evaluated", None),
                "hits": getattr(item, "hits", None),
                "hit_rate": getattr(item, "hit_rate", None),
            }
        )
    symbol_factor = None
    if symbol and hasattr(profile, "conviction_factor"):
        symbol_factor = profile.conviction_factor(symbol)
    return {
        "generated_at": getattr(profile, "generated_at", ""),
        "evaluated": getattr(profile, "evaluated", 0),
        "hits": getattr(profile, "hits", 0),
        "hit_rate": getattr(profile, "hit_rate", None),
        "flat": getattr(profile, "flat", 0),
        "tech_weight": getattr(profile, "tech_weight", None),
        "news_weight": getattr(profile, "news_weight", None),
        "tech_hit_rate": getattr(profile, "tech_hit_rate", None),
        "news_hit_rate": getattr(profile, "news_hit_rate", None),
        "conviction_brier": getattr(profile, "conviction_brier", None),
        "conviction_brier_base": getattr(profile, "conviction_brier_base", None),
        "bins": bins,
        "symbol_stats": dict(getattr(profile, "symbol_stats", {}) or {}),
        "symbol_factors": dict(getattr(profile, "symbol_factors", {}) or {}),
        "symbol_factor": symbol_factor,
        "condition_stats": dict(getattr(profile, "condition_stats", {}) or {}),
        "condition_factors": dict(getattr(profile, "condition_factors", {}) or {}),
        "notes_ja": list(getattr(profile, "notes_ja", []) or []),
    }


def _market_context(
    analysis: object,
    *,
    news_items: Sequence[object],
    events_48h: Sequence[object],
    fetch_warnings: Sequence[str],
    calendar_ok: bool,
) -> dict[str, object]:
    currencies = getattr(analysis, "currencies", {}) or {}
    return {
        "analysis_engine": getattr(analysis, "engine", ""),
        "regime": getattr(analysis, "regime", ""),
        "regime_ja": getattr(analysis, "regime_ja", ""),
        "summary": getattr(analysis, "summary", ""),
        "calendar_ok": bool(calendar_ok),
        "fetch_warnings": list(fetch_warnings),
        "currency_sentiment": {
            str(currency): _currency_sentiment(sentiment)
            for currency, sentiment in dict(currencies).items()
        },
        "news_count": len(news_items),
        "news_items": [_news_item(item) for item in news_items],
        "event_count_48h": len(events_48h),
        "events_48h": [_event_item(event) for event in events_48h],
    }


def _technical_context(tech: object | None) -> dict[str, object] | None:
    if tech is None:
        return None
    views = getattr(tech, "views", {}) or {}
    return {
        "symbol": getattr(tech, "symbol", ""),
        "fast_window": getattr(tech, "fast_window", None),
        "slow_window": getattr(tech, "slow_window", None),
        "coverage": tech.coverage() if hasattr(tech, "coverage") else None,
        "alignment_score": tech.alignment_score() if hasattr(tech, "alignment_score") else None,
        "agreement_ratio": tech.agreement_ratio() if hasattr(tech, "agreement_ratio") else None,
        "ma_side_1h": tech.ma_side() if hasattr(tech, "ma_side") else None,
        "views": {
            str(interval): {
                "interval": getattr(view, "interval", interval),
                "recommendation": getattr(view, "recommendation", ""),
                "recommendation_ja": getattr(view, "recommendation_ja", ""),
                "score": getattr(view, "score", None),
                "buy": getattr(view, "buy", None),
                "sell": getattr(view, "sell", None),
                "neutral": getattr(view, "neutral", None),
                "close": _number_or_none(getattr(view, "close", None)),
                "rsi": _number_or_none(getattr(view, "rsi", None)),
                "macd": _number_or_none(getattr(view, "macd", None)),
                "macd_signal": _number_or_none(getattr(view, "macd_signal", None)),
                "adx": _number_or_none(getattr(view, "adx", None)),
                "atr": _number_or_none(getattr(view, "atr", None)),
                "sma_fast": _number_or_none(getattr(view, "sma_fast", None)),
                "sma_slow": _number_or_none(getattr(view, "sma_slow", None)),
            }
            for interval, view in dict(views).items()
        },
    }


def _audit_flags(plan: object) -> dict[str, object]:
    direction = str(getattr(plan, "direction", ""))
    return {
        "is_trade_candidate": direction in ("long", "short"),
        "has_price": getattr(plan, "close", None) is not None,
        "has_atr": getattr(plan, "atr", None) is not None,
        "has_tp_sl": all(
            getattr(plan, key, None) is not None for key in ("stop", "target1", "target2")
        ),
        "scoring_ready": (
            direction in ("long", "short")
            and getattr(plan, "close", None) is not None
            and getattr(plan, "atr", None) is not None
            and all(getattr(plan, key, None) is not None for key in ("stop", "target1", "target2"))
        ),
        "append_only": True,
    }


def _currency_sentiment(sentiment: object) -> dict[str, object]:
    return {
        "currency": getattr(sentiment, "currency", ""),
        "score": _number_or_none(getattr(sentiment, "score", None)),
        "label_ja": getattr(sentiment, "label_ja", ""),
        "positives": getattr(sentiment, "positives", None),
        "negatives": getattr(sentiment, "negatives", None),
        "headline_count": getattr(sentiment, "headline_count", None),
        "themes": list(getattr(sentiment, "themes", []) or []),
        "comment": getattr(sentiment, "comment", ""),
        "confidence": _number_or_none(getattr(sentiment, "confidence", None)),
    }


def _news_item(item: object) -> dict[str, object]:
    published = getattr(item, "published", None)
    return {
        "title": getattr(item, "title", ""),
        "source": getattr(item, "source", ""),
        "link": getattr(item, "link", ""),
        "published": _iso(published),
        "summary": getattr(item, "summary", ""),
        "currencies": list(getattr(item, "currencies", ()) or ()),
    }


def _event_item(event: object) -> dict[str, object]:
    return {
        "title": getattr(event, "title", ""),
        "currency": getattr(event, "currency", ""),
        "when": _iso(getattr(event, "when", None)),
        "impact": getattr(event, "impact", ""),
        "impact_ja": getattr(event, "impact_ja", ""),
        "forecast": getattr(event, "forecast", ""),
        "previous": getattr(event, "previous", ""),
    }


def _ml_artifact_snapshot(artifact: object | None) -> dict[str, object] | None:
    if artifact is None:
        return None
    return {
        "trained_at": getattr(artifact, "trained_at", ""),
        "n_train": getattr(artifact, "n_train", 0),
        "n_valid": getattr(artifact, "n_valid", 0),
        "base_rate": getattr(artifact, "base_rate", None),
        "val_logloss": getattr(artifact, "val_logloss", None),
        "baseline_logloss": getattr(artifact, "baseline_logloss", None),
        "val_brier": getattr(artifact, "val_brier", None),
        "baseline_brier": getattr(artifact, "baseline_brier", None),
        "usable": getattr(artifact, "usable", False),
        "reasons": list(getattr(artifact, "reasons", []) or []),
        "importance_by_name": dict(getattr(artifact, "importance_by_name", {}) or {}),
        "summary_ja": artifact.summary_ja() if hasattr(artifact, "summary_ja") else "",
    }


def _promotion_snapshot(state: object | None) -> dict[str, object] | None:
    if state is None:
        return None
    stage_map = state.as_stage_map() if hasattr(state, "as_stage_map") else {}
    return {
        "stages": stage_map,
        "updated_at": getattr(state, "updated_at", ""),
        "notes_ja": list(getattr(state, "notes_ja", []) or []),
        "history": list(getattr(state, "history", []) or [])[-20:],
    }


def _decision_id(
    run_id: str,
    mode: str,
    symbol: str,
    timeframe: str,
    direction: str,
    index: int,
) -> str:
    raw = "|".join((run_id, mode, symbol, timeframe, direction, str(index)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _normalize_price_entry(row: Mapping[str, object]) -> dict[str, object]:
    entry = dict(row)
    timeframe = str(entry.get("timeframe", "fusion") or "fusion")
    entry.setdefault("timeframe", timeframe)
    entry.setdefault("mode", "per_timeframe" if timeframe != "fusion" else "fusion")
    entry.setdefault("horizon_hours", _horizon_for_timeframe(timeframe))
    return _json_ready_dict(entry)


def _entry_matches_context(
    entry: Mapping[str, object],
    *,
    mode: str,
    timeframe: str,
) -> bool:
    entry_timeframe = str(entry.get("timeframe", "fusion") or "fusion")
    if entry_timeframe != timeframe:
        return False
    entry_mode = str(entry.get("mode", "") or "")
    return not entry_mode or entry_mode == mode


def _meta_key(entry: Mapping[str, object]) -> tuple[str, str, str, str, str]:
    return (
        str(entry.get("symbol", "")).upper(),
        str(entry.get("direction", "")),
        str(entry.get("ts", "")),
        str(entry.get("timeframe", "fusion") or "fusion"),
        str(entry.get("mode", "fusion") or "fusion"),
    )


def _horizon_for_timeframe(timeframe: str) -> float:
    if timeframe == "fusion":
        return 24.0
    return float(PRIMARY_HORIZON_HOURS.get(timeframe, 24.0))


def _score_label(outcome: TradeOutcome) -> str:
    if outcome.realized_r is None:
        return "unscored"
    if not outcome.tradable:
        return "low_quality"
    if outcome.first_touch in ("tp1", "tp2"):
        return "tp_hit"
    if outcome.first_touch == "sl":
        return "sl_hit"
    if outcome.first_touch == "ambiguous_sl_tp":
        return "ambiguous_sl_tp"
    return "terminal_mark_to_market"


def _run_id(now: datetime, mode: str) -> str:
    compact = now.strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(f"{mode}|{now.isoformat()}".encode()).hexdigest()[:8]
    return f"{compact}-{mode}-{digest}"


def _entry_horizon(entry: Mapping[str, object]) -> float:
    raw = entry.get("horizon_hours")
    if isinstance(raw, (int, float)) and float(raw) > 0:
        return float(raw)
    return _horizon_for_timeframe(str(entry.get("timeframe", "fusion")))


def _json_ready_dict(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _json_ready(item) for key, item in value.items()}


def _json_ready(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_ready(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    if isinstance(value, datetime):
        return _iso(value)
    return json_safe(value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    return str(value) if value is not None else ""


def _number_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None
