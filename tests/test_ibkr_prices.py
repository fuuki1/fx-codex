from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

import pytest

from fx_intel import ibkr_prices
from fx_intel import trade_outcome


@dataclass
class Contract:
    symbol: str
    currency: str


@dataclass
class Ticker:
    contract: Contract
    bid: float
    ask: float
    time: datetime


@dataclass
class Bar:
    date: datetime
    open: float
    high: float
    low: float
    close: float


def test_parse_tickers_builds_executable_quote_contract() -> None:
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    quotes = ibkr_prices.parse_tickers(
        [Ticker(Contract("USD", "JPY"), 157.25, 157.27, now - timedelta(seconds=1))],
        captured_at=now,
    )

    quote = quotes["USDJPY"]
    assert quote["source"] == "ibkr_paper_snapshot"
    assert quote["bid"] == 157.25
    assert quote["ask"] == 157.27
    assert quote["spread"] == pytest.approx(0.02)
    assert quote["role"] == "decision_quote"


def test_completed_pair_excludes_forming_bar_and_rows_keep_bid_ask() -> None:
    now = datetime(2026, 7, 17, 12, 7, tzinfo=UTC)
    completed_start = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    forming_start = datetime(2026, 7, 17, 12, 5, tzinfo=UTC)
    bids = [
        Bar(completed_start, 157.20, 157.30, 157.10, 157.25),
        Bar(forming_start, 157.25, 157.35, 157.22, 157.32),
    ]
    asks = [
        Bar(completed_start, 157.22, 157.32, 157.12, 157.27),
        Bar(forming_start, 157.27, 157.37, 157.24, 157.34),
    ]

    pair = ibkr_prices.latest_completed_pair(bids, asks, captured_at=now)
    assert pair == (bids[0], asks[0])
    rows = ibkr_prices.bars_to_rows(
        "USDJPY", pair[0], pair[1], target_timeframes=("15m", "1h"), captured_at=now
    )

    assert len(rows) == 2
    assert rows[0]["complete"] is True
    assert rows[0]["source"] == "ibkr_paper_historical"
    assert rows[0]["ohlc_scope"] == "completed_bid_ask_bar"
    assert rows[0]["bar_end"] == "2026-07-17T12:05:00+00:00"
    assert rows[0]["bid_close"] == 157.25
    assert rows[0]["ask_close"] == 157.27
    assert rows[0]["close"] == pytest.approx(157.26)


def test_live_port_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IBKR_PORT", "4001")
    with pytest.raises(ValueError, match="live port 4001"):
        ibkr_prices.IbkrPriceConfig.from_env()


def test_ibkr_quote_and_completed_bars_generate_canonical_pure_r_label() -> None:
    decision_time = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    quote = ibkr_prices.parse_tickers(
        [Ticker(Contract("EUR", "USD"), 99.99, 100.01, decision_time)],
        captured_at=decision_time,
    )["EURUSD"]
    rows: list[dict[str, object]] = [
        {
            "ts": decision_time.isoformat(),
            "symbol": "EURUSD",
            "timeframe": "15m",
            "direction": "long",
            "close": 100.0,
            "entry_bid": quote["bid"],
            "entry_ask": quote["ask"],
            "quote_observed_at": quote["observed_at"],
            "stop": 99.0,
            "target1": 102.0,
            "target2": 103.0,
            "slippage_r": 0.01,
            "commission_r": 0.01,
            "cost_model_id": "ibkr-paper-v1",
        }
    ]
    for index, close in enumerate((100.1, 100.2, 100.3)):
        start = decision_time + timedelta(minutes=5 * index)
        bid = Bar(start, close - 0.05, close + 0.05, close - 0.10, close)
        ask = Bar(start, close - 0.03, close + 0.07, close - 0.08, close + 0.02)
        rows.extend(
            ibkr_prices.bars_to_rows(
                "EURUSD", bid, ask, target_timeframes=("15m",), captured_at=start + timedelta(minutes=5)
            )
        )

    outcome = trade_outcome.evaluate_trade_outcomes(
        rows, horizon_hours=0.25, tolerance_hours=0.1, min_path_points=3
    )[0]

    assert outcome.net_label_eligible is True
    assert outcome.path_source == "bid_ask_ohlc"
    assert outcome.realized_net_r == pytest.approx(0.27)
    assert outcome.label_provenance == "paper_quote_model"
