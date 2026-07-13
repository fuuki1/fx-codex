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
| **1. AI学習ロジック** | **72** | **48** | Triple Barrier/long-short/no-trade/baseline dominance/較正/abstention/uncertainty は実装・テスト済み。実データOOSは COT PIT 単発のみで、価格ラベルの実データ学習は未実施 |
| **2. 検証の厳密さ** | **74** | **55** | 5分割・purge/embargo・CPCV-like・PSR/DSR/PBO/MTRL・block bootstrap・Holm・lockbox single-use・trial ledger 全て実装＋合成E2Eで昇格拒否を実証。実データ駆動の評価bundleが未生成 |
| **3. データ基盤** | **63** | **42** | PIT契約・immutable raw・quality state・bid/ask bar materializer・broker Protocol は実装。**COT PITは実CFTCで実証済み**。broker bid/ask は未接続、30取引日連続運用は未達 |

**総合判定: 判定2** — 70点相当の構造はほぼ完成しているが、実データ蓄積・shadow実績が不足しているため正式到達は未宣言。

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
| GBDT | `fx_intel/gbm.py` + `ml.py`（委員会側）。pipeline側は logistic/ridge を complex 候補として実装 | `test_gbm.py` | ⚠️ 部分（pipeline に GBDT 候補family未追加。§Gapへ） |

**構造の減点理由（72で止めた根拠）:**
- pipeline の complex 候補が `logistic_ridge` / `ridge_regression` のみ。タスク Phase 6 が要求する **GBDT を同一manifest比較の候補family** として pipeline に組み込む配線が未完（`fx_intel/gbm.py` は committee 側に存在するが authoritative pipeline の `_FAMILY_KIND` 未登録）。
- 階層型（global + pair/timeframe/regime補正）学習が設計文書レベルで、pipeline 実装は単一global止まり。

### 実証 48 / 100

- **実データによる価格ラベル学習の OOS 証拠が無い**。唯一の pipeline E2E は `usdjpy-synthetic-infra-selftest`（`synthetic_data:true`, `net_expectancy_r:-1.075`）で、**昇格は正しく拒否**（`failures=[non_synthetic_data, sample_size, net_expectancy, deflated_sharpe, probability_of_backtest_overfitting, untouched_lockbox, cost_stress_2x, …]`）。これは「ゲートが効く」証拠であって「モデルが効く」証拠ではない。
- 実データは **COT PIT のみ実証**（[reports/evidence/cot-cftc-real-pit-20260713](evidence/cot-cftc-real-pit-20260713/README.md)）。ただしこれは特徴量ソースの一つであり、学習ターゲット（価格の triple barrier）ではない。
- → 実証を50未満に据える。「構造 70相当 / 実証 50未満」（タスク§12の想定文面に一致）。

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

### 実証 55 / 100

- 合成E2Eで **昇格拒否の全経路が実証済み**（gate が正しく効く）。これは実証として価値がある（fail-closed の実挙動確認）。
- **deterministic replay は実データ（COT 13,727行）で実証済み** — audit がraw から再構成し `passed`。これは検証軸の実証を押し上げる本物の証拠。
- ただし **合格側（真の実データで gate を全部通す）は未実証**。実データ評価bundleが存在しないため、PBO/DSR/CI 等が実データ分布で妥当に動く証拠がない。
- → 60未満（タスク§12想定の「構造70以上 / 実証60未満」に一致）。

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

### 実証 42 / 100

- **COT PIT のみ実データで完全実証**：実CFTC 13,727行、SHA256照合、count整合、deterministic replay、PITゲート（取得前=unavailable / 後=usable）。→ [evidence bundle](evidence/cot-cftc-real-pit-20260713/README.md)。
- **broker bid/ask は完全に未接続**。実際に取引予定のbrokerのquoteを1件も取り込んでいない。quote→bar の実データ実証なし。
- **30取引日連続稼働の証拠なし**。単発snapshotのみ。launchd常駐は開発機TCC制限で不可、Mac mini本番未配備。
- macro/calendar/news の実PIT運用証拠なし。
- → 実運用40台（タスク§12想定の「構造60以上 / 実運用40未満」に一致）。

---

## 総合評価と判定

| | 構造 | 実証 |
|---|---:|---:|
| AI学習ロジック | 72（≒70相当） | **48（<50）** |
| 検証の厳密さ | 74（≥70） | **55（<60）** |
| データ基盤 | 63（≒60以上） | **42（<40台）** |

```
AI学習ロジック:   構造 70相当    実証 50未満
検証:            構造 70以上    実証 60未満
データ基盤:       構造 60以上    実運用 40未満
```

### 判定2

> **70点相当の構造は（ほぼ）完成したが、実データ蓄積・shadow実績が不足しているため正式到達は未宣言。**

証拠が不足しているため判定2とする（タスク§最終出力の指示に従う）。構造面は本セッションの監査で「既存PRスタックが要求インフラの大半を実装済み・708テスト緑」であることを確認済み。実証面は本セッションで **COT PIT を実データで初めて実証** し、データ基盤とvalidationの実証軸をわずかに押し上げたが、**価格の実データ学習・broker bid/ask接続・30取引日連続運用**という3つの主要ボトルネックが残るため、70点の"実証"は宣言できない。

関連: [research_v2_test_evidence.md](research_v2_test_evidence.md) / [research_v2_unresolved_risks.md](research_v2_unresolved_risks.md) / [docs/audits/RESEARCH_V2_GAP_ANALYSIS.md](../docs/audits/RESEARCH_V2_GAP_ANALYSIS.md)
