"""Phase 6: パイプライン統合(offline)の検証。

合成の価格CSV + COT CSV を temp の data/ に書き、dcm run --offline 相当の
run_pipeline がエラー無く最終レポートを返すことを end-to-end で確かめる。
ネットワークには触れない。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from dukascopy_cftc_model.config import PipelineConfig
from dukascopy_cftc_model.pipeline import run_pipeline


def _write_synthetic_data(data_dir: Path, n_bars: int = 6000) -> None:
    """学習可能な弱いエッジを埋め込んだ価格 + COT を data/ に書く。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(11)
    idx = pd.date_range("2022-06-01", periods=n_bars, freq="1h", tz="UTC")

    # COTを先に作る(価格に弱く連動させてエッジを持たせる)
    weeks = n_bars // (24 * 7) + 60
    cot_dates = pd.date_range("2022-01-04", periods=weeks, freq="7D")

    def make_cot(seed: int) -> pd.DataFrame:
        r = np.random.default_rng(seed)
        nl = r.integers(100_000, 260_000, weeks)
        ns = r.integers(100_000, 260_000, weeks)
        return pd.DataFrame(
            {
                "report_date": cot_dates,
                "noncomm_long": nl,
                "noncomm_short": ns,
                "comm_long": r.integers(300_000, 500_000, weeks),
                "comm_short": r.integers(300_000, 500_000, weeks),
                "open_interest": r.integers(600_000, 900_000, weeks),
                "net_noncomm": nl - ns,
            }
        )

    eur = make_cot(1)
    usd = make_cot(2)
    eur.to_csv(data_dir / "COT_EUR.csv", index=False)
    usd.to_csv(data_dir / "COT_USD.csv", index=False)

    # 価格: ランダムウォーク + 弱いモメンタム(過去リターンが将来を少し予測)
    steps = rng.normal(0, 5e-4, n_bars)
    for i in range(2, n_bars):
        steps[i] += 0.05 * steps[i - 1]  # 弱い自己相関 = 学習可能なエッジ
    close = 1.10 + np.cumsum(steps)
    prices = pd.DataFrame(
        {
            "symbol": "EURUSD",
            "open": close,
            "high": close + 3e-4,
            "low": close - 3e-4,
            "close": close,
            "volume": rng.integers(50, 500, n_bars).astype(float),
        },
        index=pd.DatetimeIndex(idx, name="timestamp"),
    )
    prices.to_csv(data_dir / "EURUSD_H1.csv")


def _args(offline: bool, horizon: int, out: str) -> argparse.Namespace:
    return argparse.Namespace(offline=offline, horizon=horizon, out=out)


def test_run_pipeline_offline_end_to_end(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_synthetic_data(data_dir)

    from dataclasses import replace

    cfg = PipelineConfig().with_symbol("EURUSD")
    cfg = replace(cfg, data=replace(cfg.data, data_dir=data_dir))
    cfg = cfg.with_walk_forward(train_bars=2000, test_bars=500, purge_bars=24, embargo_bars=24)

    out = tmp_path / "report.json"
    report = run_pipeline(cfg, args=_args(offline=True, horizon=24, out=str(out)))

    # 最終出力の全項目が揃っている
    m = report.metrics
    for key in ("expectancy_usd", "win_rate", "max_drawdown_pct", "profit_factor", "sharpe_ratio"):
        assert key in m
    assert report.feature_importance  # 特徴量寄与
    assert report.fold_summaries  # 複数fold
    assert out.exists()  # JSONが書かれた

    # レポートJSONが妥当
    import json

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["symbol"] == "EURUSD"
    assert payload["horizon"] == 24
    assert "metrics" in payload and "feature_importance" in payload


def test_run_pipeline_rejects_insufficient_samples(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_synthetic_data(data_dir, n_bars=500)  # walk-forwardに不足

    from dataclasses import replace

    import pytest

    cfg = PipelineConfig().with_symbol("EURUSD")
    cfg = replace(cfg, data=replace(cfg.data, data_dir=data_dir))
    with pytest.raises(ValueError, match="不足"):
        run_pipeline(cfg, args=_args(offline=True, horizon=24, out=str(tmp_path / "r.json")))
