"""Daily continuous-operation report: qualifying-day fields must be recomputed
from durable artifacts and fail honestly when the day was down or tampered."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from data_platform.collect.raw_first import QuoteLog
from data_platform.collect.truefx import poll_once
from data_platform.raw.immutable_store import ImmutableRawStore
from tools.data_platform_daily_report import build_report


def _truefx_payload(ms: int) -> bytes:
    return (
        f"USD/JPY,{ms},162.,211,162.,217,162.044,162.507,162.352\n"
        f"EUR/USD,{ms},1.13,955,1.13,959,1.13770,1.14061,1.13843\n"
    ).encode()


def _collect_live_day(root: Path) -> str:
    store = ImmutableRawStore(root / "raw")
    log = QuoteLog(root / "log")
    for offset in (3.0, 2.0):
        stamp = int((datetime.now(UTC) - timedelta(seconds=offset)).timestamp() * 1000)
        poll_once(
            fetcher=lambda _u, s=stamp: (200, _truefx_payload(s)),
            store=store,
            log=log,
            instruments=["USD_JPY"],
            connection_id="test",
        )
    return datetime.now(UTC).date().isoformat()


def _write_mirror_manifest(root: Path, *, day_offset: int = 0) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=day_offset)).isoformat()
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.jsonl").write_text(
        json.dumps(
            {
                "source": "dukascopy-m1",
                "status": "fetched",
                "sha256": "ab" * 32,
                "fetched_at": stamp,
                "path": "x",
            }
        )
        + "\n"
    )


class TestDailyReport:
    def test_qualifying_day_all_green(self, tmp_path: Path) -> None:
        day = _collect_live_day(tmp_path / "live")
        _write_mirror_manifest(tmp_path / "mirror")
        report = build_report(
            day=day,
            live_root=tmp_path / "live",
            mirror_root=tmp_path / "mirror",
            primary_provider="truefx",
            secondary_source_prefix="dukascopy",
            instruments=["USD_JPY"],
        )
        assert report["primary_up"] is True
        assert report["secondary_up"] is True
        assert report["raw_hash_verified"] is True
        assert report["replay_ok"] is True
        assert report["critical_incidents"] == 0
        assert report["accepted_quotes"] == 2

    def test_down_day_is_reported_down_not_skipped(self, tmp_path: Path) -> None:
        _write_mirror_manifest(tmp_path / "mirror", day_offset=1)  # fetched yesterday
        report = build_report(
            day=datetime.now(UTC).date().isoformat(),
            live_root=tmp_path / "live",  # nothing collected
            mirror_root=tmp_path / "mirror",
            primary_provider="truefx",
            secondary_source_prefix="dukascopy",
            instruments=["USD_JPY"],
        )
        assert report["primary_up"] is False
        assert report["secondary_up"] is False
        assert report["raw_hash_verified"] is False  # nothing verifiable, not assumed

    def test_tampered_raw_blob_fails_verification(self, tmp_path: Path) -> None:
        day = _collect_live_day(tmp_path / "live")
        _write_mirror_manifest(tmp_path / "mirror")
        blobs = sorted((tmp_path / "live" / "raw").rglob("*"))
        victim = next(path for path in blobs if path.is_file())
        victim.write_bytes(b"tampered")
        report = build_report(
            day=day,
            live_root=tmp_path / "live",
            mirror_root=tmp_path / "mirror",
            primary_provider="truefx",
            secondary_source_prefix="dukascopy",
            instruments=["USD_JPY"],
        )
        assert report["raw_hash_verified"] is False
        assert report["replay_ok"] is False
