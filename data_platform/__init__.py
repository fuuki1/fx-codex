"""Authoritative point-in-time data platform.

Common ingestion contracts (``PITRecord`` and per-source payloads), an immutable
content-addressed raw store, dataset-quality classification and a lineage
registry. Every source normalises into ``PITRecord`` so the single point-in-time
rule — research at time ``t`` may only read records whose ``available_at <= t`` —
is enforced identically everywhere.

Real bid/ask, macro, calendar and news feeds are declared here as contracts with
fixtures/mocks only; no live external connection is validated in this package.
"""

from __future__ import annotations

from data_platform.contracts.pit_record import (
    PITContractError,
    PITRecord,
    filter_available_at,
)

__all__ = ["PITRecord", "PITContractError", "filter_available_at"]
