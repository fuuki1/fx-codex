"""Quality-state classification and dataset lineage registry."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from data_platform.lineage.dataset_registry import (
    DatasetManifest,
    DatasetRegistry,
    DatasetRegistryError,
    SourceLineageEntry,
)
from data_platform.quality.state import (
    QualityState,
    QualityThresholds,
    classify_quality,
)


def _t() -> datetime:
    return datetime(2026, 7, 13, 9, 0, tzinfo=UTC)


class TestQualityState:
    def test_unavailable_short_circuits(self) -> None:
        assessment = classify_quality(
            available=False,
            hard_violation_counts={},
            freshness_seconds=None,
            completeness=None,
        )
        assert assessment.state is QualityState.UNAVAILABLE

    def test_hard_violation_quarantines(self) -> None:
        assessment = classify_quality(
            available=True,
            hard_violation_counts={"bid_gt_ask": 3},
            freshness_seconds=1.0,
            completeness=1.0,
        )
        assert assessment.state is QualityState.QUARANTINED
        assert "bid_gt_ask" in assessment.hard_violations

    def test_clean_and_in_slo_is_usable(self) -> None:
        assessment = classify_quality(
            available=True,
            hard_violation_counts={"duplicate_natural_key": 0},
            freshness_seconds=5.0,
            completeness=0.9999,
        )
        assert assessment.state is QualityState.USABLE
        assert assessment.is_usable

    def test_soft_slo_miss_degrades(self) -> None:
        assessment = classify_quality(
            available=True,
            hard_violation_counts={},
            freshness_seconds=120.0,  # beyond 30s
            completeness=0.9999,
        )
        assert assessment.state is QualityState.DEGRADED
        assert "freshness_seconds" in assessment.soft_violations

    def test_unmeasured_metric_is_not_treated_as_passing(self) -> None:
        # A None freshness must degrade, never silently count as fresh.
        assessment = classify_quality(
            available=True,
            hard_violation_counts={},
            freshness_seconds=None,
            completeness=1.0,
        )
        assert assessment.state is QualityState.DEGRADED
        assert "freshness_seconds" in assessment.unmeasured

    def test_unknown_hard_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown hard-violation keys"):
            classify_quality(
                available=True,
                hard_violation_counts={"made_up": 1},
                freshness_seconds=1.0,
                completeness=1.0,
            )

    def test_thresholds_require_rationale(self) -> None:
        with pytest.raises(ValueError, match="rationale"):
            QualityThresholds(rationale="short")


class TestDatasetRegistry:
    def _manifest(
        self, dataset_id: str = "usdjpy-2026-07", dataset_sha: str = "d" * 64
    ) -> DatasetManifest:
        return DatasetManifest(
            dataset_id=dataset_id,
            instrument="USDJPY",
            as_of=_t(),
            sources=(
                SourceLineageEntry(
                    source_id="broker_primary",
                    raw_sha256="a" * 64,
                    available_at_cutoff=_t(),
                    record_count=1000,
                ),
            ),
            dataset_sha256=dataset_sha,
            writer_id="collector-1",
        )

    def test_register_and_get(self, tmp_path: Path) -> None:
        registry = DatasetRegistry(tmp_path / "registry.jsonl")
        registry.register(self._manifest())
        stored = registry.get("usdjpy-2026-07")
        assert stored["manifest"]["instrument"] == "USDJPY"

    def test_reregister_identical_is_idempotent(self, tmp_path: Path) -> None:
        registry = DatasetRegistry(tmp_path / "registry.jsonl")
        first = registry.register(self._manifest())
        second = registry.register(self._manifest())
        assert first == second
        # Only one line written.
        assert len((tmp_path / "registry.jsonl").read_text().splitlines()) == 1

    def test_rewriting_lineage_is_rejected(self, tmp_path: Path) -> None:
        registry = DatasetRegistry(tmp_path / "registry.jsonl")
        registry.register(self._manifest(dataset_sha="d" * 64))
        with pytest.raises(DatasetRegistryError, match="cannot be rewritten"):
            registry.register(self._manifest(dataset_sha="e" * 64))

    def test_verify_replay_detects_drift(self, tmp_path: Path) -> None:
        registry = DatasetRegistry(tmp_path / "registry.jsonl")
        registry.register(self._manifest(dataset_sha="d" * 64))
        assert registry.verify_replay("usdjpy-2026-07", "d" * 64) is True
        assert registry.verify_replay("usdjpy-2026-07", "f" * 64) is False

    def test_manifest_requires_a_source(self) -> None:
        with pytest.raises(DatasetRegistryError, match="at least one source"):
            DatasetManifest(
                dataset_id="x",
                instrument="USDJPY",
                as_of=_t(),
                sources=(),
                dataset_sha256="d" * 64,
                writer_id="w",
            )
