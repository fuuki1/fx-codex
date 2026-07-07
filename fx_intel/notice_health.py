"""Operational health checks for the detailed notice pipeline."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Mapping

from . import notice_feedback, notice_journal, notice_quality

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


@dataclass(frozen=True)
class NoticeHealthCheck:
    name: str
    status: str
    message: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NoticeHealthReport:
    checks: list[NoticeHealthCheck]

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


def check_notice_health(
    *,
    journal_path: str | Path,
    feedback_path: str | Path,
    webhook_url: str | None = None,
    require_discord: bool = False,
    quality_json_path: str | Path | None = None,
    quality_csv_path: str | Path | None = None,
    smoke_dir: str | Path | None = None,
    max_journal_age_hours: float | None = None,
    now: datetime | None = None,
) -> NoticeHealthReport:
    """Check local operational health for detailed trade notices."""
    now = _utc(now or datetime.now(UTC))
    checks = [
        _check_discord(webhook_url, require_discord),
        _check_journal(Path(journal_path), now=now, max_age_hours=max_journal_age_hours),
        _check_feedback(Path(feedback_path)),
    ]
    if quality_json_path is not None:
        checks.append(_check_quality_json(Path(quality_json_path)))
    if quality_csv_path is not None:
        checks.append(_check_quality_csv(Path(quality_csv_path)))
    if smoke_dir is not None:
        checks.extend(_check_smoke_dir(Path(smoke_dir)))
    return NoticeHealthReport(checks=checks)


def format_health_report_ja(report: NoticeHealthReport) -> str:
    """Format a compact Japanese health report."""
    status_label = {
        STATUS_OK: "OK",
        STATUS_WARN: "WARN",
        STATUS_FAIL: "FAIL",
    }[report.status]
    lines = [f"詳細通知ヘルスチェック: {status_label}"]
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


def _check_discord(webhook_url: str | None, required: bool) -> NoticeHealthCheck:
    if webhook_url:
        return NoticeHealthCheck("discord_webhook", STATUS_OK, "Discord webhook 設定あり")
    if required:
        return NoticeHealthCheck("discord_webhook", STATUS_FAIL, "Discord webhook が未設定")
    return NoticeHealthCheck(
        "discord_webhook", STATUS_OK, "Discord webhook は今回の点検で必須ではありません"
    )


def _check_journal(
    path: Path,
    *,
    now: datetime,
    max_age_hours: float | None,
) -> NoticeHealthCheck:
    if not path.exists():
        return NoticeHealthCheck(
            "notice_journal", STATUS_WARN, "詳細通知ジャーナルがまだありません"
        )
    entries = list(notice_journal.read_notice_entries(path))
    if not entries:
        return NoticeHealthCheck(
            "notice_journal", STATUS_WARN, "詳細通知ジャーナルに有効行がありません"
        )
    latest = entries[-1]
    required = ("ts", "symbol", "direction", "price_plan", "entry_level_source", "report_sha256")
    missing = [key for key in required if key not in latest]
    if missing:
        return NoticeHealthCheck(
            "notice_journal",
            STATUS_FAIL,
            "最新ジャーナル行の必須項目が不足",
            {"missing": ",".join(missing)},
        )
    ts = _parse_dt(latest.get("ts"))
    if ts is None:
        return NoticeHealthCheck("notice_journal", STATUS_FAIL, "最新ジャーナル行の時刻が不正")
    details: dict[str, object] = {"entries": len(entries), "latest_ts": ts.isoformat()}
    if max_age_hours is not None and max_age_hours > 0:
        age_hours = (now - ts).total_seconds() / 3600
        details["age_hours"] = round(age_hours, 2)
        if age_hours > max_age_hours:
            return NoticeHealthCheck(
                "notice_journal",
                STATUS_WARN,
                "最新ジャーナル行が古くなっています",
                details,
            )
    return NoticeHealthCheck("notice_journal", STATUS_OK, "詳細通知ジャーナルは読込可能", details)


def _check_feedback(path: Path) -> NoticeHealthCheck:
    if not path.exists():
        return NoticeHealthCheck(
            "notice_feedback", STATUS_WARN, "詳細通知フィードバックがまだありません"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return NoticeHealthCheck(
            "notice_feedback",
            STATUS_FAIL,
            "詳細通知フィードバックJSONが不正",
            {"error": str(error)},
        )
    if not isinstance(raw, Mapping):
        return NoticeHealthCheck(
            "notice_feedback",
            STATUS_FAIL,
            "詳細通知フィードバックJSONのルートがobjectではありません",
        )
    profile = notice_feedback.NoticeFeedbackProfile.from_dict(raw)
    if not profile.cells:
        return NoticeHealthCheck(
            "notice_feedback", STATUS_WARN, "詳細通知フィードバックに条件セルがありません"
        )
    return NoticeHealthCheck(
        "notice_feedback",
        STATUS_OK,
        "詳細通知フィードバックは読込可能",
        {"cells": len(profile.cells), "evaluated": profile.evaluated},
    )


def _check_quality_json(path: Path) -> NoticeHealthCheck:
    if not path.exists():
        return NoticeHealthCheck(
            "quality_json", STATUS_FAIL, "指定された詳細通知評価JSONがありません"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return NoticeHealthCheck(
            "quality_json", STATUS_FAIL, "詳細通知評価JSONが不正", {"error": str(error)}
        )
    if not isinstance(raw, Mapping):
        return NoticeHealthCheck(
            "quality_json", STATUS_FAIL, "詳細通知評価JSONのルートがobjectではありません"
        )
    if raw.get("schema") != notice_quality.QUALITY_REPORT_SCHEMA_VERSION:
        return NoticeHealthCheck(
            "quality_json",
            STATUS_WARN,
            "詳細通知評価JSONのschemaが現在値と異なります",
            {"schema": raw.get("schema")},
        )
    summary = raw.get("summary")
    outcomes = raw.get("outcomes")
    if not isinstance(summary, Mapping) or not isinstance(outcomes, list):
        return NoticeHealthCheck(
            "quality_json", STATUS_FAIL, "詳細通知評価JSONのsummary/outcomesが不正"
        )
    return NoticeHealthCheck(
        "quality_json",
        STATUS_OK,
        "詳細通知評価JSONは読込可能",
        {"outcomes": len(outcomes), "evaluated": summary.get("evaluated")},
    )


def _check_quality_csv(path: Path) -> NoticeHealthCheck:
    if not path.exists():
        return NoticeHealthCheck(
            "quality_csv", STATUS_FAIL, "指定された詳細通知評価CSVがありません"
        )
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fields = set(reader.fieldnames or [])
    except OSError as error:
        return NoticeHealthCheck(
            "quality_csv", STATUS_FAIL, "詳細通知評価CSVを読めません", {"error": str(error)}
        )
    required = {"symbol", "outcome", "entry_scenario", "entry_check"}
    missing = sorted(required - fields)
    if missing:
        return NoticeHealthCheck(
            "quality_csv",
            STATUS_FAIL,
            "詳細通知評価CSVの必須列が不足",
            {"missing": ",".join(missing)},
        )
    if not rows:
        return NoticeHealthCheck(
            "quality_csv", STATUS_WARN, "詳細通知評価CSVにデータ行がありません"
        )
    return NoticeHealthCheck(
        "quality_csv", STATUS_OK, "詳細通知評価CSVは読込可能", {"rows": len(rows)}
    )


def _check_smoke_dir(path: Path) -> list[NoticeHealthCheck]:
    if not path.exists():
        return [
            NoticeHealthCheck(
                "notice_smoke", STATUS_WARN, "E2Eスモーク成果物ディレクトリがまだありません"
            )
        ]
    required = [
        "trade_notice_report.md",
        "trade_notice_journal.jsonl",
        "notice_quality.json",
        "notice_quality.csv",
        "trade_notice_feedback.json",
    ]
    missing = [name for name in required if not (path / name).exists()]
    if missing:
        return [
            NoticeHealthCheck(
                "notice_smoke",
                STATUS_WARN,
                "E2Eスモーク成果物が一部不足",
                {"missing": ",".join(missing)},
            )
        ]
    return [NoticeHealthCheck("notice_smoke", STATUS_OK, "E2Eスモーク成果物は揃っています")]


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_details(details: Mapping[str, object]) -> str:
    return ", ".join(f"{key}={value}" for key, value in details.items() if value is not None)
