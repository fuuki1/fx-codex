"""MFE/MAE/TP/SL based expectancy audit for briefing journal decisions.

The existing direction journal answers "was the direction right?".  This module
answers the trade question: "did the planned entry/SL/TP have positive
expectancy?".  It uses the same JSONL journal and the subsequent close series,
so it works offline without new market data.  Because close-only paths cannot
prove intrabar touch order, every result carries path-quality flags and sample
guards.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
import json
import math
from pathlib import Path

from .journal import DEFAULT_HORIZON_HOURS, DEFAULT_TOLERANCE_HOURS
from .market import WEEKEND_CLOSURE, open_hours_between

MIN_PATH_POINTS = 3
MIN_PATH_COVERAGE = 0.50
MIN_PATH_QUALITY = 0.35
MIN_EXPECTANCY_SAMPLES = 20
MIN_GROUP_EXPECTANCY_SAMPLES = 12
CLOSE_ONLY_QUALITY_CAP = 0.70
PARTIAL_OHLC_QUALITY_CAP = 0.85
OHLC_QUALITY_CAP = 0.95
POINTS_FOR_FULL_DENSITY = 12
WEAK_PROFIT_FACTOR = 1.05
QUALITY_WARN_THRESHOLD = 0.55
EXPECTANCY_BLOCK_FACTOR = 0.45
EXPECTANCY_WEAK_FACTOR = 0.75
MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R = 0.05
DEFAULT_TP1_R_CANDIDATES = (0.75, 1.0, 1.25)
DEFAULT_TP2_R_CANDIDATES = (1.5, 2.0, 2.5)
IMPROVEMENT_REGISTRY_SCHEMA = 1
EXPECTANCY_CANDIDATE_ACTION_TYPES = {
    "collect_samples",
    "improve_path_quality",
    "expectancy_guard",
    "tp_sl_entry_retest",
}
VARIANT_CANDIDATE_ACTION_TYPES = {"tp_sl_variant_paper_test"}

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
CONFIDENCE_BINS = ((0, 25), (25, 50), (50, 75), (75, 101))
READY_SEEN_BY_PRIORITY = {"high": 2, "medium": 3, "low": 5}
APPROVAL_PRESERVED_STAGES = {"approved", "rejected", "auto_paused"}
TRUSTED_POST_PREDICTION_OHLC_SCOPES = {
    "closed_bar_after_prediction",
    "lower_timeframe_closed_bar",
    "post_prediction_interval",
}


@dataclass(frozen=True)
class PricePathPoint:
    ts: datetime
    close: float
    high: float | None = None
    low: float | None = None
    range_scope: str = ""
    rejected_range: bool = False

    @property
    def has_range(self) -> bool:
        return self.high is not None and self.low is not None


@dataclass(frozen=True)
class TradeOutcome:
    """One journal decision scored as a hypothetical full-position trade."""

    symbol: str
    direction: str
    ts: str
    horizon_hours: float
    conviction: int
    data_quality: float | None
    entry: float | None = None
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    target_policy_id: str | None = None
    target_policy_scope: str = ""
    target_policy_key: str = ""
    atr: float | None = None
    risk_distance: float | None = None
    terminal_price: float | None = None
    terminal_r: float | None = None
    mfe: float | None = None
    mae: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    tp1_hit: bool = False
    tp2_hit: bool = False
    sl_hit: bool = False
    first_touch: str = "none"
    first_touch_ts: str | None = None
    realized_r: float | None = None
    path_points: int = 0
    path_start: str | None = None
    path_end: str | None = None
    path_source: str = "close"
    path_coverage: float = 0.0
    path_quality: float = 0.0
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tradable(self) -> bool:
        return self.realized_r is not None and self.path_quality >= MIN_PATH_QUALITY

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "ts": self.ts,
            "horizon_hours": self.horizon_hours,
            "conviction": self.conviction,
            "data_quality": self.data_quality,
            "entry": self.entry,
            "stop": self.stop,
            "target1": self.target1,
            "target2": self.target2,
            "target_policy_id": self.target_policy_id,
            "target_policy_scope": self.target_policy_scope,
            "target_policy_key": self.target_policy_key,
            "atr": self.atr,
            "risk_distance": self.risk_distance,
            "terminal_price": self.terminal_price,
            "terminal_r": self.terminal_r,
            "mfe": self.mfe,
            "mae": self.mae,
            "mfe_r": self.mfe_r,
            "mae_r": self.mae_r,
            "tp1_hit": self.tp1_hit,
            "tp2_hit": self.tp2_hit,
            "sl_hit": self.sl_hit,
            "first_touch": self.first_touch,
            "first_touch_ts": self.first_touch_ts,
            "realized_r": self.realized_r,
            "path_points": self.path_points,
            "path_start": self.path_start,
            "path_end": self.path_end,
            "path_source": self.path_source,
            "path_coverage": self.path_coverage,
            "path_quality": self.path_quality,
            "quality_flags": list(self.quality_flags),
            "tradable": self.tradable,
        }


@dataclass(frozen=True)
class ExpectancyStats:
    evaluated: int = 0
    tradable: int = 0
    low_quality: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    expectancy_r: float | None = None
    avg_win_r: float | None = None
    avg_loss_r: float | None = None
    profit_factor_r: float | None = None
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    tp1_rate: float | None = None
    tp2_rate: float | None = None
    sl_rate: float | None = None
    avg_path_quality: float | None = None
    sample_ok: bool = False
    min_samples: int = MIN_EXPECTANCY_SAMPLES

    def to_dict(self) -> dict:
        return {
            "evaluated": self.evaluated,
            "tradable": self.tradable,
            "low_quality": self.low_quality,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "expectancy_r": self.expectancy_r,
            "avg_win_r": self.avg_win_r,
            "avg_loss_r": self.avg_loss_r,
            "profit_factor_r": self.profit_factor_r,
            "avg_mfe_r": self.avg_mfe_r,
            "avg_mae_r": self.avg_mae_r,
            "tp1_rate": self.tp1_rate,
            "tp2_rate": self.tp2_rate,
            "sl_rate": self.sl_rate,
            "avg_path_quality": self.avg_path_quality,
            "sample_ok": self.sample_ok,
            "min_samples": self.min_samples,
        }


@dataclass(frozen=True)
class TradeOutcomeHealthCheck:
    name: str
    status: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class TradeOutcomeHealthReport:
    checks: list[TradeOutcomeHealthCheck]

    @property
    def status(self) -> str:
        if any(check.status == STATUS_FAIL for check in self.checks):
            return STATUS_FAIL
        if any(check.status == STATUS_WARN for check in self.checks):
            return STATUS_WARN
        return STATUS_OK

    @property
    def exit_code(self) -> int:
        return 1 if self.status == STATUS_FAIL else 0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class TradeImprovementCandidate:
    candidate_id: str
    scope: str
    key: str
    priority: str
    action_type: str
    title_ja: str
    rationale_ja: str
    proposed_change: dict[str, object]
    validation_ja: str
    guardrail_ja: str
    source_finding: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "scope": self.scope,
            "key": self.key,
            "priority": self.priority,
            "action_type": self.action_type,
            "title_ja": self.title_ja,
            "rationale_ja": self.rationale_ja,
            "proposed_change": dict(self.proposed_change),
            "validation_ja": self.validation_ja,
            "guardrail_ja": self.guardrail_ja,
            "source_finding": dict(self.source_finding),
        }


@dataclass(frozen=True)
class ExpectancyAdjustment:
    action: str = "none"
    factor: float = 1.0
    block: bool = False
    reason_ja: str = ""
    matched_scope: str = ""
    matched_key: str = ""
    severity: str = ""
    tradable: int = 0
    min_samples: int = 0
    expectancy_r: float | None = None
    profit_factor_r: float | None = None

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "factor": self.factor,
            "block": self.block,
            "reason_ja": self.reason_ja,
            "matched_scope": self.matched_scope,
            "matched_key": self.matched_key,
            "severity": self.severity,
            "tradable": self.tradable,
            "min_samples": self.min_samples,
            "expectancy_r": self.expectancy_r,
            "profit_factor_r": self.profit_factor_r,
        }


@dataclass(frozen=True)
class TradeVariantScore:
    variant_id: str
    target1_r: float
    target2_r: float
    tradable: int
    sample_ok: bool
    expectancy_r: float | None
    profit_factor_r: float | None
    tp1_rate: float | None
    tp2_rate: float | None
    sl_rate: float | None
    avg_mfe_r: float | None
    avg_mae_r: float | None
    avg_path_quality: float | None
    delta_expectancy_r: float | None
    delta_profit_factor_r: float | None
    recommendation: str
    reason_ja: str

    def to_dict(self) -> dict:
        return {
            "variant_id": self.variant_id,
            "target1_r": self.target1_r,
            "target2_r": self.target2_r,
            "tradable": self.tradable,
            "sample_ok": self.sample_ok,
            "expectancy_r": self.expectancy_r,
            "profit_factor_r": self.profit_factor_r,
            "tp1_rate": self.tp1_rate,
            "tp2_rate": self.tp2_rate,
            "sl_rate": self.sl_rate,
            "avg_mfe_r": self.avg_mfe_r,
            "avg_mae_r": self.avg_mae_r,
            "avg_path_quality": self.avg_path_quality,
            "delta_expectancy_r": self.delta_expectancy_r,
            "delta_profit_factor_r": self.delta_profit_factor_r,
            "recommendation": self.recommendation,
            "reason_ja": self.reason_ja,
        }


@dataclass(frozen=True)
class ApprovedTargetPolicy:
    candidate_id: str
    scope: str
    key: str
    target1_r: float
    target2_r: float
    priority: str = ""
    reason_ja: str = ""
    approved_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "scope": self.scope,
            "key": self.key,
            "target1_r": self.target1_r,
            "target2_r": self.target2_r,
            "priority": self.priority,
            "reason_ja": self.reason_ja,
            "approved_at": self.approved_at,
        }


def evaluate_trade_outcomes(
    entries: Iterable[Mapping[str, object]],
    *,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    tolerance_hours: float = DEFAULT_TOLERANCE_HOURS,
    min_path_points: int = MIN_PATH_POINTS,
    target1_r: float | None = None,
    target2_r: float | None = None,
) -> list[TradeOutcome]:
    materialized = list(entries)
    prices: dict[str, list[PricePathPoint]] = {}
    parsed_entries: list[tuple[datetime, Mapping[str, object]]] = []
    for entry in materialized:
        ts = _parse_ts(entry.get("ts"))
        if ts is None:
            continue
        parsed_entries.append((ts, entry))
        point = _price_path_point(ts, entry)
        if point is not None:
            prices.setdefault(str(entry.get("symbol", "")).upper(), []).append(point)
    for series in prices.values():
        series.sort(key=lambda point: point.ts)
    price_times = {symbol: [point.ts for point in series] for symbol, series in prices.items()}

    outcomes: list[TradeOutcome] = []
    for ts, entry in parsed_entries:
        direction = str(entry.get("direction", ""))
        if direction not in ("long", "short"):
            continue
        symbol = str(entry.get("symbol", "")).upper()
        entry_price = _float(entry.get("close"))
        stop = _float(entry.get("stop"))
        target1 = _float(entry.get("target1"))
        target2 = _float(entry.get("target2"))
        target_policy_id, target_policy_scope, target_policy_key = _target_policy_meta(entry)
        atr = _float(entry.get("atr"))
        conviction = _int(entry.get("conviction"))
        data_quality = _float(entry.get("data_quality"))
        future = _future_path(
            prices.get(symbol, []),
            price_times.get(symbol, []),
            ts,
            horizon_hours,
            tolerance_hours,
        )

        missing_flags: list[str] = []
        if entry_price is None:
            missing_flags.append("missing_entry")
        if stop is None or target1 is None:
            missing_flags.append("missing_risk_levels")
        risk_distance = (
            abs(entry_price - stop)
            if entry_price is not None and stop is not None and stop != entry_price
            else None
        )
        if risk_distance is None or risk_distance <= 0:
            missing_flags.append("invalid_risk_distance")
        if not future:
            missing_flags.append("no_future_prices")
        if target1_r is not None and target2_r is not None and target2_r <= target1_r:
            missing_flags.append("invalid_target_variant")
        if missing_flags:
            outcomes.append(
                TradeOutcome(
                    symbol=symbol,
                    direction=direction,
                    ts=ts.isoformat(),
                    horizon_hours=horizon_hours,
                    conviction=conviction,
                    data_quality=data_quality,
                    entry=entry_price,
                    stop=stop,
                    target1=target1,
                    target2=target2,
                    target_policy_id=target_policy_id,
                    target_policy_scope=target_policy_scope,
                    target_policy_key=target_policy_key,
                    atr=atr,
                    risk_distance=risk_distance,
                    first_touch="unscored",
                    quality_flags=tuple(dict.fromkeys(missing_flags)),
                )
            )
            continue

        assert entry_price is not None and risk_distance is not None
        if target1_r is not None:
            target1 = _target_price(direction, entry_price, risk_distance, target1_r)
        if target2_r is not None:
            target2 = _target_price(direction, entry_price, risk_distance, target2_r)
        tp1_r_value = _target_r(direction, entry_price, risk_distance, target1, default=1.0)
        tp2_r_value = _target_r(direction, entry_price, risk_distance, target2, default=2.0)
        terminal = future[-1]
        terminal_ts, terminal_price = terminal.ts, terminal.close
        terminal_r = _signed_move(direction, entry_price, terminal_price) / risk_distance
        first_touch = "none"
        first_touch_ts = None
        tp1_hit = tp2_hit = sl_hit = False
        ambiguous_intrabar = False
        active_path: list[PricePathPoint] = []
        for point in future:
            active_path.append(point)
            touch = _touch(direction, point, stop, target1, target2)
            tp1_hit = tp1_hit or touch in ("tp1", "tp2", "ambiguous_sl_tp")
            tp2_hit = tp2_hit or touch == "tp2"
            sl_hit = sl_hit or touch in ("sl", "ambiguous_sl_tp")
            ambiguous_intrabar = ambiguous_intrabar or touch == "ambiguous_sl_tp"
            if touch != "none":
                first_touch = touch
                first_touch_ts = point.ts.isoformat()
                break

        mfe = max(_favorable_move(direction, entry_price, point) for point in active_path)
        mae = max(0.0, max(_adverse_move(direction, entry_price, point) for point in active_path))

        realized_r = _realized_r(first_touch, terminal_r, tp1_r_value, tp2_r_value)
        quality, flags, path_source = _path_quality(ts, future, horizon_hours, min_path_points)
        if ambiguous_intrabar:
            flags = tuple(dict.fromkeys((*flags, "ambiguous_intrabar_touch")))
        outcomes.append(
            TradeOutcome(
                symbol=symbol,
                direction=direction,
                ts=ts.isoformat(),
                horizon_hours=horizon_hours,
                conviction=conviction,
                data_quality=data_quality,
                entry=entry_price,
                stop=stop,
                target1=target1,
                target2=target2,
                target_policy_id=target_policy_id,
                target_policy_scope=target_policy_scope,
                target_policy_key=target_policy_key,
                atr=atr,
                risk_distance=round(risk_distance, 8),
                terminal_price=terminal_price,
                terminal_r=round(terminal_r, 4),
                mfe=round(mfe, 8),
                mae=round(mae, 8),
                mfe_r=round(mfe / risk_distance, 4),
                mae_r=round(mae / risk_distance, 4),
                tp1_hit=tp1_hit,
                tp2_hit=tp2_hit,
                sl_hit=sl_hit,
                first_touch=first_touch,
                first_touch_ts=first_touch_ts,
                realized_r=round(realized_r, 4),
                path_points=len(future),
                path_start=future[0].ts.isoformat(),
                path_end=terminal_ts.isoformat(),
                path_source=path_source,
                path_coverage=round(_coverage(ts, terminal_ts, horizon_hours), 4),
                path_quality=quality,
                quality_flags=flags,
            )
        )
    return outcomes


def summarize_expectancy(
    outcomes: Sequence[TradeOutcome],
    *,
    min_samples: int = MIN_EXPECTANCY_SAMPLES,
    group_min_samples: int = MIN_GROUP_EXPECTANCY_SAMPLES,
) -> dict:
    return {
        "schema": 1,
        "overall": aggregate_expectancy(outcomes, min_samples=min_samples).to_dict(),
        "by_symbol": _aggregate_by(
            outcomes, lambda outcome: outcome.symbol, min_samples=group_min_samples
        ),
        "by_direction": _aggregate_by(
            outcomes, lambda outcome: outcome.direction, min_samples=group_min_samples
        ),
        "by_symbol_direction": _aggregate_by(
            outcomes,
            lambda outcome: f"{outcome.symbol}:{outcome.direction}",
            min_samples=group_min_samples,
        ),
        "by_target_policy": _aggregate_by(
            outcomes,
            lambda outcome: outcome.target_policy_id or "",
            min_samples=group_min_samples,
        ),
        "by_confidence": _aggregate_by(outcomes, _confidence_label, min_samples=group_min_samples),
        "quality": quality_summary(outcomes),
    }


def aggregate_expectancy(
    outcomes: Sequence[TradeOutcome],
    *,
    min_samples: int = MIN_EXPECTANCY_SAMPLES,
) -> ExpectancyStats:
    evaluated = len(outcomes)
    usable = [outcome for outcome in outcomes if outcome.realized_r is not None]
    tradable = [outcome for outcome in usable if outcome.tradable]
    r_values = [float(outcome.realized_r) for outcome in tradable if outcome.realized_r is not None]
    wins = sum(1 for value in r_values if value > 0)
    losses = sum(1 for value in r_values if value < 0)
    gross_win = sum(value for value in r_values if value > 0)
    gross_loss = abs(sum(value for value in r_values if value < 0))
    mfe_values = [float(outcome.mfe_r) for outcome in tradable if outcome.mfe_r is not None]
    mae_values = [float(outcome.mae_r) for outcome in tradable if outcome.mae_r is not None]
    qualities = [outcome.path_quality for outcome in usable]
    return ExpectancyStats(
        evaluated=evaluated,
        tradable=len(tradable),
        low_quality=evaluated - len(tradable),
        wins=wins,
        losses=losses,
        win_rate=_round(wins / len(r_values)) if r_values else None,
        expectancy_r=_round(_mean(r_values)),
        avg_win_r=_round(_mean([value for value in r_values if value > 0])),
        avg_loss_r=_round(_mean([value for value in r_values if value < 0])),
        profit_factor_r=(
            _round(gross_win / gross_loss)
            if gross_loss > 0
            else (None if gross_win <= 0 else float("inf"))
        ),
        avg_mfe_r=_round(_mean(mfe_values)),
        avg_mae_r=_round(_mean(mae_values)),
        tp1_rate=(
            _round(sum(1 for outcome in tradable if outcome.tp1_hit) / len(tradable))
            if tradable
            else None
        ),
        tp2_rate=(
            _round(sum(1 for outcome in tradable if outcome.tp2_hit) / len(tradable))
            if tradable
            else None
        ),
        sl_rate=(
            _round(sum(1 for outcome in tradable if outcome.sl_hit) / len(tradable))
            if tradable
            else None
        ),
        avg_path_quality=_round(_mean(qualities)),
        sample_ok=len(tradable) >= min_samples,
        min_samples=min_samples,
    )


def quality_summary(outcomes: Sequence[TradeOutcome]) -> dict:
    flags: dict[str, int] = {}
    for outcome in outcomes:
        for flag in outcome.quality_flags:
            flags[flag] = flags.get(flag, 0) + 1
    scored = [outcome for outcome in outcomes if outcome.realized_r is not None]
    return {
        "evaluated": len(outcomes),
        "scored": len(scored),
        "low_quality": sum(1 for outcome in outcomes if not outcome.tradable),
        "avg_path_quality": _round(_mean([outcome.path_quality for outcome in scored])),
        "flags": dict(sorted(flags.items())),
    }


def expectancy_findings(summary: Mapping[str, object], *, limit: int = 5) -> list[dict]:
    candidates: list[dict] = []
    overall = summary.get("overall")
    if isinstance(overall, Mapping):
        finding = _expectancy_finding("全体", "", overall)
        if finding is not None:
            candidates.append(finding)
    for group_key, scope in (
        ("by_symbol_direction", "通貨ペア×方向"),
        ("by_symbol", "通貨ペア"),
        ("by_direction", "方向"),
        ("by_confidence", "確信度"),
    ):
        group = summary.get(group_key)
        if not isinstance(group, Mapping):
            continue
        for key, stats in group.items():
            if isinstance(stats, Mapping):
                finding = _expectancy_finding(scope, str(key), stats)
                if finding is not None:
                    candidates.append(finding)
    quality = summary.get("quality")
    if isinstance(quality, Mapping):
        finding = _quality_finding(quality)
        if finding is not None:
            candidates.append(finding)
    candidates.sort(key=lambda item: (int(item["rank"]), -int(item["tradable"]), item["label"]))
    return candidates[: max(0, limit)]


def improvement_candidates(
    summary: Mapping[str, object],
    *,
    limit: int = 5,
) -> list[TradeImprovementCandidate]:
    output: list[TradeImprovementCandidate] = []
    for index, finding in enumerate(expectancy_findings(summary, limit=limit), start=1):
        candidate = _candidate_from_finding(finding, index)
        if candidate is not None:
            output.append(candidate)
    return output


def variant_improvement_candidates(
    variant_report: Mapping[str, object],
    *,
    limit: int = 3,
) -> list[TradeImprovementCandidate]:
    output: list[TradeImprovementCandidate] = []
    variants = variant_report.get("variants")
    baseline = variant_report.get("baseline")
    baseline_overall = baseline.get("overall") if isinstance(baseline, Mapping) else {}
    if isinstance(variants, Sequence):
        for raw in [item for item in variants if isinstance(item, Mapping)]:
            if raw.get("recommendation") != "paper_test":
                continue
            output.append(_candidate_from_variant(raw, baseline_overall, len(output) + 1))
            if len(output) >= limit:
                return output
    cells = variant_report.get("cells")
    if isinstance(cells, Mapping):
        for scope, grouped in cells.items():
            if not isinstance(grouped, Mapping):
                continue
            for key, cell_report in grouped.items():
                if not isinstance(cell_report, Mapping):
                    continue
                cell_variants = cell_report.get("variants")
                cell_baseline = cell_report.get("baseline")
                if not isinstance(cell_variants, Sequence):
                    continue
                for raw in [item for item in cell_variants if isinstance(item, Mapping)]:
                    if raw.get("recommendation") != "paper_test":
                        continue
                    output.append(
                        _candidate_from_variant(
                            raw,
                            cell_baseline,
                            len(output) + 1,
                            scope=str(scope),
                            key=str(key),
                        )
                    )
                    if len(output) >= limit:
                        return output
    return output


def decision_adjustment(
    summary: Mapping[str, object],
    symbol: str,
    direction: str,
    conviction: int,
) -> ExpectancyAdjustment:
    """今回の判断に対応する期待値ガードを返す。

    サンプル不足は警告のみ、十分なサンプルで期待Rが非正ならblock、
    PF/MFE/MAEが弱い場合は確信度減衰として返す。
    """
    findings = _matching_expectancy_findings(summary, symbol, direction, conviction)
    if not findings:
        return ExpectancyAdjustment()
    findings.sort(
        key=lambda item: (
            int(item["rank"]),
            -_finding_specificity(item),
            -int(item["tradable"]),
            item["label"],
        )
    )
    finding = findings[0]
    severity = str(finding.get("severity", ""))
    reason = str(finding.get("reason_ja", ""))
    if severity == "block":
        return _adjustment_from_finding(
            finding,
            action="block",
            factor=EXPECTANCY_BLOCK_FACTOR,
            block=True,
            reason_ja=f"{reason}。新規エントリーは見送り",
        )
    if severity == "weak":
        return _adjustment_from_finding(
            finding,
            action="dampen",
            factor=EXPECTANCY_WEAK_FACTOR,
            block=False,
            reason_ja=f"{reason}。確信度を×{EXPECTANCY_WEAK_FACTOR:.2f}に減衰",
        )
    if severity == "sample_guard":
        return _adjustment_from_finding(
            finding,
            action="sample_guard",
            factor=1.0,
            block=False,
            reason_ja=f"{reason}。実績が揃うまで参考扱い",
        )
    if severity in {"quality_warn", "quality_block"}:
        return _adjustment_from_finding(
            finding,
            action="quality_guard",
            factor=1.0,
            block=False,
            reason_ja=f"{reason}。データ補強まで最適化しない",
        )
    return ExpectancyAdjustment()


def retest_tp_sl_variants(
    entries: Iterable[Mapping[str, object]],
    *,
    target1_r_candidates: Sequence[float] = DEFAULT_TP1_R_CANDIDATES,
    target2_r_candidates: Sequence[float] = DEFAULT_TP2_R_CANDIDATES,
    cell_scopes: Sequence[str] = ("by_symbol_direction",),
    cell_limit: int = 20,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    tolerance_hours: float = DEFAULT_TOLERANCE_HOURS,
    min_samples: int = MIN_EXPECTANCY_SAMPLES,
    group_min_samples: int = MIN_GROUP_EXPECTANCY_SAMPLES,
) -> dict:
    """TP1/TP2候補をpaper再採点し、既存設定との期待値差分を返す。"""
    materialized = list(entries)
    baseline_outcomes = evaluate_trade_outcomes(
        materialized,
        horizon_hours=horizon_hours,
        tolerance_hours=tolerance_hours,
    )
    baseline_summary = summarize_expectancy(
        baseline_outcomes,
        min_samples=min_samples,
        group_min_samples=group_min_samples,
    )
    baseline_stats = baseline_summary.get("overall")
    variants: list[TradeVariantScore] = []
    variant_outcomes: list[tuple[float, float, list[TradeOutcome]]] = []
    for tp1_r in _clean_r_candidates(target1_r_candidates):
        for tp2_r in _clean_r_candidates(target2_r_candidates):
            if tp2_r <= tp1_r:
                continue
            if math.isclose(tp1_r, 1.0) and math.isclose(tp2_r, 2.0):
                continue
            outcomes = evaluate_trade_outcomes(
                materialized,
                horizon_hours=horizon_hours,
                tolerance_hours=tolerance_hours,
                target1_r=tp1_r,
                target2_r=tp2_r,
            )
            summary = summarize_expectancy(
                outcomes,
                min_samples=min_samples,
                group_min_samples=group_min_samples,
            )
            stats = summary.get("overall")
            if isinstance(stats, Mapping) and isinstance(baseline_stats, Mapping):
                variants.append(_variant_score(tp1_r, tp2_r, stats, baseline_stats))
                variant_outcomes.append((tp1_r, tp2_r, outcomes))
    variants.sort(key=_variant_sort_key)
    best = next(
        (variant for variant in variants if variant.recommendation in {"paper_test", "watch"}),
        None,
    )
    report = {
        "schema": 1,
        "baseline": baseline_summary,
        "variants": [variant.to_dict() for variant in variants],
        "best": best.to_dict() if best is not None else None,
        "cells": _variant_cell_reports(
            baseline_outcomes,
            variant_outcomes,
            scopes=cell_scopes,
            min_samples=group_min_samples,
            limit=cell_limit,
        ),
    }
    report["improvement_candidates"] = [
        candidate.to_dict() for candidate in variant_improvement_candidates(report)
    ]
    return report


def update_improvement_registry(
    previous: Mapping[str, object] | None,
    candidates: Sequence[TradeImprovementCandidate],
    *,
    now: datetime | None = None,
    managed_action_types: set[str] | None = None,
    data_contract: str | None = None,
) -> dict:
    generated_at = _utc(now or datetime.now(UTC)).isoformat()
    previous_records = _registry_records(previous)
    events = _registry_events(previous)
    current_ids = {candidate.candidate_id for candidate in candidates}
    records: dict[str, dict] = {}
    for candidate in candidates:
        prior = previous_records.get(candidate.candidate_id, {})
        seen_count = _stat_int(prior, "seen_count") + 1
        prior_stage = str(prior.get("stage", ""))
        stage = (
            prior_stage
            if prior_stage in APPROVAL_PRESERVED_STAGES
            else _candidate_stage(candidate.priority, seen_count)
        )
        record = {
            **candidate.to_dict(),
            "status": "active",
            "stage": stage,
            "first_seen": str(prior.get("first_seen") or generated_at),
            "last_seen": generated_at,
            "resolved_at": None,
            "seen_count": seen_count,
            "ready_seen_threshold": READY_SEEN_BY_PRIORITY.get(candidate.priority, 3),
        }
        if data_contract:
            record["data_contract"] = data_contract
        for key in (
            "approved_at",
            "approved_by",
            "approval_note",
            "rejected_at",
            "rejected_by",
            "rejection_note",
            "auto_paused_at",
            "auto_pause_reason_ja",
            "resumed_at",
            "resumed_by",
            "resume_note",
        ):
            if key in prior:
                record[key] = prior[key]
        records[candidate.candidate_id] = record
        if not prior:
            _append_registry_event(
                events,
                generated_at=generated_at,
                candidate_id=candidate.candidate_id,
                event_type="detected",
                to_stage=stage,
                details={"action_type": candidate.action_type, "priority": candidate.priority},
            )
        elif str(prior.get("stage", "")) != stage:
            _append_registry_event(
                events,
                generated_at=generated_at,
                candidate_id=candidate.candidate_id,
                event_type="stage_changed",
                from_stage=str(prior.get("stage", "")),
                to_stage=stage,
            )
    for candidate_id, prior in previous_records.items():
        if candidate_id in current_ids:
            continue
        if (
            managed_action_types is not None
            and prior.get("action_type") not in managed_action_types
        ):
            records[candidate_id] = dict(prior)
            continue
        record = dict(prior)
        record["status"] = "resolved"
        record["stage"] = "resolved"
        record["resolved_at"] = record.get("resolved_at") or generated_at
        record["seen_count"] = _stat_int(record, "seen_count")
        records[candidate_id] = record
        _append_registry_event(
            events,
            generated_at=generated_at,
            candidate_id=candidate_id,
            event_type="resolved",
            from_stage=str(prior.get("stage", "")),
            to_stage="resolved",
        )
    payload = _registry_payload(records, generated_at, _bounded_registry_events(events))
    if data_contract:
        payload["data_contract"] = data_contract
    return payload


def load_improvement_registry(path: str | Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_improvement_registry(registry: Mapping[str, object], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(json_safe(registry), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def set_improvement_candidate_approval(
    registry: Mapping[str, object] | None,
    candidate_id: str,
    decision: str,
    *,
    actor: str = "manual",
    note: str = "",
    now: datetime | None = None,
) -> tuple[dict, dict]:
    """改善候補を人間承認/却下/再開する。

    approved は paper_ready のみ、resumed は auto_paused のみ許可する。
    TP/SL候補は、期待Rが現行比で改善している証跡がある場合だけ昇格できる。
    """
    generated_at = _utc(now or datetime.now(UTC)).isoformat()
    records = _registry_records(registry)
    events = _registry_events(registry)
    candidate_id = str(candidate_id)
    result = {
        "candidate_id": candidate_id,
        "decision": decision,
        "status": "not_found",
        "message_ja": "候補IDが見つかりません",
    }
    record = records.get(candidate_id)
    if record is None:
        return _registry_payload(records, generated_at), result

    stage = str(record.get("stage", ""))
    status = str(record.get("status", ""))
    if status != "active":
        result.update(
            {
                "status": "not_active",
                "message_ja": "active候補ではないため承認対象外です",
            }
        )
        return _registry_payload(records, generated_at), result
    if decision == "approved" and stage != "paper_ready":
        result.update(
            {
                "status": "not_ready",
                "message_ja": "paper_ready到達前のため承認できません",
            }
        )
        return _registry_payload(records, generated_at), result
    if decision == "resumed" and stage != "auto_paused":
        result.update(
            {
                "status": "not_paused",
                "message_ja": "auto_paused候補ではないため再開できません",
            }
        )
        return _registry_payload(records, generated_at), result
    if decision not in {"approved", "rejected", "resumed"}:
        result.update(
            {
                "status": "invalid_decision",
                "message_ja": "decisionはapproved/rejected/resumedのみ有効です",
            }
        )
        return _registry_payload(records, generated_at), result
    if decision in {"approved", "resumed"}:
        improvement_ok, improvement_reason = _approval_improvement_gate(record)
        if not improvement_ok:
            result.update(
                {
                    "status": "not_improving",
                    "message_ja": improvement_reason,
                }
            )
            return _registry_payload(records, generated_at), result

    updated = dict(record)
    updated["stage"] = "approved" if decision == "resumed" else decision
    updated[f"{decision}_at"] = generated_at
    if decision == "approved":
        updated["approved_by"] = actor
        updated["approval_note"] = note
        updated.pop("rejected_at", None)
        updated.pop("rejected_by", None)
        updated.pop("rejection_note", None)
    elif decision == "resumed":
        updated["resumed_by"] = actor
        updated["resume_note"] = note
        updated["approved_by"] = actor
        updated["approval_note"] = note or str(updated.get("approval_note", ""))
        updated.pop("rejected_at", None)
        updated.pop("rejected_by", None)
        updated.pop("rejection_note", None)
    else:
        updated["rejected_by"] = actor
        updated["rejection_note"] = note
        updated.pop("approved_at", None)
        updated.pop("approved_by", None)
        updated.pop("approval_note", None)
        updated.pop("resumed_at", None)
        updated.pop("resumed_by", None)
        updated.pop("resume_note", None)
    records[candidate_id] = updated
    _append_registry_event(
        events,
        generated_at=generated_at,
        candidate_id=candidate_id,
        event_type=decision,
        from_stage=stage,
        to_stage=str(updated.get("stage", "")),
        actor=actor,
        note=note,
    )
    result.update(
        {
            "status": decision,
            "message_ja": {
                "approved": "候補を承認しました",
                "rejected": "候補を却下しました",
                "resumed": "自動停止中の候補を再開しました",
            }[decision],
        }
    )
    return _registry_payload(records, generated_at, _bounded_registry_events(events)), result


def build_monitoring_snapshot(
    summary: Mapping[str, object],
    *,
    registry: Mapping[str, object] | None = None,
    health_report: TradeOutcomeHealthReport | None = None,
    now: datetime | None = None,
) -> dict:
    """cron/dashboard向けの軽量JSON。"""
    generated_at = _utc(now or datetime.now(UTC)).isoformat()
    health_report = health_report or check_expectancy_health(summary)
    records = _registry_records(registry)
    active = [record for record in records.values() if record.get("status") == "active"]
    paper_ready = [record for record in active if record.get("stage") == "paper_ready"]
    approved = [record for record in active if record.get("stage") == "approved"]
    rejected = [record for record in active if record.get("stage") == "rejected"]
    auto_paused = [record for record in active if record.get("stage") == "auto_paused"]
    alerts = [
        {
            "type": "health",
            "severity": check.status,
            "message_ja": check.message,
            "check": check.name,
        }
        for check in health_report.checks
        if check.status in {STATUS_WARN, STATUS_FAIL}
    ]
    alerts.extend(
        {
            "type": "paper_ready",
            "severity": "info",
            "candidate_id": str(record.get("candidate_id", "")),
            "message_ja": str(record.get("title_ja", "承認待ちの改善候補")),
        }
        for record in paper_ready
    )
    alerts.extend(
        {
            "type": "auto_paused",
            "severity": "warn",
            "candidate_id": str(record.get("candidate_id", "")),
            "message_ja": str(record.get("auto_pause_reason_ja", "承認済みTP/SLを自動停止")),
        }
        for record in auto_paused
    )
    return {
        "schema": 1,
        "generated_at": generated_at,
        "status": health_report.status,
        "exit_code": health_report.exit_code,
        "health": health_report.to_dict(),
        "summary": summary,
        "registry": {
            "active_count": len(active),
            "paper_ready_count": len(paper_ready),
            "approved_count": len(approved),
            "rejected_count": len(rejected),
            "auto_paused_count": len(auto_paused),
            "resolved_count": sum(
                1 for record in records.values() if record.get("status") == "resolved"
            ),
            "paper_ready": _monitor_records(paper_ready),
            "approved": _monitor_records(approved),
            "rejected": _monitor_records(rejected),
            "auto_paused": _monitor_records(auto_paused),
        },
        "approved_policy_stats": approved_policy_stats(summary, registry=registry),
        "recent_events": _bounded_registry_events(_registry_events(registry), limit=20),
        "alerts": alerts,
    }


def approved_target_policies(registry: Mapping[str, object] | None) -> list[ApprovedTargetPolicy]:
    policies: list[ApprovedTargetPolicy] = []
    for record in _registry_records(registry).values():
        if record.get("status") != "active" or record.get("stage") != "approved":
            continue
        if record.get("action_type") != "tp_sl_variant_paper_test":
            continue
        proposed = record.get("proposed_change")
        if not isinstance(proposed, Mapping):
            continue
        target1_r = _stat_float(proposed, "target1_r")
        target2_r = _stat_float(proposed, "target2_r")
        if (
            target1_r is None
            or target2_r is None
            or not math.isfinite(target1_r)
            or not math.isfinite(target2_r)
            or target1_r <= 0
            or target2_r <= target1_r
        ):
            continue
        policies.append(
            ApprovedTargetPolicy(
                candidate_id=str(record.get("candidate_id", "")),
                scope=str(proposed.get("scope", "overall")),
                key=str(proposed.get("key", "")),
                target1_r=round(target1_r, 4),
                target2_r=round(target2_r, 4),
                priority=str(record.get("priority", "")),
                reason_ja=str(record.get("title_ja", "")),
                approved_at=(str(record.get("approved_at")) if record.get("approved_at") else None),
            )
        )
    policies.sort(
        key=lambda policy: (
            -_policy_specificity(policy),
            str(policy.approved_at or ""),
            policy.candidate_id,
        )
    )
    return policies


def select_approved_target_policy(
    registry: Mapping[str, object] | None,
    symbol: str,
    direction: str,
    conviction: int,
) -> ApprovedTargetPolicy | None:
    for policy in approved_target_policies(registry):
        if _target_policy_matches(policy, symbol, direction, conviction):
            return policy
    return None


def approved_policy_stats(
    summary: Mapping[str, object],
    *,
    registry: Mapping[str, object] | None = None,
) -> list[dict]:
    by_policy = summary.get("by_target_policy")
    if not isinstance(by_policy, Mapping):
        return []
    records = _registry_records(registry)
    rows: list[dict] = []
    for candidate_id, stats in by_policy.items():
        if not isinstance(stats, Mapping):
            continue
        record = records.get(str(candidate_id), {})
        rows.append(
            {
                "candidate_id": str(candidate_id),
                "stage": str(record.get("stage", "")),
                "title_ja": str(record.get("title_ja", "")),
                "tradable": _stat_int(stats, "tradable"),
                "evaluated": _stat_int(stats, "evaluated"),
                "sample_ok": bool(stats.get("sample_ok")),
                "expectancy_r": _stat_float(stats, "expectancy_r"),
                "profit_factor_r": _stat_float(stats, "profit_factor_r"),
                "avg_path_quality": _stat_float(stats, "avg_path_quality"),
            }
        )
    rows.sort(key=lambda row: (-int(row["tradable"]), str(row["candidate_id"])))
    return rows


def auto_pause_underperforming_approved_policies(
    registry: Mapping[str, object] | None,
    summary: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> tuple[dict, list[dict]]:
    generated_at = _utc(now or datetime.now(UTC)).isoformat()
    records = _registry_records(registry)
    events = _registry_events(registry)
    by_policy = summary.get("by_target_policy")
    if not isinstance(by_policy, Mapping):
        return _registry_payload(records, generated_at, _bounded_registry_events(events)), []

    paused: list[dict] = []
    for candidate_id, record in list(records.items()):
        if record.get("status") != "active" or record.get("stage") != "approved":
            continue
        if record.get("action_type") != "tp_sl_variant_paper_test":
            continue
        stats = by_policy.get(candidate_id)
        if not isinstance(stats, Mapping) or not bool(stats.get("sample_ok")):
            continue
        reason = _policy_pause_reason(stats)
        if not reason:
            continue
        updated = dict(record)
        updated["stage"] = "auto_paused"
        updated["auto_paused_at"] = generated_at
        updated["auto_pause_reason_ja"] = reason
        records[candidate_id] = updated
        _append_registry_event(
            events,
            generated_at=generated_at,
            candidate_id=candidate_id,
            event_type="auto_paused",
            from_stage=str(record.get("stage", "")),
            to_stage="auto_paused",
            details={
                "reason_ja": reason,
                "expectancy_r": _stat_float(stats, "expectancy_r"),
                "profit_factor_r": _stat_float(stats, "profit_factor_r"),
                "tradable": _stat_int(stats, "tradable"),
            },
        )
        paused.append(
            {
                "candidate_id": candidate_id,
                "reason_ja": reason,
                "expectancy_r": _stat_float(stats, "expectancy_r"),
                "profit_factor_r": _stat_float(stats, "profit_factor_r"),
                "tradable": _stat_int(stats, "tradable"),
            }
        )
    return _registry_payload(records, generated_at, _bounded_registry_events(events)), paused


def json_safe(value: object) -> object:
    """JSONレポート用にNaN/Infinityを標準JSONで扱える値へ落とす。"""
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def format_expectancy_report_ja(summary: Mapping[str, object], *, limit: int = 5) -> str:
    overall = summary.get("overall")
    if not isinstance(overall, Mapping) or _stat_int(overall, "evaluated") <= 0:
        return "トレード期待値監視: 対象なし"
    tradable = _stat_int(overall, "tradable")
    evaluated = _stat_int(overall, "evaluated")
    min_samples = _stat_int(overall, "min_samples")
    sample_status = "サンプルOK" if bool(overall.get("sample_ok")) else "サンプル不足"
    lines = [
        "トレード期待値監視(MFE/MAE/TP/SL): "
        f"期待R {_fmt_signed(_stat_float(overall, 'expectancy_r'), 'R')}"
        f" / PF {_fmt_number(_stat_float(overall, 'profit_factor_r'))}"
        f" / TP1 {_fmt_pct(_stat_float(overall, 'tp1_rate'))}"
        f" / TP2 {_fmt_pct(_stat_float(overall, 'tp2_rate'))}"
        f" / SL {_fmt_pct(_stat_float(overall, 'sl_rate'))}"
        f" / 平均MFE {_fmt_signed(_stat_float(overall, 'avg_mfe_r'), 'R')}"
        f" / 平均MAE {_fmt_number(_stat_float(overall, 'avg_mae_r'), 'R')}"
        f" (n={tradable}/{evaluated}, {sample_status}"
        + (f" {tradable}/{min_samples}" if min_samples else "")
        + ")"
    ]
    quality = summary.get("quality")
    if isinstance(quality, Mapping):
        lines.append(
            "経路データ品質: "
            f"scored={_stat_int(quality, 'scored')}/{_stat_int(quality, 'evaluated')}"
            f" / 低品質={_stat_int(quality, 'low_quality')}"
            f" / 平均品質{_fmt_pct(_stat_float(quality, 'avg_path_quality'))}"
        )
    findings = expectancy_findings(summary, limit=limit)
    if not findings:
        lines.append("改善候補: 期待値悪化セルなし")
    else:
        lines.append("改善候補:")
        lines.extend(f"・{finding['label']}: {finding['reason_ja']}" for finding in findings)
    return "\n".join(lines)


def format_improvement_candidates_ja(candidates: Sequence[TradeImprovementCandidate]) -> str:
    if not candidates:
        return "改善候補: なし"
    lines = ["改善候補(paper検証待ち):"]
    for candidate in candidates:
        lines.append(
            f"・[{candidate.priority}] {candidate.title_ja}: "
            f"{candidate.rationale_ja} / 検証: {candidate.validation_ja}"
        )
    return "\n".join(lines)


def format_improvement_registry_ja(registry: Mapping[str, object], *, limit: int = 5) -> str:
    records = _registry_records(registry)
    active = [record for record in records.values() if record.get("status") == "active"]
    resolved = [record for record in records.values() if record.get("status") == "resolved"]
    ready = [record for record in active if record.get("stage") == "paper_ready"]
    approved = [record for record in active if record.get("stage") == "approved"]
    rejected = [record for record in active if record.get("stage") == "rejected"]
    auto_paused = [record for record in active if record.get("stage") == "auto_paused"]
    lines = [
        f"改善候補レジストリ: active={len(active)} / paper_ready={len(ready)}"
        f" / approved={len(approved)} / auto_paused={len(auto_paused)}"
        f" / rejected={len(rejected)} / resolved={len(resolved)}"
    ]
    events = _registry_events(registry)
    if events:
        last = events[-1]
        lines.append(
            "監査イベント: "
            f"{len(events)}件 / latest={last.get('event_type')}:{last.get('candidate_id')}"
        )
    for record in sorted(
        active, key=lambda item: (-_stat_int(item, "seen_count"), str(item.get("candidate_id", "")))
    )[:limit]:
        lines.append(
            f"・{record.get('title_ja', record.get('candidate_id'))}"
            f" (stage={record.get('stage')}, seen={_stat_int(record, 'seen_count')})"
        )
    return "\n".join(lines)


def format_variant_retest_report_ja(report: Mapping[str, object], *, limit: int = 5) -> str:
    baseline = report.get("baseline")
    if not isinstance(baseline, Mapping):
        return "TP/SL候補paper再採点: 対象なし"
    overall = baseline.get("overall")
    if not isinstance(overall, Mapping) or _stat_int(overall, "evaluated") <= 0:
        return "TP/SL候補paper再採点: 対象なし"
    lines = [
        "TP/SL候補paper再採点: "
        f"現行 期待R {_fmt_signed(_stat_float(overall, 'expectancy_r'), 'R')}"
        f" / PF {_fmt_number(_stat_float(overall, 'profit_factor_r'))}"
        f" / n={_stat_int(overall, 'tradable')}/{_stat_int(overall, 'evaluated')}"
    ]
    best = report.get("best")
    if isinstance(best, Mapping):
        lines.append(
            "最有力候補: "
            f"TP1={_fmt_number(_stat_float(best, 'target1_r'), 'R')}"
            f" / TP2={_fmt_number(_stat_float(best, 'target2_r'), 'R')}"
            f" / 期待R {_fmt_signed(_stat_float(best, 'expectancy_r'), 'R')}"
            f" ({_fmt_signed(_stat_float(best, 'delta_expectancy_r'), 'R')})"
            f" / {best.get('reason_ja', '')}"
        )
    variants = report.get("variants")
    if not isinstance(variants, Sequence) or not variants:
        lines.append("候補: 比較可能なTP/SL候補なし")
        return "\n".join(lines)
    lines.append("候補上位:")
    for raw in [item for item in variants if isinstance(item, Mapping)][:limit]:
        lines.append(
            "・"
            f"TP1={_fmt_number(_stat_float(raw, 'target1_r'), 'R')}"
            f" / TP2={_fmt_number(_stat_float(raw, 'target2_r'), 'R')}"
            f" / 期待R {_fmt_signed(_stat_float(raw, 'expectancy_r'), 'R')}"
            f" / 差分 {_fmt_signed(_stat_float(raw, 'delta_expectancy_r'), 'R')}"
            f" / PF {_fmt_number(_stat_float(raw, 'profit_factor_r'))}"
            f" / {raw.get('recommendation', '')}"
        )
    cell_lines = _format_variant_cell_lines(report, limit=limit)
    if cell_lines:
        lines.append("セル別候補:")
        lines.extend(cell_lines)
    return "\n".join(lines)


def check_expectancy_health(
    summary: Mapping[str, object],
    *,
    require_sample_ok: bool = False,
) -> TradeOutcomeHealthReport:
    checks: list[TradeOutcomeHealthCheck] = []
    overall = summary.get("overall")
    if not isinstance(overall, Mapping):
        return TradeOutcomeHealthReport(
            [TradeOutcomeHealthCheck("summary", STATUS_FAIL, "overall統計がありません")]
        )
    evaluated = _stat_int(overall, "evaluated")
    tradable = _stat_int(overall, "tradable")
    min_samples = _stat_int(overall, "min_samples")
    sample_ok = bool(overall.get("sample_ok"))
    expectancy_r = _stat_float(overall, "expectancy_r")
    if evaluated <= 0:
        checks.append(
            TradeOutcomeHealthCheck("sample", STATUS_WARN, "期待値監査対象がまだありません")
        )
    elif tradable <= 0:
        checks.append(
            TradeOutcomeHealthCheck("sample", STATUS_FAIL, "期待値計算に使える判断がありません")
        )
    elif not sample_ok:
        checks.append(
            TradeOutcomeHealthCheck(
                "sample",
                STATUS_FAIL if require_sample_ok else STATUS_WARN,
                "期待値サンプルが不足しています",
                {"tradable": tradable, "min_samples": min_samples},
            )
        )
    else:
        checks.append(
            TradeOutcomeHealthCheck("sample", STATUS_OK, "期待値サンプルは最低件数を満たしています")
        )
    if sample_ok and expectancy_r is not None and expectancy_r <= 0:
        checks.append(
            TradeOutcomeHealthCheck(
                "expectancy", STATUS_FAIL, "全体期待Rが非正です", {"expectancy_r": expectancy_r}
            )
        )
    elif expectancy_r is None:
        checks.append(TradeOutcomeHealthCheck("expectancy", STATUS_WARN, "期待Rを計算できません"))
    else:
        checks.append(
            TradeOutcomeHealthCheck(
                "expectancy",
                STATUS_OK if expectancy_r > 0 else STATUS_WARN,
                "期待Rを計算済み",
                {"expectancy_r": expectancy_r},
            )
        )
    quality = summary.get("quality")
    checks.append(_health_check_quality(quality if isinstance(quality, Mapping) else {}))
    blocking = [
        finding
        for finding in expectancy_findings(summary, limit=20)
        if finding["severity"] in {"block", "quality_block"}
    ]
    checks.append(
        TradeOutcomeHealthCheck(
            "blocked_cells",
            STATUS_FAIL if blocking else STATUS_OK,
            "期待値ガード対象セルがあります" if blocking else "期待値ガード対象セルはありません",
            {"count": len(blocking)},
        )
    )
    return TradeOutcomeHealthReport(checks)


def format_expectancy_health_ja(report: TradeOutcomeHealthReport) -> str:
    labels = {STATUS_OK: "OK", STATUS_WARN: "WARN", STATUS_FAIL: "FAIL"}
    lines = [f"トレード期待値ヘルスチェック: {labels[report.status]}"]
    for check in report.checks:
        detail = _format_details(check.details)
        suffix = f" ({detail})" if detail else ""
        lines.append(
            f"- [{labels.get(check.status, check.status.upper())}] {check.name}: {check.message}{suffix}"
        )
    return "\n".join(lines)


def _aggregate_by(
    outcomes: Sequence[TradeOutcome],
    key_func: Callable[[TradeOutcome], str],
    *,
    min_samples: int,
) -> dict[str, dict]:
    grouped: dict[str, list[TradeOutcome]] = {}
    for outcome in outcomes:
        key = key_func(outcome)
        if key:
            grouped.setdefault(key, []).append(outcome)
    return {
        key: aggregate_expectancy(group, min_samples=min_samples).to_dict()
        for key, group in sorted(grouped.items())
    }


def _matching_expectancy_findings(
    summary: Mapping[str, object],
    symbol: str,
    direction: str,
    conviction: int,
) -> list[dict]:
    normalized_symbol = symbol.upper().replace("/", "")
    normalized_direction = direction.lower()
    checks = [
        ("by_symbol_direction", "通貨ペア×方向", f"{normalized_symbol}:{normalized_direction}"),
        ("by_symbol", "通貨ペア", normalized_symbol),
        ("by_direction", "方向", normalized_direction),
        ("by_confidence", "確信度", _confidence_label_from_int(conviction)),
    ]
    findings: list[dict] = []
    for group_key, scope, key in checks:
        finding = _group_finding(summary, group_key, scope, key)
        if finding is not None:
            findings.append(finding)
    overall = summary.get("overall")
    if isinstance(overall, Mapping):
        finding = _expectancy_finding("全体", "", overall)
        if finding is not None:
            findings.append(finding)
    quality = summary.get("quality")
    if isinstance(quality, Mapping):
        finding = _quality_finding(quality)
        if finding is not None:
            findings.append(finding)
    return findings


def _group_finding(
    summary: Mapping[str, object],
    group_key: str,
    scope: str,
    key: str,
) -> dict | None:
    group = summary.get(group_key)
    if not isinstance(group, Mapping):
        return None
    stats = group.get(key)
    if not isinstance(stats, Mapping):
        return None
    return _expectancy_finding(scope, key, stats)


def _adjustment_from_finding(
    finding: Mapping[str, object],
    *,
    action: str,
    factor: float,
    block: bool,
    reason_ja: str,
) -> ExpectancyAdjustment:
    return ExpectancyAdjustment(
        action=action,
        factor=round(max(0.0, min(1.0, factor)), 4),
        block=block,
        reason_ja=reason_ja,
        matched_scope=str(finding.get("scope", "")),
        matched_key=str(finding.get("key", "")),
        severity=str(finding.get("severity", "")),
        tradable=_stat_int(finding, "tradable"),
        min_samples=_stat_int(finding, "min_samples"),
        expectancy_r=_stat_float(finding, "expectancy_r"),
        profit_factor_r=_stat_float(finding, "profit_factor_r"),
    )


def _finding_specificity(finding: Mapping[str, object]) -> int:
    scope = str(finding.get("scope", ""))
    return {
        "通貨ペア×方向": 5,
        "通貨ペア": 4,
        "方向": 3,
        "確信度": 2,
        "全体": 1,
        "データ品質": 0,
    }.get(scope, 0)


def _clean_r_candidates(values: Sequence[float]) -> list[float]:
    cleaned: list[float] = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric) or numeric <= 0:
            continue
        cleaned.append(round(numeric, 4))
    return sorted(dict.fromkeys(cleaned))


def _variant_score(
    target1_r: float,
    target2_r: float,
    stats: Mapping[str, object],
    baseline: Mapping[str, object],
) -> TradeVariantScore:
    expectancy = _stat_float(stats, "expectancy_r")
    baseline_expectancy = _stat_float(baseline, "expectancy_r")
    profit_factor = _stat_float(stats, "profit_factor_r")
    baseline_pf = _stat_float(baseline, "profit_factor_r")
    delta_expectancy = (
        _round(expectancy - baseline_expectancy)
        if expectancy is not None and baseline_expectancy is not None
        else None
    )
    delta_pf = (
        _round(profit_factor - baseline_pf)
        if profit_factor is not None
        and baseline_pf is not None
        and math.isfinite(profit_factor)
        and math.isfinite(baseline_pf)
        else None
    )
    sample_ok = bool(stats.get("sample_ok"))
    recommendation, reason = _variant_recommendation(
        stats,
        baseline,
        delta_expectancy,
        profit_factor,
    )
    return TradeVariantScore(
        variant_id=f"tp1-{target1_r:g}-tp2-{target2_r:g}",
        target1_r=target1_r,
        target2_r=target2_r,
        tradable=_stat_int(stats, "tradable"),
        sample_ok=sample_ok,
        expectancy_r=expectancy,
        profit_factor_r=profit_factor,
        tp1_rate=_stat_float(stats, "tp1_rate"),
        tp2_rate=_stat_float(stats, "tp2_rate"),
        sl_rate=_stat_float(stats, "sl_rate"),
        avg_mfe_r=_stat_float(stats, "avg_mfe_r"),
        avg_mae_r=_stat_float(stats, "avg_mae_r"),
        avg_path_quality=_stat_float(stats, "avg_path_quality"),
        delta_expectancy_r=delta_expectancy,
        delta_profit_factor_r=delta_pf,
        recommendation=recommendation,
        reason_ja=reason,
    )


def _variant_cell_reports(
    baseline_outcomes: Sequence[TradeOutcome],
    variant_outcomes: Sequence[tuple[float, float, Sequence[TradeOutcome]]],
    *,
    scopes: Sequence[str],
    min_samples: int,
    limit: int,
) -> dict[str, dict[str, dict]]:
    reports: dict[str, dict[str, dict]] = {}
    for scope in scopes:
        key_func = _cell_key_func(scope)
        if key_func is None:
            continue
        baseline_groups = _group_outcomes(baseline_outcomes, key_func)
        scoped: dict[str, dict] = {}
        for key, baseline_group in baseline_groups.items():
            baseline_stats = aggregate_expectancy(baseline_group, min_samples=min_samples).to_dict()
            if _stat_int(baseline_stats, "evaluated") <= 0:
                continue
            variants: list[TradeVariantScore] = []
            for tp1_r, tp2_r, outcomes in variant_outcomes:
                grouped = _group_outcomes(outcomes, key_func)
                stats = aggregate_expectancy(
                    grouped.get(key, []), min_samples=min_samples
                ).to_dict()
                variants.append(_variant_score(tp1_r, tp2_r, stats, baseline_stats))
            variants.sort(key=_variant_sort_key)
            best = next(
                (
                    variant
                    for variant in variants
                    if variant.recommendation in {"paper_test", "watch"}
                ),
                None,
            )
            scoped[key] = {
                "baseline": baseline_stats,
                "variants": [variant.to_dict() for variant in variants],
                "best": best.to_dict() if best is not None else None,
            }
        sorted_items = sorted(scoped.items(), key=lambda item: _cell_report_sort_key(item[1]))
        reports[scope] = dict(sorted_items[: max(0, limit)])
    return reports


def _format_variant_cell_lines(report: Mapping[str, object], *, limit: int) -> list[str]:
    cells = report.get("cells")
    if not isinstance(cells, Mapping):
        return []
    lines: list[str] = []
    for scope, grouped in cells.items():
        if not isinstance(grouped, Mapping):
            continue
        for key, cell_report in grouped.items():
            if not isinstance(cell_report, Mapping):
                continue
            best = cell_report.get("best")
            if not isinstance(best, Mapping) or best.get("recommendation") != "paper_test":
                continue
            label = _variant_scope_label(str(scope), str(key))
            lines.append(
                "・"
                f"{label}: TP1={_fmt_number(_stat_float(best, 'target1_r'), 'R')}"
                f" / TP2={_fmt_number(_stat_float(best, 'target2_r'), 'R')}"
                f" / 期待R {_fmt_signed(_stat_float(best, 'expectancy_r'), 'R')}"
                f" / 差分 {_fmt_signed(_stat_float(best, 'delta_expectancy_r'), 'R')}"
            )
            if len(lines) >= limit:
                return lines
    return lines


def _group_outcomes(
    outcomes: Sequence[TradeOutcome],
    key_func: Callable[[TradeOutcome], str],
) -> dict[str, list[TradeOutcome]]:
    grouped: dict[str, list[TradeOutcome]] = {}
    for outcome in outcomes:
        key = key_func(outcome)
        if key:
            grouped.setdefault(key, []).append(outcome)
    return grouped


def _cell_key_func(scope: str) -> Callable[[TradeOutcome], str] | None:
    if scope == "by_symbol_direction":
        return lambda outcome: f"{outcome.symbol}:{outcome.direction}"
    if scope == "by_symbol":
        return lambda outcome: outcome.symbol
    if scope == "by_direction":
        return lambda outcome: outcome.direction
    if scope == "by_confidence":
        return _confidence_label
    return None


def _cell_report_sort_key(report: Mapping[str, object]) -> tuple:
    best = report.get("best")
    baseline = report.get("baseline")
    if not isinstance(best, Mapping):
        best_rank = 9
        delta = None
    else:
        best_rank = {
            "paper_test": 0,
            "watch": 1,
            "sample_guard": 2,
            "reject": 3,
        }.get(str(best.get("recommendation")), 4)
        delta = _stat_float(best, "delta_expectancy_r")
    tradable = _stat_int(baseline, "tradable") if isinstance(baseline, Mapping) else 0
    return (best_rank, -_sort_number(delta), -tradable)


def _variant_recommendation(
    stats: Mapping[str, object],
    baseline: Mapping[str, object],
    delta_expectancy: float | None,
    profit_factor: float | None,
) -> tuple[str, str]:
    if not bool(stats.get("sample_ok")):
        return "sample_guard", "サンプル不足のため採用不可"
    expectancy = _stat_float(stats, "expectancy_r")
    if expectancy is None:
        return "reject", "期待Rを計算できないため採用不可"
    if expectancy <= 0:
        return "reject", "期待Rが非正のため採用不可"
    baseline_sample_ok = bool(baseline.get("sample_ok"))
    if (
        baseline_sample_ok
        and delta_expectancy is not None
        and delta_expectancy >= MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R
        and (profit_factor is None or profit_factor >= WEAK_PROFIT_FACTOR)
    ):
        return "paper_test", "現行より期待Rが改善。paper比較候補"
    if not baseline_sample_ok:
        return "watch", "現行サンプル不足。候補として監視"
    return "watch", "改善幅が小さいため監視継続"


def _variant_sort_key(variant: TradeVariantScore) -> tuple:
    recommendation_rank = {
        "paper_test": 0,
        "watch": 1,
        "sample_guard": 2,
        "reject": 3,
    }.get(variant.recommendation, 4)
    return (
        recommendation_rank,
        -_sort_number(variant.delta_expectancy_r),
        -_sort_number(variant.expectancy_r),
        -_sort_number(variant.profit_factor_r),
        variant.target1_r,
        variant.target2_r,
    )


def _sort_number(value: float | None) -> float:
    if value is None or math.isnan(value):
        return -1_000_000.0
    if math.isinf(value):
        return 1_000_000.0 if value > 0 else -1_000_000.0
    return value


def _expectancy_finding(scope: str, key: str, stats: Mapping[str, object]) -> dict | None:
    evaluated = _stat_int(stats, "evaluated")
    tradable = _stat_int(stats, "tradable")
    min_samples = _stat_int(stats, "min_samples")
    if evaluated <= 0:
        return None
    label = scope if not key else f"{scope} {key}"
    sample_text = f"n={tradable}" + (f"/{min_samples}" if min_samples else "")
    sample_ok = bool(stats.get("sample_ok"))
    expectancy_r = _stat_float(stats, "expectancy_r")
    profit_factor = _stat_float(stats, "profit_factor_r")
    avg_mfe = _stat_float(stats, "avg_mfe_r")
    avg_mae = _stat_float(stats, "avg_mae_r")
    if tradable <= 0:
        return _finding(
            scope,
            key,
            label,
            "quality_block",
            3,
            evaluated,
            tradable,
            min_samples,
            stats,
            "期待値評価に使えるサンプルがありません",
        )
    if sample_ok and expectancy_r is not None and expectancy_r <= 0:
        return _finding(
            scope,
            key,
            label,
            "block",
            0,
            evaluated,
            tradable,
            min_samples,
            stats,
            f"期待R {_fmt_signed(expectancy_r, 'R')}が非正({sample_text})。新規判断は見送り優先",
        )
    if sample_ok and profit_factor is not None and profit_factor < WEAK_PROFIT_FACTOR:
        return _finding(
            scope,
            key,
            label,
            "weak",
            1,
            evaluated,
            tradable,
            min_samples,
            stats,
            f"PF {_fmt_number(profit_factor)}が薄い({sample_text})。利確/損切り条件を再調整",
        )
    if sample_ok and avg_mfe is not None and avg_mae is not None and avg_mfe <= avg_mae:
        return _finding(
            scope,
            key,
            label,
            "weak",
            1,
            evaluated,
            tradable,
            min_samples,
            stats,
            f"平均MFE {_fmt_signed(avg_mfe, 'R')} <= 平均MAE {_fmt_number(avg_mae, 'R')}({sample_text})",
        )
    if not sample_ok:
        return _finding(
            scope,
            key,
            label,
            "sample_guard",
            2,
            evaluated,
            tradable,
            min_samples,
            stats,
            f"期待値サンプル不足({sample_text})。方向目線は参考扱い",
        )
    avg_quality = _stat_float(stats, "avg_path_quality")
    if avg_quality is not None and avg_quality < QUALITY_WARN_THRESHOLD:
        return _finding(
            scope,
            key,
            label,
            "quality_warn",
            3,
            evaluated,
            tradable,
            min_samples,
            stats,
            f"平均経路品質{_fmt_pct(avg_quality)}が低く、TP/SL到達順の信頼度が不足",
        )
    return None


def _quality_finding(quality: Mapping[str, object]) -> dict | None:
    evaluated = _stat_int(quality, "evaluated")
    scored = _stat_int(quality, "scored")
    low_quality = _stat_int(quality, "low_quality")
    avg_quality = _stat_float(quality, "avg_path_quality")
    if evaluated <= 0:
        return None
    if scored <= 0:
        reason = "将来価格で採点できた判断がありません"
    elif low_quality / evaluated >= 0.5:
        reason = f"低品質サンプルが{low_quality}/{evaluated}件と多く、期待値判定の信頼度が不足"
    elif avg_quality is not None and avg_quality < QUALITY_WARN_THRESHOLD:
        reason = f"平均経路品質{_fmt_pct(avg_quality)}が低く、high/low付き経路の補強が必要"
    else:
        return None
    return _finding(
        "データ品質",
        "path",
        "データ品質 経路価格",
        "quality_warn",
        3,
        evaluated,
        scored,
        0,
        {},
        reason,
    )


def _candidate_from_finding(
    finding: Mapping[str, object], index: int
) -> TradeImprovementCandidate | None:
    severity = str(finding.get("severity", ""))
    scope = str(finding.get("scope", ""))
    key = str(finding.get("key", ""))
    label = str(finding.get("label", scope or "全体"))
    reason = str(finding.get("reason_ja", ""))
    candidate_id = _candidate_id(finding, index)
    if severity == "sample_guard":
        needed = max(0, _stat_int(finding, "min_samples") - _stat_int(finding, "tradable"))
        return TradeImprovementCandidate(
            candidate_id,
            scope,
            key,
            "low",
            "collect_samples",
            f"{label}のサンプル蓄積を優先",
            reason,
            {"execution_policy": "keep_guarded", "needed_samples": needed},
            f"有効サンプルをあと{needed}件以上追加し、期待R/PFを再評価",
            "サンプル不足の間はTP/SLや重みを最適化しない",
            dict(finding),
        )
    if severity in {"quality_warn", "quality_block"}:
        return TradeImprovementCandidate(
            candidate_id,
            scope,
            key,
            "medium",
            "improve_path_quality",
            f"{label}の経路データ品質を補強",
            reason,
            {"path_source": "use_high_low_ohlc_or_tick_bars"},
            "high/low付き経路でTP/SL到達順を再採点",
            "低品質経路ではTP/SL最適化を採用しない",
            dict(finding),
        )
    if severity == "block":
        return TradeImprovementCandidate(
            candidate_id,
            scope,
            key,
            "high",
            "expectancy_guard",
            f"{label}を見送り優先に戻す",
            reason,
            {"conviction_factor": EXPECTANCY_BLOCK_FACTOR, "execution_policy": "skip_new_entries"},
            "期待R>0、PF>=1.05、平均MFE>平均MAEを確認",
            "期待R非正セルはpaper検証で改善確認後に解除",
            dict(finding),
        )
    if severity == "weak":
        return TradeImprovementCandidate(
            candidate_id,
            scope,
            key,
            "medium",
            "tp_sl_entry_retest",
            f"{label}のTP/SL・エントリー条件を再検証",
            reason,
            {
                "tp1_r_candidates": _tp1_retest_candidates(finding),
                "entry_policy": "confirmation_only",
            },
            "候補TP1と確認型エントリーをpaper比較",
            "既存SL/TPを即時変更せずA/B検証してから昇格",
            dict(finding),
        )
    return None


def _candidate_from_variant(
    variant: Mapping[str, object],
    baseline_overall: object,
    index: int,
    *,
    scope: str = "overall",
    key: str = "",
) -> TradeImprovementCandidate:
    baseline = baseline_overall if isinstance(baseline_overall, Mapping) else {}
    target1_r = _stat_float(variant, "target1_r")
    target2_r = _stat_float(variant, "target2_r")
    expectancy = _stat_float(variant, "expectancy_r")
    delta = _stat_float(variant, "delta_expectancy_r")
    profit_factor = _stat_float(variant, "profit_factor_r")
    variant_id = str(variant.get("variant_id") or f"variant-{index}")
    scope_label = _variant_scope_label(scope, key)
    title = (
        (f"{scope_label}の" if scope_label else "")
        + f"TP1={_fmt_number(target1_r, 'R')} / "
        + f"TP2={_fmt_number(target2_r, 'R')}をpaper検証"
    )
    rationale = (
        f"現行比で期待Rが{_fmt_signed(delta, 'R')}改善し、"
        f"候補期待Rは{_fmt_signed(expectancy, 'R')} / PF {_fmt_number(profit_factor)}"
    )
    return TradeImprovementCandidate(
        _variant_candidate_id(scope, key, variant_id),
        "TP/SL候補" if not scope_label else f"TP/SL候補 {scope_label}",
        key or variant_id,
        "medium",
        "tp_sl_variant_paper_test",
        title,
        rationale,
        {
            "target1_r": target1_r,
            "target2_r": target2_r,
            "execution_policy": "paper_ab_test",
            "baseline_expectancy_r": _stat_float(baseline, "expectancy_r"),
            "candidate_expectancy_r": expectancy,
            "delta_expectancy_r": delta,
            "scope": scope,
            "key": key,
            "min_expected_improvement_r": MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R,
        },
        "paper期間で期待Rが現行比+0.05R以上、PF>=1.05、最低サンプル数を維持することを確認",
        "live反映はpaper_ready後も即時採用せず、人間承認とデータ品質確認を必須にする",
        {"variant": dict(variant), "baseline_overall": dict(baseline), "scope": scope, "key": key},
    )


def _health_check_quality(quality: Mapping[str, object]) -> TradeOutcomeHealthCheck:
    evaluated = _stat_int(quality, "evaluated")
    scored = _stat_int(quality, "scored")
    low_quality = _stat_int(quality, "low_quality")
    avg_quality = _stat_float(quality, "avg_path_quality")
    details: dict[str, object] = {
        "evaluated": evaluated,
        "scored": scored,
        "low_quality": low_quality,
        "avg_path_quality": avg_quality,
    }
    if evaluated <= 0:
        return TradeOutcomeHealthCheck(
            "path_quality", STATUS_WARN, "経路品質の評価対象がありません", details
        )
    if scored <= 0:
        return TradeOutcomeHealthCheck(
            "path_quality", STATUS_FAIL, "将来価格で採点できた判断がありません", details
        )
    if low_quality / evaluated >= 0.5 or (
        avg_quality is not None and avg_quality < QUALITY_WARN_THRESHOLD
    ):
        return TradeOutcomeHealthCheck(
            "path_quality", STATUS_WARN, "経路品質が低い状態です", details
        )
    return TradeOutcomeHealthCheck(
        "path_quality", STATUS_OK, "経路品質は監査可能な範囲です", details
    )


def _price_path_point(ts: datetime, entry: Mapping[str, object]) -> PricePathPoint | None:
    close = _float(entry.get("close"))
    if close is None:
        return None
    high = _float(entry.get("high"))
    low = _float(entry.get("low"))
    if high is None or low is None:
        return PricePathPoint(ts, close)
    scope = str(entry.get("ohlc_scope", "")).strip()
    if scope not in TRUSTED_POST_PREDICTION_OHLC_SCOPES:
        return PricePathPoint(ts, close, range_scope=scope, rejected_range=True)
    normalized_high = max(high, low, close)
    normalized_low = min(high, low, close)
    return PricePathPoint(ts, close, normalized_high, normalized_low, range_scope=scope)


def _target_policy_meta(entry: Mapping[str, object]) -> tuple[str | None, str, str]:
    raw = entry.get("target_policy")
    if not isinstance(raw, Mapping):
        return None, "", ""
    candidate_id = str(raw.get("candidate_id", "")).strip()
    if not candidate_id:
        return None, "", ""
    return candidate_id, str(raw.get("scope", "")), str(raw.get("key", ""))


def _future_path(
    series: Sequence[PricePathPoint],
    times: Sequence[datetime],
    ts: datetime,
    horizon_hours: float,
    tolerance_hours: float,
) -> list[PricePathPoint]:
    upper = ts + timedelta(hours=horizon_hours + tolerance_hours) + WEEKEND_CLOSURE
    future: list[PricePathPoint] = []
    for index in range(bisect_left(times, ts), len(series)):
        point = series[index]
        if point.ts <= ts:
            continue
        if point.ts > upper:
            break
        age = open_hours_between(ts, point.ts)
        if 0.0 < age <= horizon_hours + tolerance_hours:
            future.append(point)
    return future


def _path_quality(
    ts: datetime,
    future: Sequence[PricePathPoint],
    horizon_hours: float,
    min_path_points: int,
) -> tuple[float, tuple[str, ...], str]:
    flags: list[str] = []
    points = len(future)
    coverage = _coverage(ts, future[-1].ts, horizon_hours) if future else 0.0
    point_ratio = min(1.0, points / POINTS_FOR_FULL_DENSITY)
    range_points = sum(1 for point in future if point.has_range)
    range_ratio = range_points / points if points else 0.0
    if any(point.rejected_range for point in future):
        flags.append("untrusted_forming_ohlc_ignored")
    if range_ratio <= 0:
        path_source = "close"
        flags.append("close_only_path")
        quality = min(CLOSE_ONLY_QUALITY_CAP, 0.65 * coverage + 0.35 * point_ratio)
    else:
        path_source = "ohlc" if range_ratio >= 0.8 else "mixed"
        if path_source == "mixed":
            flags.append("partial_high_low_path")
        cap = OHLC_QUALITY_CAP if path_source == "ohlc" else PARTIAL_OHLC_QUALITY_CAP
        quality = min(cap, 0.50 * coverage + 0.25 * point_ratio + 0.25 * range_ratio)
    if points < min_path_points:
        flags.append("insufficient_path_points")
    if coverage < MIN_PATH_COVERAGE:
        flags.append("short_path_coverage")
    if quality < MIN_PATH_QUALITY:
        flags.append("low_path_quality")
    return round(max(0.0, min(1.0, quality)), 4), tuple(flags), path_source


def _coverage(ts: datetime, last_ts: datetime, horizon_hours: float) -> float:
    if horizon_hours <= 0:
        return 0.0
    return max(0.0, min(1.0, open_hours_between(ts, last_ts) / horizon_hours))


def _touch(
    direction: str,
    point: PricePathPoint,
    stop: float | None,
    target1: float | None,
    target2: float | None,
) -> str:
    if stop is None or target1 is None:
        return "none"
    high = point.high if point.high is not None else point.close
    low = point.low if point.low is not None else point.close
    if direction == "long":
        stop_hit = low <= stop
        tp2_hit = target2 is not None and high >= target2
        tp1_hit = high >= target1
        if stop_hit and (tp1_hit or tp2_hit):
            return "ambiguous_sl_tp"
        if stop_hit:
            return "sl"
        if tp2_hit:
            return "tp2"
        if tp1_hit:
            return "tp1"
    else:
        stop_hit = high >= stop
        tp2_hit = target2 is not None and low <= target2
        tp1_hit = low <= target1
        if stop_hit and (tp1_hit or tp2_hit):
            return "ambiguous_sl_tp"
        if stop_hit:
            return "sl"
        if tp2_hit:
            return "tp2"
        if tp1_hit:
            return "tp1"
    return "none"


def _target_price(direction: str, entry: float, risk_distance: float, target_r: float) -> float:
    sign = 1.0 if direction == "long" else -1.0
    return entry + sign * risk_distance * target_r


def _target_r(
    direction: str,
    entry: float,
    risk_distance: float,
    target: float | None,
    *,
    default: float,
) -> float:
    if target is None or risk_distance <= 0:
        return default
    return max(0.0, _signed_move(direction, entry, target) / risk_distance)


def _favorable_move(direction: str, entry: float, point: PricePathPoint) -> float:
    if direction == "long":
        return (point.high if point.high is not None else point.close) - entry
    return entry - (point.low if point.low is not None else point.close)


def _adverse_move(direction: str, entry: float, point: PricePathPoint) -> float:
    if direction == "long":
        return entry - (point.low if point.low is not None else point.close)
    return (point.high if point.high is not None else point.close) - entry


def _realized_r(
    first_touch: str,
    terminal_r: float,
    target1_r: float,
    target2_r: float,
) -> float:
    if first_touch in {"sl", "ambiguous_sl_tp"}:
        return -1.0
    if first_touch == "tp2":
        return target2_r
    if first_touch == "tp1":
        return target1_r
    return terminal_r


def _signed_move(direction: str, entry: float, price: float) -> float:
    return price - entry if direction == "long" else entry - price


def _confidence_label(outcome: TradeOutcome) -> str:
    return _confidence_label_from_int(outcome.conviction)


def _confidence_label_from_int(conviction: int) -> str:
    for low, high in CONFIDENCE_BINS:
        if low <= conviction < high:
            return f"{low}-{high - 1}"
    return "unknown"


def _candidate_stage(priority: str, seen_count: int) -> str:
    return "paper_ready" if seen_count >= READY_SEEN_BY_PRIORITY.get(priority, 3) else "watch"


def _approval_improvement_gate(record: Mapping[str, object]) -> tuple[bool, str]:
    if record.get("action_type") != "tp_sl_variant_paper_test":
        return True, ""
    proposed = record.get("proposed_change")
    if not isinstance(proposed, Mapping):
        return False, "TP/SL候補の改善根拠がないため昇格できません"
    delta = _stat_float(proposed, "delta_expectancy_r")
    candidate = _stat_float(proposed, "candidate_expectancy_r")
    min_delta = _stat_float(proposed, "min_expected_improvement_r")
    if min_delta is None:
        min_delta = MIN_VARIANT_EXPECTANCY_IMPROVEMENT_R
    if delta is None:
        return False, "TP/SL候補の期待R改善幅が未記録のため昇格できません"
    if delta < min_delta:
        return (
            False,
            f"TP/SL候補の期待R改善幅が不足しています({delta:+.2f}R<{min_delta:+.2f}R)",
        )
    if candidate is None:
        return False, "TP/SL候補の候補期待Rが未記録のため昇格できません"
    if candidate <= 0:
        return False, f"TP/SL候補の候補期待Rが非正です({candidate:+.2f}R)"
    return True, ""


def _candidate_id(finding: Mapping[str, object], index: int) -> str:
    raw = f"{finding.get('scope', 'all')}:{finding.get('key', '') or 'overall'}:{finding.get('severity', 'unknown')}".lower()
    normalized = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return f"trade-improvement-{normalized or f'candidate-{index}'}"


def _variant_candidate_id(scope: str, key: str, variant_id: str) -> str:
    raw = f"tp-sl-variant:{scope}:{key or 'overall'}:{variant_id}".lower()
    normalized = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return f"trade-improvement-{normalized or 'tp-sl-variant'}"


def _variant_scope_label(scope: str, key: str) -> str:
    if scope == "overall" or not key:
        return ""
    labels = {
        "by_symbol_direction": "通貨ペア×方向",
        "by_symbol": "通貨ペア",
        "by_direction": "方向",
        "by_confidence": "確信度",
    }
    return f"{labels.get(scope, scope)} {key}"


def _registry_records(registry: Mapping[str, object] | None) -> dict[str, dict]:
    if not isinstance(registry, Mapping):
        return {}
    raw = registry.get("candidates")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}


def _registry_events(registry: Mapping[str, object] | None) -> list[dict]:
    if not isinstance(registry, Mapping):
        return []
    raw = registry.get("events")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _append_registry_event(
    events: list[dict],
    *,
    generated_at: str,
    candidate_id: str,
    event_type: str,
    from_stage: str = "",
    to_stage: str = "",
    actor: str = "",
    note: str = "",
    details: Mapping[str, object] | None = None,
) -> None:
    event: dict[str, object] = {
        "ts": generated_at,
        "candidate_id": candidate_id,
        "event_type": event_type,
        "from_stage": from_stage,
        "to_stage": to_stage,
    }
    if actor:
        event["actor"] = actor
    if note:
        event["note"] = note
    if details:
        event["details"] = dict(details)
    events.append(event)


def _bounded_registry_events(
    events: Sequence[Mapping[str, object]], *, limit: int = 200
) -> list[dict]:
    return [dict(event) for event in list(events)[-max(0, limit) :]]


def _registry_payload(
    records: Mapping[str, Mapping[str, object]],
    generated_at: str,
    events: Sequence[Mapping[str, object]] | None = None,
) -> dict:
    payload = {
        "schema": IMPROVEMENT_REGISTRY_SCHEMA,
        "generated_at": generated_at,
        "active_count": sum(1 for record in records.values() if record.get("status") == "active"),
        "paper_ready_count": sum(
            1 for record in records.values() if record.get("stage") == "paper_ready"
        ),
        "approved_count": sum(
            1 for record in records.values() if record.get("stage") == "approved"
        ),
        "rejected_count": sum(
            1 for record in records.values() if record.get("stage") == "rejected"
        ),
        "auto_paused_count": sum(
            1 for record in records.values() if record.get("stage") == "auto_paused"
        ),
        "resolved_count": sum(
            1 for record in records.values() if record.get("status") == "resolved"
        ),
        "events": [dict(event) for event in events] if events is not None else [],
        "candidates": {str(key): dict(value) for key, value in records.items()},
    }
    contracts = {
        str(record.get("data_contract"))
        for record in records.values()
        if record.get("data_contract")
    }
    if len(contracts) == 1:
        payload["data_contract"] = contracts.pop()
    return payload


def _monitor_records(records: Sequence[Mapping[str, object]], *, limit: int = 20) -> list[dict]:
    sorted_records = sorted(
        records,
        key=lambda record: (
            -_stat_int(record, "seen_count"),
            str(record.get("priority", "")),
            str(record.get("candidate_id", "")),
        ),
    )
    output: list[dict] = []
    for record in sorted_records[: max(0, limit)]:
        proposed_change = record.get("proposed_change")
        output.append(
            {
                "candidate_id": str(record.get("candidate_id", "")),
                "stage": str(record.get("stage", "")),
                "priority": str(record.get("priority", "")),
                "action_type": str(record.get("action_type", "")),
                "title_ja": str(record.get("title_ja", "")),
                "scope": str(record.get("scope", "")),
                "key": str(record.get("key", "")),
                "seen_count": _stat_int(record, "seen_count"),
                "first_seen": record.get("first_seen"),
                "last_seen": record.get("last_seen"),
                "approved_at": record.get("approved_at"),
                "approved_by": record.get("approved_by"),
                "auto_paused_at": record.get("auto_paused_at"),
                "auto_pause_reason_ja": record.get("auto_pause_reason_ja"),
                "resumed_at": record.get("resumed_at"),
                "resumed_by": record.get("resumed_by"),
                "rejected_at": record.get("rejected_at"),
                "rejected_by": record.get("rejected_by"),
                "proposed_change": (
                    dict(proposed_change) if isinstance(proposed_change, Mapping) else {}
                ),
            }
        )
    return output


def _policy_specificity(policy: ApprovedTargetPolicy) -> int:
    return {
        "by_symbol_direction": 5,
        "by_symbol": 4,
        "by_direction": 3,
        "by_confidence": 2,
        "overall": 1,
    }.get(policy.scope, 0)


def _policy_pause_reason(stats: Mapping[str, object]) -> str:
    expectancy = _stat_float(stats, "expectancy_r")
    profit_factor = _stat_float(stats, "profit_factor_r")
    avg_quality = _stat_float(stats, "avg_path_quality")
    tradable = _stat_int(stats, "tradable")
    if expectancy is not None and expectancy <= 0:
        return f"承認済みTP/SLの適用後期待Rが{_fmt_signed(expectancy, 'R')}に悪化(n={tradable})"
    if profit_factor is not None and profit_factor < 1.0:
        return f"承認済みTP/SLの適用後PFが{_fmt_number(profit_factor)}に悪化(n={tradable})"
    if avg_quality is not None and avg_quality < QUALITY_WARN_THRESHOLD:
        return (
            f"承認済みTP/SLの適用後サンプルの経路品質が{_fmt_pct(avg_quality)}と低い(n={tradable})"
        )
    return ""


def _target_policy_matches(
    policy: ApprovedTargetPolicy,
    symbol: str,
    direction: str,
    conviction: int,
) -> bool:
    symbol = symbol.upper()
    if direction not in {"long", "short"}:
        return False
    if policy.scope == "overall":
        return True
    if policy.scope == "by_symbol_direction":
        return policy.key == f"{symbol}:{direction}"
    if policy.scope == "by_symbol":
        return policy.key == symbol
    if policy.scope == "by_direction":
        return policy.key == direction
    if policy.scope == "by_confidence":
        return policy.key == _confidence_label_from_int(conviction)
    return False


def _finding(
    scope: str,
    key: str,
    label: str,
    severity: str,
    rank: int,
    evaluated: int,
    tradable: int,
    min_samples: int,
    stats: Mapping[str, object],
    reason_ja: str,
) -> dict:
    return {
        "scope": scope,
        "key": key,
        "label": label,
        "severity": severity,
        "rank": rank,
        "evaluated": evaluated,
        "tradable": tradable,
        "min_samples": min_samples,
        "sample_ok": bool(stats.get("sample_ok")),
        "expectancy_r": _stat_float(stats, "expectancy_r"),
        "profit_factor_r": _stat_float(stats, "profit_factor_r"),
        "avg_mfe_r": _stat_float(stats, "avg_mfe_r"),
        "avg_mae_r": _stat_float(stats, "avg_mae_r"),
        "avg_path_quality": _stat_float(stats, "avg_path_quality"),
        "reason_ja": reason_ja,
    }


def _tp1_retest_candidates(finding: Mapping[str, object]) -> list[float]:
    avg_mfe = _stat_float(finding, "avg_mfe_r")
    if avg_mfe is None or avg_mfe <= 0:
        return [0.8, 1.0]
    primary = max(0.5, min(1.2, round(avg_mfe * 0.8, 2)))
    return list(dict.fromkeys([primary, 1.0, 0.75 if primary < 0.9 else primary]))[:3]


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _int(value: object) -> int:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _float(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    if math.isinf(value):
        return value
    return round(value, digits)


def _stat_int(mapping: Mapping[str, object], key: str) -> int:
    return _int(mapping.get(key))


def _stat_float(mapping: Mapping[str, object], key: str) -> float | None:
    value = mapping.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _fmt_number(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "—"
    if math.isinf(value):
        return f"∞{suffix}"
    return f"{value:.2f}{suffix}"


def _fmt_signed(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "—"
    if math.isinf(value):
        return f"∞{suffix}"
    return f"{value:+.2f}{suffix}"


def _format_details(details: Mapping[str, object]) -> str:
    return ", ".join(f"{key}={value}" for key, value in details.items() if value is not None)
