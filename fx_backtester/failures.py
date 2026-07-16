"""Typed fail-closed failure taxonomy shared by the authoritative pipeline.

Unknown, missing, stale, or contradictory state must stop a run with a typed
reason instead of degrading into a default value. Every reason listed here is
part of the public evidence contract: bundles and ledgers persist the enum
value, so renaming a member is a breaking schema change.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class FailureReason(StrEnum):
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"
    STALE = "stale"
    REVISION_CONFLICT = "revision_conflict"
    CLOCK_SKEW = "clock_skew"
    HASH_MISMATCH = "hash_mismatch"
    LINEAGE_BROKEN = "lineage_broken"
    LOCKBOX_VIOLATION = "lockbox_violation"
    DATA_LEAKAGE_DETECTED = "data_leakage_detected"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    COST_MODEL_UNAVAILABLE = "cost_model_unavailable"
    EXECUTION_MODEL_UNAVAILABLE = "execution_model_unavailable"
    PROMOTION_REJECTED = "promotion_rejected"


class TypedFailure(RuntimeError):
    """Fail-closed stop with a machine-readable reason and audit context."""

    def __init__(
        self,
        reason: FailureReason,
        message: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{reason.value}: {message}")
        self.reason = reason
        self.detail = message
        self.context: dict[str, Any] = dict(context or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "detail": self.detail,
            "context": self.context,
        }
