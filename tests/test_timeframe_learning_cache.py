from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
from unittest import mock

import pytest

from fx_intel import decision_feedback, maximization, tf_learning, tp_sl_learning
from fx_intel.learning import LearnedProfile


def test_learning_cache_round_trips(tmp_path) -> None:
    generated_at = datetime(2026, 8, 16, tzinfo=UTC).isoformat()

    tf_path = tmp_path / "tf.json"
    tf_learning.save_timeframe_learning(
        tf_learning.TimeframeLearning(
            generated_at=generated_at,
            profiles={
                ("USDJPY", "1h"): LearnedProfile(
                    generated_at=generated_at,
                    evaluated=30,
                    hits=12,
                    symbol_factors={"USDJPY": 0.8},
                )
            },
        ),
        tf_path,
    )
    loaded_tf = tf_learning.load_timeframe_learning(tf_path)
    assert loaded_tf.generated_at == generated_at
    assert loaded_tf.profile_for("USDJPY", "1h").symbol_factors["USDJPY"] == 0.8

    tp_path = tmp_path / "tp.json"
    tp_sl_learning.save_timeframe_tp_sl_learning(
        tp_sl_learning.TimeframeTpSlLearning(
            generated_at=generated_at,
            profiles={
                ("USDJPY", "1h"): tp_sl_learning.TpSlProfile(
                    generated_at=generated_at,
                    evaluated=40,
                    hits=10,
                )
            },
        ),
        tp_path,
    )
    loaded_tp = tp_sl_learning.load_timeframe_tp_sl_learning(tp_path)
    assert loaded_tp.profile_for("USDJPY", "1h").evaluated == 40

    max_path = tmp_path / "max.json"
    maximization.save_timeframe_maximization(
        maximization.TimeframeMaximization(
            generated_at=generated_at,
            cells={
                ("USDJPY", "1h", "long"): maximization.MaximizationCell(
                    "USDJPY",
                    "1h",
                    "long",
                    action="avoid",
                    factor=maximization.BLOCK_FACTOR,
                    block=True,
                )
            },
        ),
        max_path,
    )
    loaded_max = maximization.load_timeframe_maximization(max_path)
    assert loaded_max.cell_for("USDJPY", "1h", "long").block is True

    feedback_path = tmp_path / "feedback.json"
    decision_feedback.save_decision_feedback(
        decision_feedback.DecisionFeedbackProfile(
            generated_at=generated_at,
            cells={
                ("USDJPY", "1h", "long"): decision_feedback.DecisionFeedbackCell(
                    "USDJPY", "1h", "long", action="dampen", factor=0.75
                )
            },
        ),
        feedback_path,
    )
    loaded_feedback = decision_feedback.load_decision_feedback(feedback_path)
    assert loaded_feedback.cell_for("USDJPY", "1h", "long").factor == 0.75


def test_learning_cache_loaders_reject_unknown_contract(tmp_path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps({"cache_contract": "unknown", "generated_at": "2026-08-16T00:00:00+00:00"}),
        encoding="utf-8",
    )

    assert tf_learning.load_timeframe_learning(path).generated_at == ""
    assert tp_sl_learning.load_timeframe_tp_sl_learning(path).generated_at == ""
    assert maximization.load_timeframe_maximization(path).generated_at == ""
    assert decision_feedback.load_decision_feedback(path).generated_at == ""


def test_learning_cache_loaders_reject_malformed_cells(tmp_path) -> None:
    generated_at = "2026-08-16T00:00:00+00:00"
    cases = [
        (
            tf_learning.load_timeframe_learning,
            tf_learning.TIMEFRAME_LEARNING_CACHE_CONTRACT,
            {
                "profiles": {"USDJPY|1h": {"generated_at": generated_at, "bins": "bad"}},
                "per_timeframe": {},
            },
        ),
        (
            tp_sl_learning.load_timeframe_tp_sl_learning,
            tp_sl_learning.TIMEFRAME_TP_SL_CACHE_CONTRACT,
            {
                "profiles": {"USDJPY|1h": {"generated_at": generated_at, "evaluated": "bad"}},
                "per_timeframe": {},
            },
        ),
        (
            maximization.load_timeframe_maximization,
            maximization.TIMEFRAME_MAXIMIZATION_CACHE_CONTRACT,
            {
                "cells": {"USDJPY|1h|long": {"symbol": "EURUSD"}},
                "per_timeframe": {},
                "notes_ja": [],
            },
        ),
        (
            decision_feedback.load_decision_feedback,
            decision_feedback.DECISION_FEEDBACK_CACHE_CONTRACT,
            {"cells": {"USDJPY|1h|long": {"symbol": "EURUSD"}}, "notes_ja": []},
        ),
    ]
    for index, (loader, contract, body) in enumerate(cases):
        path = tmp_path / f"invalid-{index}.json"
        path.write_text(
            json.dumps({"cache_contract": contract, "generated_at": generated_at, **body}),
            encoding="utf-8",
        )
        assert loader(path).generated_at == ""


