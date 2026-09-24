from __future__ import annotations

from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
from unittest import mock

import pytest

from fx_intel import decision_feedback, maximization, state_io, tf_learning, tp_sl_learning
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
        "timeframe_learning_pit_filter",
        "timeframe_direction_learning_refresh",
        "timeframe_tp_sl_learning_refresh",
        "timeframe_maximization_learning_refresh",
        "timeframe_learning_manifest_prepare",
        "timeframe_learning_bundle_lock_wait",
        "timeframe_learning_previous_generation_check",
        "timeframe_learning_bundle_publish",
        "timeframe_learning_manifest_publish",
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


def test_timeframe_learning_monitor_derives_full_generation_before_publish(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_order", script)
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
    events: list[str] = []

    def track(name: str, wrapped):
        def call(*call_args, **call_kwargs):
            events.append(name)
            return wrapped(*call_args, **call_kwargs)

        return call

    with (
        mock.patch.object(
            module.tf_learning,
            "derive_timeframe_learning",
            side_effect=track("derive_direction", module.tf_learning.derive_timeframe_learning),
        ),
        mock.patch.object(
            module.tp_sl_learning,
            "derive_timeframe_tp_sl_learning",
            side_effect=track(
                "derive_tp_sl", module.tp_sl_learning.derive_timeframe_tp_sl_learning
            ),
        ),
        mock.patch.object(
            module.maximization,
            "derive_timeframe_maximization",
            side_effect=track(
                "derive_maximization", module.maximization.derive_timeframe_maximization
            ),
        ),
        mock.patch.object(
            module.tf_learning,
            "save_timeframe_learning",
            side_effect=track("save_direction", module.tf_learning.save_timeframe_learning),
        ),
        mock.patch.object(
            module.tp_sl_learning,
            "save_timeframe_tp_sl_learning",
            side_effect=track("save_tp_sl", module.tp_sl_learning.save_timeframe_tp_sl_learning),
        ),
        mock.patch.object(
            module.maximization,
            "save_timeframe_maximization",
            side_effect=track("save_maximization", module.maximization.save_timeframe_maximization),
        ),
    ):
        module.run(args)

    assert events == [
        "derive_direction",
        "derive_tp_sl",
        "derive_maximization",
        "save_direction",
        "save_tp_sl",
        "save_maximization",
    ]


def test_timeframe_learning_monitor_rejects_split_canonical_output_set(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_outputs", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.build_parser().parse_args(
        [
            "--monitor-json",
            str(tmp_path / "alternate-monitor.json"),
        ]
    )

    with pytest.raises(ValueError, match="outputs and monitor must be used together"):
        module.run(args)


def test_canonical_learning_bundle_rejects_noncanonical_inputs(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_inputs", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(tmp_path / "alternate-journal.jsonl"),
        ]
    )

    with pytest.raises(ValueError, match="requires canonical journal and price inputs"):
        module.run(args)


