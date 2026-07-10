"""Feed TP/SL/MFE/MAE failure analysis back into the next chart decision."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, UTC
import json
from pathlib import Path
import tempfile
from typing import Any

from .market import open_hours_between
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
STATUS_PASS = "pass"
STATUS_PENDING = "pending"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
PENDING_QUALITY_FLAGS = {"no_future_prices", "short_path_coverage"}
TRADABLE_ZERO_REASON_DEFS = {
    "pending_horizon_not_mature": (
        "主ホライズン未成熟",
        "24hなど主ホライズン経過後に再採点する",
    ),
    "no_future_prices": (
        "将来価格なし",
        "価格スナップショットまたは後続判断ログを継続保存する",
    ),
    "close_only_path": (
        "closeのみの価格経路",
        "high/low付き価格系列へ切り替えてTP/SL先着品質を上げる",
    ),
    "insufficient_path_points": (
        "経路点不足",
        "5分スナップショットを継続稼働させる",
    ),
    "low_path_quality": (
        "経路品質不足",
        "十分な経路カバレッジとOHLCを確保する",
    ),
    "missing_risk_levels": (
        "TP/SL不足",
        "判断ログにstop/target1/target2を必ず保存する",
    ),
    "invalid_risk_distance": (
        "リスク幅不正",
        "entryとstopの距離が正になるTP/SLを保存する",
    ),
    "other_low_quality": (
        "その他の採点品質不足",
        "quality_flagsとfailure_reasonsを確認する",
    ),
}

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

    def expectancy_lookup(
        self, symbol: str, timeframe: str
    ) -> Callable[[str, str, int], tuple[float, str, bool]] | None:
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
        lines.extend(f"・{cell.reason_ja}" for cell in actionable[:limit])
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
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(
                json_safe(profile.to_dict()),
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
        tmp_path.replace(target)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


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

    grouped: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}
    for outcome in _iter_outcomes(report or {}):
        direction = str(outcome.get("direction", ""))
        if direction not in ("long", "short"):
            continue
        symbol = str(outcome.get("symbol", "")).upper()
        timeframe = str(outcome.get("timeframe", "fusion") or "fusion")
        if not symbol:
            continue
        grouped.setdefault((symbol, timeframe, direction), []).append(outcome)

    cells = {key: _derive_cell(*key, rows) for key, rows in sorted(grouped.items())}
    profile = DecisionFeedbackProfile(generated_at=generated_at.isoformat(), cells=cells)
    profile.notes_ja = _notes_ja(profile)
    return profile


def feedback_findings(
    profile: DecisionFeedbackProfile,
    report: Mapping[str, object] | None = None,
    *,
    require_sample_ok: bool = False,
    now: datetime | None = None,
    limit: int = 20,
) -> list[dict[str, object]]:
    """Return operational findings for expected-R monitoring."""

    findings: list[dict[str, object]] = []
    generated_at = _utc(now or datetime.now(UTC))
    report = report or {}
    overall = _overall_stats(report)
    tradable_zero = _tradable_zero_reasons(report, generated_at)
    if overall:
        evaluated = _int(overall.get("evaluated"))
        tradable = _int(overall.get("tradable"))
        min_samples = _int(overall.get("min_samples"))
        sample_ok = bool(overall.get("sample_ok"))
        expectancy = _float(overall.get("expectancy_r"))
        if evaluated <= 0:
            findings.append(
                {
                    "scope": "overall",
                    "key": "overall",
                    "severity": "warn",
                    "action": "collect_samples",
                    "message_ja": "完全判断ログの期待R監視対象がまだありません",
                    "evaluated": evaluated,
                    "tradable": tradable,
                    "min_samples": min_samples,
                }
            )
        elif tradable <= 0:
            pending_count = _int(tradable_zero.get("pending_count"))
            blocking_count = _int(tradable_zero.get("blocking_count"))
            pending_only = pending_count > 0 and blocking_count <= 0
            findings.append(
                {
                    "scope": "overall",
                    "key": "overall",
                    "severity": STATUS_PENDING if pending_only else STATUS_FAIL,
                    "action": "collect_samples" if pending_only else "quality_guard",
                    "message_ja": (
                        "主ホライズン未成熟のため期待Rはpendingです"
                        if pending_only
                        else "期待R計算に使えるTP/SL採点済み判断がありません"
                    ),
                    "evaluated": evaluated,
                    "tradable": tradable,
                    "min_samples": min_samples,
                    "tradable_zero_reasons": tradable_zero.get("reasons", []),
                }
            )
        elif not sample_ok:
            findings.append(
                {
                    "scope": "overall",
                    "key": "overall",
                    "severity": STATUS_FAIL if require_sample_ok else STATUS_WARN,
                    "action": "collect_samples",
                    "message_ja": "完全判断ログの期待Rサンプルが不足しています",
                    "evaluated": evaluated,
                    "tradable": tradable,
                    "min_samples": min_samples,
                }
            )
        if sample_ok and expectancy is not None and expectancy <= 0:
            findings.append(
                {
                    "scope": "overall",
                    "key": "overall",
                    "severity": STATUS_FAIL,
                    "action": "avoid",
                    "message_ja": f"完全判断ログの全体期待Rが{expectancy:+.2f}Rで非正です",
                    "expectancy_r": _round(expectancy),
                    "evaluated": evaluated,
                    "tradable": tradable,
                    "min_samples": min_samples,
                }
            )

    for (symbol, timeframe, direction), cell in profile.cells.items():
        if cell.action not in {"avoid", "dampen", "quality_guard"}:
            continue
        key = f"{symbol}|{timeframe}|{direction}"
        pending_cell = (
            cell.action == "quality_guard"
            and cell.tradable <= 0
            and _cell_pending_only(report, generated_at, symbol, timeframe, direction)
        )
        severity = (
            STATUS_PENDING
            if pending_cell
            else STATUS_FAIL if cell.block or cell.action == "avoid" else STATUS_WARN
        )
        findings.append(
            {
                "scope": "cell",
                "key": key,
                "label_ja": _cell_label(symbol, timeframe, direction),
                "severity": severity,
                "action": "collect_samples" if pending_cell else cell.action,
                "factor": cell.factor,
                "block": cell.block,
                "evaluated": cell.evaluated,
                "tradable": cell.tradable,
                "expectancy_r": cell.expectancy_r,
                "hit_rate": cell.hit_rate,
                "sl_rate": cell.sl_rate,
                "tp_rate": cell.tp_rate,
                "avg_mfe_r": cell.avg_mfe_r,
                "avg_mae_r": cell.avg_mae_r,
                "high_severity_rate": cell.high_severity_rate,
                "top_failure_reasons": [reason.to_dict() for reason in cell.failure_reasons[:3]],
                "message_ja": (
                    f"{_cell_label(symbol, timeframe, direction)}: 主ホライズン未成熟のためpending"
                    if pending_cell
                    else cell.reason_ja
                ),
            }
        )

    findings.sort(
        key=lambda item: (
            _severity_rank(str(item.get("severity", ""))),
            _action_rank(str(item.get("action", ""))),
            -_int(item.get("tradable")),
            _sort_expectancy(item.get("expectancy_r")),
            str(item.get("key", "")),
        )
    )
    return findings[: max(0, limit)]


def build_monitoring_snapshot(
    profile: DecisionFeedbackProfile,
    report: Mapping[str, object] | None = None,
    *,
    now: datetime | None = None,
    require_sample_ok: bool = False,
    price_health: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Build the JSON payload used by cron/CI/dashboard expected-R monitoring."""

    generated_at = _utc(now or datetime.now(UTC))
    report = report or {}

    findings = feedback_findings(
        profile,
        report,
        require_sample_ok=require_sample_ok,
        now=generated_at,
    )
    action_counts: Counter[str] = Counter(cell.action for cell in profile.cells.values())
    evaluated_cells = [cell for cell in profile.cells.values() if cell.tradable > 0]
    mature_cells = [
        cell for cell in profile.cells.values() if cell.tradable >= MIN_FEEDBACK_SAMPLES
    ]
    tradable_zero = _tradable_zero_reasons(report, generated_at)
    overall = _overall_stats(report)
    failure_summary = _failure_summary(report)
    price_health_payload = dict(price_health or {})
    status = _monitor_status(
        findings,
        mature_cells=mature_cells,
        tradable_zero=tradable_zero,
        price_health=price_health_payload,
    )
    performance = _performance_summary(report)
    model_delta = _model_expectancy_delta(report)

    return {
        "schema": 1,
        "generated_at": generated_at.isoformat(),
        "status": status,
        "exit_code": 1 if status == STATUS_FAIL else 0,
        "summary": {
            "decision_events": _int((report or {}).get("decision_events")),
            "scored_outcomes": _int((report or {}).get("scored_outcomes")),
            "cell_count": len(profile.cells),
            "mature_cell_count": len(mature_cells),
            "action_counts": dict(sorted(action_counts.items())),
            "overall": dict(overall),
            "performance": performance,
            "tradable_zero_reasons": tradable_zero,
            "model_expectancy_delta": model_delta,
            "price_health": price_health_payload,
            "failure_reason_summary": failure_summary[:10],
            "best_cells": [
                cell.to_dict()
                for cell in sorted(
                    evaluated_cells,
                    key=lambda item: (
                        -_sort_expectancy_for_best(item.expectancy_r),
                        -item.tradable,
                        item.symbol,
                    ),
                )[:5]
            ],
            "worst_cells": [
                cell.to_dict()
                for cell in sorted(
                    evaluated_cells,
                    key=lambda item: (
                        _sort_expectancy(item.expectancy_r),
                        -item.tradable,
                        item.symbol,
                    ),
                )[:5]
            ],
        },
        "findings": findings,
        "alerts": [
            {
                "severity": _alert_severity(str(finding.get("severity", ""))),
                "message_ja": str(finding.get("message_ja") or ""),
                "action_ja": _monitor_action_ja(finding),
                "source": "decision_expectancy",
                "key": finding.get("key"),
            }
            for finding in findings[:10]
        ],
        "price_health": price_health_payload,
        "profile": profile.to_dict(),
    }


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
    r_values: list[float] = [
        value for row in tradable_rows if (value := _float(row.get("realized_r"))) is not None
    ]
    wins = sum(1 for value in r_values if value > 0)
    losses = sum(1 for value in r_values if value < 0)
    unscored = sum(1 for row in rows if _float(row.get("realized_r")) is None)
    low_quality = sum(
        1
        for row in rows
        if _float(row.get("realized_r")) is not None and not bool(row.get("tradable"))
    )
    mfe_values: list[float] = [
        value for row in tradable_rows if (value := _float(row.get("mfe_r"))) is not None
    ]
    mae_values: list[float] = [
        value for row in tradable_rows if (value := _float(row.get("mae_r"))) is not None
    ]
    sl_count = sum(1 for row in tradable_rows if row.get("first_touch") == "sl")
    tp_count = sum(1 for row in tradable_rows if row.get("first_touch") in {"tp1", "tp2"})
    reason_stats = _reason_stats(rows)
    high_severity_hits = sum(1 for row in rows if _row_has_any_reason(row, HIGH_SEVERITY_REASONS))
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
            f"{label}: 失敗分類ベースで期待R{expectancy:+.2f}R、SL率{_fmt_pct(sl_rate)}。"
            f"見送り優先 [{top_reasons}]",
        )

    adverse_dominant = avg_mfe is not None and avg_mae is not None and avg_mae >= avg_mfe
    target_retest = any(reason.key in TARGET_RETEST_REASONS for reason in reason_stats[:3])
    should_dampen = (
        (expectancy is not None and expectancy <= 0)
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
            f" (期待R {_fmt_signed(expectancy, 'R')}, MFE {_fmt_number(avg_mfe, 'R')}, "
            f"MAE {_fmt_number(avg_mae, 'R')})",
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


def _overall_stats(report: Mapping[str, object]) -> Mapping[str, object]:
    summary = report.get("summary")
    if not isinstance(summary, Mapping):
        return {}
    overall = summary.get("overall")
    return overall if isinstance(overall, Mapping) else {}


def _failure_summary(report: Mapping[str, object]) -> list[dict[str, object]]:
    raw = report.get("failure_reason_summary")
    if not isinstance(raw, list):
        return []
    return [dict(row) for row in raw if isinstance(row, Mapping)]


def _tradable_zero_reasons(
    report: Mapping[str, object],
    now: datetime,
) -> dict[str, object]:
    rows = [row for row in _iter_outcomes(report) if not bool(row.get("tradable"))]
    counts: Counter[str] = Counter()
    reason_pending_counts: Counter[str] = Counter()
    reason_blocking_counts: Counter[str] = Counter()
    pending_count = 0
    blocking_count = 0
    for row in rows:
        flags = _quality_flags(row)
        keys = _failure_keys(row)
        pending = _row_pending_immature(row, now)
        row_blocking = False
        row_reasons: list[str] = []

        if "missing_risk_levels" in flags:
            row_reasons.append("missing_risk_levels")
            row_blocking = True
        if "invalid_risk_distance" in flags:
            row_reasons.append("invalid_risk_distance")
            row_blocking = True

        if pending:
            row_reasons.append("pending_horizon_not_mature")
        elif "no_future_prices" in flags:
            row_reasons.append("no_future_prices")
            row_blocking = True

        for key in ("close_only_path", "insufficient_path_points", "low_path_quality"):
            if key in flags:
                row_reasons.append(key)
                if not pending:
                    row_blocking = True

        if "low_path_quality" in keys and "low_path_quality" not in row_reasons:
            row_reasons.append("low_path_quality")
            if not pending:
                row_blocking = True

        if not row_reasons:
            row_reasons.append("other_low_quality")
            row_blocking = True

        unique_reasons = list(dict.fromkeys(row_reasons))
        for key in unique_reasons:
            counts[key] += 1
        if pending and not row_blocking:
            pending_count += 1
            for key in unique_reasons:
                reason_pending_counts[key] += 1
        if row_blocking:
            blocking_count += 1
            for key in unique_reasons:
                if key != "pending_horizon_not_mature":
                    reason_blocking_counts[key] += 1

    total = len(rows)
    reason_rows: list[dict[str, object]] = []
    for key, count in counts.items():
        label, advice = TRADABLE_ZERO_REASON_DEFS.get(
            key,
            (key, "quality_flagsとfailure_reasonsを確認する"),
        )
        reason_rows.append(
            {
                "key": key,
                "label_ja": label,
                "count": count,
                "rate": round(count / total, 4) if total else 0.0,
                "pending_count": reason_pending_counts.get(key, 0),
                "blocking_count": reason_blocking_counts.get(key, 0),
                "pending": reason_pending_counts.get(key, 0) > 0,
                "blocking": reason_blocking_counts.get(key, 0) > 0,
                "advice_ja": advice,
            }
        )
    reason_rows.sort(
        key=lambda item: (
            0 if item["key"] == "pending_horizon_not_mature" else 1,
            -_int(item.get("count")),
            str(item.get("key")),
        )
    )
    return {
        "total_non_tradable": total,
        "pending_count": pending_count,
        "blocking_count": blocking_count,
        "reasons": reason_rows,
    }


def _row_pending_immature(row: Mapping[str, object], now: datetime) -> bool:
    flags = _quality_flags(row)
    if not flags.intersection(PENDING_QUALITY_FLAGS):
        return False
    ts = _parse_ts(row.get("ts"))
    horizon = _float(row.get("horizon_hours"))
    if ts is None or horizon is None or horizon <= 0:
        return False
    return open_hours_between(ts, now) < horizon


def _cell_pending_only(
    report: Mapping[str, object],
    now: datetime,
    symbol: str,
    timeframe: str,
    direction: str,
) -> bool:
    rows = [
        row
        for row in _iter_outcomes(report)
        if str(row.get("symbol", "")).upper() == symbol
        and str(row.get("timeframe", "fusion") or "fusion") == timeframe
        and str(row.get("direction", "")) == direction
    ]
    if not rows:
        return False
    return all(
        not bool(row.get("tradable"))
        and _row_pending_immature(row, now)
        and not _row_has_immediate_blocker(row)
        for row in rows
    )


def _row_has_immediate_blocker(row: Mapping[str, object]) -> bool:
    flags = _quality_flags(row)
    return bool(flags.intersection({"missing_risk_levels", "invalid_risk_distance"}))


def _quality_flags(row: Mapping[str, object]) -> set[str]:
    raw = row.get("quality_flags")
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if str(item)}


