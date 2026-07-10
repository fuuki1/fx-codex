"""排他ロック(tools/run_exclusive.py)・plistテンプレート・
インストールスクリプトdry-run・ジャーナル監査(tools/journal_gap_audit.py)のテスト。
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import time

# XMLパースはリポジトリ管理下のplistテンプレート(信頼済み・非外部入力)の検証のみに使う。
# defusedxmlは導入しない(依存追加禁止方針)。外部由来XMLをここで扱ってはいけない。
import xml.etree.ElementTree as ET  # noqa: S314

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclassデコレータがsys.modules[__module__]を参照するため登録してからexecする
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _start_lock_holder(tmp_path: Path, name: str = "job") -> subprocess.Popen:
    """ロックを確実に保持した状態の別プロセスを起動して返す。

    run_exclusiveはロック取得後にのみ子コマンドを実行するので、
    子が最初に書くreadyファイルの出現=ロック保持済みの合図になる。
    (単純なsleep+ポーリングだとテスト側が先にロックを取ってしまい、
    ホルダー側がスキップされるレースがある)
    """
    ready = tmp_path / "holder_ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            str(_ROOT / "tools" / "run_exclusive.py"),
            "--name",
            name,
            "--locks-dir",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            f"import pathlib, time; pathlib.Path({str(ready)!r}).write_text('1'); time.sleep(60)",
        ]
    )
    deadline = datetime.now(UTC) + timedelta(seconds=15)
    while not ready.exists():
        if datetime.now(UTC) > deadline or holder.poll() is not None:
            holder.terminate()
            raise AssertionError("ロック保持プロセスが時間内に起動しなかった")
        time.sleep(0.05)
    return holder


@pytest.fixture(scope="module")
def run_exclusive():
    return _load("run_exclusive")


@pytest.fixture(scope="module")
def gap_audit():
    return _load("journal_gap_audit")


# ---------------------------------------------------------------- 排他ロック


def test_lock_acquire_and_release(run_exclusive, tmp_path):
    lock = run_exclusive.ExclusiveLock("job", tmp_path)
    assert lock.acquire() is True
    info = lock.holder_info()
    assert info["name"] == "job" and isinstance(info["pid"], int)
    lock.release()
    # 解放後は再取得できる
    second = run_exclusive.ExclusiveLock("job", tmp_path)
    assert second.acquire() is True
    second.release()


def test_lock_blocks_second_holder_in_other_process(run_exclusive, tmp_path):
    """本物の別プロセスがロックを保持している間は取得できない。"""
    holder = _start_lock_holder(tmp_path)
    try:
        lock = run_exclusive.ExclusiveLock("job", tmp_path)
        assert lock.acquire() is False, "先行プロセス保持中に取得できてはいけない"
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    # 先行プロセス終了後(SIGTERM転送→子終了)はロックが解放されている
    lock = run_exclusive.ExclusiveLock("job", tmp_path)
    assert lock.acquire() is True
    lock.release()


def test_stale_lock_from_dead_process_is_reacquirable(run_exclusive, tmp_path):
    """異常終了(kill -9相当)したプロセスのロックはカーネルが解放する。"""
    victim = _start_lock_holder(tmp_path)
    probe = run_exclusive.ExclusiveLock("job", tmp_path)
    assert probe.acquire() is False  # 保持中は取れない
    victim.kill()  # SIGKILL: 後始末コードは一切走らない
    victim.wait(timeout=10)
    lock = run_exclusive.ExclusiveLock("job", tmp_path)
    assert lock.acquire() is True, "SIGKILL後もロックが残るならstale処理が壊れている"
    # ロックファイル自体は残っていてよい(メタデータ)が、排他は解けている
    assert lock.path.exists()
    lock.release()


def test_run_locked_returns_child_exit_code(run_exclusive, tmp_path):
    code = run_exclusive.run_locked("job", [sys.executable, "-c", "raise SystemExit(7)"], tmp_path)
    assert code == 7


def test_run_locked_busy_exit_code(run_exclusive, tmp_path):
    holder = _start_lock_holder(tmp_path)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(_ROOT / "tools" / "run_exclusive.py"),
                "--name",
                "job",
                "--locks-dir",
                str(tmp_path),
                "--busy-exit-code",
                "99",
                "--",
                sys.executable,
                "-c",
                "print('should not run')",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 99
        assert "スキップ" in result.stderr
        assert "should not run" not in result.stdout
    finally:
        holder.terminate()
        holder.wait(timeout=10)


# ------------------------------------------------------- plistテンプレート


PLIST_TEMPLATES = [
    "com.fx-codex.snapshot.plist.tmpl",
    "com.fx-codex.briefing.plist.tmpl",
    "com.fx-codex.health.plist.tmpl",
]


@pytest.mark.parametrize("template", PLIST_TEMPLATES)
def test_plist_template_renders_to_valid_xml(template, tmp_path):
    raw = (_ROOT / "ops" / "launchd" / template).read_text(encoding="utf-8")
    rendered = raw.replace("__FX_ROOT__", "/Users/example/srv/fx-codex").replace(
        "__PYTHON__", "/Users/example/srv/fx-codex/.venv/bin/python"
    )
    assert "__FX_ROOT__" not in rendered and "__PYTHON__" not in rendered
    root = ET.fromstring(rendered)  # 不正XMLならここで例外
    keys = [el.text for el in root.iter("key")]
    for required in ("Label", "ProgramArguments", "WorkingDirectory", "ProcessType"):
        assert required in keys, f"{template}: {required} がない"
    # 周期起動の定義がどちらかは必ずある
    assert "StartInterval" in keys or "StartCalendarInterval" in keys
    # 排他ロックランナー経由で起動している
    args = [el.text or "" for el in root.iter("string")]
    assert any("run_exclusive.py" in arg for arg in args)
    # 秘密情報をplistへ埋めていない
    assert "WEBHOOK" not in rendered and "API_KEY" not in rendered


_ZSH = shutil.which("zsh")  # CI(ubuntu)にはzshが無いためスキップ。macOS実機で検証する


@pytest.mark.skipif(_ZSH is None, reason="zshが必要(macOS運用環境向けスクリプト)")
def test_install_script_dry_run_makes_no_changes(tmp_path):
    """--dry-runはplist生成内容の表示だけで、LaunchAgentsやlaunchctlに触れない。"""
    result = subprocess.run(
        [_ZSH, str(_ROOT / "scripts" / "install_launchd.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout
    for label in ("com.fx-codex.snapshot", "com.fx-codex.briefing", "com.fx-codex.health"):
        assert label in result.stdout
    # 展開済みパスが含まれ、プレースホルダは残らない
    assert "__FX_ROOT__" not in result.stdout


@pytest.mark.skipif(_ZSH is None, reason="zshが必要(macOS運用環境向けスクリプト)")
def test_shell_scripts_parse(tmp_path):
    for script in (
        "install_launchd.sh",
        "uninstall_launchd.sh",
        "status_fx_services.sh",
        "restart_fx_services.sh",
        "fx_briefing_once.sh",
    ):
        result = subprocess.run(
            [_ZSH, "-n", str(_ROOT / "scripts" / script)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"


# ------------------------------------------------------- ジャーナル監査


def _journal_rows(base: datetime, hours: int, per_hour: int = 1) -> list[dict]:
    rows = []
    for hour in range(hours):
        for dup in range(per_hour):
            ts = base + timedelta(hours=hour, seconds=dup * 30)
            rows.append({"ts": ts.isoformat(), "symbol": "USDJPY", "timeframe": "1h"})
    return rows


def test_gap_audit_clean_journal(gap_audit):
    base = datetime(2026, 7, 6, 10, 10, tzinfo=UTC)
    report = gap_audit.audit_journal(_journal_rows(base, hours=24))
    assert report["duplicate_rows"] == 0
    assert report["gaps"] == []
    assert report["time_reversals"] == 0


def test_gap_audit_detects_duplicates(gap_audit):
    base = datetime(2026, 7, 6, 10, 10, tzinfo=UTC)
    report = gap_audit.audit_journal(_journal_rows(base, hours=24, per_hour=3))
    assert report["duplicate_rows"] == 48  # 各時間で2行が重複(3-1)×24
    assert report["duplicate_row_pct"] > 60


def test_gap_audit_detects_gap_and_records_period(gap_audit):
    base = datetime(2026, 7, 6, 10, 10, tzinfo=UTC)
    rows = _journal_rows(base, hours=3)
    rows += _journal_rows(base + timedelta(hours=50), hours=3)
    report = gap_audit.audit_journal(rows)
    assert len(report["gaps"]) == 1
    gap = report["gaps"][0]
    assert gap["gap_hours"] == pytest.approx(48.0, abs=0.1)
    # 欠損期間は開始・終了の絶対時刻で監査証跡に残る
    assert gap["gap_start"].startswith("2026-07-06T12:10")
    assert gap["gap_end"].startswith("2026-07-08T12:10")


def test_gap_audit_detects_time_reversal(gap_audit):
    base = datetime(2026, 7, 6, 10, 10, tzinfo=UTC)
    rows = _journal_rows(base, hours=3)
    rows.insert(
        1, {"ts": (base - timedelta(hours=5)).isoformat(), "symbol": "X", "timeframe": "1h"}
    )
    report = gap_audit.audit_journal(rows)
    assert report["time_reversals"] >= 1


def test_gap_audit_read_journal_skips_broken_lines(gap_audit, tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text(
        '{"ts": "2026-07-06T10:00:00+00:00", "symbol": "USDJPY"}\n'
        "{broken line\n"
        '{"ts": "2026-07-06T11:00:00+00:00", "symbol": "USDJPY"}\n',
        encoding="utf-8",
    )
    rows = gap_audit.read_journal(path)
    assert len(rows) == 2
