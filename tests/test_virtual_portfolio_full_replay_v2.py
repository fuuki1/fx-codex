from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools import virtual_portfolio_full_replay_v2 as full_replay

START = datetime(2026, 8, 1, tzinfo=UTC)


def _decision() -> dict[str, object]:
    return {
        "decision_id": "history-1",
        "prediction_time": START.isoformat(),
        "symbol": "USDJPY",
        "timeframe": "1h",
        "direction": "neutral",
    }


def test_full_replay_is_create_only_hash_bound_and_audited(tmp_path, monkeypatch) -> None:
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text("{}\n", encoding="utf-8")
    commits = tmp_path / "decisions.commits.jsonl"
    quotes = tmp_path / "quotes.jsonl"
    quotes.write_text("{}\n", encoding="utf-8")
    database = tmp_path / "canonical-v2.sqlite3"
    manifest = tmp_path / "manifest.json"

    monkeypatch.setattr(
        full_replay.decision_log,
        "read_decision_events",
        lambda *_args, **_kwargs: iter([_decision()]),
    )
    monkeypatch.setattr(
        full_replay,
        "_git_identity",
        lambda _root: {"commit": "a" * 40, "dirty": False},
    )
    monkeypatch.setattr(
        full_replay,
        "run_replay_cycle",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "waiting": 0,
            "opened": 0,
            "abstained": 1,
            "closed": 0,
            "conflicts": [],
        },
    )

    report = full_replay.run_full_replay(
        decisions=decisions,
        decision_commits=commits,
        quotes=quotes,
        quote_index=None,
        database=database,
        manifest=manifest,
        as_of=START,
        writer_id="test",
        repository_root=Path.cwd(),
    )

    assert report["eligible_to_resume_shadow"] is True
    assert report["git"]["commit"] == "a" * 40
    assert report["database"]["sha256"]
    assert report["audit"]["passed"] is True
    assert manifest.is_file()

    with pytest.raises(FileExistsError, match="create-only"):
        full_replay.run_full_replay(
            decisions=decisions,
            decision_commits=commits,
            quotes=quotes,
            quote_index=None,
            database=database,
            manifest=manifest,
            as_of=START,
            writer_id="test",
            repository_root=Path.cwd(),
        )