def test_canonical_learning_bundle_accepts_resolved_input_alias(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_input_alias", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    alias = tmp_path / "journal-alias.jsonl"
    alias.symlink_to(module.DEFAULT_JOURNAL)
    args = module.build_parser().parse_args(["--journal", str(alias)])

    validated = module._validate_output_bundle(args)

    assert validated[0] == module.DEFAULT_JOURNAL.resolve()


def test_timeframe_learning_monitor_rejects_lock_output_collision(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_collision", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monitor_path = tmp_path / "monitor.json"
    args = module.build_parser().parse_args(
        [
            "--tf-learning",
            str(module._bundle_lock_path(monitor_path)),
            "--tp-sl-learning",
            str(tmp_path / "tp.json"),
            "--maximization",
            str(tmp_path / "max.json"),
            "--monitor-json",
            str(monitor_path),
        ]
    )

    with pytest.raises(ValueError, match="lock must not alias data or output"):
        module.run(args)


def test_timeframe_learning_monitor_rejects_case_variant_lock_collision(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_case", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monitor_path = tmp_path / "monitor.json"
    args = module.build_parser().parse_args(
        [
            "--tf-learning",
            str(tmp_path / "MONITOR.JSON.LOCK"),
            "--tp-sl-learning",
            str(tmp_path / "tp.json"),
            "--maximization",
            str(tmp_path / "max.json"),
            "--monitor-json",
            str(monitor_path),
        ]
    )

    with pytest.raises(ValueError, match="lock must not alias data or output"):
        module.run(args)


@pytest.mark.parametrize("timing_target", ("tf", "monitor", "lock"))
def test_timeframe_learning_monitor_rejects_timing_output_alias(tmp_path, timing_target) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location(
        f"timeframe_learning_monitor_timing_{timing_target}", script
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monitor_path = tmp_path / "monitor.json"
    outputs = {
        "tf": tmp_path / "tf.json",
        "tp": tmp_path / "tp.json",
        "max": tmp_path / "max.json",
        "monitor": monitor_path,
        "lock": module._bundle_lock_path(monitor_path),
    }
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(outputs["tf"]),
            "--tp-sl-learning",
            str(outputs["tp"]),
            "--maximization",
            str(outputs["max"]),
            "--monitor-json",
            str(outputs["monitor"]),
            "--stage-timing-log",
            str(outputs[timing_target]),
        ]
    )

    with pytest.raises(ValueError, match="stage timing log must not alias"):
        module.run(args)


def test_timeframe_learning_monitor_normalizes_output_symlink_aliases(
    tmp_path, monkeypatch
) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_alias", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    canonical_dir = tmp_path / "canonical"
    alias_dir = tmp_path / "aliases"
    canonical_dir.mkdir()
    alias_dir.mkdir()
    canonical = {
        "tf": canonical_dir / "tf.json",
        "tp": canonical_dir / "tp.json",
        "max": canonical_dir / "max.json",
        "monitor": canonical_dir / "monitor.json",
    }
    aliases = {key: alias_dir / f"alias-{key}.json" for key in canonical}
    for key, alias in aliases.items():
        alias.symlink_to(canonical[key])
    monkeypatch.setattr(module, "DEFAULT_TF_LEARNING", canonical["tf"])
    monkeypatch.setattr(module, "DEFAULT_TP_SL_LEARNING", canonical["tp"])
    monkeypatch.setattr(module, "DEFAULT_MAXIMIZATION", canonical["max"])
    monkeypatch.setattr(module, "DEFAULT_MONITOR", canonical["monitor"])
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    journal_path.write_text("", encoding="utf-8")
    price_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(module, "DEFAULT_JOURNAL", journal_path)
    monkeypatch.setattr(module, "DEFAULT_PRICES", price_path)
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(aliases["tf"]),
            "--tp-sl-learning",
            str(aliases["tp"]),
            "--maximization",
            str(aliases["max"]),
            "--monitor-json",
            str(aliases["monitor"]),
        ]
    )

    module.run(args)

    assert all(alias.is_symlink() for alias in aliases.values())
    assert all(path.exists() for path in canonical.values())
    assert module._bundle_lock_path(canonical["monitor"]).exists()
    assert not aliases["monitor"].with_suffix(".json.lock").exists()
    import fx_briefing

    assert fx_briefing._timeframe_learning_bundle_lock_path(
        aliases["monitor"]
    ) == module._bundle_lock_path(canonical["monitor"])
    with (
        mock.patch.object(fx_briefing, "DEFAULT_TF_JOURNAL_PATH", journal_path),
        mock.patch.object(fx_briefing, "DEFAULT_TF_PRICES_PATH", price_path),
        mock.patch.object(fx_briefing, "DEFAULT_TF_LEARNING_PATH", aliases["tf"]),
        mock.patch.object(fx_briefing, "DEFAULT_TP_SL_LEARNING_PATH", aliases["tp"]),
        mock.patch.object(fx_briefing, "DEFAULT_MAXIMIZATION_PATH", aliases["max"]),
    ):
        loaded = fx_briefing._load_cached_timeframe_learning_bundle(
            aliases["monitor"],
            max_age_seconds=1800,
        )
    assert loaded[3:] == (True, True)


