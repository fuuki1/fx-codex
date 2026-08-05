"""Canonical metadata for simulated and paper-trade net-R labels.

The analysis service has no broker fills.  Its net label therefore uses the
decision-time executable quote plus the later executable quote path.  Spread is
embedded in those quotes; the initial cost model deliberately adds zero
slippage and commission rather than silently inventing fills.  A future model
change must use a new model id and label version.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, UTC
from decimal import Decimal, ROUND_HALF_UP
import math

NET_LABEL_VERSION = "net-r-v2"
NET_LABEL_PROVENANCE = "paper_quote_model"
FULL_COST_NET_LABEL_VERSION = "net-r-v3"
VIRTUAL_PORTFOLIO_LABEL_PROVENANCE = "virtual_portfolio_simulated_fill"
VIRTUAL_PORTFOLIO_COST_STATUS = "complete_simulated_full_cost_research_only"
FIXTURE_COST_MODEL_ID = "fixture-executable-quotes-zero-slippage-v1"
IBKR_PAPER_COST_MODEL_ID = "ibkr-paper-executable-quotes-zero-slippage-v1"
# Dukascopy は確定済みの過去1時間を取り込む historical download であり、
# 「その瞬間に約定できたはずの気配」ではない(collector自身がそう申告する)。
# 実測spreadは本物なので net-R の計算には使えるが、executable と名乗らせない。
# そのため KNOWN_EXECUTABLE_COST_MODEL_IDS には**入れない**。
DUKASCOPY_HISTORICAL_COST_MODEL_ID = "dukascopy-historical-measured-spread-v1"
DUKASCOPY_HISTORICAL_COST_STATUS = "quote_measured_historical_not_executable"
DUKASCOPY_HISTORICAL_QUOTE_SOURCE = "dukascopy_historical_datafeed"
# Existing call sites use DEFAULT for deterministic fixtures.
DEFAULT_COST_MODEL_ID = FIXTURE_COST_MODEL_ID
DEFAULT_COST_STATUS = "quote_measured_modelled_execution"
DEFAULT_SLIPPAGE_R = 0.0
DEFAULT_COMMISSION_R = 0.0
DEFAULT_COST_MODEL_VERSION = "1"
DEFAULT_SLIPPAGE_MODEL_ID = "zero-slippage-v1"
DEFAULT_COMMISSION_MODEL_ID = "zero-commission-v1"
DIAGNOSTIC_COST_MODEL_ID = "scanner-proxy-mid-diagnostic-v1"
DIAGNOSTIC_COST_STATUS = "diagnostic_only"
DIAGNOSTIC_EXECUTION_COST_MODEL_ID = "spread-plus-modelled-slippage-diagnostic-v1"
MISSING_COST_MODEL_ID = "missing"
MISSING_COST_STATUS = "unavailable"
KNOWN_NET_LABEL_VERSIONS = frozenset({NET_LABEL_VERSION, FULL_COST_NET_LABEL_VERSION})
KNOWN_NET_LABEL_PROVENANCES = frozenset({NET_LABEL_PROVENANCE, VIRTUAL_PORTFOLIO_LABEL_PROVENANCE})
PROMOTION_NET_LABEL_VERSIONS = frozenset({NET_LABEL_VERSION})
PROMOTION_NET_LABEL_PROVENANCES = frozenset({NET_LABEL_PROVENANCE})
KNOWN_EXECUTABLE_COST_MODEL_IDS = frozenset({FIXTURE_COST_MODEL_ID, IBKR_PAPER_COST_MODEL_ID})
# 実測spreadで net-R を計算できるが、執行可能性は主張しないモデル。
# executable と区別したまま測定を可能にするための独立した層。
KNOWN_MEASURED_NONEXECUTABLE_COST_MODEL_IDS = frozenset({DUKASCOPY_HISTORICAL_COST_MODEL_ID})
COST_MODEL_QUOTE_SOURCES = {
    FIXTURE_COST_MODEL_ID: frozenset({"fixture", "fixture_quotes"}),
    IBKR_PAPER_COST_MODEL_ID: frozenset({"ibkr_paper_snapshot"}),
    DUKASCOPY_HISTORICAL_COST_MODEL_ID: frozenset({DUKASCOPY_HISTORICAL_QUOTE_SOURCE}),
}
# cost_status は cost_model_id ごとに期待値が異なる。executable 系は
# DEFAULT_COST_STATUS、historical 系は not_executable であることを要求する。
COST_MODEL_EXPECTED_STATUS = {
    FIXTURE_COST_MODEL_ID: DEFAULT_COST_STATUS,
    IBKR_PAPER_COST_MODEL_ID: DEFAULT_COST_STATUS,
    DUKASCOPY_HISTORICAL_COST_MODEL_ID: DUKASCOPY_HISTORICAL_COST_STATUS,
}
# historical datafeed であること自体を示す品質フラグ。これは「欠陥」ではなく
# 恒久的な性質なので、契約違反として扱わず素通しする(消さずに伝播させる)。
EXPECTED_COST_QUALITY_FLAGS = {
    DUKASCOPY_HISTORICAL_COST_MODEL_ID: frozenset({"historical_download_not_tradable"}),
}
ZERO_COST_TOLERANCE = 1e-12


@dataclass(frozen=True)
class CostModelResult:
    """Provider-neutral cost model metadata for one canonical outcome.

    Executable bid/ask already embeds spread, so ``total_cost_r`` intentionally
    contains only additional slippage, commission, financing, and conversion
    deductions.
    ``entry_spread_r`` is an audit diagnostic and is never subtracted again.
    """

    cost_model_id: str
    cost_model_version: str
    entry_quote_source: str
    spread_source: str
    slippage_model_id: str
    commission_model_id: str
    conversion_model_id: str = "none-v1"
    entry_spread_r: float | None = None
    slippage_r: float = 0.0
    commission_r: float = 0.0
    financing_r: float = 0.0
    conversion_r: float = 0.0
    cost_status: str = DEFAULT_COST_STATUS
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_cost_r(self) -> float:
        return self.slippage_r + self.commission_r + self.financing_r + self.conversion_r

    def to_dict(self) -> dict[str, object]:
        return {
            "cost_model_id": self.cost_model_id,
            "cost_model_version": self.cost_model_version,
            "entry_quote_source": self.entry_quote_source,
            "spread_source": self.spread_source,
            "slippage_model_id": self.slippage_model_id,
            "commission_model_id": self.commission_model_id,
            "conversion_model_id": self.conversion_model_id,
            "entry_spread_r": self.entry_spread_r,
            "slippage_r": self.slippage_r,
            "commission_r": self.commission_r,
            "financing_r": self.financing_r,
            "conversion_r": self.conversion_r,
            "total_cost_r": self.total_cost_r,
            "cost_status": self.cost_status,
            "quality_flags": list(self.quality_flags),
        }


def executable_cost_model_id_for_source(source: str) -> str | None:
    """Resolve an executable cost model only for explicitly supported quote sources."""

    normalized = source.strip()
    for cost_model_id, sources in COST_MODEL_QUOTE_SOURCES.items():
        if cost_model_id not in KNOWN_EXECUTABLE_COST_MODEL_IDS:
            continue
        if normalized in sources:
            return cost_model_id
    return None


def measured_cost_model_id_for_source(source: str) -> str | None:
    """Resolve any cost model backed by measured bid/ask, executable or not.

    Callers that need executability must keep using
    :func:`executable_cost_model_id_for_source`.  This resolver exists so that
    net-R can be measured from a real spread without the quote claiming it was
    tradable at that instant.
    """

    normalized = source.strip()
    for cost_model_id, sources in COST_MODEL_QUOTE_SOURCES.items():
        if normalized in sources:
            return cost_model_id
    return None


def cost_model_contract_flags(cost: CostModelResult) -> tuple[str, ...]:
    """Return deterministic provenance/value mismatches for a declared cost model."""

    flags: list[str] = []
    allowed_sources = COST_MODEL_QUOTE_SOURCES.get(cost.cost_model_id)
    if allowed_sources is None:
        flags.append("unknown_cost_model")
    else:
        if cost.entry_quote_source not in allowed_sources:
            flags.append("cost_model_entry_source_mismatch")
        if cost.spread_source not in allowed_sources:
            flags.append("cost_model_spread_source_mismatch")
        if cost.cost_model_version != DEFAULT_COST_MODEL_VERSION:
            flags.append("cost_model_version_mismatch")
        if cost.cost_status != COST_MODEL_EXPECTED_STATUS.get(
            cost.cost_model_id, DEFAULT_COST_STATUS
        ):
            flags.append("cost_status_mismatch")
        if cost.slippage_model_id != DEFAULT_SLIPPAGE_MODEL_ID:
            flags.append("slippage_model_mismatch")
        if cost.commission_model_id != DEFAULT_COMMISSION_MODEL_ID:
            flags.append("commission_model_mismatch")
        if not _is_zero(cost.slippage_r):
            flags.append("slippage_model_value_mismatch")
        if not _is_zero(cost.commission_r):
            flags.append("commission_model_value_mismatch")
        if not _is_zero(cost.financing_r):
            flags.append("financing_model_missing")
        if not _is_zero(cost.conversion_r):
            flags.append("conversion_model_missing")
        # このcost modelに固有の性質を示すフラグ(例: historical download)は
        # 契約違反ではない。想定外のフラグだけを違反として報告する。
        expected_flags = EXPECTED_COST_QUALITY_FLAGS.get(cost.cost_model_id, frozenset())
        if set(cost.quality_flags) - set(expected_flags):
            flags.append("declared_cost_quality_flag")
    return tuple(flags)


def canonical_net_label_contract_flags(record: Mapping[str, object]) -> tuple[str, ...]:
    """Validate one persisted canonical net-R label at a downstream read boundary."""

    flags: list[str] = []
    label_version = record.get("label_version")
    label_provenance = record.get("label_provenance")
    full_cost = label_version == FULL_COST_NET_LABEL_VERSION
    if record.get("net_label_eligible") is not True:
        flags.append("net_label_not_eligible")
    if not str(record.get("decision_id") or "").strip():
        flags.append("missing_decision_id")
    if label_version not in KNOWN_NET_LABEL_VERSIONS:
        flags.append("unknown_label_version")
    if label_provenance not in KNOWN_NET_LABEL_PROVENANCES:
        flags.append("unknown_label_provenance")
    symbol = str(record.get("symbol") or "").upper()
    for separator in ("/", "_", "-"):
        symbol = symbol.replace(separator, "")
    if len(symbol) != 6 or not symbol.isalpha():
        flags.append("invalid_symbol")
    direction = str(record.get("direction") or "").lower()
    if direction not in {"long", "short"}:
        flags.append("invalid_direction")
    if not str(record.get("timeframe") or "").strip():
        flags.append("missing_timeframe")
    horizon_hours = _number(record.get("horizon_hours"))
    if horizon_hours is None or horizon_hours <= 0:
        flags.append("invalid_horizon_hours")
    prediction_time = _aware_time(record.get("prediction_time"))
    holding_end_time = _aware_time(record.get("holding_end_time"))
    if prediction_time is None:
        flags.append("invalid_prediction_time")
    if holding_end_time is None:
        flags.append("invalid_holding_end_time")
    if (
        prediction_time is not None
        and holding_end_time is not None
        and holding_end_time <= prediction_time
    ):
        flags.append("invalid_holding_interval")
    if full_cost:
        available_time = _aware_time(record.get("label_available_time"))
        ingested_time = _aware_time(record.get("label_ingested_time"))
        if available_time is None:
            flags.append("invalid_label_available_time")
        if ingested_time is None:
            flags.append("invalid_label_ingested_time")
        if (
            holding_end_time is not None
            and available_time is not None
            and ingested_time is not None
            and not holding_end_time <= available_time <= ingested_time
        ):
            flags.append("invalid_label_knowledge_interval")

    required_names = [
        "entry_bid",
        "entry_ask",
        "planned_risk_distance",
        "quote_realized_r",
        "gross_realized_r",
        "entry_spread_r",
        "slippage_r",
        "commission_r",
        "financing_r",
        "additional_cost_r",
        "execution_cost_r",
        "realized_net_r",
        "path_quality",
    ]
    if full_cost:
        required_names.extend(
            (
                "entry_executable",
                "exit_bid",
                "exit_ask",
                "exit_executable",
                "exit_mid",
                "realized_spread_cost_r",
                "conversion_r",
                "planned_risk_jpy",
                "gross_market_pnl_jpy",
                "executable_pnl_jpy",
                "spread_quote_cost_jpy",
                "slippage_cost_jpy",
                "commission_jpy",
                "financing_jpy",
                "conversion_cost_jpy",
                "net_pnl_jpy",
                "units",
                "entry_quote_to_jpy_rate",
                "exit_quote_to_jpy_rate",
            )
        )
    required = {name: _number(record.get(name)) for name in required_names}
    for name, value in required.items():
        if value is None:
            flags.append(f"missing_or_invalid_{name}")

    string_names = [
        "cost_model_id",
        "cost_model_version",
        "cost_status",
        "entry_quote_source",
        "spread_source",
        "slippage_model_id",
        "commission_model_id",
    ]
    if full_cost:
        string_names.extend(
            (
                "conversion_model_id",
                "cost_source",
                "exit_quote_source",
                "execution_observation_mode",
                "entry_source_record_id",
                "exit_source_record_id",
                "entry_quote_revision_id",
                "exit_quote_revision_id",
                "entry_knowledge_basis",
                "label_knowledge_basis",
                "entry_quote_conversion_source",
                "exit_quote_conversion_source",
            )
        )
    string_fields = {name: record.get(name) for name in string_names}
    for string_name, string_value in string_fields.items():
        if not isinstance(string_value, str) or not string_value:
            flags.append(f"missing_or_invalid_{string_name}")
    raw_cost_flags = record.get("cost_quality_flags")
    if not isinstance(raw_cost_flags, (list, tuple)) or any(
        not isinstance(flag, str) for flag in raw_cost_flags
    ):
        flags.append("invalid_cost_quality_flags")

    if full_cost:
        label_available = _aware_time(record.get("label_available_time"))
        entry_quote_event = _aware_time(record.get("entry_quote_event_time"))
        exit_quote_event = _aware_time(record.get("exit_quote_event_time"))
        for prefix in ("entry_quote", "exit_quote"):
            quote_event = _aware_time(record.get(f"{prefix}_event_time"))
            quote_available = _aware_time(record.get(f"{prefix}_available_time"))
            quote_ingested = _aware_time(record.get(f"{prefix}_ingested_time"))
            if quote_event is None or quote_available is None or quote_ingested is None:
                flags.append(f"invalid_{prefix}_knowledge_times")
            elif not quote_event <= quote_available <= quote_ingested:
                flags.append(f"reversed_{prefix}_knowledge_times")
            elif label_available is not None and quote_ingested > label_available:
                flags.append(f"future_{prefix}_asof_match")
        if (
            holding_end_time is not None
            and entry_quote_event is not None
            and not entry_quote_event < holding_end_time
        ):
            flags.append("entry_quote_event_outside_holding_interval")
        if (
            holding_end_time is not None
            and exit_quote_event is not None
            and exit_quote_event > holding_end_time
        ):
            flags.append("exit_quote_event_after_holding_end")
        if (
            record.get("execution_observation_mode") == "delayed_historical_bid_ask_replay"
            and holding_end_time is not None
            and exit_quote_event != holding_end_time
        ):
            flags.append("historical_exit_quote_event_not_holding_end")
        for hash_name in (
            "decision_record_sha256",
            "open_record_sha256",
            "open_observation_sha256",
            "close_record_sha256",
            "close_observation_sha256",
        ):
            hash_value = str(record.get(hash_name) or "")
            if len(hash_value) != 64 or any(
                character not in "0123456789abcdef" for character in hash_value
            ):
                flags.append(f"invalid_{hash_name}")

    if flags:
        return tuple(dict.fromkeys(flags))

    numeric = {name: float(value) for name, value in required.items() if value is not None}
    cost_quality_flags = tuple(raw_cost_flags) if isinstance(raw_cost_flags, (list, tuple)) else ()
    if full_cost:
        if label_provenance != VIRTUAL_PORTFOLIO_LABEL_PROVENANCE:
            flags.append("full_cost_label_provenance_mismatch")
        if string_fields["cost_status"] != VIRTUAL_PORTFOLIO_COST_STATUS:
            flags.append("full_cost_status_mismatch")
        if record.get("costs_complete") is not True:
            flags.append("full_cost_evidence_incomplete")
        if record.get("research_only") is not True:
            flags.append("full_cost_label_not_research_only")
        if record.get("promotion_eligible") is not False:
            flags.append("full_cost_label_promotion_state_invalid")
        observation_mode = str(string_fields["execution_observation_mode"])
        if observation_mode not in {
            "delayed_historical_bid_ask_replay",
            "contemporaneous_quote",
        }:
            flags.append("invalid_execution_observation_mode")
        if observation_mode == "delayed_historical_bid_ask_replay" and (
            "historical_download_not_tradable" not in cost_quality_flags
        ):
            flags.append("missing_historical_nontradable_flag")
        if string_fields["spread_source"] != (
            f"{string_fields['entry_quote_source']}+{string_fields['exit_quote_source']}"
        ):
            flags.append("full_cost_spread_source_mismatch")
    else:
        cost = CostModelResult(
            cost_model_id=str(string_fields["cost_model_id"]),
            cost_model_version=str(string_fields["cost_model_version"]),
            entry_quote_source=str(string_fields["entry_quote_source"]),
            spread_source=str(string_fields["spread_source"]),
            slippage_model_id=str(string_fields["slippage_model_id"]),
            commission_model_id=str(string_fields["commission_model_id"]),
            entry_spread_r=numeric["entry_spread_r"],
            slippage_r=numeric["slippage_r"],
            commission_r=numeric["commission_r"],
            financing_r=numeric["financing_r"],
            cost_status=str(string_fields["cost_status"]),
            quality_flags=cost_quality_flags,
        )
        flags.extend(cost_model_contract_flags(cost))

    entry_bid = numeric["entry_bid"]
    entry_ask = numeric["entry_ask"]
    planned_risk = numeric["planned_risk_distance"]
    if not 0.0 < numeric["path_quality"] <= 1.0:
        flags.append("invalid_path_quality")
    if not (0 < entry_bid <= entry_ask):
        flags.append("invalid_entry_quote")
    if planned_risk <= 0:
        flags.append("invalid_planned_risk_distance")
    if planned_risk > 0 and not math.isclose(
        numeric["entry_spread_r"],
        (entry_ask - entry_bid) / planned_risk,
        abs_tol=1e-8,
    ):
        flags.append("entry_spread_r_mismatch")
    additional_cost_r = (
        numeric["slippage_r"]
        + numeric["commission_r"]
        + numeric["financing_r"]
        + (numeric.get("conversion_r") or 0.0)
    )
    if not math.isclose(
        numeric["additional_cost_r"],
        additional_cost_r,
        abs_tol=1e-8,
    ):
        flags.append("additional_cost_identity_mismatch")
    if not math.isclose(
        numeric["realized_net_r"],
        numeric["quote_realized_r"] - numeric["additional_cost_r"],
        abs_tol=1e-8,
    ):
        flags.append("net_r_identity_mismatch")
    if not math.isclose(
        numeric["execution_cost_r"],
        numeric["gross_realized_r"] - numeric["realized_net_r"],
        abs_tol=1e-8,
    ):
        flags.append("execution_cost_identity_mismatch")
    if full_cost:
        jpy_names = (
            "planned_risk_jpy",
            "gross_market_pnl_jpy",
            "executable_pnl_jpy",
            "spread_quote_cost_jpy",
            "slippage_cost_jpy",
            "commission_jpy",
            "financing_jpy",
            "conversion_cost_jpy",
            "net_pnl_jpy",
        )
        if any(not numeric[name].is_integer() for name in jpy_names):
            flags.append("noninteger_jpy_accounting")
        risk_jpy = numeric["planned_risk_jpy"]
        gross_jpy = numeric["gross_market_pnl_jpy"]
        executable_jpy = numeric["executable_pnl_jpy"]
        spread_jpy = numeric["spread_quote_cost_jpy"]
        extra_jpy = sum(
            numeric[name]
            for name in (
                "slippage_cost_jpy",
                "commission_jpy",
                "financing_jpy",
                "conversion_cost_jpy",
            )
        )
        net_jpy = numeric["net_pnl_jpy"]
        if risk_jpy <= 0:
            flags.append("invalid_planned_risk_jpy")
        if executable_jpy != gross_jpy - spread_jpy:
            flags.append("executable_jpy_identity_mismatch")
        if net_jpy != executable_jpy - extra_jpy:
            flags.append("net_jpy_identity_mismatch")
        if risk_jpy > 0:
            r_jpy_pairs = (
                ("gross_realized_r", "gross_market_pnl_jpy"),
                ("quote_realized_r", "executable_pnl_jpy"),
                ("realized_spread_cost_r", "spread_quote_cost_jpy"),
                ("slippage_r", "slippage_cost_jpy"),
                ("commission_r", "commission_jpy"),
                ("financing_r", "financing_jpy"),
                ("conversion_r", "conversion_cost_jpy"),
                ("realized_net_r", "net_pnl_jpy"),
            )
            if any(
                not math.isclose(numeric[r_name], numeric[jpy_name] / risk_jpy, abs_tol=1e-12)
                for r_name, jpy_name in r_jpy_pairs
            ):
                flags.append("r_jpy_denominator_mismatch")
        units = numeric["units"]
        entry_rate = numeric["entry_quote_to_jpy_rate"]
        exit_rate = numeric["exit_quote_to_jpy_rate"]
        if units <= 0 or entry_rate <= 0 or exit_rate <= 0:
            flags.append("invalid_units_or_conversion_rate")
        else:

            def rounded_jpy(value: Decimal) -> int:
                return int(
                    value.quantize(
                        Decimal("1"),
                        rounding=ROUND_HALF_UP,
                    )
                )

            units_decimal = Decimal(str(units))
            entry_rate_decimal = Decimal(str(entry_rate))
            exit_rate_decimal = Decimal(str(exit_rate))
            entry_bid_decimal = Decimal(str(entry_bid))
            entry_ask_decimal = Decimal(str(entry_ask))
            exit_bid_decimal = Decimal(str(numeric["exit_bid"]))
            exit_ask_decimal = Decimal(str(numeric["exit_ask"]))
            entry_executable_decimal = Decimal(str(numeric["entry_executable"]))
            exit_executable_decimal = Decimal(str(numeric["exit_executable"]))
            planned_risk_decimal = Decimal(str(planned_risk))
            expected_risk_jpy = rounded_jpy(
                units_decimal * planned_risk_decimal * entry_rate_decimal
            )
            entry_mid_decimal = (entry_bid_decimal + entry_ask_decimal) / Decimal("2")
            exit_mid_decimal = (exit_bid_decimal + exit_ask_decimal) / Decimal("2")
            sign = Decimal("1") if direction == "long" else Decimal("-1")
            expected_gross_jpy = rounded_jpy(
                (exit_mid_decimal - entry_mid_decimal) * sign * units_decimal * exit_rate_decimal
            )
            expected_executable_jpy = rounded_jpy(
                (exit_executable_decimal - entry_executable_decimal)
                * sign
                * units_decimal
                * exit_rate_decimal
            )
            if risk_jpy != expected_risk_jpy:
                flags.append("planned_risk_jpy_recalculation_mismatch")
            if gross_jpy != expected_gross_jpy:
                flags.append("gross_jpy_recalculation_mismatch")
            if executable_jpy != expected_executable_jpy:
                flags.append("executable_jpy_recalculation_mismatch")
        exit_bid = numeric["exit_bid"]
        exit_ask = numeric["exit_ask"]
        if not (0 < exit_bid <= exit_ask):
            flags.append("invalid_exit_quote")
        expected_entry = entry_ask if direction == "long" else entry_bid
        expected_exit = exit_bid if direction == "long" else exit_ask
        if not math.isclose(numeric["entry_executable"], expected_entry, abs_tol=1e-12):
            flags.append("entry_executable_side_mismatch")
        if not math.isclose(numeric["exit_executable"], expected_exit, abs_tol=1e-12):
            flags.append("exit_executable_side_mismatch")
        if not math.isclose(
            numeric["exit_mid"],
            (exit_bid + exit_ask) / 2.0,
            abs_tol=1e-12,
        ):
            flags.append("exit_mid_mismatch")
        if not math.isclose(
            numeric["quote_realized_r"],
            numeric["gross_realized_r"] - numeric["realized_spread_cost_r"],
            abs_tol=1e-8,
        ):
            flags.append("spread_cost_identity_mismatch")
        if not math.isclose(
            numeric["execution_cost_r"],
            numeric["realized_spread_cost_r"] + numeric["additional_cost_r"],
            abs_tol=1e-8,
        ):
            flags.append("full_cost_identity_mismatch")
        if any(
            numeric[name] < 0
            for name in (
                "realized_spread_cost_r",
                "slippage_r",
                "commission_r",
                "financing_r",
                "conversion_r",
            )
        ):
            flags.append("negative_full_cost_component")
    return tuple(dict.fromkeys(flags))


def has_executable_entry(entry_bid: float | None, entry_ask: float | None) -> bool:
    """Return whether a valid, ordered decision-time quote is available."""

    return (
        entry_bid is not None
        and entry_ask is not None
        and math.isfinite(entry_bid)
        and math.isfinite(entry_ask)
        and entry_bid > 0
        and entry_ask > 0
        and entry_ask >= entry_bid
    )


def _is_zero(value: float) -> bool:
    return math.isfinite(value) and abs(value) <= ZERO_COST_TOLERANCE


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _aware_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)
