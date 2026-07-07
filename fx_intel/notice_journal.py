"""Journal detailed trade notices for later quality review."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Mapping, Sequence

from .market_structure import EntryLevels
from .trade_notice import DetailedTradeNotice

SCHEMA_VERSION = 1


def report_hash(text: str) -> str:
    """Stable SHA-256 hash of the rendered notice body."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def append_detailed_notices(
    path: str | Path,
    notices: Sequence[DetailedTradeNotice],
    *,
    report_text: str,
    entry_levels_by_symbol: Mapping[str, EntryLevels] | None = None,
    chunk_count: int = 1,
    delivery: str = "discord",
    now: datetime | None = None,
) -> None:
    """Append one JSONL row per detailed notice.

    The rendered body is not stored directly.  Instead the journal stores a
    hash, character count, conditions, and the structured levels used to create
    the message.  This keeps the audit trail compact while preserving enough
    data to review why a notice looked the way it did.
    """
    now = now or datetime.now(UTC)
    entry_levels_by_symbol = entry_levels_by_symbol or {}
    digest = report_hash(report_text)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for notice in notices:
            level = entry_levels_by_symbol.get(notice.symbol)
            handle.write(
                json.dumps(
                    {
                        "schema": SCHEMA_VERSION,
                        "ts": now.isoformat(),
                        "delivery": delivery,
                        "report_sha256": digest,
                        "report_chars": len(report_text),
                        "chunk_count": int(chunk_count),
                        "symbol": notice.symbol,
                        "direction": notice.direction,
                        "header_label": notice.header_label,
                        "stance_label": notice.stance_label,
                        "conviction": notice.conviction,
                        "current_price": notice.current_price,
                        "invalidation_line": notice.invalidation_line,
                        "valid_until": _dt(notice.valid_until),
                        "important_event": _event(notice),
                        "no_entry_window": _no_entry(notice),
                        "price_plan": _price_plan(notice),
                        "entry_level_source": _entry_level(level),
                        "entry_scenarios": _entry_scenarios(notice),
                        "skip_conditions": list(notice.skip_conditions),
                        "final_actions": list(notice.final_actions),
                        "warnings": list(notice.warnings),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


def read_notice_entries(path: str | Path):
    """Yield valid JSON objects from the detailed notice journal."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            yield entry


def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _event(notice: DetailedTradeNotice) -> dict | None:
    event = notice.important_event
    if event is None:
        return None
    return {
        "title": event.title,
        "currency": event.currency,
        "when": event.when.isoformat(),
        "impact": event.impact,
        "forecast": event.forecast,
        "previous": event.previous,
    }


def _no_entry(notice: DetailedTradeNotice) -> dict | None:
    window = notice.no_entry_window
    if window is None:
        return None
    return {"start": window.start.isoformat(), "end": window.end.isoformat()}


def _price_plan(notice: DetailedTradeNotice) -> dict:
    plan = notice.price_plan
    return {
        "current": plan.current,
        "stop": plan.stop,
        "target1": plan.target1,
        "target2": plan.target2,
        "atr": plan.atr,
        "stop_pips": plan.stop_pips,
        "target1_pips": plan.target1_pips,
        "target2_pips": plan.target2_pips,
        "rr_t1": plan.rr_t1,
        "rr_t2": plan.rr_t2,
        "stop_atr_multiple": plan.stop_atr_multiple,
    }


def _entry_level(level: EntryLevels | None) -> dict:
    if level is None:
        return {"source": "atr_fallback"}
    return {
        "source": level.source,
        "pullback_low": level.pullback_low,
        "pullback_high": level.pullback_high,
        "reclaim_level": level.reclaim_level,
        "breakout_level": level.breakout_level,
        "support": level.support,
        "resistance": level.resistance,
        "recent_low": level.recent_low,
        "recent_high": level.recent_high,
        "bars_used": level.bars_used,
    }


def _entry_scenarios(notice: DetailedTradeNotice) -> list[dict]:
    return [
        {
            "title": scenario.title,
            "trigger": scenario.trigger,
            "confirmation": scenario.confirmation,
            "entry": scenario.entry,
            "stop": scenario.stop,
            "targets": scenario.targets,
            "invalidation": scenario.invalidation,
        }
        for scenario in notice.entry_scenarios
    ]
