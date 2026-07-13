# research-v2 現状スコア — 2026-07-13

**採点者:** 主任クオンツ/MLE/データ基盤/モデルリスク管理者ロール（監査セッション）
**採点基点:** `integration/research-v2`（`origin/claude/pr-c-bidask-bars` = PR #35先端 `eb4263c` の上に本セッションの2コミット）
**実測ゲート:** `pytest 708 passed` / `ruff` pass / `black` pass / `mypy fx_backtester fx_intel data_platform tools *.py` **104ファイル clean**

> この文書は**工程・実装成熟度の監査**であり、収益性・投資助言ではない。実データによるモデル性能証拠は現時点で存在しないため、モデル性能軸を高得点にしていない（タスク禁止事項の遵守）。

---

## 採点方針

タスク要求どおり、**1つの数字に丸めず4軸で別々に**採点する。各軸は「構造（コードとして実装され単体テスト済みか）」と「実証（実データ・実運用の証拠があるか）」を分離する。

| 軸 | 構造 | 実証 | 一言 |
|---|---:|---:|---|
| **1. AI学習ロジック** | **75** | **58** | Triple Barrier/long-short/no-trade/baseline dominance/**GBDT**/較正/abstention/uncertainty は実装・テスト済み。**実USD/JPY 2024で pipeline 完走・GBDT選択・OOS評価を実証**（負の結果＝偽alpha無し）。価格ソースが close-only・単年のため実証は中位 |
| **2. 検証の厳密さ** | **74** | **62** | 5分割・purge/embargo・CPCV-like・PSR/DSR/PBO/MTRL・block bootstrap・Holm・lockbox single-use・trial ledger 全て実装＋合成E2E＋**実データE2Eで昇格拒否と deterministic replay を実証**。外部custodyは未実装 |
| **3. データ基盤** | **63** | **45** | PIT契約・immutable raw・quality state・bid/ask bar materializer・broker Protocol は実装。**COT PIT（実CFTC）と実FX価格取込を実証**。broker bid/ask は未接続、30取引日連続運用は未達 |

**総合判定: 判定2** — 70点相当の構造はほぼ完成しており、本セッションで**実データE2E**（COT PIT + 実価格 pipeline run）まで実証したが、**close-only・単年・単一pair・30取引日連続運用なし**のため正式到達は未宣言。

> 【2026-07-13 更新】初版から実証軸を上方修正。理由: (1) 実HistData USD/JPYで authoritative pipeline を完走し、GBDT含む10候補のOOS評価と昇格拒否・deterministic replay を実データで実証（[evidence](evidence/histdata-usdjpy-real-2024-1h-20260713/README.md)）。(2) GBDTが pipeline 未登録という初版の記述は誤りで、実際は登録済み・実データ run で選択された。ただし close-only（bid/ask無し）・2024単年・USD/JPY のみのため、実証は「70到達」には届かない。

---

## 1. AI学習ロジック

### 構造 72 / 100（実装・単体テスト済み）

| 要求（タスク§3,§12） | 実装 | テスト | 状態 |
|---|---|---|---|
| 主ラベル = コスト控除後 Triple Barrier | `fx_backtester/labeling.py` (`direction:int` +1/-1, net R, spread/slippage/commission控除) | `tests/test_labeling.py` 9件 | ✅ |
| long/short 両方向採点 | 同上（`triple_barrier_label(direction=...)` を両方向で呼ぶ設計、gap-through も方向別） | `test_timeout_and_short_direction_are_direction_aware` | ✅ |
| same-bar 保守的 stop-first / 曖昧除外 | `labeling.py` | `test_stop_first_is_conservative_when_both_barriers_touch` / `test_ambiguous_policy_can_abstain` | ✅ |
| next-bar entry / forming bar 非混入 | `labeling.py` | `test_entry_at_prediction_close_observes_the_next_full_bar` | ✅ |
| `label_end_time` 保存（purge用） | `labeling.py` | `test_multi_horizon_output_carries_end_time_for_purging` | ✅ |
| no-trade を正式候補 | `experiment_pipeline.py` (`side=="abstain"`→0R, `abstention_rate`)、trial ledger に候補記録（PR #33） | `test_trial_ledger.py::test_distinct_candidate_count` | ✅ |
| baseline群（constant/random/always-L/S/prev-sign/MA/RSI/logistic/ridge） | `experiment_pipeline.py` `_FAMILY_KIND` | `test_experiment_pipeline.py` | ✅ |
| 複雑モデルは baseline 優越時のみ採用 | `experiment_pipeline.py`（complex は tune で best baseline を厳密に上回る場合のみ admissible） | `test_evaluation_gates.py` | ✅ |
| 確率+期待R出力 | `experiment_pipeline.py`（`P(TP before SL)`, `E[net R]`） | pipeline tests | ✅ |
| 確率較正（Platt/isotonic/beta、専用window） | `calibration.py` | `test_calibration.py` | ✅ |
| uncertainty / abstention | `calibration.py`（abstention policy）、`drift.py` | `test_drift.py` | ✅ |
| drift時停止 | `drift.py`（PSI/KS→`abstain`/`demote`） | `test_drift.py` | ✅ |
| 正式artifactはauthoritative pipelineのみ | `experiment_pipeline.py` が唯一のformal経路。旧 `ml.py` は補助 | `test_experiment_pipeline.py` | ✅ |
| GBDT | **pipeline に登録済み**（`MODEL_FAMILY_KIND["gbdt"]="complex"`、標準ライブラリのみの gradient boosting）。`fx_intel/gbm.py` は committee 側に別途存在 | `test_gbm.py` + 実データ run で選択 | ✅ |

