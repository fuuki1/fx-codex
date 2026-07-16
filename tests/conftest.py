"""Shared pytest safety fixtures.

Tests exercise the same CLI entry points used by the local paper workflow.  Keep
every mutable runtime artifact in the test's temporary directory so a missing
per-test patch can never append synthetic decisions to ``logs/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fx_briefing

_RUNTIME_ARTIFACTS = {
    "DEFAULT_EVENTS_CSV": ("research_pack", "upcoming_events.csv"),
    "DEFAULT_EVENTS_ARCHIVE": ("research_pack", "event_history.csv"),
    "DEFAULT_JOURNAL_PATH": ("logs", "briefing_journal.jsonl"),
    "DEFAULT_LEARNING_PATH": ("logs", "briefing_learning.json"),
    "DEFAULT_TF_JOURNAL_PATH": ("logs", "briefing_tf_journal.jsonl"),
    "DEFAULT_TF_LEARNING_PATH": ("logs", "briefing_tf_learning.json"),
    "DEFAULT_TP_SL_LEARNING_PATH": ("logs", "briefing_tp_sl_learning.json"),
    "DEFAULT_MAXIMIZATION_PATH": ("logs", "briefing_maximization.json"),
    "DEFAULT_TF_PRICES_PATH": ("logs", "briefing_tf_prices.jsonl"),
    "DEFAULT_CALENDAR_CACHE": ("logs", "calendar_cache.json"),
    "DEFAULT_MACRO_CACHE": ("logs", "macro_cache.json"),
    "DEFAULT_ML_MODEL_PATH": ("logs", "ml_model.json"),
    "DEFAULT_PROMOTION_STATE": ("logs", "promotion_state.json"),
    "DEFAULT_TRADE_IMPROVEMENT_REGISTRY": ("logs", "trade_improvement_candidates.json"),
    "DEFAULT_TRADE_MONITOR_PATH": ("logs", "trade_outcome_monitor.json"),
    "DEFAULT_DECISION_LOG_PATH": ("logs", "briefing_decisions.jsonl"),
    "DEFAULT_DECISION_LATEST_PATH": ("logs", "briefing_decisions_latest.json"),
    "DEFAULT_DECISION_OUTCOMES_PATH": ("logs", "briefing_decision_outcomes.json"),
    "DEFAULT_DECISION_FEEDBACK_PATH": ("logs", "briefing_decision_feedback.json"),
}


@pytest.fixture(autouse=True)
def isolated_fx_briefing_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect all fx_briefing runtime writes away from operational data."""

    for attribute, relative_parts in _RUNTIME_ARTIFACTS.items():
        monkeypatch.setattr(fx_briefing, attribute, tmp_path.joinpath(*relative_parts))
    return tmp_path
