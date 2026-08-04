# Repository Governance

## 目的

この文書は、`fx-codex` のコード、実機、ブランチ、PR、配備物について、どれを正本として扱うかを固定する。

## 正本

- 開発上の唯一の正本は `main` とする。
- 実機の作業ツリー、`audit/`、`integration/`、`rescue/`、`wip/` ブランチを正本として扱わない。
- 実機で必要な実行コード、script、plist template、設定schema、runbookはすべてGit追跡下に置く。
- secret、runtime DB、JSONL、cache、生成物は追跡しない。

正本化作業中は Issue #86 と Draft PR #85 を統合管理の入口とする。

## ブランチ

- 通常PRのbaseは`main`とする。
- stacked PRを使う場合は、PR本文に親PR番号、依存commit、最終的な`main`への統合順を明記する。
- `integration/`、`audit/`、`rescue/`、`wip/`は一時作業・証跡用であり、そこから直接配備しない。
- 長期ブランチ上で複数機能を並行開発しない。

## PRの単位

原則として **1 PR = 1契約または1障害** とする。

分離対象の例:

- data collection
- PIT / provenance
- storage / migration
- scoring / labels
- model / decision logic
- notification
- dashboard
- operations / launchd
- documentation / CI

複数契約を同時に変更する場合は、分割不能な理由、依存方向、rollback境界を本文に記載する。

## 未追跡依存の禁止

次の状態でPRの品質ゲート成功を主張してはならない。

- importが未追跡Pythonファイルに依存する
- test fixtureが未追跡データに依存する
- 実機にだけ存在するplist/scriptを前提にする
- editable install、`.pth`、`PYTHONPATH`により別ツリーを暗黙読込する

候補SHAは新規clean cloneで検証する。

## 必須検証

コード変更では、該当する範囲について次を実施する。

- `git status --porcelain`が空
- import collection
- ruff
- black
- mypy
- pytest
- analysis-only / no-order-path safety tests
- PIT / provenance / parity tests
- failure injectionまたは負例テスト

全件を実施できない場合は、未実施範囲と理由を明記し、PRをDraftのままにする。

## 実機配備

- 配備対象はレビュー済みの特定commit SHAとする。
- dirty working treeから配備しない。
- 実機で直接ソースを編集しない。
- launchd plist実体はGit追跡templateとmanifestから生成する。
- writer ownershipをサービス単位で1つに固定する。
- 配備前にbackup、配備後にhealth/parity、rollback手順を実施する。

## 文書整合性

コードまたは運用構成を変更したPRは、必要に応じて以下を同時に更新する。

- README
- SYSTEM_OVERVIEW
- operations runbook
- service manifest
- source/provenance ledger
- migration/rollback手順

設計、実装、実機観測を混同せず、それぞれを明示する。

## 正本化期間の機能凍結

Issue #86が完了するまで、予測モデル、判断セマンティクス、データソース、ダッシュボード等の新機能PRはDraftで保持する。

許可されるのは次の変更である。

- 実機証跡の取得
- 未追跡実行コードの追跡化
- 安全・PIT・完全性・再現性の修復
- clean clone / CI / manifest / rollbackの整備
- 既存障害の再現と限定修正

## PRを閉じる条件

旧系列PRは、次のいずれかを満たした場合に閉じる。

- 必要コミットが正本系列へ移植済み
- 現行正本で同じ問題が存在しないと検証済み
- 別PRに完全に置換済み
- 方針変更により不要と判断し、その理由をコメントで記録済み