def test_advisory_file_lock_has_bounded_reader_wait(tmp_path) -> None:
    lock_path = tmp_path / "bundle.lock"
    writer_locked = threading.Event()
    release_writer = threading.Event()

    def hold_writer() -> None:
        with state_io.advisory_file_lock(lock_path, exclusive=True):
            writer_locked.set()
            assert release_writer.wait(timeout=5)

    writer = threading.Thread(target=hold_writer)
    writer.start()
    assert writer_locked.wait(timeout=5)
    try:
        with pytest.raises(TimeoutError, match="timed out waiting for state lock"):
            with state_io.advisory_file_lock(lock_path, exclusive=False, timeout_seconds=0.02):
                pass
    finally:
        release_writer.set()
        writer.join(timeout=5)

    assert not writer.is_alive()


def test_timeframe_learning_bundle_reader_waits_for_complete_publication(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_lock", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    tf_path = tmp_path / "tf.json"
    tp_path = tmp_path / "tp.json"
    max_path = tmp_path / "max.json"
    monitor_path = tmp_path / "monitor.json"
    journal_path.write_text("", encoding="utf-8")
    price_path.write_text("", encoding="utf-8")
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(tf_path),
            "--tp-sl-learning",
            str(tp_path),
            "--maximization",
            str(max_path),
            "--monitor-json",
            str(monitor_path),
        ]
    )
    module.run(args)

    writer_paused = threading.Event()
    allow_writer = threading.Event()
    reader_done = threading.Event()
    reader_clock_called = threading.Event()
    errors: list[BaseException] = []
    result: list[tuple[object, ...]] = []
    original_tp_save = module.tp_sl_learning.save_timeframe_tp_sl_learning

    def paused_tp_save(*call_args, **call_kwargs):
        writer_paused.set()
        if not allow_writer.wait(timeout=5):
            raise TimeoutError("test did not release cache publisher")
        return original_tp_save(*call_args, **call_kwargs)

    def publish() -> None:
        try:
            module.run(args)
        except BaseException as error:
            errors.append(error)

    import fx_briefing

    def read_bundle() -> None:
        def reader_clock() -> datetime:
            reader_clock_called.set()
            return datetime.now(UTC)

        try:
            result.append(
                fx_briefing._load_cached_timeframe_learning_bundle(
                    monitor_path,
                    max_age_seconds=1800,
                    clock=reader_clock,
                )
            )
        except BaseException as error:
            errors.append(error)
        finally:
            reader_done.set()

    with (
        mock.patch.object(
            module.tp_sl_learning,
            "save_timeframe_tp_sl_learning",
            side_effect=paused_tp_save,
        ),
        mock.patch.object(fx_briefing, "DEFAULT_TF_JOURNAL_PATH", journal_path),
        mock.patch.object(fx_briefing, "DEFAULT_TF_PRICES_PATH", price_path),
        mock.patch.object(fx_briefing, "DEFAULT_TF_LEARNING_PATH", tf_path),
        mock.patch.object(fx_briefing, "DEFAULT_TP_SL_LEARNING_PATH", tp_path),
        mock.patch.object(fx_briefing, "DEFAULT_MAXIMIZATION_PATH", max_path),
    ):
        writer = threading.Thread(target=publish)
        writer.start()
        assert writer_paused.wait(timeout=5)
        reader = threading.Thread(target=read_bundle)
        reader.start()
        assert not reader_done.wait(timeout=0.1)
        assert not reader_clock_called.is_set()
        allow_writer.set()
        writer.join(timeout=5)
        reader.join(timeout=5)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert reader_clock_called.is_set()
    assert errors == []
    assert len(result) == 1
    assert result[0][3:] == (True, True)


