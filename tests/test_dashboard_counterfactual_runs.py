from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "tools" / "ai_learning_dashboard"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from server import _counterfactual_runs_payload  # noqa: E402


def _write_run(root: Path, run_id: str, *, rows: str, cost_policy: str) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    dataset = run_dir / "dataset.jsonl"
    dataset.write_text(rows, encoding="utf-8")
    manifest = {
        "row_count": rows.count("\n"),
        "coverage": 0.92,
        "window_start": "2026-07-28T04:30:29+00:00",
        "window_end": "2026-09-03T22:10:38+00:00",
        "replay_cost_policy_sha256": cost_policy,
        "measured": False,
        "tradable": False,
        "promotion_eligible": False,
        "excluded_count": 489,
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    completion = {
        "run_id": run_id,
        "files": {
            "dataset.jsonl": hashlib.sha256(dataset.read_bytes()).hexdigest(),
            "manifest.json": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
    }
    (run_dir / "completion.json").write_text(json.dumps(completion), encoding="utf-8")
    return run_dir


def test_missing_root_returns_empty(tmp_path: Path) -> None:
    payload = _counterfactual_runs_payload(tmp_path / "absent")
    assert payload["runs"] == []


def test_intact_run_is_verified(tmp_path: Path) -> None:
    _write_run(tmp_path, "20260905T000124Z-abc", rows='{"a":1}\n', cost_policy="8cfe093d")
    payload = _counterfactual_runs_payload(tmp_path)
    (run,) = payload["runs"]
    assert run["verified"] is True
    assert run["row_count"] == 1
    assert run["cost_policy_sha256"] == "8cfe093d"


def test_tampered_dataset_is_reported_unverified(tmp_path: Path) -> None:
    """壊れた成果物を正常として見せないこと。"""
    run_dir = _write_run(tmp_path, "20260905T000124Z-abc", rows='{"a":1}\n', cost_policy="x")
    dataset = run_dir / "dataset.jsonl"
    dataset.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
    payload = _counterfactual_runs_payload(tmp_path)
    (run,) = payload["runs"]
    assert run["verified"] is False


def test_runs_are_listed_newest_first(tmp_path: Path) -> None:
    _write_run(tmp_path, "20260904T023814Z-old", rows='{"a":1}\n', cost_policy="1e094a66")
    _write_run(tmp_path, "20260905T000124Z-new", rows='{"a":1}\n', cost_policy="8cfe093d")
    payload = _counterfactual_runs_payload(tmp_path)
    assert [r["run_id"] for r in payload["runs"]] == [
        "20260905T000124Z-new",
        "20260904T023814Z-old",
    ]


def test_incomplete_run_directory_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "20260905T000124Z-partial").mkdir()
    payload = _counterfactual_runs_payload(tmp_path)
    assert payload["runs"] == []


def test_payload_states_labels_are_not_measured_executions(tmp_path: Path) -> None:
    _write_run(tmp_path, "20260905T000124Z-abc", rows='{"a":1}\n', cost_policy="x")
    payload = _counterfactual_runs_payload(tmp_path)
    assert "not measured executions" in str(payload["basis"])
