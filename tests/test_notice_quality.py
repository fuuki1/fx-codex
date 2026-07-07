"""Detailed notice quality scoring tests."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, UTC

from fx_intel import notice_quality as nq
from fx_intel.market_structure import OhlcBar

TS = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _entry(direction: str = "long") -> dict:
    return {
        "ts": TS.isoformat(),
        "symbol": "USDJPY",
        "direction": direction,
        "valid_until": (TS + timedelta(hours=4)).isoformat(),
        "price_plan": {
            "current": 162.296,
            "stop": 161.914 if direction == "long" else 162.678,
            "target1": 162.678 if direction == "long" else 161.914,
            "target2": 163.060 if direction == "long" else 161.532,
        },
    }


def _entry_with_levels(direction: str = "long") -> dict:
    entry = _entry(direction)
    if direction == "long":
        entry["entry_level_source"] = {
            "source": "recent_ohlc",
            "pullback_low": 162.20,
            "pullback_high": 162.30,
            "reclaim_level": 162.35,
            "breakout_level": 162.45,
        }
    else:
        entry["entry_level_source"] = {
            "source": "recent_ohlc",
            "pullback_low": 162.55,
            "pullback_high": 162.65,
            "reclaim_level": 162.20,
            "breakout_level": 162.05,
        }
    return entry


def _bar(minutes: int, high: float, low: float, close: float) -> OhlcBar:
    return OhlcBar(
        timestamp=TS + timedelta(minutes=minutes),
        open=162.296,
        high=high,
        low=low,
        close=close,
    )


def test_score_notice_entry_long_hits_t1_before_sl() -> None:
    outcome = nq.score_notice_entry(
        _entry("long"),
        [
            _bar(15, 162.40, 162.20, 162.35),
            _bar(30, 162.70, 162.30, 162.65),
        ],
    )

    assert outcome.outcome == nq.OUTCOME_HIT
    assert outcome.evaluated
    assert outcome.hit
    assert outcome.touched_at == TS + timedelta(minutes=30)
    assert outcome.max_favorable is not None and outcome.max_favorable > 0


def test_score_notice_entry_long_hits_sl_before_t1() -> None:
    outcome = nq.score_notice_entry(_entry("long"), [_bar(15, 162.35, 161.90, 162.00)])

    assert outcome.outcome == nq.OUTCOME_MISS
    assert outcome.evaluated
    assert not outcome.hit


def test_score_notice_entry_same_bar_touch_is_ambiguous() -> None:
    outcome = nq.score_notice_entry(_entry("long"), [_bar(15, 162.70, 161.90, 162.30)])

    assert outcome.outcome == nq.OUTCOME_AMBIGUOUS
    assert not outcome.evaluated
    assert "same bar" in outcome.reason


def test_score_notice_entry_short_uses_inverse_levels() -> None:
    outcome = nq.score_notice_entry(
        _entry("short"),
        [
            _bar(15, 162.40, 162.00, 162.10),
            _bar(30, 162.30, 161.90, 161.95),
        ],
    )

    assert outcome.outcome == nq.OUTCOME_HIT


def test_score_notice_entry_no_touch_keeps_signed_move() -> None:
    outcome = nq.score_notice_entry(_entry("long"), [_bar(15, 162.40, 162.20, 162.36)])

    assert outcome.outcome == nq.OUTCOME_NO_TOUCH
    assert outcome.signed_move == 162.36 - 162.296


def test_score_notice_entry_pullback_trigger_scores_after_entry_confirmation() -> None:
    outcome = nq.score_notice_entry(
        _entry_with_levels("long"),
        [
            _bar(15, 162.34, 162.22, 162.28),
            _bar(30, 162.42, 162.26, 162.36),
            _bar(45, 162.70, 162.40, 162.65),
        ],
    )

    assert outcome.outcome == nq.OUTCOME_HIT
    assert outcome.entry_check == nq.ENTRY_CHECK_TRIGGERED
    assert outcome.entry_scenario == nq.ENTRY_SCENARIO_PULLBACK
    assert outcome.entry_triggered_at == TS + timedelta(minutes=30)
    assert outcome.entry_price == 162.35
    assert outcome.touched_at == TS + timedelta(minutes=45)
    assert outcome.max_favorable == 162.70 - 162.35


def test_score_notice_entry_breakout_trigger_can_miss_after_confirmation() -> None:
    outcome = nq.score_notice_entry(
        _entry_with_levels("long"),
        [
            _bar(15, 162.52, 162.31, 162.46),
            _bar(30, 162.48, 161.90, 162.05),
        ],
    )

    assert outcome.outcome == nq.OUTCOME_MISS
    assert outcome.entry_check == nq.ENTRY_CHECK_TRIGGERED
    assert outcome.entry_scenario == nq.ENTRY_SCENARIO_BREAKOUT
    assert outcome.entry_triggered_at == TS + timedelta(minutes=15)
    assert outcome.entry_price == 162.45


def test_score_notice_entry_no_entry_when_conditions_do_not_trigger() -> None:
    outcome = nq.score_notice_entry(
        _entry_with_levels("long"),
        [
            _bar(15, 162.34, 162.31, 162.32),
            _bar(30, 162.38, 162.31, 162.34),
        ],
    )

    assert outcome.outcome == nq.OUTCOME_NO_ENTRY
    assert not outcome.evaluated
    assert outcome.entry_check == nq.ENTRY_CHECK_NOT_TRIGGERED
    assert "did not trigger" in outcome.reason


def test_score_notice_entry_no_entry_when_stop_hits_before_trigger() -> None:
    outcome = nq.score_notice_entry(
        _entry_with_levels("long"),
        [_bar(15, 162.30, 161.90, 162.28), _bar(30, 162.50, 162.20, 162.46)],
    )

    assert outcome.outcome == nq.OUTCOME_NO_ENTRY
    assert outcome.entry_check == nq.ENTRY_CHECK_NOT_TRIGGERED
    assert "stop touched before entry" in outcome.reason


def test_score_notice_entry_short_breakout_trigger() -> None:
    outcome = nq.score_notice_entry(
        _entry_with_levels("short"),
        [
            _bar(15, 162.25, 162.00, 162.04),
            _bar(30, 162.10, 161.90, 161.95),
        ],
    )

    assert outcome.outcome == nq.OUTCOME_HIT
    assert outcome.entry_check == nq.ENTRY_CHECK_TRIGGERED
    assert outcome.entry_scenario == nq.ENTRY_SCENARIO_BREAKOUT


def test_summarize_outcomes_counts_only_hit_and_miss_as_evaluated() -> None:
    outcomes = [
        nq.NoticeQualityOutcome("USDJPY", TS, "long", nq.OUTCOME_HIT, signed_move=0.2),
        nq.NoticeQualityOutcome("USDJPY", TS, "long", nq.OUTCOME_MISS, signed_move=-0.1),
        nq.NoticeQualityOutcome("USDJPY", TS, "long", nq.OUTCOME_AMBIGUOUS),
        nq.NoticeQualityOutcome("USDJPY", TS, "long", nq.OUTCOME_NO_TOUCH, signed_move=0.05),
        nq.NoticeQualityOutcome(
            "USDJPY",
            TS,
            "long",
            nq.OUTCOME_NO_ENTRY,
            entry_check=nq.ENTRY_CHECK_NOT_TRIGGERED,
        ),
        nq.NoticeQualityOutcome("USDJPY", TS, "long", nq.OUTCOME_SKIPPED),
    ]

    summary = nq.summarize_outcomes(outcomes)

    assert summary.total == 6
    assert summary.evaluated == 2
    assert summary.hits == 1
    assert summary.misses == 1
    assert summary.hit_rate == 0.5
    assert summary.ambiguous == 1
    assert summary.no_touch == 1
    assert summary.no_entry_trigger == 1
    assert summary.entry_checked == 1
    assert summary.entry_triggered == 0
    assert summary.skipped == 1
    assert "勝率50%" in nq.format_summary_ja(summary)
    assert "発火なし1件" in nq.format_summary_ja(summary)


def test_build_quality_report_serializes_summary_and_outcomes() -> None:
    entry = _entry_with_levels("long")
    outcome = nq.score_notice_entry(
        entry,
        [
            _bar(15, 162.52, 162.31, 162.46),
            _bar(30, 162.70, 162.40, 162.65),
        ],
    )

    report = nq.build_quality_report([entry], [outcome], generated_at=TS)

    assert report["schema"] == nq.QUALITY_REPORT_SCHEMA_VERSION
    assert report["generated_at"] == TS.isoformat()
    assert report["summary"]["total"] == 1
    assert report["summary"]["entry_triggered"] == 1
    assert report["outcomes"][0]["outcome"] == nq.OUTCOME_HIT
    assert report["outcomes"][0]["entry_scenario"] == nq.ENTRY_SCENARIO_BREAKOUT
    assert report["outcomes"][0]["entry_price"] == 162.45
    assert report["outcomes"][0]["current"] == 162.296


def test_write_quality_report_json_and_csv(tmp_path) -> None:
    entry = _entry_with_levels("long")
    entry["conviction"] = 52
    entry["report_sha256"] = "abc123"
    outcome = nq.score_notice_entry(
        entry,
        [
            _bar(15, 162.34, 162.22, 162.28),
            _bar(30, 162.42, 162.26, 162.36),
            _bar(45, 162.70, 162.40, 162.65),
        ],
    )
    json_path = tmp_path / "quality.json"
    csv_path = tmp_path / "quality.csv"

    nq.write_quality_report_json(json_path, [entry], [outcome], generated_at=TS)
    nq.write_quality_outcomes_csv(csv_path, [entry], [outcome])

    raw_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert raw_json["summary"]["hits"] == 1
    assert raw_json["outcomes"][0]["report_sha256"] == "abc123"

    rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8").splitlines()))
    assert rows[0]["symbol"] == "USDJPY"
    assert rows[0]["conviction"] == "52"
    assert rows[0]["entry_scenario"] == nq.ENTRY_SCENARIO_PULLBACK
    assert rows[0]["report_sha256"] == "abc123"
