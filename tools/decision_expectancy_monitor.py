#!/usr/bin/env python3
"""Operational runner for complete decision expected-R monitoring.

This command scores the complete decision log by TP/SL first-touch, MFE, MAE,
and realized R, derives the next-decision feedback profile, then writes a
monitor JSON suitable for cron, CI, and the read-only dashboard.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel import decision_feedback, decision_log, journal, trade_outcome  # noqa: E402

DEFAULT_DECISION_LOG_PATH = REPO_ROOT / "logs" / "briefing_decisions.jsonl"
DEFAULT_TF_PRICES_PATH = REPO_ROOT / "logs" / "briefing_tf_prices.jsonl"
DEFAULT_OUTCOME_REPORT_PATH = REPO_ROOT / "logs" / "briefing_decision_outcomes.json"
DEFAULT_FEEDBACK_PATH = REPO_ROOT / "logs" / "briefing_decision_feedback.json"
DEFAULT_MONITOR_PATH = REPO_ROOT / "logs" / "decision_expectancy_monitor.json"
DEFAULT_PRICE_STALE_MINUTES = 15.0


def run_decision_expectancy_monitor(
    *,
    decision_log_path: Path = DEFAULT_DECISION_LOG_PATH,
    prices_path: Path | None = DEFAULT_TF_PRICES_PATH,
    outcome_json_path: Path | None = DEFAULT_OUTCOME_REPORT_PATH,
    feedback_json_path: Path | None = DEFAULT_FEEDBACK_PATH,
    monitor_json_path: Path = DEFAULT_MONITOR_PATH,
    require_sample_ok: bool = False,
    price_stale_minutes: float | None = DEFAULT_PRICE_STALE_MINUTES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the full complete-decision expectancy monitoring pass."""

    generated_at = _utc(now or datetime.now(UTC))
    events = list(decision_log.read_decision_events(decision_log_path))
    price_rows = list(journal.read_entries(prices_path)) if prices_path is not None else []
    price_health = _price_file_health(
        prices_path,
        price_rows,
        now=generated_at,
        stale_minutes=price_stale_minutes,
    )
    outcome_report = decision_log.score_decision_events(
        events,
        price_entries=price_rows,
        now=generated_at,
    )
    profile = decision_feedback.derive_decision_feedback(outcome_report, now=generated_at)
    monitor = decision_feedback.build_monitoring_snapshot(
        profile,
        outcome_report,
        now=generated_at,
        require_sample_ok=require_sample_ok,
        price_health=price_health,
    )
    monitor["runner"] = {
        "schema": 1,
        "decision_log_path": str(decision_log_path),
        "prices_path": str(prices_path) if prices_path is not None else None,
        "outcome_json_path": str(outcome_json_path) if outcome_json_path else None,
        "feedback_json_path": str(feedback_json_path) if feedback_json_path else None,
        "monitor_json_path": str(monitor_json_path),
        "decision_event_count": len(events),
        "price_row_count": len(price_rows),
        "require_sample_ok": require_sample_ok,
        "price_stale_minutes": price_stale_minutes,
    }

    if outcome_json_path is not None:
        decision_log.save_outcome_report(outcome_report, outcome_json_path)
    if feedback_json_path is not None:
        decision_feedback.save_decision_feedback(profile, feedback_json_path)
    _write_json_atomic(monitor_json_path, monitor)

    return {
        "exit_code": int(monitor.get("exit_code", 0) or 0),
        "monitor": monitor,
        "outcome_report": outcome_report,
        "profile": profile,
        "outcome_report_path": outcome_json_path,
        "feedback_json_path": feedback_json_path,
        "monitor_json_path": monitor_json_path,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="完全判断ログのTP/SL/MFE/MAE期待R監視を一括実行する"
    )
    parser.add_argument("--decision-log", type=Path, default=DEFAULT_DECISION_LOG_PATH)
    parser.add_argument(
        "--prices",
        type=Path,
        default=DEFAULT_TF_PRICES_PATH,
        help="時間足別の価格系列JSONL。none で無効化",
    )
    parser.add_argument(
        "--outcome-json",
        type=Path,
        default=DEFAULT_OUTCOME_REPORT_PATH,
        help="完全判断ログのTP/SL/MFE/MAE採点JSON。none で無効化",
    )
    parser.add_argument(
        "--feedback-json",
        type=Path,
        default=DEFAULT_FEEDBACK_PATH,
        help="次回判断に反映する期待RフィードバックJSON。none で無効化",
    )
    parser.add_argument("--monitor-json", type=Path, default=DEFAULT_MONITOR_PATH)
    parser.add_argument(
        "--health-require-sample",
        action="store_true",
        help="最低サンプル数未満をヘルスチェック失敗扱いにする",
    )
    parser.add_argument(
        "--price-stale-minutes",
        default=str(DEFAULT_PRICE_STALE_MINUTES),
        help="価格系列のstale判定分数。none で無効化",
    )
    parser.add_argument("--quiet", action="store_true", help="標準出力の要約を抑制する")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = run_decision_expectancy_monitor(
        decision_log_path=args.decision_log,
        prices_path=_optional_path(args.prices),
        outcome_json_path=_optional_path(args.outcome_json),
        feedback_json_path=_optional_path(args.feedback_json),
        monitor_json_path=args.monitor_json,
        require_sample_ok=args.health_require_sample,
        price_stale_minutes=_optional_float(args.price_stale_minutes),
    )
    if not args.quiet:
        _print_summary(result)
    return int(result["exit_code"])


