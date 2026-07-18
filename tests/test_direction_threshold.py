from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json

import pytest

from fx_intel import direction_threshold as dt

NOW = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)


def _outcomes(count: int = 300, *, losing: bool = False) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(count):
        strong = index % 2 == 0
        composite = 0.28 if strong else 0.18
        net_r = (-0.4 if losing else 0.55) if strong else -0.35
        rows.append(
            {
                "ts": (NOW - timedelta(hours=8 * (count - index))).isoformat(),
                "decision_id": f"decision-{index}",
                "symbol": "USDJPY",
                "direction": "long",
                "composite": composite,
                "realized_r": net_r + 0.1,
                "realized_net_r": net_r + (index % 5) * 0.01,
                "tradable": True,
                "net_label_eligible": True,
                "label_version": "net-r-v1",
                "cost_model_id": "quotes-v1",
            }
        )
    return rows


def test_candidate_evaluation_only_promotes_stricter_profitable_threshold() -> None:
    policy = dt.evaluate_threshold_candidates(_outcomes(), now=NOW)
    assert policy.threshold >= dt.DEFAULT_THRESHOLD
    assert policy.threshold == pytest.approx(0.25)
    assert policy.stage == "ready_for_review"
    assert policy.oos_net_r_lcb is not None and policy.oos_net_r_lcb > 0
    assert policy.effective_samples >= dt.DEFAULT_MIN_TEST_SAMPLES


def test_policy_requires_human_approval_and_activation(tmp_path) -> None:
    candidate = dt.evaluate_threshold_candidates(_outcomes(), now=NOW)
    assert dt.effective_threshold(candidate, now=NOW) == dt.DEFAULT_THRESHOLD
    approved = dt.approve_policy(candidate, "risk-owner", now=NOW)
    assert dt.effective_threshold(approved, now=NOW) == dt.DEFAULT_THRESHOLD
    active = dt.activate_policy(approved, now=NOW)
    assert dt.effective_threshold(active, now=NOW) == pytest.approx(candidate.threshold)
    assert dt.effective_threshold(active, now=NOW + timedelta(days=91)) == dt.DEFAULT_THRESHOLD

    path = tmp_path / "threshold.json"
    dt.save_policy(active, path)
    loaded = dt.load_policy(path)
    assert loaded == active


def test_policy_load_and_validation_fail_closed(tmp_path) -> None:
    path = tmp_path / "threshold.json"
    path.write_text("{broken", encoding="utf-8")
    assert dt.load_policy(path) is None
    assert dt.effective_threshold(None) == dt.DEFAULT_THRESHOLD

    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "policy_id": "unsafe",
                "stage": "active",
                "threshold": 0.05,
                "fallback_threshold": 0.15,
                "scope": "overall",
            }
        ),
        encoding="utf-8",
    )
    assert dt.load_policy(path) is None


def test_active_policy_auto_pauses_when_recent_net_r_lcb_is_nonpositive() -> None:
    candidate = dt.evaluate_threshold_candidates(_outcomes(), now=NOW)
    active = dt.activate_policy(dt.approve_policy(candidate, "risk-owner", now=NOW), now=NOW)
    paused = dt.auto_pause_policy(active, _outcomes(losing=True))
    assert paused.stage == "auto_paused"
    assert dt.effective_threshold(paused, now=NOW) == dt.DEFAULT_THRESHOLD


def test_mixed_label_accounting_is_rejected() -> None:
    rows = _outcomes()
    rows[-1]["label_version"] = "net-r-v2"
    with pytest.raises(ValueError, match="混在"):
        dt.evaluate_threshold_candidates(rows, now=NOW)
