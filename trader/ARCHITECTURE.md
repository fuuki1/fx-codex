# 自動売買システム アーキテクチャ

このリポジトリの `trader/` は ARCHITECTURE をミッションクリティカル品質で実装したもの。
元の設計に対し、信頼性・安全パターン（冪等発注 / クラッシュ復旧 / リコンサイル /
自動 Kill switch / ハートビート監視 / 構造化ログ）と運用基盤を追加している。

## ファイル構成
```
trader/
├── .env / .env.example       # 秘密情報（.env はコミット禁止・chmod 600）
├── docker-compose.yml        # 全サービス（healthcheck / depends_on condition / ログ回転）
├── Makefile                  # up/down/logs/test/lint/backup/kill-switch
├── README.md / RUNBOOK.md / ARCHITECTURE.md
├── app/
│   ├── Dockerfile / requirements.txt
│   ├── config.py             # 型付き設定（env を一元検証・paper/live 判定）
│   ├── logging_setup.py      # 構造化(JSON)ログ + 相関ID(idem)
│   ├── common.py             # DB プール / Redis / 通知 / KillSwitch / Stream / heartbeat
│   ├── domain.py             # 純粋ロジック（正規化 / セッション / レート制限）
│   ├── webhook.py            # ① 外部シグナル受信（FastAPI）
│   ├── strategy.py           # ④ 自作戦略（MAクロス+ATR, パラメータ自動再読込）
│   ├── risk.py               # ② リスク管理・フィルタ
│   ├── executor.py           # ③ 注文実行（IBKR / ib_async, 冪等, realized_pnl 更新）
│   ├── reconcile.py          # ブローカー実状態 vs DB の突合
│   ├── monitor.py            # ⑥ 死活監視・日次通知
│   └── healthz.py            # コンテナ healthcheck（ハートビート鮮度）
├── db/init.sql               # events / fills / processed_orders + 索引
├── optimize/auto_optimize.py # 自律最適化（fx_backtester は別途・範囲外）
├── tests/                    # pytest（domain / risk / webhook / executor / strategy / optimizer）
└── deploy/                   # launchd plist / bootstrap.sh / watchdog.sh / backup.sh
```

## データフロー
```
TradingView アラート ─HTTPS POST→ [ngrok] ─→ ① webhook.py
  └ ボディ上限 → IP検証(XFF右端) → 生ボディJSONパース(CT非依存) → secret検証
    → 正規化 → 鮮度(MAX_SIGNAL_AGE_SEC) → idem 冪等(Redis nx,ex=3600) → Stream "signals"
    （publish 失敗時は idem を解放して 503＝再送で復旧、永久ロストを防ぐ）

④ strategy.py（並行）: interval ごとに decide()。状態変化時のみ "signals" へ publish
        ▼ Stream "signals"
② risk.py（Consumer Group "risk"）
  └ KillSwitch → 数量上限 → 取引時間帯 → 日次損失(超過で自動KillSwitch) → 発注レート
  └ 通過 → Stream "orders"
        ▼ Stream "orders"
③ executor.py（Consumer Group "exec"）
  └ KillSwitch 再確認 → 本番二重ガード → 冪等claim(processed_orders) → IBKR 発注
  └ fills 記録 → commissionReport で realized_pnl 更新 → Discord 通知
  └ 起動時 reconcile（取りこぼし/未完了の検知）

⑥ monitor.py（並行）: 60秒ごとに health + ハートビート鮮度。毎朝7時(JST)に日次サマリ
```

## 共通部品（common.py）
| 関数 | 役割 |
|---|---|
| `pool()` / `db_execute` / `db_query` | psycopg_pool による接続プール |
| `r()` | Redis クライアント（タイムアウト・再接続付き） |
| `log_event(kind, payload)` | events テーブルへ記録（best-effort） |
| `notify(text, key=, throttle=)` | Discord 通知（同一 key をスロットル） |
| `kill_switch_on` / `set_kill_switch` | Kill switch（Redis 不通時は ON 扱い=fail-safe） |
| `heartbeat` / `read_heartbeats` | サービス生存（鮮度）報告 |
| `ensure_group` / `publish` / `consume` | Redis Streams（XAUTOCLAIM 回収・dead-letter 退避） |
| `install_signal_handlers` | SIGTERM/SIGINT でのグレースフル停止 |

