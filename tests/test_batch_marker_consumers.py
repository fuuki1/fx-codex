"""Non-learning readers must hide uncommitted compact journal batches."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from fx_intel import journal
from tests.decision_fixtures import write_committed_compact_rows
from tools import learning_loop_audit

_SERVER_PATH = Path(__file__).resolve().parents[1] / "tools" / "ai_learning_dashboard" / "server.py"


def _dashboard_server():
    spec = importlib.util.spec_from_file_location("batch_dashboard_server", _SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_and_dashboard_ignore_uncommitted_batches(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    rows = [
        {
            "journal_batch_id": "crashed",
            "journal_batch_index": 0,
            "journal_batch_size": 2,
            "decision_id": "hidden",
        },
        {
            "journal_batch_id": "complete",
            "journal_batch_index": 0,
            "journal_batch_size": 1,
            "decision_id": "visible",
        },
        {
            "event_type": journal.JOURNAL_BATCH_COMMIT,
            "journal_batch_id": "complete",
            "journal_batch_size": 1,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    audit_rows = learning_loop_audit.read_jsonl(path).rows
    dashboard_rows = _dashboard_server()._read_journal(path)
    assert [row["decision_id"] for row in audit_rows] == ["visible"]
    assert [row["decision_id"] for row in dashboard_rows] == ["visible"]


def test_audit_and_dashboard_require_cross_log_commit(tmp_path) -> None:
    path = tmp_path / "briefing_tf_journal.jsonl"
    timestamp = "2026-07-27T07:00:00+00:00"
    row = {
        "ts": timestamp,
        "prediction_time": timestamp,
        "source_cutoff": timestamp,
        "max_feature_available_time": timestamp,
        "pit_eligible": True,
        "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
        "decision_id": "committed",
        "mode": "per_timeframe",
        "producer": journal.TIMEFRAME_PRODUCER,
        "producer_version": journal.TIMEFRAME_PRODUCER_VERSION,
        "input_context_id": "context",
        "source_record_ids": ["source"],
        "symbol": "USDJPY",
        "timeframe": "1h",
        "direction": "long",
        "close": 150.0,
    }
    write_committed_compact_rows(path, [row], mode="per_timeframe")

    assert [item["decision_id"] for item in learning_loop_audit.read_jsonl(path).rows] == [
        "committed"
    ]
    assert [item["decision_id"] for item in _dashboard_server()._read_journal(path)] == [
        "committed"
    ]

    decision_path = tmp_path / "briefing_decisions.jsonl"
    wrapper_only = decision_path.read_text(encoding="utf-8").splitlines()[0]
    decision_path.write_text(wrapper_only + "\n", encoding="utf-8")
    assert learning_loop_audit.read_jsonl(path).rows == []
    assert _dashboard_server()._read_journal(path) == []
