# Institutional gap analysis — 2026-07-12

## 監査アイデンティティ

| 項目 | 証拠 |
|---|---|
| 実施日 | 2026-07-12 JST |
| 基点ブランチ | `codex/institutional-research-pipeline`(`codex/cot-pit-source-adapter` = PR #31先端 `0fce3fa` から分岐) |
| 依存トポロジ | `main (3595582)` ← PR #26 (`feat/decision-pipeline-checklist`, `3c5bbc7`) ← PR #29 (`codex/research-experiment-manifest`, `95f5807`) ← PR #31 (`codex/cot-pit-source-adapter`, `0fce3fa`)。stacked構成を`git merge-base --is-ancestor`で確認 |
| PR #30 | `codex/hash-pinned-requirements-lock`。mainベースで独立。CI緑(test 3.11/3.12, trader-build-image, trader-test) |
| CI | #26/#29/#30/#31 すべて現行headでpass |
| ベースライン品質ゲート(本worktreeで再実行) | `pytest -q`: **558 passed, 1 skipped**; `ruff check .`: pass; `black --check .`: 126 files unchanged; `mypy fx_backtester fx_intel *.py tools/cot_pit_pipeline.py`: 68 files, no issues |
| 並行作業 | メイン checkout (`/Users/takahashifuuki/Desktop/fx-codex`) は別セッションが `feat/decision-pipeline-checklist` 上で**現在進行形で編集中**(dirty ファイルが監査中に増加)。本作業は隔離worktreeで実施し、メインcheckoutには一切触れていない |

この文書は工程・実装ギャップの監査であり、収益性や投資助言ではない。

## 1. 現在実装済みの能力(単体テスト済み)

| 能力 | 実装 | テスト |
|---|---|---|
| PITレコード契約(availability正規化・将来as-of拒否・canonical hash) | `fx_backtester/point_in_time.py` | `tests/test_point_in_time.py` |
| content-addressed生データ保存・改竄検出・完全再構成 | `fx_backtester/pit_dataset.py` | `tests/test_pit_dataset.py` |
| Triple-barrier(stop-first・gap-through・MFE/MAE・net R・`label_end_time`) | `fx_backtester/labeling.py` | `tests/test_labeling.py` |
| purge/embargo付きwalk-forward・CPCV-like・5分割chronological partitions | `fx_backtester/time_series_validation.py` | `tests/test_time_series_validation.py` |
| PSR / MTRL / circular block bootstrap CI / block sign permutation / Holm | `fx_backtester/statistical_validation.py` | `tests/test_statistical_validation.py` |
| PBO(CSCV)/ DSR(欠測行列は拒否=fail-closed) | `fx_backtester/overfitting.py` | `tests/test_overfitting.py` |
| 確率較正(Platt/isotonic/beta)+abstention policy | `fx_backtester/calibration.py` | `tests/test_calibration.py` |
| drift監視(PSI/KS/W距離、schema不一致はabstain) | `fx_backtester/drift.py` | `tests/test_drift.py` |
| 昇格ゲート・model registry・hard veto(欠損証拠=不合格、live遷移拒否) | `fx_backtester/governance.py` | `tests/test_governance.py` |
| 事前計算証拠binder+実験ID排他claim(local lockbox) | `fx_backtester/research_experiment.py` | `tests/test_research_experiment.py` |
| CFTC COT PIT adapter(raw replay・revision・typed as-of) | `fx_intel/cot_pit.py` + `tools/cot_pit_pipeline.py` | `tests/test_cot_pit*.py`(35件) |
| コストストレス全再実行(post-hoc減算ではなくengine再実行) | `fx_backtester/stress.py` | `tests/test_stress.py` |
| 次バーオープン約定・spread/slippage/commission・TTL失効 | `fx_backtester/execution.py` / `engine.py` | `tests/test_backtester.py` |
| リスク層(レバレッジlatch・exposure・drawdown) | `fx_backtester/risk.py` | `tests/test_risk.py` |
| 鮮度veto・freshness監視 | `fx_intel/freshness.py` / `tools/data_freshness_monitor.py` | `tests/test_freshness*.py` |
| 実行run artifact manifest(hash・git provenance) | `fx_backtester/artifacts.py` | `tests/test_artifacts.py` |
| 最適化試行ログ(run単位書き出し) | `fx_backtester/trial_log.py` | `tests/test_trial_log.py` |

## 2. 実装済みだが未配備の能力

- single-writer launchd stack(`scripts/install_launchd.sh` ほか)— Mac mini未配備。
- 鮮度監視・Discord直接通知 — ローカルテストのみ。
- COT PIT pipeline — fake-sessionテストのみで実corpus・ライセンス・外部attestationなし。
- hash-pinned `requirements.lock`(PR #30)— 本branch系列には未反映(mainベース)。

## 3. テストのみで実データ証跡がない能力

- `research_experiment.py` の lockbox claim / 評価経路(合成入力のみ)。
- `pit_dataset.py` の监査経路(fixtureのみ)。
- benchmark(`reports/institutional_benchmark_20260711.md`)は `examples/sample_prices.csv`(合成、seed 42)であり、**性能主張には使用不能**。
- promotion gates は一度も真の証拠で駆動されていない(全フィールド `None` → 全ゲート不合格が正しい動作)。

## 4. 存在しない能力(本作業の実装対象)

| # | 欠落 | 現状の最接近実装と不足 |
|---|---|---|
| G1 | **単一のauthoritative pipeline**(raw→PIT→品質→feature as-of→label→split→train→calibration→selection→lockbox→cost stress→promotion→evidence bundle を1つのentrypointが所有) | `research_experiment.py` は事前計算入力のbinderで、trainer・feature join・label生成・cost rerunの呼び出しグラフを所有しない(監査文書のExit criteria #4に対応) |
| G2 | **append-only trial ledger**(全試行・失敗も記録・上書き禁止・改竄検出・全試行数をDSR/PBOへ供給) | `trial_log.py` はプロセス内バッファの一括書き出し。追記排他・hash連鎖・削除検出・失敗試行の契約がない |
| G3 | **永続lockbox境界**(dataset ID/hash固定・アクセス台帳・アクセス後manifest変更拒否・再最適化拒否) | `research_experiment.py` のclaim storeは実験ID単位のlocal排他のみ。dataset単位のアクセス回数・理由台帳がない |
| G4 | **baseline群runner**(no-skill/random/always-long/short/prev-sign/MA/RSI/logistic/Ridge/GBDT を同一条件比較) | `cli.py backtest` に個別戦略はあるが、統一比較・sample-size guard・「単純baselineを上回る場合のみ候補」の強制がない |
| G5 | **promotion policy の設定ファイル化** | `PromotionPolicy` はdataclass既定値。設定ファイル(JSON)からのロード・rationale必須・stage別条件の宣言がない |
| G6 | **source adapter共通契約**(broker quote / macro / calendar / news / scanner schema、typed failure) | `cot_pit.py` はCOT固有。共通interface・typed failure enum・未実装sourceの明示がない |
| G7 | **data-quality SLO測定**(freshness/completeness/duplicate/late/out-of-order/revision/clock skew/divergence/schema violation) | `evaluate_price_quality` は価格のみ・部分的 |
| G8 | **shadow order intent / execution event / TCA schema** + mock/replay adapter | 存在しない(旧trader/は削除済み。再作成はしない — broker送信なしのintent/TCAのみ) |
| G9 | **strategy card契約**(economic mechanism・who_is_paying_the_edge・pre-registered metrics) | `RESEARCH_PROTOCOL.md` に文書要件はあるがコード上の契約・検証がない |
| G10 | **Mac mini single-writer移行runbook**(専用手順+障害注入) | `docs/OPERATIONS_RUNBOOK.md` に断片。`docs/runbooks/MAC_MINI_SINGLE_WRITER_MIGRATION.md` は不在 |
| G11 | **typed failure分類の統一**(unavailable/invalid/incomplete/stale/…/promotion_rejected) | 各moduleに散在する例外はあるが横断enumがない |

## 5. PR #26/#29/#30/#31 の依存関係

```
main (3595582)
 ├─ #26 feat/decision-pipeline-checklist (3c5bbc7)   ← 基盤。171 files、trader/削除を含む
 │    └─ #29 codex/research-experiment-manifest (95f5807)  ← +pit_dataset/research_experiment
 │         └─ #31 codex/cot-pit-source-adapter (0fce3fa)   ← +cot_pit/CLI/briefing統合
 └─ #30 codex/hash-pinned-requirements-lock             ← 独立。mainベース
```

- **統合順序は #26 → #29 → #31**(stackedのため逆順不可)。#29/#31は#26統合後にrebaseまたはそのままfast-forward系mergeが可能。
- **#30はmainベース**のため、#26統合後にconflict確認が必要(`requirements.lock`は#26系列にも存在)。#30のCIにはtrader-buildジョブが含まれる=#26のtrader/削除とworkflowが競合する可能性が高い。#26統合後に#30を再検証すること。
- 本branch(`codex/institutional-research-pipeline`)は#31先端から分岐したstacked第4層。

## 6. 重複実装・競合・削除候補

| 対象 | 状況 | 判断 |
|---|---|---|
| `fx_intel/promotion.py` vs `fx_backtester/governance.py` | 前者はlegacy委員(shadow固定)の段階管理、後者はモデルregistry。用語が重複するが役割は別 | 共存維持。新promotionエンジンは`governance.py`を拡張し、`fx_intel/promotion.py`は非影響shadow専用として温存 |
| `trial_log.py` vs 新trial ledger | 前者はrun内バッファ。append-only性がない | 置換せず併存: `trial_log.py`はPBO入力行列生成に既に配線済み。新ledgerは横断的な永続台帳として追加し、run書き出しを取り込む |
| `artifacts.py` manifest / `research_experiment.py` manifest / 新experiment manifest | 3種のmanifestが併存 | 新manifestは既存2種を置換しない。authoritative pipelineの実行宣言として上位に置き、lineageで両者のhashを参照する |
| 旧 `wip/parallel-work-snapshot-20260708` 等の古branch | 棚卸し対象 | 本作業のscope外。削除しない |

## 7. main統合時の危険箇所

1. **#26は171 files**。`trader/`削除(約9,000行)と`fx_intel`広範改変を含む。コミット件名`10d6cbe`は「テスト修正」だが実diffは広い(監査文書High-7で指摘済み)。**subject名からscopeを推測せずdiff実レビュー必須**。
2. **メインcheckoutのdirty変更**(別セッションが`fx_backtester/point_in_time.py`・`fx_intel/*`を編集中)は#26 branchに未コミット。この変更が先にコミットされると本branch系列とのconflictが発生し得る。統合前に`git status`/HEAD再確認必須。
3. **CI workflow**: #26は`ci.yml`からtraderジョブを削除している一方、#30(mainベース)はtraderジョブを前提にCIが走っている。#26統合後、#30はrebase+lock再生成が必要。
4. **週末依存テスト**は`10d6cbe`で修正済み(本監査で土曜日にpytest全緑を確認)。
5. `runs/` / `logs/` はgit excludeされたruntime data。統合作業でこれらに触れないこと。

## 8. 残存リスク(P0/P1/P2)

### P0(昇格を直接阻止する)
- **実データが存在しない**: promotion-admissibleな価格(bid/ask)・macro・calendarのPIT corpusが未取得。COTすら実corpus未取得。→ いかなる実装でも「モデル性能評価可能」には**実データ収集期間**が必要。
- **authoritative pipelineの不在**(G1)。
- **trial ledgerの不在**(G2): 現状では試行の過少申告を検出できない。

### P1(統制の穴)
- lockboxのdataset単位アクセス台帳不在(G3)。
- promotion policyのhard-coded既定値(G5)。
- data-quality SLOの未測定(G7)。
- `research_experiment.py`のclaim storeがglobal custodyでない(既知・文書化済み)。
- Mac mini運用状態(重複writer・stale data)の未解消 — 移行runbook実行は人間承認待ち。

### P2(受容可能・後続課題)
- COT release evidenceの外部attestation・ライセンス。
- FRED現行値CSV(revision履歴なし)の置換。
- `.venv`のpip内部エラー(監査文書Medium-3)。
- 深いレジーム分割の未定義。

## 9. 監査成熟度12軸の再採点(2026-07-12時点、本作業実施前)

採点基準: 0=存在しない / 1=設計・断片のみ / 2=実装・単体テスト済み / 3=main統合・実データ・再現可能な証跡あり / 4=独立検証・shadow/paper実績・障害試験あり / 5=長期運用・変更統制・複数レジーム実績。

**注意**: 既存監査(1.83/5)の数字はコード存在ベースで妥当。ただし全軸とも**mainに未統合**(stacked PRのみ)のため、本基準では3に到達し得ない。実データ証跡もゼロ。

| 軸 | 採点 | 証拠 | 3へ上げる条件 |
|---|---:|---|---|
| Data integrity | 2 | `pit_dataset.py`+35 COTテスト。実corpusなし | main統合+実データingestion稼働 |
| PIT integrity | 2 | `point_in_time.py`のas-of join・将来拒否テスト。feature graph全体の証明なし | authoritative pipelineでのfeature as-of所有+実データ |
| Label quality | 2 | triple-barrierテスト。実PIT labelコーパスなし | 実データでのlabel生成証跡 |
| Validation rigor | 2 | purge/embargo/CPCV/5分割テスト。orchestrationなし | G1解消+実データ実験1本 |
| **Model performance** | **0** | **評価不能**。合成4戦略中3負、唯一の正は11 trades・CI跨零・1.5×で負 | 実データ+本作業の評価基盤+十分サンプル |
| Probability calibration | 2 | 較正split・Brier/log-lossゲート。実較正証跡なし | 実データreliability curve |
| Execution reproducibility | 2 | 決定論的fill・TTL・cost stressテスト。broker/venue replayなし | shadow intent+実quote照合 |
| Risk management | 2 | veto・latch・exposureテスト。実運用照合なし | shadow実績30日 |
| Reproducibility | 2 | manifest・hash・seed。依存hash未固定(#30未統合)・dirty経緯 | #30統合+同一manifest同一hashのCIテスト |
| Monitoring | 2 | freshness veto・drift。Mac mini未配備 | runbook実行(人間承認後) |
| Governance | 2 | ゲート・registry・claim store。append-only ledger・アクセス台帳なし | G2/G3解消+独立レビュー |
| Operational safety | 2 | single-writer設計・fail-closed script。未配備 | 移行実施+障害注入試験 |

**unweighted 1.83/5(既存監査と一致)**。本作業(G1〜G11)はコード面で3への**構造**を作るが、3の宣言には (a) main統合、(b) 実データ証跡、の2つが人間の作業として必要。**コードを書いただけでは2のまま**である。

## 10. 採点の証拠

- テスト: 本worktreeで `pytest -q` → 558 passed, 1 skipped(2026-07-12、土曜日に実行し週末依存なしを確認)。
- 品質: ruff / black / mypy(68 files)すべてpass。
- CI: PR #26 run `29135404071`、#29 `29146673480`、#30 `29156577877`、#31 `29157699125` すべてpass。
- 文書: `docs/audits/INSTITUTIONAL_READINESS_AUDIT.md`(evidence freeze 2026-07-11)、`reports/institutional_benchmark_20260711.md`(dataset SHA-256 `b93513ba…8427`)。
- 性能: **alphaは確認されていない**。合成benchmarkは機能検証のみで、性能検証は「評価不能」。この結論は本監査でも変わらない。
