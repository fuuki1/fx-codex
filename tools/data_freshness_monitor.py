"""学習データ供給パイプラインの鮮度監視 + Discord通知(WARNING/CRITICAL/RECOVERY)。

fx_tf_snapshot(5分毎)とfx_briefing(毎時:10)が書き続けるジャーナル・スナップショット・
学習ファイルの最終更新を監視し、停止を数分以内に検知してDiscordへ通知する。
launchd(com.fx-codex.health)から5分間隔のワンショットで起動される前提。

設計原則:
- 監視対象と閾値はコードにハードコードせず ops/freshness_targets.json で設定する
- 通知は「状態が変化した時」だけ送る(ok→warning→critical→ok=recovery)。
  同一状態の再通知はcooldown(既定6時間)経過後のみ。状態は
  logs/freshness_state.json に永続化し、プロセス再起動をまたいで重複抑止する
- Discord送信失敗はWARNINGログを残すだけで監視自体は失敗させない
  (通知経路の障害がデータ収集や監視の停止に波及しない)
- 状態・レポートのJSON書込みは tmp→fsync→atomic rename で破損を防ぐ
- 鮮度はfilesystem mtimeとpayload内の設定済みtimestampの古い方で判定する。
  mtime更新で古い観測値を新鮮に見せられない
- 欠損は隠さない: 監視レポートに age_seconds / last_ok / 遷移履歴を必ず残す
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, UTC
import hashlib
import json
from math import isfinite
import os
from pathlib import Path
import socket
import sys
import tempfile
from collections.abc import Callable, Mapping

DEFAULT_CONFIG_PATH = "ops/freshness_targets.json"
DEFAULT_STATE_PATH = "logs/freshness_state.json"
DEFAULT_REPORT_PATH = "logs/freshness_report.json"
DEFAULT_COOLDOWN_SECONDS = 6 * 3600

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_CRITICAL = "critical"
STATUS_ORDER = {STATUS_OK: 0, STATUS_WARNING: 1, STATUS_CRITICAL: 2}

# Discordのembed色(左バー)。重要度が一目で分かるように標準色に合わせる
EMBED_COLORS = {
    STATUS_WARNING: 0xF1C40F,  # 黄
    STATUS_CRITICAL: 0xE74C3C,  # 赤
    "recovery": 0x2ECC71,  # 緑
}

NotifySender = Callable[[str, dict], bool]


@dataclass(frozen=True)
class TargetConfig:
    name: str
    path: str
    timestamp_field: str
    kind: str = "jsonl"
    expected_interval_seconds: float = 3600.0
    warn_after_seconds: float = 7200.0
    critical_after_seconds: float | None = 21600.0
    manual_action_ja: str = ""
    require_content_hash: bool = False
    expected_key_fields: tuple[str, ...] = ()
    expected_keys: tuple[tuple[str, ...], ...] = ()
    unchanged_value_fields: tuple[str, ...] = ()
    max_unchanged_observations: int | None = None
    lookback_records: int = 2048
    source_timestamp_field: str = ""
    source_warn_after_seconds: float | None = None
    source_critical_after_seconds: float | None = None
    source_timestamp_missing_status: str = STATUS_WARNING

    def __post_init__(self) -> None:
        expected = _finite_float(self.expected_interval_seconds, "expected_interval_seconds")
        warning = _finite_float(self.warn_after_seconds, "warn_after_seconds")
        critical = (
            _finite_float(self.critical_after_seconds, "critical_after_seconds")
            if self.critical_after_seconds is not None
            else None
        )
        if expected <= 0 or warning <= expected:
            raise ValueError("freshness thresholds require 0 < expected_interval < warn_after")
        if critical is not None and critical <= warning:
            raise ValueError("critical_after_seconds must exceed warn_after_seconds")
        source_warning = (
            _finite_float(self.source_warn_after_seconds, "source_warn_after_seconds")
            if self.source_warn_after_seconds is not None
            else None
        )
        source_critical = (
            _finite_float(self.source_critical_after_seconds, "source_critical_after_seconds")
            if self.source_critical_after_seconds is not None
            else None
        )
        if not self.source_timestamp_field and (
            source_warning is not None or source_critical is not None
        ):
            raise ValueError("source staleness thresholds require source_timestamp_field")
        if source_warning is not None and source_warning <= 0:
            raise ValueError("source_warn_after_seconds must be positive")
        if source_critical is not None and (
            source_warning is None or source_critical <= source_warning
        ):
            raise ValueError(
                "source_critical_after_seconds requires and must exceed source_warn_after_seconds"
            )
        if self.source_timestamp_missing_status not in {STATUS_WARNING, STATUS_CRITICAL}:
            raise ValueError("source_timestamp_missing_status must be warning or critical")


@dataclass
class TargetResult:
    """1対象の監視結果。JSONレポートの1行に対応。"""

    name: str
    path: str
    status: str = STATUS_OK
    reason: str = ""
    last_update: str | None = None
    record_timestamp: str | None = None
    file_mtime: str | None = None
    timestamp_field: str = ""
    age_seconds: float | None = None
    expected_interval_seconds: float = 0.0
    warn_after_seconds: float = 0.0
    critical_after_seconds: float | None = None
    manual_action_ja: str = ""
    quality_details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "status": self.status,
            "reason": self.reason,
            "last_update": self.last_update,
            "record_timestamp": self.record_timestamp,
            "file_mtime": self.file_mtime,
            "timestamp_field": self.timestamp_field,
            "age_seconds": self.age_seconds,
            "expected_interval_seconds": self.expected_interval_seconds,
            "warn_after_seconds": self.warn_after_seconds,
            "critical_after_seconds": self.critical_after_seconds,
            "manual_action_ja": self.manual_action_ja,
            "quality_details": self.quality_details,
        }


@dataclass
class TargetState:
    """状態遷移と通知抑止のための永続状態(1対象ぶん)。"""

    status: str = STATUS_OK
    since: str | None = None
    last_ok: str | None = None
    consecutive_failures: int = 0
    last_notified_status: str = ""
    last_notified_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "since": self.since,
            "last_ok": self.last_ok,
            "consecutive_failures": self.consecutive_failures,
            "last_notified_status": self.last_notified_status,
            "last_notified_at": self.last_notified_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> TargetState:
        return cls(
            status=str(payload.get("status", STATUS_OK)),
            since=_opt_str(payload.get("since")),
            last_ok=_opt_str(payload.get("last_ok")),
            consecutive_failures=_opt_int(payload.get("consecutive_failures")),
            last_notified_status=str(payload.get("last_notified_status", "")),
            last_notified_at=_opt_str(payload.get("last_notified_at")),
        )


@dataclass
class Notification:
    """送信予定のDiscord通知1件。"""

    severity: str  # "warning" / "critical" / "recovery"
    target: str
    title: str
    body_lines: list[str] = field(default_factory=list)


def _opt_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _opt_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    numeric = float(value)
    return max(0, int(numeric)) if isfinite(numeric) else 0


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} must be a finite number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a finite number") from error
    if not isfinite(numeric):
        raise ValueError(f"{label} must be a finite number")
    return numeric


def load_config(path: str | Path) -> tuple[list[TargetConfig], float]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("freshness config must be a JSON object")
    cooldown = _finite_float(
        payload.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS),
        "cooldown_seconds",
    )
    if cooldown <= 0:
        raise ValueError("cooldown_seconds must be positive")
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("freshness config requires at least one target")
    targets: list[TargetConfig] = []
    for row in raw_targets:
        if not isinstance(row, Mapping):
            raise ValueError("freshness target must be a JSON object")
        critical_raw = row.get("critical_after_seconds")
        timestamp_field = str(row.get("timestamp_field", "")).strip()
        if not timestamp_field:
            raise ValueError(f"freshness target {row.get('name')!r} requires timestamp_field")
        kind = str(row.get("kind", "jsonl"))
        if kind not in {"json", "jsonl"}:
            raise ValueError(f"unsupported freshness target kind: {kind!r}")
        expected_interval = _finite_float(
            row.get("expected_interval_seconds", 3600),
            "expected_interval_seconds",
        )
        warn_after = _finite_float(row.get("warn_after_seconds", 7200), "warn_after_seconds")
        critical_after = (
            _finite_float(critical_raw, "critical_after_seconds")
            if critical_raw is not None
            else None
        )
        if expected_interval <= 0 or warn_after <= expected_interval:
            raise ValueError("freshness thresholds require 0 < expected_interval < warn_after")
        if critical_after is not None and critical_after <= warn_after:
            raise ValueError("critical_after_seconds must exceed warn_after_seconds")

        key_fields = _field_names(row.get("expected_key_fields"))
        expected_keys = _expected_keys(row.get("expected_keys"), len(key_fields))
        unchanged_fields = _field_names(row.get("unchanged_value_fields"))
        unchanged_limit_raw = row.get("max_unchanged_observations")
        unchanged_limit = (
            _positive_int(unchanged_limit_raw, "max_unchanged_observations")
            if unchanged_limit_raw is not None
            else None
        )
        lookback = _positive_int(row.get("lookback_records", 2048), "lookback_records")
        source_timestamp_field = str(row.get("source_timestamp_field", "")).strip()
        source_warn_raw = row.get("source_warn_after_seconds")
        source_critical_raw = row.get("source_critical_after_seconds")
        source_missing_status = (
            str(row.get("source_timestamp_missing_status", STATUS_WARNING)).strip().lower()
        )
        if source_missing_status not in {STATUS_WARNING, STATUS_CRITICAL}:
            raise ValueError("source_timestamp_missing_status must be warning or critical")
        source_warn = (
            _finite_float(source_warn_raw, "source_warn_after_seconds")
            if source_warn_raw is not None
            else None
        )
        source_critical = (
            _finite_float(source_critical_raw, "source_critical_after_seconds")
            if source_critical_raw is not None
            else None
        )
        if not source_timestamp_field and (source_warn is not None or source_critical is not None):
            raise ValueError("source staleness thresholds require source_timestamp_field")
        if source_timestamp_field:
            source_warn = warn_after if source_warn is None else source_warn
            source_critical = critical_after if source_critical is None else source_critical
            if source_warn <= 0:
                raise ValueError("source_warn_after_seconds must be positive")
            if source_critical is not None and source_critical <= source_warn:
                raise ValueError(
                    "source_critical_after_seconds must exceed source_warn_after_seconds"
                )
        if bool(key_fields) != bool(expected_keys):
            raise ValueError("expected_key_fields and expected_keys must be configured together")
        if unchanged_limit is not None:
            if kind != "jsonl" or not key_fields or not unchanged_fields:
                raise ValueError("unchanged checks require JSONL, expected keys, and value fields")
            if unchanged_limit < 2 or lookback < unchanged_limit:
                raise ValueError("unchanged lookback/limit is invalid")
        targets.append(
            TargetConfig(
                name=str(row["name"]),
                path=str(row["path"]),
                timestamp_field=timestamp_field,
                kind=kind,
                expected_interval_seconds=expected_interval,
                warn_after_seconds=warn_after,
                critical_after_seconds=critical_after,
                manual_action_ja=str(row.get("manual_action_ja", "")),
                require_content_hash=bool(row.get("require_content_hash", False)),
                expected_key_fields=key_fields,
                expected_keys=expected_keys,
                unchanged_value_fields=unchanged_fields,
                max_unchanged_observations=unchanged_limit,
                lookback_records=lookback,
                source_timestamp_field=source_timestamp_field,
                source_warn_after_seconds=source_warn,
                source_critical_after_seconds=source_critical,
                source_timestamp_missing_status=source_missing_status,
            )
        )
    return targets, cooldown


def _field_names(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ValueError("freshness field lists must be non-empty arrays")
    values = tuple(str(value).strip() for value in raw)
    if any(not value for value in values) or len(values) != len(set(values)):
        raise ValueError("freshness field names must be unique and non-empty")
    return values


def _expected_keys(raw: object, width: int) -> tuple[tuple[str, ...], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw or width <= 0:
        raise ValueError("expected_keys requires expected_key_fields")
    keys: list[tuple[str, ...]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != width:
            raise ValueError("each expected key must match expected_key_fields")
        key = tuple(str(value).strip() for value in item)
        if any(not value for value in key):
            raise ValueError("expected key values must be non-empty")
        keys.append(key)
    if len(keys) != len(set(keys)):
        raise ValueError("expected_keys must be unique")
    return tuple(keys)


def _positive_int(raw: object, label: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return raw


def check_target(target: TargetConfig, root: Path, now: datetime) -> TargetResult:
    """1対象の存在・payload timestamp・mtimeをfail closedで検証する。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    result = TargetResult(
        name=target.name,
        path=target.path,
        timestamp_field=target.timestamp_field,
        expected_interval_seconds=target.expected_interval_seconds,
        warn_after_seconds=target.warn_after_seconds,
        critical_after_seconds=target.critical_after_seconds,
        manual_action_ja=target.manual_action_ja,
    )
    file_path = root / target.path
    if not file_path.exists():
        result.status = STATUS_CRITICAL
        result.reason = "file_missing"
        return result
    if not file_path.is_file():
        result.status = STATUS_CRITICAL
        result.reason = "file_not_regular"
        return result

    try:
        mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=UTC)
    except OSError:
        result.status = STATUS_CRITICAL
        result.reason = "file_unreadable"
        return result
    result.file_mtime = mtime.isoformat()
    mtime_age = (now - mtime).total_seconds()
    if mtime_age < 0:
        result.status = STATUS_CRITICAL
        result.reason = "mtime_future"
        result.last_update = mtime.isoformat()
        result.age_seconds = round(mtime_age, 1)
        return result

    payload, payload_error = _read_target_payload(file_path, target.kind)
    if payload_error:
        result.status = STATUS_CRITICAL
        result.reason = payload_error
        return result
    assert payload is not None
    hash_error = _payload_hash_error(payload, required=target.require_content_hash)
    if hash_error:
        result.status = STATUS_CRITICAL
        result.reason = hash_error
        return result

    record_timestamp, timestamp_error = _payload_timestamp(payload, target.timestamp_field)
    if timestamp_error:
        result.status = STATUS_CRITICAL
        result.reason = timestamp_error
        return result
    assert record_timestamp is not None
    result.record_timestamp = record_timestamp.isoformat()
    record_age = (now - record_timestamp).total_seconds()
    if record_age < 0:
        result.status = STATUS_CRITICAL
        result.reason = "timestamp_future"
        result.last_update = record_timestamp.isoformat()
        result.age_seconds = round(record_age, 1)
        return result

    # 両方が新鮮であることを必須とする。touch/copyでmtimeだけ更新しても
    # 古いrecordは古いまま。逆に古いmtimeも配置/復元異常として残る。
    effective_update = min(record_timestamp, mtime)
    age = max(record_age, mtime_age)
    result.last_update = effective_update.isoformat()
    result.age_seconds = round(age, 1)

    if target.critical_after_seconds is not None and age > target.critical_after_seconds:
        result.status = STATUS_CRITICAL
        result.reason = "stale_critical"
    elif age > target.warn_after_seconds:
        result.status = STATUS_WARNING
        result.reason = "stale_warning"
    if target.kind == "jsonl":
        recent_status, recent_reason, recent_details = _check_recent_integrity(
            file_path, target, now
        )
        result.quality_details.extend(recent_details)
        if STATUS_ORDER[recent_status] > STATUS_ORDER[result.status]:
            result.status = recent_status
            result.reason = recent_reason
        elif recent_status == result.status and recent_reason:
            result.reason = recent_reason
    if target.expected_keys:
        series_status, series_reason, details = _check_expected_series(file_path, target, now)
        result.quality_details.extend(details)
        if STATUS_ORDER[series_status] > STATUS_ORDER[result.status]:
            result.status = series_status
            result.reason = series_reason
        elif series_status == result.status and series_reason:
            result.reason = series_reason
    return result


