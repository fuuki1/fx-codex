"""Trade-like outcome scoring for fx_intel briefing decisions.

This module turns directional briefing journal rows into trade-quality outcomes:
MFE/MAE, TP/SL touches, realized R, expectancy, and path-data quality.  The
first implementation intentionally works with the existing journal close series
so it can evaluate historical logs immediately.  Because close-only paths cannot
prove intrabar order, every outcome carries quality flags and aggregate sample
guards.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
import math

from .coerce import to_int
from .journal import DEFAULT_HORIZON_HOURS, DEFAULT_TOLERANCE_HOURS
from .market import WEEKEND_CLOSURE, open_hours_between

MIN_PATH_POINTS = 3
MIN_PATH_COVERAGE = 0.50
MIN_PATH_QUALITY = 0.35
MIN_EXPECTANCY_SAMPLES = 20
MIN_GROUP_EXPECTANCY_SAMPLES = 12
CLOSE_ONLY_QUALITY_CAP = 0.70
POINTS_FOR_FULL_DENSITY = 12
WEAK_PROFIT_FACTOR = 1.05
QUALITY_WARN_THRESHOLD = 0.55

CONFIDENCE_BINS = ((0, 25), (25, 50), (50, 75), (75, 101))
STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
EXPECTANCY_BLOCK_FACTOR = 0.45


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
    first_touch: str = "none"  # tp1 / tp2 / sl / none / unscored
    first_touch_ts: str | None = None
    realized_r: float | None = None
    path_points: int = 0
    path_start: str | None = None
    path_end: str | None = None
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
            "path_coverage": self.path_coverage,
            "path_quality": self.path_quality,
            "quality_flags": list(self.quality_flags),
            "tradable": self.tradable,
        }


@dataclass(frozen=True)
class ExpectancyStats:
    """Aggregate R-multiple and path-quality statistics."""

    evaluated: int = 0
    tradable: int = 0
    low_quality: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    avg_r: float | None = None
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
            "avg_r": self.avg_r,
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


@dataclass(frozen=True)
class TradeImprovementCandidate:
    """One deterministic improvement proposal derived from expectancy findings."""

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


def evaluate_trade_outcomes(
    entries: Iterable[Mapping[str, object]],
    *,
    horizon_hours: float = DEFAULT_HORIZON_HOURS,
    tolerance_hours: float = DEFAULT_TOLERANCE_HOURS,
    min_path_points: int = MIN_PATH_POINTS,
) -> list[TradeOutcome]:
    """Score directional journal rows over the post-decision path.

    The price path is built from every journal row with a numeric ``close`` for
    the same symbol.  Directionless rows therefore contribute future prices but
    are not scored as trades.
    """

    materialized = list(entries)
    prices: dict[str, list[tuple[datetime, float]]] = {}
    parsed_entries: list[tuple[datetime, Mapping[str, object]]] = []
    for entry in materialized:
        ts = _parse_ts(entry.get("ts"))
        if ts is None:
            continue
        parsed_entries.append((ts, entry))
        close = _float(entry.get("close"))
        if close is not None:
            prices.setdefault(str(entry.get("symbol", "")).upper(), []).append((ts, close))
    for series in prices.values():
        series.sort(key=lambda point: point[0])
    price_times = {symbol: [point[0] for point in series] for symbol, series in prices.items()}

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
        atr = _float(entry.get("atr"))
        conviction = to_int(entry.get("conviction"))
        data_quality = _float(entry.get("data_quality"))
        series = prices.get(symbol, [])
        times = price_times.get(symbol, [])
        future = _future_path(series, times, ts, horizon_hours, tolerance_hours)

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
                    atr=atr,
                    risk_distance=risk_distance,
                    first_touch="unscored",
                    quality_flags=tuple(dict.fromkeys(missing_flags)),
                )
            )
            continue

        assert entry_price is not None
        assert risk_distance is not None
        terminal_ts, terminal_price = future[-1]
        signed_moves = [_signed_move(direction, entry_price, price) for _, price in future]
        mfe = max(signed_moves)
        mae = max(0.0, -min(signed_moves))
        terminal_r = signed_moves[-1] / risk_distance

        first_touch = "none"
        first_touch_ts = None
        tp1_hit = tp2_hit = sl_hit = False
        for point_ts, price in future:
            touch = _touch(direction, price, stop, target1, target2)
            tp1_hit = tp1_hit or touch in ("tp1", "tp2")
            tp2_hit = tp2_hit or touch == "tp2"
            sl_hit = sl_hit or touch == "sl"
            if first_touch == "none" and touch != "none":
                first_touch = touch
                first_touch_ts = point_ts.isoformat()

        realized_r = _realized_r(first_touch, terminal_r)
        quality, flags = _path_quality(ts, future, horizon_hours, min_path_points)
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
                path_start=future[0][0].isoformat(),
                path_end=terminal_ts.isoformat(),
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
    """Build a JSON-friendly expectancy summary with sample guards."""

    return {
        "schema": 1,
        "overall": aggregate_expectancy(outcomes, min_samples=min_samples).to_dict(),
        "by_symbol": _aggregate_by(
            outcomes,
            lambda outcome: outcome.symbol,
            min_samples=group_min_samples,
        ),
        "by_direction": _aggregate_by(
            outcomes,
            lambda outcome: outcome.direction,
            min_samples=group_min_samples,
        ),
        "by_confidence": _aggregate_by(
            outcomes,
            _confidence_label,
            min_samples=group_min_samples,
        ),
        "quality": quality_summary(outcomes),
    }


def expectancy_findings(summary: Mapping[str, object], *, limit: int = 5) -> list[dict]:
    """Return weak expectancy/sample/quality cells sorted by operational priority."""
    if not isinstance(summary, Mapping):
        return []
    candidates: list[dict] = []
    overall = summary.get("overall")
    if isinstance(overall, Mapping):
        finding = _expectancy_finding("全体", "", overall)
        if finding is not None:
            candidates.append(finding)
    for group_key, scope in (
        ("by_symbol", "通貨ペア"),
        ("by_direction", "方向"),
        ("by_confidence", "確信度"),
    ):
        group = summary.get(group_key)
        if not isinstance(group, Mapping):
            continue
        for key, stats in group.items():
            if not isinstance(stats, Mapping):
                continue
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


def format_expectancy_report_ja(summary: Mapping[str, object], *, limit: int = 5) -> str:
    """Format expectancy, sample guard, and path-quality status for CLI/audit output."""
    if not isinstance(summary, Mapping):
        return "トレード期待値監視: サマリーなし"
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
        flags = quality.get("flags")
        flag_text = "なし"
        if isinstance(flags, Mapping) and flags:
            flag_text = " / ".join(f"{key}={value}" for key, value in sorted(flags.items()))
        lines.append(
            "経路データ品質: "
            f"scored={_stat_int(quality, 'scored')}/{_stat_int(quality, 'evaluated')}"
            f" / 低品質={_stat_int(quality, 'low_quality')}"
            f" / 平均品質{_fmt_pct(_stat_float(quality, 'avg_path_quality'))}"
            f" / flags {flag_text}"
        )

    findings = expectancy_findings(summary, limit=limit)
    if not findings:
        lines.append("改善候補: 期待値悪化セルなし")
        return "\n".join(lines)

    lines.append("改善候補:")
    for finding in findings:
        lines.append(f"・{finding['label']}: {finding['reason_ja']}")
    return "\n".join(lines)


def improvement_candidates(
    summary: Mapping[str, object],
    *,
    limit: int = 5,
) -> list[TradeImprovementCandidate]:
    """Create deterministic paper-validation candidates from expectancy findings."""
    findings = expectancy_findings(summary, limit=limit)
    candidates: list[TradeImprovementCandidate] = []
    for index, finding in enumerate(findings, start=1):
        candidate = _candidate_from_finding(finding, index)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def format_improvement_candidates_ja(
    candidates: Sequence[TradeImprovementCandidate],
) -> str:
    """Format improvement candidates for CLI/audit output."""
    if not candidates:
        return "改善候補: なし"
    lines = ["改善候補(paper検証待ち):"]
    for candidate in candidates:
        lines.append(
            f"・[{candidate.priority}] {candidate.title_ja}: "
            f"{candidate.rationale_ja} / 検証: {candidate.validation_ja}"
        )
    return "\n".join(lines)


def check_expectancy_health(
    summary: Mapping[str, object],
    *,
    require_sample_ok: bool = False,
) -> TradeOutcomeHealthReport:
    """Convert an expectancy summary into operational OK/WARN/FAIL checks."""
    checks: list[TradeOutcomeHealthCheck] = []
    if not isinstance(summary, Mapping):
        return TradeOutcomeHealthReport(
            [TradeOutcomeHealthCheck("summary", STATUS_FAIL, "期待値サマリーが不正")]
        )
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
            TradeOutcomeHealthCheck(
                "sample",
                STATUS_FAIL,
                "期待値計算に使える判断がありません",
                {"evaluated": evaluated, "tradable": tradable},
            )
        )
    elif not sample_ok:
        status = STATUS_FAIL if require_sample_ok else STATUS_WARN
        checks.append(
            TradeOutcomeHealthCheck(
                "sample",
                status,
                "期待値サンプルが不足しています",
                {"tradable": tradable, "min_samples": min_samples},
            )
        )
    else:
        checks.append(
            TradeOutcomeHealthCheck(
                "sample",
                STATUS_OK,
                "期待値サンプルは最低件数を満たしています",
                {"tradable": tradable, "min_samples": min_samples},
            )
        )

    if sample_ok and expectancy_r is not None and expectancy_r <= 0:
        checks.append(
            TradeOutcomeHealthCheck(
                "expectancy",
                STATUS_FAIL,
                "全体期待Rが非正です",
                {"expectancy_r": expectancy_r},
            )
        )
    elif expectancy_r is None:
        checks.append(TradeOutcomeHealthCheck("expectancy", STATUS_WARN, "期待Rを計算できません"))
    else:
        checks.append(
            TradeOutcomeHealthCheck(
                "expectancy",
                STATUS_OK if expectancy_r > 0 else STATUS_WARN,
                "全体期待Rは正です" if expectancy_r > 0 else "期待Rはサンプル不足下の参考値です",
                {"expectancy_r": expectancy_r},
            )
        )

    quality = summary.get("quality")
    checks.append(_health_check_quality(quality if isinstance(quality, Mapping) else {}))

    blocking_findings = [
        finding
        for finding in expectancy_findings(summary, limit=20)
        if finding["severity"] in {"block", "quality_block"}
    ]
    if blocking_findings:
        checks.append(
            TradeOutcomeHealthCheck(
                "blocked_cells",
                STATUS_FAIL,
                "期待値ガード対象セルがあります",
                {"count": len(blocking_findings), "first": blocking_findings[0]["label"]},
            )
        )
    else:
        checks.append(
            TradeOutcomeHealthCheck("blocked_cells", STATUS_OK, "期待値ガード対象セルはありません")
        )
    return TradeOutcomeHealthReport(checks)


def format_expectancy_health_ja(report: TradeOutcomeHealthReport) -> str:
    status_label = {
        STATUS_OK: "OK",
        STATUS_WARN: "WARN",
        STATUS_FAIL: "FAIL",
    }[report.status]
    lines = [f"トレード期待値ヘルスチェック: {status_label}"]
    for check in report.checks:
        label = {
            STATUS_OK: "OK",
            STATUS_WARN: "WARN",
            STATUS_FAIL: "FAIL",
        }.get(check.status, check.status.upper())
        detail = _format_details(check.details)
        suffix = f" ({detail})" if detail else ""
        lines.append(f"- [{label}] {check.name}: {check.message}{suffix}")
    return "\n".join(lines)


def aggregate_expectancy(
    outcomes: Sequence[TradeOutcome],
    *,
    min_samples: int = MIN_EXPECTANCY_SAMPLES,
) -> ExpectancyStats:
    evaluated = len(outcomes)
    usable = [outcome for outcome in outcomes if outcome.realized_r is not None]
    tradable = [outcome for outcome in usable if outcome.tradable]
    low_quality = evaluated - len(tradable)
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
        low_quality=low_quality,
        wins=wins,
        losses=losses,
        win_rate=_round(wins / len(r_values)) if r_values else None,
        avg_r=_round(_mean(r_values)),
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


def _expectancy_finding(
    scope: str,
    key: str,
    stats: Mapping[str, object],
) -> dict | None:
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
            "SL/TPまたは将来経路価格が不足し、期待値評価に使えるサンプルがありません",
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
    return {
        "scope": "データ品質",
        "key": "path",
        "label": "データ品質 経路価格",
        "severity": "quality_warn",
        "rank": 3,
        "evaluated": evaluated,
        "tradable": scored,
        "min_samples": 0,
        "sample_ok": False,
        "expectancy_r": None,
        "profit_factor_r": None,
        "avg_mfe_r": None,
        "avg_mae_r": None,
        "reason_ja": reason,
    }


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
    if low_quality / evaluated >= 0.5:
        return TradeOutcomeHealthCheck(
            "path_quality", STATUS_WARN, "低品質な経路サンプルが多い状態です", details
        )
    if avg_quality is not None and avg_quality < QUALITY_WARN_THRESHOLD:
        return TradeOutcomeHealthCheck(
            "path_quality", STATUS_WARN, "平均経路品質が低い状態です", details
        )
    return TradeOutcomeHealthCheck(
        "path_quality", STATUS_OK, "経路品質は監査可能な範囲です", details
    )


def _candidate_from_finding(
    finding: Mapping[str, object],
    index: int,
) -> TradeImprovementCandidate | None:
    severity = str(finding.get("severity", ""))
    scope = str(finding.get("scope", ""))
    key = str(finding.get("key", ""))
    label = str(finding.get("label", scope or "全体"))
    reason = str(finding.get("reason_ja", ""))
    candidate_id = _candidate_id(finding, index)
    if severity == "sample_guard":
        tradable = _stat_int(finding, "tradable")
        min_samples = _stat_int(finding, "min_samples")
        needed = max(0, min_samples - tradable)
        return TradeImprovementCandidate(
            candidate_id=candidate_id,
            scope=scope,
            key=key,
            priority="low",
            action_type="collect_samples",
            title_ja=f"{label}のサンプル蓄積を優先",
            rationale_ja=reason,
            proposed_change={
                "execution_policy": "keep_guarded",
                "needed_samples": needed,
                "parameter_change": "none_until_sample_ok",
            },
            validation_ja=f"有効サンプルをあと{needed}件以上追加し、期待R/PFを再評価",
            guardrail_ja="サンプル不足の間はTP/SLや重みを最適化しない",
            source_finding=dict(finding),
        )
    if severity in {"quality_warn", "quality_block"}:
        return TradeImprovementCandidate(
            candidate_id=candidate_id,
            scope=scope,
            key=key,
            priority="high" if severity == "quality_block" else "medium",
            action_type="improve_path_quality",
            title_ja=f"{label}の経路データ品質を補強",
            rationale_ja=reason,
            proposed_change={
                "path_source": "use_high_low_ohlc_or_tick_bars",
                "min_path_points": MIN_PATH_POINTS,
                "target_avg_path_quality": QUALITY_WARN_THRESHOLD,
            },
            validation_ja="high/low付き経路でTP/SL到達順を再採点し、品質WARNが消えるか確認",
            guardrail_ja="close系列だけの低品質結果ではTP/SL最適化を採用しない",
            source_finding=dict(finding),
        )
    if severity == "block":
        return TradeImprovementCandidate(
            candidate_id=candidate_id,
            scope=scope,
            key=key,
            priority="high",
            action_type="expectancy_guard",
            title_ja=f"{label}を見送り優先に戻す",
            rationale_ja=reason,
            proposed_change={
                "conviction_factor": EXPECTANCY_BLOCK_FACTOR,
                "execution_policy": "skip_new_entries",
                "paper_validation_required": True,
            },
            validation_ja="次の最低サンプル到達時に期待R>0、PF>=1.05、平均MFE>平均MAEを確認",
            guardrail_ja="期待Rが非正のセルは自動解除せず、paper検証で改善確認後に解除",
            source_finding=dict(finding),
        )
    if severity == "weak":
        tp1_candidates = _tp1_retest_candidates(finding)
        return TradeImprovementCandidate(
            candidate_id=candidate_id,
            scope=scope,
            key=key,
            priority="medium",
            action_type="tp_sl_entry_retest",
            title_ja=f"{label}のTP/SL・エントリー条件を再検証",
            rationale_ja=reason,
            proposed_change={
                "tp1_r_candidates": tp1_candidates,
                "entry_policy": "confirmation_only",
                "avoid_market_entry": True,
                "min_profit_factor_r": WEAK_PROFIT_FACTOR,
            },
            validation_ja="候補TP1と確認型エントリーをpaperで比較し、期待RとPFが改善するものだけ採用",
            guardrail_ja="既存SL/TPを即時変更せず、同一サンプル窓でA/B検証してから昇格",
            source_finding=dict(finding),
        )
    return None


def _tp1_retest_candidates(finding: Mapping[str, object]) -> list[float]:
    avg_mfe = _stat_float(finding, "avg_mfe_r")
    if avg_mfe is None or avg_mfe <= 0:
        return [0.8, 1.0]
    primary = max(0.5, min(1.2, round(avg_mfe * 0.8, 2)))
    candidates = [primary, 1.0]
    if primary < 0.9:
        candidates.append(0.75)
    output: list[float] = []
    for value in candidates:
        if value not in output:
            output.append(value)
    return output[:3]


def _candidate_id(finding: Mapping[str, object], index: int) -> str:
    scope = str(finding.get("scope", "all"))
    key = str(finding.get("key", "")) or "overall"
    severity = str(finding.get("severity", "unknown"))
    raw = f"{scope}:{key}:{severity}".lower()
    normalized = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return f"trade-improvement-{index}-{normalized or 'overall'}"


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
        "reason_ja": reason_ja,
    }


def _future_path(
    series: Sequence[tuple[datetime, float]],
    times: Sequence[datetime],
    ts: datetime,
    horizon_hours: float,
    tolerance_hours: float,
) -> list[tuple[datetime, float]]:
    lower = ts
    upper = ts + timedelta(hours=horizon_hours + tolerance_hours) + WEEKEND_CLOSURE
    future: list[tuple[datetime, float]] = []
    for index in range(bisect_left(times, lower), len(series)):
        point_ts, close = series[index]
        if point_ts <= ts:
            continue
        if point_ts > upper:
            break
        age = open_hours_between(ts, point_ts)
        if 0.0 < age <= horizon_hours + tolerance_hours:
            future.append((point_ts, close))
    return future


def _path_quality(
    ts: datetime,
    future: Sequence[tuple[datetime, float]],
    horizon_hours: float,
    min_path_points: int,
) -> tuple[float, tuple[str, ...]]:
    flags = ["close_only_path"]
    points = len(future)
    coverage = _coverage(ts, future[-1][0], horizon_hours) if future else 0.0
    point_ratio = min(1.0, points / POINTS_FOR_FULL_DENSITY)
    quality = min(CLOSE_ONLY_QUALITY_CAP, 0.65 * coverage + 0.35 * point_ratio)
    if points < min_path_points:
        flags.append("insufficient_path_points")
    if coverage < MIN_PATH_COVERAGE:
        flags.append("short_path_coverage")
    if quality < MIN_PATH_QUALITY:
        flags.append("low_path_quality")
    return round(max(0.0, min(1.0, quality)), 4), tuple(flags)


def _coverage(ts: datetime, last_ts: datetime, horizon_hours: float) -> float:
    if horizon_hours <= 0:
        return 0.0
    return max(0.0, min(1.0, open_hours_between(ts, last_ts) / horizon_hours))


def _touch(
    direction: str,
    price: float,
    stop: float | None,
    target1: float | None,
    target2: float | None,
) -> str:
    if stop is None or target1 is None:
        return "none"
    if direction == "long":
        if price <= stop:
            return "sl"
        if target2 is not None and price >= target2:
            return "tp2"
        if price >= target1:
            return "tp1"
    else:
        if price >= stop:
            return "sl"
        if target2 is not None and price <= target2:
            return "tp2"
        if price <= target1:
            return "tp1"
    return "none"


def _realized_r(first_touch: str, terminal_r: float) -> float:
    if first_touch == "sl":
        return -1.0
    if first_touch == "tp2":
        return 2.0
    if first_touch == "tp1":
        return 1.0
    return terminal_r


def _signed_move(direction: str, entry: float, price: float) -> float:
    return price - entry if direction == "long" else entry - price


def _confidence_label(outcome: TradeOutcome) -> str:
    for low, high in CONFIDENCE_BINS:
        if low <= outcome.conviction < high:
            return f"{low}-{high - 1}"
    return "unknown"


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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
    return to_int(mapping.get(key))


def _stat_float(mapping: Mapping[str, object], key: str) -> float | None:
    value = mapping.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return None


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
