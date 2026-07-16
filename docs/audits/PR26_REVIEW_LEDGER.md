# PR #26 レビュー台帳(領域別独立レビュー)

対象: PR #26 `feat/decision-pipeline-checklist`(head `3c5bbc7`、mainとの差分171 files)。
stacked後続の #29(`95f5807`)・#31(`0fce3fa`)の追加差分も末尾に記載する。

**方針**: 171 filesの単一PRはそのままでは安全にレビューできないため、変更を9領域へ分類し、
領域ごとに独立レビュー可能な台帳を作る。既存commitの書き換え(rebase分割)は、
別セッションが同branchを編集中である事実と、監査文書が「published historyを書き換えない」
と定めていることから**行わない**。マージは人間のdiffレビュー完了が条件であり、
この台帳はそれを置換しない。

各領域の共通ロールバック: 領域単位のrevertは不可能(単一branch)。ロールバックは
(1) mainへのマージ前なら「マージしない」、(2) マージ後は `git revert -m 1 <merge-commit>`
でPR全体を戻す。部分的な巻き戻しが必要な場合は該当ファイルのみを対象とした
follow-up PRを作る。

---

## A. PIT・データ契約

- **目的**: aware UTC・availability/ingestion/revision分離・将来as-of拒否・canonical hash。
- **変更ファイル**: `fx_backtester/point_in_time.py`(A)、`fx_backtester/pit_dataset.py`(A, #29)、`fx_intel/cot_pit.py`(A, #31)、`docs/research/SOURCE_LEDGER.md`(A)。
- **入力**: 生データbytes、source宣言、時刻metadata。
- **出力**: `PointInTimeRecord`、content-addressed dataset artifact、typed as-of join結果。
- **不変条件**: `available_at >= max(publication, revision, ingestion, validation)`。将来as-of拒否。raw hash一致。等価availabilityの曖昧keyは拒否。
- **失敗時挙動**: `PointInTimeError` / `PITDatasetError` で停止(fail-closed)。
- **テスト**: `tests/test_point_in_time.py`、`tests/test_pit_dataset.py`、`tests/test_cot_pit*.py`(35)。
- **既知の限界**: feature graph全体のas-of証明はない。COT以外のsource adapterは未接続。release evidenceはlocal。
- **独立レビュー結果**: 監査文書のData/macro/PITレビューおよびPIT artifact red teamで反証→fail-closedテスト化済み。source statusは`declared_verified`へ弱体化済み。**人間の最終diffレビューは未了**。

## B. ラベル・Triple-barrier・MFE/MAE

- **目的**: 次バーオープンentry・stop-first・gap-through・first-touch・MFE/MAE cutoff・cost控除後R。
- **変更ファイル**: `fx_backtester/labeling.py`(A)。
- **入力**: OHLC bars、barrier設定、コスト。
- **出力**: `TripleBarrierLabel`(label_end_time付き)。
- **不変条件**: TP/SL同時touchはstop-firstまたはambiguityフラグ付き破棄。forming barを事後経路として扱わない。
- **失敗時挙動**: 入力不足・不整合は`_unavailable_label`(欠測明示)または例外。
- **テスト**: `tests/test_labeling.py`。
- **既知の限界**: 実PIT labelコーパスなし。financingは既知の場合のみ控除。
- **独立レビュー結果**: Quant/risk/MLレビュー通過(反証はfail-closed化)。人間diffレビュー未了。

## C. walk-forward・purging・embargo・CPCV

- **目的**: ラベル期間重複の排除、anchored/rolling WF、CPCV-like fold、5分割chronological partitions。
- **変更ファイル**: `fx_backtester/time_series_validation.py`(A)、`fx_backtester/walk_forward.py`(M)。
- **不変条件**: 訓練ラベル終端 < 評価窓開始 − embargo。test fold重複は集約主張では拒否。
- **失敗時挙動**: `TemporalLeakageError`。
- **テスト**: `tests/test_time_series_validation.py`、`tests/test_walk_forward.py`。
- **既知の限界**: orchestratorが存在せず、正しく呼ぶ責任は呼び出し側にある(→本作業G1で解消対象)。
- **独立レビュー結果**: 反証なし。人間diffレビュー未了。

## D. PSR・DSR・PBO・bootstrap・多重検定

- **変更ファイル**: `fx_backtester/statistical_validation.py`(A)、`fx_backtester/overfitting.py`(M)、`fx_backtester/trial_log.py`(M)。
- **不変条件**: PBOは欠測を0埋めせず拒否。DSRは全試行数を入力に取る。非有限は拒否。
- **失敗時挙動**: `ValueError`(evaluation unavailable)。
- **テスト**: `tests/test_statistical_validation.py`、`tests/test_overfitting.py`、`tests/test_trial_log.py`。
- **既知の限界**: trial_logは揮発性バッファで、試行の過少申告を構造的に防げない(→G2)。
- **独立レビュー結果**: Quant/risk/MLレビューで「skipped PBO/DSR」欠陥→fail-closed化済み。

## E. calibration・abstention・drift

- **変更ファイル**: `fx_backtester/calibration.py`(A)、`fx_backtester/drift.py`(A)、`fx_intel/ml.py`(M)。
- **不変条件**: 較正は専用partitionでのみfit。schema不一致・未熟labelはabstain/human_review。ML schema v3はBrier/log-loss改善+AUC≥0.55必須。
- **失敗時挙動**: `CalibrationError`、drift veto。
- **テスト**: `tests/test_calibration.py`、`tests/test_drift.py`、`tests/test_ml*.py`。
- **既知の限界**: 実データreliability証跡なし。しきい値は実shadow証跡で再推定が必要。
- **独立レビュー結果**: prevalence-shift usability反証→修正済み。

## F. model registry・promotion gate

- **変更ファイル**: `fx_backtester/governance.py`(A)、`fx_intel/promotion.py`(M)、`fx_intel/committee.py`(M)、`docs/MODEL_GOVERNANCE.md`(A)。
- **不変条件**: 欠損証拠=不合格。隣接stage遷移のみ。`limited_live`/`live`はregistry buildで拒否。承認者・理由必須。
- **失敗時挙動**: `GovernanceError`。
- **テスト**: `tests/test_governance.py`、`tests/test_promotion.py`、`tests/test_committee.py`。
- **既知の限界**: PromotionPolicyがコード既定値(→G5)。evidenceは呼び出し側供給で独立検証と未結合。
- **独立レビュー結果**: 非独立legacy自動昇格の反証→legacy委員は非影響shadow固定へ。

## G. portfolio risk・order simulation

- **変更ファイル**: `fx_backtester/risk.py`(M)、`fx_backtester/engine.py`(M)、`fx_backtester/execution.py`(M)、`fx_backtester/metrics.py`(M)、`fx_backtester/stress.py`(A)。
- **不変条件**: レバレッジはmark-to-marketでlatch。pending注文TTL。per-symbol最終bar close。cost stressはengine全再実行。
- **テスト**: `tests/test_risk.py`、`tests/test_backtester.py`、`tests/test_stress.py`。
- **既知の限界**: mid+静的proxy。歴史的bid/ask・depth・latency・reject・partial fillなし(→G8はschemaのみ実装、fillは主張しない)。
- **独立レビュー結果**: entry-onlyレバレッジ・stale pending・early-ending symbolの反証→修正済み。

## H. single-writer・監視・runbook

- **変更ファイル**: `scripts/*.sh`(M)、`fx_intel/freshness.py`(A)、`fx_intel/signal_board.py`(A)、`tools/data_freshness_monitor.py`ほか、`docs/OPERATIONS_RUNBOOK.md`(M)、`docs/FX_AI_OPERATIONS.md`(M)。
- **不変条件**: 鮮度veto(missing/malformed/stale/future=critical)。writer検出でinstall拒否。`--no-notify`は通知状態を消費しない。
- **失敗時挙動**: veto・nonzero exit。
- **テスト**: `tests/test_freshness*.py`、`tests/test_ops_scripts.py`(zsh依存はCIでskip)。
- **既知の限界**: Mac mini未配備。journal横断の排他はprice snapshotのみ。
- **独立レビュー結果**: Repository/operationsレビュー2巡(rollback二重writer・git add -A等)→修正済み。**実機移行は人間承認待ち**。

## I. CI・依存関係・文書・Codex Skills

- **変更ファイル**: `.github/workflows/ci.yml`(M)、`AGENTS.md`(A)、`CLAUDE.md`(M)、`README.md`/`SYSTEM_OVERVIEW.md`(M)、`docs/INSTITUTIONAL_*.md`(A)、`.codex/skills/*`(A)、削除: `auto_optimize.py`・`params_gate.py`・`promote_params.py`・`trader/`全体。
- **不変条件**: trader/再作成禁止。mainへ直接push禁止。
- **既知の限界**: `10d6cbe`のcommit件名がscopeを過少表現(監査文書High-7)。**このcommitは特に件名でなくdiffをレビューすること**。#30(mainベース)のCIはtraderジョブを含み、#26統合後に競合する。
- **独立レビュー結果**: 監査文書で指摘済み。人間diffレビュー未了。

---

## #29 追加差分(#26 → #29)

`pit_dataset.py`・`research_experiment.py`・`docs/RESEARCH_PROTOCOL.md`更新・テスト。
領域A/D/Fに属する。experiment binder red team実施済み(監査文書)。

## #31 追加差分(#29 → #31)

`fx_intel/cot_pit.py`・`tools/cot_pit_pipeline.py`・briefing統合・テスト35件。
領域Aに属する。COT PIT adapter red team実施済み(監査文書)。

## 安全な統合手順(人間実施、自動マージ禁止)

```bash
# 0. 前提: メインcheckoutの未コミット変更を先に確定またはstashする(別セッションと調整)
git -C /Users/takahashifuuki/Desktop/fx-codex status   # dirtyなら停止して調整

# 1. #26 を人間レビュー(特に 10d6cbe は件名でなく diff)→ GitHub上でマージ
gh pr diff 26 | less        # 実diffレビュー
gh pr merge 26 --merge      # 承認後のみ。--admin/--auto は使わない

# 2. #29 を新mainへrebaseしてCI再実行→レビュー→マージ
git switch codex/research-experiment-manifest
git fetch origin && git rebase origin/main
git push --force-with-lease origin codex/research-experiment-manifest
gh pr checks 29 --watch && gh pr merge 29 --merge

# 3. #31 も同様
git switch codex/cot-pit-source-adapter
git rebase origin/main
git push --force-with-lease origin codex/cot-pit-source-adapter
gh pr checks 31 --watch && gh pr merge 31 --merge

# 4. #30 は最後: traderジョブ削除後のci.ymlと整合させ、lockを再生成してレビュー
git switch codex/hash-pinned-requirements-lock
git rebase origin/main   # conflictが出たら requirements.lock を再生成して解消
```

各段階でconflict・CI赤が出たら**停止して原因を記録**し、推測で継続しない。