def _print_summary(result: Mapping[str, Any]) -> None:
    monitor = result.get("monitor")
    if not isinstance(monitor, Mapping):
        print("decision expectancy monitor: no monitor payload")
        return
    summary = monitor.get("summary") if isinstance(monitor.get("summary"), Mapping) else {}
    overall = summary.get("overall") if isinstance(summary.get("overall"), Mapping) else {}
    counts = (
        summary.get("action_counts") if isinstance(summary.get("action_counts"), Mapping) else {}
    )
    price_health = (
        summary.get("price_health") if isinstance(summary.get("price_health"), Mapping) else {}
    )
    print(
        "decision expectancy monitor: "
        f"status={monitor.get('status')} exit={monitor.get('exit_code')} "
        f"events={summary.get('decision_events', 0)} "
        f"scored={summary.get('scored_outcomes', 0)} "
        f"expectancy={_fmt_r(overall.get('expectancy_r') if overall else None)} "
        f"price={price_health.get('status', 'unknown')} "
        f"avoid={counts.get('avoid', 0) if counts else 0} "
        f"dampen={counts.get('dampen', 0) if counts else 0}"
    )
    print(f"outcome_json={result.get('outcome_report_path')}")
    print(f"feedback_json={result.get('feedback_json_path')}")
    print(f"monitor_json={result.get('monitor_json_path')}")


def _optional_path(path: Path | str | None) -> Path | None:
    if path is None:
        return None
    if str(path).strip().lower() == "none":
        return None
    return Path(path)


def _optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    if str(value).strip().lower() == "none":
        return None
    return float(value)


def _price_file_health(
    path: Path | None,
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    stale_minutes: float | None,
) -> dict[str, object]:
    if path is None:
        return {
            "status": "pass",
            "enabled": False,
            "path": None,
            "message_ja": "価格系列のstale監視は無効です",
        }
    if stale_minutes is None:
        return {
            "status": "pass",
            "enabled": False,
            "path": str(path),
            "row_count": len(rows),
            "message_ja": "価格系列のstale監視は無効です",
        }
    if not path.exists():
        return {
            "status": "fail",
            "enabled": True,
            "path": str(path),
            "row_count": 0,
            "stale_minutes": stale_minutes,
            "message_ja": "時間足別価格スナップショットが未作成です",
        }
    latest = _latest_row_ts(rows) or datetime.fromtimestamp(path.stat().st_mtime, UTC)
    age_minutes = max(0.0, (now - latest).total_seconds() / 60.0)
    status = "fail" if age_minutes > stale_minutes or not rows else "pass"
    return {
        "status": status,
        "enabled": True,
        "path": str(path),
        "row_count": len(rows),
        "latest_ts": latest.isoformat(),
        "age_minutes": round(age_minutes, 1),
        "stale_minutes": stale_minutes,
        "message_ja": (
            f"時間足別価格スナップショットがstaleです({age_minutes:.1f}分)"
            if status == "fail"
            else "時間足別価格スナップショットは更新されています"
        ),
    }


def _latest_row_ts(rows: Sequence[Mapping[str, Any]]) -> datetime | None:
    latest: datetime | None = None
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
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
                trade_outcome.json_safe(payload),
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


def _fmt_r(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}R"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
