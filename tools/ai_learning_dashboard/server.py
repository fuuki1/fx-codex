#!/usr/bin/env python3
"""Read-only web dashboard for fx_intel learning state.

This tool intentionally lives outside fx_intel/trader system code. It serves a
small static UI and exposes a read-only JSON summary of logs/*.json/jsonl.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left
from collections.abc import Mapping
import json
import math
import mimetypes
import os
import subprocess
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
REPO_ROOT = APP_DIR.parents[1]
DEFAULT_LOG_DIR = REPO_ROOT / "logs"

JOURNAL_FILE = "briefing_journal.jsonl"
LEARNING_FILE = "briefing_learning.json"
ML_FILE = "ml_model.json"
HISTORICAL_CHART_FILE = "historical_chart_training.json"
PROMOTION_FILE = "promotion_state.json"
TRADE_MONITOR_FILE = "trade_outcome_monitor.json"
TRADE_REGISTRY_FILE = "trade_improvement_candidates.json"
DECISION_LOG_FILE = "briefing_decisions.jsonl"
DECISION_LATEST_FILE = "briefing_decisions_latest.json"
DECISION_OUTCOMES_FILE = "briefing_decision_outcomes.json"
DECISION_FEEDBACK_FILE = "briefing_decision_feedback.json"
DECISION_MONITOR_FILE = "decision_expectancy_monitor.json"
# 時間足別モード(fx_briefing --per-timeframe)の記録
TF_JOURNAL_FILE = "briefing_tf_journal.jsonl"
TF_LEARNING_FILE = "briefing_tf_learning.json"
# 5分ごとの価格スナップショット(fx_tf_snapshot.py)。短い足の採点窓に入る
# 将来価格を密に供給する価格専用系列。採点の将来価格解決に使う(判断は無い)。
TF_PRICES_FILE = "briefing_tf_prices.jsonl"
HORIZON_JOURNAL_FILE = "briefing_horizon_forecasts.jsonl"
HORIZON_LEARNING_FILE = "briefing_horizon_learning.json"
TF_PRICES_STALE_MINUTES = 15
_TIMEFRAME_ORDER = {"15m": 0, "1h": 1, "4h": 2, "1d": 3}
_HORIZON_ORDER = {
    label: index
    for index, label in enumerate(("5m", "15m", "30m", "1h", "3h", "6h", "12h", "24h", "3d"))
}
LAUNCHD_SERVICES = (
    ("snapshot_service", "価格スナップショット定期サービス", "com.fx-codex.snapshot"),
    ("briefing_service", "ブリーフィング定期サービス", "com.fx-codex.briefing"),
    ("health_service", "鮮度監視定期サービス", "com.fx-codex.health"),
    ("horizon_service", "9ホライズン定期サービス", "com.fx-codex.horizon"),
    ("monitor_service", "期待値監視定期サービス", "com.fx-codex.monitors"),
)

# Keep these dashboard-only mirrors aligned with fx_intel.ml.  The dashboard is
# intentionally standalone, but it still needs to explain why a model has not
# been created without importing the research pipeline.
ML_MIN_TRAIN_ROWS = 150
ML_THIN_MIN_GAP_HOURS = 4.0
ML_ARTIFACT_SCHEMA = 4
ML_TRAINING_CONTRACT = "fusion-pit-v1"

# 週末クローズ(金曜21:00 UTC → 日曜22:00 UTC)。fx_intel.market と同じ近似。
# ダッシュボードは fx_intel に依存しない方針なのでここに独立して持つ。
_CLOSE_WEEKDAY = 4  # 金曜
_CLOSE_HOUR_UTC = 21
_WEEKEND_CLOSURE = timedelta(hours=49)


def _closure_start_on_or_before(moment: datetime) -> datetime:
    anchor = moment.replace(hour=_CLOSE_HOUR_UTC, minute=0, second=0, microsecond=0)
    anchor -= timedelta(days=(moment.weekday() - _CLOSE_WEEKDAY) % 7)
    if anchor > moment:
        anchor -= timedelta(days=7)
    return anchor


def _open_hours_between(start: datetime, end: datetime) -> float:
    """start→end の経過から週末クローズ分を除いた市場オープン時間(時間単位)。

    fx_intel.market.open_hours_between と同じロジック。採点の将来価格を
    fx_intel 本体と同じ「市場オープン時間換算」で選ぶために使う(壁時計時間で
    採点すると週末跨ぎで本体の学習的中率とズレるため)。
    """
    if end <= start:
        return 0.0
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    closed = timedelta()
    cursor = _closure_start_on_or_before(end_utc)
    while cursor + _WEEKEND_CLOSURE > start_utc:
        overlap = min(cursor + _WEEKEND_CLOSURE, end_utc) - max(cursor, start_utc)
        if overlap > timedelta():
            closed += overlap
        cursor -= timedelta(days=7)
    return (end_utc - start_utc - closed).total_seconds() / 3600.0


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_pit_eligible_fusion_row(entry: dict[str, Any]) -> bool:
    """Mirror fx_intel.journal's fail-closed fusion learning provenance contract."""
    if entry.get("pit_eligible") is not True:
        return False

    def aware(value: object) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)

    recorded = aware(entry.get("ts"))
    prediction = aware(entry.get("prediction_time"))
    source_cutoff = aware(entry.get("source_cutoff"))
    feature_available = aware(entry.get("max_feature_available_time"))
    if any(value is None for value in (recorded, prediction, source_cutoff, feature_available)):
        return False
    assert recorded is not None
    assert prediction is not None
    assert source_cutoff is not None
    assert feature_available is not None
    return recorded == prediction and source_cutoff <= feature_available <= prediction


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_journal(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _file_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "size": 0, "mtime": None, "path": str(path)}
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        "path": str(path),
    }


def _file_status_with_age(path: Path, now: datetime) -> dict[str, Any]:
    status = _file_status(path)
    mtime = _parse_ts(status.get("mtime"))
    status["age_minutes"] = (
        round((now - mtime).total_seconds() / 60.0, 1) if mtime is not None else None
    )
    return status