def _check_recent_integrity(
    path: Path,
    target: TargetConfig,
    now: datetime,
) -> tuple[str, str, list[str]]:
    """Verify the bounded recent history, including hidden secondary clocks."""

    try:
        lines = _read_recent_nonempty_lines(path, target.lookback_records)
    except (OSError, UnicodeError):
        return STATUS_CRITICAL, "file_unreadable", []
    if not lines:
        return STATUS_CRITICAL, "jsonl_empty", []

    timestamp_fields = (
        "ts",
        "event_time",
        "available_time",
        "ingested_time",
        "published_time",
        "revision_time",
        "source_time",
    )
    latest: dict[tuple[str, ...], tuple[datetime, Mapping[str, object], dict[str, datetime]]] = {}
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return STATUS_CRITICAL, "jsonl_corrupt_recent", []
        if not isinstance(payload, Mapping):
            return STATUS_CRITICAL, "payload_not_object", []
        hash_error = _payload_hash_error(payload, required=target.require_content_hash)
        if hash_error:
            return STATUS_CRITICAL, f"recent_{hash_error}", []
        record_time, timestamp_error = _payload_timestamp(payload, target.timestamp_field)
        if timestamp_error or record_time is None:
            return STATUS_CRITICAL, f"recent_{timestamp_error or 'timestamp_invalid'}", []
        if record_time > now:
            return STATUS_CRITICAL, "recent_timestamp_future", []

        parsed: dict[str, datetime] = {}
        for timestamp_column in timestamp_fields:
            raw = payload.get(timestamp_column)
            if raw is None:
                continue
            value, error = _payload_timestamp(payload, timestamp_column)
            if error or value is None:
                return (
                    STATUS_CRITICAL,
                    f"recent_{timestamp_column}_{error or 'invalid'}",
                    [],
                )
            if value > now:
                return STATUS_CRITICAL, f"recent_{timestamp_column}_future", []
            parsed[timestamp_column] = value
        available = parsed.get("available_time")
        if available is not None:
            for field in (
                "event_time",
                "ingested_time",
                "published_time",
                "revision_time",
                "source_time",
            ):
                value = parsed.get(field)
                if value is not None and value > available:
                    return STATUS_CRITICAL, f"recent_{field}_after_available", []

        key = (
            tuple(str(payload.get(field, "")).strip() for field in target.expected_key_fields)
            if target.expected_key_fields
            else ("__target__",)
        )
        prior = latest.get(key)
        if prior is None or record_time > prior[0]:
            latest[key] = (record_time, payload, parsed)

    if not target.source_timestamp_field:
        return STATUS_OK, "", []

    details: list[str] = []
    status = STATUS_OK
    reason = ""
    for key, (_record_time, payload, parsed) in sorted(latest.items()):
        label = ",".join(key)
        source_raw = payload.get(target.source_timestamp_field)
        if source_raw is None:
            details.append(f"{label}: {target.source_timestamp_field}_unavailable")
            if STATUS_ORDER[target.source_timestamp_missing_status] > STATUS_ORDER[status]:
                status = target.source_timestamp_missing_status
                reason = "source_timestamp_missing"
            continue
        source_time = parsed.get(target.source_timestamp_field)
        if source_time is None:
            return STATUS_CRITICAL, "source_timestamp_invalid", details
        age = (now - source_time).total_seconds()
        if (
            target.source_critical_after_seconds is not None
            and age > target.source_critical_after_seconds
        ):
            status = STATUS_CRITICAL
            reason = "source_stale_critical"
            details.append(f"{label}: source_age_seconds={age:.1f}")
        elif (
            target.source_warn_after_seconds is not None
            and age > target.source_warn_after_seconds
            and status != STATUS_CRITICAL
        ):
            status = STATUS_WARNING
            reason = "source_stale_warning"
            details.append(f"{label}: source_age_seconds={age:.1f}")
    return status, reason, details


