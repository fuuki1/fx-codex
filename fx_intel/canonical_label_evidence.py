"""Versioned, hash-bound evidence for canonical Phase 2 label fields.

The canonical outcome remains the accounting source of truth.  This sidecar
contract preserves path-dependent facts without changing the meaning or hash
of existing ``net-r-v2``/``net-r-v3`` outcomes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import math

from .evaluation_labels import canonical_net_label_contract_flags
from .canonical_label_path_verifier import (
    CanonicalPathVerification,
    RAW_PATH_VERIFICATION_CONTRACT,
    RAW_REPLAYED_LABEL_QUALITY,
)
from .trade_outcome import CanonicalTradeOutcome
from .virtual_portfolio_net_labels import (
    HISTORICAL_NONTRADABLE_FLAG,
    VIRTUAL_LEARNING_CONTRACT,
    canonical_outcome_from_virtual_row,
    canonical_outcome_sha256,
)

LABEL_EVIDENCE_VERSION = "canonical-label-evidence-v2"
LEGACY_LABEL_EVIDENCE_VERSION = "canonical-label-evidence-v1"
LABEL_PATH_EVIDENCE_CONTRACT = "canonical-label-path-evidence-v1"
HASH_BOUND_LABEL_QUALITY = "hash_bound_unrecomputed_path"
# A creation-time replay attestation is useful provenance, but it is not a
# durable readiness proof by itself.  The readiness gate does not yet reopen
# the pinned quote prefix and raw preimages, and the content-addressed blob
# store has no hour manifest with which to prove that an entire source blob was
# not omitted.  Keep Phase 2 fail-closed until those inputs are freshly replayed
# at assessment time.
VERIFIED_LABEL_QUALITIES: frozenset[str] = frozenset()
TP_TOUCHES = frozenset({"tp1", "tp2", "target1", "target2"})
SL_TOUCHES = frozenset({"sl", "sl_gap", "ambiguous_sl_tp", "stop_loss", "stop_gap"})
TERMINAL_TOUCHES = frozenset({"terminal", "time_exit", "session_end"})
KNOWN_TOUCHES = TP_TOUCHES | SL_TOUCHES | TERMINAL_TOUCHES
ACCOUNTING_TOLERANCE = 1e-8


class InvalidCanonicalLabelEvidence(ValueError):
    """Path or accounting evidence cannot support a canonical label sidecar."""


@dataclass(frozen=True)
class CanonicalLabelEvidence:
    """Immutable target-label evidence bound to one canonical outcome hash."""

    evidence_version: str
    decision_id: str
    label_version: str
    label_provenance: str
    canonical_outcome_sha256: str
    source_contract: str
    source_close_record_sha256: str
    source_close_payload: Mapping[str, object]
    symbol: str
    timeframe: str
    direction: str
    prediction_time: str
    holding_end_time: str
    label_available_time: str
    label_ingested_time: str
    first_touch: str
    first_touch_ts: str
    entry_bid: float
    entry_ask: float
    entry_executable: float
    exit_bid: float
    exit_ask: float
    exit_executable: float
    exit_mid: float
    gross_r: float
    spread_cost_r: float
    slippage_cost_r: float
    commission_cost_r: float
    financing_cost_r: float
    conversion_cost_r: float
    execution_cost_r: float
    net_r: float
    mae_r: float | None
    mfe_r: float | None
    time_to_tp: float | None
    time_to_sl: float | None
    holding_seconds: float
    label_quality: str
    path_quality: float | None
    path_source: str
    path_evidence_contract: str
    path_tick_count: int | None
    path_source_ids_sha256: str
    path_raw_payload_hashes_sha256: str
    quality_flags: tuple[str, ...] = field(default_factory=tuple)
    research_only: bool = True
    promotion_eligible: bool = False
    path_verification_contract: str = ""
    quote_log_prefix_size_bytes: int | None = None
    quote_log_prefix_sha256: str = ""
    raw_blob_count: int | None = None
    raw_blob_hashes_sha256: str = ""

    @property
    def evidence_key(self) -> tuple[str, str, str]:
        return self.decision_id, self.label_version, self.evidence_version

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "evidence_version": self.evidence_version,
            "decision_id": self.decision_id,
            "label_version": self.label_version,
            "label_provenance": self.label_provenance,
            "canonical_outcome_sha256": self.canonical_outcome_sha256,
            "source_contract": self.source_contract,
            "source_close_record_sha256": self.source_close_record_sha256,
            "source_close_payload": dict(self.source_close_payload),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "prediction_time": self.prediction_time,
            "holding_end_time": self.holding_end_time,
            "label_available_time": self.label_available_time,
            "label_ingested_time": self.label_ingested_time,
            "first_touch": self.first_touch,
            "first_touch_ts": self.first_touch_ts,
            "entry_bid": self.entry_bid,
            "entry_ask": self.entry_ask,
            "entry_executable": self.entry_executable,
            "exit_bid": self.exit_bid,
            "exit_ask": self.exit_ask,
            "exit_executable": self.exit_executable,
            "exit_mid": self.exit_mid,
            "gross_r": self.gross_r,
            "spread_cost_r": self.spread_cost_r,
            "slippage_cost_r": self.slippage_cost_r,
            "commission_cost_r": self.commission_cost_r,
            "financing_cost_r": self.financing_cost_r,
            "conversion_cost_r": self.conversion_cost_r,
            "execution_cost_r": self.execution_cost_r,
            "net_r": self.net_r,
            "mae_r": self.mae_r,
            "mfe_r": self.mfe_r,
            "time_to_tp": self.time_to_tp,
            "time_to_sl": self.time_to_sl,
            "holding_seconds": self.holding_seconds,
            "label_quality": self.label_quality,
            "path_quality": self.path_quality,
            "path_source": self.path_source,
            "path_evidence_contract": self.path_evidence_contract,
            "path_tick_count": self.path_tick_count,
            "path_source_ids_sha256": self.path_source_ids_sha256,
            "path_raw_payload_hashes_sha256": self.path_raw_payload_hashes_sha256,
            "quality_flags": list(self.quality_flags),
            "research_only": self.research_only,
            "promotion_eligible": self.promotion_eligible,
        }
        if self.evidence_version == LABEL_EVIDENCE_VERSION:
            payload.update(
                {
                    "path_verification_contract": self.path_verification_contract,
                    "quote_log_prefix_size_bytes": self.quote_log_prefix_size_bytes,
                    "quote_log_prefix_sha256": self.quote_log_prefix_sha256,
                    "raw_blob_count": self.raw_blob_count,
                    "raw_blob_hashes_sha256": self.raw_blob_hashes_sha256,
                }
            )
        return payload


def canonical_label_evidence_from_virtual_row(
    row: Mapping[str, object],
    outcome: CanonicalTradeOutcome,
    *,
    path_verification: CanonicalPathVerification | None = None,
) -> CanonicalLabelEvidence:
    """Build evidence from one hash-checked immutable virtual learning row."""

    rebuilt = canonical_outcome_from_virtual_row(row)
    if rebuilt != outcome:
        raise InvalidCanonicalLabelEvidence(
            "virtual row does not reproduce the supplied canonical outcome"
        )
    payload = row.get("canonical_outcome")
    if not isinstance(payload, Mapping) or dict(payload) != outcome.to_dict():
        raise InvalidCanonicalLabelEvidence("virtual row canonical payload mismatch")
    outcome_hash = canonical_outcome_sha256(outcome)
    if row.get("canonical_outcome_sha256") != outcome_hash:
        raise InvalidCanonicalLabelEvidence("virtual row canonical outcome hash mismatch")

    prediction = _aware(outcome.prediction_time, "prediction_time")
    entry = _aware(row.get("simulated_entry_time"), "simulated_entry_time")
    holding_end = _aware(outcome.holding_end_time, "holding_end_time")
    first_touch_time = _aware(outcome.first_touch_ts, "first_touch_ts")
    if not prediction <= entry <= holding_end:
        raise InvalidCanonicalLabelEvidence("entry/holding interval is reversed")
    if not prediction <= first_touch_time <= holding_end:
        raise InvalidCanonicalLabelEvidence("first touch is outside the holding interval")

    first_touch = _text(outcome.first_touch, "first_touch")
    if first_touch not in KNOWN_TOUCHES:
        raise InvalidCanonicalLabelEvidence("unknown first-touch vocabulary")
    touch_seconds = (first_touch_time - prediction).total_seconds()
    time_to_tp = touch_seconds if first_touch in TP_TOUCHES else None
    time_to_sl = touch_seconds if first_touch in SL_TOUCHES else None
    mae = _optional_nonnegative(row.get("maximum_adverse_excursion_r"), "mae_r")
    mfe = _optional_nonnegative(row.get("maximum_favorable_excursion_r"), "mfe_r")

    flags = list(outcome.quality_flags)
    if mae is None:
        flags.append("missing_mae_r")
    if mfe is None:
        flags.append("missing_mfe_r")
    path_count: int | None = None
    path_evidence_contract = ""
    path_hash = ""
    path_raw_hash = ""
    path_verified = False
    if outcome.execution_observation_mode == "delayed_historical_bid_ask_replay":
        attribution = _mapping(row.get("attribution"), "attribution")
        replay = _mapping(attribution.get("replay_evidence"), "replay_evidence")
        path_count = _positive_integer(
            replay.get("path_tick_count_to_exit"),
            "path_tick_count_to_exit",
        )
        path_hash = _sha256_text(
            replay.get("path_source_ids_sha256"),
            "path_source_ids_sha256",
        )
        path_evidence_contract = str(replay.get("label_path_evidence_contract") or "")
        raw_hash_value = replay.get("path_raw_payload_hashes_sha256")
        if path_evidence_contract == LABEL_PATH_EVIDENCE_CONTRACT and raw_hash_value is not None:
            path_raw_hash = _sha256_text(
                raw_hash_value,
                "path_raw_payload_hashes_sha256",
            )
            path_verified = True
        else:
            flags.append("raw_path_hash_not_available")
    else:
        flags.append("excursion_path_not_verified")
    label_quality = (
        HASH_BOUND_LABEL_QUALITY
        if path_verified and mae is not None and mfe is not None
        else "incomplete"
    )
    if path_verification is not None:
        _validate_path_verification_binding(
            path_verification,
            row=row,
            path_count=path_count,
            path_hash=path_hash,
            path_raw_hash=path_raw_hash,
            mae=mae,
            mfe=mfe,
        )
        label_quality = RAW_REPLAYED_LABEL_QUALITY
    source_close_payload = canonical_close_payload_from_virtual_row(row, outcome)
    source_close_record_sha256 = _sha256_text(
        row.get("close_record_sha256"),
        "close_record_sha256",
    )
    if _sha256(source_close_payload) != source_close_record_sha256:
        raise InvalidCanonicalLabelEvidence("immutable close record hash mismatch")

    evidence = CanonicalLabelEvidence(
        # Aggregate-only evidence must not consume the append-only v2 key.  A
        # later independently replayed row can then coexist as the v2 upgrade
        # instead of conflicting with its own incomplete predecessor.
        evidence_version=(
            LABEL_EVIDENCE_VERSION
            if path_verification is not None
            else LEGACY_LABEL_EVIDENCE_VERSION
        ),
        decision_id=outcome.decision_id,
        label_version=outcome.label_version,
        label_provenance=outcome.label_provenance,
        canonical_outcome_sha256=outcome_hash,
        source_contract=VIRTUAL_LEARNING_CONTRACT,
        source_close_record_sha256=source_close_record_sha256,
        source_close_payload=source_close_payload,
        symbol=outcome.symbol,
        timeframe=outcome.timeframe,
        direction=outcome.direction,
        prediction_time=prediction.isoformat(),
        holding_end_time=holding_end.isoformat(),
        label_available_time=_aware(
            outcome.label_available_time,
            "label_available_time",
        ).isoformat(),
        label_ingested_time=_aware(
            outcome.label_ingested_time,
            "label_ingested_time",
        ).isoformat(),
        first_touch=first_touch,
        first_touch_ts=first_touch_time.isoformat(),
        entry_bid=_required_number(outcome.entry_bid, "entry_bid"),
        entry_ask=_required_number(outcome.entry_ask, "entry_ask"),
        entry_executable=_required_number(outcome.entry_executable, "entry_executable"),
        exit_bid=_required_number(outcome.exit_bid, "exit_bid"),
        exit_ask=_required_number(outcome.exit_ask, "exit_ask"),
        exit_executable=_required_number(outcome.exit_executable, "exit_executable"),
        exit_mid=_required_number(outcome.exit_mid, "exit_mid"),
        gross_r=_required_number(outcome.gross_realized_r, "gross_r"),
        spread_cost_r=_required_number(
            outcome.realized_spread_cost_r,
            "spread_cost_r",
        ),
        slippage_cost_r=_required_number(outcome.slippage_r, "slippage_cost_r"),
        commission_cost_r=_required_number(outcome.commission_r, "commission_cost_r"),
        financing_cost_r=_required_number(outcome.financing_r, "financing_cost_r"),
        conversion_cost_r=_required_number(outcome.conversion_r, "conversion_cost_r"),
        execution_cost_r=_required_number(outcome.execution_cost_r, "execution_cost_r"),
        net_r=_required_number(outcome.realized_net_r, "net_r"),
        mae_r=mae,
        mfe_r=mfe,
        time_to_tp=time_to_tp,
        time_to_sl=time_to_sl,
        holding_seconds=(holding_end - prediction).total_seconds(),
        label_quality=label_quality,
        path_quality=outcome.path_quality,
        path_source=outcome.path_source,
        path_evidence_contract=path_evidence_contract,
        path_tick_count=path_count,
        path_source_ids_sha256=path_hash,
        path_raw_payload_hashes_sha256=path_raw_hash,
        quality_flags=tuple(dict.fromkeys(flags)),
        research_only=True,
        promotion_eligible=False,
        path_verification_contract=(
            path_verification.contract if path_verification is not None else ""
        ),
        quote_log_prefix_size_bytes=(
            path_verification.quote_log_prefix_size_bytes if path_verification is not None else None
        ),
        quote_log_prefix_sha256=(
            path_verification.quote_log_prefix_sha256 if path_verification is not None else ""
        ),
        raw_blob_count=(
            path_verification.raw_blob_count if path_verification is not None else None
        ),
        raw_blob_hashes_sha256=(
            path_verification.raw_blob_hashes_sha256 if path_verification is not None else ""
        ),
    )
    validate_canonical_label_evidence(evidence)
    validate_evidence_binding(evidence, outcome)
    return evidence


def validate_canonical_label_evidence(evidence: CanonicalLabelEvidence) -> None:
    """Validate one evidence payload without trusting its store envelope."""

    if evidence.evidence_version not in {
        LEGACY_LABEL_EVIDENCE_VERSION,
        LABEL_EVIDENCE_VERSION,
    }:
        raise InvalidCanonicalLabelEvidence("unknown label evidence version")
    if evidence.evidence_version == LEGACY_LABEL_EVIDENCE_VERSION:
        if evidence.label_quality == RAW_REPLAYED_LABEL_QUALITY:
            raise InvalidCanonicalLabelEvidence("legacy evidence cannot claim raw replay quality")
        if any(
            value not in {None, ""}
            for value in (
                evidence.path_verification_contract,
                evidence.quote_log_prefix_size_bytes,
                evidence.quote_log_prefix_sha256,
                evidence.raw_blob_count,
                evidence.raw_blob_hashes_sha256,
            )
        ):
            raise InvalidCanonicalLabelEvidence("legacy evidence contains v2-only fields")
    elif evidence.label_quality != RAW_REPLAYED_LABEL_QUALITY:
        raise InvalidCanonicalLabelEvidence("v2 evidence requires raw replay quality")
    for name in (
        "decision_id",
        "label_version",
        "label_provenance",
        "symbol",
        "timeframe",
        "direction",
        "first_touch",
        "path_source",
    ):
        _text(getattr(evidence, name), name)
    if evidence.source_contract != VIRTUAL_LEARNING_CONTRACT:
        raise InvalidCanonicalLabelEvidence("unsupported evidence source contract")
    _sha256_text(evidence.canonical_outcome_sha256, "canonical_outcome_sha256")
    _sha256_text(evidence.source_close_record_sha256, "source_close_record_sha256")
    close_payload = _mapping(evidence.source_close_payload, "source_close_payload")
    if _sha256(close_payload) != evidence.source_close_record_sha256:
        raise InvalidCanonicalLabelEvidence("source close payload hash mismatch")
    if evidence.direction not in {"long", "short"}:
        raise InvalidCanonicalLabelEvidence("direction must be long or short")
    if evidence.first_touch not in KNOWN_TOUCHES:
        raise InvalidCanonicalLabelEvidence("unknown first-touch vocabulary")

    prediction = _aware(evidence.prediction_time, "prediction_time")
    holding_end = _aware(evidence.holding_end_time, "holding_end_time")
    first_touch = _aware(evidence.first_touch_ts, "first_touch_ts")
    available = _aware(evidence.label_available_time, "label_available_time")
    ingested = _aware(evidence.label_ingested_time, "label_ingested_time")
    if not prediction <= first_touch <= holding_end <= available <= ingested:
        raise InvalidCanonicalLabelEvidence("evidence knowledge interval is reversed")
    holding_seconds = _required_nonnegative(evidence.holding_seconds, "holding_seconds")
    if not _same(holding_seconds, (holding_end - prediction).total_seconds()):
        raise InvalidCanonicalLabelEvidence("holding_seconds does not reconcile")
    expected_touch_seconds = (first_touch - prediction).total_seconds()
    expected_tp = expected_touch_seconds if evidence.first_touch in TP_TOUCHES else None
    expected_sl = expected_touch_seconds if evidence.first_touch in SL_TOUCHES else None
    if not _same_optional(evidence.time_to_tp, expected_tp):
        raise InvalidCanonicalLabelEvidence("time_to_tp does not reconcile")
    if not _same_optional(evidence.time_to_sl, expected_sl):
        raise InvalidCanonicalLabelEvidence("time_to_sl does not reconcile")
    if evidence.first_touch in TERMINAL_TOUCHES and first_touch != holding_end:
        raise InvalidCanonicalLabelEvidence("terminal touch must equal holding end")

    for name in (
        "entry_bid",
        "entry_ask",
        "entry_executable",
        "exit_bid",
        "exit_ask",
        "exit_executable",
        "exit_mid",
        "gross_r",
        "spread_cost_r",
        "slippage_cost_r",
        "commission_cost_r",
        "financing_cost_r",
        "conversion_cost_r",
        "execution_cost_r",
        "net_r",
    ):
        _required_number(getattr(evidence, name), name)
    if not (0 < evidence.entry_bid <= evidence.entry_ask):
        raise InvalidCanonicalLabelEvidence("entry bid/ask is crossed or non-positive")
    if not (0 < evidence.exit_bid <= evidence.exit_ask):
        raise InvalidCanonicalLabelEvidence("exit bid/ask is crossed or non-positive")
    if not _same(evidence.exit_mid, (evidence.exit_bid + evidence.exit_ask) / 2.0):
        raise InvalidCanonicalLabelEvidence("exit_mid does not reconcile")
    expected_entry = evidence.entry_ask if evidence.direction == "long" else evidence.entry_bid
    expected_exit = evidence.exit_bid if evidence.direction == "long" else evidence.exit_ask
    if not _same(evidence.entry_executable, expected_entry) or not _same(
        evidence.exit_executable,
        expected_exit,
    ):
        raise InvalidCanonicalLabelEvidence("executable quote uses the wrong bid/ask side")
    costs = (
        evidence.spread_cost_r,
        evidence.slippage_cost_r,
        evidence.commission_cost_r,
        evidence.financing_cost_r,
        evidence.conversion_cost_r,
    )
    if any(value < -ACCOUNTING_TOLERANCE for value in costs):
        raise InvalidCanonicalLabelEvidence("label costs cannot be negative")
    if not _same(evidence.execution_cost_r, sum(costs)) or not _same(
        evidence.net_r,
        evidence.gross_r - evidence.execution_cost_r,
    ):
        raise InvalidCanonicalLabelEvidence("label R accounting does not reconcile")
    _optional_nonnegative(evidence.mae_r, "mae_r")
    _optional_nonnegative(evidence.mfe_r, "mfe_r")
    if evidence.path_quality is not None:
        quality = _required_nonnegative(evidence.path_quality, "path_quality")
        if quality > 1:
            raise InvalidCanonicalLabelEvidence("path_quality must be within [0, 1]")
    if evidence.path_tick_count is not None and evidence.path_tick_count <= 0:
        raise InvalidCanonicalLabelEvidence("path_tick_count must be positive")
    if evidence.path_source_ids_sha256:
        _sha256_text(evidence.path_source_ids_sha256, "path_source_ids_sha256")
    if evidence.path_evidence_contract not in {"", LABEL_PATH_EVIDENCE_CONTRACT}:
        raise InvalidCanonicalLabelEvidence("unknown path evidence contract")
    if evidence.path_raw_payload_hashes_sha256:
        _sha256_text(
            evidence.path_raw_payload_hashes_sha256,
            "path_raw_payload_hashes_sha256",
        )
    if evidence.label_quality in {HASH_BOUND_LABEL_QUALITY, RAW_REPLAYED_LABEL_QUALITY}:
        if (
            evidence.mae_r is None
            or evidence.mfe_r is None
            or evidence.path_tick_count is None
            or not evidence.path_source_ids_sha256
            or evidence.path_evidence_contract != LABEL_PATH_EVIDENCE_CONTRACT
            or not evidence.path_raw_payload_hashes_sha256
            or evidence.path_source != "delayed_historical_bid_ask_replay"
            or HISTORICAL_NONTRADABLE_FLAG not in evidence.quality_flags
        ):
            raise InvalidCanonicalLabelEvidence("hash-bound label quality lacks path evidence")
        if evidence.label_quality == RAW_REPLAYED_LABEL_QUALITY:
            if (
                evidence.path_verification_contract != RAW_PATH_VERIFICATION_CONTRACT
                or evidence.quote_log_prefix_size_bytes is None
                or evidence.quote_log_prefix_size_bytes <= 0
                or not evidence.quote_log_prefix_sha256
                or evidence.raw_blob_count is None
                or evidence.raw_blob_count <= 0
                or not evidence.raw_blob_hashes_sha256
            ):
                raise InvalidCanonicalLabelEvidence(
                    "raw-replayed label quality lacks immutable preimage evidence"
                )
            _sha256_text(evidence.quote_log_prefix_sha256, "quote_log_prefix_sha256")
            _sha256_text(evidence.raw_blob_hashes_sha256, "raw_blob_hashes_sha256")
    elif evidence.label_quality != "incomplete":
        raise InvalidCanonicalLabelEvidence("unknown label quality")
    if evidence.research_only is not True or evidence.promotion_eligible is not False:
        raise InvalidCanonicalLabelEvidence("label evidence must remain research-only")
    _validate_close_payload_binding(evidence, close_payload)


def _validate_path_verification_binding(
    verification: CanonicalPathVerification,
    *,
    row: Mapping[str, object],
    path_count: int | None,
    path_hash: str,
    path_raw_hash: str,
    mae: float | None,
    mfe: float | None,
) -> None:
    if verification.contract != RAW_PATH_VERIFICATION_CONTRACT:
        raise InvalidCanonicalLabelEvidence("unknown raw path verification contract")
    exact = {
        "path_tick_count": (verification.path_tick_count, path_count),
        "path_source_ids_sha256": (verification.path_source_ids_sha256, path_hash),
        "path_raw_payload_hashes_sha256": (
            verification.path_raw_payload_hashes_sha256,
            path_raw_hash,
        ),
        "exit_trigger": (verification.exit_trigger, row.get("close_reason")),
        "exit_time": (verification.exit_time, row.get("label_end_time")),
    }
    for name, (actual, expected) in exact.items():
        if actual != expected:
            raise InvalidCanonicalLabelEvidence(f"raw path binding mismatch: {name}")
    numeric = {
        "exit_bid": (verification.exit_bid, row.get("exit_bid")),
        "exit_ask": (verification.exit_ask, row.get("exit_ask")),
        "exit_executable": (verification.exit_executable, row.get("exit_executable")),
        "mae_r": (verification.mae_r, mae),
        "mfe_r": (verification.mfe_r, mfe),
    }
    for name, (actual, expected) in numeric.items():
        if not _same_optional(actual, expected):
            raise InvalidCanonicalLabelEvidence(f"raw path numeric binding mismatch: {name}")
    replay = _mapping(
        _mapping(row.get("attribution"), "attribution").get("replay_evidence"),
        "replay_evidence",
    )
    if replay.get("source_size_bytes") != verification.quote_log_prefix_size_bytes:
        raise InvalidCanonicalLabelEvidence("raw path source prefix size mismatch")
    if replay.get("source_prefix_sha256") != verification.quote_log_prefix_sha256:
        raise InvalidCanonicalLabelEvidence("raw path source prefix hash mismatch")
    if verification.research_only is not True or verification.promotion_eligible is not False:
        raise InvalidCanonicalLabelEvidence("raw path verification must remain research-only")


def canonical_close_payload_from_virtual_row(
    row: Mapping[str, object],
    outcome: CanonicalTradeOutcome,
) -> dict[str, object]:
    """Reconstruct the exact immutable virtual close record payload."""

    attribution = _mapping(row.get("attribution"), "attribution")
    failures = row.get("failures")
    if not isinstance(failures, Sequence) or isinstance(failures, (str, bytes)):
        raise InvalidCanonicalLabelEvidence("failures must be an array")
    return {
        "trade_id": _text(row.get("trade_id"), "trade_id"),
        "close_time": _aware(row.get("label_end_time"), "label_end_time").isoformat(),
        "close_reason": _text(row.get("close_reason"), "close_reason"),
        "exit_bid": _required_number(row.get("exit_bid"), "exit_bid"),
        "exit_ask": _required_number(row.get("exit_ask"), "exit_ask"),
        "exit_mid": _required_number(row.get("exit_mid"), "exit_mid"),
        "exit_executable": _required_number(
            row.get("exit_executable"),
            "exit_executable",
        ),
        "quote_to_jpy_rate": _required_number(
            row.get("exit_quote_to_jpy_rate"),
            "exit_quote_to_jpy_rate",
        ),
        "quote_conversion_source": _text(
            row.get("exit_quote_conversion_source"),
            "exit_quote_conversion_source",
        ),
        "gross_market_pnl_jpy": _required_integer(
            attribution.get("gross_market_pnl_jpy"),
            "gross_market_pnl_jpy",
        ),
        "executable_pnl_jpy": _required_integer(
            outcome.executable_pnl_jpy,
            "executable_pnl_jpy",
        ),
        "spread_quote_cost_jpy": _required_integer(
            attribution.get("spread_quote_cost_jpy"),
            "spread_quote_cost_jpy",
        ),
        "slippage_cost_jpy": _required_integer(
            attribution.get("slippage_cost_jpy"),
            "slippage_cost_jpy",
        ),
        "commission_jpy": _required_integer(
            attribution.get("commission_jpy"),
            "commission_jpy",
        ),
        "financing_jpy": _required_integer(
            attribution.get("financing_jpy"),
            "financing_jpy",
        ),
        "conversion_cost_jpy": _required_integer(
            attribution.get("conversion_cost_jpy"),
            "conversion_cost_jpy",
        ),
        "net_pnl_jpy": _required_integer(row.get("net_pnl_jpy"), "net_pnl_jpy"),
        "realized_net_r": _required_number(row.get("realized_net_r"), "realized_net_r"),
        "maximum_favorable_excursion_r": _optional_nonnegative(
            row.get("maximum_favorable_excursion_r"),
            "maximum_favorable_excursion_r",
        ),
        "maximum_adverse_excursion_r": _optional_nonnegative(
            row.get("maximum_adverse_excursion_r"),
            "maximum_adverse_excursion_r",
        ),
        "quote": dict(_mapping(row.get("exit_quote"), "exit_quote")),
        "attribution": dict(attribution),
        "failures": list(failures),
        "learning_eligible": True,
    }


def _validate_close_payload_binding(
    evidence: CanonicalLabelEvidence,
    close_payload: Mapping[str, object],
) -> None:
    exact = {
        "close_time": evidence.holding_end_time,
        "close_reason": evidence.first_touch,
        "maximum_favorable_excursion_r": evidence.mfe_r,
        "maximum_adverse_excursion_r": evidence.mae_r,
    }
    _text(close_payload.get("quote_conversion_source"), "close quote_conversion_source")
    for name, expected in exact.items():
        if close_payload.get(name) != expected:
            raise InvalidCanonicalLabelEvidence(f"close payload binding mismatch: {name}")
    numeric = {
        "exit_bid": evidence.exit_bid,
        "exit_ask": evidence.exit_ask,
        "exit_mid": evidence.exit_mid,
        "exit_executable": evidence.exit_executable,
        "realized_net_r": evidence.net_r,
    }
    for name, expected in numeric.items():
        if not _same_optional(close_payload.get(name), expected):
            raise InvalidCanonicalLabelEvidence(f"close payload numeric binding mismatch: {name}")
    attribution = _mapping(close_payload.get("attribution"), "close attribution")
    replay = _mapping(attribution.get("replay_evidence"), "close replay_evidence")
    replay_exact = {
        "path_tick_count_to_exit": evidence.path_tick_count,
        "path_source_ids_sha256": evidence.path_source_ids_sha256,
        "label_path_evidence_contract": evidence.path_evidence_contract,
        "path_raw_payload_hashes_sha256": evidence.path_raw_payload_hashes_sha256,
    }
    if evidence.label_quality == RAW_REPLAYED_LABEL_QUALITY:
        replay_exact.update(
            {
                "source_size_bytes": evidence.quote_log_prefix_size_bytes,
                "source_prefix_sha256": evidence.quote_log_prefix_sha256,
            }
        )
    for name, expected in replay_exact.items():
        actual = replay.get(name)
        if expected in {None, ""}:
            actual = actual if actual is not None else "" if expected == "" else None
        if actual != expected:
            raise InvalidCanonicalLabelEvidence(f"close path binding mismatch: {name}")


def validate_evidence_binding(
    evidence: CanonicalLabelEvidence,
    outcome: CanonicalTradeOutcome,
) -> None:
    """Prove the sidecar is an exact extension of one canonical outcome."""

    validate_canonical_label_evidence(evidence)
    flags = canonical_net_label_contract_flags(outcome.to_dict())
    if flags:
        raise InvalidCanonicalLabelEvidence(
            f"canonical outcome contract violation: {','.join(flags)}"
        )
    if evidence.canonical_outcome_sha256 != canonical_outcome_sha256(outcome):
        raise InvalidCanonicalLabelEvidence("canonical outcome binding hash mismatch")
    exact = {
        "decision_id": outcome.decision_id,
        "label_version": outcome.label_version,
        "label_provenance": outcome.label_provenance,
        "symbol": outcome.symbol,
        "timeframe": outcome.timeframe,
        "direction": outcome.direction,
        "prediction_time": outcome.prediction_time,
        "holding_end_time": outcome.holding_end_time,
        "label_available_time": outcome.label_available_time,
        "label_ingested_time": outcome.label_ingested_time,
        "first_touch": outcome.first_touch,
        "first_touch_ts": outcome.first_touch_ts,
        "source_close_record_sha256": outcome.close_record_sha256,
        "path_source": outcome.path_source,
    }
    for name, exact_expected in exact.items():
        if getattr(evidence, name) != exact_expected:
            raise InvalidCanonicalLabelEvidence(f"canonical binding mismatch: {name}")
    numeric = {
        "entry_bid": outcome.entry_bid,
        "entry_ask": outcome.entry_ask,
        "entry_executable": outcome.entry_executable,
        "exit_bid": outcome.exit_bid,
        "exit_ask": outcome.exit_ask,
        "exit_executable": outcome.exit_executable,
        "exit_mid": outcome.exit_mid,
        "gross_r": outcome.gross_realized_r,
        "spread_cost_r": outcome.realized_spread_cost_r,
        "slippage_cost_r": outcome.slippage_r,
        "commission_cost_r": outcome.commission_r,
        "financing_cost_r": outcome.financing_r,
        "conversion_cost_r": outcome.conversion_r,
        "execution_cost_r": outcome.execution_cost_r,
        "net_r": outcome.realized_net_r,
        "path_quality": outcome.path_quality,
    }
    for name, numeric_expected in numeric.items():
        if not _same_optional(getattr(evidence, name), numeric_expected):
            raise InvalidCanonicalLabelEvidence(f"canonical numeric binding mismatch: {name}")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidCanonicalLabelEvidence(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidCanonicalLabelEvidence(f"{name} is required")
    return text


def _aware(value: object, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise InvalidCanonicalLabelEvidence(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidCanonicalLabelEvidence(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _required_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidCanonicalLabelEvidence(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidCanonicalLabelEvidence(f"{name} must be finite")
    return number


def _required_nonnegative(value: object, name: str) -> float:
    number = _required_number(value, name)
    if number < 0:
        raise InvalidCanonicalLabelEvidence(f"{name} must be non-negative")
    return number


def _required_integer(value: object, name: str) -> int:
    number = _required_number(value, name)
    integer = int(number)
    if number != integer:
        raise InvalidCanonicalLabelEvidence(f"{name} must be an integer")
    return integer


def _optional_nonnegative(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _required_nonnegative(value, name)


def _positive_integer(value: object, name: str) -> int:
    number = _required_number(value, name)
    integer = int(number)
    if integer <= 0 or number != integer:
        raise InvalidCanonicalLabelEvidence(f"{name} must be a positive integer")
    return integer


def _sha256_text(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise InvalidCanonicalLabelEvidence(f"{name} must be a SHA-256 hex digest")
    return text


def _same(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=ACCOUNTING_TOLERANCE)


def _same_optional(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if (
        isinstance(left, bool)
        or not isinstance(left, (int, float))
        or isinstance(right, bool)
        or not isinstance(right, (int, float))
    ):
        return False
    return _same(float(left), float(right))


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CanonicalLabelEvidence",
    "InvalidCanonicalLabelEvidence",
    "HASH_BOUND_LABEL_QUALITY",
    "LABEL_EVIDENCE_VERSION",
    "LEGACY_LABEL_EVIDENCE_VERSION",
    "LABEL_PATH_EVIDENCE_CONTRACT",
    "RAW_REPLAYED_LABEL_QUALITY",
    "VERIFIED_LABEL_QUALITIES",
    "canonical_close_payload_from_virtual_row",
    "canonical_label_evidence_from_virtual_row",
    "validate_canonical_label_evidence",
    "validate_evidence_binding",
]
