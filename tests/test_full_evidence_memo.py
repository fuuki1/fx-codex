"""Receipt full-log evidence is reused only inside a byte-identical full-log prefix."""

from __future__ import annotations

from datetime import datetime, UTC
import importlib.util
import json
from pathlib import Path

from fx_intel import decision_commit, decision_log, journal

NOW = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)


def _pit_envelope(decision_id: str) -> dict[str, object]:
    return {
        "ts": NOW.isoformat(),
        "prediction_time": NOW.isoformat(),
        "source_cutoff": NOW.isoformat(),
        "max_feature_available_time": NOW.isoformat(),
        "pit_eligible": True,
        "pit_contract": journal.DECISION_JOURNAL_PIT_CONTRACT,
        "decision_id": decision_id,
        "mode": "per_timeframe",
        "producer": journal.TIMEFRAME_PRODUCER,
        "producer_version": journal.TIMEFRAME_PRODUCER_VERSION,
        "input_context_id": f"context:{decision_id}",
        "source_record_ids": [f"source:{decision_id}"],
    }


def _commit_transaction(full_path: Path, compact_path: Path, decision_id: str) -> None:
    transaction_id = decision_commit.transaction_id_for([decision_id])
    full_batch = decision_log.append_decision_events(
        full_path,
        [
            {
                "schema": 1,
                "event_type": decision_log.EVENT_TYPE,
                "symbol": "USDJPY",
                "timeframe": "1h",
                "horizon_hours": 1.0,
                "source": "fx_briefing",
                "decision": {"direction": "long", "close": 150.0},
                **_pit_envelope(decision_id),
            }
        ],
        transaction_id=transaction_id,
    )
    compact_batch = journal._append_journal_batch(  # noqa: SLF001
        compact_path,
        [
            {
                "symbol": "USDJPY",
                "timeframe": "1h",
                "horizon_hours": 1.0,
                "direction": "long",
                "close": 150.0,
                **_pit_envelope(decision_id),
            }
        ],
        decision_transaction_id=transaction_id,
    )
    decision_commit.append_commit(
        full_path,
        decision_ids=[decision_id],
        full_batch_sha256=str(full_batch["batch_sha256"]),
        full_batch_line_start_offset=int(full_batch["line_start_offset"]),
        full_batch_line_sha256=str(full_batch["line_sha256"]),
        compact_batch_sha256=str(compact_batch["batch_sha256"]),
        compact_batch_line_start_offset=int(compact_batch["line_start_offset"]),
        compact_batch_byte_length=int(compact_batch["byte_length"]),
        compact_batch_payload_sha256=str(compact_batch["payload_sha256"]),
        mode="per_timeframe",
        committed_at=NOW,
    )


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return (
        tmp_path / decision_commit.DEFAULT_COMMIT_FILENAME,
        tmp_path / "briefing_tf_journal.jsonl",
    )


def _load(compact_path: Path, full_path: Path, memo=None) -> dict[str, dict[str, object]]:
    return decision_commit.load_compact_receipts(
        compact_path,
        compact_end_offset=compact_path.stat().st_size,
        full_end_offset=full_path.stat().st_size,
        full_evidence_memo=memo,
    )


def test_memo_reuses_only_trusted_receipts_and_matches_uncached_result(tmp_path) -> None:
    full_path, compact_path = _paths(tmp_path)
    for index in range(3):
        _commit_transaction(full_path, compact_path, f"decision-{index}")
    first = decision_commit.FullEvidenceMemo()
    baseline = _load(compact_path, full_path)

    assert _load(compact_path, full_path, first) == baseline
    assert (first.reused, first.computed, len(first.proven)) == (0, 3, 3)

    boundary = full_path.stat().st_size
    _commit_transaction(full_path, compact_path, "decision-3")
    second = decision_commit.FullEvidenceMemo(first.proven, trusted_through=boundary)

    assert _load(compact_path, full_path, second) == _load(compact_path, full_path)
    assert (second.reused, second.computed, len(second.proven)) == (3, 1, 4)


def test_memo_does_not_trust_receipt_outside_trusted_prefix(tmp_path) -> None:
    full_path, compact_path = _paths(tmp_path)
    _commit_transaction(full_path, compact_path, "decision-0")
    proven = decision_commit.FullEvidenceMemo()
    _load(compact_path, full_path, proven)

    # The key is known, but its full-log lines are not inside the trusted range.
    narrowed = decision_commit.FullEvidenceMemo(proven.proven, trusted_through=1)

    assert len(_load(compact_path, full_path, narrowed)) == 1
    assert (narrowed.reused, narrowed.computed) == (0, 1)


def test_memo_is_ignored_for_unknown_receipts(tmp_path) -> None:
    full_path, compact_path = _paths(tmp_path)
    _commit_transaction(full_path, compact_path, "decision-0")
    memo = decision_commit.FullEvidenceMemo(["0" * 64], trusted_through=full_path.stat().st_size)

    assert len(_load(compact_path, full_path, memo)) == 1
    assert (memo.reused, memo.computed) == (0, 1)


def _monitor(name: str):
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _monitor_args(module, tmp_path: Path, journal_path: Path, price_path: Path):
    return module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(tmp_path / "tf.json"),
            "--tp-sl-learning",
            str(tmp_path / "tp.json"),
            "--maximization",
            str(tmp_path / "max.json"),
            "--monitor-json",
            str(tmp_path / "monitor.json"),
            "--stage-timing-log",
            str(tmp_path / "timing.jsonl"),
        ]
    )


