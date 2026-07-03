"""fx_briefing のML自動再学習判定のテスト(ネットワーク不要)。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

from fx_briefing import ML_RETRAIN_DAYS, ml_needs_retrain
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