**構造の減点理由（75で止めた根拠）:**
- 階層型（global + pair/timeframe/regime補正）学習が設計文書レベルで、pipeline 実装は単一global止まり（P2-2）。
- close-only 経路の品質上限は実装されているが、bid/ask を実測して net R を実データで駆動する経路は未接続（データ基盤側の制約）。

### 実証 58 / 100

- **実データで pipeline を完走・GOOS評価を実証**（[histdata-usdjpy-real-2024-1h](evidence/histdata-usdjpy-real-2024-1h-20260713/README.md)）: 実USD/JPY 2024 1h（6,265本）で 10候補（baseline7+logistic+ridge+**GBDT**）を train→tune→test。`gbdt-small` 選択、`net_expectancy_r:-0.065`、CI下限 -0.203、DSR 0.167、PBO 0.20、**Holm補正後 p値=全候補1.0**、PIT/future violations 0/0、**昇格DENIED**、deterministic replay 一致。→ **フレームワークが実データで偽alphaを作らないことの実証**。
- 合成E2E（`usdjpy-synthetic-infra-selftest`, `synthetic_data:true`）でも昇格拒否を実証（ゲートの健全性）。
- COT PIT（実CFTC）は特徴量ソースとして別途実証（[cot-cftc-real-pit](evidence/cot-cftc-real-pit-20260713/README.md)）。
- → 実証は50を超えたが、**close-only・2024単年・USD/JPYのみ**のため 70 には届かない。「構造 70相当 / 実証 50台後半」。

---

## 2. 検証の厳密さ

### 構造 74 / 100（実装・単体テスト済み）

| 要求（タスク§4,§12） | 実装 | テスト |
|---|---|---|
| 5区画分離（train/tune/calibration/test/lockbox） | `experiment_pipeline.py` + `time_series_validation.py` | `test_experiment_pipeline.py::TestManifestContract` |
| purge / embargo / `label_end_time`重複除去 | `time_series_validation.py` | `test_time_series_validation.py` |
| anchored + rolling walk-forward / CPCV-like | `time_series_validation.py` / `walk_forward.py` | 同上 |
| test 単回使用・見た後の再調整禁止 | `lockbox.py` + `experiment_pipeline.py` | `test_experiment_pipeline.py::test_experiment_output_directory_is_single_use` |
| lockbox single-use / 開封後frozen / 再探索禁止 | `lockbox.py` | `test_lockbox.py`（`test_single_use_access`, `test_rerun_after_open_is_frozen`, `test_registry_wipe_cannot_reopen_an_evaluated_bundle`） |
| 全trial記録（失敗も）・改竄検出 | `trial_ledger.py`（append-only, hash chain） | `test_trial_ledger.py`（`test_failed_trial_requires_reason`, tamper検出5件） |
| 実験横断の多重性（research_program/hypothesis_family/experiment/trial ID） | `experiment_manifest.py` / `trial_ledger.py` | `test_experiment_pipeline.py` |
| baseline/no-trade優越必須 | `experiment_pipeline.py` | `test_evaluation_gates.py` |
| コストストレス（post-hoc減算でなくengine再実行） | `stress.py` | `test_stress.py` |
| 信頼区間（block bootstrap） | `statistical_validation.py::circular_block_bootstrap_mean_ci` | `test_statistical_validation.py` |
| PSR / MTRL / block sign permutation / Holm | `statistical_validation.py` | 同上 |
| PBO(CSCV) / DSR（欠測は fail-closed 拒否） | `overfitting.py`（`probability_of_backtest_overfitting`, `deflated_sharpe_ratio`） | `test_overfitting.py` |
| deterministic replay | `experiment_pipeline.py`（`test_run_is_reproducible`）＋ **COT audit で実データ replay 実証済み** | `test_experiment_pipeline.py` + 実データtranscript |
| dirty worktree / commit / raw hash / lock mismatch は fail-closed | `experiment_pipeline.py` | `test_dirty_worktree_rejects_formal_claim`, `test_commit_mismatch_is_rejected`, `test_raw_hash_mismatch_fails_closed`, `test_dependency_lock_mismatch_fails_closed` |
| 統計値が計算不能なら0でなく unavailable→昇格失敗 | `governance.py`（hard veto, 欠損証拠=不合格） | `test_governance.py::test_missing_or_synthetic_evidence_fails_closed` |

