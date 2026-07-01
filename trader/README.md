# trader — Mac mini 自動売買サーバー（ミッションクリティカル構成）

TradingView / 自作戦略のシグナルを受け、リスク管理を通して IBKR（IB Gateway）へ
発注する自動売買システム。Docker Compose で Mac mini 上に常時稼働させる前提で、
**冪等な発注 / クラッシュ復旧 / 自動 Kill switch / 死活監視 / 構造化ログ** を備える。

> ⚠️ **実弾運用の前に必ず読むこと**: 既定は **paper（IB Gateway 4002）**。本番は
> `.env` で `TRADING_MODE=live` かつ `ALLOW_LIVE=1` の二重設定が必要。コードは安全な
> 土台と手順を提供するが、実資金投入の可否はあなたの責任。まず paper で実績と監視・
> 復旧を検証してから、小ロットで段階的に本番化すること（RUNBOOK の go-live チェックリスト）。

## クイックスタート（Mac mini）
```bash
cd trader
./deploy/bootstrap.sh        # .env 作成・launchd 設置（Docker Desktop が必要）
$EDITOR .env                 # IBKR / DB / WEBHOOK_SECRET / DISCORD などを設定
make up                      # 全サービス起動
make ps                      # 状態
make logs                    # ログ追従
```

## アーキテクチャ概要
```
TradingView ─HTTPS→ ngrok ─→ webhook ─┐
                                       ├→ Redis Stream "signals" → risk → Stream "orders" → executor → IB Gateway
自作戦略 strategy ─────────────────────┘                                         │
monitor（死活監視・日次通知）            reconcile（起動時/定期の突合）            └→ fills / events (TimescaleDB)
```
詳細は [ARCHITECTURE.md](./ARCHITECTURE.md)、運用は [RUNBOOK.md](./RUNBOOK.md)。

## サービス
| サービス | 役割 |
|---|---|
| `webhook` | シグナル受信（IP+secret 検証・`text/plain` 対応・鮮度・正規化・冪等）。FastAPI / `/health`。本番堅牢化: `main()` 実行エントリ・XFF ホップ不足フォールバック・IP 正規化 |
| `strategy` | 自作戦略（MA クロス+ATR）。**実建玉から目標ポジションへの差分発注**でバックテストと遷移一致（反転=クローズ+新規 / 目標0=クローズ / ストップ後再エントリー）。`strategy_params.json` をホットリロード。既定 OFF |
| `risk` | **プロ級リスクエンジン**: リスク基準サイジング / 連敗スロットル / 日次・週次損失 / 相関・同時保有 / イベントブラックアウト / セッション / レート制限。撤退(intent=exit)は素通し。通過分だけサイズを確定して発注へ（→ [RISK.md](./RISK.md)） |
| `executor` | IBKR 発注（冪等・自動再接続）。**エントリーに保護ストップ(STP)を付与**（バックテストの ATR ストップ）。**実約定を execDetails で fills に記録**し commissionReport で realized_pnl 更新。起動時リコンサイル |
| `monitor` | 60 秒ごとの死活＆ハートビート監視、毎朝 7 時（JST）日次サマリ |
| `redis` / `timescaledb` / `ib-gateway` / `ngrok` | 基盤 |

## ミッションクリティカルの要点
- **プロ級リスク管理**: 「予測より撤退・サイズ・相関」。サイズはストップ距離×口座リスクで決め（Kovner）、連敗で縮小→停止（Lipschutz）、日次/週次損失・同時保有数・通貨エクスポージャ・重要指標ブラックアウトで「入らない自由」を自動化。期待値/R 倍数をジャーナルで検証（→ [RISK.md](./RISK.md)）。
- **堅牢な受信**: TradingView は本文を `text/plain` で送るため Content-Type 非依存で JSON パース（`application/json` 期待だと 422 で全弾はじく）。XFF 右端で IP 偽装を防ぎ、`{{timenow}}` でリプレイ拒否。publish 失敗時は idem を解放して再送可能にする。
- **冪等発注**: webhook で idem を Redis に記録 + executor が `processed_orders`（PK=idem）で二重発注を防止。
- **クラッシュ復旧**: Redis Streams を成功時のみ ACK、`XAUTOCLAIM` で宙づりを回収、N 回失敗は dead-letter へ。
- **自動 Kill switch**: 日次損失超過 / 連続発注エラーで自動 ON ＋ Discord 通知。Redis 不通時は発注停止（fail-safe）。
- **二重ガード**: `live` でも `ALLOW_LIVE=1` が無ければ発注しない。
- **監視**: 各サービスがハートビートを打ち、monitor が「停止」だけでなく「ハング」も検知。
- **常時稼働**: docker `restart: always` + healthcheck + launchd watchdog（120 秒ごと）+ 日次 DB バックアップ。

## 開発
```bash
pip install -r requirements-dev.txt
make lint      # ruff
make test      # pytest（外部依存は fakeredis / モック）
make config    # docker compose 構文検証
```

## 分析・最適化（fx_backtester）
実弾判断に使える「信頼できる分析」のためのバックテスタを [`fx-codex/`](./fx-codex/) に同梱。
先読みバイアスなし・コスト考慮・**ウォークフォワード/OOS 検証**で過剰最適化を検出する。
`optimize/auto_optimize.py` がこれを使って `strategy_params.json` を OOS 検証して更新し、
`strategy.py` がホットリロードする。詳細は [fx-codex/README.md](./fx-codex/README.md)。

## Kill switch
```bash
make kill-on        # 全発注を即時停止
make kill-off       # 再開
make kill-status    # 現在値
```

## リスク・ジャーナル / DB マイグレーション
```bash
make journal        # 直近30日の期待値・R倍数・連敗・PF（勝率に依存しない検証）
make migrate        # db/migrations/*.sql を既存 DB に適用（冪等。fills へ列追加）
```
