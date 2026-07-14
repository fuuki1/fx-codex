"""Narrow contract hardening tests added during research-v3 integration review."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from data_platform.collect.contract import CollectedQuote, QuoteContractError

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _payload() -> dict[str, object]:
    return {
        "provider": "oanda",
        "account_environment": "practice",
        "instrument": "USDJPY",
        "provider_event_time": NOW,
        "received_at": NOW,
        "bid": 155.001,
        "ask": 155.004,
        "bid_size": None,
        "ask_size": None,
        "tradable": False,
        "sequence_id": None,
        "connection_id": "connection",
        "writer_id": "writer",
        "revision_id": None,
        "raw_payload_sha256": "a" * 64,
        "source_endpoint_class": "replay_fixture",
        "collection_mode": "replay",
    }


@pytest.mark.parametrize(
    "invalid_hash",
    [
        "a" * 63,
        "A" * 64,
        "g" * 64,
        "not-a-sha256",
    ],
)
def test_raw_hash_must_be_lowercase_hex(invalid_hash: str) -> None:
    payload = _payload()
    payload["raw_payload_sha256"] = invalid_hash

    with pytest.raises(QuoteContractError, match="lowercase"):
        CollectedQuote(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid_tradable", ["false", 0, 1, None])
def test_tradable_must_be_actual_bool(invalid_tradable: object) -> None:
    payload = _payload()
    payload["tradable"] = invalid_tradable

    with pytest.raises(QuoteContractError, match="tradable must be a bool"):
        CollectedQuote(**payload)  # type: ignore[arg-type]


def test_from_dict_does_not_coerce_string_false_to_true() -> None:
    quote = CollectedQuote(**_payload())  # type: ignore[arg-type]
    serialized = quote.to_dict()
    serialized["tradable"] = "false"

    with pytest.raises(QuoteContractError, match="tradable must be a bool"):
        CollectedQuote.from_dict(serialized)
