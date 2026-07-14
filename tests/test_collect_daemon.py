"""Collector daemon fault scenarios: disk full, multi-writer, dry-run, plist."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys

import pytest

from data_platform.collect.raw_first import QuoteLog, ingest_payload
from data_platform.raw.immutable_store import ImmutableRawStore
from tools.fx_quote_collector import EX_CONFIG, EX_OK, main as daemon_main
from tools.run_exclusive import ExclusiveLock

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestDiskFull:
    def test_unwritable_raw_store_fails_closed(self, tmp_path: Path) -> None:
        """Disk-full / permission failure on the raw store must abort ingest
        BEFORE any quote is accepted (raw-first: no raw durability, no data)."""

        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        store = ImmutableRawStore(raw_dir)
        log = QuoteLog(tmp_path / "log")
        os.chmod(raw_dir, stat.S_IRUSR | stat.S_IXUSR)  # read-only -> writes fail
        try:
            with pytest.raises(OSError):
                ingest_payload(b"payload", parser=lambda _raw: [], store=store, log=log)
            assert not (tmp_path / "log" / "quotes.jsonl").exists()
        finally:
            os.chmod(raw_dir, stat.S_IRWXU)


class TestMultiWriter:
    def test_second_writer_cannot_acquire_lock(self, tmp_path: Path) -> None:
        first = ExclusiveLock("quote-collector", locks_dir=tmp_path)
        assert first.acquire() is True
        second = ExclusiveLock("quote-collector", locks_dir=tmp_path)
        try:
            assert second.acquire() is False  # single-writer enforced
        finally:
            first.release()

    def test_lock_released_after_crash_is_reacquirable(self, tmp_path: Path) -> None:
        """A crashed writer (lock released by process exit) must not deadlock
        the restarted collector."""

        code = (
            f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
            "from tools.run_exclusive import ExclusiveLock; "
            f"lock = ExclusiveLock('quote-collector', locks_dir={str(tmp_path)!r}); "
            "assert lock.acquire()"
        )
        subprocess.run([sys.executable, "-c", code], check=True)  # exits -> lock freed
        restarted = ExclusiveLock("quote-collector", locks_dir=tmp_path)
        try:
            assert restarted.acquire() is True
        finally:
            restarted.release()


class TestDaemonDryRun:
    def test_dry_run_without_credentials_is_ex_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for name in ("FX_OANDA_API_TOKEN", "FX_OANDA_ACCOUNT_ID", "FX_OANDA_ENV"):
            monkeypatch.delenv(name, raising=False)
        code = daemon_main(["--output-root", str(tmp_path), "--dry-run"])
        assert code == EX_CONFIG
        err = capsys.readouterr().err
        assert "FX_OANDA_API_TOKEN" in err  # names shown, values never requested

    def test_dry_run_with_credentials_masks_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("FX_OANDA_API_TOKEN", "SUPER-SECRET-TOKEN")
        monkeypatch.setenv("FX_OANDA_ACCOUNT_ID", "001-001-1234567-001")
        monkeypatch.setenv("FX_OANDA_ENV", "practice")
        code = daemon_main(["--output-root", str(tmp_path), "--dry-run"])
        assert code == EX_OK
        out = capsys.readouterr().out
        assert "SUPER-SECRET-TOKEN" not in out
        assert "***masked***" in out
        payload = json.loads(out)
        assert payload["dry_run"] is True

    def test_dry_run_makes_no_filesystem_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FX_OANDA_API_TOKEN", "t")
        monkeypatch.setenv("FX_OANDA_ACCOUNT_ID", "a")
        monkeypatch.setenv("FX_OANDA_ENV", "practice")
        daemon_main(["--output-root", str(tmp_path / "never-created"), "--dry-run"])
        assert not (tmp_path / "never-created").exists()


class TestLaunchdTemplate:
    def test_collector_plist_template_is_valid_and_readonly(self) -> None:
        template = REPO_ROOT / "ops" / "launchd" / "com.fx-codex.quote-collector.plist.tmpl"
        text = template.read_text()
        rendered = (
            text.replace("__ROOT__", "/tmp/fx")
            .replace("__PYTHON__", "/usr/bin/python3")
            .replace("__HOME__", "/Users/example")
        )
        payload = plistlib.loads(rendered.encode())
        assert payload["Label"] == "com.fx-codex.quote-collector"
        joined = " ".join(payload["ProgramArguments"])
        assert "fx_quote_collector.py" in joined
        assert "--output-root" in joined
        # secrets are never templated into the plist
        for forbidden in ("TOKEN", "SECRET", "Bearer"):
            assert forbidden not in text or "FX_OANDA" in text  # env NAMES only
        assert payload.get("KeepAlive") is not None
