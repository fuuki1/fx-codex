from __future__ import annotations

import io
import zipfile

from scripts import fetch_histdata_bidask as fetch


def _zip(name: str, text: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, text)
    return out.getvalue()


def test_bid_and_ask_payloads_merge_to_completed_m5_rows() -> None:
    bid_lines = "".join(
        f"20200102 000{i}00;1.100{i};1.101{i};1.099{i};1.100{i};0\n" for i in range(5)
    )
    ask_lines = "".join(f"20200102 000{i}30;1.101{i};0\n" for i in range(5))
    bid = fetch.bid_m1_to_ohlc(_zip("bid.csv", bid_lines))
    ask = fetch.price_to_ohlc(
        fetch.ask_tick_zip_to_frame(_zip("ask.csv", ask_lines)), price_column="price"
    )

    merged, invalid = fetch.merge_bid_ask("EURUSD", bid, ask)

    assert invalid == 0
    assert len(merged) == 1
    assert merged.iloc[0]["timestamp"] == "2020-01-02T05:05:00+00:00"
    assert merged.iloc[0]["bid_close"] == 1.1004
    assert merged.iloc[0]["ask_close"] == 1.1014
    assert merged.iloc[0]["promotion_admissible"] == False  # noqa: E712
