"""TradingView単一取得cacheの鮮度・完全性・PIT回帰テスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json
import multiprocessing
from pathlib import Path
import plistlib
import time
from typing import Any

import fx_tf_snapshot
import pytest
from fx_intel import technical_cache, technicals
from fx_intel.technicals import IntervalView, PairTechnicals

ROOT = Path(__file__).resolve().parents[1]


def _technical_map(
    symbols: tuple[str, ...],
    intervals: tuple[str, ...],
    *,
    observed_at: datetime | None = None,
) -> dict[str, PairTechnicals]:
    return {
        symbol: PairTechnicals(
            symbol=symbol,
            views={
                interval: IntervalView(
                    interval=interval,
                    recommendation="BUY",
                    buy=7,
                    sell=2,
                    neutral=3,
                    close=150.0 if symbol == "USDJPY" else 1.1,
                    open=149.9 if symbol == "USDJPY" else 1.09,
                    high=150.2 if symbol == "USDJPY" else 1.11,
                    low=149.8 if symbol == "USDJPY" else 1.08,
                    atr=0.2 if symbol == "USDJPY" else 0.002,
                    sma_fast=150.0 if symbol == "USDJPY" else 1.1,
                    sma_slow=149.0 if symbol == "USDJPY" else 1.0,
                )
                for interval in intervals
            },
            observed_at=observed_at,
        )
        for symbol in symbols
    }


class FakeFetcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        symbols,
        *,
        intervals,
        fast_window,
        slow_window,
        exchange,
        screener,
    ):
        normalized_symbols = tuple(symbols)
        normalized_intervals = tuple(intervals)
        self.calls.append(
            {
                "symbols": normalized_symbols,
                "intervals": normalized_intervals,
                "fast_window": fast_window,
                "slow_window": slow_window,
                "exchange": exchange,
                "screener": screener,
            }
        )
        return _technical_map(normalized_symbols, normalized_intervals), []


def _concurrent_refresh_worker(cache_path: str, calls_path: str) -> None:
    def fetcher(
        symbols,
        *,
        intervals,
        fast_window,
        slow_window,
        exchange,
        screener,
    ):
        del fast_window, slow_window, exchange, screener
        with Path(calls_path).open("a", encoding="utf-8") as handle:
            handle.write(f"{multiprocessing.current_process().pid}\n")
            handle.flush()
        time.sleep(0.2)
        return _technical_map(tuple(symbols), tuple(intervals)), []

    technical_cache.refresh_pair_technicals(
        ["USDJPY", "EURUSD", "GBPUSD"],
        cache_path=cache_path,
        fetcher=fetcher,
    )


def test_snapshot_producer_fetches_shared_universe_once_and_cache_is_private(tmp_path) -> None:
    cache = tmp_path / "tradingview.json"
    fetched_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    fetcher = FakeFetcher()

    first, warnings = technical_cache.refresh_pair_technicals(
        ["USDJPY", "EURUSD"],
        cache_path=cache,
        now=fetched_at,
        fetcher=fetcher,
    )
    second, _ = technical_cache.refresh_pair_technicals(
        ["GBPUSD"],
        cache_path=cache,
        now=fetched_at + timedelta(seconds=120),
        fetcher=fetcher,
    )

    assert warnings == []
    assert len(fetcher.calls) == 1
    assert fetcher.calls[0]["symbols"] == technical_cache.DEFAULT_SHARED_SYMBOLS
    assert set(first) == {"USDJPY", "EURUSD"}
    assert set(second) == {"GBPUSD"}
    assert first["USDJPY"].observed_at == fetched_at
    assert second["GBPUSD"].observed_at == fetched_at
    assert first["USDJPY"].source_snapshot_hash
    assert cache.stat().st_mode & 0o777 == 0o600
    assert cache.with_name(f".{cache.name}.lock").stat().st_mode & 0o777 == 0o600

    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["source_event_time"] is None
    assert payload["available_time"] == fetched_at.isoformat()
    assert payload["ingested_time"] == fetched_at.isoformat()
    assert payload["capture_slot"] == fetched_at.isoformat()


def test_producer_replaces_partial_cache_with_full_shared_contract(tmp_path) -> None:
    cache = tmp_path / "tradingview.json"
    fetched_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    partial_fetcher = FakeFetcher()
    full_fetcher = FakeFetcher()

    technical_cache.refresh_pair_technicals(
        ["USDJPY"],
        cache_path=cache,
        shared_symbols=("USDJPY",),
        now=fetched_at,
        fetcher=partial_fetcher,
    )
    technical_cache.refresh_pair_technicals(
        ["USDJPY"],
        cache_path=cache,
        now=fetched_at + timedelta(seconds=60),
        fetcher=full_fetcher,
    )

    assert len(partial_fetcher.calls) == 1
    assert len(full_fetcher.calls) == 1
    assert full_fetcher.calls[0]["symbols"] == technical_cache.DEFAULT_SHARED_SYMBOLS
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["symbols"] == list(technical_cache.DEFAULT_SHARED_SYMBOLS)


def test_concurrent_processes_share_one_network_fetch(tmp_path) -> None:
    cache = tmp_path / "tradingview.json"
    calls = tmp_path / "fetch_calls.txt"
    context = multiprocessing.get_context("spawn")
    workers = [
        context.Process(
            target=_concurrent_refresh_worker,
            args=(str(cache), str(calls)),
        )
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(worker.exitcode == 0 for worker in workers)
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1


def test_read_only_consumer_uses_cache_and_preserves_acquisition_time(tmp_path) -> None:
    cache = tmp_path / "tradingview.json"
    fetched_at = datetime(2026, 7, 26, 12, 2, tzinfo=UTC)
    technical_cache.refresh_pair_technicals(
        ["USDJPY"],
        cache_path=cache,
        now=fetched_at,
        fetcher=FakeFetcher(),
    )

    result, warnings = technical_cache.load_pair_technicals(
        ["USDJPY"],
        cache_path=cache,
        now=fetched_at + timedelta(seconds=300),
        max_age_seconds=360,
    )

    assert warnings == []
    assert result["USDJPY"].coverage() == 1.0
    assert result["USDJPY"].observed_at == fetched_at


def test_naive_technical_observation_is_rejected() -> None:
    technical_map = _technical_map(
        ("USDJPY",),
        technicals.DEFAULT_INTERVALS,
        observed_at=datetime(2026, 7, 26, 12, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        technicals.latest_observed_at(technical_map)


def test_stale_cache_fails_closed_without_network_fallback(tmp_path) -> None:
    cache = tmp_path / "tradingview.json"
    fetched_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    technical_cache.refresh_pair_technicals(
        ["USDJPY"],
        cache_path=cache,
        now=fetched_at,
        fetcher=FakeFetcher(),
    )

    result, warnings = technical_cache.load_pair_technicals(
        ["USDJPY"],
        cache_path=cache,
        now=fetched_at + timedelta(seconds=361),
        max_age_seconds=360,
    )

    assert result["USDJPY"].views == {}
    assert result["USDJPY"].observed_at is None
    assert result["USDJPY"].transient_failures == list(technicals.DEFAULT_INTERVALS)
    assert any("stale" in warning for warning in warnings)


def test_hash_tampering_fails_closed_and_producer_can_repair(tmp_path) -> None:
    cache = tmp_path / "tradingview.json"
    fetched_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    technical_cache.refresh_pair_technicals(
        ["USDJPY"],
        cache_path=cache,
        now=fetched_at,
        fetcher=FakeFetcher(),
    )
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["pairs"]["USDJPY"]["views"]["1h"]["close"] = 999.0
    cache.write_text(json.dumps(payload), encoding="utf-8")

    unavailable, warnings = technical_cache.load_pair_technicals(
        ["USDJPY"],
        cache_path=cache,
        now=fetched_at + timedelta(seconds=10),
    )
    assert unavailable["USDJPY"].views == {}
    assert any("hash mismatch" in warning for warning in warnings)

    repair_fetcher = FakeFetcher()
    repaired, repair_warnings = technical_cache.refresh_pair_technicals(
        ["USDJPY"],
        cache_path=cache,
        now=fetched_at + timedelta(seconds=20),
        fetcher=repair_fetcher,
    )
    assert len(repair_fetcher.calls) == 1
    assert repaired["USDJPY"].coverage() == 1.0
    assert any("再生成" in warning for warning in repair_warnings)


def test_snapshot_rows_keep_cache_available_time_instead_of_restamping(
    tmp_path, monkeypatch
) -> None:
    cache_time = datetime(2026, 7, 26, 12, 2, 30, tzinfo=UTC)
    output = tmp_path / "prices.jsonl"
    technical_map = _technical_map(
        ("USDJPY",),
        technicals.DEFAULT_INTERVALS,
        observed_at=cache_time,
    )
    monkeypatch.setattr(fx_tf_snapshot, "DEFAULT_TF_PRICES_PATH", output)
    monkeypatch.setattr(
        technical_cache,
        "refresh_pair_technicals",
        lambda *_args, **_kwargs: (technical_map, []),
    )

    result = fx_tf_snapshot.main(
        [
            "--symbols",
            "USDJPY",
            "--refresh-technical-cache",
            "--technical-cache",
            str(tmp_path / "cache.json"),
        ]
    )

    assert result == 0
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(technicals.DEFAULT_INTERVALS)
    assert {row["available_time"] for row in rows} == {cache_time.isoformat()}


def test_launchd_topology_has_one_network_producer_and_cache_only_consumers() -> None:
    snapshot_template = (ROOT / "ops" / "launchd" / "com.fx-codex.snapshot.plist.tmpl").read_text(
        encoding="utf-8"
    )
    rendered = snapshot_template.replace("__FX_ROOT__", "/srv/fx-codex").replace(
        "__PYTHON__", "/srv/fx-codex/.venv/bin/python"
    )
    arguments = plistlib.loads(rendered.encode())["ProgramArguments"]

    assert "--refresh-technical-cache" in arguments
    assert "--technical-cache-only" in (ROOT / "scripts" / "fx_briefing_once.sh").read_text(
        encoding="utf-8"
    )
    assert "--technical-cache-only" in (ROOT / "scripts" / "fx_horizon_once.sh").read_text(
        encoding="utf-8"
    )
    assert "--stage-timing-log" in (ROOT / "scripts" / "fx_horizon_once.sh").read_text(
        encoding="utf-8"
    )
