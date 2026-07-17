"""Point-in-time decision inputs shared by fusion and per-timeframe paths.

The objects in this module are immutable audit contracts.  They deliberately
separate numeric features from provenance and missing-value masks so an absent
macro value cannot silently become a real zero or a neutral market opinion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, UTC
import hashlib
import json

from .calendar import symbol_currencies
from .macro import MacroSeries, MacroSnapshot, macro_pair_view

CONTEXT_SCHEMA_VERSION = "decision-input-v1"
MACRO_SCHEMA_VERSION = "macro-features-v1"
LIQUIDITY_SCHEMA_VERSION = "fx-liquidity-proxy-v1"


@dataclass(frozen=True)
class PointInTimeValue:
    value: float
    unit: str
    event_time: str
    available_time: str
    ingested_time: str
    first_seen_time: str
    source: str
    source_record_id: str
    content_hash: str
    quality_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "unit": self.unit,
            "event_time": self.event_time,
            "available_time": self.available_time,
            "ingested_time": self.ingested_time,
            "first_seen_time": self.first_seen_time,
            "source": self.source,
            "source_record_id": self.source_record_id,
            "content_hash": self.content_hash,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True)
class MacroFeatureSnapshot:
    snapshot_id: str
    features: Mapping[str, float | None]
    feature_masks: Mapping[str, int]
    values: Mapping[str, PointInTimeValue]
    quality: float
    quality_status: str
    missing: tuple[str, ...]
    schema_version: str = MACRO_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "features": dict(self.features),
            "feature_masks": dict(self.feature_masks),
            "values": {key: value.to_dict() for key, value in self.values.items()},
            "quality": self.quality,
            "quality_status": self.quality_status,
            "missing": list(self.missing),
        }


@dataclass(frozen=True)
class QuoteSnapshot:
    bid: float | None
    ask: float | None
    observed_at: str
    available_time: str
    ingested_time: str
    source: str
    role: str
    source_record_id: str
    content_hash: str
    quality_status: str
    quality_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "bid": self.bid,
            "ask": self.ask,
            "observed_at": self.observed_at,
            "available_time": self.available_time,
            "ingested_time": self.ingested_time,
            "source": self.source,
            "role": self.role,
            "source_record_id": self.source_record_id,
            "content_hash": self.content_hash,
            "quality_status": self.quality_status,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True)
class LiquiditySnapshot:
    snapshot_id: str
    status: str
    reason_codes: tuple[str, ...]
    features: Mapping[str, float | None]
    feature_masks: Mapping[str, int]
    quote: QuoteSnapshot | None
    baseline_scope: str
    policy_version: str
    schema_version: str = LIQUIDITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "features": dict(self.features),
            "feature_masks": dict(self.feature_masks),
            "quote": self.quote.to_dict() if self.quote is not None else None,
            "baseline_scope": self.baseline_scope,
            "policy_version": self.policy_version,
        }

    def shadow_gate_trace(self) -> dict[str, object]:
        would_block = self.status in {"stressed", "invalid"}
        return {
            "gate": "liquidity",
            "policy_version": self.policy_version,
            "stage": "shadow",
            "input_status": self.status,
            "would_block": would_block,
            "would_dampen": self.status == "thin",
            "applied": False,
            "reason_codes": list(self.reason_codes),
            "status": "observed",
        }


@dataclass(frozen=True)
class DecisionInputContext:
    context_id: str
    symbol: str
    decision_time: str
    macro: MacroFeatureSnapshot
    liquidity: LiquiditySnapshot
    learning_dimensions: Mapping[str, object]
    run_id: str = ""
    schema: int = 1
    context_schema_version: str = CONTEXT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "context_schema_version": self.context_schema_version,
            "context_id": self.context_id,
            "run_id": self.run_id,
            "symbol": self.symbol,
            "decision_time": self.decision_time,
            "macro": self.macro.to_dict(),
            "liquidity": self.liquidity.to_dict(),
            "learning_dimensions": dict(self.learning_dimensions),
        }

    def flat_features(self, *, atr: float | None = None) -> dict[str, float | None]:
        output = {f"macro__{key}": value for key, value in self.macro.features.items()}
        output.update(
            {f"liquidity__{key}": value for key, value in self.liquidity.features.items()}
        )
        spread = _number(self.liquidity.features.get("spread_price"))
        atr_value = _number(atr)
        output["liquidity__spread_atr"] = (
            spread / atr_value
            if spread is not None and atr_value is not None and atr_value > 0
            else None
        )
        for status in ("normal", "thin", "stressed", "unknown", "invalid"):
            output[f"liquidity__status_{status}"] = float(self.liquidity.status == status)
        return output

    def flat_masks(self, *, atr: float | None = None) -> dict[str, int]:
        output = {f"macro__{key}": int(value) for key, value in self.macro.feature_masks.items()}
        output.update(
            {f"liquidity__{key}": int(value) for key, value in self.liquidity.feature_masks.items()}
        )
        atr_value = _number(atr)
        output["liquidity__spread_atr"] = int(
            self.liquidity.feature_masks.get("spread_price", 0) == 1
            and atr_value is not None
            and atr_value > 0
        )
        for status in ("normal", "thin", "stressed", "unknown", "invalid"):
            output[f"liquidity__status_{status}"] = 1
        return output


def build_macro_feature_snapshot(
    snapshot: MacroSnapshot | None,
    symbol: str,
    *,
    decision_time: datetime,
) -> MacroFeatureSnapshot:
    """Build one pair-specific macro snapshot using only values available as-of the decision."""

    expected = (
        "vix_level",
        "vix_change_5d_pct",
        "us10y_level_pct",
        "us10y_change_5d_bp",
        "us2y_level_pct",
        "us2y_change_5d_bp",
        "curve_2s10s_bp",
        "usd_index_level",
        "usd_index_change_5d_pct",
        "cot_base_net_ratio",
        "cot_quote_net_ratio",
        "cot_pair_diff",
        "macro_pair_score",
        "macro_pair_confidence",
    )
    features: dict[str, float | None] = {key: None for key in expected}
    values: dict[str, PointInTimeValue] = {}
    invalid = False
    if snapshot is not None:
        safe_snapshot = MacroSnapshot(fetched_at=_utc(decision_time))
        series_specs = {
            "vix": ("vix_level", "vix_change_5d_pct", "index", "pct", True),
            "us10y": ("us10y_level_pct", "us10y_change_5d_bp", "pct", "bp", False),
            "us2y": ("us2y_level_pct", "us2y_change_5d_bp", "pct", "bp", False),
            "usd_index": (
                "usd_index_level",
                "usd_index_change_5d_pct",
                "index",
                "pct",
                True,
            ),
        }
        for series_key, (
            level_key,
            change_key,
            level_unit,
            change_unit,
            pct_change,
        ) in series_specs.items():
            series = snapshot.fresh_series(series_key)
            if series is None:
                continue
            envelope = _series_envelope(snapshot, series, level_unit, decision_time)
            if envelope is None:
                invalid = invalid or _provenance_is_future(snapshot, series_key, decision_time)
                continue
            latest = series.last()
            assert latest is not None
            features[level_key] = latest.value
            values[level_key] = envelope
            safe_snapshot.series[series_key] = series
            raw_change = series.change_pct(5) if pct_change else series.change(5)
            if raw_change is not None:
                change = raw_change if pct_change else raw_change * 100.0
                features[change_key] = round(change, 6)
                values[change_key] = _derived_envelope(envelope, change, change_unit, change_key)

        us10y = snapshot.fresh_series("us10y")
        us2y = snapshot.fresh_series("us2y")
        ten_last = us10y.last() if us10y is not None else None
        two_last = us2y.last() if us2y is not None else None
        if us10y is not None and us2y is not None and ten_last is not None and two_last is not None:
            ten = _series_envelope(snapshot, us10y, "pct", decision_time)
            two = _series_envelope(snapshot, us2y, "pct", decision_time)
            if ten is not None and two is not None:
                curve = (ten_last.value - two_last.value) * 100.0
                features["curve_2s10s_bp"] = round(curve, 6)
                values["curve_2s10s_bp"] = _combined_envelope(
                    ten, two, curve, "bp", "curve_2s10s_bp"
                )

        base, quote = symbol_currencies(symbol)
        base_report = snapshot.fresh_cot(base)
        quote_report = snapshot.fresh_cot(quote)
        base_value = _cot_envelope(snapshot, base_report, base, decision_time)
        quote_value = _cot_envelope(snapshot, quote_report, quote, decision_time)
        if base_report is not None and base_value is not None:
            features["cot_base_net_ratio"] = base_report.net_ratio
            values["cot_base_net_ratio"] = base_value
            safe_snapshot.cot[base] = base_report
        if quote_report is not None and quote_value is not None:
            features["cot_quote_net_ratio"] = quote_report.net_ratio
            values["cot_quote_net_ratio"] = quote_value
            safe_snapshot.cot[quote] = quote_report
        if base_value is not None and quote_value is not None and base_report and quote_report:
            diff = base_report.net_ratio - quote_report.net_ratio
            features["cot_pair_diff"] = round(diff, 6)
            values["cot_pair_diff"] = _combined_envelope(
                base_value, quote_value, diff, "ratio", "cot_pair_diff"
            )

        score, confidence, _notes = macro_pair_view(base, quote, safe_snapshot)
        if confidence > 0:
            pair_inputs = [
                value
                for key, value in values.items()
                if key
                in {
                    "cot_base_net_ratio",
                    "cot_quote_net_ratio",
                    "vix_level",
                    "us10y_level_pct",
                    "usd_index_level",
                }
            ]
            if pair_inputs:
                features["macro_pair_score"] = score
                features["macro_pair_confidence"] = confidence
                anchor = pair_inputs[-1]
                values["macro_pair_score"] = _derived_envelope(
                    anchor, score, "score", "macro_pair_score"
                )
                values["macro_pair_confidence"] = _derived_envelope(
                    anchor, confidence, "ratio", "macro_pair_confidence"
                )

    masks = {key: int(_number(value) is not None) for key, value in features.items()}
    missing = tuple(key for key, available in masks.items() if not available)
    quality = round(sum(masks.values()) / len(masks), 4) if masks else 0.0
    status = (
        "invalid"
        if invalid
        else ("unknown" if quality == 0 else ("usable" if quality == 1 else "partial"))
    )
    payload = {
        "schema_version": MACRO_SCHEMA_VERSION,
        "symbol": symbol,
        "features": features,
        "feature_masks": masks,
        "values": {key: value.to_dict() for key, value in values.items()},
        "quality_status": status,
    }
    return MacroFeatureSnapshot(
        snapshot_id=_stable_id("macro", payload),
        features=features,
        feature_masks=masks,
        values=values,
        quality=quality,
        quality_status=status,
        missing=missing,
    )


def build_decision_input_context(
    symbol: str,
    *,
    decision_time: datetime,
    macro: MacroFeatureSnapshot,
    liquidity: LiquiditySnapshot,
    learning_dimensions: Mapping[str, object],
    run_id: str = "",
) -> DecisionInputContext:
    decision = _utc(decision_time).isoformat()
    identity = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "symbol": symbol,
        "decision_time": decision,
        "macro_snapshot_id": macro.snapshot_id,
        "liquidity_snapshot_id": liquidity.snapshot_id,
        "learning_dimensions": dict(learning_dimensions),
    }
    return DecisionInputContext(
        context_id=_stable_id("context", identity),
        symbol=symbol,
        decision_time=decision,
        macro=macro,
        liquidity=liquidity,
        learning_dimensions=dict(learning_dimensions),
        run_id=run_id,
    )


def context_from_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def flat_features_from_mapping(
    value: object, *, atr: float | None = None
) -> tuple[dict[str, float | None], dict[str, int]]:
    """Read a serialized context without requiring callers to reconstruct dataclasses."""

    context = context_from_mapping(value)
    features: dict[str, float | None] = {}
    masks: dict[str, int] = {}
    for section_name in ("macro", "liquidity"):
        section = context.get(section_name)
        if not isinstance(section, Mapping):
            continue
        raw_features = section.get("features")
        raw_masks = section.get("feature_masks")
        if isinstance(raw_features, Mapping):
            for key, raw in raw_features.items():
                features[f"{section_name}__{key}"] = _number(raw)
        if isinstance(raw_masks, Mapping):
            for key, raw in raw_masks.items():
                masks[f"{section_name}__{key}"] = int(bool(raw))
    spread = features.get("liquidity__spread_price")
    atr_value = _number(atr)
    features["liquidity__spread_atr"] = (
        spread / atr_value
        if spread is not None and atr_value is not None and atr_value > 0
        else None
    )
    liquidity_section = context.get("liquidity")
    liquidity_status = (
        str(liquidity_section.get("status") or "unknown")
        if isinstance(liquidity_section, Mapping)
        else "unknown"
    )
    for status in ("normal", "thin", "stressed", "unknown", "invalid"):
        key = f"liquidity__status_{status}"
        features[key] = float(liquidity_status == status)
        masks[key] = 1
    masks["liquidity__spread_atr"] = int(features["liquidity__spread_atr"] is not None)
    for key, raw in features.items():
        masks.setdefault(key, int(raw is not None))
    return features, masks


def decision_quote_from_mapping(
    value: object,
) -> tuple[float | None, float | None, str | None]:
    context = context_from_mapping(value)
    liquidity = context.get("liquidity")
    if not isinstance(liquidity, Mapping):
        return None, None, None
    quote = liquidity.get("quote")
    if not isinstance(quote, Mapping) or quote.get("role") != "decision_quote":
        return None, None, None
    bid = _number(quote.get("bid"))
    ask = _number(quote.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None, None, None
    observed = str(quote.get("observed_at") or quote.get("available_time") or "") or None
    return bid, ask, observed


def macro_score_from_mapping(value: object) -> float | None:
    context = context_from_mapping(value)
    macro = context.get("macro")
    if not isinstance(macro, Mapping):
        return None
    features = macro.get("features")
    return _number(features.get("macro_pair_score")) if isinstance(features, Mapping) else None


def liquidity_gate_trace_from_mapping(value: object) -> dict[str, object] | None:
    context = context_from_mapping(value)
    liquidity = context.get("liquidity")
    if not isinstance(liquidity, Mapping):
        return None
    status = str(liquidity.get("status") or "unknown")
    reasons = liquidity.get("reason_codes")
    reason_codes = [str(item) for item in reasons] if isinstance(reasons, list | tuple) else []
    return {
        "gate": "liquidity",
        "policy_version": str(liquidity.get("policy_version") or "unknown"),
        "stage": "shadow",
        "input_status": status,
        "would_block": status in {"stressed", "invalid"},
        "would_dampen": status == "thin",
        "applied": False,
        "reason_codes": reason_codes,
        "status": "observed",
    }


def _series_envelope(
    snapshot: MacroSnapshot,
    series: MacroSeries,
    unit: str,
    decision_time: datetime,
) -> PointInTimeValue | None:
    last = series.last()
    if last is None:
        return None
    meta = snapshot.provenance.get(series.key, {})
    available = _parse_time(meta.get("first_seen_time") or meta.get("fetched_at"))
    if available is None:
        available = snapshot.fetched_at
    if _utc(available) > _utc(decision_time):
        return None
    event_time = datetime.combine(last.when, time.min, tzinfo=UTC).isoformat()
    content_hash = str(meta.get("content_hash") or _hash_value(series.key, last.when, last.value))
    source = str(meta.get("source") or "fred")
    fetched = str(meta.get("fetched_at") or snapshot.fetched_at.isoformat())
    first_seen = str(meta.get("first_seen_time") or fetched)
    return PointInTimeValue(
        value=float(last.value),
        unit=unit,
        event_time=event_time,
        available_time=first_seen,
        ingested_time=fetched,
        first_seen_time=first_seen,
        source=source,
        source_record_id=f"{series.key}:{last.when.isoformat()}",
        content_hash=content_hash,
    )


def _cot_envelope(
    snapshot: MacroSnapshot,
    report: object,
    currency: str,
    decision_time: datetime,
) -> PointInTimeValue | None:
    if report is None:
        return None
    meta = snapshot.provenance.get("cot", {})
    available = _parse_time(meta.get("first_seen_time") or meta.get("fetched_at"))
    if available is None:
        available = snapshot.fetched_at
    if _utc(available) > _utc(decision_time):
        return None
    report_date = getattr(report, "report_date")
    ratio = float(getattr(report, "net_ratio"))
    fetched = str(meta.get("fetched_at") or snapshot.fetched_at.isoformat())
    first_seen = str(meta.get("first_seen_time") or fetched)
    return PointInTimeValue(
        value=ratio,
        unit="ratio",
        event_time=datetime.combine(report_date, time.min, tzinfo=UTC).isoformat(),
        available_time=first_seen,
        ingested_time=fetched,
        first_seen_time=first_seen,
        source=str(meta.get("source") or "cftc"),
        source_record_id=f"cot:{currency}:{report_date.isoformat()}",
        content_hash=str(meta.get("content_hash") or _hash_value(currency, report_date, ratio)),
    )


def _derived_envelope(
    anchor: PointInTimeValue, value: float, unit: str, key: str
) -> PointInTimeValue:
    return PointInTimeValue(
        value=float(value),
        unit=unit,
        event_time=anchor.event_time,
        available_time=anchor.available_time,
        ingested_time=anchor.ingested_time,
        first_seen_time=anchor.first_seen_time,
        source=anchor.source,
        source_record_id=f"{anchor.source_record_id}:{key}",
        content_hash=_hash_value(anchor.content_hash, key, value),
        quality_flags=anchor.quality_flags,
    )


def _combined_envelope(
    left: PointInTimeValue,
    right: PointInTimeValue,
    value: float,
    unit: str,
    key: str,
) -> PointInTimeValue:
    left_available = _parse_time(left.available_time)
    right_available = _parse_time(right.available_time)
    left_ingested = _parse_time(left.ingested_time)
    right_ingested = _parse_time(right.ingested_time)
    left_first_seen = _parse_time(left.first_seen_time)
    right_first_seen = _parse_time(right.first_seen_time)
    left_event = _parse_time(left.event_time)
    right_event = _parse_time(right.event_time)
    assert left_available and right_available
    assert left_ingested and right_ingested
    assert left_first_seen and right_first_seen
    assert left_event and right_event
    available = max(left_available, right_available)
    ingested = max(left_ingested, right_ingested)
    first_seen = max(left_first_seen, right_first_seen)
    event = max(left_event, right_event)
    return PointInTimeValue(
        value=float(value),
        unit=unit,
        event_time=event.isoformat(),
        available_time=available.isoformat(),
        ingested_time=ingested.isoformat(),
        first_seen_time=first_seen.isoformat(),
        source=f"derived:{left.source}+{right.source}",
        source_record_id=f"{key}:{left.source_record_id}:{right.source_record_id}",
        content_hash=_hash_value(left.content_hash, right.content_hash, key, value),
    )


def _provenance_is_future(snapshot: MacroSnapshot, key: str, decision_time: datetime) -> bool:
    meta = snapshot.provenance.get(key, {})
    available = _parse_time(meta.get("first_seen_time") or meta.get("fetched_at"))
    if available is None:
        available = snapshot.fetched_at
    return available is not None and _utc(available) > _utc(decision_time)


def _stable_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _hash_value(*parts: object) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
