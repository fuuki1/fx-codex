"""Atomic replacement guarantees for the full-decision latest snapshot."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest import mock

import pytest

from fx_intel import decision_log


def test_latest_snapshot_replace_failure_preserves_previous_file(tmp_path) -> None:
    path = tmp_path / "latest.json"
    path.write_text('{"previous": true}', encoding="utf-8")
    event = {
        "decision_id": "decision-1",
        "decision": {"direction": "neutral"},
    }

    with mock.patch.object(
        decision_log.os,
        "replace",
        side_effect=OSError("simulated replace failure"),
    ):
        with pytest.raises(OSError, match="replace failure"):
            decision_log.save_latest_snapshot(
                path,
                [event],
                now=datetime(2026, 7, 27, tzinfo=UTC),
            )

    assert path.read_text(encoding="utf-8") == '{"previous": true}'
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []
