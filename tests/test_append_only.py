from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from fx_intel import journal
from fx_intel.append_only import (
    AppendOnlyReadError,
    AppendOnlyWriteError,
    append_jsonl_idempotent,
    canonical_row_hash,
    read_jsonl_strict,
)
from fx_intel.briefing import TradePlan

NOW = datetime(2026, 7, 13, 3, 0, tzinfo=UTC)


def _write_row(path: Path, **overrides: object) -> None:
    row: dict[str, object] = {
        "schema_version": 2,
        "ts": (NOW - timedelta(seconds=5)).isoformat(),
        "event_time": (NOW - timedelta(seconds=5)).isoformat(),
        "source_time": (NOW - timedelta(seconds=8)).isoformat(),
        "published_time": (NOW - timedelta(seconds=7)).isoformat(),
        "revision_time": (NOW - timedelta(seconds=6)).isoformat(),
        "ingested_time": (NOW - timedelta(seconds=4)).isoformat(),
        "available_time": (NOW - timedelta(seconds=3)).isoformat(),
        "source": "test",
    }
    row.update(overrides)
    row["content_hash"] = canonical_row_hash(row)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_strict_reader_validates_every_declared_timestamp_against_as_of(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_row(path, source_time=(NOW + timedelta(seconds=1)).isoformat())

    with pytest.raises(AppendOnlyReadError, match="source_time beyond"):
        list(read_jsonl_strict(path, as_of=NOW))


@pytest.mark.parametrize(
    "field",
    [
        "ts",
        "event_time",
        "available_time",
        "ingested_time",
        "published_time",
        "revision_time",
        "source_time",
    ],
)
def test_strict_reader_rejects_naive_secondary_timestamps(tmp_path: Path, field: str) -> None:
    path = tmp_path / "events.jsonl"
    _write_row(path, **{field: "2026-07-13T02:59:00"})

    with pytest.raises(AppendOnlyReadError, match=field):
        list(read_jsonl_strict(path, as_of=NOW))


@pytest.mark.parametrize("field", ["event_time", "ingested_time", "source_time"])
def test_strict_reader_rejects_clocks_after_availability(tmp_path: Path, field: str) -> None:
    path = tmp_path / "events.jsonl"
    _write_row(
        path,
        available_time=(NOW - timedelta(seconds=3)).isoformat(),
        **{field: (NOW - timedelta(seconds=2)).isoformat()},
    )

    with pytest.raises(AppendOnlyReadError, match=f"{field} is later"):
        list(read_jsonl_strict(path, as_of=NOW))


def test_strict_reader_rejects_revision_before_publication(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_row(
        path,
        published_time=(NOW - timedelta(seconds=6)).isoformat(),
        revision_time=(NOW - timedelta(seconds=7)).isoformat(),
    )

    with pytest.raises(AppendOnlyReadError, match="revision_time precedes"):
        list(read_jsonl_strict(path, as_of=NOW))


def test_strict_reader_accepts_ordered_aware_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_row(path)

    rows = list(read_jsonl_strict(path, as_of=NOW))

    assert len(rows) == 1


def test_append_rejects_naive_timestamp_before_poisoning_journal(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    row = {"schema_version": 2, "ts": "2026-07-13T02:59:00", "id": "one"}

    with pytest.raises(AppendOnlyWriteError, match="timestamp is naive"):
        append_jsonl_idempotent(
            path,
            [row],
            identity=lambda value: str(value.get("id") or "") or None,
        )

    assert not path.exists() or path.read_text(encoding="utf-8") == ""


def test_strict_reader_rejects_duplicate_decision_identity(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    row: dict[str, object] = {
        "schema_version": 2,
        "decision_id": "decision-1",
        "ts": (NOW - timedelta(seconds=1)).isoformat(),
    }
    row["content_hash"] = canonical_row_hash(row)
    line = json.dumps(row) + "\n"
    path.write_text(line + line, encoding="utf-8")

    with pytest.raises(AppendOnlyReadError, match="duplicate append-only identity"):
        list(read_jsonl_strict(path, as_of=NOW))


def test_journal_rejects_forged_self_reported_identity_on_read_and_append(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.jsonl"
    plan = TradePlan(
        symbol="USDJPY",
        direction="long",
        action="no_trade",
        conviction=40,
        composite=0.4,
        tech_score=0.5,
        news_score=0.2,
        close=150.0,
        atr=0.2,
    )
    journal.append_plans(path, [plan], now=NOW, run_slot=NOW)
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["decision_id"] = "forged-distinct-id"
    forged["content_hash"] = canonical_row_hash(forged)
    path.write_text(json.dumps(forged) + "\n", encoding="utf-8")

    with pytest.raises(AppendOnlyReadError, match="decision_id does not match"):
        list(journal.read_entries(path, as_of=NOW))
    with pytest.raises(AppendOnlyWriteError, match="decision_id does not match"):
        journal.append_plans(
            path,
            [plan],
            now=NOW + timedelta(minutes=5),
            run_slot=NOW + timedelta(minutes=5),
        )
