from pathlib import Path
from datetime import UTC, datetime

import pytest

from tools.capture_journal_soak import (
    DOCUMENTED_FOUR_PAIR_MESSAGES_PER_SECOND,
    _representative_payload,
    main,
    run_soak,
)


def test_soak_reports_bounded_files_and_crash_recovery(tmp_path: Path) -> None:
    report = run_soak(tmp_path, messages=50, min_free_after_bytes=0)

    assert report["replayed_quotes"] == 50
    assert report["journal_file_count"] <= 3
    assert report["incomplete_visible_before_recovery"] == 0
    assert report["recovery_appended_unavailable"] is True
    assert report["integrity_verified_after_recovery"] is True
    assert report["payload_profile"] == "representative_oanda_price_json_not_provider_captured"
    assert report["parser_profile"] == "production_parse_price_line_with_replay_provenance"
    assert report["raw_payload_bytes"]["min"] >= 500
    assert report["latency_ms"]["sample_size"] <= 100_000
    assert report["logical_window_end"].startswith("2026-01-05T00:00:03.")
    assert report["journal_shard_count"] == 1
    assert report["main_window_journal_shard_count"] == 1
    assert report["bounded_file_layout"] is True
    assert report["passed"] is True
    assert all(report["checks"].values())
    assert report["post_run_disk_reserve_ok"] is True
    assert report["production_daemon_rss_validated"] is False
    assert report["memory_evidence_scope"] == "probe_process_peak_not_production_daemon"


def test_representative_payload_contains_oanda_price_shape() -> None:
    payload = _representative_payload(
        0,
        datetime(2026, 1, 5, tzinfo=UTC),
    )

    assert b'"type":"PRICE"' in payload
    assert b'"instrument":"USD_JPY"' in payload
    assert b'"bids":' in payload
    assert b'"asks":' in payload


def test_soak_rejects_invalid_rate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="message_rate"):
        run_soak(
            tmp_path,
            messages=DOCUMENTED_FOUR_PAIR_MESSAGES_PER_SECOND,
            message_rate=0,
        )


def test_soak_cli_returns_failure_when_post_run_reserve_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--messages",
            "1",
            "--progress-every",
            "0",
            "--min-free-after-gib",
            "1000000",
        ]
    )

    report = capsys.readouterr().out
    assert exit_code == 1
    assert '"passed": false' in report
    assert '"post_run_disk_reserve": false' in report
