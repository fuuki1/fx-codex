from __future__ import annotations

import fx_briefing


def test_naked_or_single_sample_expectancy_is_not_valid_order_evidence() -> None:
    summary = {
        "by_symbol_direction": {
            "USDJPY:long": {
                "tradable": 1,
                "sample_ok": False,
                "expectancy_r": 2.0,
            }
        }
    }

    assert fx_briefing._realized_expectancy_r(summary, "USDJPY", "long") is None


def test_only_complete_positive_ci_net_oos_evidence_is_accepted() -> None:
    cell = {
        "evidence_schema": 2,
        "sample_ok": True,
        "net_of_costs": True,
        "independent_test": True,
        "label_version": "triple-barrier-v2",
        "expectancy_r": 0.18,
        "expectancy_r_ci_lower": 0.03,
    }
    summary = {"by_symbol_direction": {"USDJPY:long": cell}}
    assert fx_briefing._realized_expectancy_r(summary, "USDJPY", "long") == 0.18

    cell["expectancy_r_ci_lower"] = -0.01
    assert fx_briefing._realized_expectancy_r(summary, "USDJPY", "long") is None
