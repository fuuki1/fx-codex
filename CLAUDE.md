# CLAUDE.md — fx-codex 開発ガイド

このファイルはClaude Codeセッションの入口です。常設ルールの一次情報は
[AGENTS.md](AGENTS.md)、システムの実体はコードです。記述が競合する場合は推測せず、
コード・テスト・実行時証跡を確認してください。

## 目的と安全境界

このリポジトリは、USD/JPY・EUR/USD・GBP/USDを中心とする再現可能なFX研究・
意思決定支援基盤です。最適化対象は見栄えの良い勝率ではなく、point-in-time整合性、
コスト控除後の未知データ期待値、確率較正、ドローダウン、見送り能力、監査可能性です。

**本システムは恒久的に研究・意思決定支援専用です。**
市場データ収集、履歴研究、オフラインシミュレーション、shadow判断、Discord通知のみを扱います。
brokerへの注文作成・変更・取消・決済・ポジション操作は対象外であり、自動売買開始フェーズは存在しません。
旧`trader/`、executor、`ALLOW_LIVE`、broker注文経路、パラメータから注文への自動接続を、
履歴・rescue branch・backup・別名実装から復元してはいけません。
Mac miniのprocess、launchd、cron、Docker、runtime dataは、人間の明示承認なしに変更しません。

## 主要パス

| パス | 役割 |
|---|---|
| `fx_backtester/` | PIT、ラベル、時系列検証、較正、統計、simulation、risk、governance |
| `fx_intel/` | briefing、macro/news、committee、learning、decision journal |
| `fx_briefing.py` | 分析・Discord通知の統合入口 |
| `tools/` / `scripts/` / `ops/` | 鮮度監視、排他実行、launchd運用 |
| `docs/` / `reports/` / `runs/` | protocol、監査証跡、benchmark、実験artifact |
| `.codex/skills/` | 反復監査・検証workflow |

全体像は[SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md)、運用境界は
[Operations runbook](docs/OPERATIONS_RUNBOOK.md)、監視手順は
[FX AI operations](docs/FX_AI_OPERATIONS.md)を参照してください。

## 必須品質ゲート

repository rootで実行します。

```bash
.venv/bin/ruff check .
.venv/bin/black --check .
.venv/bin/mypy fx_backtester fx_intel *.py
.venv/bin/pytest -q
```

`examples/sample_prices.csv`は合成データです。機能・再現性確認には使えますが、収益性、
validated、paper、live、または「機関投資家級」の根拠にはできません。

## 非交渉ルール

- aware UTC、availability/ingestion/revisionを分離し、未来as-of、random temporal split、
  全期間fit、test再利用を拒否する。
- spread、slippage、commission、financing、gap/stop挙動を必要に応じて含める。
  不明なコストや品質をゼロ・正常として扱わない。
- data-quality/risk vetoをcommitteeやconfidenceで上書きしない。
- synthetic、research、validated、shadow、offline simulationを混同しない。
  shadowとオフラインシミュレーションが最終段階であり、paper/live broker executionへの昇格は存在しない。
- 自動売買開始、live移行、broker発注再導入をTODO・ロードマップ・将来案として提示しない。
- dirty worktree、ユーザーファイル、journal、runtime dataを破壊しない。
- 重要変更はテスト、benchmark、文書、差分レビュー、独立した批判的レビューまで行い、
  性能改善がなければ「改善なし」、証拠不足なら「評価不能・昇格不能」と記録する。

## Git / 並行作業

branch→PR→CI→reviewを使い、`main`へ直接pushしません。同一worktreeを別セッションが
変更し得るため、編集・stage・commit前後に`git status`とHEADを再確認します。
コミット件名だけでscopeを推測せず、実diffをレビューしてください。
