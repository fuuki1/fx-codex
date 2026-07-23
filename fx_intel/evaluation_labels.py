"""Canonical metadata for paper-trade net-R labels.

The analysis service has no broker fills.  Its net label therefore uses the
decision-time executable quote plus the later executable quote path.  Spread is
embedded in those quotes; the initial cost model deliberately adds zero
slippage and commission rather than silently inventing fills.  A future model
change must use a new model id and label version.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NET_LABEL_VERSION = "net-r-v1"
NET_LABEL_PROVENANCE = "paper_quote_model"
DEFAULT_COST_MODEL_ID = "executable-quotes-zero-slippage-v1"
DEFAULT_COST_STATUS = "quote_measured_modelled_execution"
DEFAULT_SLIPPAGE_R = 0.0
DEFAULT_COMMISSION_R = 0.0
KNOWN_NET_LABEL_VERSIONS = frozenset({NET_LABEL_VERSION})
KNOWN_NET_LABEL_PROVENANCES = frozenset({NET_LABEL_PROVENANCE})
KNOWN_EXECUTABLE_COST_MODEL_IDS = frozenset({DEFAULT_COST_MODEL_ID})


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


def has_executable_entry(entry_bid: float | None, entry_ask: float | None) -> bool:
    """Return whether a valid, ordered decision-time quote is available."""

    return (
        entry_bid is not None
        and entry_ask is not None
        and entry_bid > 0
        and entry_ask > 0
        and entry_ask >= entry_bid
    )