def test_partial_bundle_publication_fails_closed_after_writer_error(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_failure", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    tf_path = tmp_path / "tf.json"
    tp_path = tmp_path / "tp.json"
    max_path = tmp_path / "max.json"
    monitor_path = tmp_path / "monitor.json"
    journal_path.write_text("", encoding="utf-8")
    price_path.write_text("", encoding="utf-8")
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(tf_path),
            "--tp-sl-learning",
            str(tp_path),
            "--maximization",
            str(max_path),
            "--monitor-json",
            str(monitor_path),
        ]
    )
    module.run(args)

    with (
        mock.patch.object(
            module.tp_sl_learning,
            "save_timeframe_tp_sl_learning",
            side_effect=OSError("injected TP cache failure"),
        ),
        pytest.raises(OSError, match="injected TP cache failure"),
    ):
        module.run(args)

    import fx_briefing

    with (
        mock.patch.object(fx_briefing, "DEFAULT_TF_JOURNAL_PATH", journal_path),
        mock.patch.object(fx_briefing, "DEFAULT_TF_PRICES_PATH", price_path),
        mock.patch.object(fx_briefing, "DEFAULT_TF_LEARNING_PATH", tf_path),
        mock.patch.object(fx_briefing, "DEFAULT_TP_SL_LEARNING_PATH", tp_path),
        mock.patch.object(fx_briefing, "DEFAULT_MAXIMIZATION_PATH", max_path),
    ):
        loaded = fx_briefing._load_cached_timeframe_learning_bundle(
            monitor_path,
            max_age_seconds=1800,
        )

    assert loaded[3:] == (False, False)


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
    populations = iter((decision_rows, price_rows))

    def read_entries(_path, *, audit, **_kwargs):
        rows = next(populations)
        audit.update(
            {
                "physical_rows": len(rows),
                "parsed_object_rows": len(rows),
                "blank_rows": 0,
                "source_sha256": hashlib.sha256(Path(_path).read_bytes()).hexdigest(),
            }
        )
        return iter(rows)

    def derive(entries, *, now):
        captured.append(list(entries))
        return tf_learning.TimeframeLearning(generated_at=now.isoformat())

    with (
        mock.patch.object(
            module.journal,
            "read_entries",
            side_effect=read_entries,
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


def test_timeframe_learning_monitor_accepts_append_after_snapshot_boundary(tmp_path) -> None:
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

    original_read_entries = module.journal.read_entries
    appended = False

    def appending_read(path, **kwargs):
        nonlocal appended
        if Path(path) == journal_path and not appended:
            with journal_path.open("a", encoding="utf-8") as handle:
                handle.write('{"appended": true}\n')
            appended = True
        return original_read_entries(path, **kwargs)

    with mock.patch.object(module.journal, "read_entries", side_effect=appending_read):
        result = module.run(args)

    assert result["decision_rows"] == 0
    watermark = result["input_watermarks"]["decisions"]
    assert watermark["read_offset_bytes"] == 0
    assert journal_path.stat().st_size > watermark["read_offset_bytes"]


def test_timeframe_learning_monitor_rejects_source_replacement_during_read(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_replace", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    journal_path.write_text("{}\n", encoding="utf-8")
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
    original_read_entries = module.journal.read_entries
    replaced = False

    def replacing_read(path, **kwargs):
        nonlocal replaced
        rows = original_read_entries(path, **kwargs)
        if Path(path) == journal_path and not replaced:
            replacement = tmp_path / "replacement.jsonl"
            replacement.write_text("{}\n", encoding="utf-8")
            replacement.replace(journal_path)
            replaced = True
        return rows

    with (
        mock.patch.object(module.journal, "read_entries", side_effect=replacing_read),
        pytest.raises(RuntimeError, match="source identity changed"),
    ):
        module.run(args)

    assert not (tmp_path / "monitor.json").exists()


def test_timeframe_learning_monitor_rejects_same_inode_prefix_rewrite(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_rewrite", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    journal_path.write_bytes(b'{"id": 1}\n')
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
    original_read_entries = module.journal.read_entries
    rewritten = False

    def rewriting_read(path, **kwargs):
        nonlocal rewritten
        if Path(path) == journal_path and not rewritten:
            with journal_path.open("r+b") as handle:
                handle.write(b'{"id": 2}\n')
                handle.flush()
            rewritten = True
        return original_read_entries(path, **kwargs)

    with (
        mock.patch.object(module.journal, "read_entries", side_effect=rewriting_read),
        pytest.raises(RuntimeError, match="source prefix changed"),
    ):
        module.run(args)

    assert not (tmp_path / "monitor.json").exists()


@pytest.mark.parametrize("rewritten_source", ("journal", "prices"))
def test_timeframe_learning_monitor_rejects_prior_prefix_rewrite_plus_append(
    tmp_path, rewritten_source
) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location(
        f"timeframe_learning_monitor_prior_rewrite_{rewritten_source}", script
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    journal_path.write_bytes(b'{"id": 1}\n')
    price_path.write_bytes(b'{"id": 1}\n')
    outputs = {
        "tf": tmp_path / "tf.json",
        "tp": tmp_path / "tp.json",
        "max": tmp_path / "max.json",
        "monitor": tmp_path / "monitor.json",
    }
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(outputs["tf"]),
            "--tp-sl-learning",
            str(outputs["tp"]),
            "--maximization",
            str(outputs["max"]),
            "--monitor-json",
            str(outputs["monitor"]),
        ]
    )
    module.run(args)
    before = {key: path.read_bytes() for key, path in outputs.items()}
    target = journal_path if rewritten_source == "journal" else price_path
    with target.open("r+b") as handle:
        handle.write(b'{"id": 2}\n')
        handle.seek(0, 2)
        handle.write(b'{"id": 3}\n')
        handle.flush()

    with pytest.raises(RuntimeError, match="previous prefix changed"):
        module.run(args)

    assert {key: path.read_bytes() for key, path in outputs.items()} == before


def test_timeframe_learning_source_snapshot_has_bounded_lock_wait(tmp_path, monkeypatch) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_timeout", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "prices.jsonl"
    source.write_text("", encoding="utf-8")
    writer_locked = threading.Event()
    release_writer = threading.Event()

    def hold_writer() -> None:
        with source.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            writer_locked.set()
            assert release_writer.wait(timeout=5)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    writer = threading.Thread(target=hold_writer)
    writer.start()
    assert writer_locked.wait(timeout=5)
    monkeypatch.setattr(module, "SOURCE_LOCK_TIMEOUT_SECONDS", 0.02)
    try:
        with pytest.raises(TimeoutError, match="learning source lock"):
            module._source_snapshot(source)
    finally:
        release_writer.set()
        writer.join(timeout=5)

    assert not writer.is_alive()


@pytest.mark.parametrize("corrupt_source", ("journal", "prices"))
def test_timeframe_learning_monitor_preserves_bundle_on_malformed_source(
    tmp_path, corrupt_source
) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location(
        f"timeframe_learning_monitor_malformed_{corrupt_source}", script
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    outputs = {
        "tf": tmp_path / "tf.json",
        "tp": tmp_path / "tp.json",
        "max": tmp_path / "max.json",
        "monitor": tmp_path / "monitor.json",
    }
    journal_path.write_text("", encoding="utf-8")
    price_path.write_text("", encoding="utf-8")
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(outputs["tf"]),
            "--tp-sl-learning",
            str(outputs["tp"]),
            "--maximization",
            str(outputs["max"]),
            "--monitor-json",
            str(outputs["monitor"]),
        ]
    )
    module.run(args)
    before = {key: path.read_bytes() for key, path in outputs.items()}
    target = journal_path if corrupt_source == "journal" else price_path
    target.write_text("{malformed-json}\n", encoding="utf-8")

    with pytest.raises(module.journal.JournalIntegrityError, match="malformed JSONL row"):
        module.run(args)

    assert {key: path.read_bytes() for key, path in outputs.items()} == before


def test_timeframe_learning_monitor_rejects_valid_but_truncated_source(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_truncate", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "journal.jsonl"
    price_path = tmp_path / "prices.jsonl"
    outputs = {
        "tf": tmp_path / "tf.json",
        "tp": tmp_path / "tp.json",
        "max": tmp_path / "max.json",
        "monitor": tmp_path / "monitor.json",
    }
    journal_path.write_text("{}\n", encoding="utf-8")
    price_path.write_text("", encoding="utf-8")
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(outputs["tf"]),
            "--tp-sl-learning",
            str(outputs["tp"]),
            "--maximization",
            str(outputs["max"]),
            "--monitor-json",
            str(outputs["monitor"]),
        ]
    )
    module.run(args)
    before = {key: path.read_bytes() for key, path in outputs.items()}
    journal_path.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="source regressed"):
        module.run(args)

    assert {key: path.read_bytes() for key, path in outputs.items()} == before