def _memo_stages(tmp_path: Path) -> list[dict[str, object]]:
    rows = [json.loads(line) for line in (tmp_path / "timing.jsonl").read_text().splitlines()]
    return [row for row in rows if row["stage"] == "timeframe_learning_full_evidence_memo_save"]


def _learned_inputs(tmp_path: Path) -> dict[str, object]:
    manifest = json.loads((tmp_path / "monitor.json").read_text(encoding="utf-8"))
    decisions = manifest["input_watermarks"]["decisions"]
    return {key: decisions[key] for key in ("sha256", "source_sha256", "raw_rows")}


def test_monitor_reuses_memo_and_learns_identical_rows(tmp_path) -> None:
    module = _monitor("timeframe_learning_monitor_memo_reuse")
    full_path, journal_path = _paths(tmp_path)
    price_path = tmp_path / "prices.jsonl"
    price_path.write_text("", encoding="utf-8")
    for index in range(3):
        _commit_transaction(full_path, journal_path, f"decision-{index}")
    args = _monitor_args(module, tmp_path, journal_path, price_path)
    memo_path = tmp_path / "monitor.full_evidence_memo.json"

    module.run(args)
    first_memo = json.loads(memo_path.read_text(encoding="utf-8"))
    _commit_transaction(full_path, journal_path, "decision-3")
    module.run(args)
    with_memo = _learned_inputs(tmp_path)
    memo_path.unlink()
    module.run(args)

    first, second, cold = _memo_stages(tmp_path)
    assert first["detail"] == {
        "memo_status": "absent",
        "trusted_receipts": 0,
        "reused_receipts": 0,
        "computed_receipts": 3,
    }
    assert second["detail"] == {
        "memo_status": "trusted",
        "trusted_receipts": 3,
        "reused_receipts": 3,
        "computed_receipts": 1,
    }
    assert cold["detail"]["computed_receipts"] == 4
    assert with_memo["raw_rows"] == 4
    assert _learned_inputs(tmp_path) == with_memo
    assert first_memo["contract"] == module.FULL_EVIDENCE_MEMO_CONTRACT
    assert len(first_memo["receipt_keys"]) == 3
    saved = json.loads(memo_path.read_text(encoding="utf-8"))
    assert saved["full_log_read_offset_bytes"] == full_path.stat().st_size
    assert len(saved["receipt_keys"]) == 4


def test_monitor_rejects_memo_whose_full_prefix_changed(tmp_path) -> None:
    module = _monitor("timeframe_learning_monitor_memo_prefix")
    full_path, journal_path = _paths(tmp_path)
    price_path = tmp_path / "prices.jsonl"
    price_path.write_text("", encoding="utf-8")
    _commit_transaction(full_path, journal_path, "decision-0")
    args = _monitor_args(module, tmp_path, journal_path, price_path)
    memo_path = tmp_path / "monitor.full_evidence_memo.json"
    module.run(args)
    memo = json.loads(memo_path.read_text(encoding="utf-8"))
    memo["full_log_prefix_sha256"] = "f" * 64
    memo_path.write_text(json.dumps(memo), encoding="utf-8")

    module.run(args)

    stage = _memo_stages(tmp_path)[-1]
    assert stage["detail"]["memo_status"] == "prefix_changed"
    assert stage["detail"]["reused_receipts"] == 0
    assert stage["detail"]["computed_receipts"] == 1


def test_monitor_ignores_memo_beyond_snapshot_and_malformed_memo(tmp_path) -> None:
    module = _monitor("timeframe_learning_monitor_memo_bounds")
    full_path, journal_path = _paths(tmp_path)
    price_path = tmp_path / "prices.jsonl"
    price_path.write_text("", encoding="utf-8")
    _commit_transaction(full_path, journal_path, "decision-0")
    args = _monitor_args(module, tmp_path, journal_path, price_path)
    memo_path = tmp_path / "monitor.full_evidence_memo.json"
    memo_path.write_text(
        json.dumps(
            {
                "contract": module.FULL_EVIDENCE_MEMO_CONTRACT,
                "full_log_read_offset_bytes": full_path.stat().st_size + 1,
                "full_log_prefix_sha256": "a" * 64,
                "receipt_keys": [],
            }
        ),
        encoding="utf-8",
    )
    module.run(args)
    memo_path.write_text('{"contract": "other"}', encoding="utf-8")
    module.run(args)

    beyond, malformed = _memo_stages(tmp_path)
    assert beyond["detail"]["memo_status"] == "prefix_beyond_snapshot"
    assert malformed["detail"]["memo_status"] == "absent"
    assert malformed["detail"]["computed_receipts"] == 1


def test_monitor_memo_write_failure_does_not_block_publication(tmp_path, monkeypatch) -> None:
    module = _monitor("timeframe_learning_monitor_memo_write")
    full_path, journal_path = _paths(tmp_path)
    price_path = tmp_path / "prices.jsonl"
    price_path.write_text("", encoding="utf-8")
    _commit_transaction(full_path, journal_path, "decision-0")
    args = _monitor_args(module, tmp_path, journal_path, price_path)
    real_write = module.atomic_write_json

    def failing_write(path, payload):
        if Path(path).name.endswith(".full_evidence_memo.json"):
            raise OSError("disk full")
        return real_write(path, payload)

    monkeypatch.setattr(module, "atomic_write_json", failing_write)

    result = module.run(args)

    assert result["status"] == "ok"
    stage = _memo_stages(tmp_path)[-1]
    assert stage["status"] == "handled_error"
