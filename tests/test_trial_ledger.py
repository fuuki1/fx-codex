"""Append-only trial ledger: chain integrity, tamper detection, no overwrite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fx_backtester.failures import FailureReason, TypedFailure
from fx_backtester.trial_ledger import TrialLedger, TrialLedgerEntry


def _entry(trial_id: str, *, status: str = "succeeded", **overrides: object) -> TrialLedgerEntry:
    payload: dict[str, object] = {
        "trial_id": trial_id,
        "experiment_id": "exp-ledger-test",
        "parent_trial_id": None,
        "started_at": "2026-07-12T00:00:00+00:00",
        "finished_at": "2026-07-12T00:00:01+00:00",
        "status": status,
        "git_commit": "a" * 40,
        "manifest_hash": "1" * 64,
        "dataset_hash": "2" * 64,
        "feature_hash": "3" * 64,
        "label_hash": "4" * 64,
        "split_hash": "5" * 64,
        "model_family": "logistic_ridge",
        "hyperparameters": {"ridge": 1.0},
        "seed": 42,
        "metrics": {"trade_count": 10},
        "cost_metrics": None,
        "failure_reason": {"reason": "invalid", "detail": "x"} if status == "failed" else None,
        "selected": False,
        "extra": {"candidate_id": trial_id.split(":")[0]},
    }
    payload.update(overrides)
    return TrialLedgerEntry(**payload)  # type: ignore[arg-type]


@pytest.fixture()
def ledger(tmp_path: Path) -> TrialLedger:
    return TrialLedger(tmp_path / "ledger.jsonl")


class TestAppend:
    def test_round_trip_and_audit(self, ledger: TrialLedger) -> None:
        ledger.append(_entry("cand-a:run1"))
        ledger.append(_entry("cand-b:run1", status="failed"))
        audit = ledger.verify()
        assert audit.entry_count == 2
        assert audit.experiment_ids == ("exp-ledger-test",)
        entries = ledger.entries()
        assert [entry["trial_id"] for entry in entries] == ["cand-a:run1", "cand-b:run1"]
        assert entries[1]["status"] == "failed"
        assert entries[1]["failure_reason"] is not None

    def test_duplicate_trial_id_rejected(self, ledger: TrialLedger) -> None:
        ledger.append(_entry("cand-a:run1"))
        with pytest.raises(TypedFailure) as excinfo:
            ledger.append(_entry("cand-a:run1"))
        assert excinfo.value.reason is FailureReason.INVALID

    def test_failed_trial_requires_reason(self, ledger: TrialLedger) -> None:
        with pytest.raises(TypedFailure) as excinfo:
            _entry("cand-a:run1", status="failed", failure_reason=None)
        assert excinfo.value.reason is FailureReason.INCOMPLETE

    def test_naive_timestamp_rejected(self, ledger: TrialLedger) -> None:
        with pytest.raises(TypedFailure):
            _entry("cand-a:run1", started_at="2026-07-12T00:00:00")

    def test_distinct_candidate_count(self, ledger: TrialLedger) -> None:
        ledger.append(_entry("cand-a:run1"))
        ledger.append(_entry("cand-b:run1"))
        ledger.append(_entry("cand-a:run2"))
        assert ledger.distinct_candidate_count("exp-ledger-test") == 2


class TestTamperDetection:
    def test_edited_line_detected(self, ledger: TrialLedger) -> None:
        ledger.append(_entry("cand-a:run1"))
        ledger.append(_entry("cand-b:run1"))
        lines = ledger.path.read_text("utf-8").splitlines()
        record = json.loads(lines[0])
        record["entry"]["metrics"] = {"trade_count": 999}
        lines[0] = json.dumps(record, sort_keys=True, ensure_ascii=True)
        ledger.path.write_text("\n".join(lines) + "\n", "utf-8")
        with pytest.raises(TypedFailure) as excinfo:
            ledger.verify()
        assert excinfo.value.reason is FailureReason.HASH_MISMATCH

    def test_deleted_middle_line_detected(self, ledger: TrialLedger) -> None:
        for name in ("cand-a:run1", "cand-b:run1", "cand-c:run1"):
            ledger.append(_entry(name))
        lines = ledger.path.read_text("utf-8").splitlines()
        del lines[1]
        ledger.path.write_text("\n".join(lines) + "\n", "utf-8")
        with pytest.raises(TypedFailure) as excinfo:
            ledger.verify()
        assert excinfo.value.reason is FailureReason.LINEAGE_BROKEN

    def test_tail_truncation_detected_via_head(self, ledger: TrialLedger) -> None:
        ledger.append(_entry("cand-a:run1"))
        ledger.append(_entry("cand-b:run1"))
        lines = ledger.path.read_text("utf-8").splitlines()
        ledger.path.write_text(lines[0] + "\n", "utf-8")
        with pytest.raises(TypedFailure) as excinfo:
            ledger.verify()
        assert excinfo.value.reason is FailureReason.LINEAGE_BROKEN

    def test_missing_head_sidecar_detected(self, ledger: TrialLedger) -> None:
        ledger.append(_entry("cand-a:run1"))
        ledger.head_path.unlink()
        with pytest.raises(TypedFailure) as excinfo:
            ledger.verify()
        assert excinfo.value.reason is FailureReason.LINEAGE_BROKEN

    def test_missing_ledger_is_unavailable(self, tmp_path: Path) -> None:
        with pytest.raises(TypedFailure) as excinfo:
            TrialLedger(tmp_path / "absent.jsonl").verify()
        assert excinfo.value.reason is FailureReason.UNAVAILABLE
