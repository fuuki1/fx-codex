# 検証プロトコル V2

**ステータス:** 構造実装済み（708テスト緑）／外部custody未実装・実データ評価bundle未生成。
**一次コード:** `fx_backtester/{time_series_validation,statistical_validation,overfitting,lockbox,trial_ledger,experiment_pipeline,governance,stress}.py`。**コードが正。**

## 1. データ5区画（物理・論理分離）

```
train  →  tune  →  calibration  →  test  →  withheld_lockbox
```

| 区画 | 唯一の用途 | 見てはいけないもの |
|---|---|---|
| train | モデルパラメータ学習 | test / calibration / lockbox |
| tune | ハイパラ・long/short閾値・abstention閾値・candidate選択 | test / lockbox |
| calibration | Platt/isotonic/beta の選択と fit | test / lockbox |
| test | 選択済みモデルの**1回限り**正式OOS。見た後の再調整禁止 | lockbox |
| lockbox | 事前登録の最終確認。**single-use**、開封後 frozen、再探索禁止 | — |

実装: `experiment_pipeline.py` が区画を manifest で固定（`TestManifestContract`）。**旧 `ml.py` のように同一 validation set を early stopping/較正/Brier に使い回す経路は正式昇格に使用禁止**。

## 2. walk-forward と交差検証

| 手法 | 実装 | テスト |
|---|---|---|
| anchored / rolling walk-forward | `time_series_validation.py` / `walk_forward.py` | `test_time_series_validation.py` |
| purge（ラベル期間重複除去） | `label_end_time` ベース | 同上 |
| embargo（split境界） | `time_series_validation.py` | 同上 |
| 同一時点重複予測除去 | 同上 | 同上 |
| CPCV-like | 同上 | 同上 |
| regime / month / pair / session 別評価 | `fold_dispersion`/`rank_stability` 等の部品 | ⚠️ **正式ゲート化は部分的**（P2-3） |

## 3. 統計検定（正式評価bundleの最低要件）

実装済み（`statistical_validation.py` / `overfitting.py` / `metrics.py`）:

```
net expectancy R + CI(circular block bootstrap) / profit factor / win rate
average win R / average loss R / Sharpe per trade / Sortino / max drawdown R
recovery factor / tail loss / Brier / log loss / calibration error / Brier skill
PSR / DSR / MTRL / PBO(CSCV) / block sign permutation / Holm補正
```

**統計値が計算不能なら 0 にせず `unavailable` → 昇格失敗**（`governance.py` の require が `None` を未達扱い、`test_missing_or_synthetic_evidence_fails_closed`）。

## 4. 実験横断の多重性

階層ID: `research_program_id / hypothesis_family_id / experiment_id / trial_id`（`experiment_manifest.py`）。

**trial ledger は全実験で共有**（`trial_ledger.py`、append-only + hash chain）。記録対象:

```
成功 / 失敗 / 中断 / invalid / sample不足 / 選択されなかった / synthetic run / lockbox失敗 / 再現性失敗
```

全試行数を DSR（`expected_max_sharpe(n_trials)`）・PBO・多重検定補正へ供給。**失敗trialも削除不能**（tamper検出テスト5件緑）。

## 5. lockbox

実装（`lockbox.py`）: manifest hash / dataset hash / git commit / dependency lock hash / access actor / purpose / timestamp を保存。single-use・開封後frozen・改竄検出（bundle再hash・registry全消しでも再開封不可）。

**限界（正直な申告）**: durable **local** custody。研究プロセスと同一ホスト・同一権限のため「研究プロセス自身が消せない」保証は原理的に不完全。外部custody（GitHub Actions artifact / 別アカS3 / write-only）は**未実装**（[LOCKBOX_CUSTODY runbook](runbooks/LOCKBOX_CUSTODY.md) に interface/手順）。**ローカルで完全防御とは主張しない。**

## 6. fail-closed 昇格前提条件

`experiment_pipeline.py` が正式claim前に検査（全て fail-closed、テスト緑）:
- dirty worktree 拒否 / git commit mismatch 拒否 / raw hash mismatch 拒否 / dependency lock mismatch 拒否 / 資格trade不足で selection unavailable / experiment output dir は single-use

## 7. コストストレス

`stress.py` — post-hoc 減算ではなく**エンジン再実行**でコストを乗せる（`test_stress.py`）。2x スプレッド等で期待Rが崩れないか確認（synthetic self-test では `cost_stress_2x_expectancy_r:-1.20` で当然不合格）。

## 8. deterministic replay

- 合成: `test_run_is_reproducible`（seed固定で同一結果、seed変更は新experiment ID必須）。
- **実データ実証済み**: COT audit が raw から 13,727 obs を再構成し `passed`（[evidence](../reports/evidence/cot-cftc-real-pit-20260713/README.md)）。

## 9. 完了条件に対する現状（タスク§12）

| 条件 | 状態 |
|---|---|
| 5区画分離 / purge / embargo | ✅ 構造 |
| test 単回使用 / lockbox single-use | ✅ 構造 |
| 全trial記録 / 実験横断多重性 | ✅ 構造 |
| baseline/no-trade優越 / コストストレス / CI / PBO/DSR | ✅ 構造 |
| deterministic replay | ✅ 構造＋**実データ実証** |
| **外部または独立 custody** | ❌ **未**（local のみ） |
| 実データで gate を全通過する評価bundle（合格側） | ❌ **未** |

→ **構造 70以上 / 実証 60未満**。
