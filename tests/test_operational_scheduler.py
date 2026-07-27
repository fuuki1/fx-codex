from __future__ import annotations

from datetime import datetime, UTC
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from fx_intel import operational_scheduler as scheduler

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_new_run_id_uses_aware_utc_and_pid() -> None:
    run_id = scheduler.new_run_id(
        now=datetime(2026, 7, 27, 4, 5, 6, 789, tzinfo=UTC),
        process_id=42,
    )

    assert run_id == "20260727T040506000789Z-p42"


@pytest.mark.parametrize(
    "run_id",
    ("../escape", "has.dot", "has space", "", "x" * 129),
)
def test_validate_run_id_rejects_unsafe_names(run_id: str) -> None:
    with pytest.raises(scheduler.OperationalScheduleError):
        scheduler.validate_run_id(run_id)


def test_unsupported_action_fails_before_creating_evidence(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    with pytest.raises(scheduler.OperationalScheduleError, match="unsupported"):
        scheduler.run_scheduled(  # type: ignore[arg-type]
            action="delete",
            database_path=tmp_path / "candidate.sqlite3",
            decision_path=tmp_path / "decisions.jsonl",
            price_path=tmp_path / "prices.jsonl",
            evidence_dir=evidence_dir,
        )
    assert not evidence_dir.exists()


def test_sync_writes_create_only_evidence_and_preserves_quality_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_sync_sources(**kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        assert kwargs["writer_id"] == "scheduled-shadow-sync-sync-1"
        return {"operation": "shadow_sync", "quality_failure": True}

    monkeypatch.setattr(scheduler, "sync_sources", fake_sync_sources)
    evidence_dir = tmp_path / "evidence"
    result = scheduler.run_scheduled(
        action="sync",
        database_path=tmp_path / "candidate.sqlite3",
        decision_path=tmp_path / "decisions.jsonl",
        price_path=tmp_path / "prices.jsonl",
        evidence_dir=evidence_dir,
        run_id="sync-1",
    )

    assert result.exit_code == 1
    assert result.intent_path == evidence_dir / "shadow-sync-sync-1.intent.json"
    assert result.report_path == evidence_dir / "shadow-sync-sync-1.result.json"
    assert result.intent_path.is_file()
    assert json.loads(result.report_path.read_text(encoding="utf-8")) == result.payload
    assert result.payload["scheduler"]["run_id"] == "sync-1"
    with pytest.raises(scheduler.OperationalScheduleError, match="already exists"):
        scheduler.run_scheduled(
            action="sync",
            database_path=tmp_path / "candidate.sqlite3",
            decision_path=tmp_path / "decisions.jsonl",
            price_path=tmp_path / "prices.jsonl",
            evidence_dir=evidence_dir,
            run_id="sync-1",
        )
    assert calls == 1


def test_full_replay_verdict_controls_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_action(**kwargs: object) -> dict[str, object]:
        assert kwargs["writer_id"] == "scheduled-full-replay-replay-1"
        return {"operation": "operational_full_replay", "verdict": "drift_detected"}

    monkeypatch.setattr(scheduler, "_run_action", fake_action)
    result = scheduler.run_scheduled(
        action="full-replay",
        database_path=tmp_path / "candidate.sqlite3",
        decision_path=tmp_path / "decisions.jsonl",
        price_path=tmp_path / "prices.jsonl",
        evidence_dir=tmp_path / "evidence",
        run_id="replay-1",
    )

    assert result.exit_code == 1
    assert result.report_path.name == "full-replay-replay-1.result.json"


def test_full_replay_catch_up_quality_failure_controls_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_action(**kwargs: object) -> dict[str, object]:
        return {
            "operation": "operational_full_replay",
            "verdict": "full_replay_verified",
            "pre_replay_sync": {"quality_failure": True},
        }

    monkeypatch.setattr(scheduler, "_run_action", fake_action)
    result = scheduler.run_scheduled(
        action="full-replay",
        database_path=tmp_path / "candidate.sqlite3",
        decision_path=tmp_path / "decisions.jsonl",
        price_path=tmp_path / "prices.jsonl",
        evidence_dir=tmp_path / "evidence",
        run_id="quality-failure",
    )

    assert result.exit_code == 1
    assert result.payload["verdict"] == "full_replay_verified"
    assert result.payload["pre_replay_sync"]["quality_failure"] is True


def test_started_failure_keeps_intent_and_terminal_failure_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_sync(**kwargs: object) -> dict[str, object]:
        raise RuntimeError("price source failed after decision parsing")

    monkeypatch.setattr(scheduler, "sync_sources", fail_sync)
    evidence = tmp_path / "evidence"

    with pytest.raises(scheduler.ScheduledRunFailure) as failed:
        scheduler.run_scheduled(
            action="sync",
            database_path=tmp_path / "candidate.sqlite3",
            decision_path=tmp_path / "decisions.jsonl",
            price_path=tmp_path / "prices.jsonl",
            evidence_dir=evidence,
            run_id="failed-1",
        )

    intent = failed.value.intent_path
    failure = failed.value.failure_path
    assert intent.name == "shadow-sync-failed-1.intent.json"
    assert failure.name == "shadow-sync-failed-1.failure.json"
    assert intent.is_file()
    payload = json.loads(failure.read_text(encoding="utf-8"))
    assert payload["error_type"] == "RuntimeError"
    assert payload["intent_sha256"] == scheduler.file_sha256(intent)


@pytest.mark.parametrize(
    "template",
    (
        "com.fx-codex.operational-sync.plist.tmpl",
        "com.fx-codex.operational-replay.plist.tmpl",
    ),
)
def test_operational_plists_start_evidence_aware_scheduler_directly(
    template: str,
) -> None:
    raw = (REPO_ROOT / "ops" / "launchd" / template).read_text(encoding="utf-8")
    rendered = (
        raw.replace("__FX_ROOT__", "/Users/example/srv/fx-codex")
        .replace("__OPERATIONAL_PYTHON__", "/approved/python")
        .replace("__OPERATIONAL_DB__", "/data/operational.sqlite3")
        .replace("__OPERATIONAL_EVIDENCE_DIR__", "/data/evidence")
    )

    root = ET.fromstring(rendered)
    arguments = [node.text or "" for node in root.iter("string")]
    assert any(value.endswith("operational_store_scheduled.py") for value in arguments)
    assert not any(value.endswith("run_exclusive.py") for value in arguments)
