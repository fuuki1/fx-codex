# fx-codex

Mac mini をサーバー化して動かす自動売買（FX）システム。

実体は [`trader/`](./trader/) にある。セットアップ・運用・設計は以下を参照:
- [trader/README.md](./trader/README.md) — クイックスタート
- [trader/ARCHITECTURE.md](./trader/ARCHITECTURE.md) — 構成と実装
- [trader/RISK.md](./trader/RISK.md) — プロ級リスクエンジン（サイジング・連敗・相関・ブラックアウト）
- [trader/RUNBOOK.md](./trader/RUNBOOK.md) — 運用手順・go-live チェックリスト

> 既定は paper（IB Gateway 4002）。本番は `TRADING_MODE=live` かつ `ALLOW_LIVE=1` の二重設定が必要。
