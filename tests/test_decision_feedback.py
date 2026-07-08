"""失敗理由フィードバックの次回判断反映テスト。"""

from __future__ import annotations

from datetime import datetime, UTC

from fx_intel import decision_feedback

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
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "mode": "per_timeframe" if timeframe != "fusion" else "fusion",
        "direction": direction,
        "realized_r": realized_r,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "first_touch": first_touch,
        "tradable": tradable,
        "path_quality": 0.8 if tradable else 0.2,
        "failure_reasons": reasons,
        "primary_failure_reason": reason_key or None,
    }


def test_decision_feedback_blocks_repeated_negative_sl_cell() -> None:
    report = {"outcomes": [_outcome(-1.0) for _ in range(20)]}

    profile = decision_feedback.derive_decision_feedback(report, now=NOW)
    cell = profile.cell_for("USDJPY", "1h", "long")

    assert cell is not None
    assert cell.action == "avoid"
    assert cell.block is True
    assert cell.factor == decision_feedback.BLOCK_FACTOR
    assert cell.expectancy_r == -1.0
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
            )
            for _ in range(8)
        ]
    }

    profile = decision_feedback.derive_decision_feedback(report, now=NOW)
    cell = profile.cell_for("USDJPY", "1h", "long")

    assert cell is not None
    assert cell.action == "dampen"
    assert cell.block is False
    assert cell.factor == decision_feedback.WATCH_FACTOR


def test_decision_feedback_fusion_adjuster_uses_fusion_cell() -> None:
    report = {"outcomes": [_outcome(-1.0, timeframe="fusion") for _ in range(20)]}

    profile = decision_feedback.derive_decision_feedback(report, now=NOW)
    adjuster = profile.fusion_adjuster()

    assert adjuster is not None
    factor, reason, block = adjuster("USDJPY", "long", 70)
    assert factor == decision_feedback.BLOCK_FACTOR
    assert block is True
    assert "USDJPY fusion ロング" in reason
