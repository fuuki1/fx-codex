"""Fail-closed Phase 2 readiness checks for canonical net-R labels.

The canonical outcome store remains the source of truth.  This module does not
rescore decisions or fill missing costs with zero.  It projects the stored
accounting into the target architecture's field names and records whether each
value was stored, deterministically derived, or unavailable.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import math

from .canonical_label_evidence import (
    CanonicalLabelEvidence,
    InvalidCanonicalLabelEvidence,
    LABEL_EVIDENCE_VERSION,
    VERIFIED_LABEL_QUALITIES,
    validate_evidence_binding,
)
from .evaluation_labels import (
    FULL_COST_NET_LABEL_VERSION,
    NET_LABEL_VERSION,
    canonical_net_label_contract_flags,
)
from .trade_outcome import CanonicalTradeOutcome

READINESS_SCHEMA_VERSION = 2
ACCOUNTING_TOLERANCE = 1e-8
NONZERO_TOLERANCE = 1e-12
TP_TOUCHES = frozenset({"tp1", "tp2", "target1", "target2"})
SL_TOUCHES = frozenset({"sl", "sl_gap", "ambiguous_sl_tp", "stop_loss", "stop_gap"})
TERMINAL_TOUCHES = frozenset({"terminal", "time_exit", "session_end"})
KNOWN_TOUCHES = TP_TOUCHES | SL_TOUCHES | TERMINAL_TOUCHES

TARGET_LABEL_FIELDS: tuple[str, ...] = (
    "gross_r",
    "spread_cost_r",
    "slippage_cost_r",
    "commission_cost_r",
    "financing_cost_r",
    "execution_cost_r",
    "net_r",
    "mae_r",
    "mfe_r",
    "time_to_tp",
    "time_to_sl",
    "holding_seconds",
    "label_quality",
    "label_version",
)


@dataclass(frozen=True)
class CanonicalLabelReadiness:
    """One deterministic, read-only assessment of Phase 2 evidence."""

    status: str
    phase2_complete: bool
    source_verified: bool
    evidence_source_verified: bool | None
    outcome_rows: int
    evidence_rows: int
    canonical_rows: int
    net_r_rows: int
    nonzero_net_r_rows: int
    canonical_coverage: float | None
    evidence_coverage: float | None
    completion_conditions: Mapping[str, bool]
    field_coverage: Mapping[str, float | None]
    field_sources: Mapping[str, Mapping[str, int]]
    missing_reasons: Mapping[str, int]
    label_versions: tuple[str, ...]
    label_provenances: tuple[str, ...]
    cost_model_ids: tuple[str, ...]
    evidence_versions: tuple[str, ...]
    label_qualities: tuple[str, ...]
    source_error: str | None = None
    evidence_source_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": READINESS_SCHEMA_VERSION,
            "status": self.status,
            "phase2_complete": self.phase2_complete,
            "source_verified": self.source_verified,
            "evidence_source_verified": self.evidence_source_verified,
            "outcome_rows": self.outcome_rows,
            "evidence_rows": self.evidence_rows,
            "canonical_rows": self.canonical_rows,
            "net_r_rows": self.net_r_rows,
            "nonzero_net_r_rows": self.nonzero_net_r_rows,
            "canonical_coverage": self.canonical_coverage,
            "evidence_coverage": self.evidence_coverage,
            "completion_conditions": dict(self.completion_conditions),
            "target_label_fields": list(TARGET_LABEL_FIELDS),
            "field_coverage": dict(self.field_coverage),
            "field_sources": {field: dict(counts) for field, counts in self.field_sources.items()},
            "missing_reasons": dict(self.missing_reasons),
            "label_versions": list(self.label_versions),
            "label_provenances": list(self.label_provenances),
            "cost_model_ids": list(self.cost_model_ids),
            "evidence_versions": list(self.evidence_versions),
            "label_qualities": list(self.label_qualities),
            "source_error": self.source_error,
            "evidence_source_error": self.evidence_source_error,
        }


def canonical_label_projection(
    outcome: CanonicalTradeOutcome | Mapping[str, object],
) -> tuple[dict[str, object], dict[str, str]]:
    """Project one outcome into target label fields without inventing evidence."""

    row = _as_mapping(outcome)
    gross = _number(row.get("gross_realized_r"))
    quote = _number(row.get("quote_realized_r"))
    stored_spread = _number(row.get("realized_spread_cost_r"))
    spread = stored_spread
    spread_source = "stored"
    if spread is None and gross is not None and quote is not None:
        spread = gross - quote
        spread_source = "derived:gross_realized_r-quote_realized_r"
    elif spread is None:
        spread_source = "unavailable"

    prediction = _aware_time(row.get("prediction_time"))
    holding_end = _aware_time(row.get("holding_end_time"))
    holding_seconds = _elapsed_seconds(prediction, holding_end)
    first_touch = str(row.get("first_touch") or "")
    first_touch_time = _aware_time(row.get("first_touch_ts"))
    touch_seconds = (
        _elapsed_seconds(prediction, first_touch_time)
        if _touch_within_holding_interval(prediction, first_touch_time, holding_end)
        else None
    )

    time_to_tp = touch_seconds if first_touch in TP_TOUCHES else None
    time_to_sl = touch_seconds if first_touch in SL_TOUCHES else None
    label_quality = _label_quality(row)
    projection: dict[str, object] = {
        "gross_r": gross,
        "spread_cost_r": spread,
        "slippage_cost_r": _number(row.get("slippage_r")),
        "commission_cost_r": _number(row.get("commission_r")),
        "financing_cost_r": _number(row.get("financing_r")),
        "execution_cost_r": _number(row.get("execution_cost_r")),
        "net_r": _number(row.get("realized_net_r")),
        "mae_r": _number(row.get("mae_r", row.get("maximum_adverse_excursion_r"))),
        "mfe_r": _number(row.get("mfe_r", row.get("maximum_favorable_excursion_r"))),
        "time_to_tp": time_to_tp,
        "time_to_sl": time_to_sl,
        "holding_seconds": holding_seconds,
        "label_quality": label_quality,
        "label_version": str(row.get("label_version") or "") or None,
        # Supporting stored components used to prove the target accounting.
        # They are intentionally not advertised as target label fields.
        "_additional_cost_r": _number(row.get("additional_cost_r")),
        "_conversion_cost_r": _number(row.get("conversion_r")),
        "_quote_r": quote,
    }
    sources = {
        "gross_r": _stored_source(gross),
        "spread_cost_r": spread_source,
        "slippage_cost_r": _stored_source(projection["slippage_cost_r"]),
        "commission_cost_r": _stored_source(projection["commission_cost_r"]),
        "financing_cost_r": _stored_source(projection["financing_cost_r"]),
        "execution_cost_r": _stored_source(projection["execution_cost_r"]),
        "net_r": _stored_source(projection["net_r"]),
        "mae_r": _excursion_source(row, "mae_r", "maximum_adverse_excursion_r"),
        "mfe_r": _excursion_source(row, "mfe_r", "maximum_favorable_excursion_r"),
        "time_to_tp": (
            "derived:prediction_time-first_touch_ts"
            if time_to_tp is not None
            else "not_applicable" if first_touch not in TP_TOUCHES else "unavailable"
        ),
        "time_to_sl": (
            "derived:prediction_time-first_touch_ts"
            if time_to_sl is not None
            else ("not_applicable" if first_touch not in SL_TOUCHES else "unavailable")
        ),
        "holding_seconds": (
            "derived:prediction_time-holding_end_time"
            if holding_seconds is not None
            else "unavailable"
        ),
        "label_quality": "derived:canonical_contract+path_quality+quality_flags",
        "label_version": _stored_source(projection["label_version"]),
    }
    return projection, sources


def assess_canonical_label_readiness(
    outcomes: Sequence[CanonicalTradeOutcome | Mapping[str, object]],
    *,
    source_verified: bool,
    source_error: str | None = None,
    label_evidence: Sequence[CanonicalLabelEvidence] | None = None,
    evidence_source_verified: bool | None = None,
    evidence_source_error: str | None = None,
) -> CanonicalLabelReadiness:
    """Assess target-field coverage and the four Phase 2 completion conditions."""

    verified_store_shape = bool(outcomes) and all(
        isinstance(outcome, CanonicalTradeOutcome) for outcome in outcomes
    )
    source_verified = source_verified and verified_store_shape
    evidence_mode = (
        label_evidence is not None
        or evidence_source_verified is not None
        or evidence_source_error is not None
    )
    evidence_rows = list(label_evidence or ())
    evidence_shape_verified = all(
        isinstance(evidence, CanonicalLabelEvidence) for evidence in evidence_rows
    )
    effective_evidence_verified: bool | None = None
    if evidence_mode:
        effective_evidence_verified = bool(evidence_source_verified) and evidence_shape_verified
        source_verified = source_verified and effective_evidence_verified
    rows = [_as_mapping(outcome) for outcome in outcomes]
    missing: Counter[str] = Counter()
    canonical: list[Mapping[str, object]] = []
    projections: list[dict[str, object]] = []
    projection_sources: list[dict[str, str]] = []
    matched_evidence: list[CanonicalLabelEvidence] = []
    evidence_by_key: dict[tuple[str, str, str], CanonicalLabelEvidence] = {}
    duplicate_evidence_keys: set[tuple[str, str, str]] = set()
    for evidence_row in evidence_rows:
        key = evidence_row.evidence_key
        if key in evidence_by_key:
            duplicate_evidence_keys.add(key)
        evidence_by_key[key] = evidence_row
    if duplicate_evidence_keys:
        effective_evidence_verified = False
        source_verified = False
        missing["duplicate_label_evidence_key"] += len(duplicate_evidence_keys)

    for original, row in zip(outcomes, rows, strict=True):
        contract_flags = canonical_net_label_contract_flags(row)
        if contract_flags:
            for flag in contract_flags:
                missing[f"contract:{flag}"] += 1
            continue
        projection, sources = canonical_label_projection(row)
        if evidence_mode:
            key = (
                str(row.get("decision_id") or ""),
                str(row.get("label_version") or ""),
                LABEL_EVIDENCE_VERSION,
            )
            evidence = evidence_by_key.get(key)
            if evidence is None:
                missing["missing_label_evidence"] += 1
            elif not isinstance(original, CanonicalTradeOutcome):
                effective_evidence_verified = False
                source_verified = False
                missing["unverified_label_evidence_binding"] += 1
            else:
                try:
                    validate_evidence_binding(evidence, original)
                except InvalidCanonicalLabelEvidence:
                    effective_evidence_verified = False
                    source_verified = False
                    missing["invalid_label_evidence_binding"] += 1
                else:
                    projection, sources = canonical_label_evidence_projection(evidence)
                    matched_evidence.append(evidence)
        canonical.append(row)
        projections.append(projection)
        projection_sources.append(sources)

    if source_error:
        missing["source_unavailable"] += 1
    if evidence_source_error:
        missing["evidence_source_unavailable"] += 1
    canonical_keys = {
        (str(row.get("decision_id") or ""), str(row.get("label_version") or ""))
        for row in canonical
    }
    extra_evidence = {
        evidence_key for evidence_key in evidence_by_key if evidence_key[:2] not in canonical_keys
    }
    if evidence_mode and extra_evidence:
        effective_evidence_verified = False
        source_verified = False
        missing["orphan_label_evidence"] += len(extra_evidence)
    if not source_verified:
        missing["source_not_verified"] += 1

    net_r_rows = sum(_number(projection.get("net_r")) is not None for projection in projections)
    nonzero_net_r_rows = sum(
        abs(value) > NONZERO_TOLERANCE
        for projection in projections
        if (value := _number(projection.get("net_r"))) is not None
    )
    side_verified = _all_or_false(canonical, _side_is_recomputable)
    cost_recomputable = _all_or_false(projections, _costs_recompute)
    legacy_delta = _all_or_false(projections, _legacy_delta_recomputes)
    timeline_valid = _all_or_false(canonical, _timeline_is_recomputable)

    for row in canonical:
        _record_side_missing(row, missing)
        if not _timeline_is_recomputable(row):
            missing["label_timeline_not_recomputable"] += 1
    for projection in projections:
        if not _costs_recompute(projection):
            missing["cost_breakdown_not_recomputable"] += 1
        if not _legacy_delta_recomputes(projection):
            missing["legacy_delta_not_explainable"] += 1

    field_sources: dict[str, Mapping[str, int]] = {}
    field_coverage: dict[str, float | None] = {}
    for field in TARGET_LABEL_FIELDS:
        counts = Counter(sources[field] for sources in projection_sources)
        field_sources[field] = dict(sorted(counts.items()))
        available = sum(
            source not in {"unavailable"}
            for source in (sources[field] for sources in projection_sources)
        )
        field_coverage[field] = available / len(canonical) if canonical else None
        if canonical and available < len(canonical):
            missing[f"field_unavailable:{field}"] += len(canonical) - available

    required_field_complete = bool(canonical) and all(
        field_coverage[field] == 1.0 for field in TARGET_LABEL_FIELDS
    )
    label_quality_verified = (
        bool(canonical)
        and len(matched_evidence) == len(canonical)
        and all(evidence.label_quality in VERIFIED_LABEL_QUALITIES for evidence in matched_evidence)
    )
    if canonical and not label_quality_verified:
        missing["label_quality_not_verified"] += len(canonical) - sum(
            evidence.label_quality in VERIFIED_LABEL_QUALITIES for evidence in matched_evidence
        )
    conditions = {
        "net_r_present": net_r_rows > 0,
        "entry_exit_bid_ask_side_verified": side_verified,
        "cost_breakdown_recomputable": cost_recomputable,
        "legacy_label_delta_explainable": legacy_delta,
        "label_timeline_valid": timeline_valid,
        "target_label_fields_complete": required_field_complete,
        "label_quality_verified": label_quality_verified,
    }
    phase2_complete = (
        source_verified
        and not source_error
        and not evidence_source_error
        and all(conditions.values())
    )
    if source_error or evidence_source_error or not source_verified or not canonical:
        status = "evaluation_unavailable"
    elif phase2_complete:
        status = "ready"
    else:
        status = "incomplete"

    return CanonicalLabelReadiness(
        status=status,
        phase2_complete=phase2_complete,
        source_verified=source_verified,
        evidence_source_verified=effective_evidence_verified,
        outcome_rows=len(rows),
        evidence_rows=len(evidence_rows),
        canonical_rows=len(canonical),
        net_r_rows=net_r_rows,
        nonzero_net_r_rows=nonzero_net_r_rows,
        canonical_coverage=(len(canonical) / len(rows) if rows else None),
        evidence_coverage=(
            len(matched_evidence) / len(canonical) if canonical and evidence_mode else None
        ),
        completion_conditions=conditions,
        field_coverage=field_coverage,
        field_sources=field_sources,
        missing_reasons=dict(sorted(missing.items())),
        label_versions=tuple(sorted({str(row.get("label_version")) for row in canonical})),
        label_provenances=tuple(sorted({str(row.get("label_provenance")) for row in canonical})),
        cost_model_ids=tuple(sorted({str(row.get("cost_model_id")) for row in canonical})),
        evidence_versions=tuple(
            sorted({evidence.evidence_version for evidence in matched_evidence})
        ),
        label_qualities=tuple(sorted({evidence.label_quality for evidence in matched_evidence})),
        source_error=source_error,
        evidence_source_error=evidence_source_error,
    )


def canonical_label_evidence_projection(
    evidence: CanonicalLabelEvidence,
) -> tuple[dict[str, object], dict[str, str]]:
    """Project exact stored sidecar fields after outcome-hash binding succeeds."""

    projection: dict[str, object] = {
        "gross_r": evidence.gross_r,
        "spread_cost_r": evidence.spread_cost_r,
        "slippage_cost_r": evidence.slippage_cost_r,
        "commission_cost_r": evidence.commission_cost_r,
        "financing_cost_r": evidence.financing_cost_r,
        "execution_cost_r": evidence.execution_cost_r,
        "net_r": evidence.net_r,
        "mae_r": evidence.mae_r,
        "mfe_r": evidence.mfe_r,
        "time_to_tp": evidence.time_to_tp,
        "time_to_sl": evidence.time_to_sl,
        "holding_seconds": evidence.holding_seconds,
        "label_quality": evidence.label_quality,
        "label_version": evidence.label_version,
        "_additional_cost_r": (
            evidence.slippage_cost_r
            + evidence.commission_cost_r
            + evidence.financing_cost_r
            + evidence.conversion_cost_r
        ),
        "_conversion_cost_r": evidence.conversion_cost_r,
        "_quote_r": evidence.gross_r - evidence.spread_cost_r,
    }
    source_name = f"stored:{evidence.evidence_version}"
    sources = {
        field: (
            "not_applicable"
            if field == "time_to_tp"
            and evidence.time_to_tp is None
            and evidence.first_touch not in TP_TOUCHES
            else (
                "not_applicable"
                if field == "time_to_sl"
                and evidence.time_to_sl is None
                and evidence.first_touch not in SL_TOUCHES
                else "unavailable" if projection[field] is None else source_name
            )
        )
        for field in TARGET_LABEL_FIELDS
    }
    return projection, sources


def _as_mapping(
    outcome: CanonicalTradeOutcome | Mapping[str, object],
) -> Mapping[str, object]:
    return outcome.to_dict() if isinstance(outcome, CanonicalTradeOutcome) else outcome


def _label_quality(row: Mapping[str, object]) -> dict[str, object]:
    flags = row.get("quality_flags")
    return {
        "status": (
            "verified_canonical"
            if row.get("net_label_eligible") is True and not canonical_net_label_contract_flags(row)
            else "ineligible"
        ),
        "path_quality": _number(row.get("path_quality")),
        "quality_flags": (
            [str(flag) for flag in flags] if isinstance(flags, (list, tuple)) else []
        ),
    }


def _side_is_recomputable(row: Mapping[str, object]) -> bool:
    direction = str(row.get("direction") or "")
    entry_bid = _number(row.get("entry_bid"))
    entry_ask = _number(row.get("entry_ask"))
    entry_executable = _number(row.get("entry_executable"))
    exit_bid = _number(row.get("exit_bid"))
    exit_ask = _number(row.get("exit_ask"))
    exit_executable = _number(row.get("exit_executable"))
    if None in (entry_bid, entry_ask, entry_executable, exit_bid, exit_ask, exit_executable):
        return False
    assert entry_bid is not None
    assert entry_ask is not None
    assert entry_executable is not None
    assert exit_bid is not None
    assert exit_ask is not None
    assert exit_executable is not None
    if not (0 < entry_bid <= entry_ask and 0 < exit_bid <= exit_ask):
        return False
    risk = _number(row.get("planned_risk_distance"))
    quote_r = _number(row.get("quote_realized_r"))
    gross_r = _number(row.get("gross_realized_r"))
    exit_mid = _number(row.get("exit_mid"))
    if risk is None or risk <= 0 or quote_r is None or gross_r is None or exit_mid is None:
        return False
    entry_mid = (entry_bid + entry_ask) / 2.0
    if not _same(exit_mid, (exit_bid + exit_ask) / 2.0):
        return False
    label_version = str(row.get("label_version") or "")
    if label_version == FULL_COST_NET_LABEL_VERSION:
        if direction == "long":
            return _same(entry_executable, entry_ask) and _same(exit_executable, exit_bid)
        if direction == "short":
            return _same(entry_executable, entry_bid) and _same(exit_executable, exit_ask)
        return False
    if label_version != NET_LABEL_VERSION:
        return False
    if direction == "long":
        return (
            _same(entry_executable, entry_ask)
            and _same(exit_executable, exit_bid)
            and _same(quote_r, (exit_executable - entry_executable) / risk)
            and _same(gross_r, (exit_mid - entry_mid) / risk)
        )
    if direction == "short":
        return (
            _same(entry_executable, entry_bid)
            and _same(exit_executable, exit_ask)
            and _same(quote_r, (entry_executable - exit_executable) / risk)
            and _same(gross_r, (entry_mid - exit_mid) / risk)
        )
    return False


def _timeline_is_recomputable(row: Mapping[str, object]) -> bool:
    prediction = _aware_time(row.get("prediction_time"))
    holding_end = _aware_time(row.get("holding_end_time"))
    first_touch_time = _aware_time(row.get("first_touch_ts"))
    first_touch = str(row.get("first_touch") or "")
    if not _touch_within_holding_interval(prediction, first_touch_time, holding_end):
        return False
    if first_touch in TERMINAL_TOUCHES:
        return first_touch_time == holding_end
    return first_touch in KNOWN_TOUCHES


def _record_side_missing(row: Mapping[str, object], missing: Counter[str]) -> None:
    if _number(row.get("entry_executable")) is None:
        missing["missing_entry_executable"] += 1
    if _number(row.get("exit_bid")) is None or _number(row.get("exit_ask")) is None:
        missing["missing_exit_bid_ask"] += 1
    if _number(row.get("exit_executable")) is None:
        missing["missing_exit_executable"] += 1
    if not _side_is_recomputable(row):
        missing["bid_ask_side_not_recomputable"] += 1


def _costs_recompute(projection: Mapping[str, object]) -> bool:
    names = (
        "gross_r",
        "spread_cost_r",
        "slippage_cost_r",
        "commission_cost_r",
        "financing_cost_r",
        "execution_cost_r",
        "net_r",
    )
    values = {name: _number(projection.get(name)) for name in names}
    additional = _number(projection.get("_additional_cost_r"))
    conversion = _number(projection.get("_conversion_cost_r"))
    quote = _number(projection.get("_quote_r"))
    if any(value is None for value in values.values()) or additional is None or quote is None:
        return False
    numeric = {name: float(value) for name, value in values.items() if value is not None}
    named_additional = sum(
        numeric[name]
        for name in (
            "slippage_cost_r",
            "commission_cost_r",
            "financing_cost_r",
        )
    )
    if conversion is not None:
        named_additional += conversion
    elif not _same(additional, named_additional):
        # A residual cost exists but its component is absent. Do not silently
        # call that residual zero or fold it into a named target component.
        return False
    cost_components = [
        numeric["spread_cost_r"],
        numeric["slippage_cost_r"],
        numeric["commission_cost_r"],
        numeric["financing_cost_r"],
    ]
    if conversion is not None:
        cost_components.append(conversion)
    if any(value < -ACCOUNTING_TOLERANCE for value in cost_components):
        return False
    return (
        _same(additional, named_additional)
        and _same(numeric["spread_cost_r"], numeric["gross_r"] - quote)
        and _same(numeric["execution_cost_r"], numeric["spread_cost_r"] + additional)
        and _same(numeric["net_r"], numeric["gross_r"] - numeric["execution_cost_r"])
        and _same(numeric["net_r"], quote - additional)
        and _same(numeric["net_r"], numeric["gross_r"] - numeric["spread_cost_r"] - additional)
    )


def _legacy_delta_recomputes(projection: Mapping[str, object]) -> bool:
    gross = _number(projection.get("gross_r"))
    execution = _number(projection.get("execution_cost_r"))
    net = _number(projection.get("net_r"))
    return (
        gross is not None
        and execution is not None
        and net is not None
        and _same(gross - net, execution)
    )


def _all_or_false(
    rows: Sequence[Mapping[str, object]],
    predicate: Callable[[Mapping[str, object]], bool],
) -> bool:
    return bool(rows) and all(predicate(row) for row in rows)


def _stored_source(value: object) -> str:
    return "stored" if value is not None else "unavailable"


def _excursion_source(row: Mapping[str, object], primary: str, fallback: str) -> str:
    if _number(row.get(primary)) is not None:
        return "stored"
    if _number(row.get(fallback)) is not None:
        return f"stored:{fallback}"
    return "unavailable"


def _aware_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _elapsed_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds()


def _touch_within_holding_interval(
    prediction: datetime | None,
    first_touch: datetime | None,
    holding_end: datetime | None,
) -> bool:
    return (
        prediction is not None
        and first_touch is not None
        and holding_end is not None
        and prediction <= first_touch <= holding_end
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _same(left: float | None, right: float | None) -> bool:
    return (
        left is not None
        and right is not None
        and math.isclose(left, right, rel_tol=1e-9, abs_tol=ACCOUNTING_TOLERANCE)
    )
