#!/usr/bin/env python3
"""Fetch HistData.com free ASCII M1 forex history and convert to pipeline CSV.

Research-only helper. HistData provides free historical *bars* (open/high/low/
close) — there is NO bid/ask and NO real volume, so any dataset produced here is
CLOSE-ONLY and is NOT admissible for a promotion or performance claim. The
authoritative pipeline's declared static spread applies instead, and label
quality is capped accordingly.

The download is a token+cookie POST flow (HistData's own web form): GET the
per-instrument/year page to obtain a session cookie and a hidden ``tk`` token,
then POST it to ``/get.php`` with a matching ``Referer``. We preserve the exact
downloaded bytes; timestamps are HistData's US/Eastern and are converted to UTC.

Usage:
    python3 scripts/fetch_histdata.py --pair USDJPY --year 2024 \
        --out data/real/histdata/usdjpy_2024_1h.csv --resample 1h
"""

from __future__ import annotations

import argparse
import http.cookiejar
import io
import re
import sys
import urllib.parse
import urllib.request
import zipfile

import pandas as pd

_PAGE = (
    "https://www.histdata.com/download-free-forex-historical-data/"
    "?/ascii/1-minute-bar-quotes/{pair_lower}/{year}"
)
_POST = "https://www.histdata.com/get.php"
_UA = "Mozilla/5.0 (research; fx-codex histdata fetch)"
_TK_RE = re.compile(r'name="tk"[^>]*value="([0-9a-f]+)"')


def download_zip(pair: str, year: int, *, timeout: float = 90.0) -> bytes:
    """Return the raw HistData ASCII M1 ZIP bytes for one pair/year."""

    page = _PAGE.format(pair_lower=pair.lower(), year=year)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", _UA)]
    with opener.open(page, timeout=timeout) as resp:
        html = resp.read().decode("utf-8", "replace")
    match = _TK_RE.search(html)
    if not match:
        raise RuntimeError("could not find HistData 'tk' token; page layout may have changed")
    token = match.group(1)
    form = urllib.parse.urlencode(
        {
            "tk": token,
            "date": str(year),
            "datemonth": str(year),
            "platform": "ASCII",
            "timeframe": "M1",
            "fxpair": pair.upper(),
        }
    ).encode()
    request = urllib.request.Request(_POST, data=form, headers={"Referer": page})
    with opener.open(request, timeout=timeout) as resp:
        payload = resp.read()
    if payload[:2] != b"PK":
        raise RuntimeError(f"HistData did not return a ZIP (got {payload[:64]!r})")
    return payload


def zip_to_frame(zip_bytes: bytes) -> pd.DataFrame:
    """Parse the M1 CSV from the ZIP into a UTC-indexed OHLC frame (EST->UTC)."""

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        raw = archive.read(name)
    frame = pd.read_csv(
        io.BytesIO(raw),
        sep=";",
        header=None,
        names=["ts", "open", "high", "low", "close", "volume"],
        dtype={"ts": str},
    )
    stamp = pd.to_datetime(frame["ts"], format="%Y%m%d %H%M%S")
    stamp = stamp.dt.tz_localize("US/Eastern", ambiguous="NaT", nonexistent="NaT")
    frame = frame.assign(timestamp=stamp).dropna(subset=["timestamp"])
    return frame.set_index("timestamp").sort_index()[["open", "high", "low", "close"]]


def resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample to ``rule`` with OHLC aggregation; drop market-closed empty bins.

    HistData M1 rows are stamped with the bar OPEN time, so aggregated bars
    must keep ``label="left", closed="left"``: the 10:00 hourly bar covers
    [10:00, 11:00) and is labelled 10:00. The original v1 exports used
    ``label="right"`` which shifted every label +1h — measured against
    Dukascopy/FXCM UTC hours (p50 mid diff 6.5 pips at lag 0 vs 1-2 pips at
    -1h, uniform across months) and fixed in v2.
    """

    if rule.lower() in {"m1", "1min", "1m", "none"}:
        out = frame.copy()
    else:
        out = (
            frame.resample(rule, label="left", closed="left")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
            .dropna(subset=["open", "high", "low", "close"])
        )
    out.index = out.index.tz_convert("UTC")
    return out


def to_pipeline_csv(frame: pd.DataFrame, path: str) -> int:
    """Write ``timestamp,open,high,low,close`` (UTC ISO-8601). Returns row count."""

    out = frame.reset_index()
    out["timestamp"] = (
        out["timestamp"]
        .dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        .str.replace(r"(\d{2})(\d{2})$", r"\1:\2", regex=True)
    )
    out[["timestamp", "open", "high", "low", "close"]].to_csv(path, index=False)
    return len(out)


def write_metadata(
    path: str,
    *,
    pair: str,
    year: int,
    rule: str,
    rows: int,
    zip_sha256: str,
) -> str:
    """Write the sidecar declaring what the CSV prices MEAN. v2 provenance.

    HistData ASCII quotes are BID prices (per the provider's FAQ) with no ask
    and no real volume — the basis is declared here instead of being silently
    assumed downstream. ``label_convention`` documents the v2 fix: bar labels
    are the bar OPEN time in UTC (v1 files were labelled +1h, see
    INC-20260714-M1 in reports/evidence/data-platform-maximization-20260714/).
    """

    import hashlib as _hashlib
    import json as _json

    meta_path = f"{path}.meta.json"
    payload = {
        "schema": "histdata_pipeline_csv_meta_v2",
        "pair": pair.upper(),
        "year": year,
        "resample_rule": rule,
        "rows": rows,
        "price_basis": "bid",
        "ask_available": False,
        "volume_available": False,
        "timezone_provenance": "provider US/Eastern (DST-aware, verified against "
        "Dukascopy UTC hours), converted to UTC",
        "label_convention": "bar OPEN time, UTC (v2; v1 was shifted +1h by a "
        "label='right' resample bug)",
        "source": "histdata.com free ASCII M1",
        "source_zip_sha256": zip_sha256,
        "csv_sha256": _hashlib.sha256(open(path, "rb").read()).hexdigest(),
        "research_only": True,
        "promotion_admissible": False,
    }
    with open(meta_path, "w", encoding="utf-8") as handle:
        handle.write(_json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return meta_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair", required=True, help="e.g. USDJPY, EURUSD, GBPUSD")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--out", required=True, help="output pipeline CSV path")
    parser.add_argument("--resample", default="1h", help="pandas offset (e.g. 1h, 4h, 1d, M1)")
    args = parser.parse_args(argv)

    import hashlib

    zip_bytes = download_zip(args.pair, args.year)
    frame = resample(zip_to_frame(zip_bytes), args.resample)
    rows = to_pipeline_csv(frame, args.out)
    meta_path = write_metadata(
        args.out,
        pair=args.pair,
        year=args.year,
        rule=args.resample,
        rows=rows,
        zip_sha256=hashlib.sha256(zip_bytes).hexdigest(),
    )
    lo, hi = frame["close"].min(), frame["close"].max()
    print(
        f"{args.pair} {args.year}: {rows} {args.resample} bars -> {args.out} (+{meta_path}) "
        f"(close {lo:.3f}..{hi:.3f}). BID-basis CLOSE-ONLY, research-only, "
        "not promotion-admissible."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
