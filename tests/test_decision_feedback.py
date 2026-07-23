"""失敗理由フィードバックの次回判断反映テスト。"""

from __future__ import annotations

from datetime import datetime, UTC

from fx_intel import decision_feedback
from fx_intel.evaluation_labels import (
    DEFAULT_COMMISSION_MODEL_ID,
    DEFAULT_COST_MODEL_ID,
    DEFAULT_COST_MODEL_VERSION,
    DEFAULT_COST_STATUS,
    DEFAULT_SLIPPAGE_MODEL_ID,
    NET_LABEL_PROVENANCE,
    NET_LABEL_VERSION,
)

NOW = datetime(2026, 7, 8, 8, 0, tzinfo=UTC)


def _outcome(
    realized_r: float | None,
    *,
    symbol: str = "USDJPY",
    timeframe: str = "1h",
    direction: str = "long",
    first_touch: str = "sl",
    mfe_r: float | None = 0.2,
    mae_r: float | None = 1.0,
    tradable: bool = True,
    reason_key: str = "sl_first",
    label_ja: str = "SL先着",
    learning_context: dict | None = None,
    quality_flags: list[str] | None = None,
    index: int = 0,
) -> dict:
    reasons = []
    if reason_key:
        reasons.append(
            {
                "key": reason_key,
                "label_ja": label_ja,
                "advice_ja": "test advice",
                "evidence": {},
            }
        )
    realized_net_r = realized_r - 0.2 if realized_r is not None else None
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "mode": "per_timeframe" if timeframe != "fusion" else "fusion",
        "direction": direction,
        "realized_r": realized_r,
        "gross_realized_r": realized_r,
        "quote_realized_r": realized_net_r,
        "realized_net_r": realized_net_r,
        "net_label_eligible": realized_net_r is not None,
        "decision_id": f"decision-{symbol}-{timeframe}-{direction}-{index}",
        "label_version": NET_LABEL_VERSION,
        "label_provenance": NET_LABEL_PROVENANCE,
        "entry_bid": 99.9,
        "entry_ask": 100.1,
        "planned_risk_distance": 1.0,
        "entry_spread_r": 0.2,
        "slippage_r": 0.0,
        "commission_r": 0.0,
        "financing_r": 0.0,
        "additional_cost_r": 0.0,
        "execution_cost_r": 0.2,
        "cost_model_id": DEFAULT_COST_MODEL_ID,
        "cost_model_version": DEFAULT_COST_MODEL_VERSION,
        "cost_status": DEFAULT_COST_STATUS,
        "entry_quote_source": "fixture",
        "spread_source": "fixture",
        "slippage_model_id": DEFAULT_SLIPPAGE_MODEL_ID,
        "commission_model_id": DEFAULT_COMMISSION_MODEL_ID,
        "cost_quality_flags": [],
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "first_touch": first_touch,
        "tradable": tradable,
        "path_quality": 0.8 if tradable else 0.2,
        "quality_flags": quality_flags or [],
        "learning_context": learning_context or {},
        "failure_reasons": reasons,
        "primary_failure_reason": reason_key or None,
    }


def test_decision_feedback_blocks_repeated_negative_sl_cell() -> None:
    report = {"outcomes": [_outcome(-1.0, index=index) for index in range(20)]}

    profile = decision_feedback.derive_decision_feedback(report, now=NOW)
    cell = profile.cell_for("USDJPY", "1h", "long")

    assert cell is not None
    assert cell.action == "avoid"
    assert cell.block is True
    assert cell.factor == decision_feedback.BLOCK_FACTOR
    assert cell.gross_expectancy_r == -1.0
    assert cell.net_expectancy_r == -1.2
    assert cell.sl_rate == 1.0
    assert cell.failure_reasons[0].key == "sl_first"

    adjuster = profile.expectancy_lookup("USDJPY", "1h")
    assert adjuster is not None
    factor, reason, block = adjuster("USDJPY", "long", 80)
    assert factor == decision_feedback.BLOCK_FACTOR
    assert block is True
    assert "見送り優先" in reason


def test_decision_feedback_dampens_tp_too_far_without_blocking() -> None:
    report = {
        "outcomes": [
            _outcome(
                -0.1,
                first_touch="none",
                mfe_r=0.9,
                mae_r=0.2,
                reason_key="tp_too_far",
                label_ja="TPが遠い/利確未達",
                index=index,
            )
            for index in range(8)
        ]
    }

    profile = decision_feedback.derive_decision_feedback(report, now=NOW)
    cell = profile.cell_for("USDJPY", "1h", "long")

    assert cell is not None
    assert cell.action == "dampen"
    assert cell.block is False
    assert cell.factor == decision_feedback.WATCH_FACTOR


def test_decision_feedback_fusion_adjuster_uses_fusion_cell() -> None:
    report = {"outcomes": [_outcome(-1.0, timeframe="fusion", index=index) for index in range(20)]}

    profile = decision_feedback.derive_decision_feedback(report, now=NOW)
    adjuster = profile.fusion_adjuster()

    assert adjuster is not None
    factor, reason, block = adjuster("USDJPY", "long", 70)
    assert factor == decision_feedback.BLOCK_FACTOR
    assert block is True
    assert "USDJPY fusion ロング" in reason


def test_monitoring_snapshot_reports_model_expected_r_delta() -> None:
    report = {
        "summary": {
            "overall": {
                "evaluated": 3,
                "tradable": 3,
                "sample_ok": False,
                "min_samples": 20,
                "expectancy_r": 1.1667,
            }
        },
        "outcomes": [
            _outcome(1.0, reason_key="", index=1),
            _outcome(0.5, reason_key="", index=2),
            _outcome(
                2.0,
                reason_key="",
                learning_context={"decision_feedback": {"action": "dampen"}},
                index=3,
            ),
        ],
    }

    profile = decision_feedback.derive_decision_feedback(report, now=NOW)
    monitor = decision_feedback.build_monitoring_snapshot(profile, report, now=NOW)
    delta = monitor["summary"]["model_expectancy_delta"]

    assert delta["status"] == "ready"
    assert delta["baseline_model"]["expected_R"] == 0.55
    assert delta["learning_model"]["expected_R"] == 1.8
    assert delta["delta_expected_R"] == 1.25
