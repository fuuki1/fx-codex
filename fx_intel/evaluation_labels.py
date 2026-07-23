"""Canonical metadata for paper-trade net-R labels.

The analysis service has no broker fills.  Its net label therefore uses the
decision-time executable quote plus the later executable quote path.  Spread is
embedded in those quotes; the initial cost model deliberately adds zero
slippage and commission rather than silently inventing fills.  A future model
change must use a new model id and label version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

NET_LABEL_VERSION = "net-r-v2"
NET_LABEL_PROVENANCE = "paper_quote_model"
FIXTURE_COST_MODEL_ID = "fixture-executable-quotes-zero-slippage-v1"
IBKR_PAPER_COST_MODEL_ID = "ibkr-paper-executable-quotes-zero-slippage-v1"
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
KNOWN_NET_LABEL_VERSIONS = frozenset({NET_LABEL_VERSION})
KNOWN_NET_LABEL_PROVENANCES = frozenset({NET_LABEL_PROVENANCE})
KNOWN_EXECUTABLE_COST_MODEL_IDS = frozenset({FIXTURE_COST_MODEL_ID, IBKR_PAPER_COST_MODEL_ID})
COST_MODEL_QUOTE_SOURCES = {
    FIXTURE_COST_MODEL_ID: frozenset({"fixture", "fixture_quotes"}),
    IBKR_PAPER_COST_MODEL_ID: frozenset({"ibkr_paper_snapshot"}),
}
ZERO_COST_TOLERANCE = 1e-12


@dataclass(frozen=True)
class CostModelResult:
    """Provider-neutral cost model metadata for one canonical outcome.

    Executable bid/ask already embeds spread, so ``total_cost_r`` intentionally
    contains only additional slippage, commission, and financing deductions.
    ``entry_spread_r`` is an audit diagnostic and is never subtracted again.
    """

    cost_model_id: str
    cost_model_version: str
    entry_quote_source: str
    spread_source: str
    slippage_model_id: str
    commission_model_id: str
    entry_spread_r: float | None = None
    slippage_r: float = 0.0
    commission_r: float = 0.0
    financing_r: float = 0.0
    cost_status: str = DEFAULT_COST_STATUS
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_cost_r(self) -> float:
        return self.slippage_r + self.commission_r + self.financing_r

    def to_dict(self) -> dict[str, object]:
        return {
            "cost_model_id": self.cost_model_id,
            "cost_model_version": self.cost_model_version,
            "entry_quote_source": self.entry_quote_source,
            "spread_source": self.spread_source,
            "slippage_model_id": self.slippage_model_id,
            "commission_model_id": self.commission_model_id,
            "entry_spread_r": self.entry_spread_r,
            "slippage_r": self.slippage_r,
            "commission_r": self.commission_r,
            "financing_r": self.financing_r,
            "total_cost_r": self.total_cost_r,
            "cost_status": self.cost_status,
            "quality_flags": list(self.quality_flags),
        }


def executable_cost_model_id_for_source(source: str) -> str | None:
    """Resolve an executable cost model only for explicitly supported quote sources."""

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
        if cost.cost_status != DEFAULT_COST_STATUS:
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
        if cost.quality_flags:
            flags.append("declared_cost_quality_flag")
    return tuple(flags)


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
