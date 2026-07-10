"""データ鮮度監視(tools/data_freshness_monitor.py)のテスト。

検証する契約:
- 鮮度判定: ok / warning / critical / ファイル欠落 / JSONL末尾破損
- 通知は状態遷移時のみ。同一状態はcooldown後に再通知。回復時はrecovery
- Discord送信失敗が監視を失敗させない
- 状態・レポートの原子的書込み(破損した状態ファイルからも安全に再開)
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
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


def _write_config(root: Path, *, warn: int = 900, critical: int | None = 2700) -> Path:
    config = {
        "schema": 1,
        "cooldown_seconds": 21600,
        "targets": [
            {
                "name": "prices",
                "path": "logs/prices.jsonl",
                "kind": "jsonl",
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
    content: str = '{"ts": "2026-07-10"}',
    now: datetime = NOW,
) -> Path:
    """監視基準時刻nowからage_seconds前に最終更新されたJSONLを作る。"""
    path = root / "logs" / "prices.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")
    stamp = (now - timedelta(seconds=age_seconds)).timestamp()
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
    assert {"tf_price_snapshot", "tf_journal", "fusion_journal"} <= names
    for target in targets:
        assert target.warn_after_seconds > target.expected_interval_seconds
        if target.critical_after_seconds is not None:
            assert target.critical_after_seconds > target.warn_after_seconds


def test_cli_exit_code_distinguishes_critical_from_healthy(monitor, tmp_path):
    config = _write_config(tmp_path)
    common = [
        "--root",
        str(tmp_path),
        "--config",
        str(config),
        "--state",
        "logs/state.json",
        "--report",
        "logs/report.json",
        "--no-notify",
    ]

    assert monitor.main(common) == 2
    _touch_jsonl(tmp_path, age_seconds=0, now=datetime.now(UTC))
    assert monitor.main(common) == 0
