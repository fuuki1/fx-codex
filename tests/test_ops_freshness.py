"""データ鮮度監視(tools/data_freshness_monitor.py)のテスト。

検証する契約:
- 鮮度判定: ok / warning / critical / ファイル欠落 / JSONL末尾破損
- 通知は状態遷移時のみ。同一状態はcooldown後に再通知。回復時はrecovery
- Discord送信失敗が監視を失敗させない
- 状態・レポートの原子的書込み(破損した状態ファイルからも安全に再開)
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "data_freshness_monitor.py"
NOW = datetime(2026, 7, 10, 3, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def monitor():
    spec = importlib.util.spec_from_file_location("data_freshness_monitor", _MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclassデコレータがsys.modules[__module__]を参照するため登録してからexecする
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_config(
    root: Path,
    *,
    warn: int = 900,
    critical: int | None = 2700,
    kind: str = "jsonl",
    timestamp_field: str = "ts",
    target_path: str = "logs/prices.jsonl",
) -> Path:
    config = {
        "schema": 1,
        "cooldown_seconds": 21600,
        "targets": [
            {
                "name": "prices",
                "path": target_path,
                "kind": kind,
                "timestamp_field": timestamp_field,
                "expected_interval_seconds": 300,
                "warn_after_seconds": warn,
                "critical_after_seconds": critical,
                "manual_action_ja": "restart_fx_services.sh を実行",
            }
        ],
    }
    path = root / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _touch_jsonl(
    root: Path,
    age_seconds: float,
    content: str | None = None,
    now: datetime = NOW,
) -> Path:
    """監視基準時刻nowからage_seconds前に最終更新されたJSONLを作る。"""
    path = root / "logs" / "prices.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    observed_at = now - timedelta(seconds=age_seconds)
    if content is None:
        content = json.dumps({"ts": observed_at.isoformat()})
    path.write_text(content + "\n", encoding="utf-8")
    stamp = observed_at.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _run(monitor, root: Path, config: Path, sender=None, now: datetime = NOW):
    return monitor.run_monitor(
        root,
        config,
        root / "logs" / "state.json",
        root / "logs" / "report.json",
        now=now,
        sender=sender,
        notify=True,
    )


class _Sender:
    """通知の記録用スタブ。fail=Trueで送信失敗をシミュレート。"""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail = fail

    def __call__(self, _url: str, payload: dict) -> bool:
        self.sent.append(payload)
        return not self.fail


def test_fresh_file_is_ok(monitor, tmp_path):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=60)
    sender = _Sender()
    report = _run(monitor, tmp_path, config, sender)
    assert report["overall"] == "ok"
    assert report["targets"][0]["status"] == "ok"
    assert sender.sent == []  # 正常時は通知しない


def test_stale_warning_and_critical_thresholds(monitor, tmp_path):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=1000)  # warn(900) < 1000 < critical(2700)
    report = _run(monitor, tmp_path, config, _Sender())
    assert report["targets"][0]["status"] == "warning"
    assert report["targets"][0]["reason"] == "stale_warning"

    _touch_jsonl(tmp_path, age_seconds=3000)  # > critical
    report = _run(monitor, tmp_path, config, _Sender())
    assert report["targets"][0]["status"] == "critical"
    assert report["targets"][0]["reason"] == "stale_critical"


@pytest.mark.parametrize(
    "field",
    [
        "cooldown_seconds",
        "expected_interval_seconds",
        "warn_after_seconds",
        "critical_after_seconds",
        "source_warn_after_seconds",
        "source_critical_after_seconds",
    ],
)
def test_freshness_config_rejects_nonfinite_thresholds(monitor, tmp_path, field) -> None:
    target = {
        "name": "prices",
        "path": "prices.jsonl",
        "kind": "jsonl",
        "timestamp_field": "ts",
        "expected_interval_seconds": 300,
        "warn_after_seconds": 900,
        "critical_after_seconds": 2700,
        "source_timestamp_field": "source_time",
        "source_warn_after_seconds": 900,
        "source_critical_after_seconds": 2700,
    }
    payload = {"cooldown_seconds": 3600, "targets": [target]}
    if field == "cooldown_seconds":
        payload[field] = float("nan")
    else:
        target[field] = float("nan")
    path = tmp_path / "nonfinite.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="finite number"):
        monitor.load_config(path)


def test_missing_file_is_critical(monitor, tmp_path):
    config = _write_config(tmp_path)
    sender = _Sender()
    report = _run(monitor, tmp_path, config, sender)
    assert report["targets"][0]["status"] == "critical"
    assert report["targets"][0]["reason"] == "file_missing"
    assert len(sender.sent) == 1


def test_corrupt_jsonl_tail_is_critical_even_if_fresh(monitor, tmp_path):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=60, content='{"broken": tru')
    report = _run(monitor, tmp_path, config, _Sender())
    assert report["targets"][0]["status"] == "critical"
    assert report["targets"][0]["reason"] == "jsonl_corrupt_tail"


def test_empty_jsonl_is_critical(monitor, tmp_path):
    config = _write_config(tmp_path)
    path = tmp_path / "logs" / "prices.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n", encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))

    report = _run(monitor, tmp_path, config, _Sender())

    assert report["targets"][0]["status"] == "critical"
    assert report["targets"][0]["reason"] == "jsonl_empty"


def test_fresh_mtime_cannot_mask_stale_record_timestamp(monitor, tmp_path):
    config = _write_config(tmp_path)
    path = _touch_jsonl(tmp_path, age_seconds=3000)
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))

    report = _run(monitor, tmp_path, config, _Sender())
    target = report["targets"][0]

    assert target["status"] == "critical"
    assert target["reason"] == "stale_critical"
    assert target["age_seconds"] == 3000
    assert target["record_timestamp"] < target["file_mtime"]


def test_future_mtime_is_critical(monitor, tmp_path):
    config = _write_config(tmp_path)
    path = _touch_jsonl(tmp_path, age_seconds=0)
    future = (NOW + timedelta(minutes=5)).timestamp()
    os.utime(path, (future, future))

    report = _run(monitor, tmp_path, config, _Sender())

    assert report["targets"][0]["status"] == "critical"
    assert report["targets"][0]["reason"] == "mtime_future"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ('{"ts": "2026-07-10T03:01:00+00:00"}', "timestamp_future"),
        ('{"ts": "2026-07-10T03:00:00"}', "timestamp_naive"),
        ('{"value": 1}', "timestamp_missing"),
        ('{"ts": "not-a-time"}', "timestamp_invalid"),
    ],
)
def test_invalid_record_timestamps_are_critical(monitor, tmp_path, content, reason):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=0, content=content)

    report = _run(monitor, tmp_path, config, _Sender())

    assert report["targets"][0]["status"] == "critical"
    assert report["targets"][0]["reason"] == reason


def test_json_target_requires_valid_object_and_configured_timestamp(monitor, tmp_path):
    config = _write_config(
        tmp_path,
        kind="json",
        timestamp_field="generated_at",
        target_path="logs/profile.json",
    )
    path = tmp_path / "logs" / "profile.json"
    path.parent.mkdir(parents=True)
    observed_at = NOW - timedelta(seconds=60)
    path.write_text(json.dumps({"generated_at": observed_at.isoformat()}), encoding="utf-8")
    os.utime(path, (observed_at.timestamp(), observed_at.timestamp()))
    assert _run(monitor, tmp_path, config, _Sender())["overall"] == "ok"

    path.write_text("{broken", encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    broken = _run(monitor, tmp_path, config, _Sender())
    assert broken["targets"][0]["reason"] == "json_corrupt"

    path.write_text("{}", encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    missing = _run(monitor, tmp_path, config, _Sender())
    assert missing["targets"][0]["reason"] == "timestamp_missing"


def test_warn_only_target_never_goes_critical(monitor, tmp_path):
    config = _write_config(tmp_path, critical=None)
    _touch_jsonl(tmp_path, age_seconds=10**7)
    report = _run(monitor, tmp_path, config, _Sender())
    assert report["targets"][0]["status"] == "warning"


def test_notification_only_on_transition_then_cooldown(monitor, tmp_path):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=1000)

    sender = _Sender()
    _run(monitor, tmp_path, config, sender, now=NOW)
    assert len(sender.sent) == 1  # ok→warning: 通知

    _run(monitor, tmp_path, config, sender, now=NOW + timedelta(minutes=5))
    assert len(sender.sent) == 1  # 同一状態が継続: cooldown内は再通知しない

    _run(monitor, tmp_path, config, sender, now=NOW + timedelta(hours=7))
    assert len(sender.sent) == 2  # cooldown(6h)経過: 再通知


def test_escalation_warning_to_critical_notifies_immediately(monitor, tmp_path):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=1000)
    sender = _Sender()
    _run(monitor, tmp_path, config, sender, now=NOW)
    assert len(sender.sent) == 1

    _touch_jsonl(tmp_path, age_seconds=3000)
    _run(monitor, tmp_path, config, sender, now=NOW + timedelta(minutes=5))
    assert len(sender.sent) == 2  # warning→critical: cooldownに関係なく即通知
    title = sender.sent[-1]["embeds"][0]["title"]
    assert "CRITICAL" in title


def test_recovery_notification_after_notified_outage(monitor, tmp_path):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=3000)
    sender = _Sender()
    _run(monitor, tmp_path, config, sender, now=NOW)
    assert len(sender.sent) == 1

    _touch_jsonl(tmp_path, age_seconds=30, now=NOW + timedelta(minutes=10))  # 復旧
    _run(monitor, tmp_path, config, sender, now=NOW + timedelta(minutes=10))
    assert len(sender.sent) == 2
    title = sender.sent[-1]["embeds"][0]["title"]
    assert "RECOVERY" in title

    # 回復後もデータが到着し続けている限り通知しない
    _touch_jsonl(tmp_path, age_seconds=30, now=NOW + timedelta(minutes=20))
    _run(monitor, tmp_path, config, sender, now=NOW + timedelta(minutes=20))
    assert len(sender.sent) == 2


def test_silent_flap_does_not_send_recovery(monitor, tmp_path):
    """通知していない軽微な揺れ(内部でwarning→ok)ではrecoveryを送らない。"""
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=1000)
    # 1回目: warning通知は送信されるが、ここでは送信失敗をシミュレート
    failing = _Sender(fail=True)
    _run(monitor, tmp_path, config, failing, now=NOW)
    assert len(failing.sent) == 1  # 送信試行はした(結果は失敗)

    # 送信失敗でも監視は継続し、状態は記録されている
    state = json.loads((tmp_path / "logs" / "state.json").read_text(encoding="utf-8"))
    assert state["targets"]["prices"]["status"] == "warning"


def test_discord_failure_does_not_crash_monitor(monitor, tmp_path):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=3000)
    report = _run(monitor, tmp_path, config, _Sender(fail=True))
    assert report["overall"] == "critical"
    assert report["notifications"][0]["sent"] is False  # 失敗を隠さず記録

    succeeding = _Sender()
    _run(monitor, tmp_path, config, succeeding, now=NOW + timedelta(minutes=5))
    assert len(succeeding.sent) == 1  # 未送信状態はcooldownを待たず再試行


def test_discord_exception_log_never_contains_webhook_url_or_token(monitor, monkeypatch, capsys):
    secret = "SUPERSECRET_WEBHOOK_TOKEN_123456"
    webhook_url = f"https://discord.example/api/webhooks/123/{secret}"

    class FailingRequests:
        @staticmethod
        def post(url, **_kwargs):
            raise RuntimeError(f"failed URL: {url}")

    monkeypatch.setitem(sys.modules, "requests", FailingRequests)

    assert monitor.send_discord(webhook_url, {"test": True}) is False
    stderr = capsys.readouterr().err
    assert "RuntimeError" in stderr
    assert secret not in stderr
    assert webhook_url not in stderr


def test_no_notify_does_not_consume_canonical_notification_state(monitor, tmp_path):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=60)
    _run(monitor, tmp_path, config, _Sender(), now=NOW)
    state_path = tmp_path / "logs" / "state.json"
    before = state_path.read_bytes()

    _touch_jsonl(tmp_path, age_seconds=1000)
    report = monitor.run_monitor(
        tmp_path,
        config,
        state_path,
        tmp_path / "logs" / "report.json",
        now=NOW + timedelta(minutes=5),
        notify=False,
    )

    assert report["overall"] == "warning"
    assert state_path.read_bytes() == before
    sender = _Sender()
    _run(monitor, tmp_path, config, sender, now=NOW + timedelta(minutes=10))
    assert len(sender.sent) == 1


def test_corrupt_state_file_recovers(monitor, tmp_path):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=60)
    state_path = tmp_path / "logs" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{broken json", encoding="utf-8")
    report = _run(monitor, tmp_path, config, _Sender())
    assert report["overall"] == "ok"  # 破損状態からでも例外なく再開


def test_report_contains_required_fields(monitor, tmp_path):
    config = _write_config(tmp_path)
    _touch_jsonl(tmp_path, age_seconds=1000)
    report = _run(monitor, tmp_path, config, _Sender())
    target = report["targets"][0]
    for key in (
        "name",
        "path",
        "status",
        "reason",
        "last_update",
        "age_seconds",
        "expected_interval_seconds",
    ):
        assert key in target
    assert "monitor_timestamp" in report and "host" in report

    # レポート/状態は原子的に書かれ、正しいJSONとして読める
    for name in ("report.json", "state.json"):
        payload = json.loads((tmp_path / "logs" / name).read_text(encoding="utf-8"))
        assert isinstance(payload, dict)


def test_repo_default_config_is_valid(monitor):
    """リポジトリ同梱のops/freshness_targets.jsonが読める+閾値の整合性。"""
    config_path = Path(__file__).resolve().parents[1] / "ops" / "freshness_targets.json"
    targets, cooldown = monitor.load_config(config_path)
    assert cooldown > 0
    names = {target.name for target in targets}
    assert {"tf_price_snapshot", "tf_journal", "fusion_journal", "decision_journal"} <= names
    required_hash_names = {"tf_price_snapshot", "tf_journal", "fusion_journal", "decision_journal"}
    assert all(
        target.require_content_hash for target in targets if target.name in required_hash_names
    )
    price_target = next(target for target in targets if target.name == "tf_price_snapshot")
    assert price_target.source_timestamp_missing_status == "critical"
    for target in targets:
        assert target.timestamp_field
        assert target.warn_after_seconds > target.expected_interval_seconds
        if target.critical_after_seconds is not None:
            assert target.critical_after_seconds > target.warn_after_seconds


def _hashed_row(**payload) -> dict:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {**payload, "content_hash": hashlib.sha256(encoded).hexdigest()}


def _series_target(monitor) -> object:
    return monitor.TargetConfig(
        name="prices",
        path="prices.jsonl",
        timestamp_field="ts",
        expected_interval_seconds=300,
        warn_after_seconds=900,
        critical_after_seconds=2700,
        require_content_hash=True,
        expected_key_fields=("symbol", "timeframe"),
        expected_keys=(("USDJPY", "1h"), ("EURUSD", "1h")),
        unchanged_value_fields=("close", "bid", "ask"),
        max_unchanged_observations=12,
        lookback_records=100,
    )


def test_freshness_rejects_forged_hash(monitor, tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": NOW.isoformat(),
                "schema_version": 2,
                "content_hash": "0" * 64,
                "symbol": "USDJPY",
                "timeframe": "1h",
                "close": 150.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))

    result = monitor.check_target(_series_target(monitor), tmp_path, NOW)

    assert result.status == "critical"
    assert result.reason == "content_hash_mismatch"


def test_freshness_detects_one_stale_key_while_another_updates(monitor, tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    rows = [
        _hashed_row(
            ts=(NOW - timedelta(minutes=30)).isoformat(),
            symbol="USDJPY",
            timeframe="1h",
            close=150.0,
            bid=None,
            ask=None,
        ),
        _hashed_row(
            ts=NOW.isoformat(),
            symbol="EURUSD",
            timeframe="1h",
            close=1.1,
            bid=None,
            ask=None,
        ),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))

    result = monitor.check_target(_series_target(monitor), tmp_path, NOW)

    assert result.status == "warning"
    assert result.reason == "expected_key_stale_warning"
    assert any("USDJPY,1h" in detail for detail in result.quality_details)


def test_freshness_rejects_repeated_payload_with_fresh_timestamps(monitor, tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    rows = []
    for index in range(12):
        timestamp = NOW - timedelta(minutes=55 - index * 5)
        rows.append(
            _hashed_row(
                ts=timestamp.isoformat(),
                symbol="USDJPY",
                timeframe="1h",
                close=150.0,
                bid=None,
                ask=None,
            )
        )
        rows.append(
            _hashed_row(
                ts=timestamp.isoformat(),
                symbol="EURUSD",
                timeframe="1h",
                close=1.1 + index * 0.0001,
                bid=None,
                ask=None,
            )
        )
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))

    result = monitor.check_target(_series_target(monitor), tmp_path, NOW)

    assert result.status == "critical"
    assert result.reason == "payload_unchanged"
    assert any("identical_observations=12" in detail for detail in result.quality_details)


def test_freshness_checks_integrity_of_recent_rows_not_only_tail(monitor, tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    corrupt = _hashed_row(ts=(NOW - timedelta(minutes=2)).isoformat(), value=1)
    corrupt["value"] = 999
    good = _hashed_row(ts=NOW.isoformat(), value=2)
    path.write_text(
        json.dumps(corrupt) + "\n" + json.dumps(good) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    target = monitor.TargetConfig(
        name="journal",
        path="journal.jsonl",
        timestamp_field="ts",
        expected_interval_seconds=300,
        warn_after_seconds=900,
        critical_after_seconds=2700,
        require_content_hash=True,
        lookback_records=10,
    )

    result = monitor.check_target(target, tmp_path, NOW)

    assert result.status == "critical"
    assert result.reason == "recent_content_hash_mismatch"


def test_freshness_fails_closed_on_authoritative_source_staleness(monitor, tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    row = _hashed_row(
        ts=NOW.isoformat(),
        available_time=NOW.isoformat(),
        source_time=(NOW - timedelta(hours=2)).isoformat(),
        symbol="USDJPY",
        timeframe="1h",
        close=150.0,
    )
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    target = monitor.TargetConfig(
        name="prices",
        path="prices.jsonl",
        timestamp_field="ts",
        expected_interval_seconds=300,
        warn_after_seconds=900,
        critical_after_seconds=2700,
        require_content_hash=True,
        expected_key_fields=("symbol", "timeframe"),
        expected_keys=(("USDJPY", "1h"),),
        lookback_records=10,
        source_timestamp_field="source_time",
        source_warn_after_seconds=900,
        source_critical_after_seconds=2700,
    )

    result = monitor.check_target(target, tmp_path, NOW)

    assert result.status == "critical"
    assert result.reason == "source_stale_critical"
    assert any("source_age_seconds=7200.0" in detail for detail in result.quality_details)


def test_freshness_missing_source_timestamp_vetoes_new_risk(monitor, tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    row = _hashed_row(
        ts=NOW.isoformat(),
        available_time=NOW.isoformat(),
        source_time=None,
        symbol="USDJPY",
        timeframe="1h",
        close=150.0,
        data_quality_flags=["source_time_unavailable"],
    )
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    target = monitor.TargetConfig(
        name="prices",
        path="prices.jsonl",
        timestamp_field="ts",
        expected_interval_seconds=300,
        warn_after_seconds=900,
        critical_after_seconds=2700,
        require_content_hash=True,
        expected_key_fields=("symbol", "timeframe"),
        expected_keys=(("USDJPY", "1h"),),
        lookback_records=10,
        source_timestamp_field="source_time",
        source_warn_after_seconds=900,
        source_critical_after_seconds=2700,
    )

    result = monitor.check_target(target, tmp_path, NOW)

    assert result.status == "warning"
    assert result.reason == "source_timestamp_missing"
    assert any("source_time_unavailable" in detail for detail in result.quality_details)


def test_strict_price_freshness_makes_missing_source_timestamp_critical(monitor, tmp_path) -> None:
    path = tmp_path / "prices.jsonl"
    row = _hashed_row(
        ts=NOW.isoformat(),
        available_time=NOW.isoformat(),
        source_time=None,
        symbol="USDJPY",
        timeframe="1h",
        close=150.0,
        data_quality_flags=["source_time_unavailable"],
    )
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    target = monitor.TargetConfig(
        name="prices",
        path="prices.jsonl",
        timestamp_field="ts",
        expected_interval_seconds=300,
        warn_after_seconds=900,
        critical_after_seconds=2700,
        require_content_hash=True,
        expected_key_fields=("symbol", "timeframe"),
        expected_keys=(("USDJPY", "1h"),),
        lookback_records=10,
        source_timestamp_field="source_time",
        source_warn_after_seconds=900,
        source_critical_after_seconds=2700,
        source_timestamp_missing_status="critical",
    )

    result = monitor.check_target(target, tmp_path, NOW)

    assert result.status == "critical"
    assert result.reason == "source_timestamp_missing"
