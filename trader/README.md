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
| `webhook` | シグナル受信（IP+secret 検証・正規化・冪等）。FastAPI / `/health` |
| `strategy` | 自作戦略（MA クロス+ATR）。`strategy_params.json` をホットリロード。既定 OFF |
| `risk` | Kill switch / 数量 / セッション(休日カレンダー対応) / 日次損失 / レート制限。通過分のみ発注へ |
| `executor` | IBKR 発注（冪等・ATRストップ実発注・realized_pnl 更新・自動再接続）。起動時リコンサイル |
| `monitor` | 60 秒ごとの死活＆ハートビート監視、毎朝 7 時（JST）日次サマリ |
| `redis` / `timescaledb` / `ib-gateway` / `ngrok` | 基盤 |

## ミッションクリティカルの要点
- **ATR ストップの実発注**: 自作戦略（strategy.py）が計算するストップ幅は executor が STP 注文として
  実際にブローカーへ送信する。ポジション反転時は古いストップを取消して張り替える。
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
`strategy.py` がホットリロードする。launchd `com.trader.optimize` が毎週日曜 05:00 に自動実行
（手動は `make optimize`）。最適化対象は `export_history.py` が IB Gateway から取得する実
ヒストリカルデータを優先し、取得できない場合のみ同梱サンプルへフォールバックする。
`overfit_warning`（過学習の疑い）や取引数不足が検出された場合は `strategy_params.json` を
上書きしない（既存パラメータ、または strategy.py の既定値を維持）。詳細は
[fx-codex/README.md](./fx-codex/README.md)。

## Kill switch
```bash
make kill-on        # 全発注を即時停止
make kill-off       # 再開
make kill-status    # 現在値
```
