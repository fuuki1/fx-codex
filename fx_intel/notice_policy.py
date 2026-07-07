"""Detailed trade notice policy knobs.

The values here are presentation and execution-guidance policy, not signal
generation.  Keeping them outside briefing.build_trade_plan prevents Discord
copy changes from changing the underlying directional decision.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NoticePolicy:
    """Configuration for the human-facing detailed trade notice."""

    low_conviction_max: int = 55
    strong_conviction_min: int = 70
    default_valid_hours: float = 4.0
    no_entry_minutes_before_event: int = 30
    no_entry_minutes_after_event: int = 15
    pullback_atr_fraction: float = 0.65
    reclaim_atr_fraction: float = 0.35
    breakout_atr_fraction: float = 1.0
    risk_pct_min: float = 0.25
    risk_pct_max: float = 0.5
    example_account_jpy: int = 100_000
    jpy_intervention_warning_level: float = 160.0
