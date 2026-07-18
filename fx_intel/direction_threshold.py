"""Governed abstention threshold policy for analysis-only FX decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, UTC
import hashlib
import json
import math
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_THRESHOLD = 0.15
# Phase D4 only permits stricter challengers. Lowering the threshold increases
# signal frequency and requires a separate future governance design.
DEFAULT_CANDIDATES = (0.15, 0.20, 0.25, 0.30)
DEFAULT_MIN_TEST_SAMPLES = 20
EMBARGO_HOURS = 72.0
POLICY_VALID_DAYS = 90
ACTIVE_STAGE = "active"
VALID_STAGES = {
    "candidate",
    "shadow",
    "ready_for_review",
    "approved",
    ACTIVE_STAGE,
    "rejected",
    "auto_paused",
}


@dataclass(frozen=True)
class ThresholdPolicy:
    policy_id: str = ""
    stage: str = "candidate"
    threshold: float = DEFAULT_THRESHOLD
    fallback_threshold: float = DEFAULT_THRESHOLD
    scope: str = "overall"
    label_version: str = ""
    cost_model_id: str = ""
    dataset_hash: str = ""
    generated_at: str = ""
    train_end: str = ""
    test_end: str = ""
    effective_samples: int = 0
    oos_mean_net_r: float | None = None
    oos_net_r_lcb: float | None = None
    dsr: float | None = None
    max_drawdown_r: float | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    activated_at: str | None = None
    expires_at: str | None = None
    auto_pause_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"schema": SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ThresholdPolicy:
        if _int(payload.get("schema")) != SCHEMA_VERSION:
            raise ValueError("threshold policy schema不一致")
        policy = cls(
            policy_id=str(payload.get("policy_id", "")),
            stage=str(payload.get("stage", "candidate")),
            threshold=_float(payload.get("threshold")) or DEFAULT_THRESHOLD,
            fallback_threshold=_float(payload.get("fallback_threshold")) or DEFAULT_THRESHOLD,
            scope=str(payload.get("scope", "overall")),
            label_version=str(payload.get("label_version", "")),
            cost_model_id=str(payload.get("cost_model_id", "")),
            dataset_hash=str(payload.get("dataset_hash", "")),
            generated_at=str(payload.get("generated_at", "")),
            train_end=str(payload.get("train_end", "")),
            test_end=str(payload.get("test_end", "")),
            effective_samples=_int(payload.get("effective_samples")),
            oos_mean_net_r=_float(payload.get("oos_mean_net_r")),
            oos_net_r_lcb=_float(payload.get("oos_net_r_lcb")),
            dsr=_float(payload.get("dsr")),
            max_drawdown_r=_float(payload.get("max_drawdown_r")),
            approved_by=_optional_text(payload.get("approved_by")),
            approved_at=_optional_text(payload.get("approved_at")),
            activated_at=_optional_text(payload.get("activated_at")),
            expires_at=_optional_text(payload.get("expires_at")),
            auto_pause_reason=_optional_text(payload.get("auto_pause_reason")),
        )
        _validate_policy(policy)
        return policy


def effective_threshold(policy: ThresholdPolicy | None, *, now: datetime | None = None) -> float:
    """Fail closed to 0.15 unless a fully valid active policy is supplied."""

    if policy is None or policy.stage != ACTIVE_STAGE:
        return DEFAULT_THRESHOLD
    try:
        _validate_policy(policy)
    except ValueError:
        return DEFAULT_THRESHOLD
    if not policy.approved_by or not policy.approved_at or not policy.activated_at:
        return DEFAULT_THRESHOLD
    expires_at = _parse_ts(policy.expires_at)
    if expires_at is None or _utc(now or datetime.now(UTC)) >= expires_at:
        return DEFAULT_THRESHOLD
    return policy.threshold


def load_policy(path: str | Path) -> ThresholdPolicy | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return None
        return ThresholdPolicy.from_mapping(payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def save_policy(policy: ThresholdPolicy, path: str | Path) -> None:
    _validate_policy(policy)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(policy.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(target)


def evaluate_threshold_candidates(
    outcomes: Sequence[Mapping[str, object]],
    *,
    candidates: Sequence[float] = DEFAULT_CANDIDATES,
    now: datetime | None = None,
    min_test_samples: int = DEFAULT_MIN_TEST_SAMPLES,
) -> ThresholdPolicy:
    now = _utc(now or datetime.now(UTC))
    rows = _eligible_rows(outcomes)
    if not rows:
        return ThresholdPolicy(
            policy_id=_policy_id(now, DEFAULT_THRESHOLD, "empty"),
            stage="shadow",
            generated_at=now.isoformat(),
        )
    versions = {row[3] for row in rows}
    cost_models = {row[4] for row in rows}
    if len(versions) != 1 or len(cost_models) != 1:
        raise ValueError("label_version または cost_model_id が混在")
    allowed = sorted(
        {
            float(candidate)
            for candidate in candidates
            if math.isfinite(float(candidate)) and DEFAULT_THRESHOLD <= float(candidate) <= 1.0
        }
    )
    if DEFAULT_THRESHOLD not in allowed:
        allowed.insert(0, DEFAULT_THRESHOLD)
    rows.sort(key=lambda row: row[0])
    tune_start = max(1, int(len(rows) * 0.6))
    test_start = max(tune_start + 1, int(len(rows) * 0.8))
    if test_start >= len(rows):
        test_start = len(rows) - 1
    test_start_ts = rows[test_start][0]
    tune_cut = test_start_ts - timedelta(hours=EMBARGO_HOURS)
    tune_rows = rows[tune_start:test_start]
    tune_rows = [row for row in tune_rows if row[0] < tune_cut]
    test_rows = rows[test_start:]

    def _candidate_rank(threshold: float) -> tuple[float, float]:
        lower_bound = _lcb(_returns_for(tune_rows, threshold))
        return (lower_bound if lower_bound is not None else float("-inf"), threshold)

    best_threshold = max(allowed, key=_candidate_rank)
    test_returns = _returns_for(test_rows, best_threshold)
    mean = sum(test_returns) / len(test_returns) if test_returns else None
    lcb = _lcb(test_returns)
    dsr = _dsr(test_returns, [_sharpe(_returns_for(tune_rows, threshold)) for threshold in allowed])
    stage = (
        "ready_for_review"
        if len(test_returns) >= min_test_samples
        and lcb is not None
        and lcb > 0
        and dsr is not None
        and dsr >= 0.95
        and best_threshold > DEFAULT_THRESHOLD
        else "shadow"
    )
    dataset_hash = _dataset_hash(rows)
    return ThresholdPolicy(
        policy_id=_policy_id(now, best_threshold, dataset_hash),
        stage=stage,
        threshold=best_threshold,
        label_version=next(iter(versions)),
        cost_model_id=next(iter(cost_models)),
        dataset_hash=dataset_hash,
        generated_at=now.isoformat(),
        train_end=rows[tune_start - 1][0].isoformat(),
        test_end=rows[-1][0].isoformat(),
        effective_samples=len(test_returns),
        oos_mean_net_r=round(mean, 6) if mean is not None else None,
        oos_net_r_lcb=round(lcb, 6) if lcb is not None else None,
        dsr=round(dsr, 6) if dsr is not None else None,
        max_drawdown_r=_max_drawdown(test_returns),
        expires_at=(now + timedelta(days=POLICY_VALID_DAYS)).isoformat(),
    )


def approve_policy(
    policy: ThresholdPolicy, approved_by: str, *, now: datetime | None = None
) -> ThresholdPolicy:
    if policy.stage != "ready_for_review":
        raise ValueError("ready_for_review の候補だけ承認できます")
    actor = approved_by.strip()
    if not actor:
        raise ValueError("approved_by が必要です")
    return replace(
        policy,
        stage="approved",
        approved_by=actor,
        approved_at=_utc(now or datetime.now(UTC)).isoformat(),
    )


def activate_policy(policy: ThresholdPolicy, *, now: datetime | None = None) -> ThresholdPolicy:
    if policy.stage != "approved" or not policy.approved_by or not policy.approved_at:
        raise ValueError("人間承認済みの候補だけ有効化できます")
    return replace(
        policy,
        stage=ACTIVE_STAGE,
        activated_at=_utc(now or datetime.now(UTC)).isoformat(),
    )


def auto_pause_policy(
    policy: ThresholdPolicy,
    outcomes: Sequence[Mapping[str, object]],
    *,
    min_samples: int = DEFAULT_MIN_TEST_SAMPLES,
) -> ThresholdPolicy:
    if policy.stage != ACTIVE_STAGE:
        return policy
    try:
        rows = _eligible_rows(outcomes)
        matching = [
            row for row in rows if row[3] == policy.label_version and row[4] == policy.cost_model_id
        ]
        recent_returns = _returns_for(
            matching[-max(min_samples * 2, min_samples) :], policy.threshold
        )
        lcb = _lcb(recent_returns)
    except ValueError as error:
        return replace(policy, stage="auto_paused", auto_pause_reason=str(error))
    if len(recent_returns) >= min_samples and (lcb is None or lcb <= 0):
        return replace(
            policy,
            stage="auto_paused",
            auto_pause_reason=f"直近純Rの片側信頼下限が非正({lcb})",
        )
    return policy


def _eligible_rows(
    outcomes: Sequence[Mapping[str, object]],
) -> list[tuple[datetime, float, float, str, str, str]]:
    rows: list[tuple[datetime, float, float, str, str, str]] = []
    for outcome in outcomes:
        if not bool(outcome.get("net_label_eligible", outcome.get("tradable", False))):
            continue
        ts = _parse_ts(outcome.get("ts"))
        composite = _float(outcome.get("composite"))
        net_r = _float(outcome.get("realized_net_r"))
        version = str(outcome.get("label_version", "")).strip()
        cost_model = str(outcome.get("cost_model_id", "")).strip()
        decision_id = str(outcome.get("decision_id", ""))
        if ts is None or composite is None or net_r is None or not version or not cost_model:
            continue
        rows.append((ts, composite, net_r, version, cost_model, decision_id))
    return rows


def _returns_for(
    rows: Sequence[tuple[datetime, float, float, str, str, str]], threshold: float
) -> list[float]:
    return [row[2] for row in rows if abs(row[1]) >= threshold]


def _lcb(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    # Conservative one-sided 95% normal lower bound. Candidate generation and
    # final lockbox remain separate; this is not presented as an exact small-n t interval.
    return mean - 1.645 * math.sqrt(variance / len(values))


def _sharpe(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return mean / math.sqrt(variance) if variance > 0 else 0.0


def _dsr(values: Sequence[float], trial_sharpes: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    try:
        from fx_backtester.overfitting import deflated_sharpe_ratio

        return float(deflated_sharpe_ratio(list(values), list(trial_sharpes))["dsr"])
    except (ImportError, TypeError, ValueError, KeyError):
        return None


def _max_drawdown(values: Sequence[float]) -> float | None:
    if not values:
        return None
    equity = peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return round(drawdown, 6)


def _dataset_hash(rows: Sequence[tuple[datetime, float, float, str, str, str]]) -> str:
    canonical = "\n".join(
        f"{row[0].isoformat()}|{row[1]:.8f}|{row[2]:.8f}|{row[3]}|{row[4]}|{row[5]}" for row in rows
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _policy_id(now: datetime, threshold: float, dataset_hash: str) -> str:
    raw = f"{now.isoformat()}|{threshold:.4f}|{dataset_hash}"
    return "threshold-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _validate_policy(policy: ThresholdPolicy) -> None:
    if policy.stage not in VALID_STAGES:
        raise ValueError("threshold policy stage不正")
    if policy.scope != "overall":
        raise ValueError("初期版はoverall scopeのみ")
    if not math.isfinite(policy.threshold) or not DEFAULT_THRESHOLD <= policy.threshold <= 1.0:
        raise ValueError("threshold は0.15以上1.0以下")
    if policy.fallback_threshold != DEFAULT_THRESHOLD:
        raise ValueError("fallback threshold は0.15固定")
    if policy.stage in {"ready_for_review", "approved", ACTIVE_STAGE}:
        if not policy.label_version or not policy.cost_model_id or not policy.dataset_hash:
            raise ValueError("昇格候補の来歴が不足")
        if _parse_ts(policy.expires_at) is None:
            raise ValueError("昇格候補の期限が不足")


def _parse_ts(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _float(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
