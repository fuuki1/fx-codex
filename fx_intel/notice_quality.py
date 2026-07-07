"""Quality scoring for detailed trade notices.

This module evaluates the notice, not live execution.  It checks whether the
future OHLC path touched T1 or SL first within the notice validity window and
keeps ambiguous same-bar touches out of hit-rate statistics.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Mapping, Sequence

from .coerce import to_float_or_none, to_int_or_none
from .market_structure import OhlcBar

QUALITY_REPORT_SCHEMA_VERSION = 1

OUTCOME_HIT = "hit_t1"
OUTCOME_MISS = "hit_sl"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_NO_TOUCH = "no_touch"
OUTCOME_NO_ENTRY = "no_entry_trigger"
OUTCOME_SKIPPED = "skipped"

ENTRY_CHECK_UNAVAILABLE = "unavailable"
ENTRY_CHECK_TRIGGERED = "triggered"
ENTRY_CHECK_NOT_TRIGGERED = "not_triggered"
ENTRY_SCENARIO_PULLBACK = "pullback_reclaim"
ENTRY_SCENARIO_BREAKOUT = "breakout_hold"


@dataclass(frozen=True)
class NoticeQualityOutcome:
    symbol: str
    ts: datetime | None
    direction: str
    outcome: str
    touched_at: datetime | None = None
    close_at_end: float | None = None
    signed_move: float | None = None
    max_favorable: float | None = None
    max_adverse: float | None = None
    reason: str = ""
    entry_check: str = ENTRY_CHECK_UNAVAILABLE
    entry_scenario: str = ""
    entry_triggered_at: datetime | None = None
    entry_price: float | None = None
    entry_reason: str = ""

    @property
    def evaluated(self) -> bool:
        return self.outcome in (OUTCOME_HIT, OUTCOME_MISS)

    @property
    def hit(self) -> bool:
        return self.outcome == OUTCOME_HIT

    @property
    def entry_checked(self) -> bool:
        return self.entry_check in (ENTRY_CHECK_TRIGGERED, ENTRY_CHECK_NOT_TRIGGERED)


@dataclass(frozen=True)
class NoticeQualitySummary:
    total: int
    evaluated: int
    hits: int
    misses: int
    ambiguous: int
    no_touch: int
    skipped: int
    avg_signed_move: float | None
    entry_checked: int = 0
    entry_triggered: int = 0
    no_entry_trigger: int = 0

    @property
    def hit_rate(self) -> float | None:
        if self.evaluated == 0:
            return None
        return self.hits / self.evaluated


def score_notice_entry(
    entry: Mapping[str, object],
    bars: Sequence[OhlcBar],
) -> NoticeQualityOutcome:
    """Score one detailed notice journal row against future OHLC bars."""
    symbol = str(entry.get("symbol", ""))
    direction = str(entry.get("direction", ""))
    ts = _parse_dt(entry.get("ts"))
    valid_until = _parse_dt(entry.get("valid_until"))
    price_plan = entry.get("price_plan")
    if not isinstance(price_plan, Mapping):
        return _skipped(symbol, ts, direction, "missing price_plan")
    current = _float(price_plan.get("current"))
    stop = _float(price_plan.get("stop"))
    target1 = _float(price_plan.get("target1"))
    if direction not in ("long", "short"):
        return _skipped(symbol, ts, direction, "non-directional notice")
    if ts is None:
        return _skipped(symbol, ts, direction, "invalid ts")
    if current is None or stop is None or target1 is None:
        return _skipped(symbol, ts, direction, "missing current/stop/target1")
    if valid_until is None:
        return _skipped(symbol, ts, direction, "missing valid_until")

    future = _future_bars(bars, ts, valid_until)
    if not future:
        return _skipped(symbol, ts, direction, "no future bars in validity window")

    trigger = _entry_trigger(entry, direction, future, stop)
    score_price = current
    score_bars = future
    entry_check = trigger.status
    entry_scenario = trigger.scenario
    entry_triggered_at = trigger.triggered_at
    entry_price = trigger.entry_price
    entry_reason = trigger.reason
    if trigger.status == ENTRY_CHECK_NOT_TRIGGERED:
        return _outcome(
            symbol,
            ts,
            direction,
            OUTCOME_NO_ENTRY,
            None,
            future,
            current,
            reason=trigger.reason or "entry condition did not trigger",
            entry_check=entry_check,
            entry_scenario=entry_scenario,
            entry_triggered_at=entry_triggered_at,
            entry_price=entry_price,
            entry_reason=entry_reason,
        )
    if trigger.status == ENTRY_CHECK_TRIGGERED:
        score_price = trigger.entry_price if trigger.entry_price is not None else current
        score_bars = (
            [bar for bar in future if _bar_ts(bar) > trigger.triggered_at]
            if trigger.triggered_at is not None
            else []
        )

    for bar in score_bars:
        hit_target = _touches_target(direction, bar, target1)
        hit_stop = _touches_stop(direction, bar, stop)
        if hit_target and hit_stop:
            return _outcome(
                symbol,
                ts,
                direction,
                OUTCOME_AMBIGUOUS,
                bar.timestamp,
                score_bars,
                score_price,
                reason="target and stop touched in the same bar",
                entry_check=entry_check,
                entry_scenario=entry_scenario,
                entry_triggered_at=entry_triggered_at,
                entry_price=entry_price,
                entry_reason=entry_reason,
            )
        if hit_target:
            return _outcome(
                symbol,
                ts,
                direction,
                OUTCOME_HIT,
                bar.timestamp,
                score_bars,
                score_price,
                entry_check=entry_check,
                entry_scenario=entry_scenario,
                entry_triggered_at=entry_triggered_at,
                entry_price=entry_price,
                entry_reason=entry_reason,
            )
        if hit_stop:
            return _outcome(
                symbol,
                ts,
                direction,
                OUTCOME_MISS,
                bar.timestamp,
                score_bars,
                score_price,
                entry_check=entry_check,
                entry_scenario=entry_scenario,
                entry_triggered_at=entry_triggered_at,
                entry_price=entry_price,
                entry_reason=entry_reason,
            )

    return _outcome(
        symbol,
        ts,
        direction,
        OUTCOME_NO_TOUCH,
        None,
        score_bars,
        score_price,
        entry_check=entry_check,
        entry_scenario=entry_scenario,
        entry_triggered_at=entry_triggered_at,
        entry_price=entry_price,
        entry_reason=entry_reason,
    )


def score_notice_entries(
    entries: Sequence[Mapping[str, object]],
    bars_by_symbol: Mapping[str, Sequence[OhlcBar]],
) -> list[NoticeQualityOutcome]:
    """Score multiple detailed notice rows using symbol-keyed OHLC bars."""
    outcomes: list[NoticeQualityOutcome] = []
    for entry in entries:
        symbol = str(entry.get("symbol", ""))
        outcomes.append(score_notice_entry(entry, bars_by_symbol.get(symbol, ())))
    return outcomes


def summarize_outcomes(outcomes: Sequence[NoticeQualityOutcome]) -> NoticeQualitySummary:
    total = len(outcomes)
    evaluated = sum(1 for outcome in outcomes if outcome.evaluated)
    hits = sum(1 for outcome in outcomes if outcome.outcome == OUTCOME_HIT)
    misses = sum(1 for outcome in outcomes if outcome.outcome == OUTCOME_MISS)
    ambiguous = sum(1 for outcome in outcomes if outcome.outcome == OUTCOME_AMBIGUOUS)
    no_touch = sum(1 for outcome in outcomes if outcome.outcome == OUTCOME_NO_TOUCH)
    no_entry_trigger = sum(1 for outcome in outcomes if outcome.outcome == OUTCOME_NO_ENTRY)
    skipped = sum(1 for outcome in outcomes if outcome.outcome == OUTCOME_SKIPPED)
    moves = [outcome.signed_move for outcome in outcomes if outcome.signed_move is not None]
    avg_move = sum(moves) / len(moves) if moves else None
    entry_checked = sum(1 for outcome in outcomes if outcome.entry_checked)
    entry_triggered = sum(1 for outcome in outcomes if outcome.entry_check == ENTRY_CHECK_TRIGGERED)
    return NoticeQualitySummary(
        total=total,
        evaluated=evaluated,
        hits=hits,
        misses=misses,
        ambiguous=ambiguous,
        no_touch=no_touch,
        skipped=skipped,
        avg_signed_move=avg_move,
        entry_checked=entry_checked,
        entry_triggered=entry_triggered,
        no_entry_trigger=no_entry_trigger,
    )


def format_summary_ja(summary: NoticeQualitySummary) -> str:
    """Format a compact Japanese summary for CLI output."""
    if summary.total == 0:
        return "詳細通知評価: 対象なし"
    hit_rate = "—" if summary.hit_rate is None else f"{summary.hit_rate:.0%}"
    avg_move = "—" if summary.avg_signed_move is None else f"{summary.avg_signed_move:+.5f}"
    entry_stats = ""
    if summary.entry_checked:
        entry_stats = (
            f" / 条件確認{summary.entry_checked}件 / 条件発火{summary.entry_triggered}件 / "
            f"発火なし{summary.no_entry_trigger}件"
        )
    return (
        "詳細通知評価: "
        f"対象{summary.total}件 / 評価{summary.evaluated}件 / "
        f"T1先着{summary.hits}件 / SL先着{summary.misses}件 / "
        f"勝率{hit_rate} / 曖昧{summary.ambiguous}件 / 未到達{summary.no_touch}件 / "
        f"除外{summary.skipped}件{entry_stats} / 平均方向変化{avg_move}"
    )


def summary_to_dict(summary: NoticeQualitySummary) -> dict:
    """Convert a quality summary to a stable JSON-friendly shape."""
    return {
        "total": summary.total,
        "evaluated": summary.evaluated,
        "hits": summary.hits,
        "misses": summary.misses,
        "hit_rate": summary.hit_rate,
        "ambiguous": summary.ambiguous,
        "no_touch": summary.no_touch,
        "skipped": summary.skipped,
        "avg_signed_move": summary.avg_signed_move,
        "entry_checked": summary.entry_checked,
        "entry_triggered": summary.entry_triggered,
        "no_entry_trigger": summary.no_entry_trigger,
    }


def outcome_to_dict(
    entry: Mapping[str, object],
    outcome: NoticeQualityOutcome,
    *,
    index: int | None = None,
) -> dict:
    """Convert one scored notice row to a stable JSON/CSV-friendly dict."""
    price_plan = entry.get("price_plan")
    price_plan = price_plan if isinstance(price_plan, Mapping) else {}
    level = entry.get("entry_level_source")
    level = level if isinstance(level, Mapping) else {}
    row = {
        "index": index,
        "symbol": outcome.symbol,
        "ts": _dt(outcome.ts),
        "direction": outcome.direction,
        "conviction": _int_or_none(entry.get("conviction")),
        "valid_until": _string_or_none(entry.get("valid_until")),
        "outcome": outcome.outcome,
        "evaluated": outcome.evaluated,
        "hit": outcome.hit,
        "touched_at": _dt(outcome.touched_at),
        "close_at_end": outcome.close_at_end,
        "signed_move": outcome.signed_move,
        "max_favorable": outcome.max_favorable,
        "max_adverse": outcome.max_adverse,
        "reason": outcome.reason,
        "entry_check": outcome.entry_check,
        "entry_scenario": outcome.entry_scenario,
        "entry_triggered_at": _dt(outcome.entry_triggered_at),
        "entry_price": outcome.entry_price,
        "entry_reason": outcome.entry_reason,
        "entry_source": _string_or_none(level.get("source")),
        "current": _float(price_plan.get("current")),
        "stop": _float(price_plan.get("stop")),
        "target1": _float(price_plan.get("target1")),
        "target2": _float(price_plan.get("target2")),
        "report_sha256": _string_or_none(entry.get("report_sha256")),
    }
    return row


def build_quality_report(
    entries: Sequence[Mapping[str, object]],
    outcomes: Sequence[NoticeQualityOutcome],
    *,
    generated_at: datetime | None = None,
) -> dict:
    """Build a serializable detailed-notice quality report."""
    generated_at = generated_at or datetime.now(UTC)
    summary = summarize_outcomes(outcomes)
    return {
        "schema": QUALITY_REPORT_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "summary": summary_to_dict(summary),
        "outcomes": [
            outcome_to_dict(entry, outcome, index=index)
            for index, (entry, outcome) in enumerate(zip(entries, outcomes, strict=False))
        ],
    }


def write_quality_report_json(
    path: str | Path,
    entries: Sequence[Mapping[str, object]],
    outcomes: Sequence[NoticeQualityOutcome],
    *,
    generated_at: datetime | None = None,
) -> None:
    """Write a JSON quality report for dashboard/audit use."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    report = build_quality_report(entries, outcomes, generated_at=generated_at)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def write_quality_outcomes_csv(
    path: str | Path,
    entries: Sequence[Mapping[str, object]],
    outcomes: Sequence[NoticeQualityOutcome],
) -> None:
    """Write flat quality outcomes for spreadsheet-style review."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        outcome_to_dict(entry, outcome, index=index)
        for index, (entry, outcome) in enumerate(zip(entries, outcomes, strict=False))
    ]
    fieldnames = list(_QUALITY_CSV_FIELDS)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


_QUALITY_CSV_FIELDS = (
    "index",
    "symbol",
    "ts",
    "direction",
    "conviction",
    "valid_until",
    "outcome",
    "evaluated",
    "hit",
    "touched_at",
    "entry_check",
    "entry_scenario",
    "entry_triggered_at",
    "entry_price",
    "entry_source",
    "current",
    "stop",
    "target1",
    "target2",
    "close_at_end",
    "signed_move",
    "max_favorable",
    "max_adverse",
    "reason",
    "entry_reason",
    "report_sha256",
)


@dataclass(frozen=True)
class _EntryTrigger:
    status: str = ENTRY_CHECK_UNAVAILABLE
    scenario: str = ""
    triggered_at: datetime | None = None
    entry_price: float | None = None
    reason: str = ""


def _outcome(
    symbol: str,
    ts: datetime | None,
    direction: str,
    outcome: str,
    touched_at: datetime | None,
    future: Sequence[OhlcBar],
    current: float,
    reason: str = "",
    entry_check: str = ENTRY_CHECK_UNAVAILABLE,
    entry_scenario: str = "",
    entry_triggered_at: datetime | None = None,
    entry_price: float | None = None,
    entry_reason: str = "",
) -> NoticeQualityOutcome:
    last_close = future[-1].close if future else None
    signed_move = None
    max_favorable = None
    max_adverse = None
    if last_close is not None:
        signed_move = _signed(direction, last_close - current)
    if future:
        highs = [bar.high for bar in future]
        lows = [bar.low for bar in future]
        if direction == "long":
            max_favorable = max(highs) - current
            max_adverse = current - min(lows)
        elif direction == "short":
            max_favorable = current - min(lows)
            max_adverse = max(highs) - current
    return NoticeQualityOutcome(
        symbol=symbol,
        ts=ts,
        direction=direction,
        outcome=outcome,
        touched_at=touched_at,
        close_at_end=last_close,
        signed_move=signed_move,
        max_favorable=max_favorable,
        max_adverse=max_adverse,
        reason=reason,
        entry_check=entry_check,
        entry_scenario=entry_scenario,
        entry_triggered_at=entry_triggered_at,
        entry_price=entry_price,
        entry_reason=entry_reason,
    )


def _skipped(symbol: str, ts: datetime | None, direction: str, reason: str) -> NoticeQualityOutcome:
    return NoticeQualityOutcome(
        symbol=symbol,
        ts=ts,
        direction=direction,
        outcome=OUTCOME_SKIPPED,
        reason=reason,
    )


def _future_bars(bars: Sequence[OhlcBar], start: datetime, end: datetime) -> list[OhlcBar]:
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    return [
        bar
        for bar in sorted(bars, key=lambda item: item.timestamp)
        if start_utc < _bar_ts(bar) <= end_utc
    ]


def _entry_trigger(
    entry: Mapping[str, object],
    direction: str,
    future: Sequence[OhlcBar],
    stop: float,
) -> _EntryTrigger:
    levels = entry.get("entry_level_source")
    if not isinstance(levels, Mapping):
        return _EntryTrigger()
    pullback_low = _float(levels.get("pullback_low"))
    pullback_high = _float(levels.get("pullback_high"))
    reclaim = _float(levels.get("reclaim_level"))
    breakout = _float(levels.get("breakout_level"))
    source = str(levels.get("source", "")).strip()
    if not source or source == "atr_fallback":
        return _EntryTrigger()
    if pullback_low is None or pullback_high is None or reclaim is None or breakout is None:
        return _EntryTrigger()

    candidates: list[_EntryTrigger] = []
    pullback_seen = False
    for bar in future:
        if _touches_stop(direction, bar, stop):
            if candidates:
                break
            return _EntryTrigger(
                status=ENTRY_CHECK_NOT_TRIGGERED,
                reason="stop touched before entry condition confirmed",
            )
        if _touches_zone(bar, pullback_low, pullback_high):
            pullback_seen = True
        if pullback_seen and _confirmed_reclaim(direction, bar, reclaim):
            candidates.append(
                _EntryTrigger(
                    status=ENTRY_CHECK_TRIGGERED,
                    scenario=ENTRY_SCENARIO_PULLBACK,
                    triggered_at=_bar_ts(bar),
                    entry_price=reclaim,
                    reason="pullback zone touched and reclaim confirmed by close",
                )
            )
        if _confirmed_breakout(direction, bar, breakout):
            candidates.append(
                _EntryTrigger(
                    status=ENTRY_CHECK_TRIGGERED,
                    scenario=ENTRY_SCENARIO_BREAKOUT,
                    triggered_at=_bar_ts(bar),
                    entry_price=breakout,
                    reason="breakout level held into bar close",
                )
            )
        if candidates:
            break

    if not candidates:
        return _EntryTrigger(
            status=ENTRY_CHECK_NOT_TRIGGERED,
            reason="entry condition did not trigger within validity window",
        )
    return min(
        candidates,
        key=lambda item: (
            item.triggered_at or datetime.max.replace(tzinfo=UTC),
            0 if item.scenario == ENTRY_SCENARIO_PULLBACK else 1,
        ),
    )


def _bar_ts(bar: OhlcBar) -> datetime:
    return bar.timestamp if bar.timestamp.tzinfo else bar.timestamp.replace(tzinfo=UTC)


def _touches_target(direction: str, bar: OhlcBar, target: float) -> bool:
    if direction == "long":
        return bar.high >= target
    if direction == "short":
        return bar.low <= target
    return False


def _touches_stop(direction: str, bar: OhlcBar, stop: float) -> bool:
    if direction == "long":
        return bar.low <= stop
    if direction == "short":
        return bar.high >= stop
    return False


def _touches_zone(bar: OhlcBar, level_a: float, level_b: float) -> bool:
    low = min(level_a, level_b)
    high = max(level_a, level_b)
    return bar.low <= high and bar.high >= low


def _confirmed_reclaim(direction: str, bar: OhlcBar, reclaim: float) -> bool:
    if direction == "long":
        return bar.close >= reclaim
    if direction == "short":
        return bar.close <= reclaim
    return False


def _confirmed_breakout(direction: str, bar: OhlcBar, breakout: float) -> bool:
    if direction == "long":
        return bar.close >= breakout
    if direction == "short":
        return bar.close <= breakout
    return False


def _signed(direction: str, move: float) -> float:
    return move if direction == "long" else -move


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _float(value: object) -> float | None:
    return to_float_or_none(value)


def _int_or_none(value: object) -> int | None:
    return to_int_or_none(value)


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _csv_value(value: object) -> object:
    return "" if value is None else value