def _failure_keys(row: Mapping[str, object]) -> set[str]:
    raw = row.get("failure_reasons")
    if not isinstance(raw, list):
        return set()
    return {
        str(reason.get("key"))
        for reason in raw
        if isinstance(reason, Mapping) and str(reason.get("key", ""))
    }


def _performance_summary(report: Mapping[str, object]) -> dict[str, object]:
    outcomes = list(_iter_outcomes(report))
    tradable = [
        row
        for row in outcomes
        if bool(row.get("tradable")) and _float(row.get("realized_r")) is not None
    ]
    r_values = [_float(row.get("realized_r")) for row in tradable]
    mfe_values = [_float(row.get("mfe_r")) for row in tradable]
    mae_values = [_float(row.get("mae_r")) for row in tradable]
    r_numbers = [value for value in r_values if value is not None]
    mfe_numbers = [value for value in mfe_values if value is not None]
    mae_numbers = [value for value in mae_values if value is not None]
    return {
        "evaluated": len(outcomes),
        "tradable": len(tradable),
        "net_R": _round(sum(r_numbers)) if r_numbers else None,
        "expected_R": _round(_mean(r_numbers)),
        "avg_mfe_R": _round(_mean(mfe_numbers)),
        "avg_mae_R": _round(_mean(mae_numbers)),
    }


