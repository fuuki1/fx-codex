"""Bid/ask bars -> authoritative pipeline connection.

Covers the loader's declared price basis (bid OHLC + opening spread), the
manifest gate for ``measured_bar_spread_v1`` (bid/ask sources only), the
fail-closed behaviour when a measured cost is requested without a measurement,
and an end-to-end run whose per-trade costs come from per-bar measured spreads
instead of a declared constant. All payloads are synthetic FIXTURES.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from data_platform.collect.candles import CollectedCandle
from data_platform.materialize.candle_bars import bars_to_csv_bytes, materialize_candle_bars
from fx_backtester.data import load_bidask_bars_csv
from fx_backtester.experiment_manifest import parse_experiment_manifest
from fx_backtester.experiment_pipeline import GitState, _cost_r, run_experiment, trade_net_r
from fx_backtester.failures import TypedFailure
from test_experiment_pipeline import COMMIT, _manifest_dict

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)
START = datetime(2024, 1, 2, tzinfo=UTC)


def _write_bidask_bars(path: Path, *, rows: int = 700, seed: int = 7) -> str:
    """Synthetic hourly bid/ask candles -> canonical CandleBar CSV fixture."""

    rng = np.random.default_rng(seed)
    drift = np.sin(np.arange(rows) / 24.0) * 0.02
    noise = rng.normal(0.0, 0.05, size=rows)
    close = 145.0 + np.cumsum(drift + noise)
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0.0, 0.01, size=rows)) + 0.002
    spreads = 0.004 + np.abs(rng.normal(0.0, 0.002, size=rows))  # ~0.4-1 pip, varying
    candles: list[CollectedCandle] = []
    for index in range(rows):
        stamp = START + timedelta(hours=index)
        bid_high = max(open_[index], close[index]) + wick[index]
        bid_low = min(open_[index], close[index]) - wick[index]

        def side(name: str, shift: float) -> CollectedCandle:
            return CollectedCandle(
                provider="dukascopy",
                account_environment="datafeed",
                instrument="USDJPY",
                side=name,
                interval="1h",
                open_time=stamp,
                open=float(open_[index] + shift),
                high=float(bid_high + shift),
                low=float(bid_low + shift),
                close=float(close[index] + shift),
                volume=100.0,
                received_at=NOW,
                connection_id="fixture",
                writer_id="fixture",
                raw_payload_sha256="ab" * 32,
                source_endpoint_class="historical_datafeed",
                collection_mode="historical_download",
            )

        candles.append(side("bid", 0.0))
        candles.append(side("ask", float(spreads[index])))
    bars = materialize_candle_bars(candles, "1h").bars
    path.write_bytes(bars_to_csv_bytes(bars))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def bidask_setup(tmp_path: Path) -> dict[str, Any]:
    csv_path = tmp_path / "usdjpy_bidask_1h.csv"
    csv_sha = _write_bidask_bars(csv_path)
    (tmp_path / "requirements.lock").write_text("numpy==2.3.5\n", encoding="utf-8")
    return {"tmp_path": tmp_path, "csv_path": csv_path, "csv_sha": csv_sha}


def _bidask_manifest(setup: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload = _manifest_dict(setup["csv_path"], setup["csv_sha"], **overrides)
    payload["data"]["sources"][0]["source_id"] = "synthetic_bidask_bars"
    payload["data"]["sources"][0]["kind"] = "bidask_bars_csv"
    payload["costs"]["cost_model_version"] = "measured_bar_spread_v1"
    return payload


class TestLoader:
    def test_bid_basis_and_opening_spread(self, bidask_setup: dict[str, Any]) -> None:
        loaded = load_bidask_bars_csv(bidask_setup["csv_path"])
        frame = loaded["USDJPY"]
        assert {"open", "high", "low", "close", "spread_price", "spread_close"} <= set(
            frame.columns
        )
        assert (frame["spread_price"] > 0).all()
        # the declared basis must match the canonical columns exactly:
        # open/high/low/close are the BID side, spread_price is spread_open
        import pandas as pd

        raw = pd.read_csv(bidask_setup["csv_path"])
        assert frame["open"].tolist() == raw["bid_open"].tolist()
        assert frame["close"].tolist() == raw["bid_close"].tolist()
        assert frame["spread_price"].tolist() == raw["spread_open"].tolist()

    def test_wrong_schema_fails_closed(self, tmp_path: Path) -> None:
        bogus = tmp_path / "x.csv"
        bogus.write_text("timestamp,open,high,low,close\n2024-01-01T00:00:00+00:00,1,2,0,1\n")
        with pytest.raises(ValueError, match="canonical bid/ask bars"):
            load_bidask_bars_csv(bogus)

    def test_symbol_mismatch_fails_closed(self, bidask_setup: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="not the requested symbol"):
            load_bidask_bars_csv(bidask_setup["csv_path"], symbol="EURUSD")


class TestManifestGate:
    def test_measured_costs_reject_close_only_sources(self, bidask_setup: dict[str, Any]) -> None:
        payload = _manifest_dict(bidask_setup["csv_path"], bidask_setup["csv_sha"])
        payload["costs"]["cost_model_version"] = "measured_bar_spread_v1"
        with pytest.raises(TypedFailure, match="bidask_bars_csv"):
            parse_experiment_manifest(payload)

    def test_measured_costs_with_bidask_source_parse(self, bidask_setup: dict[str, Any]) -> None:
        manifest = parse_experiment_manifest(_bidask_manifest(bidask_setup))
        assert manifest.costs.cost_model_version == "measured_bar_spread_v1"


class TestMeasuredCost:
    def _manifest(self, setup: dict[str, Any]):
        return parse_experiment_manifest(_bidask_manifest(setup))

    def test_cost_uses_the_measured_entry_spread(self, bidask_setup: dict[str, Any]) -> None:
        manifest = self._manifest(bidask_setup)
        row = {
            "long_gross_r": 1.0,
            "long_bars_to_exit": 3,
            "volatility": 0.05,
            "entry_spread_price": 0.006,
        }
        wide = dict(row, entry_spread_price=0.012)
        narrow_net = trade_net_r(manifest, row, "long")
        wide_net = trade_net_r(manifest, wide, "long")
        risk_distance = 0.05 * manifest.labels.stop_vol_multiple
        assert narrow_net - wide_net == pytest.approx((0.012 - 0.006) / risk_distance)

    def test_measured_cost_without_measurement_fails_closed(
        self, bidask_setup: dict[str, Any]
    ) -> None:
        manifest = self._manifest(bidask_setup)
        with pytest.raises(TypedFailure, match="never zero-filled"):
            _cost_r(manifest, 0.05, 3, 1.0, measured_spread_price=None)


class TestEndToEnd:
    def _run(self, setup: dict[str, Any], payload: dict[str, Any], name: str):
        manifest_path = setup["tmp_path"] / f"{name}.json"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        return run_experiment(
            manifest_path,
            output_root=setup["tmp_path"] / name,
            repository_root=setup["tmp_path"],
            git_state=GitState(commit=COMMIT, dirty=False),
        )

    def test_pipeline_completes_on_bidask_bars_with_measured_costs(
        self, bidask_setup: dict[str, Any]
    ) -> None:
        result = self._run(bidask_setup, _bidask_manifest(bidask_setup), "out-a")
        # synthetic fixture data must still be denied promotion
        assert result.promotion_passed is False
        assert "non_synthetic_data" in result.promotion_failures
        rows = [
            json.loads(line)
            for line in (result.output_dir / "dataset_rows.jsonl").read_text("utf-8").splitlines()
            if line.strip()
        ]
        assert rows and all(float(r["entry_spread_price"]) > 0 for r in rows)

    def test_measured_run_is_reproducible(self, bidask_setup: dict[str, Any]) -> None:
        payload = _bidask_manifest(bidask_setup)
        first = self._run(bidask_setup, payload, "out-a")
        second = self._run(bidask_setup, payload, "out-b")
        assert first.deterministic_result_sha256 == second.deterministic_result_sha256
        assert first.manifest_sha256 == second.manifest_sha256


class TestDatasetIdentity:
    def test_spread_is_part_of_the_dataset_hash(self) -> None:
        import pandas as pd

        from fx_backtester.experiment_pipeline import _normalized_dataset_hash

        index = pd.date_range("2024-01-02", periods=3, freq="1h", tz="UTC")
        base = pd.DataFrame(
            {
                "open": [1.0, 1.1, 1.2],
                "high": [1.1, 1.2, 1.3],
                "low": [0.9, 1.0, 1.1],
                "close": [1.05, 1.15, 1.25],
                "spread_price": [0.004, 0.004, 0.004],
            },
            index=index,
        )
        wider = base.assign(spread_price=[0.008, 0.008, 0.008])
        assert _normalized_dataset_hash(base) != _normalized_dataset_hash(wider)
        # and a legacy close-only frame still hashes without a spread column
        legacy = base.drop(columns=["spread_price"])
        assert _normalized_dataset_hash(legacy) != _normalized_dataset_hash(base)