def test_timeframe_learning_monitor_rejects_cross_log_commit_replacement(tmp_path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_full_replace", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "briefing_tf_journal.jsonl"
    full_path = tmp_path / "briefing_decisions.jsonl"
    price_path = tmp_path / "prices.jsonl"
    outputs = {
        "tf": tmp_path / "tf.json",
        "tp": tmp_path / "tp.json",
        "max": tmp_path / "max.json",
        "monitor": tmp_path / "monitor.json",
    }
    journal_path.write_text("", encoding="utf-8")
    full_path.write_text("", encoding="utf-8")
    price_path.write_text("", encoding="utf-8")
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(outputs["tf"]),
            "--tp-sl-learning",
            str(outputs["tp"]),
            "--maximization",
            str(outputs["max"]),
            "--monitor-json",
            str(outputs["monitor"]),
        ]
    )
    module.run(args)
    before = {key: path.read_bytes() for key, path in outputs.items()}
    replacement = tmp_path / "replacement-full.jsonl"
    replacement.write_text("", encoding="utf-8")
    replacement.replace(full_path)

    with pytest.raises(RuntimeError, match="cross-log commit source identity changed"):
        module.run(args)

    assert {key: path.read_bytes() for key, path in outputs.items()} == before


def test_timeframe_learning_monitor_rejects_cross_log_prior_prefix_rewrite_plus_append(
    tmp_path,
) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location("timeframe_learning_monitor_full_rewrite", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    journal_path = tmp_path / "briefing_tf_journal.jsonl"
    full_path = tmp_path / "briefing_decisions.jsonl"
    price_path = tmp_path / "prices.jsonl"
    journal_path.write_text("", encoding="utf-8")
    full_path.write_bytes(b'{"id": 1}\n')
    price_path.write_text("", encoding="utf-8")
    outputs = {
        "tf": tmp_path / "tf.json",
        "tp": tmp_path / "tp.json",
        "max": tmp_path / "max.json",
        "monitor": tmp_path / "monitor.json",
    }
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(outputs["tf"]),
            "--tp-sl-learning",
            str(outputs["tp"]),
            "--maximization",
            str(outputs["max"]),
            "--monitor-json",
            str(outputs["monitor"]),
        ]
    )
    module.run(args)
    before = {key: path.read_bytes() for key, path in outputs.items()}
    with full_path.open("r+b") as handle:
        handle.write(b'{"id": 2}\n')
        handle.seek(0, 2)
        handle.write(b'{"id": 3}\n')
        handle.flush()

    with pytest.raises(RuntimeError, match="cross-log commit source previous prefix changed"):
        module.run(args)

    assert {key: path.read_bytes() for key, path in outputs.items()} == before


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


