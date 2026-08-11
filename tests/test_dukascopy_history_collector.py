from __future__ import annotations

from datetime import UTC, datetime
import json

from tools.fx_dukascopy_history_collector import main


def test_dry_run_is_credential_free_read_only_and_two_hours_delayed(tmp_path, capsys) -> None:
    output_root = tmp_path / "collect"
    result = main(
        [
            "--output-root",
            str(output_root),
            "--now",
            "2026-08-10T12:34:00+00:00",
            "--dry-run",
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["broker_connected"] is False
    assert payload["collection_mode"] == "historical_download"
    assert payload["eligible_through"] == "2026-08-10T10:00:00+00:00"
    assert len(payload["plan"]["USDJPY"]) == 4
    assert not output_root.exists()


def test_aware_now_is_required(tmp_path) -> None:
    try:
        main(
            [
                "--output-root",
                str(tmp_path / "collect"),
                "--now",
                datetime(2026, 8, 10, 12, 0, tzinfo=UTC).replace(tzinfo=None).isoformat(),
                "--dry-run",
            ]
        )
    except SystemExit as error:
        assert "timezone-aware" in str(error)
    else:
        raise AssertionError("naive --now must fail closed")
