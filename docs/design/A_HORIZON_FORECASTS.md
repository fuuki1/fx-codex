# 設計A: マルチホライズン予測トラック

実装ステータス: A0/A1/A2コード実装済み、全セルshadow開始。A3の統合入力資格はセル別ゲートで自動判定する。

## 確定仕様

- ホライズン: 本番候補 `15m/30m/1h/3h/6h/12h/24h/3d` の8本と、恒久shadowの `5m`。合計9本。`9h` は含めない。
- 対象: `USDJPY/EURUSD/GBPUSD`。
- 横ばい: `max(ATR_h × 0.1, 実測spread × 2)`。
- 分析時間足 `15m/1h/4h/1d` は特徴量、ホライズンは予測対象として分離する。
- 既存の融合判断・時間足別判断・Discord通知・発注系は変更しない。

## データ契約

予測journalは `logs/briefing_horizon_forecasts.jsonl`。1行は1
`(symbol, horizon, cycle)` で、契約は `horizon-pit-v1`。

書込み前に次をfail-closedで検証する。

1. `source_cutoff <= max_feature_available_time <= prediction_time`
2. 全時刻がtimezone付き
3. `p_up + p_down + p_flat = 1`
4. ホライズンlabelと時間が定数表に一致
5. 同一batch内に重複prediction IDがない

中立、standby、closed、鮮度gateで止めた行も記録する。全行は初期
`track_stage=shadow` で、5mは `shadow_only=true` のため昇格不能。

## 生成・採点・学習

- 生成: `fx_intel/horizon_forecast.py`
- PIT journal: `fx_intel/horizon_journal.py`
- 採点・学習: `fx_intel/horizon_learning.py`
- 共有定数: `fx_intel/horizons.py`

採点は完了済みM5価格系列を使い、市場オープン時間で満期最近傍を選ぶ。方向、
3クラスBrier/log loss、p10/p50/p90 pinball loss、帯カバレッジ、値幅比、
MFE/MAE、spread控除後Rを同じコードパスで再計算する。

セルはホライズン別gapで間引く。n>=50で3クラス較正、n>=40で
volatility×session経験帯、n>=20でホライズン全体帯へ縮退する。重みは各入力が
20件以上になってからシュリンク付きで更新する。

昇格資格は、実効n、Brierがclimatology以下、80%帯カバレッジ70–90%、
平均純R>0、直近7日のPIT/鮮度違反0を全て満たす場合だけ付く。5mは成績に関係なく
恒久shadow。ここでの昇格は将来の時間軸統合への入力資格だけで、発注権限ではない。

## 3ヘッドGBDTのA2前レビュー

既存の3ヘッド実装（方向分類、期待純R、p10/p50/p90分位点群）は、A2配線前に次を確認した。

- 正規ラベル `realized_net_r` だけを使用する。
- `label_version` と `cost_model_id` の混在を拒否する。
- 時系列split、embargo、最低件数、RMSE/DSR/t-stat gateを持つ。
- 分位点crossingは出力時に順序化する。
- 回帰・ML・純R採点の契約テスト58件が成功。

ホライズン側のモデル入力契約は `horizon-pit-v1` に固定した。経験較正/経験帯は
常時baselineとして残し、将来GBDTがpinball lossでbaselineに負けるセルでは置換しない。

## 運用

`com.fx-codex.horizon` が5分ごとに次を実行する。

```bash
.venv/bin/python fx_briefing.py --horizon-only --no-discord --no-llm \
  --no-export-events --no-event-archive
```

ロールバックは `--no-horizon-forecasts`、またはlaunchdの
`com.fx-codex.horizon` 停止。既存journalや既存判断には触れない。
