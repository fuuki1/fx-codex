# RESEARCH_V2 ギャップ分析 — 2026-07-13

タスク「AI学習・検証・データ基盤を70点へ」の**着手前監査**の結論。コード変更前に実測で確認した。
先行の [reports/institutional_gap_analysis_20260712.md](../../reports/institutional_gap_analysis_20260712.md)（#31基点）を継承・更新する。

## 監査アイデンティティ

| 項目 | 値 |
|---|---|
| 実施日 | 2026-07-13 JST |
| main | `3595582`（`trader/` 現存、`data_platform/` 不在） |
| 監査基点 | `origin/claude/pr-c-bidask-bars`（#35先端 `eb4263c`）を `integration/research-v2` へ |
| 実測 | pytest 708 passed / ruff / black / mypy（監査時点で tools/ に28エラー→本セッションで解消） |
| 非干渉 | Mac mini本番・他worktreeには触れていない |

## 1. 最重要の発見：要求インフラの大半は既にスタックに実装済み

タスクは「70点へ引き上げるために実装せよ」と読めるが、**実態は PR #26→#35 スタックが要求のほぼ全てを実装済み・テスト緑**。新規実装より**監査・検証・実データ実証・ドキュメント化**が本作業の価値。

| タスクが「作れ」と言う対象 | 実態（どのPR層で実装済みか） |
|---|---|
| authoritative pipeline（raw→…→evidence bundle） | #32 `experiment_pipeline.py`（2270行） |
| train/tune/calibration/test/lockbox 5分割 | #32、テスト緑 |
| Triple Barrier long/short・net R・MFE/MAE・label_end_time | #26 `labeling.py`（direction-aware、9テスト緑） |
| no-trade 正式候補・baseline群・dominance gate | #32 + #33 |
| 確率較正 Platt/isotonic/beta・drift・abstention | #26/#32 `calibration.py`/`drift.py` |
| trial ledger（失敗も記録・改竄検出）・lockbox single-use | #32 `trial_ledger.py`/`lockbox.py`（tamper検出テスト緑） |
| data_platform（PIT契約・immutable raw・quality・bid/ask bar・broker adapter） | #34/#35 |
| PSR/DSR/PBO/MTRL/block bootstrap/Holm | #26 `statistical_validation.py`/`overfitting.py` |

## 2. PRスタック依存トポロジ（実測）

```
main (3595582)  [trader/ 現存]
 └─ #26 feat/decision-pipeline-checklist (3c5bbc7)   ← trader/削除(61ファイル,-7202) + decision_pipeline + labeling/stats/calibration/overfitting
     └─ #29 codex/research-experiment-manifest        ← +pit_dataset/research_experiment（#26と1コミット分岐=CONFLICTING表示）
         └─ #31 codex/cot-pit-source-adapter          ← +cot_pit(実CFTC fetch)/CLI/briefing統合
             └─ #32 codex/institutional-research-pipeline  ← authoritative pipeline中核(experiment_pipeline/trial_ledger/lockbox/promotion_policy/shadow_execution/stress/governance)
                 ├─ #33 claude/pr-a-no-trade-candidate      ← no-trade を trial ledger の明示候補へ
                 └─ #34 claude/pr-b-data-platform-pit       ← data_platform 契約/immutable raw/quality/lineage
                     └─ #35 claude/pr-c-bidask-bars         ← bid/ask bar materializer + broker adapter + gap/divergence監査
 └─ #30 codex/hash-pinned-requirements-lock            ← main ベース独立（trader-build ジョブ含む→#26 の trader/削除と競合の可能性）
```

- **統合順序**: #26 → #29 → #31 → #32 →（#33 / #34）→ #35（stacked のため逆順不可）。#30 は #26 統合後に再検証。
- **#29 の CONFLICTING**: #26 と共通祖先 `3c5bbc7` から各1コミット分岐しているため。内容衝突ではなく分岐。

## 3. 能力分類表（タスク§0の A–G）

