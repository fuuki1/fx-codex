# research-v2 テスト証拠 — 2026-07-13

本セッションで**実際に実行**した検証の記録。「テストが緑だから性能を証明済み」とは扱わない（タスク§14）——
これは「構造が実装され、fail-closed が実挙動として効き、実データ replay が決定論的である」ことの証拠であって、
モデルが利益を出す証拠ではない。

## 環境

| 項目 | 値 |
|---|---|
| ブランチ | `integration/research-v2`（`origin/claude/pr-c-bidask-bars` #35先端 `eb4263c` + 本セッション2コミット） |
| Python | 3.12.4（CIは 3.11 / 3.12 マトリクス想定） |
| 実行場所 | 隔離worktree（Mac mini本番・他セッションworktreeには非干渉） |

## 品質ゲート（全て本セッションで実測）

| ゲート | コマンド | 結果 |
|---|---|---|
| 単体テスト | `python3 -m pytest -q` | **708 passed**（33–35s） |
| lint | `python3 -m ruff check .` | All checks passed |
| format | `python3 -m black --check .` | 163 files unchanged |
| 型（**tools/含む、タスク要件**） | `python3 -m mypy fx_backtester fx_intel data_platform tools *.py` | **Success: no issues found in 104 source files** |

> 注: 監査開始時点の #35 先端では `mypy tools` に **28エラー**（`ai_learning_dashboard/server.py`, `decision_expectancy_monitor.py`）が存在した。本セッションのコミット `3a28efd` で解消済み。修正は `# type: ignore` を使わず、`_mapping()`/`_list_of_dicts()` ヘルパーで `Any|dict|None` union を具体型へ畳む方式（欠損payloadは空へ fail-soft、挙動不変）。

## 安全不変条件（タスク§1）— 実挙動として実証

`pytest tests/test_lockbox.py tests/test_governance.py tests/test_trial_ledger.py tests/test_labeling.py tests/test_experiment_pipeline.py -v` で全緑。特に重要なもの:

| 不変条件 | テスト | 実証内容 |
|---|---|---|
| synthetic では昇格不能 | `test_experiment_pipeline.py::test_end_to_end_denies_promotion_on_synthetic_data` | 合成入力で pipeline を完走 → 昇格 denied |
| synthetic/欠損証拠は fail-closed | `test_governance.py::test_missing_or_synthetic_evidence_fails_closed` | 証拠欠損 → 不合格 |
| live 昇格は人間承認必須・自動不可 | `test_governance.py::test_registry_requires_adjacent_human_approved_promotion_and_blocks_live` | 数字が揃っても live 遷移を拒否 |
| lockbox single-use / 開封後frozen | `test_lockbox.py::{test_single_use_access,test_rerun_after_open_is_frozen}` | 2度目のアクセス拒否 |
| lockbox 改竄検出（bundle再hash・registry全消し） | `test_lockbox.py::{test_bundle_tamper_blocks_access,test_registry_wipe_cannot_reopen_an_evaluated_bundle,test_rehashed_statistics_edit_is_still_detected}` | 改竄・消去では再開封できない |
| 失敗trialも記録・理由必須・削除不能 | `test_trial_ledger.py::{test_failed_trial_requires_reason,test_edited_line_detected,test_deleted_middle_line_detected}` | append-only + hash chain の tamper 検出 |
| dirty worktree / commit mismatch は formal claim 拒否 | `test_experiment_pipeline.py::{test_dirty_worktree_rejects_formal_claim,test_commit_mismatch_is_rejected}` | 来歴不整合を fail-closed |
| raw hash / dependency lock mismatch は fail-closed | `test_experiment_pipeline.py::{test_raw_hash_mismatch_fails_closed,test_dependency_lock_mismatch_fails_closed}` | データ/依存の同一性を強制 |
| Triple Barrier: stop-first / gap-through / 次バーentry / label_end_time | `test_labeling.py`（9件） | 保守的・将来非混入・purge用終端保存 |

