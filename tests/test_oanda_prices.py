"""OANDA bid/ask OHLC取得のネットワーク非依存テスト。"""

from __future__ import annotations

from datetime import datetime, UTC

import pytest

from fx_intel import oanda_prices

CAPTURED = datetime(2026, 7, 17, 4, 41, tzinfo=UTC)


def _candle(*, complete: bool = True, stamp: str = "2026-07-17T04:35:00.000000000Z"):
    return {
        "complete": complete,
        "time": stamp,
        "volume": 42,
        "bid": {"o": "1.14360", "h": "1.14380", "l": "1.14350", "c": "1.14370"},
        "ask": {"o": "1.14362", "h": "1.14382", "l": "1.14352", "c": "1.14372"},
    }


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def test_candle_to_rows_preserves_completed_bid_ask_ohlc() -> None:
    rows = oanda_prices.candle_to_rows(
        "EURUSD",
        _candle(),
        granularity="M5",
        target_timeframes=("15m", "1h"),
        captured_at=CAPTURED,
    )

    assert len(rows) == 2
    assert {row["timeframe"] for row in rows} == {"15m", "1h"}
    row = rows[0]
    assert row["bar_start"] == "2026-07-17T04:35:00+00:00"
    assert row["bar_end"] == "2026-07-17T04:40:00+00:00"
    assert row["ts"] == row["bar_end"]
    assert row["ohlc_scope"] == "completed_bid_ask_bar"
    assert row["bid_high"] == 1.1438
    assert row["ask_low"] == 1.14352
    assert row["close"] == pytest.approx((1.14370 + 1.14372) / 2)
    assert row["spread"] == pytest.approx(0.00002)
    assert row["content_hash"]


def test_candle_to_rows_rejects_incomplete_candle() -> None:
    with pytest.raises(ValueError, match="未完了"):
        oanda_prices.candle_to_rows(
            "EURUSD", _candle(complete=False), granularity="M5", captured_at=CAPTURED
        )


def test_fetch_uses_bid_ask_and_latest_complete_candle() -> None:
    session = _Session(
        {
            "candles": [
                _candle(stamp="2026-07-17T04:30:00Z"),
                _candle(stamp="2026-07-17T04:35:00Z"),
                _candle(complete=False, stamp="2026-07-17T04:40:00Z"),
            ]
        }
    )
    config = oanda_prices.OandaPriceConfig(token="secret-token")

    rows, warnings = oanda_prices.fetch_completed_bid_ask_rows(
        ["EUR/USD"],
        config,
        target_timeframes=("15m",),
        now=CAPTURED,
        session=session,
    )

    assert warnings == []
    assert rows[0]["bar_start"] == "2026-07-17T04:35:00+00:00"
    url, kwargs = session.calls[0]
    assert url.endswith("/v3/instruments/EUR_USD/candles")
    assert kwargs["params"]["price"] == "BA"
    assert kwargs["params"]["granularity"] == "M5"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


def test_config_requires_token_and_reads_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_TOKEN", raising=False)
    with pytest.raises(ValueError, match="OANDA_API_TOKEN"):
        oanda_prices.OandaPriceConfig.from_env(project_root=tmp_path)

    (tmp_path / ".env").write_text(
        "OANDA_API_TOKEN=test-token\nOANDA_ENVIRONMENT=practice\n", encoding="utf-8"
    )
    config = oanda_prices.OandaPriceConfig.from_env(project_root=tmp_path)
    assert config.token == "test-token"
    assert config.base_url == oanda_prices.PRACTICE_URL


def test_parse_decision_quotes_keeps_broker_timestamp_and_role() -> None:
    quotes = oanda_prices.parse_decision_quotes(
        {
            "prices": [
                {
                    "instrument": "USD_JPY",
                    "time": "2026-07-17T04:40:59Z",
                    "tradeable": True,
                    "status": "tradeable",
                    "bids": [{"price": "157.121", "liquidity": 1000000}],
                    "asks": [{"price": "157.129", "liquidity": 1000000}],
                }
            ]
        },
        captured_at=CAPTURED,
    )

    quote = quotes["USDJPY"]
    assert quote["bid"] == 157.121
    assert quote["ask"] == 157.129
    assert quote["role"] == "decision_quote"
    assert quote["source"] == "oanda_v20_pricing"
    assert quote["available_time"] == CAPTURED.isoformat()
    assert quote["content_hash"]


def test_fetch_decision_quotes_requires_account_id() -> None:
    quotes, warnings = oanda_prices.fetch_decision_quotes(
        ["USDJPY"], oanda_prices.OandaPriceConfig(token="secret")
    )
    assert quotes == {}
    assert "OANDA_ACCOUNT_ID" in warnings[0]
