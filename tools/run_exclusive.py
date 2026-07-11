"""排他ロック付きでコマンドを1回実行するランナー。

学習データ収集ジョブ(fx_tf_snapshot / fx_briefing)の二重起動を防ぐ。
Mac mini実機で「手動loop×3 + launchd + cron」が同時稼働してジャーナルを
重複汚染していた事故(2026-07-10監査)の再発防止が目的。

設計:
- ロックは fcntl.flock(LOCK_EX | LOCK_NB)。プロセス終了(クラッシュ・SIGKILL含む)で
  カーネルが自動解放するため、stale PIDファイルの手動掃除が不要。
  macOSのローカルAPFS/HFS+で確実に動作する(NFS等のネットワークFSは非対象)。
- ロックファイルにはpid/host/開始時刻をJSONで記録する。これは診断用の
  メタデータであり、排他判定には使わない(判定はflockのみ)。
- ロック取得失敗(=先行プロセスが実行中)は既定で exit 0。launchdのStartIntervalで
  定期起動される前提のため、「スキップ」は正常系でありエラー扱いにしない
  (--busy-exit-code で変更可能)。
- SIGTERM/SIGINT は子プロセスへ転送し、子の終了を待ってから同じコードで終了する
  (launchctl bootout / kickstart -k での停止を正常終了扱いにするため)。

使用例(launchdのProgramArgumentsから):
    python3 tools/run_exclusive.py --name fx-snapshot --locks-dir logs/locks \
        -- .venv/bin/python fx_tf_snapshot.py
"""

from __future__ import annotations

import argparse
from datetime import datetime, UTC
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from types import FrameType
from typing import IO

DEFAULT_LOCKS_DIR = "logs/locks"
BUSY_EXIT_CODE_DEFAULT = 0


class ExclusiveLock:
    """flockベースの排他ロック。with文で使い、プロセス終了時は自動解放。"""

    def __init__(self, name: str, locks_dir: str | Path = DEFAULT_LOCKS_DIR) -> None:
        self.name = name
        self.path = Path(locks_dir) / f"{name}.lock"
        self._handle: IO[str] | None = None

    def acquire(self) -> bool:
        """ロックを取得できたらTrue。先行プロセスが保持中ならFalse(待たない)。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        # 診断メタデータ(排他判定には使わない。判定はflockが唯一の真実)
        handle.seek(0)
        handle.truncate()
        json.dump(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "acquired_at": datetime.now(UTC).isoformat(),
                "name": self.name,
            },
            handle,
        )
        handle.flush()
        self._handle = handle
        return True

    def holder_info(self) -> dict[str, object]:
        """先行保持者の診断メタデータ(読めなければ空)。"""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> ExclusiveLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def run_locked(
    name: str,
    command: list[str],
    locks_dir: str | Path = DEFAULT_LOCKS_DIR,
    busy_exit_code: int = BUSY_EXIT_CODE_DEFAULT,
) -> int:
    """ロック下でコマンドを実行し、子の終了コードを返す。取得失敗はbusy_exit_code。"""
    with ExclusiveLock(name, locks_dir) as lock:
        if not lock.acquire():
            holder = lock.holder_info()
            print(
                f"[run_exclusive] {name}: 先行プロセスが実行中のためスキップ "
                f"(holder={holder.get('pid', '?')}@{holder.get('host', '?')} "
                f"since {holder.get('acquired_at', '?')})",
                file=sys.stderr,
            )
            return busy_exit_code

        child = subprocess.Popen(command)

        def _forward(signum: int, _frame: FrameType | None) -> None:
            # launchdからの停止(SIGTERM)やCtrl-Cを子へ転送し、子の後始末を待つ
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

        previous_term = signal.signal(signal.SIGTERM, _forward)
        previous_int = signal.signal(signal.SIGINT, _forward)
        try:
            return child.wait()
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="排他ロック付きコマンド実行")
    parser.add_argument("--name", required=True, help="ロック名(ジョブ識別子)")
    parser.add_argument("--locks-dir", default=DEFAULT_LOCKS_DIR)
    parser.add_argument(
        "--busy-exit-code",
        type=int,
        default=BUSY_EXIT_CODE_DEFAULT,
        help="ロック取得失敗(先行実行中)時の終了コード。既定0=正常スキップ",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- の後に実行コマンド")
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("実行コマンドを -- の後に指定してください")
    return run_locked(args.name, command, args.locks_dir, args.busy_exit_code)


if __name__ == "__main__":
    sys.exit(main())
