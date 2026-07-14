from __future__ import annotations

from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def sample_prices_path() -> str:
    return str(EXAMPLES / "sample_prices.csv")


@pytest.fixture
def sample_events_path() -> str:
    return str(EXAMPLES / "sample_events.csv")


@pytest.fixture
def sample_df():
    from fx_backtester import data as d

    return d.load_prices(EXAMPLES / "sample_prices.csv")
