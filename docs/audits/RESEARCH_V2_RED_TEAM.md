# RESEARCH_V2 レッドチームレビュー — 2026-07-13

「この基盤の主張を、敵対的に壊しにいく」視点の監査。先行 [reports/INDEPENDENT_RED_TEAM_REVIEW_20260712.md](../../reports/INDEPENDENT_RED_TEAM_REVIEW_20260712.md) を継承し、本セッションで**実際にコードを読んで/実行して**確認した結果を記す。

各項目: **攻撃仮説 → 実際に確認したコード/挙動 → 判定**。

## A. 安全不変条件を破れるか（最重要）

### A-1: synthetic データを昇格させられるか
- **攻撃**: 合成データで良い数字を作り、`synthetic_data` フラグを誤魔化して昇格させる。
- **確認**: `governance.py:153-157` の gate は `evidence.synthetic_data is False`（**厳密に False**）を要求。`None`（未指定）・欠損・`True` は全て不合格。合成E2E `test_end_to_end_denies_promotion_on_synthetic_data` が `failures` に `non_synthetic_data` を含めて denied を実証。
- **判定**: ✅ 破れない。フラグ未指定でも fail-closed。

### A-2: live 取引を有効化できるか
- **攻撃**: 十分な数字＋承認文字列を与えて live へ昇格させる。
- **確認**: `governance.py:365-366` — `target_stage in {"limited_live","live"}` なら**無条件で** `GovernanceError("this registry build cannot enable live trading")`。証拠が揃っても承認があっても live へ遷移しない（コードレベルの hard disable）。加えて `promotion_policy.py` の build は `allow_live`/`allow_limited_live` を true にする policy を拒否。さらに一段階ずつ＋人間承認者＋理由が必須（`:363,:369`）。
- **判定**: ✅ 多層で破れない。`ALLOW_LIVE` 相当は research pipeline 側に存在しない。

### A-3: 欠損値を0で埋めて昇格を通せるか
- **攻撃**: 期待値・Sharpe・サンプル数が計算不能な時、0扱いで gate を通す。
- **確認**: `experiment_pipeline.py` の expectancy/sharpe/sample 経路に `or 0` フォールバック無し（grep 実測で0件）。`governance.py` の require は `None` を「未達」として扱い、統計不能→unavailable→昇格失敗（`test_governance.py::test_missing_or_synthetic_evidence_fails_closed`）。
- **判定**: ✅ 破れない。ただし §C-2 の注意あり。

### A-4: lockbox を再利用・改竄できるか
- **攻撃**: test/lockbox を見た後に再最適化する、または評価済み bundle を消して再開封する。
- **確認**: `test_lockbox.py` の8+6件が single-use・開封後frozen・bundle再hash改竄検出・**registry全消しでも再開封不可**（`test_registry_wipe_cannot_reopen_an_evaluated_bundle`）・rehash後の統計改竄検出を実証。
- **判定**: ✅ ローカル範囲では破れない。**ただし §D の外部custody限界**。

## B. データの将来情報混入（PIT）を破れるか

### B-1: 取得前のデータを判断に使えるか
- **攻撃**: 今日取得したデータを、過去の予測時点で「既知だった」ことにする。
- **確認（実データで実行）**: COT を今日 13:06 に取得 → as-of `2026-07-13T12:00Z`（13:06の前）で `unavailable`。`available_time` は capture/attestation 実時刻へ正規化（report date ではない）。§[evidence bundle](../../reports/evidence/cot-cftc-real-pit-20260713/README.md)。
- **判定**: ✅ 実データで破れないことを実証。

### B-2: revision を初回公開値へ遡及上書きできるか
- **攻撃**: 改定値で過去を書き換え、当時知り得なかった値を使う。
- **確認**: `cot_pit.py` は改定を別 revision として保存（flag `revision_detection_limited_to_stable_cftc_row_id`）。ただし**限界を自己申告**：CFTC の stable row id に依存した検出であり、row id が変わる改定は取りこぼす可能性。
- **判定**: ⚠️ 設計は正しいが検出は限定的。過大主張していない点は良い。

## C. 統計・評価を欺けるか

