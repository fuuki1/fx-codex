"""Expectancy-maximization layer for per-timeframe chart decisions.

This module turns scored trade outcomes into a deployment-facing policy:
boost high-quality positive-expectancy cells, dampen weak cells, and block
cells whose out-of-sample-like history has non-positive expectancy.  It is
intentionally conservative and works at symbol x timeframe x direction
granularity.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, UTC
import json
import math
from pathlib import Path
from typing import Any

from .timeframe import PRIMARY_HORIZON_HOURS, tolerance_for
from .tp_sl_learning import MVP_SYMBOLS, MVP_TIMEFRAMES
from .trade_outcome import (
    TradeOutcome,
    aggregate_expectancy,
    evaluate_trade_outcomes,
    json_safe,
)

MIN_CELL_SAMPLES = 30
BOOST_MIN_SAMPLES = 100
EXPECTANCY_GOOD_R = 0.10
WEAK_PROFIT_FACTOR = 1.05
GOOD_PROFIT_FACTOR = 1.20
QUALITY_FLOOR = 0.55
STABILITY_FLOOR = 0.55
SORTINO_FLOOR = 0.10
RECOVERY_FACTOR_FLOOR = 0.50
CALIBRATION_ERROR_CEILING = 0.25
BLOCK_FACTOR = 0.45
DAMPEN_FACTOR = 0.75
BOOST_FACTOR_MAX = 1.10
STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
CALIBRATION_BINS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))


@dataclass(frozen=True)
class MaximizationCell:
    symbol: str
    timeframe: str
    direction: str
    evaluated: int = 0
    tradable: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    expectancy_r: float | None = None
    profit_factor_r: float | None = None
    avg_win_r: float | None = None
    avg_loss_r: float | None = None
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    sl_rate: float | None = None
    avg_path_quality: float | None = None
    brier: float | None = None
    brier_base: float | None = None
    calibration_error: float | None = None
    brier_skill: float | None = None
    payoff_ratio: float | None = None
    max_drawdown_r: float | None = None
    recovery_factor: float | None = None
    downside_deviation_r: float | None = None
    sortino_r: float | None = None
    tail_loss_r: float | None = None
    stability_score: float | None = None
    sample_confidence: float = 0.0
    score: float = 0.0
    action: str = "collect_samples"
    factor: float = 1.0
    block: bool = False
    reason_ja: str = ""
    sample_ok: bool = False
    min_samples: int = MIN_CELL_SAMPLES

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "evaluated": self.evaluated,
            "tradable": self.tradable,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "expectancy_r": self.expectancy_r,
            "profit_factor_r": self.profit_factor_r,
            "avg_win_r": self.avg_win_r,
            "avg_loss_r": self.avg_loss_r,
            "avg_mfe_r": self.avg_mfe_r,
            "avg_mae_r": self.avg_mae_r,
            "sl_rate": self.sl_rate,
            "avg_path_quality": self.avg_path_quality,
            "brier": self.brier,
            "brier_base": self.brier_base,
            "calibration_error": self.calibration_error,
            "brier_skill": self.brier_skill,
            "payoff_ratio": self.payoff_ratio,
            "max_drawdown_r": self.max_drawdown_r,
            "recovery_factor": self.recovery_factor,
            "downside_deviation_r": self.downside_deviation_r,
            "sortino_r": self.sortino_r,
            "tail_loss_r": self.tail_loss_r,
            "stability_score": self.stability_score,
            "sample_confidence": self.sample_confidence,
            "score": self.score,
            "action": self.action,
            "factor": self.factor,
            "block": self.block,
            "reason_ja": self.reason_ja,
            "sample_ok": self.sample_ok,
            "min_samples": self.min_samples,
        }


@dataclass
class TimeframeMaximization:
    generated_at: str = ""
    cells: dict[tuple[str, str, str], MaximizationCell] = field(default_factory=dict)
    per_timeframe: dict[str, MaximizationCell] = field(default_factory=dict)
    notes_ja: list[str] = field(default_factory=list)

    def cell_for(self, symbol: str, timeframe: str, direction: str) -> MaximizationCell | None:
        return self.cells.get((symbol.upper(), timeframe, direction))

    def expectancy_lookup(
        self, symbol: str, timeframe: str
    ) -> Callable[[str, str, int], tuple[float, str, bool]] | None:
        if not any(key[0] == symbol.upper() and key[1] == timeframe for key in self.cells):
            return None

        def adjust(symbol_arg: str, direction: str, _conviction: int) -> tuple[float, str, bool]:
            cell = self.cell_for(symbol_arg, timeframe, direction)
            if cell is None:
                return 1.0, "", False
            if cell.action in {"collect_samples", "hold"}:
                return 1.0, "", False
            return cell.factor, cell.reason_ja, cell.block

        return adjust

    def summary_ja(self) -> str:
        if not self.notes_ja:
            return (
                "最大化プロファイル蓄積中 — 期待R/PF/Brierで判定できる"
                f"セルがまだありません(n>={MIN_CELL_SAMPLES}から反映)"
            )
        return "\n".join(self.notes_ja)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "cells": {
                f"{symbol}|{timeframe}|{direction}": cell.to_dict()
                for (symbol, timeframe, direction), cell in self.cells.items()
            },
            "per_timeframe": {
                timeframe: cell.to_dict() for timeframe, cell in self.per_timeframe.items()
            },
            "notes_ja": list(self.notes_ja),
        }


def derive_timeframe_maximization(
    entries: Iterable[Mapping[str, object]],
    *,
    now: datetime | None = None,
    symbols: Sequence[str] = MVP_SYMBOLS,
    timeframes: Sequence[str] = MVP_TIMEFRAMES,
) -> TimeframeMaximization:
    now = now or datetime.now(UTC)
    materialized = list(entries)
    cells: dict[tuple[str, str, str], MaximizationCell] = {}
    per_timeframe: dict[str, MaximizationCell] = {}

    for timeframe in timeframes:
        outcomes = evaluate_timeframe_outcomes(materialized, timeframe, symbols=symbols)
        if not outcomes:
            continue
        per_timeframe[timeframe] = derive_maximization_cell("ALL", timeframe, "all", outcomes)
        grouped: dict[tuple[str, str], list[TradeOutcome]] = {}
        for outcome in outcomes:
            if outcome.direction not in ("long", "short"):
                continue
            grouped.setdefault((outcome.symbol, outcome.direction), []).append(outcome)
        for (symbol, direction), group in sorted(grouped.items()):
            cells[(symbol, timeframe, direction)] = derive_maximization_cell(
                symbol, timeframe, direction, group
            )

    profile = TimeframeMaximization(
        generated_at=now.isoformat(),
        cells=cells,
        per_timeframe=per_timeframe,
    )
    profile.notes_ja = _notes_ja(profile)
    return profile


def evaluate_timeframe_outcomes(
    entries: Iterable[Mapping[str, object]],
    timeframe: str,
    *,
    symbols: Sequence[str] = MVP_SYMBOLS,
) -> list[TradeOutcome]:
    allowed = {symbol.upper() for symbol in symbols}
    filtered = [
        entry
        for entry in entries
        if str(entry.get("timeframe", "")) == timeframe
        and str(entry.get("symbol", "")).upper() in allowed
    ]
    horizon = PRIMARY_HORIZON_HOURS.get(timeframe, 24.0)
    return evaluate_trade_outcomes(
        filtered,
        horizon_hours=horizon,
        tolerance_hours=tolerance_for(horizon),
    )


def derive_maximization_cell(
    symbol: str,
    timeframe: str,
    direction: str,
    outcomes: Sequence[TradeOutcome],
) -> MaximizationCell:
    stats = aggregate_expectancy(outcomes, min_samples=MIN_CELL_SAMPLES).to_dict()
    brier, brier_base = _brier_stats(outcomes)
    advanced = _advanced_metrics(outcomes, brier, brier_base)
    score = maximization_score(stats, brier, brier_base, advanced)
    action, factor, block, reason = _policy(
        symbol,
        timeframe,
        direction,
        stats,
        score,
        brier,
        brier_base,
        advanced,
    )
    return MaximizationCell(
        symbol=symbol,
        timeframe=timeframe,
        direction=direction,
        evaluated=_int(stats.get("evaluated")),
        tradable=_int(stats.get("tradable")),
        wins=_int(stats.get("wins")),
        losses=_int(stats.get("losses")),
        win_rate=_float(stats.get("win_rate")),
        expectancy_r=_float(stats.get("expectancy_r")),
        profit_factor_r=_float(stats.get("profit_factor_r")),
        avg_win_r=_float(stats.get("avg_win_r")),
        avg_loss_r=_float(stats.get("avg_loss_r")),
        avg_mfe_r=_float(stats.get("avg_mfe_r")),
        avg_mae_r=_float(stats.get("avg_mae_r")),
        sl_rate=_float(stats.get("sl_rate")),
        avg_path_quality=_float(stats.get("avg_path_quality")),
        brier=brier,
        brier_base=brier_base,
        calibration_error=_float(advanced.get("calibration_error")),
        brier_skill=_float(advanced.get("brier_skill")),
        payoff_ratio=_float(advanced.get("payoff_ratio")),
        max_drawdown_r=_float(advanced.get("max_drawdown_r")),
        recovery_factor=_float(advanced.get("recovery_factor")),
        downside_deviation_r=_float(advanced.get("downside_deviation_r")),
        sortino_r=_float(advanced.get("sortino_r")),
        tail_loss_r=_float(advanced.get("tail_loss_r")),
        stability_score=_float(advanced.get("stability_score")),
        sample_confidence=float(advanced.get("sample_confidence", 0.0) or 0.0),
        score=score,
        action=action,
        factor=factor,
        block=block,
        reason_ja=reason,
        sample_ok=bool(stats.get("sample_ok")),
        min_samples=MIN_CELL_SAMPLES,
    )


def maximization_score(
    stats: Mapping[str, object],
    brier: float | None = None,
    brier_base: float | None = None,
    advanced: Mapping[str, object] | None = None,
) -> float:
    advanced = advanced or {}
    expectancy = _float(stats.get("expectancy_r")) or 0.0
    profit_factor = _float(stats.get("profit_factor_r"))
    win_rate = _float(stats.get("win_rate"))
    quality = _float(stats.get("avg_path_quality"))
    sample_ok = bool(stats.get("sample_ok"))
    max_drawdown = _float(advanced.get("max_drawdown_r"))
    recovery = _float(advanced.get("recovery_factor"))
    sortino = _float(advanced.get("sortino_r"))
    tail_loss = _float(advanced.get("tail_loss_r"))
    stability = _float(advanced.get("stability_score"))
    calibration_error = _float(advanced.get("calibration_error"))
    brier_skill = _float(advanced.get("brier_skill"))
    sample_confidence = _float(advanced.get("sample_confidence")) or 0.0

    pf_bonus = 0.0
    if profit_factor is not None:
        if math.isinf(profit_factor):
            pf_bonus = 0.30
        elif profit_factor >= 1.0:
            pf_bonus = min(0.30, (profit_factor - 1.0) * 0.20)
        else:
            pf_bonus = -min(0.30, (1.0 - profit_factor) * 0.35)

    win_bonus = (win_rate - 0.5) * 0.30 if win_rate is not None else 0.0
    sortino_bonus = min(0.25, max(-0.25, (sortino or 0.0) * 0.08))
    recovery_bonus = min(0.20, max(-0.20, (recovery - 1.0) * 0.08)) if recovery is not None else 0.0
    stability_bonus = (stability - 0.5) * 0.20 if stability is not None else -0.05
    brier_skill_bonus = (
        max(-0.20, min(0.20, brier_skill * 0.20)) if brier_skill is not None else 0.0
    )
    brier_penalty = (
        max(0.0, brier - brier_base) * 0.50 if brier is not None and brier_base is not None else 0.0
    )
    calibration_penalty = (
        max(0.0, calibration_error - 0.10) * 0.40 if calibration_error is not None else 0.05
    )
    drawdown_penalty = min(0.50, max(0.0, (max_drawdown or 0.0) - 3.0) * 0.05)
    tail_loss_penalty = min(0.35, max(0.0, (tail_loss or 0.0) - 1.0) * 0.10)
    quality_penalty = max(0.0, QUALITY_FLOOR - quality) * 0.50 if quality is not None else 0.10
    sample_penalty = (1.0 - sample_confidence) * 0.20 + (0.0 if sample_ok else 0.15)
    score = _clip(expectancy, -2.0, 2.0)
    score += pf_bonus + win_bonus + sortino_bonus + recovery_bonus + stability_bonus
    score += brier_skill_bonus
    score -= (
        brier_penalty
        + calibration_penalty
        + drawdown_penalty
        + tail_loss_penalty
        + quality_penalty
        + sample_penalty
    )
    return round(score, 4)


def save_timeframe_maximization(profile: TimeframeMaximization, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(json_safe(profile.to_dict()), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def maximization_findings(
    profile: TimeframeMaximization,
    *,
    limit: int = 20,
) -> list[dict[str, object]]:
    """Return operational findings sorted by action severity and sample size."""

    findings: list[dict[str, object]] = []
    for cell in profile.cells.values():
        if cell.action not in {"avoid", "dampen", "boost", "quality_guard"}:
            continue
        severity = {
            "avoid": "block",
            "dampen": "warn",
            "quality_guard": "quality_warn",
            "boost": "opportunity",
        }.get(cell.action, "info")
        findings.append(
            {
                "scope": "symbol_timeframe_direction",
                "key": f"{cell.symbol}|{cell.timeframe}|{cell.direction}",
                "label": _cell_label(cell.symbol, cell.timeframe, cell.direction),
                "action": cell.action,
                "severity": severity,
                "factor": cell.factor,
                "block": cell.block,
                "score": cell.score,
                "tradable": cell.tradable,
                "expectancy_r": cell.expectancy_r,
                "profit_factor_r": cell.profit_factor_r,
                "brier": cell.brier,
                "brier_base": cell.brier_base,
                "calibration_error": cell.calibration_error,
                "brier_skill": cell.brier_skill,
                "max_drawdown_r": cell.max_drawdown_r,
                "recovery_factor": cell.recovery_factor,
                "sortino_r": cell.sortino_r,
                "tail_loss_r": cell.tail_loss_r,
                "stability_score": cell.stability_score,
                "sample_confidence": cell.sample_confidence,
                "reason_ja": cell.reason_ja or _action_reason(cell),
            }
        )
    findings.sort(
        key=lambda item: (
            _action_rank(str(item.get("action", ""))),
            -_int(item.get("tradable")),
            _sort_score(item.get("score")),
            str(item.get("key", "")),
        )
    )
    return findings[: max(0, limit)]


def improvement_candidates(
    profile: TimeframeMaximization,
    *,
    limit: int = 10,
) -> list[dict[str, object]]:
    """Translate maximization findings into concrete improvement work items."""

    candidates: list[dict[str, object]] = []
    for finding in maximization_findings(profile, limit=limit * 2):
        action = str(finding.get("action", ""))
        key = str(finding.get("key", ""))
        label = str(finding.get("label", key))
        base = {
            "candidate_id": f"max-{action}-{key}".replace("|", "-").replace(":", "-"),
            "scope": "最大化セル",
            "key": key,
            "source_finding": finding,
        }
        if action == "avoid":
            candidates.append(
                {
                    **base,
                    "priority": "high",
                    "action_type": "max_expectancy_avoid",
                    "title_ja": f"{label}を見送り優先にする",
                    "rationale_ja": str(finding.get("reason_ja", "")),
                    "proposed_change": {
                        "decision_policy": "avoid_new_entries",
                        "confidence_factor": finding.get("factor"),
                    },
                    "validation_ja": "次回以降のOOS期待Rと見送り後の機会損失を比較する",
                    "guardrail_ja": f"有効サンプル{MIN_CELL_SAMPLES}件未満では反映しない",
                }
            )
        elif action == "dampen":
            candidates.append(
                {
                    **base,
                    "priority": "medium",
                    "action_type": "max_expectancy_dampen",
                    "title_ja": f"{label}の確信度を減衰",
                    "rationale_ja": str(finding.get("reason_ja", "")),
                    "proposed_change": {
                        "confidence_factor": finding.get("factor"),
                    },
                    "validation_ja": "補正後Brierと期待Rが補正前より悪化しないか確認する",
                    "guardrail_ja": "方向反転はしない。減衰のみ",
                }
            )
        elif action == "quality_guard":
            candidates.append(
                {
                    **base,
                    "priority": "medium",
                    "action_type": "max_path_quality",
                    "title_ja": f"{label}の経路データ品質を補強",
                    "rationale_ja": str(finding.get("reason_ja", "")),
                    "proposed_change": {
                        "path_source": "use_high_low_ohlc_or_tick_bars",
                    },
                    "validation_ja": "high/low付き経路でTP/SL先着を再採点する",
                    "guardrail_ja": "低品質データだけでTP/SLや見送りを最適化しない",
                }
            )
        elif action == "boost":
            candidates.append(
                {
                    **base,
                    "priority": "low",
                    "action_type": "max_expectancy_boost",
                    "title_ja": f"{label}の確信度を小さく強化",
                    "rationale_ja": str(finding.get("reason_ja", "")),
                    "proposed_change": {
                        "confidence_factor": finding.get("factor"),
                    },
                    "validation_ja": "強化後もPF・期待R・Brierが維持されるかpaper監視する",
                    "guardrail_ja": f"強化は最大{BOOST_FACTOR_MAX:.2f}倍まで",
                }
            )
        if len(candidates) >= limit:
            break
    return candidates


def build_monitoring_snapshot(
    profile: TimeframeMaximization,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    else:
        generated_at = generated_at.astimezone(UTC)

    findings = maximization_findings(profile)
    candidates = improvement_candidates(profile)
    action_counts: dict[str, int] = {}
    for cell in profile.cells.values():
        action_counts[cell.action] = action_counts.get(cell.action, 0) + 1
    mature_cells = [cell for cell in profile.cells.values() if cell.tradable >= MIN_CELL_SAMPLES]
    status = STATUS_OK
    if any(cell.action == "avoid" for cell in mature_cells):
        status = STATUS_FAIL
    elif any(cell.action in {"dampen", "quality_guard"} for cell in mature_cells):
        status = STATUS_WARN
    elif not mature_cells:
        status = STATUS_WARN

    return {
        "schema": 1,
        "generated_at": generated_at.isoformat(),
        "status": status,
        "exit_code": 1 if status == STATUS_FAIL else 0,
        "summary": {
            "cell_count": len(profile.cells),
            "mature_cell_count": len(mature_cells),
            "action_counts": dict(sorted(action_counts.items())),
            "best_cells": [
                cell.to_dict()
                for cell in sorted(
                    mature_cells,
                    key=lambda item: (-item.score, -item.tradable, item.symbol),
                )[:5]
            ],
            "worst_cells": [
                cell.to_dict()
                for cell in sorted(
                    mature_cells,
                    key=lambda item: (item.score, -item.tradable, item.symbol),
                )[:5]
            ],
        },
        "findings": findings,
        "improvement_candidates": candidates,
        "profile": profile.to_dict(),
    }


def _advanced_metrics(
    outcomes: Sequence[TradeOutcome],
    brier: float | None,
    brier_base: float | None,
) -> dict[str, float | None]:
    tradable = [
        outcome for outcome in outcomes if outcome.tradable and outcome.realized_r is not None
    ]
    r_values = [float(outcome.realized_r) for outcome in tradable if outcome.realized_r is not None]
    if not r_values:
        return {
            "calibration_error": None,
            "brier_skill": None,
            "payoff_ratio": None,
            "max_drawdown_r": None,
            "recovery_factor": None,
            "downside_deviation_r": None,
            "sortino_r": None,
            "tail_loss_r": None,
            "stability_score": None,
            "sample_confidence": 0.0,
        }

    wins = [value for value in r_values if value > 0]
    losses = [value for value in r_values if value < 0]
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    payoff_ratio = (
        abs(avg_win / avg_loss)
        if avg_win is not None and avg_loss is not None and avg_loss < 0
        else (math.inf if avg_win is not None and avg_win > 0 else None)
    )
    max_drawdown = _max_drawdown(r_values)
    total_r = sum(r_values)
    recovery_factor = (
        total_r / max_drawdown if max_drawdown > 0 else (math.inf if total_r > 0 else 0.0)
    )
    downside_deviation = _downside_deviation(r_values)
    expectancy = sum(r_values) / len(r_values)
    sortino = (
        expectancy / downside_deviation
        if downside_deviation > 0
        else (math.inf if expectancy > 0 else 0.0)
    )
    worst_count = max(1, math.ceil(len(r_values) * 0.10))
    tail_loss = abs(sum(sorted(r_values)[:worst_count]) / worst_count)
    stability = _stability_score(r_values)
    calibration_error = _calibration_error(tradable)
    brier_skill = _brier_skill(brier, brier_base)
    sample_confidence = min(1.0, math.sqrt(len(r_values) / BOOST_MIN_SAMPLES))
    return {
        "calibration_error": _round(calibration_error),
        "brier_skill": _round(brier_skill),
        "payoff_ratio": _round(payoff_ratio),
        "max_drawdown_r": _round(max_drawdown),
        "recovery_factor": _round(recovery_factor),
        "downside_deviation_r": _round(downside_deviation),
        "sortino_r": _round(sortino),
        "tail_loss_r": _round(tail_loss),
        "stability_score": _round(stability),
        "sample_confidence": _round(sample_confidence),
    }


def _policy(
    symbol: str,
    timeframe: str,
    direction: str,
    stats: Mapping[str, object],
    score: float,
    brier: float | None,
    brier_base: float | None,
    advanced: Mapping[str, object],
) -> tuple[str, float, bool, str]:
    tradable = _int(stats.get("tradable"))
    expectancy = _float(stats.get("expectancy_r"))
    profit_factor = _float(stats.get("profit_factor_r"))
    avg_mfe = _float(stats.get("avg_mfe_r"))
    avg_mae = _float(stats.get("avg_mae_r"))
    quality = _float(stats.get("avg_path_quality"))
    stability = _float(advanced.get("stability_score"))
    calibration_error = _float(advanced.get("calibration_error"))
    sortino = _float(advanced.get("sortino_r"))
    recovery = _float(advanced.get("recovery_factor"))
    max_drawdown = _float(advanced.get("max_drawdown_r"))
    label = _cell_label(symbol, timeframe, direction)

    if tradable < MIN_CELL_SAMPLES:
        return "collect_samples", 1.0, False, ""
    if quality is not None and quality < QUALITY_FLOOR:
        return (
            "quality_guard",
            1.0,
            False,
            f"{label}: 経路品質{quality:.0%}が低く、TP/SL到達順の信頼度が不足",
        )
    if expectancy is not None and expectancy <= 0:
        return (
            "avoid",
            BLOCK_FACTOR,
            True,
            f"{label}: 期待R {expectancy:+.2f}R、最大化スコア{score:+.2f}。見送り優先",
        )
    if score < -0.05:
        return (
            "avoid",
            BLOCK_FACTOR,
            True,
            f"{label}: 最大化スコア{score:+.2f}が低く、見送り優先",
        )
    if profit_factor is not None and profit_factor < WEAK_PROFIT_FACTOR:
        return (
            "dampen",
            DAMPEN_FACTOR,
            False,
            f"{label}: PF {profit_factor:.2f}が薄い。確信度を×{DAMPEN_FACTOR:.2f}に減衰",
        )
    if stability is not None and stability < STABILITY_FLOOR:
        return (
            "dampen",
            DAMPEN_FACTOR,
            False,
            f"{label}: 成績安定性{stability:.0%}が低い。確信度を減衰",
        )
    if calibration_error is not None and calibration_error > CALIBRATION_ERROR_CEILING:
        return (
            "dampen",
            DAMPEN_FACTOR,
            False,
            f"{label}: 確信度校正誤差{calibration_error:.2f}が大きい。確信度を減衰",
        )
    if avg_mfe is not None and avg_mae is not None and avg_mfe <= avg_mae:
        return (
            "dampen",
            DAMPEN_FACTOR,
            False,
            f"{label}: 平均MFE {avg_mfe:.2f}R <= 平均MAE {avg_mae:.2f}R。確信度を減衰",
        )
    if sortino is not None and sortino < SORTINO_FLOOR:
        return (
            "dampen",
            DAMPEN_FACTOR,
            False,
            f"{label}: Sortino {sortino:.2f}が低い。下方リスクに対して期待値が薄い",
        )
    if (
        recovery is not None
        and max_drawdown is not None
        and max_drawdown > 0
        and recovery < RECOVERY_FACTOR_FLOOR
    ):
        return (
            "dampen",
            DAMPEN_FACTOR,
            False,
            f"{label}: 回復係数{recovery:.2f}が低い。DDに対して利益が不足",
        )
    brier_ok = brier is None or brier_base is None or brier <= brier_base
    advanced_ok = (
        (stability is None or stability >= STABILITY_FLOOR)
        and (calibration_error is None or calibration_error <= CALIBRATION_ERROR_CEILING)
        and (sortino is None or sortino >= SORTINO_FLOOR)
        and (recovery is None or recovery >= RECOVERY_FACTOR_FLOOR)
    )
    if (
        tradable >= BOOST_MIN_SAMPLES
        and expectancy is not None
        and expectancy >= EXPECTANCY_GOOD_R
        and (profit_factor is None or profit_factor >= GOOD_PROFIT_FACTOR)
        and brier_ok
        and advanced_ok
        and score >= 0.25
    ):
        factor = min(BOOST_FACTOR_MAX, round(1.0 + min(0.10, score * 0.08), 2))
        return (
            "boost",
            factor,
            False,
            f"{label}: 期待R {expectancy:+.2f}R、最大化スコア{score:+.2f}。確信度を×{factor:.2f}",
        )
    return "hold", 1.0, False, ""


def _brier_stats(outcomes: Sequence[TradeOutcome]) -> tuple[float | None, float | None]:
    tradable = [
        outcome for outcome in outcomes if outcome.tradable and outcome.realized_r is not None
    ]
    if not tradable:
        return None, None
    labels = [1.0 if float(outcome.realized_r or 0.0) > 0 else 0.0 for outcome in tradable]
    base = sum(labels) / len(labels)
    brier = sum(
        (outcome.conviction / 100.0 - label) ** 2 for outcome, label in zip(tradable, labels)
    ) / len(labels)
    brier_base = sum((base - label) ** 2 for label in labels) / len(labels)
    return round(brier, 4), round(brier_base, 4)


def _notes_ja(profile: TimeframeMaximization) -> list[str]:
    cells = [cell for cell in profile.cells.values() if cell.tradable > 0]
    if not cells:
        return []
    notes = ["最大化プロファイル(期待R/PF/Brier/経路品質):"]
    active = [
        cell for cell in cells if cell.action in {"avoid", "dampen", "boost", "quality_guard"}
    ]
    if active:
        priority = {"avoid": 0, "dampen": 1, "quality_guard": 2, "boost": 3}
        active.sort(key=lambda cell: (priority.get(cell.action, 9), cell.score))
        for cell in active[:4]:
            action_ja = {
                "avoid": "見送り",
                "dampen": "減衰",
                "quality_guard": "品質警戒",
                "boost": "強化",
            }.get(cell.action, cell.action)
            notes.append(
                f"・{action_ja}: {_cell_label(cell.symbol, cell.timeframe, cell.direction)}"
                f" 期待R {_fmt_signed(cell.expectancy_r, 'R')}"
                f" / PF {_fmt_number(cell.profit_factor_r)}"
                f" / score {cell.score:+.2f}"
                f" / n={cell.tradable}"
            )
    else:
        total = sum(cell.tradable for cell in cells)
        notes.append(f"・反映対象セルなし。採点済みサンプル n={total}")
    return notes


def _cell_label(symbol: str, timeframe: str, direction: str) -> str:
    if direction == "all":
        return f"{timeframe}全体"
    direction_ja = {"long": "ロング", "short": "ショート"}.get(direction, direction)
    return f"{symbol} {timeframe} {direction_ja}"


def _fmt_signed(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}{suffix}"


def _fmt_number(value: float | None) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "∞"
    return f"{value:.2f}"


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    if math.isinf(value):
        return value
    if not math.isfinite(value):
        return None
    return round(value, digits)


def _max_drawdown(r_values: Sequence[float]) -> float:
    peak = 0.0
    equity = 0.0
    max_drawdown = 0.0
    for value in r_values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return max_drawdown


def _downside_deviation(r_values: Sequence[float]) -> float:
    downside = [min(0.0, value) ** 2 for value in r_values]
    return math.sqrt(sum(downside) / len(downside)) if downside else 0.0


def _stability_score(r_values: Sequence[float]) -> float | None:
    if len(r_values) < MIN_CELL_SAMPLES:
        return None
    midpoint = len(r_values) // 2
    first = _mean(r_values[:midpoint])
    second = _mean(r_values[midpoint:])
    overall = _mean(r_values)
    if first is None or second is None or overall is None:
        return None
    denominator = max(abs(overall), 0.25)
    gap = abs(first - second) / denominator
    sign_penalty = 0.25 if first * second < 0 else 0.0
    return max(0.0, min(1.0, 1.0 - gap - sign_penalty))


def _calibration_error(outcomes: Sequence[TradeOutcome]) -> float | None:
    if not outcomes:
        return None
    total = 0
    weighted_error = 0.0
    for low, high in CALIBRATION_BINS:
        bucket = [outcome for outcome in outcomes if low <= outcome.conviction / 100.0 < high]
        if not bucket:
            continue
        avg_probability = sum(outcome.conviction / 100.0 for outcome in bucket) / len(bucket)
        hit_rate = sum(1 for outcome in bucket if float(outcome.realized_r or 0.0) > 0) / len(
            bucket
        )
        weighted_error += len(bucket) * abs(avg_probability - hit_rate)
        total += len(bucket)
    return weighted_error / total if total else None


def _brier_skill(brier: float | None, brier_base: float | None) -> float | None:
    if brier is None or brier_base is None:
        return None
    if brier_base == 0:
        return 1.0 if brier == 0 else -1.0
    return 1.0 - brier / brier_base


def _float(value: object) -> float | None:
    if isinstance(value, int | float):
        numeric = float(value)
        return numeric if math.isfinite(numeric) or math.isinf(numeric) else None
    return None


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float) and math.isfinite(float(value)):
        return int(value)
    return 0


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _action_rank(action: str) -> int:
    return {
        "avoid": 0,
        "dampen": 1,
        "quality_guard": 2,
        "boost": 3,
    }.get(action, 9)


def _sort_score(value: object) -> float:
    numeric = _float(value)
    if numeric is None:
        return 1_000_000.0
    return numeric


def _action_reason(cell: MaximizationCell) -> str:
    label = _cell_label(cell.symbol, cell.timeframe, cell.direction)
    if cell.action == "boost":
        return f"{label}: 最大化スコア{cell.score:+.2f}。確信度を×{cell.factor:.2f}"
    if cell.action == "dampen":
        return f"{label}: 最大化スコア{cell.score:+.2f}。確信度を×{cell.factor:.2f}"
    if cell.action == "quality_guard":
        return f"{label}: 経路品質が低く、最大化判断は品質警戒"
    if cell.action == "avoid":
        return f"{label}: 最大化スコア{cell.score:+.2f}。見送り優先"
    return ""