def test_policy_cache_loaders_reject_semantic_type_and_block_mismatch(tmp_path) -> None:
    generated_at = "2026-08-16T00:00:00+00:00"
    max_path = tmp_path / "max.json"
    maximization.save_timeframe_maximization(
        maximization.TimeframeMaximization(
            generated_at=generated_at,
            cells={
                ("USDJPY", "1h", "long"): maximization.MaximizationCell(
                    "USDJPY",
                    "1h",
                    "long",
                    action="avoid",
                    factor=maximization.BLOCK_FACTOR,
                    block=True,
                )
            },
        ),
        max_path,
    )
    max_payload = json.loads(max_path.read_text(encoding="utf-8"))
    max_payload["cells"]["USDJPY|1h|long"].update({"factor": 1.0, "block": 0, "sample_ok": "yes"})
    max_path.write_text(json.dumps(max_payload), encoding="utf-8")
    assert maximization.load_timeframe_maximization(max_path).generated_at == ""

    feedback_path = tmp_path / "feedback.json"
    decision_feedback.save_decision_feedback(
        decision_feedback.DecisionFeedbackProfile(
            generated_at=generated_at,
            cells={
                ("USDJPY", "1h", "long"): decision_feedback.DecisionFeedbackCell(
                    "USDJPY",
                    "1h",
                    "long",
                    action="avoid",
                    factor=decision_feedback.BLOCK_FACTOR,
                    block=True,
                )
            },
        ),
        feedback_path,
    )
    feedback_payload = json.loads(feedback_path.read_text(encoding="utf-8"))
    feedback_payload["cells"]["USDJPY|1h|long"].update(
        {"factor": 1.0, "block": 0, "sample_ok": "yes"}
    )
    feedback_path.write_text(json.dumps(feedback_payload), encoding="utf-8")
    assert decision_feedback.load_decision_feedback(feedback_path).generated_at == ""


def test_timeframe_learning_monitor_publishes_all_caches(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    journal_path.write_text("", encoding="utf-8")
    price_path.write_text("", encoding="utf-8")
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
            "--stage-timing-log",
            str(tmp_path / "timing.jsonl"),
        ]
    )

    result = module.run(args)

    assert result["status"] == "ok"
    for name in ("tf.json", "tp.json", "max.json", "monitor.json", "timing.jsonl"):
        assert (tmp_path / name).exists()
    timing_rows = [
        json.loads(line) for line in (tmp_path / "timing.jsonl").read_text().splitlines()
    ]
    assert {row["stage"] for row in timing_rows} == {
        "timeframe_learning_journal_read",
        "timeframe_learning_price_read",
        "timeframe_direction_learning_refresh",
        "timeframe_tp_sl_learning_refresh",
        "timeframe_maximization_learning_refresh",
    }
    import fx_briefing

    generated_at = str(result["generated_at"])
    with (
        mock.patch.object(fx_briefing, "DEFAULT_TF_JOURNAL_PATH", journal_path),
        mock.patch.object(fx_briefing, "DEFAULT_TF_PRICES_PATH", price_path),
        mock.patch.object(fx_briefing, "DEFAULT_TF_LEARNING_PATH", tmp_path / "tf.json"),
        mock.patch.object(fx_briefing, "DEFAULT_TP_SL_LEARNING_PATH", tmp_path / "tp.json"),
        mock.patch.object(fx_briefing, "DEFAULT_MAXIMIZATION_PATH", tmp_path / "max.json"),
    ):
        assert fx_briefing.timeframe_learning_bundle_is_valid(
            tmp_path / "monitor.json",
            generated_at=generated_at,
            now=datetime.fromisoformat(generated_at),
            max_age_seconds=1800,
        )
        for name in ("tf.json", "tp.json", "max.json"):
            artifact = tmp_path / name
            original = artifact.read_bytes()
            artifact.write_bytes(original + b"\n")
            assert not fx_briefing.timeframe_learning_bundle_is_valid(
                tmp_path / "monitor.json",
                generated_at=generated_at,
                now=datetime.fromisoformat(generated_at),
                max_age_seconds=1800,
            )
            artifact.write_bytes(original)
        manifest = json.loads((tmp_path / "monitor.json").read_text(encoding="utf-8"))
        manifest["input_watermarks"]["prices"]["max_available_time"] = (
            datetime.fromisoformat(generated_at) + timedelta(seconds=1)
        ).isoformat()
        (tmp_path / "monitor.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert not fx_briefing.timeframe_learning_bundle_is_valid(
            tmp_path / "monitor.json",
            generated_at=generated_at,
            now=datetime.fromisoformat(generated_at) + timedelta(seconds=5),
            max_age_seconds=1800,
        )
        manifest["input_watermarks"]["prices"]["max_available_time"] = None
        manifest["input_watermarks"]["prices"]["source_path"] = str(
            (tmp_path / "alternate-prices.jsonl").resolve()
        )
        (tmp_path / "monitor.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert not fx_briefing.timeframe_learning_bundle_is_valid(
            tmp_path / "monitor.json",
            generated_at=generated_at,
            now=datetime.fromisoformat(generated_at),
            max_age_seconds=1800,
        )