def _read_last_nonempty_line(path: Path, chunk: int = 65536) -> str | None:
    """ファイル全体を読まずに末尾の非空行を返す(ジャーナルは数MBに育つため)。"""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - chunk))
        data = handle.read().decode("utf-8")
    lines = [line for line in data.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _read_recent_nonempty_lines(
    path: Path,
    max_lines: int,
    *,
    chunk: int = 65536,
) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        while position > 0 and data.count(b"\n") < max_lines + 1:
            amount = min(chunk, position)
            position -= amount
            handle.seek(position)
            data = handle.read(amount) + data
    if position > 0:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    return [line for line in data.decode("utf-8").splitlines() if line.strip()][-max_lines:]


def _payload_hash_error(payload: Mapping[str, object], *, required: bool) -> str:
    supplied = payload.get("content_hash")
    if supplied is None:
        return "content_hash_missing" if required else ""
    if not isinstance(supplied, str) or len(supplied) != 64:
        return "content_hash_invalid"
    canonical = {str(key): value for key, value in payload.items() if key != "content_hash"}
    try:
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return "content_hash_unverifiable"
    expected = hashlib.sha256(encoded).hexdigest()
    return "" if supplied == expected else "content_hash_mismatch"


def _check_expected_series(
    path: Path,
    target: TargetConfig,
    now: datetime,
) -> tuple[str, str, list[str]]:
    try:
        lines = _read_recent_nonempty_lines(path, target.lookback_records)
    except (OSError, UnicodeError):
        return STATUS_CRITICAL, "file_unreadable", []

    observations: list[tuple[datetime, tuple[str, ...], tuple[object, ...]]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return STATUS_CRITICAL, "jsonl_corrupt_recent", []
        if not isinstance(payload, Mapping):
            return STATUS_CRITICAL, "payload_not_object", []
        hash_error = _payload_hash_error(payload, required=target.require_content_hash)
        if hash_error:
            return STATUS_CRITICAL, f"recent_{hash_error}", []
        timestamp, timestamp_error = _payload_timestamp(payload, target.timestamp_field)
        if timestamp_error or timestamp is None:
            return STATUS_CRITICAL, f"recent_{timestamp_error or 'timestamp_invalid'}", []
        if timestamp > now:
            return STATUS_CRITICAL, "recent_timestamp_future", []
        key = tuple(str(payload.get(field, "")).strip() for field in target.expected_key_fields)
        if any(not value for value in key):
            return STATUS_CRITICAL, "expected_key_missing_field", []
        values = tuple(payload.get(field) for field in target.unchanged_value_fields)
        observations.append((timestamp, key, values))

    expected = set(target.expected_keys)
    latest: dict[tuple[str, ...], datetime] = {}
    latest_value: dict[tuple[str, ...], tuple[object, ...]] = {}
    unchanged_count: dict[tuple[str, ...], int] = {}
    closed: set[tuple[str, ...]] = set()
    for timestamp, key, values in reversed(observations):
        if key not in expected or key in closed:
            continue
        if key not in latest:
            latest[key] = timestamp
            latest_value[key] = values
            unchanged_count[key] = 1
        elif latest_value[key] == values:
            unchanged_count[key] += 1
        else:
            closed.add(key)

    missing = sorted(expected - set(latest))
    if missing:
        missing_details = ["missing=" + ",".join(key) for key in missing]
        return STATUS_CRITICAL, "expected_key_missing", missing_details

    details: list[str] = []
    status = STATUS_OK
    reason = ""
    for key in sorted(expected):
        key_label = ",".join(key)
        age = (now - latest[key]).total_seconds()
        if target.critical_after_seconds is not None and age > target.critical_after_seconds:
            status = STATUS_CRITICAL
            reason = "expected_key_stale_critical"
            details.append(f"{key_label}: age_seconds={age:.1f}")
        elif age > target.warn_after_seconds and status != STATUS_CRITICAL:
            status = STATUS_WARNING
            reason = "expected_key_stale_warning"
            details.append(f"{key_label}: age_seconds={age:.1f}")
        if (
            target.max_unchanged_observations is not None
            and unchanged_count[key] >= target.max_unchanged_observations
        ):
            status = STATUS_CRITICAL
            reason = "payload_unchanged"
            details.append(f"{key_label}: identical_observations={unchanged_count[key]}")
    return status, reason, details


def _read_target_payload(path: Path, kind: str) -> tuple[Mapping[str, object] | None, str]:
    """Read the authoritative payload and return a fail-closed reason on any ambiguity."""
    try:
        if kind == "jsonl":
            tail = _read_last_nonempty_line(path)
            if tail is None:
                return None, "jsonl_empty"
            try:
                payload = json.loads(tail)
            except json.JSONDecodeError:
                return None, "jsonl_corrupt_tail"
        elif kind == "json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None, "json_corrupt"
        else:
            return None, "unsupported_kind"
    except (OSError, UnicodeError):
        return None, "file_unreadable"
    if not isinstance(payload, Mapping):
        return None, "payload_not_object"
    return payload, ""


def _payload_timestamp(
    payload: Mapping[str, object], timestamp_field: str
) -> tuple[datetime | None, str]:
    raw = payload.get(timestamp_field)
    if not isinstance(raw, str) or not raw.strip():
        return None, "timestamp_missing"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None, "timestamp_invalid"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "timestamp_naive"
    return parsed.astimezone(UTC), ""


def evaluate(
    results: list[TargetResult],
    states: dict[str, TargetState],
    now: datetime,
    cooldown_seconds: float,
) -> tuple[dict[str, TargetState], list[Notification]]:
    """監視結果を前回状態と突き合わせ、新状態と送るべき通知を決める。

    通知ポリシー:
    - 悪化(ok→warn, ok→crit, warn→crit): 即通知
    - 回復(warn/crit→ok): recovery通知(直前が通知済みの場合のみ)
    - 同一の非ok状態: cooldown経過後のみ再通知(状態は更新し続ける)
    """
    host = socket.gethostname()
    new_states: dict[str, TargetState] = {}
    notifications: list[Notification] = []
    for result in results:
        previous = states.get(result.name, TargetState())
        state = TargetState(
            status=result.status,
            since=previous.since,
            last_ok=previous.last_ok,
            consecutive_failures=previous.consecutive_failures,
            last_notified_status=previous.last_notified_status,
            last_notified_at=previous.last_notified_at,
        )
        if result.status != previous.status:
            state.since = now.isoformat()
        if result.status == STATUS_OK:
            state.last_ok = now.isoformat()
            state.consecutive_failures = 0
        else:
            state.consecutive_failures = previous.consecutive_failures + 1

        should_notify = False
        severity = result.status
        if result.status == STATUS_OK:
            # 直前に非okを「通知していた」場合だけrecoveryを送る(無音の揺れは黙殺)
            if previous.status != STATUS_OK and previous.last_notified_status in (
                STATUS_WARNING,
                STATUS_CRITICAL,
            ):
                should_notify = True
                severity = "recovery"
        elif STATUS_ORDER[result.status] > STATUS_ORDER.get(previous.status, 0):
            should_notify = True  # 悪化は即通知
        elif result.status == previous.status:
            last_at = _parse_ts(previous.last_notified_at)
            if previous.last_notified_status != result.status:
                should_notify = True  # 前回の通知が未送信/失敗なら次周期に再試行
            elif last_at is not None and (now - last_at).total_seconds() >= cooldown_seconds:
                should_notify = True  # 同一状態の継続はcooldown後に再通知
        # 改善方向(crit→warn)は通知しない(recoveryまで待つ)

        if should_notify:
            notifications.append(_build_notification(severity, result, state, previous, host, now))
        new_states[result.name] = state
    return new_states, notifications


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "不明"
    if seconds < 120:
        return f"{seconds:.0f}秒"
    if seconds < 7200:
        return f"{seconds / 60:.0f}分"
    return f"{seconds / 3600:.1f}時間"


def _build_notification(
    severity: str,
    result: TargetResult,
    state: TargetState,
    previous: TargetState,
    host: str,
    now: datetime,
) -> Notification:
    label = {"warning": "⚠️ WARNING", "critical": "🚨 CRITICAL", "recovery": "✅ RECOVERY"}[
        severity if severity in ("warning", "critical", "recovery") else "warning"
    ]
    lines = [
        f"ホスト: {host}",
        f"対象: {result.name} ({result.path})",
        f"発生時刻: {now.isoformat()}",
        f"最終更新: {result.last_update or '記録なし'}",
        f"経過: {_format_duration(result.age_seconds)} (期待間隔 {_format_duration(result.expected_interval_seconds)})",
        f"最終正常: {state.last_ok or previous.last_ok or '記録なし'}",
    ]
    if severity == "recovery":
        outage_start = _parse_ts(previous.since)
        if outage_start is not None:
            lines.append(f"停止時間: {_format_duration((now - outage_start).total_seconds())}")
        lines.append("データ収集の鮮度が正常へ回復しました")
    else:
        lines.append(f"理由: {result.reason}")
        lines.append(f"連続検知: {state.consecutive_failures}回目")
        if result.manual_action_ja:
            lines.append(f"手動対応: {result.manual_action_ja}")
    return Notification(
        severity=severity,
        target=result.name,
        title=f"{label} データ鮮度 — {result.name}",
        body_lines=lines,
    )


def load_webhook_url(root: Path) -> str | None:
    """DISCORD_OPS_WEBHOOK_URL(運用専用)を優先し、無ければ既存のDISCORD_WEBHOOK_URL。

    fx_briefing.pyのload_webhook_urlと同じく環境変数→.envの順で読む。
    秘密情報はplistへ埋めず、実行時にここで解決する。
    """
    for key in ("DISCORD_OPS_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
        url = os.environ.get(key)
        if url:
            return url.strip()
    env_path = root / ".env"
    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("DISCORD_OPS_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"):
        if values.get(key):
            return values[key]
    return None


def send_discord(webhook_url: str, payload: dict) -> bool:
    """Discordへ送信。失敗してもFalseを返すだけで例外は伝播させない。"""
    import requests

    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        return 200 <= response.status_code < 300
    except Exception as exc:  # noqa: BLE001 - 通知失敗が監視を殺してはいけない
        # requests例外の文字列にはwebhook path/tokenが含まれ得る。
        # 例外型以外は永続ログへ出さない。
        print(
            f"[freshness] Discord送信失敗: {type(exc).__name__}",
            file=sys.stderr,
        )
        return False


def notification_payload(notification: Notification) -> dict:
    return {
        "embeds": [
            {
                "title": notification.title,
                "description": "\n".join(notification.body_lines),
                "color": EMBED_COLORS.get(notification.severity, EMBED_COLORS["warning"]),
            }
        ]
    }


def atomic_write_json(path: Path, payload: object) -> None:
    """tmp→fsync→renameの原子的書込み(途中クラッシュで壊れたJSONを残さない)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        tmp_name = handle.name
    os.replace(tmp_name, path)


def load_states(path: Path) -> dict[str, TargetState]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    targets = payload.get("targets", {}) if isinstance(payload, dict) else {}
    if not isinstance(targets, dict):
        return {}
    return {
        str(name): TargetState.from_dict(row)
        for name, row in targets.items()
        if isinstance(row, dict)
    }


def run_monitor(
    root: Path,
    config_path: Path,
    state_path: Path,
    report_path: Path,
    now: datetime | None = None,
    sender: NotifySender | None = None,
    notify: bool = True,
) -> dict[str, object]:
    """監視を1回実行し、レポートdictを返す(launchdから5分毎に呼ばれる)。"""
    now = now or datetime.now(UTC)
    targets, cooldown = load_config(config_path)
    results = [check_target(target, root, now) for target in targets]
    states = load_states(state_path)
    new_states, notifications = evaluate(results, states, now, cooldown)

    sent: list[dict[str, object]] = []
    if notify and notifications:
        webhook_url = load_webhook_url(root)
        for notification in notifications:
            ok = False
            if sender is not None:
                ok = sender("", notification_payload(notification))
            elif webhook_url:
                ok = send_discord(webhook_url, notification_payload(notification))
            else:
                print(
                    f"[freshness] webhook未設定のため通知をスキップ: {notification.title}",
                    file=sys.stderr,
                )
            sent.append(
                {"target": notification.target, "severity": notification.severity, "sent": ok}
            )
            if ok:
                state = new_states[notification.target]
                state.last_notified_status = (
                    notification.severity if notification.severity != "recovery" else STATUS_OK
                )
                state.last_notified_at = now.isoformat()

    report: dict[str, object] = {
        "monitor_timestamp": now.isoformat(),
        "host": socket.gethostname(),
        "targets": [result.to_dict() for result in results],
        "notifications": sent,
        "overall": max(
            (result.status for result in results),
            key=lambda status: STATUS_ORDER[status],
            default=STATUS_OK,
        ),
    }
    if notify:
        atomic_write_json(
            state_path,
            {
                "updated_at": now.isoformat(),
                "targets": {k: v.to_dict() for k, v in new_states.items()},
            },
        )
    atomic_write_json(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="学習データ鮮度監視")
    parser.add_argument("--root", default=".", help="fx-codexリポジトリのルート")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state", default=DEFAULT_STATE_PATH)
    parser.add_argument("--report", default=DEFAULT_REPORT_PATH)
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="通知せず判定とレポートだけ更新する（通知状態は変更しない）",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        report = run_monitor(
            root,
            root / args.config,
            root / args.state,
            root / args.report,
            notify=not args.no_notify,
        )
    except Exception as exc:  # noqa: BLE001 - 設定破損等は明示メッセージでexit 1
        print(f"[freshness] 監視の実行に失敗: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
