"""Operator-level ALFRED macro PIT capture evidence tests."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from data_platform.collect.fred_macro import MacroPITError, MacroPITLog, parse_vintage_csv
from data_platform.raw.immutable_store import ImmutableRawStore
from tools import fred_macro_capture

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 23, 1, 0, tzinfo=UTC)


def _append_same_observation(path: str) -> int:
    """Spawn-safe worker used to exercise the process-level JSONL lock."""

    vintage = NOW.date()
    raw = b"observation_date,CPIAUCSL_20260723\n2026-06-01,1.0\n"
    observations = parse_vintage_csv(
        raw,
        series_id="CPIAUCSL",
        vintage=vintage,
        source_uri="https://example.invalid/alfred.csv",
        received_at=NOW,
    )
    return MacroPITLog(path).append_new(observations)


def _csv_for_url(url: str, *, malformed: bool = False) -> bytes:
    query = parse_qs(urlparse(url).query)
    series = query["id"][0]
    vintage = query["vintage_date"][0]
    if malformed:
        return f"observation_date,{series}\n2026-06-01,1.0\n".encode()
    stamp = vintage.replace("-", "")
    return f"observation_date,{series}_{stamp}\n2026-06-01,1.0\n".encode()


def test_complete_capture_writes_raw_log_manifest_and_idempotent_retry(tmp_path: Path) -> None:
    output_root = tmp_path / "macro"
    report_path = tmp_path / "latest.json"

    def fetcher(url: str) -> tuple[int, bytes]:
        return 200, _csv_for_url(url)

    first = fred_macro_capture.run_capture(
        root=ROOT,
        output_root=output_root,
        report_path=report_path,
        series_ids=("CPIAUCSL", "UNRATE"),
        vintage=NOW.date(),
        observation_start=date(2026, 6, 1),
        observation_end=NOW.date(),
        fetcher=fetcher,
        clock=lambda: NOW,
    )
    retry = fred_macro_capture.run_capture(
        root=ROOT,
        output_root=output_root,
        report_path=report_path,
        series_ids=("CPIAUCSL", "UNRATE"),
        vintage=NOW.date(),
        observation_start=date(2026, 6, 1),
        observation_end=NOW.date(),
        fetcher=fetcher,
        clock=lambda: NOW + timedelta(hours=1),
    )

    assert first["status"] == "complete"
    assert first["promotion_eligible"] is False
    assert first["canonical_briefing_connected"] is False
    assert [row["appended_count"] for row in first["results"]] == [1, 1]
    assert [row["appended_count"] for row in retry["results"]] == [0, 0]
    assert len(MacroPITLog(output_root / "observations.jsonl").rows()) == 2
    assert len(list((output_root / "manifests").glob("macro-*.json"))) == 2
    latest = json.loads(report_path.read_text(encoding="utf-8"))
    manifest_path = Path(str(latest["manifest_path"]))
    assert latest["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def test_partial_failure_retains_raw_and_reports_series_error(tmp_path: Path) -> None:
    output_root = tmp_path / "macro"

    def fetcher(url: str) -> tuple[int, bytes]:
        series = parse_qs(urlparse(url).query)["id"][0]
        return 200, _csv_for_url(url, malformed=series == "UNRATE")

    report = fred_macro_capture.run_capture(
        root=ROOT,
        output_root=output_root,
        report_path=tmp_path / "latest.json",
        series_ids=("CPIAUCSL", "UNRATE"),
        vintage=NOW.date(),
        observation_start=date(2026, 6, 1),
        observation_end=NOW.date(),
        fetcher=fetcher,
        clock=lambda: NOW,
    )

    assert report["status"] == "partial_failure"
    results = {str(row["series_id"]): row for row in report["results"]}
    assert results["CPIAUCSL"]["status"] == "ok"
    assert results["UNRATE"]["status"] == "error"
    assert results["UNRATE"]["raw_retained"] is True
    raw_sha = str(results["UNRATE"]["raw_payload_sha256"])
    assert ImmutableRawStore(output_root / "raw").exists(raw_sha)


def test_http_failure_reports_raw_not_retained(tmp_path: Path) -> None:
    report = fred_macro_capture.run_capture(
        root=ROOT,
        output_root=tmp_path / "macro",
        report_path=tmp_path / "latest.json",
        series_ids=("CPIAUCSL",),
        vintage=NOW.date(),
        observation_start=date(2026, 6, 1),
        observation_end=NOW.date(),
        fetcher=lambda _url: (503, b""),
        clock=lambda: NOW,
    )

    result = report["results"][0]
    assert result["status"] == "error"
    assert result["raw_payload_sha256"] is None
    assert result["raw_retained"] is False


def test_append_new_is_process_safe_and_natural_key_unique(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    with ProcessPoolExecutor(max_workers=8) as executor:
        appended = list(executor.map(_append_same_observation, [str(path)] * 16))

    rows = MacroPITLog(path).rows()
    assert sum(appended) == 1
    assert len(rows) == 1
    assert rows[0]["event_id"] == "CPIAUCSL:2026-06-01:2026-07-23"


def test_append_new_deduplicates_same_batch_and_rejects_partial_tail(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    log = MacroPITLog(path)
    vintage = NOW.date()
    observations = parse_vintage_csv(
        b"observation_date,CPIAUCSL_20260723\n2026-06-01,1.0\n",
        series_id="CPIAUCSL",
        vintage=vintage,
        source_uri="https://example.invalid/alfred.csv",
        received_at=NOW,
    )

    assert log.append_new([observations[0], observations[0]]) == 1
    path.write_bytes(path.read_bytes() + b'{"event_id":"partial"')
    with pytest.raises(MacroPITError, match="incomplete trailing row"):
        log.append_new(observations)


def test_existing_duplicate_natural_key_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    row = {
        "event_id": "CPIAUCSL:2026-06-01:2026-07-23",
        "series_id": "CPIAUCSL",
    }
    encoded = json.dumps(row, sort_keys=True) + "\n"
    path.write_text(encoded + encoded, encoding="utf-8")

    with pytest.raises(MacroPITError, match="duplicate event_id"):
        MacroPITLog(path).rows()


def test_macro_capture_rejects_naive_received_time() -> None:
    with pytest.raises(MacroPITError, match="received_at must be timezone-aware"):
        parse_vintage_csv(
            b"observation_date,CPIAUCSL_20260723\n2026-06-01,1.0\n",
            series_id="CPIAUCSL",
            vintage=NOW.date(),
            source_uri="https://example.invalid/alfred.csv",
            received_at=datetime(2026, 7, 23, 1, 0),
        )


def test_dry_run_is_network_free_and_writes_nothing(tmp_path: Path, capsys) -> None:
    exit_code = fred_macro_capture.main(
        [
            "--root",
            str(tmp_path),
            "--series",
            "GDPC1",
            "--vintage-date",
            NOW.date().isoformat(),
            "--observation-start",
            "2026-01-01",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert not (tmp_path / "collect").exists()
    assert not (tmp_path / "logs").exists()