## DB スキーマ（TimescaleDB）
- `events(ts, kind, payload jsonb)` — 全イベント監査（hypertable, idem 索引）
- `fills(ts, symbol, side, qty, status, broker, ref, realized_pnl, idem)` — 約定記録
- `processed_orders(idem PK, client_order_id, submitted_at, broker_ref, status)` — 冪等発注の決め手
- `daily_pnl`（ビュー）— 日次実現損益の集計

## Docker サービス
| サービス | イメージ | 役割 |
|---|---|---|
| `ib-gateway` | `ghcr.io/gnzsnz/ib-gateway:stable` | IBKR 自動ログイン（paper 4002 / live 4001） |
| `redis` | `redis:7-alpine` | Stream / KillSwitch / 冪等キー / ハートビート（AOF 永続） |
| `timescaledb` | `timescale/timescaledb:latest-pg16` | 時系列ログ DB |
| `webhook` `strategy` `risk` `executor` `monitor` | `./app` | アプリ各サービス |
| `ngrok` | `ngrok/ngrok:latest` | HTTPS 公開トンネル |

## 元設計からの「未実装ギャップ」解消状況
| 箇所 | 対応 |
|---|---|
| `within_session()` | ✅ FX 24/5・日本株・米株の取引時間を実装（祝日は拡張点） |
| `realized_pnl` 更新 | ✅ executor が commissionReport で約定後に更新 |
| 自作戦略 `decide()` | ✅ ATR ストップ付き MA クロス + パラメータ自動再読込 |
| DB 接続プール | ✅ psycopg_pool へ移行 |
| レート制限の永続化 | ✅ Redis ZSET のスライディングウィンドウ（再起動で消えない） |

## 追加したミッションクリティカル要素
- Webhook 受信の堅牢化（TradingView の `text/plain` を Content-Type 非依存で受理 /
  XFF 右端で IP 偽装迂回を防止 / `{{timenow}}` 鮮度チェックでリプレイ防止 /
  publish 失敗時の idem 解放で永久ロスト防止 / ボディサイズ上限）
- 冪等・取りこぼしなし発注（idem + processed_orders + Streams ACK/XAUTOCLAIM/dead-letter）
- 自動 Kill switch（日次損失・連続エラー）と本番二重ガード（`ALLOW_LIVE`）
- リコンサイル（起動時・手動）、構造化ログ + 相関 ID、ハートビート監視（ハング検知）
- 接続耐性（指数バックオフ・自動再接続）、グレースフル停止、Mac mini 常駐（launchd watchdog）
- テスト（pytest）と CI（ruff + pytest + compose 検証 + image build）

## TradingView アラート設定
Webhook URL: `https://<NGROK_DOMAIN>/webhook`
```json
{
  "secret": "（.env の WEBHOOK_SECRET）",
  "symbol": "USDJPY",
  "asset":  "fx",
  "side":   "{{strategy.order.action}}",
  "qty":    1000,
  "type":   "market",
  "time":   "{{timenow}}",
  "id":     "{{timenow}}-{{ticker}}"
}
```
- **`time`**: 鮮度判定用。発火時刻 `{{timenow}}`（ISO8601）を入れる。`MAX_SIGNAL_AGE_SEC`
  より古い／未来すぎる受信は 409 で拒否し、リプレイや遅延配信での誤発注を防ぐ。
  bar 時刻 `{{time}}` は上位足だと古くなり誤拒否の原因になるので使わないこと。
- **`id`**: 冪等キー。`{{timenow}}-{{ticker}}` のように発火ごとに一意にする。
- **Content-Type の注意**: TradingView は本文を `text/plain` で送る。webhook は
  Content-Type に依存せず生ボディを JSON パースするので、そのままで受理できる
  （`application/json` を期待する実装だと 422 で全弾はじかれる—対策済み）。
- **送信元 IP**: TradingView 公式の 4 つ（`52.89.214.238 / 34.212.75.30 /
  54.218.53.128 / 52.32.178.7`）を `TV_ALLOWED_IPS` に設定。ngrok 経由では
  `X-Forwarded-For` の右端（信頼プロキシが付与した実クライアント）で照合するため、
  偽の XFF を足しても IP 検証は迂回されない（`TV_TRUSTED_PROXY_HOPS=1`）。
