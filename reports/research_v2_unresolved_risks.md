# research-v2 残存リスク — 2026-07-13

P0 = 実運用・安全に直結 / P1 = 正式到達に必須 / P2 = 品質・完成度。
各リスクは「なぜ問題か」「現状」「解消に必要なこと」を記す。

## P0（安全・実運用に直結）

### P0-1: スタック統合は `trader/` を丸ごと削除する
- **現状**: `main`（`3595582`）には `trader/`（executor/webhook/risk 等 61ファイル）が現存。PR #26 がこれを全削除（7202行削除）し、その上の #29→#35 も削除を継承。
- **なぜ問題か**: `integration/research-v2` を main にマージすると発注スタックが消える。IBKR未開設で executor は稼働不能なため機能影響は無いが、**大きな安全関連の削除**であり、黙って実行してはならない（タスク§14「Mac mini本番を書き換える」「巨大PRをレビューなしで統合」の禁止に該当）。
- **解消に必要**: 統合前にユーザーの明示承認。`trader/` を rescue ブランチへ退避してから main を進める（[runbook](../docs/runbooks/MAC_MINI_RESEARCH_V2_MIGRATION.md) 参照）。

### P0-2: 実データによるモデル性能証拠が皆無 → 昇格判断の根拠が無い
- **現状**: 唯一の pipeline E2E は synthetic self-test（`net_expectancy_r:-1.075`、昇格 denied）。実価格での triple-barrier 学習・評価bundleが存在しない。
- **なぜ問題か**: 「70点（実証）」も「shadow昇格」も、実データOOSの正の期待値なしには宣言不可（タスク§7,§12）。現状 shadow へ進める根拠がゼロ。
- **解消に必要**: 実FX価格OHLC（できれば取引予定brokerのbid/ask）を PIT取込 → authoritative pipeline を実データで完走 → 実データ評価bundle。**これが最大のボトルネック。**

### P0-3: 学習層のデータ飢餓（価格スナップショット常駐が本番未配備）
- **現状**: 収集は launchd 常駐設計（`scripts/install_launchd.sh` 等）だが Mac mini 未配備。開発機は TCC 制限で `~/Desktop` 配下を常駐から読めない。
- **なぜ問題か**: 価格スナップショットが止まると採点・昇格ゲートが0件で足止め（CLAUDE.md明記の既知失敗モード）。30取引日連続稼働（P0-2の前提）が始まらない。
- **解消に必要**: Mac mini `~/srv/fx-codex/` への single-writer 常駐配備＋鮮度監視。

## P1（正式到達に必須）

### P1-1: broker bid/ask adapter が未接続（実quote 0件）
- **現状**: `data_platform/adapters/broker.py` は Protocol + Replay + Unimplemented(fail-closed) のみ。実brokerからの取込ゼロ。
- **なぜ問題か**: データ基盤70点条件（タスク§12「実broker bid/ask」）と cross-source divergence の実証に必須。
- **解消に必要**: 取引予定broker（IBKR等）の quote API に対する実 `QuoteSource` 実装。実注文は行わない。**認証情報が必要なため本セッションでは実装不可**（正直に未接続）。

### P1-2: 外部lockbox custody が未実装
- **現状**: lockbox は durable local custody。GitHub Actions artifact / 別アカS3 / write-only 外部ストレージのいずれも未配線。
- **なぜ問題か**: 研究実行プロセス自身が証拠を消せない保証（タスク§4）が local では不完全。「ローカルで完全防御」とは主張してはならない。
- **解消に必要**: [docs/runbooks/LOCKBOX_CUSTODY.md](../docs/runbooks/LOCKBOX_CUSTODY.md) の interface を実装（本セッションでは runbook/設計のみ）。

### P1-3: GBDT が authoritative pipeline の候補family未登録
- **現状**: `fx_intel/gbm.py` は committee 側に存在。pipeline の complex 候補は logistic_ridge / ridge_regression のみ。
- **なぜ問題か**: タスク Phase 6 が「同一manifestで GBDT を含む全baseline比較」を要求。
- **解消に必要**: `experiment_pipeline.py` の `_FAMILY_KIND` に GBDT を complex 候補として登録＋標準ライブラリ実装（依存追加禁止方針に注意）。

### P1-4: CI に research-v2 専用ジョブが未追加
- **現状**: 既存CIは test(3.11/3.12)/ruff/black/mypy。タスク§10 が要求する deterministic replay / synthetic promotion denial / lockbox single-use / trial ledger tamper / raw-to-bar hash / no-live invariant の**専用CIジョブ**は未追加。
- **なぜ問題か**: これらはテストとしては存在するが、CIゲートとして明示結線されていないと回帰を防げない。
- **解消に必要**: `.github/workflows` に該当ジョブ追加＋`mypy ... data_platform tools` をCIに含める（現在mainのCIは tools/ を除外）。

## P2（品質・完成度）

### P2-1: macro/calendar/news の実PIT運用証拠なし（契約のみ）
- `data_platform/contracts/{economic_event,macro_release,news_event}.py` は実装済みだが実ソース未接続。revision handling は契約レベル。

### P2-2: 階層型学習（global+pair/tf/regime補正）が pipeline 未実装
- 設計はあるが pipeline は単一 global モデル。データ不足時の階層縮退が未配線。

### P2-3: month/pair/session concentration の正式ゲート化が部分的
- `fold_dispersion` 等の部品はあるが、pipeline 出力の昇格ゲートとしての結線が未完。

### P2-4: PR #30（hash-pinned requirements.lock）と #26系の workflow 競合
- #30 は main ベース・trader-build ジョブを含む。#26 の trader/ 削除と競合の可能性。統合後に再検証が必要（既存 gap 分析で指摘済み）。

## リスク・サマリ表

| ID | 分類 | 一言 | 本セッションで解消したか |
|---|---|---|---|
| P0-1 | 安全 | 統合で trader/ 削除 | ❌（承認待ち・runbook作成のみ） |
| P0-2 | 実証 | 実データモデル性能証拠皆無 | ⚠️ 部分（COT PITは実証、価格学習は未） |
| P0-3 | 運用 | 収集常駐が本番未配備 | ❌ |
| P1-1 | データ | broker bid/ask 未接続 | ❌（認証情報必要） |
| P1-2 | 検証 | 外部custody未実装 | ❌（runbook/設計のみ） |
| P1-3 | 学習 | GBDT が pipeline 未登録 | ❌ |
| P1-4 | CI | research-v2専用ジョブ未追加 | ❌ |
| — | CI | **mypy tools/ 28エラー** | ✅ **解消（コミット `3a28efd`）** |
| — | データ | **COT PIT 実データ実証** | ✅ **完了（コミット `e8976f5`）** |
