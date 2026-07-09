# dukascopy_cftc_model

Dukascopy価格データ × CFTC COT（投機筋ポジション）から、**Ridge回帰で将来リターンを
予測**し、ウォークフォワード・バックテストで期待値・勝率・DD・PF・Sharpe・特徴量寄与を
出力するパイプライン。

```
Dukascopy価格 + CFTCポジション
        ↓  dukascopy.py / cftc.py      データ取得（実データ・キー不要）
データ品質チェック
        ↓  quality.py                  coverage スコア + warnings
正規化OHLCV / COT時系列
        ↓  quality.py                  log-return / 週次整列
テクニカル特徴量 / COT特徴量
        ↓  features.py                 as-of結合でリーク防止
将来リターンラベル
        ↓  labels.py                   horizon 先の log-return
Ridge回帰
        ↓  ridge.py                    numpy閉形式 (XᵀX+αI)⁻¹Xᵀy
ウォークフォワード・バックテスト
        ↓  walk_forward.py             purge/embargo + α内部CV
期待値・勝率・DD・PF・Sharpe・特徴量寄与
           report.py                   + PBO/DSR 過剰最適化検定
```

## 設計原則

- **サードパーティ依存を増やさない** — Ridgeは numpy の閉形式解で自前実装（sklearn不要）。
  既存 `fx_intel/gbm.py`（純Python GBDT）と同じ判断。
- **リークゼロ** — 特徴量は判断時刻までの情報のみ。COTは発表ラグ（既定3日）を考慮した
  as-of結合。walk-forward は purge/embargo（>= horizon）で train/test の情報重複を断つ。
- **劣化しても死なない** — 各データ取得は独立に失敗でき、失敗はスキップ + 品質ゲートに反映。
  一部の時間の取得失敗で全体は落ちない。
- **決定論・来歴** — モデルは `to_dict`/`from_dict` でJSON直列化。レポートに来歴・品質を記録。

## 既存コードベースの再利用

| 目的 | 再利用先 |
|---|---|
| OHLCV正規化・重複検知 | `fx_backtester.data.load_price_csv` |
| テクニカル指標 | `fx_backtester.indicators`（sma/rsi/average_true_range） |
| CFTC契約コード | `fx_intel.macro`（COT_CONTRACT_CODES / CFTC_COT_URL） |
| バックテスト指標 | `fx_backtester.metrics.calculate_metrics` |
| 過剰最適化検定 | `fx_backtester.overfitting`（PBO/DSR） |

## 使い方（CLI）

```bash
# 1. 実データを取得（Dukascopy価格 + CFTC COT → data/ に保存）
dcm fetch --symbol EURUSD --start 2022-06-01 --end 2024-12-31 --timeframe H1

# 2. 品質チェック
dcm qa --symbol EURUSD --timeframe H1

# 3. 全パイプライン実行（取得済みデータで）→ レポートJSON + 標準出力サマリ
dcm run --symbol EURUSD --horizon 24 --offline
```

`dcm run`（`--offline` 無し）は取得から一括実行する。`--out` でレポートJSONの
出力先を指定できる（既定 `data/<SYM>_report.json`）。

## Python API

```python
from dukascopy_cftc_model.config import PipelineConfig
from dukascopy_cftc_model.pipeline import run_pipeline

cfg = PipelineConfig().with_symbol("EURUSD").with_labels(horizon=24)
report = run_pipeline(cfg)          # 取得込み
print(report.summary())             # 人間可読サマリ
report_dict = report.to_dict()      # JSON化可能な全結果
```

## データソース（全て無料・APIキー不要）

- **Dukascopy datafeed** — 時間ごとの tick バイナリ（`.bi5`、LZMA圧縮）を時間足OHLCVへ集計。
  URLの月は0始まり。tick は20バイト `>iiiff`（ms, ask, bid, askvol, bidvol）。
- **CFTC Socrata COT** — レガシー先物の週次 Commitments of Traders（投機筋 long/short/net）。

## テスト

```bash
pytest tests/test_dcm_*.py -q     # フェーズ別（ネットワーク非依存・フィクスチャベース）
```

- `test_dcm_smoke` — パッケージ/CLI骨格
- `test_dcm_data` — .bi5デコード / COT時系列パース
- `test_dcm_quality` — 品質チェック / 正規化
- `test_dcm_features` — 特徴量 / ラベル / **リーク無し**（as-of結合）
- `test_dcm_ridge` — β回復 / α→0でOLS一致 / 直列化
- `test_dcm_walk_forward` — purge/embargo / 学習可能シグナルで正の期待値 / ノイズで≒0
- `test_dcm_report` — 最終出力の全項目 / DSR / JSON化
