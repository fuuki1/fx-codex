# FX Codex 正本統合計画 — 2026-08-04

## 結論

このブランチ `recovery/canonical-20260804` は、分岐した `main`・Mac mini 実機系列・未追跡/WIP 系列を、検証可能な1本の履歴へ戻すための**隔離統合ブランチ**である。

このブランチ自体を現時点で本番正本とは認定しない。正本認定は、実機インベントリ、clean clone 再現、PIT/判断ログ parity、サービス構成照合、全品質ゲートを通過した特定コミット SHA に対してのみ行う。

## 観測済みの構成問題

2026-08-04 時点で、少なくとも次が確認されている。

1. `main` と実機系列候補 `audit/operational-shadow-v5-20260728` は共通祖先から大きく分岐している。
2. 実機系列には `decision_commit`、operational store、shadow sync、replay、migration、追加 launchd 構成など、`main` に存在しない重要経路がある。
3. 過去に実機で動作する collector・launchd template・運用文書が Git 未追跡だった。
4. 一部 PR は `main` 以外の integration/audit ブランチを base とし、複数の設計系列が並行している。
5. `main` の `fx_briefing.py` には、実機系列で除去された全ログ再読込・再採点経路が残るため、`main` をそのまま本番へ投入できない。
6. README/SYSTEM_OVERVIEW の正規サービス記述と、実機系列の operational services が一致していない。

## 統合中の非交渉ルール

### 1. 新機能凍結

正本認定まで、予測モデル、判断セマンティクス、ゲート、データソース、通知、ダッシュボードの新機能を `main` へ追加しない。

許可する変更は次だけとする。

- 実機状態の取得と追跡化
- 再現性・PIT・データ完全性・安全境界の修復
- 既存障害の再現テストと限定修正
- 統合に必要な文書・manifest・CI

### 2. 実機を無条件に正としない

Mac mini の作業ツリーは重要な一次証拠だが、未追跡・dirty・ローカル依存があり得る。実機の挙動を保存した後、Git 履歴・レビュー・clean clone テストを通して初めて正本候補へ昇格させる。

### 3. `main` を無条件に正としない

`main` は公開履歴上の既定ブランチだが、実機で除去済みの障害経路や、実機にしかない安全契約が存在する。統合は `main` への機械的 merge ではなく、契約単位の選別と parity 検証で行う。

### 4. 1 PR = 1 契約

巨大な系列統合を一括 merge しない。各 PR は次のいずれか1つだけを扱う。

- runtime inventory / provenance
- PIT・cross-log commit
- operational store
- collector / quote source
- scorer / outcome
- Discord delivery
- dashboard/read model
- documentation / CI

### 5. clean clone を合格条件にする

ローカル未追跡ファイルが存在する状態のテスト成功を受け入れない。すべての候補 SHA は新規 clone で次を通す。

- `git status --porcelain` が空
- dependency lock から環境再構築可能
- import collection 成功
- ruff / black / mypy
- full pytest
- no-order-path / analysis-only safety tests

## Phase 0 — 実機凍結インベントリ

Mac mini を変更せず、次を1つの証跡 bundle に記録する。

- `git rev-parse HEAD`
- `git status --porcelain=v2`
- tracked / untracked / ignored file一覧
- tracked file diff と untracked source の SHA-256
- `launchctl print` の対象サービス
- 実行中 PID、親子関係、entrypoint、interpreter realpath/hash
- plist 実体とリポジトリ template の差分
- Python version、venv、lockfile、installed package hash
- runtime paths、SQLite/JSONL writer、listener、cron
- 最新判断・採点・Discord・freshness の時刻

この証跡を取得するまで、実機 deploy、checkout、reset、clean、backfill を行わない。

## Phase 1 — 追跡化と再現

1. 実機で必要な source/config/template/runbook をすべて Git 追跡下へ置く。
2. secrets、runtime DB、巨大ログ、個人用 token は追跡しない。
3. clean clone で実機 entrypoint の import と dry-run を再現する。
4. 実機系列の特定 SHA と clean clone の tree identity を一致させる。

## Phase 2 — 契約別統合

優先順は次とする。

1. analysis-only / no-order-path 安全境界
2. PIT envelope / cross-log commit / reader visibility
3. canonical bid/ask と cost provenance
4. operational store と legacy JSONL parity
5. scorer / immutable outcome / net-R
6. launchd topology と writer single ownership
7. Discord delivery と freshness
8. dashboard/read model
9. model/decision enhancements

各段階で旧経路と新経路の二重 writer を禁止する。dual-read/shadow は許可するが、正本 writer は常に1つに固定する。

## Phase 3 — `main` 正本化

次を満たす単一 SHA のみを `main` 候補とする。

- clean clone 全品質ゲート成功
- 実機インベントリとの差分が説明済み
- PIT visibility / decision-log parity 成功
- service manifest と launchd 実体が一致
- 未追跡 Python/launchd source 0件
- full replay または同等の復元テスト成功
- rollback 手順の実演成功
- 本番 deploy は承認済み SHA からのみ可能

認定後、Mac mini は approved SHA の clean checkout へ切り替え、dirty changes を禁止する。

## 現在の PR 取扱い

正本統合が終わるまで、既存 PR は次のように扱う。

| PR | 一時分類 | 方針 |
|---:|---|---|
| #42 | 旧 integration 系列 | Draft維持。必要コミットを契約単位で再提出 |
| #70 | 古い main 基準の修正 | Draft化。現系列で再現してから再提出 |
| #71 | canonical capture 大型PR | Draft維持。collector/journal/licensingを分割 |
| #77 | 実機未追跡 collector の追跡化 | Draft化。Phase 1 の候補として選別 |
| #78 | 実機系列上の性能修正 | Draft化。正本候補で再測定して選別 |
| #82 | 出所不明を含む2,959行 | Draft化。inventory/approval/runbookへ分割 |
| #83 | 新判断セマンティクス設計 | Draft化。機能凍結解除後に再開 |
| #84 | 新analytics実装 | Draft化。機能凍結解除後に再開 |

PRを閉じるのは、必要コミットが正本系列へ移植済み、または明確に不要と確認できた場合だけとする。

## 禁止事項

- `main` または実機系列への force push
- dirty 実機での `git clean` / `reset --hard`
- 複数の大型PRを同時merge
- 未追跡sourceに依存したテスト結果の採用
- 既存ログの事前退避なしbackfill
- PIT/commit契約を無視した機械的 conflict resolution
- 新モデル追加による構成問題の先送り

## 完了の定義

「ぐちゃぐちゃではない」と判定できる状態は次である。

- `main` が唯一の開発正本
- Mac mini が approved `main` SHA の clean checkout
- 未追跡実行コード 0件
- 本番サービスが manifest と一致
- 開いているPRがすべて `main` base、または明示された stacked dependency を持つ
- 旧 integration/rescue/wip ブランチはタグ化・アーカイブ対象一覧へ移動
- README、SYSTEM_OVERVIEW、runbook、実機構成が一致
- deploy / rollback / replay が別環境で再現可能
