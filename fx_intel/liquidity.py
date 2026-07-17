"""Broker-spread liquidity proxies with strict as-of baseline construction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, time, UTC
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .input_context import LiquiditySnapshot, QuoteSnapshot
from .market_session import classify_market_session

DEFAULT_POLICY: dict[str, object] = {
    "schema_version": "fx-liquidity-policy-v1",
    "stage": "shadow",
    "lookback_market_days": 20,
    "min_baseline_samples": 100,
    "thin_percentile": 0.90,
    "stressed_percentile": 0.99,
    "max_quote_age_seconds": 600.0,
    "rollover_timezone": "America/New_York",
    "rollover_hour": 17,
    "rollover_minutes_before": 5,
    "rollover_minutes_after": 15,
    "absolute_spread_pips": {},
}


@dataclass(frozen=True)
class LiquidityPolicy:
    schema_version: str = "fx-liquidity-policy-v1"
    stage: str = "shadow"
    lookback_market_days: int = 20
    min_baseline_samples: int = 100
    thin_percentile: float = 0.90
    stressed_percentile: float = 0.99
    max_quote_age_seconds: float = 600.0
    rollover_timezone: str = "America/New_York"
    rollover_hour: int = 17
    rollover_minutes_before: int = 5
    rollover_minutes_after: int = 15
    absolute_spread_pips: Mapping[str, float] | None = None

    @classmethod
    def from_mapping(cls, value: object) -> LiquidityPolicy:
        raw = value if isinstance(value, Mapping) else {}
        defaults = DEFAULT_POLICY
        absolute = raw.get("absolute_spread_pips", defaults["absolute_spread_pips"])
        return cls(
            schema_version=str(raw.get("schema_version", defaults["schema_version"])),
            stage=str(raw.get("stage", defaults["stage"])),
            lookback_market_days=max(1, int(raw.get("lookback_market_days", 20))),
            min_baseline_samples=max(1, int(raw.get("min_baseline_samples", 100))),
            thin_percentile=float(raw.get("thin_percentile", 0.90)),
            stressed_percentile=float(raw.get("stressed_percentile", 0.99)),
            max_quote_age_seconds=max(0.0, float(raw.get("max_quote_age_seconds", 600.0))),
            rollover_timezone=str(raw.get("rollover_timezone", "America/New_York")),
            rollover_hour=int(raw.get("rollover_hour", 17)),
            rollover_minutes_before=max(0, int(raw.get("rollover_minutes_before", 5))),
            rollover_minutes_after=max(0, int(raw.get("rollover_minutes_after", 15))),
            absolute_spread_pips=(
                {str(key): float(item) for key, item in absolute.items()}
                if isinstance(absolute, Mapping)
                else {}
            ),
        )


def load_policy(path: str | Path) -> LiquidityPolicy:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        payload = DEFAULT_POLICY
    return LiquidityPolicy.from_mapping(payload)


def scanner_quote(
    symbol: str,
    *,
    bid: float | None,
    ask: float | None,
    observed_at: datetime,
) -> QuoteSnapshot | None:
    if bid is None and ask is None:
        return None
    stamp = _utc(observed_at).isoformat()
    flags = _quote_flags(bid, ask)
    payload = f"{symbol}|{stamp}|{bid}|{ask}|tradingview_oanda_scanner"
    return QuoteSnapshot(
        bid=bid,
        ask=ask,
        observed_at=stamp,
        available_time=stamp,
        ingested_time=stamp,
        source="tradingview_oanda_scanner",
        role="scanner_quote_proxy",
        source_record_id=f"{symbol}:scanner:{stamp}",
        content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        quality_status="invalid" if flags else "proxy",
        quality_flags=tuple(flags),
    )


def quote_from_mapping(value: object) -> QuoteSnapshot | None:
    if not isinstance(value, Mapping):
        return None
    bid = _number(value.get("bid"))
    ask = _number(value.get("ask"))
    observed = str(value.get("observed_at") or value.get("available_time") or "")
    available = str(value.get("available_time") or observed)
    ingested = str(value.get("ingested_time") or available)
    if not observed:
        return None
    flags = _quote_flags(bid, ask)
    if value.get("tradeable") is False:
        flags.append("not_tradeable")
    return QuoteSnapshot(
        bid=bid,
        ask=ask,
        observed_at=observed,
        available_time=available,
        ingested_time=ingested,
        source=str(value.get("source") or "unknown"),
        role=str(value.get("role") or "decision_quote"),
        source_record_id=str(value.get("source_record_id") or ""),
        content_hash=str(value.get("content_hash") or ""),
        quality_status="invalid" if flags else "measured",
        quality_flags=tuple(flags),
    )


def build_liquidity_snapshot(
    symbol: str,
    *,
    decision_time: datetime,
    quote: QuoteSnapshot | None,
    price_rows: Sequence[Mapping[str, object]],
    session_bucket: str,
    policy: LiquidityPolicy | None = None,
) -> LiquiditySnapshot:
    policy = policy or LiquidityPolicy()
    features: dict[str, float | None] = {
        "spread_price": None,
        "spread_pips": None,
        "spread_bps": None,
        "spread_percentile": None,
        "quote_age_sec": None,
        "baseline_n": None,
        "is_rollover_window": float(is_rollover_window(decision_time, policy)),
    }
    reasons: list[str] = []
    status = "unknown"
    scope = "none"

    bid = quote.bid if quote is not None else None
    ask = quote.ask if quote is not None else None
    quote_flags = _quote_flags(bid, ask)
    if quote is None:
        reasons.append("missing_quote")
    elif quote_flags or quote.quality_status == "invalid":
        status = "invalid"
        reasons.extend(quote_flags or quote.quality_flags or ("invalid_quote",))
    else:
        assert bid is not None and ask is not None
        spread = ask - bid
        mid = (ask + bid) / 2.0
        pip = pip_size(symbol)
        spread_pips = spread / pip
        observed = _parse_time(quote.available_time)
        age = (_utc(decision_time) - observed).total_seconds() if observed is not None else None
        features.update(
            {
                "spread_price": spread,
                "spread_pips": spread_pips,
                "spread_bps": spread / mid * 10_000.0 if mid > 0 else None,
                "quote_age_sec": age,
            }
        )
        if age is None:
            reasons.append("missing_quote_time")
        elif age < 0:
            status = "invalid"
            reasons.append("future_quote")
        elif age > policy.max_quote_age_seconds:
            reasons.append("stale_quote")

        baseline, scope = _baseline_spreads(
            symbol,
            price_rows,
            decision_time=decision_time,
            session_bucket=session_bucket,
            policy=policy,
        )
        features["baseline_n"] = float(len(baseline))
        if len(baseline) < policy.min_baseline_samples:
            reasons.append("baseline_insufficient")
        else:
            percentile = sum(1 for item in baseline if item <= spread) / len(baseline)
            features["spread_percentile"] = percentile
            hard_limit = (policy.absolute_spread_pips or {}).get(symbol.upper())
            hard_stress = hard_limit is not None and spread_pips >= hard_limit
            if status == "invalid":
                pass
            elif hard_stress:
                status = "stressed"
                reasons.append("absolute_spread_limit")
            elif percentile >= policy.stressed_percentile:
                status = "stressed"
                reasons.append(
                    "spread_p99_same_session" if scope == "session" else "spread_p99_symbol"
                )
            elif percentile >= policy.thin_percentile:
                status = "thin"
                reasons.append(
                    "spread_p90_same_session" if scope == "session" else "spread_p90_symbol"
                )
            elif status != "invalid":
                status = "normal"
        if "stale_quote" in reasons and status not in {"invalid", "stressed"}:
            status = "unknown"

    masks = {key: int(_number(value) is not None) for key, value in features.items()}
    payload = {
        "schema_version": "fx-liquidity-proxy-v1",
        "symbol": symbol,
        "decision_time": _utc(decision_time).isoformat(),
        "status": status,
        "reason_codes": sorted(set(reasons)),
        "features": features,
        "quote": quote.to_dict() if quote is not None else None,
        "baseline_scope": scope,
        "policy_version": policy.schema_version,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return LiquiditySnapshot(
        snapshot_id=f"liquidity:sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}",
        status=status,
        reason_codes=tuple(sorted(set(reasons))),
        features=features,
        feature_masks=masks,
        quote=quote,
        baseline_scope=scope,
        policy_version=policy.schema_version,
    )


def pip_size(symbol: str) -> float:
    cleaned = symbol.upper().replace("/", "").replace("_", "")
    return 0.01 if cleaned.endswith("JPY") else 0.0001


def is_rollover_window(moment: datetime, policy: LiquidityPolicy | None = None) -> bool:
    policy = policy or LiquidityPolicy()
    zone = ZoneInfo(policy.rollover_timezone)
    local = _utc(moment).astimezone(zone)
    rollover = datetime.combine(local.date(), time(policy.rollover_hour), tzinfo=zone)
    return (
        rollover - timedelta(minutes=policy.rollover_minutes_before)
        <= local
        < rollover + timedelta(minutes=policy.rollover_minutes_after)
    )


def _baseline_spreads(
    symbol: str,
    rows: Sequence[Mapping[str, object]],
    *,
    decision_time: datetime,
    session_bucket: str,
    policy: LiquidityPolicy,
) -> tuple[list[float], str]:
    deduped: dict[str, tuple[float, str, datetime]] = {}
    for row in rows:
        if str(row.get("symbol", "")).upper().replace("/", "") != symbol.upper().replace("/", ""):
            continue
        available = _parse_time(row.get("available_time") or row.get("ts"))
        if available is None or available >= _utc(decision_time):
            continue
        spread = _number(row.get("spread"))
        if spread is None or spread <= 0:
            bid = _number(row.get("bid") or row.get("bid_close"))
            ask = _number(row.get("ask") or row.get("ask_close"))
            spread = ask - bid if bid is not None and ask is not None and ask >= bid else None
        if spread is None or spread <= 0:
            continue
        record = _base_record_id(row, available)
        bucket, _active = classify_market_session(available)
        if bucket == "closed":
            continue
        deduped[record] = (spread, bucket, available)
    observed_dates = sorted({available.date() for _spread, _bucket, available in deduped.values()})
    allowed_dates = set(observed_dates[-policy.lookback_market_days :])
    recent = [item for item in deduped.values() if item[2].date() in allowed_dates]
    same_session = [spread for spread, bucket, _available in recent if bucket == session_bucket]
    if len(same_session) >= policy.min_baseline_samples:
        return same_session, "session"
    all_symbol = [spread for spread, _bucket, _available in recent]
    return all_symbol, (
        "symbol" if len(all_symbol) >= policy.min_baseline_samples else "insufficient"
    )


def _base_record_id(row: Mapping[str, object], available: datetime) -> str:
    symbol = str(row.get("symbol", ""))
    source = str(row.get("source", "unknown"))
    start = str(row.get("bar_start") or row.get("event_time") or available.isoformat())
    granularity = str(row.get("bar_granularity", "unknown"))
    return f"{source}:{symbol}:{granularity}:{start}"


def _quote_flags(bid: float | None, ask: float | None) -> list[str]:
    flags: list[str] = []
    if bid is None or ask is None:
        flags.append("incomplete_bid_ask")
    elif bid <= 0 or ask <= 0:
        flags.append("non_positive_quote")
    elif ask < bid:
        flags.append("ask_below_bid")
    return flags


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        return _utc(datetime.fromisoformat(str(value or "").replace("Z", "+00:00")))
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
