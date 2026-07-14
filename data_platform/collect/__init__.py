"""Read-only market/macro data collection (raw-first, fail-closed).

This package NEVER touches an order path: no order/trade/position endpoints,
no executor imports, no live-trading switches. Enforced by
``tests/test_collect_no_order_path.py``.
"""

from data_platform.collect.contract import CollectedQuote, QuoteContractError
from data_platform.collect.raw_first import IngestResult, QuoteLog, ingest_payload

__all__ = [
    "CollectedQuote",
    "QuoteContractError",
    "IngestResult",
    "QuoteLog",
    "ingest_payload",
]