**構造の減点理由（74で止めた根拠）:**
- **外部custody未実装**。lockbox は「durable local custody」であり、タスク§4が要求する GitHub Actions artifact / 別アカウントS3 / 書き込み専用外部ストレージのいずれも未配線。インターフェース・runbook も未整備（本セッションで runbook を新規作成、実装は未）。「ローカル保管で完全防御」とは主張しない。
- month/pair/session concentration 評価は fold_dispersion 等の部品はあるが、pipeline 出力の正式ゲートとしての結線が部分的。

### 実証 62 / 100

- **実データ E2E で検証機構が実証済み**: 実USD/JPY 2024 で PBO 0.20 / DSR 0.167 / Holm補正後 p値 全1.0 / block bootstrap CI が実データ分布で妥当に動作。昇格拒否の全経路（net_expectancy/CI/DSR/cost_stress_2x/untouched_lockbox）が**実データで**発火。
- **cross-pair 頑健性を実証**（[multipair](evidence/histdata-multipair-real-2024-1h-20260713/README.md)）: USD/JPY・EUR/USD・GBP/USD の3pair全てで net expectancy が負・CI下限が負・昇格DENIED。選択候補はpairで異なり（GBDT / always-long）、単一モデルのチェリーピッキングでないことを示す。**pair-concentration 懸念に実データで応答**。
- **deterministic replay を実データで実証**: COT audit のraw再構成 `passed` に加え、3pair全てが2回runで同一 result hash（fedc9d83 / 0a18713c / 8bbdbf9f）。
- **lockbox の再最適化拒否を実データで実証**: pip_size 修正で manifest content が変わった際、lockbox が旧 experiment_id での再実行を拒否（`lockbox_violation`）→ 新 id 必須。
- ただし **実データ評価は close-only・単年（2024）**。多年・複数regime・合格側（実データで gate 全通過）は未実施。外部custodyも未実装。
- → 60台前半。実データで gate が pair をまたいで頑健に動くことは実証、網羅性（多年/合格側）は未達。

---

## 3. データ基盤

### 構造 63 / 100（実装・単体テスト済み）

| 要求（タスク§5,§6,§12） | 実装 | テスト | 状態 |
|---|---|---|---|
| PIT契約（availability正規化・future as-of拒否・canonical hash） | `data_platform/contracts/pit_record.py` + `fx_backtester/point_in_time.py` | `test_point_in_time.py` | ✅ |
| immutable raw（content-addressed, SHA-256, append-only, quarantine） | `data_platform/raw/immutable_store.py`, `content_addressed.py` / `fx_backtester/pit_dataset.py` | `test_pit_dataset.py` | ✅ |
| broker quote adapter（bid/ask/mid/spread/… 全フィールド） | `data_platform/contracts/market_quote.py` + `data_platform/adapters/broker.py`（Protocol + Replay + Unimplemented=fail-closed） | `test_data_platform_bars.py` | ⚠️ **実broker未接続**（credentials無し、正直に unvalidated と明記） |
| bid/ask bar materialization（bid/ask/mid OHLC, spread p95/max, stale/gap/dup） | `data_platform/materialize/bid_ask_bars.py` | `test_data_platform_bars.py` | ✅（合成/replay入力で） |
| quote→bar 再生成 determinism | `bid_ask_bars.py` | `test_data_platform_bars.py` | ✅ |
| 品質SLO状態（usable/degraded/quarantined/unavailable） | `data_platform/quality/state.py`, `quality/bars.py` | `test_data_platform_quality.py` | ✅ |
| cross-source divergence | `data_platform/quality/`（divergence監査 §2-3, PR #35） | `test_data_platform_bars.py` | ⚠️ 1ソースのみで実証（2系統実データ未接続） |
| 経済指標PIT（scheduled≠公開時刻、revision別保存、fetch失敗を0置換しない） | `data_platform/contracts/economic_event.py` / `macro_release.py` | contract tests | ⚠️ 契約のみ（実FRED/カレンダー未接続） |
| **COT PIT（実CFTC取込、fixtureで終わらせない）** | `fx_intel/cot_pit.py` + `tools/cot_pit_pipeline.py` | `test_cot_pit*.py` 35件 **＋実データ実証** | ✅ **実証済み** |
| ニュースPIT | `data_platform/contracts/news_event.py` | contract tests | ⚠️ 契約のみ |
| single-writer規律 | broker adapter の `writer_id` スタンプ + launchd排他 | `test_data_platform_bars.py` | ✅（設計）/ ⚠️（本番配備なし） |

