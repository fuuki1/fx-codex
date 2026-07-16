#!/usr/bin/env python3
"""予測→答え合わせ→学習→判断反映のE2Eループを読み取り専用で監査する。

Mac mini本番・開発機のどちらでも、logs/配下のファイルとlaunchd状態だけから
「ループが実際に回っているか」を判定する。プロセスが起動している・行数が
増えているだけでは合格にせず、各段に証拠(ファイル・行数・時刻・サンプル数)を
要求する。

設計上の制約:
- 標準ライブラリのみ(SSH経由で `python3 - --log-dir ...` としてstream実行できる)。
- 読み取り専用。書き込みは --json-out / --markdown-out で明示された先だけ。
- fx_intel をimportしない。市場休場・主ホライズン・許容誤差はここに複製し、
  乖離したらテストで検出する(tests/test_learning_loop_audit.py)。

使い方:
    python -m tools.learning_loop_audit --window-hours 72 \
        --json-out reports/learning-loop-audit.json \
        --markdown-out reports/learning-loop-audit.md

終了コード: 0=pass / 1=warn / 2=fail / 3=入力不正。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path

SCHEMA_VERSION = 1

# fx_intel.timeframe と同じ主ホライズン(市場オープン時間換算)と許容誤差。
PRIMARY_HORIZON_HOURS: dict[str, float] = {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}
HORIZON_TOLERANCE_HOURS: dict[float, float] = {0.25: 0.1, 1.0: 0.25, 4.0: 1.0, 24.0: 2.0}
FUSION_HORIZON_HOURS = 24.0
FUSION_TOLERANCE_HOURS = 2.0
ATR_FLAT_FRACTION = 0.1  # |値動き| < 0.1*ATR は flat(learning.DEFAULT_ATR_FRACTION)

# fx_intel.market と同じ週末クローズ(金曜21:00 UTC → 日曜22:00 UTC)。
WEEKEND_CLOSE_START_HOUR = 21  # Friday
WEEKEND_OPEN_HOUR = 22  # Sunday
WEEKEND_CLOSURE_HOURS = 49.0

# fx_intel.learning のサンプル数ガード(重み再推定/ペア減衰/状態別/ML)。
MIN_WEIGHT_SAMPLES = 20
MIN_SYMBOL_SAMPLES = 8
MIN_CONDITION_SAMPLES = 12
ML_MIN_TRAIN_ROWS = 150
DEFAULT_TECH_WEIGHT = 0.55

CAPTURE_INTERVAL_MINUTES = 5.0
STALE_WARN_MINUTES = 15.0  # 3周期
STALE_FAIL_MINUTES = 45.0  # 9周期(freshness criticalと同じ)
FUSION_EXPECTED_INTERVAL_MINUTES = 60.0

PASS = "pass"
WARN = "warn"
FAIL = "fail"
UNKNOWN = "unknown"
_STATUS_RANK = {PASS: 0, UNKNOWN: 1, WARN: 2, FAIL: 3}

LAUNCHD_LABELS = ("com.fx-codex.snapshot", "com.fx-codex.briefing", "com.fx-codex.health")
SCANNER_ERROR_PATTERNS = ("429", "JSONDecodeError", "Expecting value", "RateLimited")
TIMEOUT_PATTERNS = ("timed out", "timeout", "Timeout")
DUPLICATE_WRITER_PATTERN = "duplicate writer"


# ---------------------------------------------------------------------------
# 市場時間(fx_intel.marketの複製。乖離はテストで検出する)


def is_market_open(moment: datetime) -> bool:
    """金曜21:00 UTC〜日曜22:00 UTCを休場とする近似。"""
    utc_moment = moment.astimezone(UTC)
    weekday = utc_moment.weekday()  # Mon=0
    if weekday == 5:
        return False
    if weekday == 4 and utc_moment.hour >= WEEKEND_CLOSE_START_HOUR:
        return False
    if weekday == 6 and utc_moment.hour < WEEKEND_OPEN_HOUR:
        return False
    return True


def _weekend_closures_between(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """[start, end]に重なる週末クローズ区間(UTC)の一覧。"""
    if end <= start:
        return []
    closures: list[tuple[datetime, datetime]] = []
    # startの直前の金曜21:00から走査する
    probe = start - timedelta(days=7)
    probe = probe.replace(hour=0, minute=0, second=0, microsecond=0)
    while probe <= end:
        if probe.weekday() == 4:
            close_start = probe.replace(hour=WEEKEND_CLOSE_START_HOUR)
            close_end = close_start + timedelta(hours=WEEKEND_CLOSURE_HOURS)
            if close_end > start and close_start < end:
                closures.append((max(close_start, start), min(close_end, end)))
        probe += timedelta(days=1)
    return closures


def open_hours_between(start: datetime, end: datetime) -> float:
    """市場オープン時間換算の経過時間(時間)。end<=startは0。"""
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    if end_utc <= start_utc:
        return 0.0
    total = (end_utc - start_utc).total_seconds() / 3600.0
    closed = sum(
        (span_end - span_start).total_seconds() / 3600.0
        for span_start, span_end in _weekend_closures_between(start_utc, end_utc)
    )
    return max(0.0, total - closed)


def tolerance_for(horizon_hours: float) -> float:
    return HORIZON_TOLERANCE_HOURS.get(horizon_hours, 2.0)


# ---------------------------------------------------------------------------
# 入力読み込み


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


@dataclass
class JsonlFile:
    path: Path
    rows: list[dict] = field(default_factory=list)
    total_lines: int = 0
    malformed_lines: int = 0
    exists: bool = False

    @property
    def evidence(self) -> dict:
        return {
            "path": str(self.path),
            "exists": self.exists,
            "total_lines": self.total_lines,
            "malformed_lines": self.malformed_lines,
        }


def read_jsonl(path: Path) -> JsonlFile:
    result = JsonlFile(path=path)
    if not path.exists():
        return result
    result.exists = True
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                result.total_lines += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    result.malformed_lines += 1
                    continue
                if isinstance(row, dict):
                    result.rows.append(row)
                else:
                    result.malformed_lines += 1
    except OSError:
        result.exists = False
    return result


def read_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def _bucket_5m(ts: datetime) -> datetime:
    return ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# セクション判定


def _section(status: str, summary_ja: str, evidence: dict) -> dict:
    return {"status": status, "summary_ja": summary_ja, "evidence": evidence}


def _worst(*statuses: str) -> str:
    return max(statuses, key=lambda status: _STATUS_RANK.get(status, 1))


def audit_data_collection(
    prices: JsonlFile, now: datetime, window_hours: float, symbols: set[str]
) -> dict:
    """5分ごとの価格取得が期待周期・カバレッジで継続しているか。"""
    window_start = now - timedelta(hours=window_hours)
    cells: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for row in prices.rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts < window_start or ts > now:
            continue
        close = row.get("close")
        if not isinstance(close, (int, float)):
            continue
        cells[(str(row.get("symbol", "")), str(row.get("timeframe", "")))].append(ts)

    open_hours = open_hours_between(window_start, now)
    expected_slots = open_hours * 60.0 / CAPTURE_INTERVAL_MINUTES
    per_cell: dict[str, dict] = {}
    worst_gap_minutes = 0.0
    min_coverage = 1.0 if cells else 0.0
    last_ts_all: datetime | None = None
    for (symbol, timeframe), stamps in sorted(cells.items()):
        stamps.sort()
        buckets = {_bucket_5m(ts) for ts in stamps}
        coverage = min(1.0, len(buckets) / expected_slots) if expected_slots > 0 else 0.0
        gap = 0.0
        for previous, current in zip(stamps, stamps[1:], strict=False):
            gap = max(gap, open_hours_between(previous, current) * 60.0)
        worst_gap_minutes = max(worst_gap_minutes, gap)
        min_coverage = min(min_coverage, coverage)
        last_ts_all = max(last_ts_all, stamps[-1]) if last_ts_all else stamps[-1]
        per_cell[f"{symbol}:{timeframe}"] = {
            "rows": len(stamps),
            "distinct_5m_slots": len(buckets),
            "coverage_ratio": round(coverage, 4),
            "max_open_market_gap_minutes": round(gap, 1),
            "last_ts": stamps[-1].isoformat(),
        }

    last_age_minutes = (
        open_hours_between(last_ts_all, now) * 60.0 if last_ts_all is not None else None
    )
    status = PASS
    reasons: list[str] = []
    if not prices.exists:
        status = FAIL
        reasons.append("価格系列ファイルが存在しない")
    elif not cells:
        status = FAIL
        reasons.append(f"直近{window_hours:.0f}hに価格行が1行もない")
    else:
        if last_age_minutes is not None and last_age_minutes > STALE_FAIL_MINUTES:
            status = FAIL
            reasons.append(f"最終価格が市場時間換算{last_age_minutes:.0f}分前(>45分)")
        elif last_age_minutes is not None and last_age_minutes > STALE_WARN_MINUTES:
            status = _worst(status, WARN)
            reasons.append(f"最終価格が市場時間換算{last_age_minutes:.0f}分前(>15分)")
        if worst_gap_minutes > STALE_FAIL_MINUTES:
            status = _worst(status, WARN)
            reasons.append(f"窓内に最大{worst_gap_minutes:.0f}分の収集ギャップ")
        if min_coverage < 0.5:
            status = _worst(status, FAIL if min_coverage < 0.25 else WARN)
            reasons.append(f"5分スロットのカバレッジ最小{min_coverage:.0%}")
        missing_symbols = symbols - {key.split(":", 1)[0] for key in per_cell}
        if missing_symbols:
            status = _worst(status, WARN)
            reasons.append(f"価格行が無いsymbol: {sorted(missing_symbols)}")
    summary = "、".join(reasons) if reasons else "5分価格取得は期待範囲で継続"
    return _section(
        status,
        summary,
        {
            **prices.evidence,
            "window_open_hours": round(open_hours, 2),
            "expected_5m_slots_per_cell": round(expected_slots, 1),
            "cells": per_cell,
            "min_coverage_ratio": round(min_coverage, 4),
            "worst_open_market_gap_minutes": round(worst_gap_minutes, 1),
            "last_price_age_open_minutes": (
                round(last_age_minutes, 1) if last_age_minutes is not None else None
            ),
        },
    )


def audit_prediction_capture(
    tf_journal: JsonlFile, fusion_journal: JsonlFile, now: datetime, window_hours: float
) -> dict:
    """時間足別予測と融合予測が記録され続けているか。"""
    window_start = now - timedelta(hours=window_hours)
    tf_rows = []
    directional: Counter[str] = Counter()
    per_timeframe: Counter[str] = Counter()
    last_tf_ts: datetime | None = None
    for row in tf_journal.rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts < window_start or ts > now:
            continue
        timeframe = str(row.get("timeframe", ""))
        if not timeframe:
            continue
        tf_rows.append(row)
        per_timeframe[timeframe] += 1
        direction = str(row.get("direction", ""))
        directional[direction] += 1
        last_tf_ts = max(last_tf_ts, ts) if last_tf_ts else ts

    fusion_ts: list[datetime] = []
    pit_rows = 0
    for row in fusion_journal.rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts < window_start or ts > now:
            continue
        fusion_ts.append(ts)
        if row.get("pit_eligible") is True:
            pit_rows += 1
    fusion_ts.sort()
    fusion_gaps = [
        (current - previous).total_seconds() / 60.0
        for previous, current in zip(fusion_ts, fusion_ts[1:], strict=False)
        if (current - previous).total_seconds() > 60  # 同一実行の複数symbol行を除く
    ]
    median_fusion_gap = sorted(fusion_gaps)[len(fusion_gaps) // 2] if fusion_gaps else None

    last_age_minutes = open_hours_between(last_tf_ts, now) * 60.0 if last_tf_ts else None
    status = PASS
    reasons: list[str] = []
    if not tf_journal.exists or not tf_rows:
        status = FAIL
        reasons.append(f"直近{window_hours:.0f}hに時間足別予測行がない")
    else:
        if last_age_minutes is not None and last_age_minutes > STALE_FAIL_MINUTES:
            status = FAIL
            reasons.append(f"最終予測が市場時間換算{last_age_minutes:.0f}分前(>45分)")
        elif last_age_minutes is not None and last_age_minutes > STALE_WARN_MINUTES:
            status = WARN
            reasons.append(f"最終予測が市場時間換算{last_age_minutes:.0f}分前(>15分)")
        if set(per_timeframe) != set(PRIMARY_HORIZON_HOURS):
            status = _worst(status, WARN)
            reasons.append(f"揃っていない時間足: 記録={sorted(per_timeframe)}")
    if not fusion_ts:
        status = _worst(status, WARN)
        reasons.append(f"直近{window_hours:.0f}hに融合予測行がない")
    summary = "、".join(reasons) if reasons else "時間足別・融合の予測記録は継続"
    return _section(
        status,
        summary,
        {
            "tf_journal": tf_journal.evidence,
            "fusion_journal": fusion_journal.evidence,
            "tf_rows_in_window": len(tf_rows),
            "tf_rows_per_timeframe": dict(per_timeframe),
            "tf_direction_counts": dict(directional),
            "tf_last_ts": last_tf_ts.isoformat() if last_tf_ts else None,
            "tf_last_age_open_minutes": (
                round(last_age_minutes, 1) if last_age_minutes is not None else None
            ),
            "fusion_rows_in_window": len(fusion_ts),
            "fusion_pit_rows_in_window": pit_rows,
            "fusion_median_run_gap_minutes": (
                round(median_fusion_gap, 1) if median_fusion_gap is not None else None
            ),
        },
    )


@dataclass
class MaturationResult:
    matured_scored: int = 0
    matured_unresolved: int = 0
    immature: int = 0
    outcomes: Counter = field(default_factory=Counter)
    per_cell_scored: Counter = field(default_factory=Counter)
    unresolved_reasons: Counter = field(default_factory=Counter)


def score_timeframe_predictions(
    tf_journal: JsonlFile,
    prices: JsonlFile,
    now: datetime,
) -> MaturationResult:
    """時間足別の方向予測を主ホライズンで再採点する(learning.evaluate_historyの複製)。

    ここでの再計算は監査用で、学習本体はfx_intel側の実装が行う。乖離チェックの
    ために独立実装しておく。
    """
    result = MaturationResult()
    series: dict[tuple[str, str], list[tuple[datetime, float]]] = defaultdict(list)
    for source in (tf_journal, prices):
        for row in source.rows:
            ts = _parse_ts(row.get("ts"))
            close = row.get("close")
            timeframe = str(row.get("timeframe", ""))
            if ts is None or not isinstance(close, (int, float)) or not timeframe:
                continue
            series[(str(row.get("symbol", "")), timeframe)].append((ts, float(close)))
    for points in series.values():
        points.sort(key=lambda point: point[0])

    for row in tf_journal.rows:
        direction = row.get("direction")
        if direction not in ("long", "short"):
            continue
        ts = _parse_ts(row.get("ts"))
        close = row.get("close")
        timeframe = str(row.get("timeframe", ""))
        if ts is None or not isinstance(close, (int, float)) or not timeframe:
            continue
        horizon = PRIMARY_HORIZON_HOURS.get(timeframe, 24.0)
        tol = tolerance_for(horizon)
        age = open_hours_between(ts, now)
        if age < horizon + tol:
            result.immature += 1
            continue
        cell = (str(row.get("symbol", "")), timeframe)
        window_lower = ts + timedelta(hours=horizon - tol)
        window_upper = ts + timedelta(hours=horizon + tol + WEEKEND_CLOSURE_HOURS)
        best: tuple[float, float] | None = None
        for point_ts, point_close in series.get(cell, []):
            if point_ts < window_lower:
                continue
            if point_ts > window_upper:
                break
            point_age = open_hours_between(ts, point_ts)
            if not (horizon - tol <= point_age <= horizon + tol):
                continue
            gap = abs(point_age - horizon)
            if best is None or gap < best[0]:
                best = (gap, point_close)
        if best is None:
            result.matured_unresolved += 1
            result.unresolved_reasons["no_future_price_in_tolerance"] += 1
            continue
        move = best[1] - float(close)
        signed = move if direction == "long" else -move
        atr = row.get("atr")
        threshold = (
            ATR_FLAT_FRACTION * float(atr) if isinstance(atr, (int, float)) and atr > 0 else 0.0
        )
        if signed > threshold:
            outcome = "hit"
        elif signed < -threshold:
            outcome = "miss"
        else:
            outcome = "flat"
        result.matured_scored += 1
        result.outcomes[outcome] += 1
        result.per_cell_scored[f"{cell[0]}:{cell[1]}"] += 1
    return result


def audit_outcome_maturation(maturation: MaturationResult) -> dict:
    """満期を迎えた予測が実際に採点可能か。"""
    matured = maturation.matured_scored + maturation.matured_unresolved
    resolved_ratio = maturation.matured_scored / matured if matured else None
    status = PASS
    reasons: list[str] = []
    if matured == 0:
        status = WARN
        reasons.append("満期予測がまだ無い(蓄積待ち)")
    else:
        assert resolved_ratio is not None
        if resolved_ratio < 0.5:
            status = FAIL
            reasons.append(f"満期予測の採点成功率{resolved_ratio:.0%}(<50%)")
        elif resolved_ratio < 0.9:
            status = WARN
            reasons.append(f"満期予測の採点成功率{resolved_ratio:.0%}(<90%)")
    summary = (
        "、".join(reasons)
        if reasons
        else (f"満期{matured}件中{maturation.matured_scored}件を採点済み")
    )
    return _section(
        status,
        summary,
        {
            "matured_scored": maturation.matured_scored,
            "matured_unresolved": maturation.matured_unresolved,
            "immature": maturation.immature,
            "resolved_ratio": round(resolved_ratio, 4) if resolved_ratio is not None else None,
            "outcome_counts": dict(maturation.outcomes),
            "unresolved_reasons": dict(maturation.unresolved_reasons),
            "scored_per_cell": dict(maturation.per_cell_scored),
        },
    )


def audit_trade_outcome(
    decision_outcomes: dict | None,
    decision_outcomes_mtime: datetime | None,
    monitor: dict | None,
    now: datetime,
) -> dict:
    """MFE/MAE/TP/SL先着の期待値監査が生成されているか。"""
    status = PASS
    reasons: list[str] = []
    overall: dict = {}
    if decision_outcomes is None:
        status = FAIL
        reasons.append("briefing_decision_outcomes.json が読めない")
    else:
        summary = decision_outcomes.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("overall"), dict):
            overall = summary["overall"]
        evaluated = int(overall.get("evaluated", 0) or 0)
        tradable = int(overall.get("tradable", 0) or 0)
        age_hours = (
            (now - decision_outcomes_mtime).total_seconds() / 3600.0
            if decision_outcomes_mtime
            else None
        )
        if age_hours is not None and age_hours > 6.0:
            status = _worst(status, WARN)
            reasons.append(f"期待値レポートが{age_hours:.1f}h更新されていない")
        if evaluated == 0:
            status = _worst(status, WARN)
            reasons.append("採点済みトレード仮説がまだ0件(蓄積待ち)")
        elif tradable > 0 and overall.get("avg_mfe_r") is None:
            status = _worst(status, WARN)
            reasons.append("tradableありだがMFE/MAEが算出されていない")
    monitor_note = None
    if monitor is not None:
        monitor_note = {
            "generated_at": monitor.get("generated_at"),
            "outcome_count": monitor.get("outcome_count"),
            "pit_eligible_journal_rows": monitor.get("pit_eligible_journal_rows"),
            "pit_ineligible_journal_rows": monitor.get("pit_ineligible_journal_rows"),
        }
    summary_ja = "、".join(reasons) if reasons else "MFE/MAE/TP/SL先着の期待値監査が生成されている"
    return _section(
        status,
        summary_ja,
        {
            "decision_outcomes_overall": {
                key: overall.get(key)
                for key in (
                    "evaluated",
                    "tradable",
                    "wins",
                    "losses",
                    "win_rate",
                    "expectancy_r",
                    "avg_mfe_r",
                    "avg_mae_r",
                    "tp1_rate",
                    "tp2_rate",
                    "sl_rate",
                    "sample_ok",
                )
            },
            "decision_outcomes_mtime": (
                decision_outcomes_mtime.isoformat() if decision_outcomes_mtime else None
            ),
            "trade_outcome_monitor": monitor_note,
        },
    )


def audit_learning_update(
    tf_learning: dict | None,
    tf_learning_mtime: datetime | None,
    fusion_learning: dict | None,
    now: datetime,
) -> dict:
    """symbol×timeframe学習が更新され、重み・確信度・条件補正が導かれているか。"""
    status = PASS
    reasons: list[str] = []
    cells_evaluated = 0
    cells_with_adjusted_weights = 0
    cells_with_conviction_damping = 0
    cells_with_condition_factors = 0
    cell_samples: dict[str, int] = {}
    if tf_learning is None:
        status = FAIL
        reasons.append("briefing_tf_learning.json が読めない")
    else:
        profiles = tf_learning.get("profiles")
        if isinstance(profiles, dict):
            # キーは "SYMBOL|timeframe" のフラット辞書(tf_learning.save形式)
            for cell_key, profile in profiles.items():
                if not isinstance(profile, dict):
                    continue
                evaluated = int(profile.get("evaluated", 0) or 0)
                cell_samples[str(cell_key)] = evaluated
                if evaluated > 0:
                    cells_evaluated += 1
                tech_weight = profile.get("tech_weight")
                if isinstance(tech_weight, (int, float)) and (
                    abs(tech_weight - DEFAULT_TECH_WEIGHT) > 1e-9
                ):
                    cells_with_adjusted_weights += 1
                factors = profile.get("symbol_factors")
                if isinstance(factors, dict) and any(
                    isinstance(value, (int, float)) and value < 1.0 for value in factors.values()
                ):
                    cells_with_conviction_damping += 1
                conditions = profile.get("condition_factors")
                if isinstance(conditions, dict) and conditions:
                    cells_with_condition_factors += 1
        age_hours = (
            (now - tf_learning_mtime).total_seconds() / 3600.0 if tf_learning_mtime else None
        )
        if age_hours is not None and age_hours > 6.0:
            status = _worst(status, FAIL if age_hours > 24.0 else WARN)
            reasons.append(f"時間足別学習ファイルが{age_hours:.1f}h更新されていない")
        if cells_evaluated == 0:
            status = _worst(status, WARN)
            reasons.append("採点済みサンプルを持つ学習セルがまだ無い")
    fusion_note = None
    if isinstance(fusion_learning, dict):
        fusion_note = {
            "generated_at": fusion_learning.get("generated_at"),
            "evaluated": fusion_learning.get("evaluated"),
            "tech_weight": fusion_learning.get("tech_weight"),
            "news_weight": fusion_learning.get("news_weight"),
        }
    summary = (
        "、".join(reasons)
        if reasons
        else (
            f"学習セル{cells_evaluated}件が採点済み"
            f"(重み調整{cells_with_adjusted_weights}/確信度減衰{cells_with_conviction_damping}"
            f"/条件補正{cells_with_condition_factors})"
        )
    )
    return _section(
        status,
        summary,
        {
            "tf_learning_mtime": tf_learning_mtime.isoformat() if tf_learning_mtime else None,
            "cells_with_scored_samples": cells_evaluated,
            "cells_with_adjusted_weights": cells_with_adjusted_weights,
            "cells_with_conviction_damping": cells_with_conviction_damping,
            "cells_with_condition_factors": cells_with_condition_factors,
            "cell_scored_samples": cell_samples,
            "fusion_learning": fusion_note,
        },
    )


def audit_decision_application(
    tf_journal: JsonlFile,
    learning_section: dict,
    now: datetime,
    window_hours: float,
) -> dict:
    """学習・期待値・承認済みTP/SLが直近の判断に実際に注入されているか。

    ジャーナル行のcomponents(実効重み)・net_expected_r・target_policyを証拠にする。
    学習セルがまだ無い段階では「未適用」はwarn(蓄積待ち)であってfailではない。
    """
    window_start = now - timedelta(hours=window_hours)
    rows_in_window = 0
    rows_with_nondefault_weight = 0
    rows_with_expectancy = 0
    rows_with_target_policy = 0
    last_nondefault: datetime | None = None
    for row in tf_journal.rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts < window_start or ts > now:
            continue
        rows_in_window += 1
        components = row.get("components")
        if isinstance(components, list):
            for component in components:
                if (
                    isinstance(component, dict)
                    and component.get("key") == "tech"
                    and isinstance(component.get("weight"), (int, float))
                    and abs(float(component["weight"]) - DEFAULT_TECH_WEIGHT) > 1e-6
                ):
                    rows_with_nondefault_weight += 1
                    last_nondefault = max(last_nondefault, ts) if last_nondefault else ts
        if row.get("net_expected_r") is not None:
            rows_with_expectancy += 1
        target_policy = row.get("target_policy")
        if isinstance(target_policy, dict) and target_policy:
            rows_with_target_policy += 1

    learned_cells = int(learning_section["evidence"].get("cells_with_adjusted_weights", 0))
    status = PASS
    reasons: list[str] = []
    if rows_in_window == 0:
        status = FAIL
        reasons.append("窓内に判断行が無く適用証拠を確認できない")
    elif learned_cells > 0 and rows_with_nondefault_weight == 0:
        status = WARN
        reasons.append("学習済み重みがあるのに直近判断へ非既定重みが1件も現れない")
    elif learned_cells == 0 and rows_with_nondefault_weight == 0:
        status = WARN
        reasons.append("学習セル不足のため既定重みで運転中(蓄積待ち。反映経路は未実証)")
    summary = (
        "、".join(reasons)
        if reasons
        else f"非既定重み{rows_with_nondefault_weight}行/期待値注入{rows_with_expectancy}行を確認"
    )
    return _section(
        status,
        summary,
        {
            "rows_in_window": rows_in_window,
            "rows_with_nondefault_tech_weight": rows_with_nondefault_weight,
            "rows_with_net_expected_r": rows_with_expectancy,
            "rows_with_target_policy": rows_with_target_policy,
            "last_nondefault_weight_ts": last_nondefault.isoformat() if last_nondefault else None,
            "learned_weight_cells": learned_cells,
        },
    )


def audit_duplicates(
    tf_journal: JsonlFile,
    prices: JsonlFile,
    err_log_hits: dict[str, int],
    now: datetime,
    window_hours: float,
) -> dict:
    """同一(ts,symbol,timeframe)の重複と重複writer証跡。"""
    window_start = now - timedelta(hours=window_hours)

    def duplicate_stats(source: JsonlFile, key_fields: tuple[str, ...]) -> tuple[int, int]:
        seen: Counter = Counter()
        rows = 0
        for row in source.rows:
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts < window_start or ts > now:
                continue
            rows += 1
            seen[tuple(str(row.get(field_, "")) for field_ in key_fields)] += 1
        duplicates = sum(count - 1 for count in seen.values() if count > 1)
        return rows, duplicates

    journal_rows, journal_dups = duplicate_stats(tf_journal, ("ts", "symbol", "timeframe"))
    price_rows, price_dups = duplicate_stats(prices, ("ts", "symbol", "timeframe"))
    duplicate_writer_hits = err_log_hits.get(DUPLICATE_WRITER_PATTERN, 0)

    journal_rate = journal_dups / journal_rows if journal_rows else 0.0
    price_rate = price_dups / price_rows if price_rows else 0.0
    status = PASS
    reasons: list[str] = []
    if journal_rate > 0.01 or price_rate > 0.01:
        status = FAIL
        reasons.append(f"重複率がジャーナル{journal_rate:.1%}/価格{price_rate:.1%}(>1%)")
    elif journal_dups or price_dups:
        status = WARN
        reasons.append(f"少数の重複行(ジャーナル{journal_dups}件/価格{price_dups}件)")
    if duplicate_writer_hits:
        status = _worst(status, WARN)
        reasons.append(f"errログにduplicate writer痕跡{duplicate_writer_hits}件(競合ガード発火)")
    summary = "、".join(reasons) if reasons else "重複writer・重複行は検出されない"
    return _section(
        status,
        summary,
        {
            "journal_rows_in_window": journal_rows,
            "journal_duplicate_rows": journal_dups,
            "price_rows_in_window": price_rows,
            "price_duplicate_rows": price_dups,
            "err_log_duplicate_writer_hits": duplicate_writer_hits,
        },
    )


def audit_freshness(report: dict | None, now: datetime) -> dict:
    """freshness monitorの最新レポートを転記する(自前判定はしない)。"""
    if report is None:
        return _section(FAIL, "freshness_report.json が読めない", {})
    monitored = _parse_ts(report.get("monitor_timestamp"))
    age_minutes = (now - monitored).total_seconds() / 60.0 if monitored else None
    targets = report.get("targets")
    statuses: dict[str, str] = {}
    worst = PASS
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, dict):
                continue
            name = str(target.get("name", "?"))
            raw = str(target.get("status", "unknown"))
            statuses[name] = raw
            mapped = {"ok": PASS, "warning": WARN, "critical": FAIL}.get(raw, UNKNOWN)
            worst = _worst(worst, mapped)
    if age_minutes is not None and age_minutes > 30.0:
        worst = _worst(worst, WARN)
    summary = (
        f"鮮度レポート{age_minutes:.0f}分前・対象status={statuses}"
        if age_minutes is not None
        else "鮮度レポートの時刻が読めない"
    )
    return _section(
        worst,
        summary,
        {
            "monitor_timestamp": report.get("monitor_timestamp"),
            "report_age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
            "target_statuses": statuses,
        },
    )


def audit_scanner_429(err_log_hits: dict[str, int], tail_hits: dict[str, int]) -> dict:
    """TradingViewスキャナーの429/JSONDecodeError/timeoutが制御下にあるか。

    errログは行に時刻が無いため、全期間カウントと末尾200行カウントを分けて出す。
    """
    total_429 = err_log_hits.get("scanner_errors", 0)
    recent_429 = tail_hits.get("scanner_errors", 0)
    recent_timeout = tail_hits.get("timeouts", 0)
    status = PASS
    reasons: list[str] = []
    if recent_429 > 5:
        status = FAIL
        reasons.append(f"末尾200行に429/decode失敗{recent_429}件(再発中)")
    elif recent_429 > 0:
        status = WARN
        reasons.append(f"末尾200行に429/decode失敗{recent_429}件")
    if recent_timeout > 5:
        status = _worst(status, WARN)
        reasons.append(f"末尾200行にtimeout {recent_timeout}件")
    summary = "、".join(reasons) if reasons else "429/JSONDecodeErrorの再発なし"
    return _section(
        status,
        summary,
        {
            "all_time_scanner_error_lines": total_429,
            "recent_scanner_error_lines_tail200": recent_429,
            "recent_timeout_lines_tail200": recent_timeout,
        },
    )


def audit_sample_sufficiency(
    maturation: MaturationResult, fusion_journal: JsonlFile, now: datetime
) -> dict:
    """学習ガード(20/8/12件)とML(150件)に対するサンプル充足度。"""
    cells = dict(maturation.per_cell_scored)
    cells_ge_weight = sum(1 for count in cells.values() if count >= MIN_WEIGHT_SAMPLES)
    cells_ge_symbol = sum(1 for count in cells.values() if count >= MIN_SYMBOL_SAMPLES)
    pit_directional = 0
    for row in fusion_journal.rows:
        if row.get("pit_eligible") is True and row.get("direction") in ("long", "short"):
            pit_directional += 1
    status = PASS
    reasons: list[str] = []
    if not cells:
        status = WARN
        reasons.append("採点済みセルがまだ無い")
    elif cells_ge_symbol == 0:
        status = WARN
        reasons.append(f"ペア別減衰の下限{MIN_SYMBOL_SAMPLES}件に達したセルが無い")
    summary = (
        "、".join(reasons)
        if reasons
        else f"重み学習可能セル{cells_ge_weight}件/減衰可能セル{cells_ge_symbol}件"
    )
    return _section(
        status,
        summary,
        {
            "scored_samples_per_cell": cells,
            "cells_at_or_above_weight_min_20": cells_ge_weight,
            "cells_at_or_above_symbol_min_8": cells_ge_symbol,
            "condition_cell_min": MIN_CONDITION_SAMPLES,
            "fusion_pit_directional_rows_total": pit_directional,
            "ml_min_train_rows": ML_MIN_TRAIN_ROWS,
        },
    )


def audit_blocking_reasons(
    decision_feedback: dict | None,
    tf_journal: JsonlFile,
    now: datetime,
    window_hours: float,
) -> dict:
    """負期待値ブロック・減衰などのガード発動状況(情報提供)。"""
    window_start = now - timedelta(hours=window_hours)
    negative_expectancy_rows = 0
    for row in tf_journal.rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts < window_start or ts > now:
            continue
        net = row.get("net_expected_r")
        if isinstance(net, (int, float)) and net < 0:
            negative_expectancy_rows += 1
    feedback_summary: dict[str, object] = {}
    if isinstance(decision_feedback, dict):
        feedback_summary["generated_at"] = decision_feedback.get("generated_at")
        cells = decision_feedback.get("cells")
        if isinstance(cells, dict):
            dampened = 0
            blocked = 0
            for cell in cells.values():
                if not isinstance(cell, dict):
                    continue
                factor = cell.get("factor")
                if isinstance(factor, (int, float)) and factor < 1.0:
                    dampened += 1
                if cell.get("block") is True or (
                    isinstance(factor, (int, float)) and factor <= 0.0
                ):
                    blocked += 1
            feedback_summary["cells_total"] = len(cells)
            feedback_summary["cells_dampened"] = dampened
            feedback_summary["cells_blocked"] = blocked
    summary = f"負期待値の判断行{negative_expectancy_rows}件(窓内)" + (
        f"、feedback={feedback_summary.get('action_counts')}"
        if feedback_summary.get("action_counts")
        else ""
    )
    return _section(
        PASS,
        summary,
        {
            "negative_net_expected_r_rows_in_window": negative_expectancy_rows,
            "decision_feedback": feedback_summary or None,
        },
    )


def audit_launchd(launchctl_output: dict[str, str]) -> dict:
    """launchd 3サービスの登録・最終exit code(読み取りのみ)。"""
    services: dict[str, dict] = {}
    status = PASS
    reasons: list[str] = []
    for label in LAUNCHD_LABELS:
        output = launchctl_output.get(label, "")
        loaded = bool(output.strip())
        last_exit: int | None = None
        runs: int | None = None
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if line.startswith("last exit code =") and last_exit is None:
                try:
                    last_exit = int(line.partition("=")[2].strip())
                except ValueError:
                    last_exit = None
            elif line.startswith("runs =") and runs is None:
                try:
                    runs = int(line.partition("=")[2].strip())
                except ValueError:
                    runs = None
        services[label] = {"loaded": loaded, "last_exit_code": last_exit, "runs": runs}
        if not loaded:
            status = FAIL
            reasons.append(f"{label} が未登録")
        elif last_exit not in (0, None):
            status = _worst(status, WARN)
            reasons.append(f"{label} の最終exit={last_exit}")
    if not launchctl_output:
        return _section(UNKNOWN, "launchctlを実行できない環境(--no-launchd等)", {"services": {}})
    summary = "、".join(reasons) if reasons else "3サービス登録済み・最終exit 0"
    return _section(status, summary, {"services": services})


def collect_launchctl() -> dict[str, str]:
    outputs: dict[str, str] = {}
    for label in LAUNCHD_LABELS:
        try:
            outputs[label] = subprocess.check_output(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            outputs[label] = ""
    return outputs


def scan_error_logs(paths: list[Path]) -> tuple[dict[str, int], dict[str, int]]:
    """errログ全体と末尾200行のパターン件数。(時刻情報が無いための近似)"""
    totals = {"scanner_errors": 0, "timeouts": 0, DUPLICATE_WRITER_PATTERN: 0}
    tails = {"scanner_errors": 0, "timeouts": 0, DUPLICATE_WRITER_PATTERN: 0}
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for scope, subset in (("all", lines), ("tail", lines[-200:])):
            bucket = totals if scope == "all" else tails
            for line in subset:
                if any(pattern in line for pattern in SCANNER_ERROR_PATTERNS):
                    bucket["scanner_errors"] += 1
                if any(pattern in line for pattern in TIMEOUT_PATTERNS):
                    bucket["timeouts"] += 1
                if DUPLICATE_WRITER_PATTERN in line:
                    bucket[DUPLICATE_WRITER_PATTERN] += 1
    return totals, tails


def aggregate_overall(sections: dict[str, dict]) -> str:
    """総合判定: どこか1つでもfailなら全体fail、warn/unknownがあればwarn。

    「蓄積待ち」はセクション側でwarnにする(失敗ではない)ため、failは
    ループ段の実際の破損だけを意味する。
    """
    worst = PASS
    for section in sections.values():
        status = section.get("status", UNKNOWN)
        worst = _worst(worst, WARN if status == UNKNOWN else status)
    return worst


def run_audit(
    log_dir: Path,
    window_hours: float,
    now: datetime | None = None,
    launchctl_output: dict[str, str] | None = None,
    err_log_paths: list[Path] | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    prices = read_jsonl(log_dir / "briefing_tf_prices.jsonl")
    tf_journal = read_jsonl(log_dir / "briefing_tf_journal.jsonl")
    fusion_journal = read_jsonl(log_dir / "briefing_journal.jsonl")
    tf_learning_path = log_dir / "briefing_tf_learning.json"
    fusion_learning_path = log_dir / "briefing_learning.json"
    outcomes_path = log_dir / "briefing_decision_outcomes.json"
    feedback_path = log_dir / "briefing_decision_feedback.json"
    monitor_path = log_dir / "trade_outcome_monitor.json"
    if err_log_paths is None:
        err_log_paths = [
            log_dir / "launchd" / "snapshot.err.log",
            log_dir / "launchd" / "briefing.err.log",
            log_dir / "fx_integrated_briefing.log",
            log_dir / "fx_fusion_capture.log",
        ]
    err_totals, err_tails = scan_error_logs(err_log_paths)

    symbols = {
        str(row.get("symbol", "")) for row in tf_journal.rows if isinstance(row.get("symbol"), str)
    }
    maturation = score_timeframe_predictions(tf_journal, prices, now)

    sections: dict[str, dict] = {}
    sections["data_collection"] = audit_data_collection(prices, now, window_hours, symbols)
    sections["prediction_capture"] = audit_prediction_capture(
        tf_journal, fusion_journal, now, window_hours
    )
    sections["outcome_maturation"] = audit_outcome_maturation(maturation)
    sections["trade_outcome"] = audit_trade_outcome(
        read_json(outcomes_path), _mtime(outcomes_path), read_json(monitor_path), now
    )
    sections["learning_update"] = audit_learning_update(
        read_json(tf_learning_path), _mtime(tf_learning_path), read_json(fusion_learning_path), now
    )
    sections["decision_application"] = audit_decision_application(
        tf_journal, sections["learning_update"], now, window_hours
    )
    sections["duplicate_detection"] = audit_duplicates(
        tf_journal, prices, err_totals, now, window_hours
    )
    sections["freshness"] = audit_freshness(read_json(log_dir / "freshness_report.json"), now)
    sections["scanner_429"] = audit_scanner_429(
        {"scanner_errors": err_totals["scanner_errors"]},
        {"scanner_errors": err_tails["scanner_errors"], "timeouts": err_tails["timeouts"]},
    )
    sections["sample_sufficiency"] = audit_sample_sufficiency(maturation, fusion_journal, now)
    sections["blocking_reasons"] = audit_blocking_reasons(
        read_json(feedback_path), tf_journal, now, window_hours
    )
    if launchctl_output is not None:
        sections["launchd"] = audit_launchd(launchctl_output)

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "log_dir": str(log_dir),
        "window_hours": window_hours,
        "overall_status": aggregate_overall(sections),
        "sections": sections,
    }


def to_markdown(report: dict) -> str:
    lines = [
        "# E2E学習ループ監査レポート",
        "",
        f"- 生成時刻: {report['generated_at']}",
        f"- 対象: `{report['log_dir']}` / 窓 {report['window_hours']:.0f}h",
        f"- **総合判定: {report['overall_status'].upper()}**",
        "",
        "| セクション | 判定 | 概要 |",
        "|---|---|---|",
    ]
    for name, section in report["sections"].items():
        lines.append(f"| {name} | {section['status']} | {section['summary_ja']} |")
    lines.append("")
    lines.append("## 証拠(セクション別)")
    for name, section in report["sections"].items():
        lines.append("")
        lines.append(f"### {name}")
        lines.append("```json")
        lines.append(json.dumps(section["evidence"], ensure_ascii=False, indent=2, default=str))
        lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument("--window-hours", type=float, default=72.0)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    parser.add_argument(
        "--no-launchd",
        action="store_true",
        help="launchctlを実行しない(他OS・CI・リモート集計用)",
    )
    parser.add_argument(
        "--now",
        type=str,
        default=None,
        help="監査基準時刻(ISO-8601, テスト用)。省略時は現在UTC",
    )
    args = parser.parse_args(argv)

    if args.window_hours <= 0:
        print("--window-hours は正の値が必要です", file=sys.stderr)
        return 3
    now = _parse_ts(args.now) if args.now else datetime.now(UTC)
    if args.now and now is None:
        print("--now はtimezone付きISO-8601が必要です", file=sys.stderr)
        return 3

    launchctl_output = None if args.no_launchd else collect_launchctl()
    report = run_audit(
        log_dir=args.log_dir,
        window_hours=args.window_hours,
        now=now,
        launchctl_output=launchctl_output,
    )

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    if args.markdown_out is not None:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(to_markdown(report), encoding="utf-8")

    print(f"overall_status: {report['overall_status']}")
    for name, section in report["sections"].items():
        print(f"  [{section['status']:4s}] {name}: {section['summary_ja']}")
    return {PASS: 0, WARN: 1, FAIL: 2}.get(report["overall_status"], 1)


if __name__ == "__main__":
    sys.exit(main())