def test_timeframe_learning_monitor_excludes_non_pit_decisions(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_pit", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    journal_path.write_text("", encoding="utf-8")
    price_path.write_text("", encoding="utf-8")
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
    decision_rows = [
        {"id": "eligible", "prediction_time": "2026-08-15T00:00:00+00:00"},
        {"id": "legacy", "prediction_time": "2026-08-15T00:00:00+00:00"},
    ]
    price_rows = [{"id": "price"}]
    captured: list[list[dict[str, str]]] = []

    def derive(entries, *, now):
        captured.append(list(entries))
        return tf_learning.TimeframeLearning(generated_at=now.isoformat())

    with (
        mock.patch.object(
            module.journal,
            "read_entries",
            side_effect=[iter(decision_rows), iter(price_rows)],
        ),
        mock.patch.object(
            module.journal,
            "is_pit_eligible_entry",
            side_effect=lambda row: row.get("id") == "eligible",
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
            return_value=tp_sl_learning.TimeframeTpSlLearning(
                generated_at="2026-08-16T00:00:00+00:00"
            ),
        ),
        mock.patch.object(
            module.maximization,
            "derive_timeframe_maximization",
            return_value=maximization.TimeframeMaximization(
                generated_at="2026-08-16T00:00:00+00:00"
            ),
        ),
    ):
        result = module.run(args)

    assert result["decision_rows"] == 2
    assert result["pit_eligible_decision_rows"] == 1
    assert captured == [
        [
            {"id": "eligible", "prediction_time": "2026-08-15T00:00:00+00:00"},
            {"id": "price"},
        ]
    ]


def test_timeframe_learning_monitor_rejects_future_price(tmp_path, monkeypatch) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_future", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixed_now = datetime(2026, 8, 16, tzinfo=UTC)
    valid = module.price_history.snapshot_entries(
        {"USDJPY": {"1h": 150.0}}, now=fixed_now - timedelta(minutes=5)
    )[0]
    future = module.price_history.snapshot_entries(
        {"USDJPY": {"1h": 151.0}}, now=fixed_now + timedelta(minutes=50)
    )[0]
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    journal_path.write_text("", encoding="utf-8")
    price_path.write_text(
        "\n".join(json.dumps(row) for row in (valid, future)) + "\n", encoding="utf-8"
    )
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
    captured: list[list[dict[str, object]]] = []
    original_datetime = module.datetime

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", FixedDatetime)

    def derive(entries, *, now):
        captured.append(list(entries))
        return tf_learning.TimeframeLearning(generated_at=now.isoformat())

    with mock.patch.object(module.tf_learning, "derive_timeframe_learning", side_effect=derive):
        result = module.run(args)

    monkeypatch.setattr(module, "datetime", original_datetime)
    assert result["price_rows"] == 2
    assert result["pit_eligible_price_rows"] == 1
    assert result["excluded_future_or_invalid_price_rows"] == 1
    assert [row["close"] for row in captured[0]] == [150.0]


def test_timeframe_learning_monitor_rejects_source_change_during_read(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_race", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    journal_path.write_text("", encoding="utf-8")
    price_path.write_text("", encoding="utf-8")
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

    def changing_read(_path):
        journal_path.write_text("{}\n", encoding="utf-8")
        return iter(())

    with (
        mock.patch.object(module.journal, "read_entries", side_effect=changing_read),
        pytest.raises(RuntimeError, match="decision journal changed"),
    ):
        module.run(args)

    assert not (tmp_path / "monitor.json").exists()


def test_learning_cache_freshness_rejects_future_and_stale() -> None:
    import fx_briefing

    now = datetime(2026, 8, 16, tzinfo=UTC)
    assert fx_briefing.learning_cache_is_fresh(now.isoformat(), now=now, max_age_seconds=1800)
    assert not fx_briefing.learning_cache_is_fresh(
        (now - timedelta(hours=1)).isoformat(), now=now, max_age_seconds=1800
    )
    assert not fx_briefing.learning_cache_is_fresh(
        (now + timedelta(seconds=1)).isoformat(), now=now, max_age_seconds=1800
    )