| 能力 | 実装 | 分類 |
|---|---|---|
| PIT契約・immutable raw・quality state | `data_platform/*`, `point_in_time.py`, `pit_dataset.py` | **C**（PR上）+ **D**（テスト済）+ **G**（実データ証拠なし、bar/quality部分） |
| Triple Barrier（long/short/net R/MFE/MAE） | `fx_backtester/labeling.py` | **C** + **D** |
| authoritative pipeline / trial ledger / lockbox | `experiment_pipeline.py` / `trial_ledger.py` / `lockbox.py` | **C** + **D**（合成E2Eのみ→**E/G**） |
| baseline dominance / no-trade | `experiment_pipeline.py` | **C** + **D** |
| 較正・drift・abstention | `calibration.py` / `drift.py` | **C** + **D** |
| PSR/DSR/PBO/MTRL/bootstrap/Holm | `statistical_validation.py` / `overfitting.py` | **C** + **D** |
| **COT PIT（実CFTC）** | `fx_intel/cot_pit.py` + `tools/cot_pit_pipeline.py` | **C** + **D** + **F**（★本セッションで実データ実証） |
| broker bid/ask adapter | `data_platform/adapters/broker.py` | **C** + **E**（Replay/Unimplementedのみ、実接続なし=**G**） |
| 現行AI分析経路（committee→briefing→Discord） | `fx_intel/committee.py`, `fx_briefing.py` | **A**（main稼働、ただし収集は本番未配備） |
| 収集 launchd 常駐 | `scripts/install_launchd.sh`, `ops/` | **B**（実装済・未配備） |
| freshness監視 | `tools/data_freshness_monitor.py`, `fx_intel/freshness.py` | **B** + **D** |

- **A（main稼働中）**: 現行の「委員会→リスクオフィサー→Discord通知」分析経路。ただし CLAUDE.md どおり収集の本番常駐は未達。
- **F（実データ証拠あり）**: 本セッション時点で **COT PIT のみ**。

## 4. 現行 AI 分析経路 vs 研究パイプライン経路の違い

| | 現行分析経路（main稼働） | authoritative research pipeline（スタック） |
|---|---|---|
| 入口 | `fx_briefing.py`（毎時） | `experiment_pipeline.py`（実験単位） |
| 目的 | Discord通知用 TradePlan | 正式モデル artifact + 昇格判定 |
| 学習 | `learning.py` の重み再推定（オンライン的） | train/tune/calibration/test/lockbox の一括 |
| 昇格 | `promotion.py`（shadow→paper→live 状態機械） | `promotion_policy.py` + `governance.py`（証拠bundle駆動） |
| 正式性 | 運用判断（通知）専用 | **正式性能主張はこちらのみ**（タスク§2） |

→ タスク§2の要求「正式性能主張と昇格は authoritative pipeline のみ」は構造上満たされている。旧 `ml.py`（同一validation setを early stopping/較正/Brier に使い回す）は補助・互換用途に留まっており、正式artifact生成には使われない。

## 5. 70点に対する具体的ギャップ（何が足りないか）

構造ギャップは小さく、**実証ギャップが大きい**。詳細は [reports/research_v2_unresolved_risks.md](../../reports/research_v2_unresolved_risks.md)。

| 軸 | 構造ギャップ（小） | 実証ギャップ（大） |
|---|---|---|
| AI学習 | GBDT を pipeline 候補family へ登録／階層型学習の pipeline 実装 | 実価格 triple-barrier の OOS 証拠（P0-2） |
| 検証 | 外部custody 実装／concentration の正式ゲート化 | 実データで gate を全通過する評価bundle（合格側の実証） |
| データ基盤 | — | broker bid/ask 実接続（P1-1）／30取引日連続運用（P0-3）／macro/calendar/news 実PIT |

## 6. 本セッションで解消したギャップ

1. **mypy tools/ 28エラー解消**（コミット `3a28efd`）— タスク§10の `mypy ... tools` 要件を実際に満たした。
2. **COT PIT 実データ実証**（コミット `e8976f5`）— 初の非合成 real-data 証拠。データ基盤とvalidationの実証軸をわずかに押し上げた。
3. **監査・スコア・設計ドキュメント一式**（本コミット群）— タスク§11 の未作成ドキュメントを整備。

## 7. 結論

構造面は「既存スタックが要求のほぼ全てを実装済み・708テスト緑」。実証面は COT PIT を除き未達。
**判定2**（構造≒70完成、実証不足で正式未宣言）。統合順序と移行手順は [MAC_MINI_RESEARCH_V2_MIGRATION.md](../runbooks/MAC_MINI_RESEARCH_V2_MIGRATION.md)。
