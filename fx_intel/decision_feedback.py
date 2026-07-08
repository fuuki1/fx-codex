"""Feed TP/SL/MFE/MAE failure analysis back into the next decision.

decision_log.py produces complete scoring reports.  This module turns those
reports into a conservative deployment hook: repeated bad symbol x timeframe x
direction cells are blocked or dampened on the next briefing, while low-quality
cells only emit a quality warning.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, UTC
import json
from pathlib import Path

from .trade_outcome import json_safe

MIN_FEEDBACK_SAMPLES = 8
BLOCK_MIN_SAMPLES = 20
BLOCK_FACTOR = 0.45
DAMPEN_FACTOR = 0.75
WATCH_FACTOR = 0.85
QUALITY_WARN_RATE = 0.50
SL_WARN_RATE = 0.45
NEGATIVE_EXPECTANCY_BLOCK_R = -0.20
HIGH_SEVERITY_REASONS = {
    "sl_first",
    "adverse_excursion_dominant",
    "large_adverse_excursion",
    "confidence_overreach",
}
TARGET_RETEST_REASONS = {"tp_too_far", "weak_favorable_excursion"}

FeedbackAdjuster = Callable[[str, str, int], tuple[float, str, bool]]


@dataclass(frozen=True)
class FailureReasonStats:
    key: str
    label_ja: str
    count: int
    primary_count: int = 0
    rate: float = 0.0
    avg_realized_r: float | None = None
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    advice_ja: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label_ja": self.label_ja,
            "count": self.count,
            "primary_count": self.primary_count,
            "rate": self.rate,
            "avg_realized_r": self.avg_realized_r,
            "avg_mfe_r": self.avg_mfe_r,
            "avg_mae_r": self.avg_mae_r,
            "advice_ja": self.advice_ja,
        }


@dataclass(frozen=True)
class DecisionFeedbackCell:
    symbol: str
    timeframe: str
    direction: str
    evaluated: int = 0
    tradable: int = 0
    wins: int = 0
    losses: int = 0
    unscored: int = 0
    low_quality: int = 0
    hit_rate: float | None = None
    expectancy_r: float | None = None
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    sl_rate: float | None = None
    tp_rate: float | None = None
    high_severity_rate: float = 0.0
    failure_reasons: list[FailureReasonStats] = field(default_factory=list)
    action: str = "collect_samples"
    factor: float = 1.0
    block: bool = False
    reason_ja: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "evaluated": self.evaluated,
            "tradable": self.tradable,
            "wins": self.wins,
            "losses": self.losses,
            "unscored": self.unscored,
            "low_quality": self.low_quality,
            "hit_rate": self.hit_rate,
            "expectancy_r": self.expectancy_r,
            "avg_mfe_r": self.avg_mfe_r,
            "avg_mae_r": self.avg_mae_r,
            "sl_rate": self.sl_rate,
            "tp_rate": self.tp_rate,
            "high_severity_rate": self.high_severity_rate,
            "failure_reasons": [reason.to_dict() for reason in self.failure_reasons],
            "action": self.action,
            "factor": self.factor,
            "block": self.block,
            "reason_ja": self.reason_ja,
        }


@dataclass
class DecisionFeedbackProfile:
    generated_at: str = ""
    cells: dict[tuple[str, str, str], DecisionFeedbackCell] = field(default_factory=dict)
    notes_ja: list[str] = field(default_factory=list)

    def cell_for(self, symbol: str, timeframe: str, direction: str) -> DecisionFeedbackCell | None:
        return self.cells.get((symbol.upper(), timeframe, direction))

    def expectancy_lookup(self, symbol: str, timeframe: str) -> Callable[[str, str, int], tuple[float, str, bool]] | None:
        if not any(key[0] == symbol.upper() and key[1] == timeframe for key in self.cells):
            return None

        def adjust(symbol_arg: str, direction: str, _conviction: int) -> tuple[float, str, bool]:
            cell = self.cell_for(symbol_arg, timeframe, direction)
            if cell is None or cell.action in {"collect_samples", "hold"}:
                return 1.0, "", False
            return cell.factor, cell.reason_ja, cell.block

        return adjust

    def fusion_adjuster(self) -> FeedbackAdjuster | None:
        if not any(key[1] == "fusion" for key in self.cells):
            return None

        def adjust(symbol: str, direction: str, _conviction: int) -> tuple[float, str, bool]:
            cell = self.cell_for(symbol, "fusion", direction)
            if cell is None or cell.action in {"collect_samples", "hold"}:
                return 1.0, "", False
            return cell.factor, cell.reason_ja, cell.block

        return adjust

    def summary_ja(self, limit: int = 5) -> str:
        actionable = [
            cell
            for cell in self.cells.values()
            if cell.action in {"avoid", "dampen", "quality_guard"}
        ]
        if not actionable:
            return (
                "失敗理由フィードバック蓄積中 — TP/SL/MFE/MAEの失敗分類が"
                f"{MIN_FEEDBACK_SAMPLES}件たまるまで次回判断は補正しません"
            )
        actionable.sort(
            key=lambda cell: (
                {"avoid": 0, "dampen": 1, "quality_guard": 2}.get(cell.action, 3),
                -cell.evaluated,
                cell.symbol,
                cell.timeframe,
            )
        )
        lines = ["失敗理由フィードバック(次回判断へ反映):"]
        for cell in actionable[:limit]:
            lines.append(f"・{cell.reason_ja}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "cells": {
                f"{symbol}|{timeframe}|{direction}": cell.to_dict()
                for (symbol, timeframe, direction), cell in self.cells.items()
            },
            "notes_ja": list(self.notes_ja),
        }


def load_decision_outcome_report(path: str | Path) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_decision_feedback(profile: DecisionFeedbackProfile, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(json_safe(profile.to_dict()), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def derive_decision_feedback(
    report: Mapping[str, object] | None,
    *,
    now: datetime | None = None,
) -> DecisionFeedbackProfile:
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    else:
        generated_at = generated_at.astimezone(UTC)

    outcomes = list(_iter_outcomes(report or {}))
    grouped: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}
    for outcome in outcomes:
        direction = str(outcome.get("direction", ""))
        if direction not in ("long", "short"):
            continue
        symbol = str(outcome.get("symbol", "")).upper()
        timeframe = str(outcome.get("timeframe", "fusion") or "fusion")
        if not symbol:
            continue
        grouped.setdefault((symbol, timeframe, direction), []).append(outcome)

    cells = {
        key: _derive_cell(*key, rows)
        for key, rows in sorted(grouped.items())
    }
    profile = DecisionFeedbackProfile(generated_at=generated_at.isoformat(), cells=cells)
    profile.notes_ja = _notes_ja(profile)
    return profile


def _derive_cell(
    symbol: str,
    timeframe: str,
    direction: str,
    rows: list[Mapping[str, object]],
) -> DecisionFeedbackCell:
    evaluated = len(rows)
    tradable_rows = [
        row
        for row in rows
        if bool(row.get("tradable")) and _float(row.get("realized_r")) is not None
    ]
    r_values = [_float(row.get("realized_r")) for row in tradable_rows]
    r_values = [value for value in r_values if value is not None]
    wins = sum(1 for value in r_values if value > 0)
    losses = sum(1 for value in r_values if value < 0)
    unscored = sum(1 for row in rows if _float(row.get("realized_r")) is None)
    low_quality = sum(
        1
        for row in rows
        if _float(row.get("realized_r")) is not None and not bool(row.get("tradable"))
    )
    mfe_values = [_float(row.get("mfe_r")) for row in tradable_rows]
    mae_values = [_float(row.get("mae_r")) for row in tradable_rows]
    mfe_values = [value for value in mfe_values if value is not None]
    mae_values = [value for value in mae_values if value is not None]
    sl_count = sum(1 for row in tradable_rows if row.get("first_touch") == "sl")
    tp_count = sum(1 for row in tradable_rows if row.get("first_touch") in {"tp1", "tp2"})
    reason_stats = _reason_stats(rows)
    high_severity_hits = sum(
        1 for row in rows if _row_has_any_reason(row, HIGH_SEVERITY_REASONS)
    )
    tradable = len(tradable_rows)
    hit_rate = wins / tradable if tradable else None
    expectancy = _mean(r_values)
    avg_mfe = _mean(mfe_values)
    avg_mae = _mean(mae_values)
    sl_rate = sl_count / tradable if tradable else None
    tp_rate = tp_count / tradable if tradable else None
    high_severity_rate = high_severity_hits / evaluated if evaluated else 0.0
    action, factor, block, reason = _policy(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        evaluated=evaluated,
        tradable=tradable,
        unscored=unscored,
        low_quality=low_quality,
        expectancy=expectancy,
        avg_mfe=avg_mfe,
        avg_mae=avg_mae,
        sl_rate=sl_rate,
        high_severity_rate=high_severity_rate,
        reason_stats=reason_stats,
    )
    return DecisionFeedbackCell(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        evaluated=evaluated,
        tradable=tradable,
        wins=wins,
        losses=losses,
        unscored=unscored,
        low_quality=low_quality,
        hit_rate=_round(hit_rate),
        expectancy_r=_round(expectancy),
        avg_mfe_r=_round(avg_mfe),
        avg_mae_r=_round(avg_mae),
        sl_rate=_round(sl_rate),
        tp_rate=_round(tp_rate),
        high_severity_rate=_round(high_severity_rate) or 0.0,
        failure_reasons=reason_stats,
        action=action,
        factor=factor,
        block=block,
        reason_ja=reason,
    )


def _policy(
    *,
    symbol: str,
    timeframe: str,
    direction: str,
    evaluated: int,
    tradable: int,
    unscored: int,
    low_quality: int,
    expectancy: float | None,
    avg_mfe: float | None,
    avg_mae: float | None,
    sl_rate: float | None,
    high_severity_rate: float,
    reason_stats: list[FailureReasonStats],
) -> tuple[str, float, bool, str]:
    label = _cell_label(symbol, timeframe, direction)
    top_reasons = " / ".join(reason.label_ja for reason in reason_stats[:3]) or "失敗理由未分類"
    if evaluated < MIN_FEEDBACK_SAMPLES:
        return "collect_samples", 1.0, False, ""

    quality_bad = evaluated > 0 and (unscored + low_quality) / evaluated >= QUALITY_WARN_RATE
    if quality_bad or tradable < MIN_FEEDBACK_SAMPLES:
        return (
            "quality_guard",
            1.0,
            False,
            f"{label}: 採点品質不足({tradable}/{evaluated})。次回判断は参考扱い [{top_reasons}]",
        )

    severe_block = (
        tradable >= BLOCK_MIN_SAMPLES
        and expectancy is not None
        and expectancy <= NEGATIVE_EXPECTANCY_BLOCK_R
        and ((sl_rate or 0.0) >= SL_WARN_RATE or high_severity_rate >= 0.50)
    )
    if severe_block:
        return (
            "avoid",
            BLOCK_FACTOR,
            True,
            f"{label}: 失敗分類ベースで期待R{expectancy:+.2f}R、SL率{_fmt_pct(sl_rate)}。見送り優先 [{top_reasons}]",
        )

    adverse_dominant = avg_mfe is not None and avg_mae is not None and avg_mae >= avg_mfe
    target_retest = any(reason.key in TARGET_RETEST_REASONS for reason in reason_stats[:3])
    should_dampen = (
        expectancy is not None
        and expectancy <= 0
        or (sl_rate is not None and sl_rate >= SL_WARN_RATE)
        or adverse_dominant
        or high_severity_rate >= 0.35
        or target_retest
    )
    if should_dampen:
        factor = WATCH_FACTOR if target_retest and not adverse_dominant else DAMPEN_FACTOR
        return (
            "dampen",
            factor,
            False,
            f"{label}: 失敗分類({top_reasons})を次回へ反映し確信度×{factor:.2f}"
            f" (期待R {_fmt_signed(expectancy, 'R')}, MFE {_fmt_number(avg_mfe, 'R')}, MAE {_fmt_number(avg_mae, 'R')})",
        )
    return "hold", 1.0, False, ""


def _reason_stats(rows: list[Mapping[str, object]]) -> list[FailureReasonStats]:
    counts: Counter[str] = Counter()
    primary_counts: Counter[str] = Counter()
    labels: dict[str, str] = {}
    advice: dict[str, str] = {}
    r_values: dict[str, list[float]] = {}
    mfe_values: dict[str, list[float]] = {}
    mae_values: dict[str, list[float]] = {}
    for row in rows:
        reasons = row.get("failure_reasons")
        if not isinstance(reasons, list):
            continue
        primary = str(row.get("primary_failure_reason") or "")
        realized = _float(row.get("realized_r"))
        mfe = _float(row.get("mfe_r"))
        mae = _float(row.get("mae_r"))
        for reason in reasons:
            if not isinstance(reason, Mapping):
                continue
            key = str(reason.get("key", ""))
            if not key:
                continue
            counts[key] += 1
            labels[key] = str(reason.get("label_ja", key))
            advice[key] = str(reason.get("advice_ja", ""))
            if key == primary:
                primary_counts[key] += 1
            if realized is not None:
                r_values.setdefault(key, []).append(realized)
            if mfe is not None:
                mfe_values.setdefault(key, []).append(mfe)
            if mae is not None:
                mae_values.setdefault(key, []).append(mae)
    total = len(rows)
    stats = [
        FailureReasonStats(
            key=key,
            label_ja=labels.get(key, key),
            count=count,
            primary_count=primary_counts.get(key, 0),
            rate=round(count / total, 4) if total else 0.0,
            avg_realized_r=_round(_mean(r_values.get(key, []))),
            avg_mfe_r=_round(_mean(mfe_values.get(key, []))),
            avg_mae_r=_round(_mean(mae_values.get(key, []))),
            advice_ja=advice.get(key, ""),
        )
        for key, count in counts.items()
    ]
    stats.sort(key=lambda item: (-item.primary_count, -item.count, item.key))
    return stats


def _iter_outcomes(report: Mapping[str, object]) -> Iterable[Mapping[str, object]]:
    outcomes = report.get("outcomes")
    if not isinstance(outcomes, list):
        return []
    return [row for row in outcomes if isinstance(row, Mapping)]


def _row_has_any_reason(row: Mapping[str, object], keys: set[str]) -> bool:
    reasons = row.get("failure_reasons")
    if not isinstance(reasons, list):
        return False
    return any(isinstance(reason, Mapping) and reason.get("key") in keys for reason in reasons)


def _notes_ja(profile: DecisionFeedbackProfile) -> list[str]:
    summary = profile.summary_ja()
    return [] if summary.startswith("失敗理由フィードバック蓄積中") else summary.splitlines()


def _cell_label(symbol: str, timeframe: str, direction: str) -> str:
    direction_ja = "ロング" if direction == "long" else "ショート"
    return f"{symbol} {timeframe} {direction_ja}"


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _fmt_signed(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}{suffix}"


def _fmt_number(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}{suffix}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0%}"