def _load_learning_monitor(name: str):
    script = Path(__file__).resolve().parents[1] / "tools" / "timeframe_learning_monitor.py"
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _learning_monitor_args(module, tmp_path: Path, journal_path: Path, price_path: Path):
    outputs = {
        "tf": tmp_path / "tf.json",
        "tp": tmp_path / "tp.json",
        "max": tmp_path / "max.json",
        "monitor": tmp_path / "monitor.json",
    }
    args = module.build_parser().parse_args(
        [
            "--journal",
            str(journal_path),
            "--prices",
            str(price_path),
            "--tf-learning",
            str(outputs["tf"]),
            "--tp-sl-learning",
            str(outputs["tp"]),
            "--maximization",
            str(outputs["max"]),
            "--monitor-json",
            str(outputs["monitor"]),
        ]
    )
    return args, outputs


def _relocate_behind_symlink(source: Path, destination: Path, appended: bytes) -> None:
    # A copy to another directory gets a new path and inode (and, across
    # volumes, a new st_dev) while every previously learned byte is preserved.
    destination.write_bytes(source.read_bytes() + appended)
    source.unlink()
    source.symlink_to(destination)


@pytest.mark.parametrize("relocated_source", ["journal", "prices"])
def test_timeframe_learning_monitor_accepts_relocated_source_with_verified_prefix(
    tmp_path, relocated_source
) -> None:
    module = _load_learning_monitor(f"timeframe_learning_monitor_relocate_{relocated_source}")
    logs = tmp_path / "logs"
    hot = tmp_path / "hot"
    logs.mkdir()
    hot.mkdir()
    journal_path = logs / "journal.jsonl"
    price_path = logs / "prices.jsonl"
    journal_path.write_bytes(b'{"id": 1}\n')
    price_path.write_bytes(b'{"id": 1}\n')
    args, outputs = _learning_monitor_args(module, tmp_path, journal_path, price_path)
    module.run(args)
    previous = json.loads(outputs["monitor"].read_text(encoding="utf-8"))
    scope = "decisions" if relocated_source == "journal" else "prices"
    source = journal_path if relocated_source == "journal" else price_path
    moved = hot / source.name
    _relocate_behind_symlink(source, moved, b'{"id": 2}\n')

    result = module.run(args)

    evidence = result["input_watermarks"][scope]
    assert evidence["source_path"] == str(moved.resolve())
    assert evidence["inode"] != previous["input_watermarks"][scope]["inode"]
    assert evidence["read_offset_bytes"] == moved.stat().st_size
    published = json.loads(outputs["monitor"].read_text(encoding="utf-8"))
    assert published["generated_at"] == result["generated_at"]