### C-1: 多重検定を隠して見かけの有意性を出せるか
- **攻撃**: 多数の trial を回し、当たった1つだけ報告する。
- **確認**: `trial_ledger.py` は append-only + hash chain で**失敗trialも削除不能**（tamper検出テスト緑）。DSR は `expected_max_sharpe(n_trials, ...)` で試行数を反映、PBO は CSCV。
- **判定**: ✅ 構造上、隠せば改竄検出。ただし **ledger に記録する規律が運用者に依存**（記録し忘れた trial は補正に入らない）。

### C-2: 単一レジーム/単一月に集中したデータで良く見せられるか
- **攻撃**: 特定の相場つき/月に偏った期間で評価。
- **確認**: `fold_dispersion`/`rank_stability` 等の部品は存在するが、**pipeline 出力の正式ゲートとしての結線が部分的**（§gap P2-3）。concentration が昇格を止める強制力が弱い。
- **判定**: ⚠️ 部品はあるがゲート化未完。実データ評価で悪用余地が残る。

## D. lockbox custody の限界（正直な自己申告）

- **限界**: lockbox は **durable local custody**。研究実行プロセスと同一ホスト・同一権限で動くため、「研究プロセス自身が証拠を消せない」保証が原理的に不完全。materialize 時の warning も「release sidecars are locally bound, not externally signed or independently timed」と明示。
- **タスク要求**: GitHub Actions artifact / 別アカS3 / write-only 外部ストレージのいずれか。
- **現状**: 未実装。[LOCKBOX_CUSTODY runbook](../runbooks/LOCKBOX_CUSTODY.md) に interface/手順を設計したが実装は未。
- **判定**: ❌ 未達。**「ローカル保管で完全防御」とは主張しない**（タスク§4の指示を遵守）。

## E. 「機関投資家級」の過大主張リスク

- **確認**: 既存ドキュメント（`INSTITUTIONAL_*`）や evidence bundle は `research_only`/`promotion_eligible:false` を一貫して付与。合成benchmark（`reports/institutional_benchmark_20260711.md`）は「性能主張に使用不能」と明記。
- **残リスク**: ファイル名・見出しに "institutional" を多用しており、**文脈を切り取ると過大に読める**。本監査の [current_score](../../reports/research_v2_current_score.md) は判定2を明示してこれを打ち消す。
- **判定**: ⚠️ コード/証拠は正直。命名が誤読を招きうる。

## F. 統合そのもののリスク

- **F-1**: `integration/research-v2` を main へ入れると `trader/` が消える（-7202行）。レビューなし一括統合は禁止（タスク§14）。→ 段階レビュー＋承認＋rescueブランチ（[migration runbook](../runbooks/MAC_MINI_RESEARCH_V2_MIGRATION.md)）。
- **F-2**: 31k行・237ファイルの巨大差分。CI緑だけを根拠に「性能証明済み」としてはならない（タスク§14）。本監査は性能未証明を明示。

## レッドチーム総括

| 領域 | 破れたか | 備考 |
|---|---|---|
| synthetic 昇格 | ❌ 破れない | 厳密 `is False`、フラグ未指定でも fail-closed |
| live 有効化 | ❌ 破れない | コードレベル hard disable + 多層 |
| 欠損0埋め昇格 | ❌ 破れない | `or 0` フォールバック無し |
| lockbox 再利用/改竄 | ❌（ローカル範囲） | 外部custodyは未実装＝限界を自己申告 |
| PIT 将来情報混入 | ❌ 破れない（実データ実証） | revision検出は限定的 |
| 多重検定隠蔽 | ❌（記録すれば） | ledger記録の運用規律に依存 |
| concentration悪用 | ⚠️ 余地あり | ゲート化未完（P2-3） |
| 過大主張 | ⚠️ 命名リスク | 証拠は正直、判定2で打ち消し |

**結論**: 安全・PIT・改竄耐性の核は敵対的に見ても堅牢。弱点は (1) 外部custody未実装、(2) concentrationゲート化未完、(3) 実データ評価の合格側が未実証、(4) "institutional" 命名の誤読リスク。いずれも [unresolved_risks](../../reports/research_v2_unresolved_risks.md) に登記済み。
