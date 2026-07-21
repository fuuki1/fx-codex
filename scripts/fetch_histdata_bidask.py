#!/usr/bin/env python3
"""HistDataのbid M1とask tickを監査可能な5分bid/ask OHLCへ統合する。

raw ZIPを保存し、同じ入力から再生成できるようSHA-256 manifestを残す。
出力は historical chart shadow 専用で、broker約定やlive昇格証拠ではない。
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, UTC
import hashlib
import http.cookiejar
import io
import json
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request
import zipfile

import pandas as pd

from scripts import fetch_histdata

BASE = "https://www.histdata.com"
ASK_PAGE = (
    BASE
    + "/download-free-forex-historical-data/"
    + "?/ninjatrader/tick-ask-quotes/{pair_lower}/{year}/{month}"
)
POST = BASE + "/get.php"
UA = "Mozilla/5.0 (research; fx-codex historical bid-ask fetch)"
FIELDS = ("tk", "date", "datemonth", "platform", "timeframe", "fxpair")
DEFAULT_PAIRS = ("EURUSD", "GBPUSD", "USDJPY")
DEFAULT_YEARS = tuple(range(2020, 2026))


@dataclass(frozen=True)
class RawArtifact:
    path: str
    url: str
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "url": self.url,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


def download_form_zip(page_url: str, *, timeout: float = 180.0, retries: int = 3) -> bytes:
    """HistDataのtoken+cookieフォームを使いZIPを取得する。"""

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            opener.addheaders = [("User-Agent", UA)]
            with opener.open(page_url, timeout=timeout) as response:
                html = response.read().decode("utf-8", "replace")
            form: dict[str, str] = {}
            for name in FIELDS:
                match = re.search(
                    rf'name=["\']{name}["\'][^>]*value=["\']([^"\']+)["\']',
                    html,
                    re.IGNORECASE,
                )
                if not match:
                    raise RuntimeError(f"HistData form field missing: {name}")
                form[name] = match.group(1)
            request = urllib.request.Request(
                POST,
                data=urllib.parse.urlencode(form).encode(),
                headers={"Referer": page_url, "User-Agent": UA},
            )
            with opener.open(request, timeout=timeout) as response:
                payload = response.read()
            if payload[:2] != b"PK":
                raise RuntimeError(f"HistData did not return ZIP: {payload[:64]!r}")
            return payload
        except Exception as error:  # noqa: BLE001 - network boundary
            last_error = error
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def ask_tick_zip_to_frame(payload: bytes) -> pd.DataFrame:
    """NinjaTrader ask tick ZIPをUS/Eastern時刻のprice系列へ読む。"""

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
        raw = archive.read(name)
    frame = pd.read_csv(
        io.BytesIO(raw),
        sep=";",
        header=None,
        names=["ts", "price", "volume"],
        usecols=[0, 1],
        dtype={"ts": str, "price": float},
    )
    stamp = pd.to_datetime(frame["ts"], format="%Y%m%d %H%M%S")
    stamp = stamp.dt.tz_localize("US/Eastern", ambiguous="NaT", nonexistent="NaT")
    return frame.assign(timestamp=stamp).dropna(subset=["timestamp"]).set_index("timestamp")


def price_to_ohlc(frame: pd.DataFrame, *, price_column: str, rule: str = "5min") -> pd.DataFrame:
    """実時刻を左閉右表示の完了足へ集約する。"""

    out = frame[price_column].resample(rule, closed="left", label="right").ohlc().dropna()
    out.index = out.index.tz_convert("UTC")
    return out


def bid_m1_to_ohlc(payload: bytes, *, rule: str = "5min") -> pd.DataFrame:
    frame = fetch_histdata.zip_to_frame(payload)
    out = (
        frame.resample(rule, closed="left", label="right")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
    )
    out.index = out.index.tz_convert("UTC")
    return out


def merge_bid_ask(pair: str, bid: pd.DataFrame, ask: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    joined = bid.add_prefix("bid_").join(ask.add_prefix("ask_"), how="inner")
    valid = pd.Series(True, index=joined.index)
    for field in ("open", "high", "low", "close"):
        valid &= joined[f"bid_{field}"] <= joined[f"ask_{field}"]
    invalid = int((~valid).sum())
    joined = joined.loc[valid].copy()
    for field in ("open", "high", "low", "close"):
        joined[field] = (joined[f"bid_{field}"] + joined[f"ask_{field}"]) / 2.0
    joined["spread"] = joined["ask_close"] - joined["bid_close"]
    joined.insert(0, "symbol", pair)
    joined.insert(0, "timestamp", joined.index.strftime("%Y-%m-%dT%H:%M:%S%z"))
    joined["timestamp"] = joined["timestamp"].str.replace(
        r"(\d{2})(\d{2})$", r"\1:\2", regex=True
    )
    joined["source"] = "histdata_bid_m1_ask_tick"
    joined["is_backfill"] = True
    joined["promotion_admissible"] = False
    return joined.reset_index(drop=True), invalid


def _cached_payload(path: Path, url: str, *, loader) -> tuple[bytes, RawArtifact]:
    if path.exists():
        payload = path.read_bytes()
    else:
        payload = loader()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if payload[:2] != b"PK":
        raise RuntimeError(f"invalid cached ZIP: {path}")
    return payload, RawArtifact(str(path), url, hashlib.sha256(payload).hexdigest(), len(payload))


def fetch_pair_year(pair: str, year: int, root: Path) -> dict[str, object]:
    pair = pair.upper().replace("/", "")
    raw_dir = root / "raw" / pair / str(year)
    out_dir = root / "bars_m5" / pair
    manifest_dir = root / "manifests" / pair
    bid_url = fetch_histdata._PAGE.format(pair_lower=pair.lower(), year=year)
    bid_payload, bid_artifact = _cached_payload(
        raw_dir / f"{pair}_{year}_bid_m1.zip",
        bid_url,
        loader=lambda: fetch_histdata.download_zip(pair, year),
    )
    bid = bid_m1_to_ohlc(bid_payload)
    ask_frames: list[pd.DataFrame] = []
    raw_artifacts = [bid_artifact]
    for month in range(1, 13):
        url = ASK_PAGE.format(pair_lower=pair.lower(), year=year, month=month)
        payload, artifact = _cached_payload(
            raw_dir / f"{pair}_{year}{month:02d}_ask_tick.zip",
            url,
            loader=lambda url=url: download_form_zip(url),
        )
        raw_artifacts.append(artifact)
        ask_frames.append(price_to_ohlc(ask_tick_zip_to_frame(payload), price_column="price"))
    ask = pd.concat(ask_frames).sort_index()
    ask = ask[~ask.index.duplicated(keep="last")]
    merged, invalid_rows = merge_bid_ask(pair, bid, ask)
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / f"{pair}_{year}_m5_bidask.csv.gz"
    merged.to_csv(output, index=False, compression="gzip")
    manifest = {
        "schema_version": 1,
        "pair": pair,
        "year": year,
        "generated_at": datetime.now(UTC).isoformat(),
        "output": str(output),
        "rows": len(merged),
        "first_timestamp": merged["timestamp"].iloc[0] if len(merged) else None,
        "last_timestamp": merged["timestamp"].iloc[-1] if len(merged) else None,
        "invalid_crossed_rows_dropped": invalid_rows,
        "source_contract": "histdata-bid-m1-plus-ask-tick-v1",
        "raw_artifacts": [artifact.to_dict() for artifact in raw_artifacts],
        "historical_only": True,
        "promotion_admissible": False,
        "reason": "historical quotes are not IBKR execution or live forward evidence",
    }
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{year}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", nargs="+", default=list(DEFAULT_PAIRS))
    parser.add_argument("--years", nargs="+", type=int, default=list(DEFAULT_YEARS))
    parser.add_argument("--root", type=Path, default=Path("data/historical_training/histdata"))
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args(argv)
    tasks = [(pair, year) for pair in args.pairs for year in args.years]
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(fetch_pair_year, pair, year, args.root): (pair, year)
            for pair, year in tasks
        }
        for future in as_completed(futures):
            pair, year = futures[future]
            result = future.result()
            results.append(result)
            print(f"{pair} {year}: {result['rows']} M5 bid/ask rows")
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "pairs": sorted({str(result["pair"]) for result in results}),
        "years": sorted({int(result["year"]) for result in results}),
        "rows": sum(int(result["rows"]) for result in results),
        "datasets": sorted(results, key=lambda row: (str(row["pair"]), int(row["year"]))),
        "promotion_admissible": False,
    }
    args.root.mkdir(parents=True, exist_ok=True)
    (args.root / "manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"manifest: {args.root / 'manifest.json'} ({summary['rows']} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
