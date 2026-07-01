# fx_backtester — FX 分析エンジン（ミッションクリティアル品質）

実弾判断に使える「信頼できる分析」を目的にした、先読みバイアスなし・コスト考慮・
アウトオブサンプル(OOS)検証つきのバックテスタ。`optimize/auto_optimize.py` が依存し、
ライブの `strategy.py` 用パラメータ（`strategy_params.json`）を OOS 検証して書き出す。

## 設計上の保証（なぜ信頼できるか）
- **先読みバイアスなし**: 指標は「そのバーまで」の情報のみ。エンジンはポジションを
  1 バー遅らせて執行（判断に使った情報より後の価格で約定）。
- **現実的コスト**: スプレッド + スリッページを pip→価格に変換し、ポジション変更ごとに課す。
- **ATR ストップ**: 高値/安値でストップ到達を判定、ギャップ時は寄りで約定（不利側）。
- **データ品質チェック**: 重複/欠損/高安逆転/非正値などは即エラー（fail-fast）。
- **頑健な指標**: Sharpe / Sortino / Profit Factor / 最大DD / 期待値 / 勝率 / CAGR（ゼロ割安全）。
- **OOS / ウォークフォワード**: in-sample で最適化→未学習区間で検証。`overfit_warning` と
  パラメータ安定性・取引数の十分性で採否を判断（過剰最適化を検出）。
- **堅牢性の定量化（`robust.py`・López de Prado / Bailey）**: PSR / Deflated Sharpe（歪度・尖度・
  標本長＋試行回数で多重検定を補正）、PBO（CSCV による過剰最適化確率）、モンテカルロ（定常
  ブートストラップで Sharpe・最大DD・勝ち越し確率の分布）。「高 Sharpe だが見せかけ」を弾く。

## 構成
```
fx_backtester/
  data.py        # 読み込み + データ品質チェック + 年率換算推定
  indicators.py  # SMA / ATR（先読みなし）
  costs.py       # pip→価格コスト
  engine.py      # バー単位ループ（1バー遅延・ストップ・コスト）
  metrics.py     # 指標一式
  robust.py      # 堅牢性: PSR / Deflated Sharpe / PBO(CSCV) / モンテカルロ
  strategy/      # base.py（契約）+ ma_cross.py
  registry.py    # 戦略レジストリ
  validation.py  # walk-forward / OOS / 過剰最適化ガード（PBO 込み）
  cli.py         # backtest(--robust) / walkforward / optimize
examples/        # 決定論的サンプル（generate_sample.py で再生成可）
tests/           # pytest
```

## 使い方
```bash
# 単発バックテスト（指標 JSON）
python -m fx_backtester.cli backtest --data examples/sample_prices.csv \
  --strategy ma_cross --param fast_window=20 --param slow_window=60 \
  --param atr_window=14 --param stop_atr_multiple=2.0 \
  --spread-pips USDJPY=0.3 --slippage-pips USDJPY=0.1

# ウォークフォワード検証サマリ
python -m fx_backtester.cli walkforward --data examples/sample_prices.csv \
  --strategy ma_cross --grid fast_window=10,20,30 --grid slow_window=40,60,80 \
  --grid atr_window=14 --grid stop_atr_multiple=1.5,2.0,2.5 \
  --spread-pips USDJPY=0.3 --train 252 --test 63

# OOS 検証で配備用パラメータを選ぶ（auto_optimize が利用）
python -m fx_backtester.cli optimize --data examples/sample_prices.csv ... \
  --grid ... --train 252 --test 63 --min-trades 20
```

`backtest` の出力は `sharpe_ratio` / `profit_factor` / `max_drawdown_pct` を含む（既存
`auto_optimize` と互換）。`--robust` を付けると `robust`（PSR + モンテカルロ分布）も出力。
`optimize` は配備用パラメータ＋`_validation`（OOS 成績・安定性・**pbo**・`overfit_warning`）を返す。

```bash
# 堅牢性指標つき単発バックテスト（PSR + モンテカルロ）
python -m fx_backtester.cli backtest --data examples/sample_prices.csv \
  --strategy ma_cross --param fast_window=20 --param slow_window=60 --robust
```

## データ形式
- prices: `timestamp,open,high,low,close`（timestamp は UTC パース可能であること）
- events: `timestamp,kind`（`high_impact`/`news`/`halt`/`blackout` は no-trade 窓として新規を抑止）

## 既知の制約（拡張点）
- 戦略は ma_cross の参照実装。`strategy/` に追加して `registry.py` に登録すれば拡張可能。
- 祝日カレンダーやスリッページの状態依存（ボラ連動）は未対応。
- ストップはバー内 1 回判定（ティック精度ではない）。
