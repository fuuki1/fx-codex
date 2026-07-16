"""Broker quote-source adapters.

A ``QuoteSource`` yields :class:`~data_platform.contracts.market_quote.MarketQuote`
records for one instrument. The interface is deliberately small so a real broker
adapter, a replay-from-fixture adapter and a mock can all satisfy it identically.

No real broker adapter is implemented here — we have no credentials, so a live
connection is *unvalidated*. :class:`UnimplementedQuoteSource` fails closed if a
pipeline asks for a source that does not exist yet, rather than silently
returning nothing (which would read as "no data" and could be mistaken for a
quiet market).

Single-writer discipline: each source declares one ``writer_id`` and stamps it
on every quote, so a downstream store can enforce "one writer per dataset".
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Protocol, runtime_checkable

from data_platform.contracts.market_quote import MarketQuote
from data_platform.contracts.pit_record import PITContractError


class QuoteSourceError(RuntimeError):
    """Raised when a quote source cannot honour its contract."""


@runtime_checkable
class QuoteSource(Protocol):
    """A source of ordered, single-writer quotes for one instrument."""

    @property
    def source_id(self) -> str: ...

    @property
    def writer_id(self) -> str: ...

    @property
    def instrument(self) -> str: ...

    def quotes(self) -> Iterator[MarketQuote]:
        """Yield quotes in non-decreasing ``sequence_id`` order."""
        ...


class ReplayQuoteSource:
    """Replays a fixed list of quotes deterministically.

    Used for research replay and tests: given the same fixture, it yields the
    same quotes in the same order every time. It enforces the ordering and
    single-writer invariants on construction so a malformed fixture fails fast
    rather than producing an out-of-order stream downstream.
    """

    def __init__(
        self,
        source_id: str,
        writer_id: str,
        instrument: str,
        quotes: Sequence[MarketQuote],
    ) -> None:
        if not source_id.strip() or not writer_id.strip() or not instrument.strip():
            raise QuoteSourceError("source_id, writer_id and instrument are required")
        self._source_id = source_id
        self._writer_id = writer_id
        self._instrument = instrument
        self._quotes = tuple(quotes)
        self._validate()

    def _validate(self) -> None:
        last_sequence: int | None = None
        for quote in self._quotes:
            if quote.instrument != self._instrument:
                raise QuoteSourceError(
                    f"quote instrument {quote.instrument} != source instrument {self._instrument}"
                )
            if quote.writer_id != self._writer_id:
                raise QuoteSourceError(
                    "a single source must have exactly one writer_id; "
                    f"found {quote.writer_id} and {self._writer_id}"
                )
            if last_sequence is not None and quote.sequence_id < last_sequence:
                raise QuoteSourceError(
                    "replay fixture is out of order; sequence_id must be non-decreasing"
                )
            last_sequence = quote.sequence_id

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def writer_id(self) -> str:
        return self._writer_id

    @property
    def instrument(self) -> str:
        return self._instrument

    def quotes(self) -> Iterator[MarketQuote]:
        yield from self._quotes


class MockQuoteSource(ReplayQuoteSource):
    """A tiny synthetic quote source for tests.

    Explicitly a *mock*: its quotes are fabricated, never real market data. It
    exists so higher layers can be exercised without a broker connection; results
    from it are never evidence of real behaviour.
    """


class UnimplementedQuoteSource:
    """A declared-but-unbuilt real broker source that fails closed.

    Instantiating it is fine (it documents intent); *using* it raises, so a
    pipeline can never mistake an unimplemented feed for an empty market.
    """

    def __init__(self, source_id: str, instrument: str) -> None:
        self._source_id = source_id
        self._instrument = instrument

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def writer_id(self) -> str:
        return f"{self._source_id}:unimplemented"

    @property
    def instrument(self) -> str:
        return self._instrument

    def quotes(self) -> Iterator[MarketQuote]:
        raise QuoteSourceError(
            f"quote source {self._source_id!r} has no implemented, credentialed adapter; "
            "a live connection is unvalidated. Refusing to yield an empty stream."
        )


def collect_quotes(source: QuoteSource) -> list[MarketQuote]:
    """Drain a source into a list, re-checking ordering defensively.

    A convenience for the materializer. It re-validates ordering because a
    Protocol implementer other than ``ReplayQuoteSource`` might not.
    """

    collected: list[MarketQuote] = []
    last_sequence: int | None = None
    for quote in source.quotes():
        if last_sequence is not None and quote.sequence_id < last_sequence:
            raise QuoteSourceError("quote source yielded out-of-order sequence_id")
        last_sequence = quote.sequence_id
        collected.append(quote)
    return collected


def normalize_quotes(quotes: Iterable[MarketQuote]) -> list[MarketQuote]:
    """Deduplicate by natural key ``(instrument, sequence_id)`` and order.

    A duplicate natural key with *identical* content is dropped (idempotent
    re-delivery); a duplicate key with *different* content is a hard data defect
    and raises rather than silently picking one.
    """

    by_key: dict[tuple[str, int], MarketQuote] = {}
    for quote in quotes:
        key = (quote.instrument, quote.sequence_id)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = quote
            continue
        if (existing.bid, existing.ask, existing.source_timestamp) != (
            quote.bid,
            quote.ask,
            quote.source_timestamp,
        ):
            raise PITContractError(
                f"duplicate natural key {key} with conflicting content; refusing to guess"
            )
    return [by_key[key] for key in sorted(by_key)]
