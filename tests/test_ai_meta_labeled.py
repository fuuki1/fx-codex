"""ai_meta_labeled 戦略のテスト(トリプルバリア×メタラベリング×分数次差分の合成)。

一次(MAクロス方向)→トリプルバリアでメタラベル→二次ロジスティックが張る/見送る、
というパイプライン全体を、合成価格系列で検証する。特にリーク防止(未来の変更が
過去のシグナルを変えない)を回帰テストとして固定する。
"""

from __future__ import annotations

import pandas as pd
import pytest

from fx_backtester.cli import main
from fx_backtester.strategies import AIMetaLabeledStrategy


def _price_frame(periods: int = 260) -> pd.DataFrame:
    """レジームが交互に切り替わるトレンド+振動の合成OHLC(spread_price付き)。"""
    index = pd.date_range("2024-01-01", periods=periods, freq="h")
    rows = []
    previous_close = 1.10
    for position in range(periods):
        regime = 0.0012 if (position // 20) % 2 == 0 else -0.0010
        oscillation = ((position % 6) - 2.5) * 0.00010
        close = previous_close * (1 + regime + oscillation)
        open_ = previous_close
        high = max(open_, close) + 0.0012
        low = min(open_, close) - 0.0012
        rows.append(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "spread_price": 0.00004,
            }
        )
        previous_close = close
    return pd.DataFrame(rows, index=index)


def _fast_strategy(**overrides) -> AIMetaLabeledStrategy:
    params = dict(
        fast_window=6,
        slow_window=18,
        frac_diff_d=0.4,
        frac_diff_threshold=1e-3,  # 窓を短く保ち、短い合成系列でも学習が回る
        vertical_bars=6,
        volatility_window=8,
        rsi_window=6,
        atr_window=6,
        min_train_bars=30,
        retrain_interval=8,
        epochs=25,
        meta_threshold=0.55,
    )
    params.update(overrides)
    return AIMetaLabeledStrategy(**params)


def test_strategy_generates_meta_diagnostics_and_valid_targets() -> None:
    data = _price_frame()
    signals = _fast_strategy().generate("EURUSD", data)

    assert {"primary_side", "meta_probability", "meta_model_ready", "meta_train_rows"}.issubset(
        signals.columns
    )
    assert bool(signals["meta_model_ready"].any())
    # 出力ポジションは -1/0/1 のみ
    assert set(signals["target_position"].unique()).issubset({-1, 0, 1})
    # 常に stop_distance > 0(基底クラスのフォールバック含む)
    assert (signals["stop_distance"] > 0).all()


def test_entry_only_when_meta_gate_passes() -> None:
    """エントリは『一次方向が出ていて、かつ二次が張るべきと言った』点のみ。"""
    data = _price_frame()
    signals = _fast_strategy().generate("EURUSD", data)
    entered = signals["target_position"] != 0
    # エントリ点では必ず一次方向と符号一致し、メタ確率が閾値以上
    for ts in signals.index[entered]:
        side = signals.loc[ts, "primary_side"]
        assert signals.loc[ts, "target_position"] == side
        assert signals.loc[ts, "meta_probability"] >= 0.55


def test_no_lookahead_future_change_does_not_alter_past_signals() -> None:
    data = _price_frame(220)
    modified = data.copy()
    cut = 160
    for col in ("open", "high", "low", "close"):
        modified.iloc[cut:, modified.columns.get_loc(col)] *= 1.2

    strategy = _fast_strategy()
    original = strategy.generate("EURUSD", data)
    changed = strategy.generate("EURUSD", modified)

    pd.testing.assert_series_equal(
        original["target_position"].iloc[:cut],
        changed["target_position"].iloc[:cut],
    )


def test_invalid_params_raise() -> None:
    with pytest.raises(ValueError):
        AIMetaLabeledStrategy(frac_diff_d=1.5).generate("EURUSD", _price_frame(60))
    with pytest.raises(ValueError):
        AIMetaLabeledStrategy(meta_threshold=0.3).generate("EURUSD", _price_frame(60))
    with pytest.raises(ValueError):
        AIMetaLabeledStrategy(fast_window=50, slow_window=20).generate(
            "EURUSD", _price_frame(60)
        )


def test_strategy_runs_from_cli(tmp_path) -> None:
    metrics_path = tmp_path / "meta_metrics.json"
    exit_code = main(
        [
            "backtest",
            "--data",
            "examples/sample_prices.csv",
            "--strategy",
            "ai_meta_labeled",
            "--param",
            "min_train_bars=30",
            "--param",
            "epochs=20",
            "--param",
            "fast_window=6",
            "--param",
            "slow_window=18",
            "--param",
            "vertical_bars=6",
            "--param",
            "frac_diff_threshold=0.001",
            "--output-metrics",
            str(metrics_path),
        ]
    )
    assert exit_code == 0
    assert metrics_path.exists()
