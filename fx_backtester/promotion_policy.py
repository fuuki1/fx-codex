"""Promotion policy configuration: thresholds are mandate choices, not constants.

A policy file is strict JSON whose keys are exactly the ``PromotionPolicy``
fields. A written rationale is mandatory, unknown keys are rejected, and this
build refuses any policy that tries to enable limited_live or live. Numeric
thresholds are validated for shape only — no value here is an industry
standard, and lowering a threshold because current data fails it is a
governance decision that belongs in review, not in code.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from fx_backtester.failures import FailureReason, TypedFailure
from fx_backtester.governance import PromotionPolicy

_INT_FIELDS = frozenset(
    {
        "min_samples",
        "min_regimes",
        "min_pairs",
        "min_shadow_days_for_paper",
        "min_paper_days_for_limited_live",
    }
)
_FLOAT_FIELDS = frozenset(
    {
        "min_net_expectancy_r",
        "min_expectancy_ci_lower_r",
        "min_dsr_probability",
        "max_pbo_probability",
        "max_drawdown_pct",
        "min_brier_improvement",
        "min_cost_stress_2x_expectancy_r",
    }
)
_BOOL_FIELDS = frozenset({"allow_limited_live", "allow_live"})
_ALLOWED_KEYS = _INT_FIELDS | _FLOAT_FIELDS | _BOOL_FIELDS | {"rationale"}


def load_promotion_policy(path: str | Path) -> PromotionPolicy:
    policy_path = Path(path)
    if not policy_path.is_file():
        raise TypedFailure(
            FailureReason.UNAVAILABLE,
            "promotion policy file does not exist",
            context={"path": str(policy_path)},
        )
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TypedFailure(
            FailureReason.INVALID,
            "promotion policy is not valid JSON",
            context={"path": str(policy_path)},
        ) from error
    return parse_promotion_policy(payload)


def parse_promotion_policy(payload: Any) -> PromotionPolicy:
    if not isinstance(payload, dict):
        raise TypedFailure(FailureReason.INVALID, "promotion policy must be a JSON object")
    unknown = sorted(set(payload) - _ALLOWED_KEYS)
    if unknown:
        raise TypedFailure(
            FailureReason.INVALID,
            "promotion policy has unknown keys",
            context={"unknown_keys": unknown},
        )
    missing = sorted(_ALLOWED_KEYS - set(payload))
    if missing:
        raise TypedFailure(
            FailureReason.INCOMPLETE,
            "promotion policy is missing required keys; every threshold must be "
            "an explicit reviewed choice",
            context={"missing_keys": missing},
        )
    rationale = payload["rationale"]
    if not isinstance(rationale, str) or len(rationale.strip()) < 20:
        raise TypedFailure(
            FailureReason.INVALID,
            "promotion policy requires a substantive written rationale",
        )
    values: dict[str, Any] = {"rationale": rationale.strip()}
    for name in _INT_FIELDS:
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypedFailure(
                FailureReason.INVALID,
                f"policy field {name} must be a non-negative integer",
                context={"observed": value},
            )
        values[name] = value
    for name in _FLOAT_FIELDS:
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypedFailure(
                FailureReason.INVALID,
                f"policy field {name} must be a number",
                context={"observed": value},
            )
        number = float(value)
        if not math.isfinite(number):
            raise TypedFailure(FailureReason.INVALID, f"policy field {name} must be finite")
        values[name] = number
    for name in _BOOL_FIELDS:
        value = payload[name]
        if not isinstance(value, bool):
            raise TypedFailure(FailureReason.INVALID, f"policy field {name} must be a boolean")
        values[name] = value
    if values["allow_limited_live"] or values["allow_live"]:
        raise TypedFailure(
            FailureReason.PROMOTION_REJECTED,
            "this build cannot accept a policy that enables limited_live or live",
        )
    if not 0.0 < values["min_dsr_probability"] <= 1.0:
        raise TypedFailure(FailureReason.INVALID, "min_dsr_probability must be inside (0, 1]")
    if not 0.0 <= values["max_pbo_probability"] <= 1.0:
        raise TypedFailure(FailureReason.INVALID, "max_pbo_probability must be inside [0, 1]")
    return PromotionPolicy(**values)
