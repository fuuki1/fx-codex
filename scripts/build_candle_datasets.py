#!/usr/bin/env python3
"""Build spread-aware bar datasets from the mirrored public candle files.

Pipeline (raw-first, fail-closed, deterministic):

    mirror file bytes
      -> sha256 verified against the mirror manifest (a mismatch aborts)
      -> content-addressed immutable raw store
      -> provider parser (padding excluded with counts, contract-validated)
      -> paired bid/ask bar materialization (candle_bars)
      -> canonical CSV(.gz) under --out-root + append-only dataset registry

Datasets produced per pair (USDJPY / EURUSD / GBPUSD):
    dukascopy h1 candles  -> 1h bid/ask bars over the mirrored year range
    dukascopy m1 candles  -> 5m and 1h bid/ask bars (minute-boundary spreads)
    fxcm H1 candles       -> 1h bid/ask bars (independent broker source)

Everything committed is REAL bid/ask market data from historical downloads;
provenance (provider, mode, raw hashes, fetch times) travels in the registry
and the stats JSON. Nothing here fabricates ticks from candles.

Usage:
    python3 scripts/build_candle_datasets.py \
        --mirror-root logs/data_platform/mirror \
        --raw-store logs/data_platform/raw_store \
        --out-root data/real \
        --stats-out logs/data_platform/candle_dataset_stats.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_platform.collect.candles import CollectedCandle, ParsedCandles  # noqa: E402
from data_platform.collect.dukascopy_candles import (  # noqa: E402
    h1_month_context,
    m1_day_context,
    parse_candle_payload,
)
from data_platform.collect.fxcm_candles import FxcmContext, parse_week_h1  # noqa: E402
from data_platform.lineage.dataset_registry import (  # noqa: E402
    DatasetManifest,
    DatasetRegistry,
    SourceLineageEntry,
)
from data_platform.materialize.candle_bars import (  # noqa: E402
    CandleBar,
    bars_sha256,
    bars_to_csv_bytes,
    candle_gap_audit,
    materialize_candle_bars,
)
from data_platform.raw.immutable_store import ImmutableRawStore  # noqa: E402

PAIRS = ("USDJPY", "EURUSD", "GBPUSD")

# Measured on the 2026-07-14 mirror: FXCM 2021 weekly files carry 2.3-6.3%
# crossed-boundary rows (up to 10 pips inverted) and 2022 up to 0.7%; 2023-2025
# have exactly zero. The dataset therefore uses only the clean years — the bad
# years are excluded by measurement, not silently repaired.
FXCM_YEARS = ("2023", "2024", "2025")


def _mirror_key(path: Path) -> tuple[str, ...]:
    """Mirror-location-independent identity: the subpath from the provider dir.

    Manifest rows record the path used at fetch time; a consumer may address
    the same mirror from a different working directory or via an absolute
    path, so both sides are keyed by the provider-rooted subpath instead.
    """

    parts = path.parts
    for index, part in enumerate(parts):
        if part in ("dukascopy", "fxcm"):
            return parts[index:]
    raise SystemExit(f"path does not look like a mirrored candle file: {path}")


def load_manifest(mirror_root: Path) -> dict[tuple[str, ...], dict[str, Any]]:
    """Latest manifest row per mirrored file (fetched/skipped only)."""

    entries: dict[tuple[str, ...], dict[str, Any]] = {}
    manifest_path = mirror_root / "manifest.jsonl"
    if not manifest_path.is_file():
        raise SystemExit(f"mirror manifest not found: {manifest_path}")
    for line in manifest_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") in ("fetched", "skipped") and row.get("sha256"):
            entries[_mirror_key(Path(str(row["path"])))] = row
    return entries


def verified_bytes(
    path: Path, manifest: dict[tuple[str, ...], dict[str, Any]], store: ImmutableRawStore
) -> tuple[bytes, dict[str, Any]]:
    """Read a mirrored file, verify it against the manifest, park it raw-first."""

    row = manifest.get(_mirror_key(path))
    if row is None:
        raise SystemExit(f"mirrored file has no manifest row (refusing to trust it): {path}")
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != row["sha256"]:
        raise SystemExit(
            f"mirror integrity failure: {path} hashes {digest[:12]}… but manifest says "
            f"{str(row['sha256'])[:12]}…"
        )
    store.put(payload)
    return payload, row


def _received_at(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(row["fetched_at"]))


def _merge_parsed(
    target: list[CollectedCandle],
    parsed: ParsedCandles,
    stats: dict[str, int],
    sources: list[SourceLineageEntry],
    *,
    raw_sha: str,
    cutoff: datetime,
) -> None:
    target.extend(parsed.candles)
    stats["candles"] += len(parsed.candles)
    stats["padding_excluded"] += parsed.padding_excluded
    stats["zero_width_excluded"] += parsed.zero_width_excluded
    stats["files"] += 1
    sources.append(
        SourceLineageEntry(
            source_id=f"raw:{raw_sha[:16]}",
            raw_sha256=raw_sha,
            available_at_cutoff=cutoff,
            record_count=len(parsed.candles),
        )
    )


def _dedupe_fxcm(candles: list[CollectedCandle]) -> tuple[list[CollectedCandle], int]:
    """FXCM weekly files can overlap at year boundaries. Identical duplicates
    are dropped (counted); a conflicting duplicate is a data fault -> abort."""

    seen: dict[tuple[str, datetime], CollectedCandle] = {}
    dropped = 0
    for candle in candles:
        key = (candle.side, candle.open_time)
        existing = seen.get(key)
        if existing is None:
            seen[key] = candle
            continue
        if (existing.open, existing.high, existing.low, existing.close) == (
            candle.open,
            candle.high,
            candle.low,
            candle.close,
        ):
            dropped += 1
            continue
        raise SystemExit(
            f"conflicting FXCM duplicate at {candle.open_time.isoformat()} {candle.side}: "
            "two files disagree on OHLC; refusing to pick one"
        )
    ordered = sorted(seen.values(), key=lambda c: (c.open_time, c.side))
    return ordered, dropped


def _emit_dataset(
    *,
    dataset_id: str,
    pair: str,
    bars: list[CandleBar],
    aggregates: dict[str, int],
    sources: list[SourceLineageEntry],
    out_path: Path,
    registry: DatasetRegistry,
    stats: dict[str, Any],
    interval: str,
) -> None:
    csv_gz = bars_to_csv_bytes(bars, compress=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(csv_gz)
    dataset_sha = bars_sha256(bars)
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        instrument=pair,
        as_of=max(source.available_at_cutoff for source in sources),
        sources=tuple(sources),
        dataset_sha256=dataset_sha,
        writer_id="scripts/build_candle_datasets.py",
    )
    lineage_sha = registry.register(manifest)
    audit = candle_gap_audit(bars, interval)
    stats[dataset_id] = {
        "pair": pair,
        "bars": len(bars),
        "interval": interval,
        "first_open": bars[0].open_time.isoformat() if bars else None,
        "last_open": bars[-1].open_time.isoformat() if bars else None,
        "dataset_sha256": dataset_sha,
        "lineage_sha256": lineage_sha,
        "csv_gz_bytes": len(csv_gz),
        "grid_expected_bars": audit.expected_bars,
        "grid_observed_bars": audit.observed_bars,
        "grid_completeness_incl_closed_market": round(audit.completeness, 6),
        "out_path": str(out_path),
        **aggregates,
    }
    print(f"[dataset] {dataset_id}: {len(bars)} bars sha={dataset_sha[:12]}…", flush=True)


def build_dukascopy_h1(
    pair: str,
    mirror_root: Path,
    manifest: dict[tuple[str, ...], dict[str, Any]],
    store: ImmutableRawStore,
    out_root: Path,
    registry: DatasetRegistry,
    stats: dict[str, Any],
) -> list[CandleBar]:
    root = mirror_root / "dukascopy" / "h1" / pair
    candles: list[CollectedCandle] = []
    counters: dict[str, int] = defaultdict(int)
    sources: list[SourceLineageEntry] = []
    for path in sorted(root.rglob("*.bi5")):
        payload, row = verified_bytes(path, manifest, store)
        year, month = int(path.parts[-3]), int(path.parts[-2])
        side = "bid" if path.name.startswith("BID") else "ask"
        context = h1_month_context(
            pair,
            year,
            month,
            side,
            received_at=_received_at(row),
            connection_id="bulk-h1",
        )
        parsed = parse_candle_payload(payload, context)
        _merge_parsed(
            candles, parsed, counters, sources, raw_sha=row["sha256"], cutoff=_received_at(row)
        )
    result = materialize_candle_bars(candles, "1h")
    span = f"{candles[0].open_time.year}-{candles[-1].open_time.year}"
    _emit_dataset(
        dataset_id=f"dukascopy_{pair.lower()}_1h_{span}_v1",
        pair=pair,
        bars=list(result.bars),
        aggregates={
            **counters,
            "unpaired_bid": result.unpaired_bid,
            "unpaired_ask": result.unpaired_ask,
            "crossed_excluded": result.crossed_excluded,
        },
        sources=sources,
        out_path=out_root / "dukascopy" / f"{pair.lower()}_{span}_1h_bidask.csv.gz",
        registry=registry,
        stats=stats,
        interval="1h",
    )
    return list(result.bars)


def build_dukascopy_m1(
    pair: str,
    mirror_root: Path,
    manifest: dict[tuple[str, ...], dict[str, Any]],
    store: ImmutableRawStore,
    out_root: Path,
    registry: DatasetRegistry,
    stats: dict[str, Any],
) -> dict[str, list[CandleBar]]:
    root = mirror_root / "dukascopy" / "m1" / pair
    counters: dict[str, int] = defaultdict(int)
    sources: list[SourceLineageEntry] = []
    bars_by_interval: dict[str, list[CandleBar]] = {"5m": [], "1h": []}
    totals = {"unpaired_bid": 0, "unpaired_ask": 0, "crossed_excluded": 0}
    for month_dir in sorted(root.glob("*/*")):  # {YYYY}/{MM}, month batches bound memory
        month_candles: list[CollectedCandle] = []
        for path in sorted(month_dir.rglob("*.bi5")):
            payload, row = verified_bytes(path, manifest, store)
            day = date(int(path.parts[-4]), int(path.parts[-3]), int(path.parts[-2]))
            side = "bid" if path.name.startswith("BID") else "ask"
            context = m1_day_context(
                pair,
                day,
                side,
                received_at=_received_at(row),
                connection_id="bulk-m1",
            )
            parsed = parse_candle_payload(payload, context)
            _merge_parsed(
                month_candles,
                parsed,
                counters,
                sources,
                raw_sha=row["sha256"],
                cutoff=_received_at(row),
            )
        if not month_candles:
            continue
        for interval in ("5m", "1h"):
            result = materialize_candle_bars(month_candles, interval)
            bars_by_interval[interval].extend(result.bars)
            if interval == "5m":  # count pairing faults once, not per interval
                totals["unpaired_bid"] += result.unpaired_bid
                totals["unpaired_ask"] += result.unpaired_ask
                totals["crossed_excluded"] += result.crossed_excluded
    year = bars_by_interval["5m"][0].open_time.year
    for interval, suffix in (("5m", "5m_bidask"), ("1h", "1h_bidask_from_m1")):
        _emit_dataset(
            dataset_id=f"dukascopy_{pair.lower()}_{interval}_from_m1_{year}_v1",
            pair=pair,
            bars=bars_by_interval[interval],
            aggregates={**counters, **totals},
            sources=sources,
            out_path=out_root / "dukascopy" / f"{pair.lower()}_{year}_{suffix}.csv.gz",
            registry=registry,
            stats=stats,
            interval=interval,
        )
    return bars_by_interval


def build_fxcm_h1(
    pair: str,
    mirror_root: Path,
    manifest: dict[tuple[str, ...], dict[str, Any]],
    store: ImmutableRawStore,
    out_root: Path,
    registry: DatasetRegistry,
    stats: dict[str, Any],
) -> list[CandleBar]:
    root = mirror_root / "fxcm" / "h1" / pair
    candles: list[CollectedCandle] = []
    counters: dict[str, int] = defaultdict(int)
    sources: list[SourceLineageEntry] = []
    paths = [path for path in sorted(root.rglob("*.csv.gz")) if path.parts[-2] in FXCM_YEARS]
    for path in paths:
        payload, row = verified_bytes(path, manifest, store)
        context = FxcmContext(
            instrument=pair,
            received_at=_received_at(row),
            connection_id="bulk-fxcm",
        )
        parsed = parse_week_h1(payload, context)
        _merge_parsed(
            candles, parsed, counters, sources, raw_sha=row["sha256"], cutoff=_received_at(row)
        )
    deduped, dropped = _dedupe_fxcm(candles)
    counters["overlap_duplicates_dropped"] = dropped
    result = materialize_candle_bars(deduped, "1h")
    span = f"{deduped[0].open_time.year}-{deduped[-1].open_time.year}"
    _emit_dataset(
        dataset_id=f"fxcm_{pair.lower()}_1h_{span}_v1",
        pair=pair,
        bars=list(result.bars),
        aggregates={
            **counters,
            "unpaired_bid": result.unpaired_bid,
            "unpaired_ask": result.unpaired_ask,
            "crossed_excluded": result.crossed_excluded,
        },
        sources=sources,
        out_path=out_root / "fxcm" / f"{pair.lower()}_{span}_1h_bidask.csv.gz",
        registry=registry,
        stats=stats,
        interval="1h",
    )
    return list(result.bars)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror-root", type=Path, default=Path("logs/data_platform/mirror"))
    parser.add_argument("--raw-store", type=Path, default=Path("logs/data_platform/raw_store"))
    parser.add_argument("--out-root", type=Path, default=Path("data/real"))
    parser.add_argument("--stats-out", type=Path, required=True)
    parser.add_argument("--pairs", nargs="+", default=list(PAIRS))
    args = parser.parse_args(argv)

    manifest = load_manifest(args.mirror_root)
    store = ImmutableRawStore(args.raw_store)
    stats: dict[str, Any] = {}
    for provider in ("dukascopy", "fxcm"):
        registry_path = args.out_root / provider / "dataset_registry.jsonl"
        registry = DatasetRegistry(registry_path)
        for pair in args.pairs:
            if provider == "dukascopy":
                build_dukascopy_h1(
                    pair, args.mirror_root, manifest, store, args.out_root, registry, stats
                )
                build_dukascopy_m1(
                    pair, args.mirror_root, manifest, store, args.out_root, registry, stats
                )
            else:
                build_fxcm_h1(
                    pair, args.mirror_root, manifest, store, args.out_root, registry, stats
                )
    args.stats_out.parent.mkdir(parents=True, exist_ok=True)
    args.stats_out.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n")
    print(f"stats written: {args.stats_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
