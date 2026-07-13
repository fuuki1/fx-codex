# AI 学習ロジック V2 設計

**ステータス:** 構造実装済み（authoritative pipeline に結線、708テスト緑）／実データOOS未実証（判定2）。
**一次コード:** `fx_backtester/{labeling,experiment_pipeline,calibration,drift}.py`。**このドキュメントとコードが食い違う場合はコードが正。**

## 1. 目的関数の再定義

### 旧（補助へ降格）
「約24時間後に方向が合っていたか」（`fx_intel/journal.py` の方向採点）。**主目的変数にはしない**。運用の健全性モニタとしては残す。

### 新（主目的変数）
各予測時点に対し、**ロング・ショート両方向**を Triple Barrier で採点する。実装は `labeling.py::triple_barrier_label(direction=+1 or -1, ...)` を両方向で呼ぶ。

各方向で以下を出力（`TripleBarrierLabel`）:

```
TP before SL / gross R / net R / MFE R / MAE R / bars to exit / first touch / ambiguity flag
```

Triple Barrier 条件（`labeling.py` で実装・テスト済み）:
- entry = **次バー始値**（`test_entry_at_prediction_close_observes_the_next_full_bar`）
- forming bar を未来経路に含めない
- same-bar TP/SL 同時到達 → 保守的 **stop-first**（`test_stop_first_is_conservative_when_both_barriers_touch`）または曖昧として除外（`test_ambiguous_policy_can_abstain`）
- gap-through 対応（`test_gap_through_stop_uses_worse_open_price`）
- `label_end_time` 保存（purge用、`test_multi_horizon_output_carries_end_time_for_purging`）
- spread/slippage/commission/financing を控除（net R）
- close-only 経路は品質上限を付け、**正式昇格データには使わない**

## 2. モデル出力

最終的に推定する量:

```
P(TP before SL | long)      P(TP before SL | short)
E[net R | long]             E[net R | short]
uncertainty_long            uncertainty_short
```

確率は `calibration.py`（Platt/isotonic/beta、**専用 calibration window** でのみ fit）で較正。uncertainty は較正の分散＋`drift.py` のドリフト指標から導出。

## 3. アクション決定

```python
action = argmax(E[net R | long], E[net R | short], 0.0)   # 0.0 = no-trade
```

**必ず no-trade**（`experiment_pipeline.py` の abstain 経路、`side=="abstain"`→0R）:
- 最大期待Rが 0 以下
- bootstrap 信頼区間下限が 0 以下
- データ品質不足 / drift 検出 / スプレッド異常 / 重要イベント未確認
- モデル間不一致大 / サンプル不足 / 対象セルの期待値が不明
- lockbox または test 汚染

### no-trade を正式 baseline に
`no-trade` は暗黙のゼロではなく **trial ledger と selection に正式候補として記録**（PR #33、`test_trial_ledger.py::test_distinct_candidate_count`）。

## 4. baseline 群と dominance gate

`experiment_pipeline.py::_FAMILY_KIND`:

| family | 種別 | 実装状態 |
|---|---|---|
| no-trade / constant_probability / random_uniform | baseline | ✅ |
| always_long / always_short / previous_return_sign | baseline | ✅ |
| ma_crossover / rsi_reversion | baseline | ✅ |
| logistic_ridge / ridge_regression | complex | ✅ |
| **GBDT** | complex | ✅ **登録済み**（`MODEL_FAMILY_KIND["gbdt"]="complex"`、`experiment_pipeline.py:693`。標準ライブラリのみの gradient boosting 実装。Newton step で学習）。**実データ run で `gbdt-small` が選択されたことを実証**（[evidence](../reports/evidence/histdata-usdjpy-real-2024-1h-20260713/README.md)） |

**複雑モデルは tune 区画で best baseline を厳密に上回る場合のみ admissible**（`test_evaluation_gates.py`）。的中率ではなく**コスト控除後期待R**が主指標。

> 【2026-07-13 訂正】初版で「GBDTは pipeline 未登録」と記したのは誤り。定数名を `_FAMILY_KIND` と誤認したもので、正しくは `MODEL_FAMILY_KIND` に登録済み。実データ run で GBDT が選択候補として動作することを確認済み。`fx_intel/gbm.py`（committee側）とは別に pipeline 独自の GBDT 実装が存在する。

## 5. 階層型学習（設計、pipeline 実装は残作業）

データ不足時に `symbol×timeframe×direction×regime` ごとの完全別モデルを**作らない**。優先:

```
global model + pair adjustment + timeframe adjustment + regime adjustment
```

各補正は十分サンプルがある場合のみ有効化。**現状 pipeline は単一 global。** 階層縮退の結線は P2-2 として登記。

## 6. 旧 `ml.py` の扱い

旧 `fx_intel/ml.py` は同一 validation set を early stopping・較正・最終Brier に使い回すため、**正式昇格には使用禁止**。観測・互換用途で残すが、正式 artifact 生成は `experiment_pipeline.py` のみ（タスク§2遵守、`test_experiment_pipeline.py` が単一経路を担保）。ML委員は検証Brierが基準率を2%以上改善しないと `usable=False`（判断不参加）。

## 7. 完了条件に対する現状（タスク§12）

| 条件 | 状態 |
|---|---|
| 主ラベル = コスト控除後 Triple Barrier | ✅ 構造 |
| long/short 両方向採点 | ✅ 構造 |
| no-trade 正式候補 | ✅ 構造 |
| baseline 優越必須 | ✅ 構造 |
| 確率と期待R出力 | ✅ 構造 |
| uncertainty / abstention / drift停止 | ✅ 構造 |
| 正式artifactは authoritative pipeline のみ | ✅ 構造 |
| **実データで再現可能なOOS証拠** | ❌ **未**（COT PIT は特徴量ソースの実証であり価格ラベル学習ではない） |

→ **構造 70相当 / 実証 50未満**。実価格OHLC接続＋pipeline 実データ完走が最大のボトルネック。
