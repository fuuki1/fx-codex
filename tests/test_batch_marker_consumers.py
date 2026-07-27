"""Non-learning readers must hide uncommitted compact journal batches."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from fx_intel import journal
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
