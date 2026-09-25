"""Learning rows are hashed in full, then compacted without changing what learners see."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, UTC
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest import mock

import pytest

from fx_intel import maximization, tf_learning, tp_sl_learning

START = datetime(2026, 9, 1, tzinfo=UTC)


def _monitor(name: str):
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _monitor("timeframe_learning_monitor_compaction")


def _unused_fields(index: int) -> dict[str, object]:
    return {
        "shadow_predictions": [{"model": f"shadow-{index % 3}", "p_long": 0.4 + index / 1000}],
        "input_features": {"rsi_1h": 50.0 + index, "adx_1h": 20.0},
        "input_feature_masks": {"rsi_1h": True, "adx_1h": index % 2 == 0},
        "price_frame": {"close": [150.0 + index / 100, 150.1]},
        "components": [{"key": "tech", "label_ja": "テクニカル", "score": 0.5, "weight": 0.55}],
    }


def _decision(index: int, timeframe: str, horizon: float, direction: str, close: float) -> dict:
    ts = START + timedelta(hours=index * horizon)
    return {
        "ts": ts.isoformat(),
        "prediction_time": ts.isoformat(),
        "symbol": "USDJPY",
        "timeframe": timeframe,
        "horizon_hours": horizon,
        "direction": direction,
        "conviction": 60,
        "composite": 0.4,
        "tech_score": 0.5,
        "news_score": 0.2,
        "close": close,
        "atr": 0.10 if timeframe == "1h" else 0.30,
        "stop": close - 0.2,
        "target1": close + 0.2,
        "target2": close + 0.4,
        "features": {"rsi_1h": 70.0 - index % 5, "adx_1h": 15.0 + index % 7},
        "learning_dimensions": {"session": "tokyo" if index % 2 else "london"},
        "gate_trace": [{"gate": "data_quality", "status": "passed"}],
        **_unused_fields(index),
    }


def _history() -> list[dict]:
    rows = []
    for index in range(24):
        rows.append(_decision(index, "1h", 1.0, "long", 156.0 - index * 0.05))
    for index in range(12):
        rows.append(_decision(index, "4h", 4.0, "short", 156.0 - index * 0.2))
    return rows


def _derive_all(entries: list[dict], out: Path) -> dict[str, bytes]:
    now = START + timedelta(days=5)
    tf_learning.save_timeframe_learning(
        tf_learning.derive_timeframe_learning(entries, now=now), out / "tf.json"
    )
    tp_sl_learning.save_timeframe_tp_sl_learning(
        tp_sl_learning.derive_timeframe_tp_sl_learning(entries, now=now), out / "tp.json"
    )
    maximization.save_timeframe_maximization(
        maximization.derive_timeframe_maximization(entries, now=now), out / "max.json"
    )
    return {name: (out / name).read_bytes() for name in ("tf.json", "tp.json", "max.json")}


def test_learners_derive_identical_caches_from_compacted_rows(tmp_path, monkeypatch) -> None:
    full = _history()
    full_dir = tmp_path / "full"
    compact_dir = tmp_path / "compact"
    full_dir.mkdir()
    compact_dir.mkdir()
    digest = MODULE._EligibleRowsDigest(lambda _row, _as_of: True, provisional_as_of=START)

    monkeypatch.chdir(full_dir)  # the settled-match cache is cwd-relative
    expected = _derive_all(copy.deepcopy(full), full_dir)
    compacted = MODULE._compact_learning_rows(
        copy.deepcopy(full),
        digest=digest,
        drop_fields=MODULE.LEARNING_UNUSED_DECISION_FIELDS,
    )
    monkeypatch.chdir(compact_dir)
    actual = _derive_all(compacted, compact_dir)

    assert json.loads(expected["tf.json"])["profiles"], "fixture must produce learned profiles"
    assert actual == expected
    assert all(
        field not in row for row in compacted for field in MODULE.LEARNING_UNUSED_DECISION_FIELDS
    )


def test_shared_strings_keeps_every_value_and_type() -> None:
    row = {
        "symbol": "USDJPY",
        "long_text": "x" * 200,
        "nested": {"status": "blocked", "values": [1, 2.5, None, True, "a"]},
        7: "non-string key",
    }

    compacted = MODULE._shared_strings(copy.deepcopy(row))

    assert compacted == row
    assert json.dumps(compacted, sort_keys=False) == json.dumps(row, sort_keys=False)
    other = MODULE._shared_strings({"symbol": "USD" + "JPY"})
    assert other["symbol"] is compacted["symbol"]


def _legacy_sha256(rows: list[dict]) -> str:
    return MODULE._rows_sha256(rows)


def _row(name: str, available: datetime | None) -> dict:
    return {"id": name, "available": available.isoformat() if available else None, "blob": [name]}


def _eligible(row, as_of):
    return row["available"] is not None and datetime.fromisoformat(row["available"]) <= as_of


@pytest.mark.parametrize("final_offset_minutes", [0, 1, 3, 10])
def test_streamed_digest_equals_hash_of_rows_eligible_at_final_time(final_offset_minutes) -> None:
    provisional = START + timedelta(minutes=10)
    rows = [
        _row("early", START),
        _row("never", None),
        _row("late-1", provisional + timedelta(minutes=1)),
        _row("on-time-after-late", START + timedelta(minutes=5)),
        _row("late-3", provisional + timedelta(minutes=3)),
        _row("never-2", None),
        _row("early-2", START + timedelta(minutes=9)),
    ]
    final = provisional + timedelta(minutes=final_offset_minutes)
    digest = MODULE._EligibleRowsDigest(_eligible, provisional_as_of=provisional)
    for row in rows:
        digest.observe(row)
        row["blob"] = None  # later mutation (compaction) must not affect the hash

    sha, count = digest.finalize(final)

    expected_rows = [
        {**row, "blob": [row["id"]]} for row in rows if _eligible({**row}, final)  # full rows
    ]
    assert (sha, count) == (_legacy_sha256(expected_rows), len(expected_rows))


def test_streamed_digest_rejects_final_time_before_provisional() -> None:
    digest = MODULE._EligibleRowsDigest(_eligible, provisional_as_of=START)
    with pytest.raises(ValueError, match="before its provisional time"):
        digest.finalize(START - timedelta(seconds=1))


def test_monitor_manifest_hashes_full_rows_and_learns_from_compacted_rows(tmp_path) -> None:
    module = _monitor("timeframe_learning_monitor_compaction_run")
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    journal_path.write_text("", encoding="utf-8")
    price_path.write_text("", encoding="utf-8")
    decision_rows = [
        {
            "id": "eligible",
            "prediction_time": "2026-09-01T00:00:00+00:00",
            **_unused_fields(1),
        },
        {"id": "legacy", "prediction_time": "2026-09-01T00:00:00+00:00", **_unused_fields(2)},
        {
            "id": "eligible-2",
            "prediction_time": "2026-09-01T01:00:00+00:00",
            **_unused_fields(3),
        },
    ]
    price_rows = [{"id": "price", "close": 150.0}, {"id": "stale-price"}]
    full_decisions = copy.deepcopy(decision_rows)
    full_prices = copy.deepcopy(price_rows)
    populations = iter((decision_rows, price_rows))
    captured: list[list[dict]] = []

    def read_entries(path, *, audit, **_kwargs):
        rows = next(populations)
        audit.update(
            {
                "physical_rows": len(rows),
                "parsed_object_rows": len(rows),
                "blank_rows": 0,
                "source_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            }
        )
        return iter(rows)

    def derive(entries, *, now):
        captured.append(list(entries))
        return tf_learning.TimeframeLearning(generated_at=now.isoformat())

    args = module.build_parser().parse_args(
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
        ]
    )
    with (
        mock.patch.object(module.journal, "read_entries", side_effect=read_entries),
        mock.patch.object(
            module.journal,
            "is_pit_eligible_entry",
            side_effect=lambda row: str(row.get("id", "")).startswith("eligible"),
        ),
        mock.patch.object(
            module.price_history,
            "is_pit_eligible_snapshot_entry",
            side_effect=lambda row, **_kwargs: row.get("id") == "price",
        ),
        mock.patch.object(module.tf_learning, "derive_timeframe_learning", side_effect=derive),
        mock.patch.object(
            module.tp_sl_learning,
            "derive_timeframe_tp_sl_learning",
            return_value=tp_sl_learning.TimeframeTpSlLearning(generated_at="x"),
        ),
        mock.patch.object(
            module.maximization,
            "derive_timeframe_maximization",
            return_value=maximization.TimeframeMaximization(generated_at="x"),
        ),
    ):
        result = module.run(args)

    watermarks = result["input_watermarks"]
    assert watermarks["decisions"]["sha256"] == module._rows_sha256(
        [full_decisions[0], full_decisions[2]]
    )
    assert watermarks["decisions"]["eligible_rows"] == 2
    assert watermarks["prices"]["sha256"] == module._rows_sha256([full_prices[0]])
    assert captured == [
        [
            {"id": "eligible", "prediction_time": "2026-09-01T00:00:00+00:00"},
            {"id": "eligible-2", "prediction_time": "2026-09-01T01:00:00+00:00"},
            {"id": "price", "close": 150.0},
        ]
    ]


def test_monitor_fails_closed_when_hashed_and_learned_populations_differ(tmp_path) -> None:
    digest = MODULE._EligibleRowsDigest(lambda _row, _as_of: True, provisional_as_of=START)
    digest.observe({"id": "a"})

    with pytest.raises(RuntimeError, match="covered 1 rows but PIT filter kept 2"):
        MODULE._finalized_digest(digest, START, eligible_rows=2, label="decision")
