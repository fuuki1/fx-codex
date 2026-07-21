"""Canonical metadata for paper-trade net-R labels.

The analysis service has no broker fills.  Its net label therefore uses the
decision-time executable quote plus the later executable quote path.  Spread is
embedded in those quotes; the initial cost model deliberately adds zero
slippage and commission rather than silently inventing fills.  A future model
change must use a new model id and label version.
"""

from __future__ import annotations

NET_LABEL_VERSION = "net-r-v1"
NET_LABEL_PROVENANCE = "paper_quote_model"
DEFAULT_COST_MODEL_ID = "executable-quotes-zero-slippage-v1"
DEFAULT_COST_STATUS = "quote_measured_modelled_execution"
DEFAULT_SLIPPAGE_R = 0.0
DEFAULT_COMMISSION_R = 0.0


def has_executable_entry(entry_bid: float | None, entry_ask: float | None) -> bool:
    """Return whether a valid, ordered decision-time quote is available."""

    return (
        entry_bid is not None
        and entry_ask is not None
        and entry_bid > 0
        and entry_ask > 0
        and entry_ask >= entry_bid
    )
