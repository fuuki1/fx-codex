"""パイプライン全段の設定データクラス。

すべて frozen dataclass。既定値だけで意味のあるパイプラインが1本通るように
選んである(EURUSD・H1・24本先リターン・walk-forward)。CLI/pipeline から
上書きする。ネットワークやファイルには一切触れない純粋な設定オブジェクト。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# Ridgeの正則化強度の既定探索グリッド(walk-forward内の時系列CVで選択)。
# 対数的に広く取り、0(=OLS)からかなり強い正則化までカバーする。
DEFAULT_ALPHA_GRID: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0, 1000.0)

# COT(週次先物ポジション)の発表ラグ。火曜集計 → 金曜発表なので、
# 価格の各バーには「そのバー時刻より前に確実に公開済みの週」だけを結合する。
COT_PUBLICATION_LAG_DAYS: int = 3


def _default_alpha_grid() -> list[float]:
    return list(DEFAULT_ALPHA_GRID)


@dataclass(frozen=True)
class DataConfig:
    """取得・キャッシュの設定。"""

    symbol: str = "EURUSD"
    start: str = "2022-01-01"
    end: str = "2024-12-31"
    timeframe: str = "H1"  # H1/H4/D1(Dukascopyの時間tickを集計する足)
    cache_dir: Path = Path("logs/dcm_cache")
    data_dir: Path = Path("data")  # fetch成果物(価格CSV/COT CSV)の保存先
    cache_ttl_hours: float = 24.0
    cot_lookback_weeks: int = 260  # 約5年ぶんのCOT週次履歴


@dataclass(frozen=True)
class FeatureConfig:
    """特徴量生成の設定。"""

    return_lags: tuple[int, ...] = (1, 3, 6, 12, 24)  # 過去リターンのlag(バー)
    rsi_window: int = 14
    atr_window: int = 14
    sma_windows: tuple[int, ...] = (20, 50)  # MA乖離の基準
    cot_index_weeks: int = 52  # COT indexの正規化窓(週)
    cot_publication_lag_days: int = COT_PUBLICATION_LAG_DAYS
    use_cot: bool = True  # False にすればテクニカルのみのベースラインを組める


@dataclass(frozen=True)
class LabelConfig:
    """将来リターンラベルの設定。"""

    horizon: int = 24  # 何バー先のリターンを予測するか
    volatility_normalized: bool = False  # True なら return/ATR をラベルにする


@dataclass(frozen=True)
class WalkForwardConfig:
    """ウォークフォワード・バックテストの設定。

    fx_backtester/walk_forward.py の train/test/step/purge/embargo 思想を
    回帰用に借用する。purge/embargo はラベルのホライズン以上に取り、
    train と test の情報重複(リーク)を断つのが要点。
    """

    train_bars: int = 2000
    test_bars: int = 500
    step_bars: int | None = None  # None なら test_bars(非重複ロール)
    purge_bars: int = 24  # train終端側の purge(>= horizon 推奨)
    embargo_bars: int = 24  # test開始側の embargo
    signal_z_threshold: float = 0.5  # 予測を train標準偏差で割り、|z|>閾値で建玉
    alpha_grid: list[float] = field(default_factory=_default_alpha_grid)
    cv_folds: int = 3  # train内の時系列CV分割数(α選択用)
    min_train_samples: int = 200

    def effective_step(self) -> int:
        return self.step_bars if self.step_bars is not None else self.test_bars


@dataclass(frozen=True)
class PipelineConfig:
    """全段を束ねる最上位設定。"""

    data: DataConfig = field(default_factory=DataConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    labels: LabelConfig = field(default_factory=LabelConfig)
    walk_forward: WalkForwardConfig = field(default_factory=WalkForwardConfig)
    initial_cash: float = 100_000.0
    risk_per_trade: float = 0.01  # 1トレードあたりのリスク割合(建玉サイジング)

    def with_symbol(self, symbol: str) -> PipelineConfig:
        return replace(self, data=replace(self.data, symbol=symbol))

    def with_walk_forward(self, **overrides: Any) -> PipelineConfig:
        return replace(self, walk_forward=replace(self.walk_forward, **overrides))

    def with_labels(self, **overrides: Any) -> PipelineConfig:
        return replace(self, labels=replace(self.labels, **overrides))
