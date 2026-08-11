from __future__ import annotations

from datetime import UTC, datetime
import json
import os

from fx_intel.service_heartbeat import evaluate_required_service_heartbeats

NOW = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)


def _config(tmp_path, services: list[dict[str, object]]):
    path = tmp_path / "heartbeats.json"
    path.write_text(json.dumps({"services": services}), encoding="utf-8")
    return path


def _mtime(path, moment: datetime) -> None:
    timestamp = moment.timestamp()
    os.utime(path, (timestamp, timestamp))


def test_required_service_heartbeats_allow_only_recent_healthy_outputs(tmp_path) -> None:
    healthy = tmp_path / "healthy.json"
    healthy.write_text(json.dumps({"overall": "ok"}), encoding="utf-8")
    _mtime(healthy, NOW)
    config = _config(
        tmp_path,
        [
            {
                "service_id": "health",
                "path": "healthy.json",
                "max_age_seconds": 60,
                "status_field": "overall",
                "status_value": "ok",
            }
        ],
    )

    gate = evaluate_required_service_heartbeats(config, root=tmp_path, now=NOW)

    assert gate.allow_new_risk is True
    assert gate.rejection_reasons == []


def test_missing_stale_or_unhealthy_heartbeat_rejects_new_risk(tmp_path) -> None:
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"overall": "ok"}), encoding="utf-8")
    _mtime(stale, datetime(2026, 8, 11, 2, 0, tzinfo=UTC))
    unhealthy = tmp_path / "unhealthy.json"
    unhealthy.write_text(json.dumps({"overall": "critical"}), encoding="utf-8")
    _mtime(unhealthy, NOW)
    config = _config(
        tmp_path,
        [
            {"service_id": "missing", "path": "missing.json", "max_age_seconds": 60},
            {"service_id": "stale", "path": "stale.json", "max_age_seconds": 60},
            {
                "service_id": "unhealthy",
                "path": "unhealthy.json",
                "max_age_seconds": 60,
                "status_field": "overall",
                "status_value": "ok",
            },
        ],
    )

    gate = evaluate_required_service_heartbeats(config, root=tmp_path, now=NOW)

    assert gate.allow_new_risk is False
    assert len(gate.rejection_reasons) == 3
    assert any("missing:file_missing" in reason for reason in gate.rejection_reasons)
    assert any("stale:stale" in reason for reason in gate.rejection_reasons)
    assert any("unhealthy:status_mismatch" in reason for reason in gate.rejection_reasons)