def _process_table(ps_output: str | None = None) -> list[dict[str, Any]]:
    if ps_output is None:
        try:
            ps_output = subprocess.check_output(
                ["ps", "-axo", "pid=,command="],
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            ps_output = ""
    rows: list[dict[str, Any]] = []
    for line in ps_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        rows.append({"pid": pid, "command": command.strip()})
    return rows


def _matching_processes(rows: list[dict[str, Any]], needle: str) -> list[dict[str, Any]]:
    return [
        {"pid": row["pid"], "command": row["command"]}
        for row in rows
        if needle in str(row.get("command", ""))
        and " rg " not in str(row.get("command", ""))
        and "ps -axo" not in str(row.get("command", ""))
    ]


def _runtime_process_status(rows: list[dict[str, Any]], key: str, label: str, needle: str) -> dict:
    matches = _matching_processes(rows, needle)
    return {
        "key": key,
        "label_ja": label,
        "running": bool(matches),
        "pids": [row["pid"] for row in matches],
    }


def _launchctl_print(label: str) -> str:
    try:
        return subprocess.check_output(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def _launchd_service_status(key: str, label_ja: str, label: str, output: str) -> dict:
    loaded = bool(output.strip())
    state: str | None = None
    last_exit_code: int | None = None
    pids: list[int] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("state ="):
            state = line.partition("=")[2].strip()
        elif line.startswith("last exit code ="):
            try:
                last_exit_code = int(line.partition("=")[2].strip())
            except ValueError:
                last_exit_code = None
        elif line.startswith("pid ="):
            try:
                pids.append(int(line.partition("=")[2].strip()))
            except ValueError:
                continue
    return {
        "key": key,
        "label_ja": label_ja,
        "launchd_label": label,
        # UI互換のrunningは、ワンショット子の瞬間的な実行状態ではなく
        # 定期サービスがlaunchdへ登録済みかを示す。
        "running": loaded,
        "loaded": loaded,
        "state": state,
        "last_exit_code": last_exit_code,
        "pids": pids,
    }


def _runtime_log_status(log_dir: Path, name: str, label: str, now: datetime) -> dict:
    status = _file_status_with_age(log_dir / name, now)
    status["name"] = name
    status["label_ja"] = label
    return status


def _ops_status(
    log_dir: Path,
    files: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime | None = None,
    ps_output: str | None = None,
    launchctl_outputs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    rows = _process_table(ps_output)
    processes = []
    for key, label_ja, launchd_label in LAUNCHD_SERVICES:
        output = (
            launchctl_outputs.get(launchd_label, "")
            if launchctl_outputs is not None
            else _launchctl_print(launchd_label)
        )
        processes.append(_launchd_service_status(key, label_ja, launchd_label, output))
    processes.append(
        _runtime_process_status(
            rows, "dashboard", "ダッシュボード", "tools/ai_learning_dashboard/server.py"
        )
    )
    runtime_logs = [
        _runtime_log_status(log_dir, JOURNAL_FILE, "融合1判断ログ", now),
        _runtime_log_status(log_dir, TF_JOURNAL_FILE, "時間足別判断ログ", now),
        _runtime_log_status(log_dir, TF_PRICES_FILE, "価格スナップショットログ", now),
        _runtime_log_status(log_dir, HORIZON_JOURNAL_FILE, "9ホライズンshadowログ", now),
        _runtime_log_status(log_dir, TRADE_MONITOR_FILE, "改善候補監視JSON", now),
        _runtime_log_status(log_dir, DECISION_MONITOR_FILE, "判断期待R監視JSON", now),
    ]
    tf_prices_status = _file_status_with_age(log_dir / TF_PRICES_FILE, now)
    file_exists = {name: bool(info.get("exists")) for name, info in files.items()}
    alerts: list[dict[str, str]] = []

    for name, label_ja, service in (
        (JOURNAL_FILE, "融合1判断ログ", "com.fx-codex.briefing"),
        (TF_JOURNAL_FILE, "時間足別判断ログ", "com.fx-codex.briefing"),
        (TF_PRICES_FILE, "時間足別採点用の5分価格系列", "com.fx-codex.snapshot"),
    ):
        if not file_exists.get(name):
            alerts.append(
                {
                    "severity": "fail",
                    "message_ja": f"{label_ja}が未作成です",
                    "action_ja": f"{service}の状態とOperations runbookの復旧手順を確認してください",
                }
            )
    if file_exists.get(DECISION_LOG_FILE) and not file_exists.get(DECISION_MONITOR_FILE):
        alerts.append(
            {
                "severity": "warn",
                "message_ja": "完全判断ログの期待R監視JSONが未作成です",
                "action_ja": "python3 tools/decision_expectancy_monitor.py を実行するとTP/SL/MFE/MAE期待Rを更新できます",
            }
        )
    if not file_exists.get(LEARNING_FILE) and not file_exists.get(TF_LEARNING_FILE):
        alerts.append(
            {
                "severity": "info",
                "message_ja": "学習プロファイルが未作成です",
                "action_ja": "判断ログが主ホライズン経過後に採点されると作成されます",
            }
        )
    for process in processes[: len(LAUNCHD_SERVICES)]:
        if not process["loaded"]:
            alerts.append(
                {
                    "severity": "fail",
                    "message_ja": f"{process['label_ja']}がlaunchdに登録されていません",
                    "action_ja": "scripts/status_fx_services.sh とOperations runbookの復旧手順を確認してください",
                }
            )
            continue
        exit_code = process.get("last_exit_code")
        if exit_code in (None, 0):
            continue
        if process["key"] == "briefing_service" and exit_code == 5:
            alerts.append(
                {
                    "severity": "warn",
                    "message_ja": "判断保存後のDiscord通知に失敗しました",
                    "action_ja": "logs/fx_integrated_briefing.log とDiscord側の応答を確認してください",
                }
            )
        else:
            alerts.append(
                {
                    "severity": "fail",
                    "message_ja": f"{process['label_ja']}の前回終了コードは{exit_code}です",
                    "action_ja": "launchd stderrと対象journalの整合性を確認してください",
                }
            )

    for runtime_log in runtime_logs:
        age = runtime_log.get("age_minutes")
        if age is None:
            continue
        stale_after = {
            JOURNAL_FILE: 90,
            TF_JOURNAL_FILE: 15,
            TF_PRICES_FILE: 15,
            HORIZON_JOURNAL_FILE: 15,
            TRADE_MONITOR_FILE: 30,
            DECISION_MONITOR_FILE: 30,
        }.get(runtime_log["name"], 30)
        if age > stale_after:
            service = {
                TF_PRICES_FILE: "com.fx-codex.snapshot",
                HORIZON_JOURNAL_FILE: "com.fx-codex.horizon",
                TRADE_MONITOR_FILE: "com.fx-codex.monitors",
                DECISION_MONITOR_FILE: "com.fx-codex.monitors",
            }.get(runtime_log["name"], "com.fx-codex.briefing")
            alerts.append(
                {
                    "severity": "warn",
                    "message_ja": f"{runtime_log['label_ja']}の更新が止まっています",
                    "action_ja": f"最終更新から約{int(age)}分経過。{service}とlaunchdログを確認してください",
                }
            )

    severity_rank = {"ok": 0, "info": 1, "warn": 2, "fail": 3}
    status = "ok"
    for alert in alerts:
        severity = str(alert.get("severity", "info"))
        if severity_rank.get(severity, 1) > severity_rank[status]:
            status = severity

    return {
        "generated_at": now.isoformat(),
        "status": status,
        "processes": processes,
        "runtime_logs": runtime_logs,
        "tf_prices": tf_prices_status,
        "signals": {
            "has_any_journal": file_exists.get(JOURNAL_FILE, False)
            or file_exists.get(TF_JOURNAL_FILE, False),
            "has_timeframe_prices": file_exists.get(TF_PRICES_FILE, False),
            "has_horizon_track": file_exists.get(HORIZON_JOURNAL_FILE, False),
            "has_any_learning": file_exists.get(LEARNING_FILE, False)
            or file_exists.get(TF_LEARNING_FILE, False),
        },
        "alerts": alerts[:12],
    }


def _future_close(
    series: list[tuple[datetime, float]],
    ts: datetime,
    horizon_hours: float = 24.0,
    tolerance_hours: float = 2.0,
) -> float | None:
    """記録時刻から主ホライズン後(市場オープン時間換算)に最も近い終値。

    fx_intel.price_history.future_close_from_series と同じく、経過は
    _open_hours_between で数える(週末クローズを除外)。壁時計の候補窓で
    ざっくり絞ってから、オープン時間換算の age で厳密に判定する。
    """
    if not series:
        return None
    # オープン時間は壁時計を超えないため、候補は壁時計で
    # [下限, 上限 + 週末クローズ1回分] に限られる
    window_lower = ts + timedelta(hours=horizon_hours - tolerance_hours)
    window_upper = ts + timedelta(hours=horizon_hours + tolerance_hours) + _WEEKEND_CLOSURE
    best: tuple[float, float] | None = None
    start_index = bisect_left(series, (window_lower, -math.inf))
    for point_ts, close in series[start_index:]:
        if point_ts > window_upper:
            break
        age = _open_hours_between(ts, point_ts)
        if not (horizon_hours - tolerance_hours <= age <= horizon_hours + tolerance_hours):
            continue
        gap = abs(age - horizon_hours)
        if best is None or gap < best[0]:
            best = (gap, close)
    return best[1] if best is not None else None


# 時間足別ジャーナルの主ホライズン(時間)と採点許容誤差(±時間)。
# fx_intel.timeframe と同じ値。dashboard は fx_intel に依存しない方針なので
# ここに独立して持つ(ズレたら表示だけの問題で、採点の正は fx_intel 側)。
_HORIZON_TOLERANCE = {
    0.25: 0.1,
    0.5: 0.15,
    1.0: 0.25,
    4.0: 1.0,
    8.0: 1.5,
    12.0: 2.0,
    24.0: 2.0,
    48.0: 4.0,
    72.0: 6.0,
}

_ANALYSIS_DIRECTION_THRESHOLD = 0.15
_ANALYSIS_MIN_QUALITY = 0.4


def _tolerance_for(horizon_hours: float) -> float:
    return _HORIZON_TOLERANCE.get(horizon_hours, 2.0)


def _analysis_direction_for_entry(entry: dict[str, Any]) -> str:
    """Return an observational direction without weakening the action veto."""
    explicit = str(entry.get("analysis_direction") or "").strip().lower()
    if explicit in {"long", "short", "neutral"}:
        return explicit
    action = str(entry.get("direction") or "").strip().lower()
    if action in {"long", "short"}:
        return action
    if action != "standby":
        return ""
    quality = _number(entry.get("data_quality"))
    composite = _number(entry.get("composite"))
    if quality is None or quality < _ANALYSIS_MIN_QUALITY or composite is None:
        return ""
    if composite >= _ANALYSIS_DIRECTION_THRESHOLD:
        return "long"
    if composite <= -_ANALYSIS_DIRECTION_THRESHOLD:
        return "short"
    return "neutral"


def _analysis_conviction_for_entry(entry: dict[str, Any]) -> int | None:
    if _analysis_direction_for_entry(entry) not in {"long", "short"}:
        return None
    explicit = _number(entry.get("analysis_conviction"))
    if explicit is not None:
        return max(0, min(100, round(explicit)))
    composite = _number(entry.get("composite"))
    quality = _number(entry.get("data_quality"))
    if composite is not None and quality is not None:
        return max(0, min(100, round(abs(composite) * 100 * quality)))
    conviction = _number(entry.get("conviction"))
    return max(0, min(100, round(conviction))) if conviction is not None else None


def _blocked_gate_for_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    traces = entry.get("gate_trace")
    if not isinstance(traces, list):
        return None
    for raw in traces:
        if not isinstance(raw, dict) or raw.get("status") != "blocked":
            continue
        gate = str(raw.get("gate") or "")
        payload: dict[str, Any] = {"gate": gate}
        for key in (
            "event_currency",
            "event_title",
            "event_impact",
            "event_time",
            "blocked_until",
        ):
            if raw.get(key) not in (None, ""):
                payload[key] = raw[key]
        return payload
    return None


def _evaluate_analysis_hypotheses(
    parsed: list[tuple[datetime, dict[str, Any]]],
    prices: dict[tuple[str, str], list[tuple[datetime, float]]],
) -> tuple[dict[str, Any], dict[tuple[str, str, str], dict[str, Any]]]:
    """Score analysis hypotheses separately from action-direction statistics."""
    directional = evaluated = hits = flat = pending = 0
    by_timeframe: dict[str, dict[str, int]] = {}
    outcomes: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for ts, entry in parsed:
        direction = _analysis_direction_for_entry(entry)
        if direction not in {"long", "short"}:
            continue
        directional += 1
        symbol = str(entry.get("symbol") or "")
        timeframe = str(entry.get("timeframe") or "").strip().lower()
        close = _number(entry.get("close"))
        atr = _number(entry.get("atr"))
        horizon = _number(entry.get("horizon_hours")) or 24.0
        future = None
        if close is not None and symbol:
            future = _future_close(
                prices.get((symbol, timeframe), []),
                ts,
                horizon_hours=horizon,
                tolerance_hours=_tolerance_for(horizon),
            )
        move = None if future is None or close is None else future - close
        if move is None:
            pending += 1
            outcome = "pending"
        else:
            signed = move if direction == "long" else -move
            threshold = (atr or 0.0) * 0.1
            if abs(signed) <= threshold:
                flat += 1
                outcome = "flat"
            else:
                evaluated += 1
                hit = signed > 0
                hits += int(hit)
                outcome = "hit" if hit else "miss"
        if timeframe:
            stat = by_timeframe.setdefault(
                timeframe,
                {"evaluated": 0, "hits": 0, "flat": 0, "pending": 0},
            )
            if outcome in {"hit", "miss"}:
                stat["evaluated"] += 1
                stat["hits"] += int(outcome == "hit")
            elif outcome in {"flat", "pending"}:
                stat[outcome] += 1
        ts_text = ts.isoformat()
        row: dict[str, Any] = {
            "ts": ts_text,
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "conviction": _analysis_conviction_for_entry(entry),
            "outcome": outcome,
            "move": round(move, 6) if move is not None else None,
            "source": "explicit" if entry.get("analysis_direction") else "composite_fallback",
        }
        outcomes.append(row)
        by_key[(ts_text, symbol, timeframe)] = row
    return (
        {
            "directional": directional,
            "evaluated": evaluated,
            "hits": hits,
            "flat": flat,
            "pending": pending,
            "hit_rate": hits / evaluated if evaluated else None,
            "by_timeframe": by_timeframe,
            "recent_outcomes": outcomes[-20:],
        },
        by_key,
    )


def _evaluate_journal(entries: list[dict[str, Any]]) -> dict[str, Any]:
    # 価格系列は (symbol, timeframe) 別に持つ。timeframe を持たない旧スキーマ行は
    # timeframe="" のキー(融合1判断)に入り、従来どおり24h採点される。
    prices: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    pit_prices: dict[tuple[str, str], list[tuple[datetime, float]]] = {}
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for entry in entries:
        ts = _parse_ts(entry.get("ts"))
        close = _number(entry.get("close"))
        symbol = str(entry.get("symbol") or "")
        timeframe = str(entry.get("timeframe") or "").strip().lower()
        if ts is None:
            continue
        parsed.append((ts, entry))
        if close is not None and symbol:
            prices.setdefault((symbol, timeframe), []).append((ts, close))
            if not timeframe and _is_pit_eligible_fusion_row(entry):
                pit_prices.setdefault((symbol, timeframe), []).append((ts, close))
    for series in prices.values():
        series.sort(key=lambda row: row[0])
    for series in pit_prices.values():
        series.sort(key=lambda row: row[0])
    parsed.sort(key=lambda row: row[0])

    analysis_evaluation, analysis_by_key = _evaluate_analysis_hypotheses(parsed, prices)

    evaluated = hits = flat = pending = directional = 0
    ml_pit_evaluated = ml_pit_pending = ml_pit_ineligible = 0
    by_symbol: dict[str, dict[str, int]] = {}
    by_timeframe: dict[str, dict[str, int]] = {}
    outcomes: list[dict[str, Any]] = []
    display_decisions: list[dict[str, Any]] = []
    for ts, entry in parsed:
        direction = str(entry.get("direction") or "").strip().lower()
        symbol = str(entry.get("symbol") or "")
        timeframe = str(entry.get("timeframe") or "").strip().lower()
        pit_eligible = _is_pit_eligible_fusion_row(entry) if not timeframe else False
        ts_text = ts.isoformat()
        display_base = {
            "ts": ts_text,
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "analysis_direction": _analysis_direction_for_entry(entry),
            "analysis_conviction": _analysis_conviction_for_entry(entry),
            "analysis_score": _number(entry.get("composite")),
            "blocked_gate": _blocked_gate_for_entry(entry),
            "pit_eligible": pit_eligible,
        }
        analysis_result = analysis_by_key.get((ts_text, symbol, timeframe))
        display_base["analysis_outcome"] = (
            analysis_result.get("outcome") if analysis_result else None
        )
        display_base["analysis_move"] = analysis_result.get("move") if analysis_result else None
        if direction not in {"long", "short"}:
            if direction in {"neutral", "standby", "closed"} and symbol:
                display_decisions.append({**display_base, "outcome": direction, "move": None})
            continue
        directional += 1
        if not timeframe and not pit_eligible:
            ml_pit_ineligible += 1
        close = _number(entry.get("close"))
        atr = _number(entry.get("atr"))
        if close is None or not symbol:
            pending += 1
            if pit_eligible:
                ml_pit_pending += 1
            if symbol:
                display_decisions.append({**display_base, "outcome": "pending", "move": None})
            continue
        # その足の主ホライズンで採点(旧スキーマ行=24h)
        horizon = _number(entry.get("horizon_hours")) or 24.0
        future = _future_close(
            (pit_prices if pit_eligible else prices).get((symbol, timeframe), []),
            ts,
            horizon_hours=horizon,
            tolerance_hours=_tolerance_for(horizon),
        )
        if future is None:
            pending += 1
            if pit_eligible:
                ml_pit_pending += 1
            display_decisions.append({**display_base, "outcome": "pending", "move": None})
            continue
        move = future - close
        signed = move if direction == "long" else -move
        threshold = (atr or 0.0) * 0.1
        # 収益R: 判断方向の値動きをATR換算(=learning.move_atr相当)し、判断時に保存した
        # 執行コスト(R換算)を引いてコスト控除後の実現Rにする。atr・コストが揃う時だけ。
        net_r: float | None = None
        if atr and atr > 0:
            realized_r_atr = signed / atr
            cost_r = _number(entry.get("execution_cost_r"))
            if cost_r is not None:
                net_r = round(realized_r_atr - cost_r, 4)
        if abs(signed) <= threshold:
            flat += 1
            outcome = "flat"
        else:
            evaluated += 1
            if pit_eligible:
                ml_pit_evaluated += 1
            hit = signed > 0
            hits += int(hit)
            stat = by_symbol.setdefault(symbol, {"evaluated": 0, "hits": 0, "flat": 0})
            stat["evaluated"] += 1
            stat["hits"] += int(hit)
            if timeframe:
                tf_stat = by_timeframe.setdefault(timeframe, {"evaluated": 0, "hits": 0, "flat": 0})
                tf_stat["evaluated"] += 1
                tf_stat["hits"] += int(hit)
            outcome = "hit" if hit else "miss"
        outcome_row = {
            **display_base,
            "outcome": outcome,
            "move": round(move, 6),
            "net_r": net_r,
        }
        outcomes.append(outcome_row)
        display_decisions.append(outcome_row)
    return {
        "directional": directional,
        "evaluated": evaluated,
        "hits": hits,
        "flat": flat,
        "pending": pending,
        "hit_rate": hits / evaluated if evaluated else None,
        "by_symbol": by_symbol,
        "by_timeframe": by_timeframe,
        "analysis": analysis_evaluation,
        "recent_outcomes": outcomes[-20:],
        "curve": _learning_curve(outcomes),
        # 時間足タブ表示用。全体の直近20件だけだと特定の時間足が数件しか
        # 残らないため、時間足ごとに直近12件ずつ保持する(UIはこちらを優先し、
        # 無ければrecent_outcomesを自前でグルーピングして後方互換)。
        "recent_outcomes_by_timeframe": _recent_outcomes_by_timeframe(outcomes),
        # 表示専用。的中率や学習件数には混ぜず、方向判断だけでなく
        # neutral / standby / closed / 未成熟も時間足ごとに保持する。
        "recent_decisions_by_timeframe": _recent_outcomes_by_timeframe(display_decisions),
        "ml_eligible_after_thinning": _thinned_outcome_count(outcomes),
        "ml_pit_evaluated": ml_pit_evaluated,
        "ml_pit_pending": ml_pit_pending,
        "ml_pit_ineligible": ml_pit_ineligible,
    }


def _recent_outcomes_by_timeframe(
    outcomes: list[dict[str, Any]], per_timeframe: int = 12
) -> dict[str, list[dict[str, Any]]]:
    """満期採点済みの結果を時間足ごとに直近per_timeframe件ずつ返す。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in outcomes:
        timeframe = str(row.get("timeframe") or "")
        if not timeframe:
            continue
        grouped.setdefault(timeframe, []).append(row)
    return {
        timeframe: sorted(rows, key=lambda row: str(row.get("ts") or ""))[-per_timeframe:]
        for timeframe, rows in grouped.items()
    }


def _thinned_outcome_count(
    outcomes: list[dict[str, Any]],
    *,
    min_gap_hours: float = ML_THIN_MIN_GAP_HOURS,
) -> int:
    """Count GBDT rows using the same thin-then-drop-flat order as fx_intel.ml."""
    stamped: list[tuple[datetime, str, str]] = []
    for outcome in outcomes:
        if outcome.get("pit_eligible") is not True:
            continue
        result = str(outcome.get("outcome") or "")
        if result not in {"hit", "miss", "flat"}:
            continue
        ts = _parse_ts(outcome.get("ts"))
        symbol = str(outcome.get("symbol") or "")
        if ts is not None and symbol:
            stamped.append((ts, symbol, result))
    stamped.sort(key=lambda row: row[0])

    last_kept: dict[str, datetime] = {}
    kept = 0
    for ts, symbol, result in stamped:
        previous = last_kept.get(symbol)
        if previous is not None and (ts - previous) < timedelta(hours=min_gap_hours):
            continue
        last_kept[symbol] = ts
        if result in {"hit", "miss"}:
            kept += 1
    return kept


def _learning_curve(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """採点済み判断を時系列に並べ、累積の採点数と累積的中率の推移を返す。

    学習プロファイル(briefing_learning.json)は毎回上書きされ履歴が残らないため、
    「学習が進んでいるか」の推移は append-only の判断ログから再構築する。各点は
    その判断までの累積で、flat(小動き)は的中率の分母から除く(hits/evaluated と同義)。
    データが1日分でも点が増えるほど曲線が伸び、的中率が基準に収束していく様子が見える。

    的中率(方向)に加え、コスト控除後の累積純R(cum_net_r)も持つ。的中率が高くても
    薄利でコスト負けしていないか=「儲かっているか」を同じ時間軸で見るため。net_r は
    execution_cost_r が保存された判断でだけ算出されるので、cum_net_r は net_r を持つ
    採点のみを累積し、net_r_points にその件数を持たせる(欠損時は前値を据え置き)。
    """
    scored = sorted(
        (o for o in outcomes if o.get("outcome") in {"hit", "miss"}),
        key=lambda o: str(o.get("ts") or ""),
    )
    curve: list[dict[str, Any]] = []
    cumulative_hits = 0
    cumulative_net_r = 0.0
    net_r_points = 0
    for index, outcome in enumerate(scored, start=1):
        cumulative_hits += int(outcome.get("outcome") == "hit")
        net_r = outcome.get("net_r")
        if isinstance(net_r, (int, float)):
            cumulative_net_r += float(net_r)
            net_r_points += 1
        curve.append(
            {
                "ts": outcome.get("ts"),
                "scored": index,
                "hits": cumulative_hits,
                "hit_rate": round(cumulative_hits / index, 4),
                "cum_net_r": round(cumulative_net_r, 4),
                "net_r_points": net_r_points,
            }
        )
    return curve


def _journal_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_symbol: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    latest_ts: datetime | None = None
    for entry in entries:
        symbol = str(entry.get("symbol") or "unknown")
        direction = str(entry.get("direction") or "unknown")
        by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
        by_direction[direction] = by_direction.get(direction, 0) + 1
        ts = _parse_ts(entry.get("ts"))
        if ts is not None and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
    recent = entries[-30:]
    return {
        "total": len(entries),
        "by_symbol": by_symbol,
        "by_direction": by_direction,
        "latest_ts": latest_ts.isoformat() if latest_ts else None,
        "recent": recent,
        "activity": _journal_activity(entries),
    }


# 活動タイムライン用の方向カテゴリ。standby/neutral(見送り)と実方向を分けて
# 「いつ・どの方向を・何回記録したか」を積み上げで見せる。
_ACTIVITY_DIRECTIONS = ("long", "short", "neutral", "standby")


def _journal_activity(entries: list[dict[str, Any]], *, buckets: int = 48) -> dict[str, Any]:
    """判断ログを1時間刻みで集計し、方向別の積み上げ用データを返す。

    「どこで何を」学習しているかの前段として、記録がいつ・どの方向で・どの
    ペアで発生したかを可視化するための素材。採点はしない(direction 無し行や
    価格スナップショットは呼び出し側で除外して渡す)。
    """
    stamped: list[tuple[datetime, dict[str, Any]]] = []
    for entry in entries:
        ts = _parse_ts(entry.get("ts"))
        if ts is not None:
            stamped.append((ts, entry))
    if not stamped:
        return {"buckets": [], "by_direction": {}, "bucket_hours": 1}
    stamped.sort(key=lambda row: row[0])
    latest = stamped[-1][0].replace(minute=0, second=0, microsecond=0)
    start = latest - timedelta(hours=buckets - 1)
    slots: list[dict[str, Any]] = [
        {
            "ts": (start + timedelta(hours=offset)).isoformat(),
            "total": 0,
            **{direction: 0 for direction in _ACTIVITY_DIRECTIONS},
        }
        for offset in range(buckets)
    ]
    by_direction: dict[str, int] = {direction: 0 for direction in _ACTIVITY_DIRECTIONS}
    for ts, entry in stamped:
        index = int((ts - start).total_seconds() // 3600)
        if index < 0 or index >= buckets:
            continue
        direction = str(entry.get("direction") or "standby")
        if direction not in by_direction:
            direction = "standby"
        slots[index][direction] += 1
        slots[index]["total"] += 1
        by_direction[direction] += 1
    return {
        "buckets": slots,
        "by_direction": by_direction,
        "bucket_hours": 1,
    }


def _symbol_rows(learning: dict[str, Any], evaluated: dict[str, Any]) -> list[dict[str, Any]]:
    raw_stats = learning.get("symbol_stats")
    if not isinstance(raw_stats, dict):
        raw_stats = evaluated.get("by_symbol", {})
    rows: list[dict[str, Any]] = []
    raw_factors = learning.get("symbol_factors")
    factors: dict[str, Any] = raw_factors if isinstance(raw_factors, dict) else {}
    for symbol, stat in raw_stats.items():
        if not isinstance(stat, dict):
            continue
        n = int(stat.get("evaluated", 0) or 0)
        h = int(stat.get("hits", 0) or 0)
        rows.append(
            {
                "symbol": symbol,
                "evaluated": n,
                "hits": h,
                "hit_rate": h / n if n else None,
                "factor": _number(factors.get(symbol)) or 1.0,
            }
        )
    rows.sort(key=lambda row: (-row["evaluated"], row["symbol"]))
    return rows


def _condition_rows(learning: dict[str, Any]) -> list[dict[str, Any]]:
    stats = learning.get("condition_stats")
    factors = learning.get("condition_factors")
    if not isinstance(stats, dict):
        return []
    if not isinstance(factors, dict):
        factors = {}
    rows: list[dict[str, Any]] = []
    for feature, buckets in stats.items():
        if not isinstance(buckets, dict):
            continue
        for bucket, directions in buckets.items():
            if not isinstance(directions, dict):
                continue
            for direction, cell in directions.items():
                if not isinstance(cell, dict):
                    continue
                n = int(cell.get("evaluated", 0) or 0)
                h = int(cell.get("hits", 0) or 0)
                factor = (
                    factors.get(feature, {}).get(bucket, {}).get(direction)
                    if isinstance(factors.get(feature), dict)
                    else None
                )
                rows.append(
                    {
                        "feature": feature,
                        "bucket": bucket,
                        "direction": direction,
                        "evaluated": n,
                        "hits": h,
                        "hit_rate": h / n if n else None,
                        "factor": _number(factor),
                    }
                )
    rows.sort(
        key=lambda row: (
            row["hit_rate"] if row["hit_rate"] is not None else 2,
            -row["evaluated"],
        )
    )
    return rows[:20]


def _dimension_rows_from_stats(stats: object) -> list[dict[str, Any]]:
    if not isinstance(stats, dict):
        return []
    rows: list[dict[str, Any]] = []
    for dimension, buckets in stats.items():
        if not isinstance(buckets, dict):
            continue
        for bucket, directions in buckets.items():
            if not isinstance(directions, dict):
                continue
            for direction, cell in directions.items():
                if not isinstance(cell, dict):
                    continue
                rows.append(
                    {
                        "dimension": str(dimension),
                        "bucket": str(bucket),
                        "direction": str(direction),
                        "raw": int(cell.get("raw", 0) or 0),
                        "evaluated": int(cell.get("evaluated", 0) or 0),
                        "hits": int(cell.get("hits", 0) or 0),
                        "flat": int(cell.get("flat", 0) or 0),
                        "hit_rate": _number(cell.get("hit_rate")),
                        "avg_move_atr": _number(cell.get("avg_move_atr")),
                    }
                )
    rows.sort(key=lambda row: (row["dimension"], -row["evaluated"], row["bucket"]))
    return rows


def _outcome_dimension_rows(summary: object) -> list[dict[str, Any]]:
    if not isinstance(summary, dict):
        return []
    rows: list[dict[str, Any]] = []
    for dimension, buckets in summary.items():
        if not isinstance(buckets, dict):
            continue
        for bucket, directions in buckets.items():
            if not isinstance(directions, dict):
                continue
            for direction, cell in directions.items():
                if not isinstance(cell, dict):
                    continue
                rows.append(
                    {
                        "dimension": str(dimension),
                        "bucket": str(bucket),
                        "direction": str(direction),
                        "effective": int(cell.get("effective", 0) or 0),
                        "net_labels": int(cell.get("net_labels", 0) or 0),
                        "net_label_coverage": _number(cell.get("net_label_coverage")),
                        "net_expectancy_r": _number(cell.get("net_expectancy_r")),
                        "cumulative_net_r": _number(cell.get("cumulative_net_r")),
                    }
                )
    rows.sort(key=lambda row: (row["dimension"], -row["net_labels"], row["bucket"]))
    return rows


def _merge_dimension_stats(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for profile in profiles:
        stats = profile.get("dimension_stats")
        if not isinstance(stats, dict):
            continue
        for dimension, buckets in stats.items():
            if not isinstance(buckets, dict):
                continue
            for bucket, directions in buckets.items():
                if not isinstance(directions, dict):
                    continue
                for direction, raw_cell in directions.items():
                    if not isinstance(raw_cell, dict):
                        continue
                    cell = (
                        merged.setdefault(str(dimension), {})
                        .setdefault(str(bucket), {})
                        .setdefault(
                            str(direction),
                            {"raw": 0, "evaluated": 0, "hits": 0, "flat": 0},
                        )
                    )
                    for key in ("raw", "evaluated", "hits", "flat"):
                        cell[key] += int(raw_cell.get(key, 0) or 0)
    for buckets in merged.values():
        for directions in buckets.values():
            for cell in directions.values():
                n = cell["evaluated"]
                cell["hit_rate"] = cell["hits"] / n if n else None
    return merged


def _timeframe_learning_summary(payload: dict[str, Any]) -> dict[str, Any]:
    per_timeframe = payload.get("per_timeframe")
    if not isinstance(per_timeframe, dict):
        per_timeframe = {}
    rows: list[dict[str, Any]] = []
    for timeframe, raw_profile in per_timeframe.items():
        if not isinstance(raw_profile, dict):
            continue
        evaluated = int(raw_profile.get("evaluated", 0) or 0)
        hits = int(raw_profile.get("hits", 0) or 0)
        flat = int(raw_profile.get("flat", 0) or 0)
        row: dict[str, Any] = {
            "timeframe": str(timeframe),
            "generated_at": raw_profile.get("generated_at") or payload.get("generated_at"),
            "evaluated": evaluated,
            "hits": hits,
            "flat": flat,
            "hit_rate": hits / evaluated if evaluated else None,
            "tech_weight": _number(raw_profile.get("tech_weight")),
            "news_weight": _number(raw_profile.get("news_weight")),
            "tech_hit_rate": _number(raw_profile.get("tech_hit_rate")),
            "news_hit_rate": _number(raw_profile.get("news_hit_rate")),
            "conviction_brier": _number(raw_profile.get("conviction_brier")),
            "conviction_brier_base": _number(raw_profile.get("conviction_brier_base")),
            "bins": (raw_profile.get("bins") if isinstance(raw_profile.get("bins"), list) else []),
            "notes_ja": (
                raw_profile.get("notes_ja") if isinstance(raw_profile.get("notes_ja"), list) else []
            ),
            # どのペア・どの市場条件で学習したか(採点実体)。融合1判断と同じ
            # 抽出器を各時間足プロファイルに適用する。
            "symbols": _symbol_rows(raw_profile, {}),
            "conditions": _condition_rows(raw_profile),
        }
        rows.append(row)
    rows.sort(
        key=lambda row: (
            _TIMEFRAME_ORDER.get(str(row["timeframe"]), 99),
            row["timeframe"],
        )
    )
    evaluated = sum(int(row["evaluated"]) for row in rows)
    hits = sum(int(row["hits"]) for row in rows)
    flat = sum(int(row["flat"]) for row in rows)
    return {
        "generated_at": payload.get("generated_at") or _latest_generated_at(rows),
        "evaluated": evaluated,
        "hits": hits,
        "flat": flat,
        "hit_rate": hits / evaluated if evaluated else None,
        "tech_weight": _weighted_average(rows, "tech_weight") or 0.55,
        "news_weight": _weighted_average(rows, "news_weight") or 0.45,
        "tech_hit_rate": _weighted_average(rows, "tech_hit_rate"),
        "news_hit_rate": _weighted_average(rows, "news_hit_rate"),
        "conviction_brier": _weighted_average(rows, "conviction_brier"),
        "conviction_brier_base": _weighted_average(rows, "conviction_brier_base"),
        "timeframes": rows,
        "dimension_stats": _merge_dimension_stats(
            [profile for profile in per_timeframe.values() if isinstance(profile, dict)]
        ),
    }


def _latest_generated_at(rows: list[dict[str, Any]]) -> str | None:
    latest: datetime | None = None
    for row in rows:
        ts = _parse_ts(row.get("generated_at"))
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest.isoformat() if latest else None


def _weighted_average(rows: list[dict[str, Any]], key: str) -> float | None:
    total_weight = 0
    total = 0.0
    for row in rows:
        value = _number(row.get(key))
        weight = int(row.get("evaluated", 0) or 0)
        if value is None or weight <= 0:
            continue
        total += value * weight
        total_weight += weight
    return total / total_weight if total_weight else None


def _learning_source(
    learning: dict[str, Any],
    tf_learning: dict[str, Any],
    evaluated: dict[str, Any],
) -> dict[str, Any]:
    fusion_evaluated = int(learning.get("evaluated", 0) or 0)
    timeframe_evaluated = int(tf_learning.get("evaluated", 0) or 0)
    if learning and fusion_evaluated > 0:
        return {"mode": "fusion", "label_ja": "融合1判断", "has_profile": True}
    if tf_learning.get("timeframes") and timeframe_evaluated > 0:
        return {"mode": "timeframe", "label_ja": "時間足別", "has_profile": True}
    if learning:
        return {"mode": "fusion", "label_ja": "融合1判断", "has_profile": True}
    if tf_learning.get("timeframes"):
        return {"mode": "timeframe", "label_ja": "時間足別", "has_profile": True}
    if int(evaluated.get("evaluated", 0) or 0) > 0:
        return {"mode": "evaluated", "label_ja": "採点のみ", "has_profile": False}
    return {"mode": "none", "label_ja": "未学習", "has_profile": False}


def _learning_payload(
    learning: dict[str, Any],
    evaluated: dict[str, Any],
    tf_learning: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    source_mode = str(source.get("mode", "none"))
    if source_mode == "timeframe":
        evaluated_count = int(tf_learning.get("evaluated", 0) or 0)
        hits = int(tf_learning.get("hits", 0) or 0)
        return {
            "source": source_mode,
            "source_label_ja": source.get("label_ja"),
            "generated_at": tf_learning.get("generated_at"),
            "evaluated": evaluated_count,
            "hits": hits,
            "flat": int(tf_learning.get("flat", 0) or 0),
            "hit_rate": hits / evaluated_count if evaluated_count else None,
            "tech_weight": _number(tf_learning.get("tech_weight")) or 0.55,
            "news_weight": _number(tf_learning.get("news_weight")) or 0.45,
            "tech_hit_rate": _number(tf_learning.get("tech_hit_rate")),
            "news_hit_rate": _number(tf_learning.get("news_hit_rate")),
            "conviction_brier": _number(tf_learning.get("conviction_brier")),
            "conviction_brier_base": _number(tf_learning.get("conviction_brier_base")),
            "bins": [],
            "notes_ja": [],
            "symbols": [],
            "conditions": [],
            "dimensions": _dimension_rows_from_stats(tf_learning.get("dimension_stats")),
        }

    stored_evaluated = _number(learning.get("evaluated"))
    stored_hits = _number(learning.get("hits"))
    stored_flat = _number(learning.get("flat"))
    evaluated_count = int(
        stored_evaluated if stored_evaluated is not None else evaluated["evaluated"]
    )
    hits = int(stored_hits if stored_hits is not None else evaluated["hits"])
    return {
        "source": source_mode,
        "source_label_ja": source.get("label_ja"),
        "generated_at": learning.get("generated_at"),
        "evaluated": evaluated_count,
        "hits": hits,
        "flat": int(stored_flat if stored_flat is not None else evaluated["flat"]),
        "hit_rate": hits / evaluated_count if evaluated_count else None,
        "tech_weight": _number(learning.get("tech_weight")) or 0.55,
        "news_weight": _number(learning.get("news_weight")) or 0.45,
        "tech_hit_rate": _number(learning.get("tech_hit_rate")),
        "news_hit_rate": _number(learning.get("news_hit_rate")),
        "conviction_brier": _number(learning.get("conviction_brier")),
        "conviction_brier_base": _number(learning.get("conviction_brier_base")),
        "bins": learning.get("bins") if isinstance(learning.get("bins"), list) else [],
        "notes_ja": (
            learning.get("notes_ja") if isinstance(learning.get("notes_ja"), list) else []
        ),
        "symbols": _symbol_rows(learning, evaluated),
        "conditions": _condition_rows(learning),
        "dimensions": _dimension_rows_from_stats(learning.get("dimension_stats")),
    }


def _ml_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if payload and (
        payload.get("schema") != ML_ARTIFACT_SCHEMA
        or payload.get("training_contract") != ML_TRAINING_CONTRACT
    ):
        return {
            "trained_at": payload.get("trained_at"),
            "usable": False,
            "n_train": 0,
            "n_valid": 0,
            "base_rate": None,
            "metrics": {},
            "reasons": ["旧PIT契約のモデルを除外しました。再学習が必要です。"],
            "importance": [],
            "has_model": False,
        }
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    raw_return_head = payload.get("return_head")
    return_head: dict[str, Any] = raw_return_head if isinstance(raw_return_head, dict) else {}
    importance = payload.get("importance_by_name")
    if not isinstance(importance, dict):
        importance = {}
    importance_rows: list[dict[str, Any]] = [
        {"name": str(name), "value": float(value)}
        for name, value in importance.items()
        if isinstance(value, (int, float))
    ]
    importance_rows.sort(key=lambda row: float(row["value"]), reverse=True)
    return {
        "trained_at": payload.get("trained_at"),
        "usable": bool(payload.get("usable", False)),
        "n_train": int(payload.get("n_train", 0) or 0),
        "n_valid": int(payload.get("n_valid", 0) or 0),
        "base_rate": _number(payload.get("base_rate")),
        "metrics": metrics,
        "reasons": (payload.get("reasons") if isinstance(payload.get("reasons"), list) else []),
        "importance": importance_rows[:12],
        "has_model": payload.get("model") is not None,
        "return_head": dict(return_head),
    }


def _ml_training_progress(evaluated: dict[str, Any]) -> dict[str, Any]:
    """Explain the current fusion-journal progress toward GBDT training."""
    return {
        "source": JOURNAL_FILE,
        "horizon_hours": 24,
        "evaluated": int(evaluated.get("ml_pit_evaluated", 0) or 0),
        "eligible_after_thinning": int(evaluated.get("ml_eligible_after_thinning", 0) or 0),
        "pending": int(evaluated.get("ml_pit_pending", 0) or 0),
        "pit_ineligible": int(evaluated.get("ml_pit_ineligible", 0) or 0),
        "minimum_required": ML_MIN_TRAIN_ROWS,
        "thin_gap_hours": ML_THIN_MIN_GAP_HOURS,
    }


def _historical_chart_summary(payload: dict[str, Any]) -> dict[str, Any]:
    raw_cells = payload.get("cells")
    cells: list[dict[str, Any]] = []
    if isinstance(raw_cells, list):
        for raw in raw_cells:
            if not isinstance(raw, dict):
                continue
            metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
            samples = raw.get("samples") if isinstance(raw.get("samples"), dict) else {}
            cells.append(
                {
                    "pair": raw.get("pair"),
                    "timeframe": raw.get("timeframe"),
                    "stage": raw.get("stage", "shadow"),
                    "samples": samples,
                    "metrics": metrics,
                }
            )
    return {
        "exists": bool(payload),
        "trained_at": payload.get("trained_at"),
        "stage": payload.get("stage", "shadow"),
        "source_contract": payload.get("source_contract"),
        "data_windows": (
            payload.get("data_windows") if isinstance(payload.get("data_windows"), dict) else {}
        ),
        "operational_log_mixed": bool(payload.get("operational_log_mixed", False)),
        "promotion_admissible": bool(payload.get("promotion_admissible", False)),
        "canonical_pure_r": bool(payload.get("canonical_pure_r", False)),
        "canonical_pure_r_reason": payload.get("canonical_pure_r_reason"),
        "cells": cells,
    }


def _promotion_summary(payload: dict[str, Any]) -> dict[str, Any]:
    raw_stages = payload.get("stages")
    stages: dict[str, Any] = raw_stages if isinstance(raw_stages, dict) else {}
    raw_history = payload.get("history")
    history: list[Any] = raw_history if isinstance(raw_history, list) else []
    return {
        "stages": {
            "macro": stages.get("macro", "shadow"),
            "ml": stages.get("ml", "shadow"),
        },
        "updated_at": payload.get("updated_at"),
        "history": [row for row in history if isinstance(row, dict)][-20:],
    }


def _registry_records(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = payload.get("candidates")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _registry_records_by_stage(
    payload: dict[str, Any],
    stage: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    records = [
        record
        for record in _registry_records(payload).values()
        if record.get("status") == "active" and record.get("stage") == stage
    ]
    records.sort(
        key=lambda row: (
            -int(row.get("seen_count", 0) or 0),
            str(row.get("priority", "")),
            str(row.get("candidate_id", "")),
        )
    )
    return records[:limit]


def _list_from_payload(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _mapping(value: object) -> dict[str, Any]:
    """Return a plain dict when ``value`` is a dict, else an empty dict.

    Missing / malformed payload sections read as empty (fail-soft) and mypy sees
    one concrete ``dict[str, Any]`` instead of the ``Any | dict | None`` union
    that inline ``x if isinstance(x, dict) else {}`` ternaries leave behind.
    """

    return dict(value) if isinstance(value, dict) else {}


def _list_of_dicts(value: object) -> list[dict[str, Any]]:
    """Return the dict elements of ``value`` when it is a list, else empty."""

    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _trade_monitor_summary(
    monitor: dict[str, Any],
    registry: dict[str, Any],
) -> dict[str, Any]:
    monitor_registry = monitor.get("registry")
    if not isinstance(monitor_registry, dict):
        monitor_registry = {}
    records = _registry_records(registry)
    active = [record for record in records.values() if record.get("status") == "active"]

    def _count(stage: str) -> int:
        value = monitor_registry.get(f"{stage}_count")
        if isinstance(value, int):
            return value
        return sum(1 for record in active if record.get("stage") == stage)

    paper_ready = _list_from_payload(monitor_registry, "paper_ready") or _registry_records_by_stage(
        registry, "paper_ready"
    )
    approved = _list_from_payload(monitor_registry, "approved") or _registry_records_by_stage(
        registry, "approved"
    )
    auto_paused = _list_from_payload(monitor_registry, "auto_paused") or _registry_records_by_stage(
        registry, "auto_paused"
    )
    rejected = _list_from_payload(monitor_registry, "rejected") or _registry_records_by_stage(
        registry, "rejected"
    )
    recent_events = (
        _list_from_payload(monitor, "recent_events") or _list_from_payload(registry, "events")[-20:]
    )
    return {
        "generated_at": monitor.get("generated_at") or registry.get("generated_at"),
        "status": monitor.get("status") or monitor.get("health", {}).get("status") or "unknown",
        "exit_code": int(monitor.get("exit_code", 0) or 0),
        "health": (monitor.get("health") if isinstance(monitor.get("health"), dict) else {}),
        "counts": {
            "active": int(monitor_registry.get("active_count", len(active)) or 0),
            "paper_ready": _count("paper_ready"),
            "approved": _count("approved"),
            "auto_paused": _count("auto_paused"),
            "rejected": _count("rejected"),
            "resolved": int(
                monitor_registry.get(
                    "resolved_count",
                    sum(1 for record in records.values() if record.get("status") == "resolved"),
                )
                or 0
            ),
        },
        "alerts": _list_from_payload(monitor, "alerts")[:20],
        "paper_ready": paper_ready[:10],
        "approved": approved[:10],
        "auto_paused": auto_paused[:10],
        "rejected": rejected[:10],
        "approved_policy_stats": _list_from_payload(monitor, "approved_policy_stats")[:20],
        "recent_events": recent_events[-20:],
    }


def _decision_monitor_summary(
    monitor: dict[str, Any],
    feedback: dict[str, Any],
) -> dict[str, Any]:
    summary = _mapping(monitor.get("summary"))
    overall = _mapping(summary.get("overall"))
    counts = _mapping(summary.get("action_counts"))
    profile = monitor.get("profile") if isinstance(monitor.get("profile"), dict) else feedback
    raw_cells = _mapping(profile.get("cells") if isinstance(profile, dict) else None)
    cells = [dict(row) for row in raw_cells.values() if isinstance(row, dict)]
    rank = {
        "avoid": 0,
        "quality_guard": 1,
        "dampen": 2,
        "hold": 3,
        "collect_samples": 4,
    }
    actionable = [
        row for row in cells if str(row.get("action")) in {"avoid", "dampen", "quality_guard"}
    ]
    actionable.sort(
        key=lambda row: (
            rank.get(str(row.get("action")), 9),
            (
                _number(row.get("expectancy_r"))
                if _number(row.get("expectancy_r")) is not None
                else 99
            ),
            -int(row.get("tradable", 0) or 0),
            str(row.get("symbol", "")),
        )
    )
    worst_cells = _list_of_dicts(summary.get("worst_cells"))
    failures = _list_of_dicts(summary.get("failure_reason_summary"))
    tradable_zero = _mapping(summary.get("tradable_zero_reasons"))
    model_delta = _mapping(summary.get("model_expectancy_delta"))
    price_health = _mapping(summary.get("price_health"))
    performance = _mapping(summary.get("performance"))
    return {
        "generated_at": monitor.get("generated_at") or feedback.get("generated_at"),
        "status": monitor.get("status") or "unknown",
        "exit_code": int(monitor.get("exit_code", 0) or 0),
        "overall": dict(overall),
        "performance": dict(performance),
        "tradable_zero_reasons": dict(tradable_zero),
        "model_expectancy_delta": dict(model_delta),
        "price_health": dict(price_health),
        "counts": dict(counts),
        "decision_events": int(summary.get("decision_events", 0) or 0),
        "scored_outcomes": int(summary.get("scored_outcomes", 0) or 0),
        "actionable_cells": actionable[:10],
        "worst_cells": [dict(row) for row in worst_cells[:10]],
        "failure_reason_summary": [dict(row) for row in failures[:10]],
        "alerts": _list_from_payload(monitor, "alerts")[:10],
        "findings": _list_from_payload(monitor, "findings")[:20],
    }


def _net_r_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Summarize canonical net-R labels without recomputing their accounting."""

    raw_outcomes = payload.get("outcomes")
    outcomes = raw_outcomes if isinstance(raw_outcomes, list) else []
    scored = [
        row
        for row in outcomes
        if isinstance(row, dict) and _number(row.get("realized_r")) is not None
    ]
    labeled = [
        row
        for row in scored
        if _number(row.get("realized_net_r")) is not None
        and bool(row.get("net_label_eligible", row.get("tradable", False)))
    ]
    values = [float(row["realized_net_r"]) for row in labeled]
    cumulative = 0.0
    curve: list[dict[str, Any]] = []
    for row, value in sorted(zip(labeled, values), key=lambda item: str(item[0].get("ts", ""))):
        cumulative += value
        curve.append(
            {
                "ts": row.get("ts"),
                "decision_id": row.get("decision_id"),
                "realized_net_r": round(value, 4),
                "cumulative_net_r": round(cumulative, 4),
            }
        )
    missing: dict[str, int] = {}
    for row in scored:
        if _number(row.get("realized_net_r")) is not None:
            continue
        flags = row.get("quality_flags")
        if not isinstance(flags, list):
            continue
        for flag in flags:
            key = str(flag)
            if key.startswith(("missing_", "invalid_", "net_label_", "negative_")):
                missing[key] = missing.get(key, 0) + 1
    return {
        "scored": len(scored),
        "labels": len(labeled),
        "coverage": len(labeled) / len(scored) if scored else 0.0,
        "expectancy_r": sum(values) / len(values) if values else None,
        "cumulative_net_r": sum(values) if values else None,
        "label_versions": sorted({str(row.get("label_version")) for row in labeled}),
        "cost_model_ids": sorted({str(row.get("cost_model_id")) for row in labeled}),
        "missing_reasons": dict(sorted(missing.items())),
        "curve": curve,
    }


def _input_context_summary(
    journal_entries: list[dict[str, Any]],
    decision_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize C input coverage without recalculating decision features."""

    contextual = [row for row in journal_entries if row.get("input_context_id")]
    by_timeframe: dict[str, dict[str, int]] = {}
    feature_available: dict[str, int] = {}
    feature_total: dict[str, int] = {}
    for row in journal_entries:
        timeframe = str(row.get("timeframe") or "fusion")
        cell = by_timeframe.setdefault(timeframe, {"rows": 0, "context": 0})
        cell["rows"] += 1
        if row.get("input_context_id"):
            cell["context"] += 1
        masks = row.get("input_feature_masks")
        if isinstance(masks, dict):
            for key, raw in masks.items():
                name = str(key)
                feature_total[name] = feature_total.get(name, 0) + 1
                feature_available[name] = feature_available.get(name, 0) + int(bool(raw))

    contexts: dict[str, dict[str, Any]] = {}
    for event in decision_events:
        decision = event.get("decision")
        if not isinstance(decision, dict):
            continue
        context = decision.get("input_context")
        if not isinstance(context, dict):
            continue
        context_id = str(context.get("context_id") or decision.get("input_context_id") or "")
        if context_id:
            contexts[context_id] = context

    liquidity_status: dict[str, int] = {}
    macro_status: dict[str, int] = {}
    quote_sources: dict[str, int] = {}
    spreads: list[float] = []
    shadow_would_block = 0
    for context in contexts.values():
        macro = context.get("macro")
        if isinstance(macro, dict):
            status = str(macro.get("quality_status") or "unknown")
            macro_status[status] = macro_status.get(status, 0) + 1
        liquidity = context.get("liquidity")
        if not isinstance(liquidity, dict):
            continue
        status = str(liquidity.get("status") or "unknown")
        liquidity_status[status] = liquidity_status.get(status, 0) + 1
        shadow_would_block += int(status in {"stressed", "invalid"})
        features = liquidity.get("features")
        if isinstance(features, dict):
            spread = _number(features.get("spread_pips"))
            if spread is not None:
                spreads.append(spread)
        quote = liquidity.get("quote")
        if isinstance(quote, dict):
            source = str(quote.get("source") or "unknown")
            quote_sources[source] = quote_sources.get(source, 0) + 1

    sorted_spreads = sorted(spreads)
    return {
        "rows": len(journal_entries),
        "context_rows": len(contextual),
        "coverage": len(contextual) / len(journal_entries) if journal_entries else 0.0,
        "unique_contexts": len(contexts),
        "by_timeframe": [
            {
                "timeframe": timeframe,
                **counts,
                "coverage": counts["context"] / counts["rows"] if counts["rows"] else 0.0,
            }
            for timeframe, counts in sorted(by_timeframe.items())
        ],
        "feature_coverage": [
            {
                "feature": key,
                "available": feature_available.get(key, 0),
                "rows": total,
                "coverage": feature_available.get(key, 0) / total if total else 0.0,
            }
            for key, total in sorted(feature_total.items())
        ],
        "macro_status": dict(sorted(macro_status.items())),
        "liquidity_status": dict(sorted(liquidity_status.items())),
        "quote_sources": dict(sorted(quote_sources.items())),
        "shadow_would_block": shadow_would_block,
        "spread_pips": {
            "n": len(sorted_spreads),
            "p50": _percentile(sorted_spreads, 0.50),
            "p90": _percentile(sorted_spreads, 0.90),
            "p99": _percentile(sorted_spreads, 0.99),
        },
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, round((len(values) - 1) * percentile)))
    return values[index]


def _horizon_summary(payload: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    raw_profiles = payload.get("profiles")
    profiles = raw_profiles if isinstance(raw_profiles, dict) else {}
    rows: list[dict[str, Any]] = []
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        promotion = profile.get("promotion")
        promotion = promotion if isinstance(promotion, dict) else {}
        rows.append(
            {
                "symbol": str(profile.get("symbol", "")),
                "horizon": str(profile.get("horizon", "")),
                "n_scored": int(profile.get("n_scored", 0) or 0),
                "hits": int(profile.get("hits", 0) or 0),
                "misses": int(profile.get("misses", 0) or 0),
                "hit_rate": _number(profile.get("hit_rate")),
                "mean_brier": _number(profile.get("mean_brier")),
                "mean_log_loss": _number(profile.get("mean_log_loss")),
                "band_coverage": _number(profile.get("band_coverage")),
                "mean_net_r": _number(profile.get("mean_net_r")),
                "calibrated": bool(profile.get("calibrated")),
                "stage": str(promotion.get("stage", "shadow")),
                "permanent_shadow": bool(promotion.get("permanent_shadow")),
                "remaining_n": promotion.get("remaining_n"),
                "estimated_market_days_remaining": _number(
                    promotion.get("estimated_market_days_remaining")
                ),
            }
        )
    rows.sort(key=lambda row: (row["symbol"], _HORIZON_ORDER.get(row["horizon"], 99)))

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        key = (str(entry.get("symbol", "")), str(entry.get("horizon", "")))
        ts = _parse_ts(entry.get("ts"))
        previous = latest.get(key)
        if ts is not None and (
            previous is None
            or ts > (_parse_ts(previous.get("ts")) or datetime.min.replace(tzinfo=UTC))
        ):
            latest[key] = entry
    matrix = [
        {
            "symbol": symbol,
            "horizon": horizon,
            "direction": row.get("direction"),
            "conviction": row.get("conviction"),
            "p_up": _number(row.get("p_up")),
            "p_down": _number(row.get("p_down")),
            "p_flat": _number(row.get("p_flat")),
            "calibrated": bool(row.get("calibrated")),
            "ts": row.get("ts"),
        }
        for (symbol, horizon), row in latest.items()
    ]
    matrix.sort(key=lambda row: (str(row["symbol"]), _HORIZON_ORDER.get(str(row["horizon"]), 99)))
    return {
        "generated_at": payload.get("generated_at"),
        "contract": payload.get("contract", "horizon-pit-v1"),
        "gbdt_review_gate": payload.get("gbdt_review_gate"),
        "scored_total": int(payload.get("scored_total", 0) or 0),
        "immature": int(payload.get("immature", 0) or 0),
        "unresolved": int(payload.get("unresolved", 0) or 0),
        "pit_ineligible": int(payload.get("pit_ineligible", 0) or 0),
        "profiles": rows,
        "latest": matrix,
        "journal_rows": len(entries),
    }


def build_state(
    log_dir: Path,
    *,
    now: datetime | None = None,
    ps_output: str | None = None,
    launchctl_outputs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    journal_path = log_dir / JOURNAL_FILE
    learning_path = log_dir / LEARNING_FILE
    ml_path = log_dir / ML_FILE
    promotion_path = log_dir / PROMOTION_FILE
    trade_monitor_path = log_dir / TRADE_MONITOR_FILE
    trade_registry_path = log_dir / TRADE_REGISTRY_FILE
    decision_log_path = log_dir / DECISION_LOG_FILE
    decision_latest_path = log_dir / DECISION_LATEST_FILE
    decision_outcomes_path = log_dir / DECISION_OUTCOMES_FILE
    decision_feedback_path = log_dir / DECISION_FEEDBACK_FILE
    decision_monitor_path = log_dir / DECISION_MONITOR_FILE
    tf_journal_path = log_dir / TF_JOURNAL_FILE
    tf_learning_path = log_dir / TF_LEARNING_FILE
    tf_prices_path = log_dir / TF_PRICES_FILE
    historical_chart_path = log_dir / HISTORICAL_CHART_FILE
    horizon_journal_path = log_dir / HORIZON_JOURNAL_FILE
    horizon_learning_path = log_dir / HORIZON_LEARNING_FILE

    entries = _read_journal(journal_path)
    tf_entries = _read_journal(tf_journal_path)
    # 価格スナップショット(direction 無し)。採点対象は増えないが、短い足の
    # 将来価格系列を密にして 15m/1h も採点可能にする(fx_briefing 本体と同じ結合)。
    tf_price_rows = _read_journal(tf_prices_path)
    horizon_entries = _read_journal(horizon_journal_path)
    horizon_learning = _load_json(horizon_learning_path)
    learning = _load_json(learning_path)
    tf_learning = _timeframe_learning_summary(_load_json(tf_learning_path))
    ml = _load_json(ml_path)
    historical_chart = _load_json(historical_chart_path)
    promotion = _load_json(promotion_path)
    trade_monitor = _load_json(trade_monitor_path)
    trade_registry = _load_json(trade_registry_path)
    decision_feedback = _load_json(decision_feedback_path)
    decision_monitor = _load_json(decision_monitor_path)
    decision_outcomes = _load_json(decision_outcomes_path)
    decision_latest = _load_json(decision_latest_path)
    raw_decision_events = decision_latest.get("events")
    decision_events = (
        [dict(row) for row in raw_decision_events if isinstance(row, dict)]
        if isinstance(raw_decision_events, list)
        else []
    )
    # 融合1判断行(24h)と時間足別行(各主ホライズン)を同じ採点器で評価する。
    # _evaluate_journal は行ごとに timeframe/horizon_hours を見て採点する。
    # 価格スナップショット行は将来価格系列にだけ寄与する(direction 無しなので
    # 採点対象=directional にはカウントされない)。
    evaluated = _evaluate_journal(entries + tf_entries + tf_price_rows)
    # GBDT is trained only from the fusion journal.  Do not present timeframe
    # outcomes as GBDT-ready samples; their horizons and schemas are different.
    fusion_evaluated = _evaluate_journal(entries)

    source = _learning_source(learning, tf_learning, evaluated)
    learning_payload = _learning_payload(learning, evaluated, tf_learning, source)
    journal_summary = _journal_summary(entries + tf_entries)
    journal_summary["fusion_total"] = len(entries)
    journal_summary["timeframe_total"] = len(tf_entries)

    files = {
        JOURNAL_FILE: _file_status(journal_path),
        LEARNING_FILE: _file_status(learning_path),
        ML_FILE: _file_status(ml_path),
        HISTORICAL_CHART_FILE: _file_status(historical_chart_path),
        PROMOTION_FILE: _file_status(promotion_path),
        TRADE_MONITOR_FILE: _file_status(trade_monitor_path),
        TRADE_REGISTRY_FILE: _file_status(trade_registry_path),
        DECISION_LOG_FILE: _file_status(decision_log_path),
        DECISION_LATEST_FILE: _file_status(decision_latest_path),
        DECISION_OUTCOMES_FILE: _file_status(decision_outcomes_path),
        DECISION_FEEDBACK_FILE: _file_status(decision_feedback_path),
        DECISION_MONITOR_FILE: _file_status(decision_monitor_path),
        TF_JOURNAL_FILE: _file_status(tf_journal_path),
        TF_LEARNING_FILE: _file_status(tf_learning_path),
        TF_PRICES_FILE: _file_status(tf_prices_path),
        HORIZON_JOURNAL_FILE: _file_status(horizon_journal_path),
        HORIZON_LEARNING_FILE: _file_status(horizon_learning_path),
    }

    return {
        "generated_at": now.isoformat(),
        "read_only": True,
        "log_dir": str(log_dir),
        "files": files,
        "ops": _ops_status(
            log_dir,
            files,
            now=now,
            ps_output=ps_output,
            launchctl_outputs=launchctl_outputs,
        ),
        "journal": journal_summary,
        "evaluation": evaluated,
        "learning_source": source,
        "learning": learning_payload,
        "tf_learning": tf_learning,
        "horizon": _horizon_summary(horizon_learning, horizon_entries),
        "ml": {
            **_ml_summary(ml),
            "training": _ml_training_progress(fusion_evaluated),
        },
        "historical_chart": _historical_chart_summary(historical_chart),
        "promotion": _promotion_summary(promotion),
        "trade_monitor": _trade_monitor_summary(trade_monitor, trade_registry),
        "decision_monitor": _decision_monitor_summary(decision_monitor, decision_feedback),
        "net_r": _net_r_summary(decision_outcomes),
        "dimension_outcomes": _outcome_dimension_rows(decision_outcomes.get("dimension_summary")),
        "shadow": (
            dict(decision_outcomes.get("shadow_summary", {}))
            if isinstance(decision_outcomes.get("shadow_summary"), dict)
            else {}
        ),
        "input_context": _input_context_summary(entries + tf_entries, decision_events),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "FxLearningDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            raw_log_dir = query.get("logDir", [str(self.server.log_dir)])[0]  # type: ignore[attr-defined]
            log_dir = Path(raw_log_dir).expanduser().resolve()
            self._send_json(build_state(log_dir))
            return
        if parsed.path in {"", "/"}:
            self._send_file(STATIC_DIR / "index.html")
            return
        target = (STATIC_DIR / parsed.path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        self._send_file(target)

    def log_message(self, fmt: str, *args: object) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {fmt % args}")

    def _send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or path.suffix in {".js", ".css"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only fx_intel learning dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(os.environ.get("FX_LEARNING_LOG_DIR", DEFAULT_LOG_DIR)),
        help="Directory containing briefing_journal.jsonl / briefing_learning.json / ml_model.json",
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.log_dir = args.log_dir.expanduser().resolve()  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/"
    print(f"AI learning dashboard: {url}")
    print(f"Reading logs from: {server.log_dir}")  # type: ignore[attr-defined]
    print("Read-only mode. Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