def _model_expectancy_delta(report: Mapping[str, object]) -> dict[str, object]:
    buckets: dict[str, list[float]] = {"baseline_model": [], "learning_model": []}
    for row in _iter_outcomes(report):
        realized = _float(row.get("realized_r"))
        if realized is None or not bool(row.get("tradable")):
            continue
        bucket = (
            "learning_model"
            if _has_learning_context(row.get("learning_context"))
            else "baseline_model"
        )
        buckets[bucket].append(realized)

    baseline = _model_bucket_stats(buckets["baseline_model"])
    learning = _model_bucket_stats(buckets["learning_model"])
    baseline_expected = _float(baseline.get("expected_R"))
    learning_expected = _float(learning.get("expected_R"))
    delta = (
        _round(learning_expected - baseline_expected)
        if baseline_expected is not None and learning_expected is not None
        else None
    )
    return {
        "baseline_model": baseline,
        "learning_model": learning,
        "delta_expected_R": delta,
        "status": "ready" if delta is not None else "insufficient_model_split",
    }


def _model_bucket_stats(values: list[float]) -> dict[str, object]:
    wins = sum(1 for value in values if value > 0)
    losses = sum(1 for value in values if value < 0)
    return {
        "tradable": len(values),
        "wins": wins,
        "losses": losses,
        "expected_R": _round(_mean(values)),
        "net_R": _round(sum(values)) if values else None,
    }