### 実証 45 / 100

- **COT PIT を実データで完全実証**：実CFTC 13,727行、SHA256照合、count整合、deterministic replay、PITゲート（取得前=unavailable / 後=usable）。→ [evidence](evidence/cot-cftc-real-pit-20260713/README.md)。
- **実FX価格取込を3pairで実証**：HistData USD/JPY・EUR/USD・GBP/USD 2024 M1→1h（各6,265/6,295/6,281本）を EST→UTC 変換・hash固定し、pipeline の `load_price_csv` で読込・品質検査・triple-barrier ラベル・OOS評価まで通した。`scripts/fetch_histdata.py` で committed CSV を byte一致再現可。→ [USD/JPY](evidence/histdata-usdjpy-real-2024-1h-20260713/README.md) / [cross-pair](evidence/histdata-multipair-real-2024-1h-20260713/README.md)。
- ただし **close-only（bid/ask無し・volume無し）**。取引予定brokerの実bid/ask quoteは1件も取り込んでいない（quote→bar の実bid/ask実証なし）。
- **30取引日連続稼働の証拠なし**。単発・単年。launchd常駐は開発機TCC制限で不可、Mac mini本番未配備。
- macro/calendar/news の実PIT運用証拠なし。
- → 実運用40台（構造60以上 / 実運用40台）。実FX価格を1本通した分だけ COT のみだった初版から上昇したが、broker bid/ask と連続運用が主要ボトルネックとして残る。

---

## 総合評価と判定

| | 構造 | 実証 |
|---|---:|---:|
| AI学習ロジック | 75（≒70相当） | **58（50台後半）** |
| 検証の厳密さ | 74（≥70） | **62（60台前半）** |
| データ基盤 | 63（≒60以上） | **45（40台）** |

```
AI学習ロジック:   構造 70相当    実証 50台後半
検証:            構造 70以上    実証 60台前半
データ基盤:       構造 60以上    実運用 40台
```

### 判定2

> **70点相当の構造は（ほぼ）完成し、本セッションで実データE2Eまで実証したが、close-only・単年・単一pair・shadow実績なしのため正式到達は未宣言。**

証拠が「70の実証」には届かないため判定2とする（タスク§最終出力の指示に従う）。構造面は監査で「既存PRスタックが要求インフラの大半を実装済み・708テスト緑」を確認。実証面は本セッションで **(1) COT PIT を実CFTCで実証、(2) authoritative pipeline を実USD/JPY価格で完走（GBDT選択・OOS評価・昇格拒否・deterministic replay）** まで到達し、実証軸を明確に押し上げた。しかし **close-only（bid/ask無し）・2024単年・USD/JPY のみ・broker bid/ask未接続・30取引日連続運用なし** が残るため、70点の"実証"は宣言できない。**「フレームワークが実データで偽alphaを作らない」ことは実証したが、「実データで利益が出る」ことは実証していない（そもそも close-only では原理的に困難）。**

関連: [research_v2_test_evidence.md](research_v2_test_evidence.md) / [research_v2_unresolved_risks.md](research_v2_unresolved_risks.md) / [docs/audits/RESEARCH_V2_GAP_ANALYSIS.md](../docs/audits/RESEARCH_V2_GAP_ANALYSIS.md)
