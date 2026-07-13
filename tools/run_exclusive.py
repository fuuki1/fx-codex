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
- 子コマンドは専用process groupで起動する。SIGTERM/SIGINTは孫プロセスも
  含むgroup全体へ転送し、猶予時間後も残る場合はSIGKILLへエスカレートする。
  groupが消滅するまでflockは解放しない。

使用例(launchdのProgramArgumentsから):
    python3 tools/run_exclusive.py --name fx-snapshot --locks-dir logs/locks \
        -- .venv/bin/python fx_tf_snapshot.py
"""

from __future__ import annotations

import argparse
from datetime import datetime, UTC
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from types import FrameType
from typing import IO

DEFAULT_LOCKS_DIR = "logs/locks"
BUSY_EXIT_CODE_DEFAULT = 0
DEFAULT_TERMINATION_GRACE_SECONDS = 10.0
PROCESS_GROUP_POLL_SECONDS = 0.05


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
        except OSError as error:
            handle.close()
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                return False
            # Unsupported locking, I/O errors, and descriptor failures are not
            # evidence of another writer.  Propagate them so launchd records a
            # failed safety boundary instead of a successful busy skip.
            raise
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
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
) -> int:
    """ロック下でコマンドを実行し、子の終了コードを返す。取得失敗はbusy_exit_code。"""
    if termination_grace_seconds <= 0:
        raise ValueError("termination_grace_seconds must be positive")
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

        # handlerをspawn前に設定し、spawn中のTERM/INTは一時保留する。
        # signal maskをblockしたままforkすると、子がexec後もそのmaskを
        # 継承してTERMを受信できないため、pthread_sigmaskは使わない。
        process_group_id: int | None = None
        pending_signals: list[int] = []
        termination_deadline: float | None = None
        force_kill_sent = False

        def _forward(signum: int, _frame: FrameType | None) -> None:
            nonlocal termination_deadline
            # signal handler内ではgroupへの転送とdeadline設定だけ行い、
            # 待機とSIGKILL escalationは下のメインループで行う。
            if termination_deadline is None:
                termination_deadline = time.monotonic() + termination_grace_seconds
            if process_group_id is None:
                pending_signals.append(signum)
                return
            _signal_process_group(process_group_id, signum)

        previous_term = signal.signal(signal.SIGTERM, _forward)
        previous_int = signal.signal(signal.SIGINT, _forward)
        try:
            # shell wrapper配下のPython等も一括停止できるよう、子を新しい
            # session/process groupのleaderにする。このgroupが消えるまでlockを保持する。
            child = subprocess.Popen(command, start_new_session=True)
        except BaseException:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
            # spawn中に終了シグナルを受けた場合は、元のハンドラを
            # 復元後に再送し、通常の終了意味を握りつぶさない。
            if pending_signals:
                os.kill(os.getpid(), pending_signals[-1])
            raise
        process_group_id = child.pid
        for pending_signum in pending_signals:
            _signal_process_group(process_group_id, pending_signum)
        pending_signals.clear()

        try:
            while True:
                returncode = child.poll()
                group_alive = _process_group_exists(process_group_id)
                if returncode is not None and not group_alive:
                    return returncode

                if (
                    termination_deadline is not None
                    and not force_kill_sent
                    and time.monotonic() >= termination_deadline
                ):
                    if group_alive:
                        print(
                            f"[run_exclusive] {name}: process groupがTERM後も残存; "
                            "SIGKILLへエスカレート",
                            file=sys.stderr,
                        )
                        _signal_process_group(process_group_id, signal.SIGKILL)
                    force_kill_sent = True

                # group leaderが先に終了しても、孫プロセスが残る間は
                # lockを解放しない。SIGKILL後もkernelの終了処理を待つ。
                time.sleep(PROCESS_GROUP_POLL_SECONDS)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)


def _signal_process_group(process_group_id: int, signum: int) -> None:
    """Best-effort signal delivery to the complete child process group."""
    try:
        os.killpg(process_group_id, signum)
    except ProcessLookupError:
        pass


def _process_group_exists(process_group_id: int) -> bool:
    """Return whether any process still belongs to ``process_group_id``."""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 同一userの子groupでは通常起きないが、不明を終了扱いしない。
        return True
    return True


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
    parser.add_argument(
        "--termination-grace-seconds",
        type=float,
        default=DEFAULT_TERMINATION_GRACE_SECONDS,
        help="TERM/INT転送後にprocess groupを待つ秒数。超過後はSIGKILL",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- の後に実行コマンド")
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("実行コマンドを -- の後に指定してください")
    return run_locked(
        args.name,
        command,
        args.locks_dir,
        args.busy_exit_code,
        args.termination_grace_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
