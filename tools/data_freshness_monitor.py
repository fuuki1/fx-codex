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
- 欠損は隠さない: 監視レポートに age_seconds / last_ok / 遷移履歴を必ず残す
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, UTC
import json
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
    kind: str = "jsonl"
    expected_interval_seconds: float = 3600.0
    warn_after_seconds: float = 7200.0
    critical_after_seconds: float | None = 21600.0
    manual_action_ja: str = ""


@dataclass
class TargetResult:
    """1対象の監視結果。JSONレポートの1行に対応。"""

    name: str
    path: str
    status: str = STATUS_OK
    reason: str = ""
    last_update: str | None = None
    age_seconds: float | None = None
    expected_interval_seconds: float = 0.0
    warn_after_seconds: float = 0.0
    critical_after_seconds: float | None = None
    manual_action_ja: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "status": self.status,
            "reason": self.reason,
            "last_update": self.last_update,
            "age_seconds": self.age_seconds,
            "expected_interval_seconds": self.expected_interval_seconds,
            "warn_after_seconds": self.warn_after_seconds,
            "critical_after_seconds": self.critical_after_seconds,
            "manual_action_ja": self.manual_action_ja,
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
    return int(value) if isinstance(value, (int, float)) else 0


def load_config(path: str | Path) -> tuple[list[TargetConfig], float]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cooldown = float(payload.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
    targets: list[TargetConfig] = []
    for row in payload.get("targets", []):
        critical_raw = row.get("critical_after_seconds")
        targets.append(
            TargetConfig(
                name=str(row["name"]),
                path=str(row["path"]),
                kind=str(row.get("kind", "jsonl")),
                expected_interval_seconds=float(row.get("expected_interval_seconds", 3600)),
                warn_after_seconds=float(row.get("warn_after_seconds", 7200)),
                critical_after_seconds=(float(critical_raw) if critical_raw is not None else None),
                manual_action_ja=str(row.get("manual_action_ja", "")),
            )
        )
    return targets, cooldown


def check_target(target: TargetConfig, root: Path, now: datetime) -> TargetResult:
    """1対象の存在・鮮度・末尾破損をチェックする(通知はしない)。"""
    result = TargetResult(
        name=target.name,
        path=target.path,
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

    mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=UTC)
    age = (now - mtime).total_seconds()
    result.last_update = mtime.isoformat()
    result.age_seconds = round(age, 1)

    if target.critical_after_seconds is not None and age > target.critical_after_seconds:
        result.status = STATUS_CRITICAL
        result.reason = "stale_critical"
    elif age > target.warn_after_seconds:
        result.status = STATUS_WARNING
        result.reason = "stale_warning"

    # JSONLの末尾行が壊れていたら書込み途中クラッシュや破損の兆候(鮮度より優先)
    if target.kind == "jsonl":
        tail = _read_last_nonempty_line(file_path)
        if tail is not None:
            try:
                json.loads(tail)
            except json.JSONDecodeError:
                result.status = STATUS_CRITICAL
                result.reason = "jsonl_corrupt_tail"
    return result


def _read_last_nonempty_line(path: Path, chunk: int = 65536) -> str | None:
    """ファイル全体を読まずに末尾の非空行を返す(ジャーナルは数MBに育つため)。"""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - chunk))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line for line in data.splitlines() if line.strip()]
    return lines[-1] if lines else None


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
        print(f"[freshness] Discord送信失敗: {exc}", file=sys.stderr)
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