@pytest.mark.parametrize("relocated_source", ["journal", "prices"])
def test_timeframe_learning_monitor_rejects_relocation_that_rewrites_prior_prefix(
    tmp_path, relocated_source
) -> None:
    module = _load_learning_monitor(f"timeframe_learning_monitor_bad_move_{relocated_source}")
    logs = tmp_path / "logs"
    hot = tmp_path / "hot"
    logs.mkdir()
    hot.mkdir()
    journal_path = logs / "journal.jsonl"
    price_path = logs / "prices.jsonl"
    journal_path.write_bytes(b'{"id": 1}\n')
    price_path.write_bytes(b'{"id": 1}\n')
    args, outputs = _learning_monitor_args(module, tmp_path, journal_path, price_path)
    module.run(args)
    before = {key: path.read_bytes() for key, path in outputs.items()}
    source = journal_path if relocated_source == "journal" else price_path
    moved = hot / source.name
    moved.write_bytes(b'{"id": 9}\n{"id": 2}\n')
    source.unlink()
    source.symlink_to(moved)

    with pytest.raises(RuntimeError, match="previous prefix changed"):
        module.run(args)

    assert {key: path.read_bytes() for key, path in outputs.items()} == before


def test_timeframe_learning_monitor_accepts_relocated_cross_log_commit_with_verified_prefix(
    tmp_path,
) -> None:
    module = _load_learning_monitor("timeframe_learning_monitor_relocate_full")
    logs = tmp_path / "logs"
    hot = tmp_path / "hot"
    logs.mkdir()
    hot.mkdir()
    journal_path = logs / "briefing_tf_journal.jsonl"
    full_path = logs / "briefing_decisions.jsonl"
    price_path = logs / "prices.jsonl"
    journal_path.write_text("", encoding="utf-8")
    full_path.write_bytes(b'{"id": 1}\n')
    price_path.write_text("", encoding="utf-8")
    args, outputs = _learning_monitor_args(module, tmp_path, journal_path, price_path)
    module.run(args)
    moved = hot / full_path.name
    _relocate_behind_symlink(full_path, moved, b'{"id": 2}\n')

    result = module.run(args)

    commit_snapshot = result["cross_log_commit_snapshot"]
    assert commit_snapshot["source_path"] == str(moved.resolve())
    assert commit_snapshot["read_offset_bytes"] == moved.stat().st_size


