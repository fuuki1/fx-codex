from __future__ import annotations


def test_parse_candles_happy_path():
    import oanda

    doc = {
        "candles": [
            {"time": "2024-01-01T00:00:00.000000000Z", "complete": True,
             "mid": {"o": "147.100", "h": "147.200", "l": "147.050", "c": "147.180"}},
            {"time": "2024-01-01T00:05:00.000000000Z", "complete": False,
             "mid": {"o": "147.180", "h": "147.250", "l": "147.150", "c": "147.210"}},
        ]
    }
    df = oanda.parse_candles(doc)
    assert list(df.columns) == ["time", "open", "high", "low", "close", "complete"]
    assert len(df) == 2
    assert df["close"].iloc[0] == 147.18
    assert df["complete"].iloc[1] is False or df["complete"].iloc[1] == False  # noqa: E712
    assert str(df["time"].dt.tz) == "UTC"


def test_parse_candles_skips_broken_and_handles_empty():
    import oanda

    doc = {"candles": [
        {"time": "2024-01-01T00:00:00Z", "mid": {"o": "1", "h": "2", "l": "0.5"}},  # c 欠落 -> skip
        {"time": "2024-01-01T00:05:00Z", "mid": {"o": "1", "h": "2", "l": "0.5", "c": "1.5"}},
    ]}
    df = oanda.parse_candles(doc)
    assert len(df) == 1
    assert oanda.parse_candles({}).empty


def test_fetch_candles_returns_none_without_token(monkeypatch):
    import oanda

    monkeypatch.setattr(oanda.settings, "oanda_api_token", "")
    # トークンが無ければネットワークに触れず None（起動しても落ちない）
    assert oanda.fetch_candles("USD_JPY", "M5", 100) is None