## 実データ証拠（本セッションで新規生成）

**初の非合成 real-data 実証。** 詳細: [reports/evidence/cot-cftc-real-pit-20260713/README.md](evidence/cot-cftc-real-pit-20260713/README.md)

| 実証項目 | 結果 |
|---|---|
| 実CFTC取込 | 13,727 FX行 / 8通貨 / report date 1986-01-15〜2026-07-07（1,926週報） |
| ページ整合性 | 全ページ SHA256 照合 OK、count-before==count-after（13,727）、fail-closed |
| deterministic replay（audit） | raw から 13,727 obs を再構成、`passed: true, errors: []` |
| **PITゲート（将来情報非混入）** | as-of `2026-07-13T12:00Z`（capture `13:06Z` の前）→ `unavailable`；as-of `2026-07-14` → `usable`（JPY net −123,778 等の実値） |
| availability正規化 | `available_time` = 実際に保持した時刻（report date ではない）。flag `availability_normalized_to_actual_use` |
| 昇格適格性 | `promotion_eligible: false`, `research_only: true`（正しく非適格） |

再現: `reports/evidence/cot-cftc-real-pit-20260713/reproduce.sh`（LIVE CFTC 再取得）。

### 実FX価格で authoritative pipeline 完走（本セッションで新規生成）

**初の実価格 pipeline E2E。** 詳細: [reports/evidence/histdata-usdjpy-real-2024-1h-20260713/README.md](evidence/histdata-usdjpy-real-2024-1h-20260713/README.md)

入力: HistData USD/JPY 2024 M1→1h（6,265本、`data/real/histdata/usdjpy_2024_1h.csv`、`raw_sha256 9e2b632d…` を manifest で固定）。

| 実証項目 | 結果 |
|---|---|
| pipeline 完走 | `status: completed`、10候補（baseline7 + logistic + ridge + **GBDT**）を train→tune→test |
| 実データ認識 | `synthetic_data: false` |
| 選択候補 | `gbdt-small`（GBDT が完全配線・競争力ありを実証） |
| OOS 期待値 | `net_expectancy_r: -0.065`（コスト控除後マイナス）、CI下限 -0.203、win_rate 34.4%、profit_factor 0.907 |
| 統計的有意性 | DSR 0.167 / PBO 0.20 / **Holm補正後 p値=全10候補 1.0**（ノイズと区別不能） |
| リーク検査 | PIT violations 0 / future-feature violations 0 |
| 昇格 | **DENIED**（net_expectancy/CI/DSR/cost_stress_2x/untouched_lockbox/pair_coverage/clean_worktree） |
| deterministic replay | 2回 run で同一 `deterministic_result_sha256 fedc9d83…`、両方 `gbdt-small` 選択 |

意義: **フレームワークが実データで偽alphaを作らないことの実証**。close-only（bid/ask無し）・単年・単一pair のため正式昇格には使用不能（`license_note` 明記）。再現: `scripts/fetch_histdata.py` + manifest（committed CSV を byte一致再現）。

## 実行していない / できない検証（正直な明示）

- **実broker bid/ask の取込テスト**：credentials 無しのため未実行（adapter は正直に `Unimplemented`=fail-closed）。
- **30取引日連続稼働**：単発のみ。launchd 常駐は開発機 TCC 制限で不可、Mac mini 本番未配備。
- **実データによる価格 triple-barrier 学習の OOS 評価**：実価格OHLCソース未接続のため未実施。
- **外部lockbox custody の実挙動**：未実装（runbook/interface のみ）。ローカル保管で完全防御できるとは主張しない。
- **CI マトリクス（3.11/3.12）・deterministic replay CI ジョブ・synthetic promotion denial CI ジョブ**：ローカルで3.12のみ実測。CI 追加は未（タスク§10の追加要件、Gap登記）。