@pytest.mark.parametrize("proof", ["matching", "none", "stale_manifest"])
def test_timeframe_learning_rollback_check_device_change_requires_prefix_proof(
    tmp_path, proof
) -> None:
    # Production 2026-09: a block-level clone kept path and inode but changed
    # st_dev (16777229 -> 16777239); the content prefix was intact.
    module = _load_learning_monitor(f"timeframe_learning_monitor_device_{proof}")
    manifest_path = tmp_path / "monitor.json"
    old_evidence = {
        "source_path": "/srv/logs/briefing_tf_prices.jsonl",
        "device": 16777229,
        "inode": 2764841,
        "read_offset_bytes": 10,
        "mtime_ns": 1,
        "source_sha256": "a" * 64,
    }
    manifest_path.write_text(
        json.dumps(
            {
                "monitor_contract": module.MONITOR_CONTRACT,
                "status": "ok",
                "generated_at": "2026-09-13T03:39:50+00:00",
                "input_watermarks": {"prices": old_evidence},
            }
        ),
        encoding="utf-8",
    )
    new_evidence = {**old_evidence, "device": 16777239, "read_offset_bytes": 20, "mtime_ns": 2}
    call = dict(
        generated_at=datetime(2026, 9, 23, tzinfo=UTC),
        input_watermarks={"prices": new_evidence},
        cross_log_commit_snapshot=None,
    )

    # A proof only licenses the manifest evidence it was computed against; one
    # taken from an earlier manifest (another publisher raced in) does not.
    proofs = {
        "matching": {"prices": (10, "a" * 64)},
        "none": {},
        "stale_manifest": {"prices": (4, "b" * 64)},
    }[proof]

    if proof == "matching":
        module._assert_not_source_rollback(manifest_path, verified_prefixes=proofs, **call)
    else:
        with pytest.raises(RuntimeError, match="prices learning source identity changed"):
            module._assert_not_source_rollback(manifest_path, verified_prefixes=proofs, **call)


def test_timeframe_learning_source_snapshot_waits_for_slow_writer(tmp_path, monkeypatch) -> None:
    module = _load_learning_monitor("timeframe_learning_monitor_slow_writer")
    source = tmp_path / "prices.jsonl"
    source.write_bytes(b'{"id": 1}\n')
    writer_locked = threading.Event()
    release_writer = threading.Event()

    def hold_writer() -> None:
        with source.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            writer_locked.set()
            assert release_writer.wait(timeout=10)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    writer = threading.Thread(target=hold_writer)
    writer.start()
    assert writer_locked.wait(timeout=5)
    monkeypatch.setattr(module, "SOURCE_LOCK_TIMEOUT_SECONDS", 5.0)
    timer = threading.Timer(0.3, release_writer.set)
    timer.start()
    try:
        snapshot, _full = module._source_snapshot(source)
    finally:
        release_writer.set()
        timer.cancel()
        writer.join(timeout=5)

    assert snapshot["read_offset_bytes"] == source.stat().st_size
    assert module.SOURCE_LOCK_TIMEOUT_SECONDS == 5.0  # patched; production default below


def test_timeframe_learning_source_lock_default_bound() -> None:
    module = _load_learning_monitor("timeframe_learning_monitor_default_bound")
    assert module.SOURCE_LOCK_TIMEOUT_SECONDS >= 120.0
    assert module.BUNDLE_LOCK_TIMEOUT_SECONDS == 5.0