def _has_learning_context(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    direct_keys = (
        "decision_feedback",
        "trade_expectancy_summary",
        "timeframe_expectancy_summary",
        "tp_sl_learning",
        "timeframe_learning",
        "directional_learning",
    )
    if any(_meaningful(value.get(key)) for key in direct_keys):
        return True
    maximization = value.get("maximization")
    if isinstance(maximization, Mapping) and _meaningful(maximization.get("active_cell")):
        return True
    ml = value.get("ml")
    if isinstance(ml, Mapping) and bool(ml.get("usable")):
        return True
    promotion = value.get("promotion")
    if isinstance(promotion, Mapping):
        stages = promotion.get("stages")
        if isinstance(stages, Mapping) and any(str(stage) != "shadow" for stage in stages.values()):
            return True
    return False


def _meaningful(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        return any(_meaningful(item) for item in value.values())
    if isinstance(value, list):
        return any(_meaningful(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _monitor_status(
    findings: list[dict[str, object]],
    *,
    mature_cells: list[DecisionFeedbackCell],
    tradable_zero: Mapping[str, object],
    price_health: Mapping[str, object],
) -> str:
    price_status = str(price_health.get("status", "") or "")
    if price_status == STATUS_FAIL:
        return STATUS_FAIL
    if any(finding.get("severity") == STATUS_FAIL for finding in findings):
        return STATUS_FAIL
    if price_status == STATUS_WARN or any(
        finding.get("severity") == STATUS_WARN for finding in findings
    ):
        return STATUS_WARN
    if any(finding.get("severity") == STATUS_PENDING for finding in findings):
        return STATUS_PENDING
    if not mature_cells and _int(tradable_zero.get("total_non_tradable")) <= 0:
        return STATUS_WARN
    return STATUS_PASS


def _alert_severity(value: str) -> str:
    if value in {STATUS_FAIL, STATUS_WARN, STATUS_PENDING, STATUS_PASS}:
        return value
    return STATUS_WARN


def _monitor_action_ja(finding: Mapping[str, object]) -> str:
    action = str(finding.get("action", ""))
    severity = str(finding.get("severity", ""))
    if severity == STATUS_PENDING:
        return "主ホライズン経過後に同じ判断を再採点する"
    if action == "avoid":
        return "次回判断では該当セルを見送り優先。期待Rが改善するまでブロック扱い"
    if action == "dampen":
        factor = _float(finding.get("factor"))
        shown = f"{factor:.2f}" if factor is not None else "既定"
        return f"次回判断では確信度を×{shown}に減衰し、TP/SL到達後に再評価"
    if action == "quality_guard":
        return "採点用の価格経路・TP/SL設定・未採点理由を確認して品質を補強"
    return "サンプルが成熟するまで期待Rを継続監視"


def _severity_rank(value: str) -> int:
    return {STATUS_FAIL: 0, STATUS_WARN: 1, STATUS_PENDING: 2, "info": 3}.get(value, 4)


def _action_rank(value: str) -> int:
    return {
        "avoid": 0,
        "quality_guard": 1,
        "dampen": 2,
        "collect_samples": 3,
        "hold": 4,
    }.get(value, 9)


def _sort_expectancy(value: object) -> float:
    number = _float(value)
    return number if number is not None else float("inf")


def _sort_expectancy_for_best(value: object) -> float:
    number = _float(value)
    return number if number is not None else float("-inf")


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
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


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
