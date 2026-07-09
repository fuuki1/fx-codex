"""Dukascopy価格 × CFTC COT → Ridge回帰による将来リターン予測パイプライン。

パイプライン全体像（土台〜最大設計）:

    Dukascopy価格 + CFTCポジション
            ↓  dukascopy.py / cftc.py      (Phase 1: 取得)
    データ品質チェック
            ↓  quality.py                  (Phase 2: 品質+正規化)
    正規化OHLCV / COT時系列
            ↓  features.py                 (Phase 3: 特徴量)
    テクニカル特徴量 / COT特徴量
            ↓  labels.py                   (Phase 3: ラベル)
    将来リターンラベル
            ↓  ridge.py                    (Phase 4: 回帰)
    Ridge回帰
            ↓  walk_forward.py             (Phase 5: 検証)
    ウォークフォワード・バックテスト
            ↓  report.py                   (Phase 6: 出力)
    期待値・勝率・DD・PF・Sharpe・特徴量寄与

設計原則:

- サードパーティ依存を増やさない。Ridgeはnumpyの閉形式解で自前実装
  (fx_intel/gbm.py が LightGBM を入れずに純Python実装したのと同じ判断)。
- リークゼロ。特徴量は判断時刻までの情報のみ、ラベルは未来リターン、
  COTは発表ラグを考慮したas-of結合、walk-forwardはpurge/embargo。
- 既存 fx_backtester / fx_intel の部品を読み取り再利用する
  (data正規化・indicators・metrics・macro COTコード・overfitting)。
"""

from __future__ import annotations

from .config import (
    DEFAULT_ALPHA_GRID,
    DataConfig,
    FeatureConfig,
    LabelConfig,
    PipelineConfig,
    WalkForwardConfig,
)

__all__ = [
    "DataConfig",
    "FeatureConfig",
    "LabelConfig",
    "WalkForwardConfig",
    "PipelineConfig",
    "DEFAULT_ALPHA_GRID",
]

__version__ = "0.1.0"
