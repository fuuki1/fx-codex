"""fx_briefing のML自動再学習判定のテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest
import requests

import fx_briefing
from fx_briefing import (
    ML_RETRAIN_DAYS,
    _prediction_time_after_acquisition,
    _resolve_run_slot,
    ml_needs_retrain,
)
from fx_intel.gbm import GradientBoostingClassifier
from fx_intel.ml import MLArtifact

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


def _artifact_with_model(trained_at: str) -> MLArtifact:
    artifact = MLArtifact(trained_at=trained_at)
    artifact.model = GradientBoostingClassifier()
    return artifact


def test_retrain_when_model_missing() -> None:
    assert ml_needs_retrain(MLArtifact(), NOW)


def test_retrain_when_trained_at_invalid() -> None:
    assert ml_needs_retrain(_artifact_with_model("not-a-date"), NOW)


def test_retrain_when_stale() -> None:
    old = (NOW - timedelta(days=ML_RETRAIN_DAYS, hours=1)).isoformat()
    assert ml_needs_retrain(_artifact_with_model(old), NOW)


def test_no_retrain_when_fresh() -> None:
    fresh = (NOW - timedelta(days=1)).isoformat()
    assert not ml_needs_retrain(_artifact_with_model(fresh), NOW)


def test_prediction_time_is_after_all_input_acquisition() -> None:
    completed = NOW + timedelta(minutes=3)
    assert _prediction_time_after_acquisition(NOW, completed_at=completed) == completed


def test_prediction_time_rejects_naive_or_backwards_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _prediction_time_after_acquisition(datetime(2026, 7, 3, 12, 0), completed_at=NOW)
    with pytest.raises(RuntimeError, match="backwards"):
        _prediction_time_after_acquisition(NOW, completed_at=NOW - timedelta(seconds=1))


def test_run_slot_is_fixed_before_acquisition_and_retry_reuses_explicit_slot() -> None:
    started = datetime(2026, 7, 3, 12, 4, 59, tzinfo=UTC)
    acquired_later = started + timedelta(minutes=6)

    implicit = _resolve_run_slot(None, started)
    assert implicit == datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
    assert _resolve_run_slot(implicit.isoformat(), acquired_later) == implicit


@pytest.mark.parametrize(
    "invalid",
    [
        "2026-07-03T12:01:00+00:00",
        "2026-07-03T12:00:01+00:00",
        "2026-07-03T12:00:00",
        "not-a-timestamp",
    ],
)
def test_run_slot_rejects_unaligned_ambiguous_or_invalid_input(invalid: str) -> None:
    with pytest.raises(ValueError, match="run-slot"):
        _resolve_run_slot(invalid, NOW)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), True])
def test_realized_expectancy_contract_rejects_nonfinite_or_bool(invalid: object) -> None:
    summary = {
        "by_symbol_direction": {
            "USDJPY:long": {
                "evidence_schema": 2,
                "sample_ok": True,
                "net_of_costs": True,
                "independent_test": True,
                "label_version": "v1",
                "expectancy_r_ci_lower": 0.1,
                "expectancy_r": invalid,
            }
        }
    }

    assert fx_briefing._realized_expectancy_r(summary, "USDJPY", "long") is None


def test_discord_network_error_never_exposes_webhook_token(monkeypatch) -> None:
    secret = "super-secret-webhook-token"

    def fail(*_args, **_kwargs):
        raise requests.ConnectionError(f"failed URL /api/webhooks/1/{secret}")

    monkeypatch.setattr(fx_briefing.requests, "post", fail)
    with pytest.raises(RuntimeError) as captured:
        fx_briefing.post_to_discord(f"https://discord.com/api/webhooks/1/{secret}", {})

    assert secret not in str(captured.value)
    assert "ConnectionError" in str(captured.value)
