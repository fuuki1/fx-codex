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
│   ├── risk.py               # ② リスク管理（状態収集 + 純粋エンジンへ委譲 + 副作用）
│   ├── risk_engine.py        # ② プロ級リスク判断の純粋ロジック（→ RISK.md）
│   ├── journal.py            # 期待値・R 倍数・連敗の成績分析（純粋 + CLI）
│   ├── executor.py           # ③ 注文実行（IBKR / ib_async, 冪等, realized_pnl/R 更新）
│   ├── reconcile.py          # ブローカー実状態 vs DB の突合
│   ├── oanda.py              # ⑦ OANDA v20 のローソク取得（アドバイザリー分析のデータ源）
│   ├── analysis.py           # ⑦ MTF 分析の純粋ロジック（HTF トレンド × LTF タイミング + ATR）
│   ├── dashboard.py          # ⑦ アドバイザリー: 分析ループ + チャート Web 表示 + Discord 通知（実売買なし）
│   ├── monitor.py            # ⑥ 死活監視・日次通知（成績サマリ含む）
│   ├── risk_calendar.example.json # 重要指標ブラックアウト窓の雛形（→ risk_calendar.json）
│   └── healthz.py            # コンテナ healthcheck（ハートビート鮮度）
├── db/init.sql               # events / fills / processed_orders + 索引
├── db/migrations/            # 既存 DB 向けの冪等マイグレーション（make migrate）
├── optimize/auto_optimize.py # 自律最適化（OOS 検証で配備可否を判定・fx_backtester は別途）
├── tests/                    # pytest（domain / risk_engine / journal / webhook / executor / …）
└── deploy/                   # launchd plist / bootstrap.sh / watchdog.sh / backup.sh
```

## データフロー
```
TradingView アラート ─HTTPS POST→ [ngrok] ─→ ① webhook.py
  └ ボディ上限 → IP検証(XFF右端) → 生ボディJSONパース(CT非依存) → secret検証
    → 正規化 → 鮮度(MAX_SIGNAL_AGE_SEC) → idem 冪等(Redis nx,ex=3600) → Stream "signals"
    （publish 失敗時は idem を解放して 503＝再送で復旧、永久ロストを防ぐ）

④ strategy.py（並行）: interval ごとに目標ポジション(-1/0/+1)を判定し、**ブローカー実
        建玉から目標への差分注文**を "signals" へ publish（バックテストのポジション遷移と一致。
        反転=クローズ+新規、目標0=クローズ、ストップ後は再エントリー。entry には stop_distance、
        exit には intent=exit を載せる）
        ▼ Stream "signals"
② risk.py（Consumer Group "risk"）→ 純粋判断は risk_engine.evaluate（→ RISK.md）
  └ KillSwitch(Redis,fail-safe) → 状態収集(残高/日次・週次損益/直近損益/建玉/カレンダー/DD)
  └ ブラックアウト → 薄商い → セッション → 実現DD → 日次損失 → 週次損失 → 連敗停止
    → 根拠必須 → 非対称性(R:R) → リスク基準サイジング → 数量上限 → 同時保有数
    → 通貨エクスポージャ →（承認後）発注レート
  └ 日次/週次/連敗停止は自動 KillSwitch + 通知。通過分は qty を確定し intended_risk を載せて
  └ Stream "orders"
        ▼ Stream "orders"
③ executor.py（Consumer Group "exec"）
  └ KillSwitch 再確認 → 本番二重ガード → 冪等claim(processed_orders) → IBKR 発注
  └ エントリーに保護ストップ(IBKR STP, `<ref>:stop`)を付与（= バックテストの ATR ストップ）。
     撤退(intent=exit)は先に保護ストップを取消してフラット化
  └ **実約定を execDetails で fills に記録**（約定価格・数量・execId、冪等）→ commissionReport が
     execId 基準で realized_pnl/realized_r を更新 → 通知
  └ 起動時 reconcile（取りこぼし/未完了の検知。`:stop` 子注文は既知親として孤児判定から除外）

⑥ monitor.py（並行）: 60秒ごとに health + ハートビート鮮度。毎朝7時(JST)に日次サマリ

⑦ dashboard.py（独立・実売買なし / profile "advisory"）: 発注系に依存しないアドバイザリー。
     oanda.py で USDJPY の下位足/上位足ローソクを定期取得 → analysis.analyze（MTF: 上位足トレンド ×
     下位足タイミング + ATR ストップ + R:R）で助言を生成 → 分析中チャート（ローソク+MA+売買マーカー+
     損切り/利確ライン, Lightweight Charts）を Web 表示（/・/api/state・/health）→ 好機の状態変化時のみ
     Discord 通知。「入る/入らない・損切り・R:R」を根拠つきで提示するだけで、注文は一切出さない。
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
- `fills(ts, symbol, side, qty, status, broker, ref, realized_pnl, idem, intended_risk, stop_distance, realized_r, fill_price, exec_id)`
  — **実約定履歴**（発注ログではない）。executor が IBKR の execDetails を受けて 1 約定 = 1 行を
  `exec_id` 冪等で記録し（`fill_price`=実約定価格・`qty`=約定数量）、commissionReport が `exec_id`
  基準で `realized_pnl`/`realized_r`（= 実現損益÷想定リスク = R 倍数）を更新する。`intended_risk`
  （発注時の想定最大損失）/ `stop_distance` は発注文脈から載せ、`journal.py` の期待値・R 倍数集計の
  基礎にする。列追加は `db/migrations/0003_fills_executions.sql`（`make migrate`・冪等）。
- `processed_orders(idem PK, client_order_id, submitted_at, broker_ref, status)` — 冪等発注の決め手
- `daily_pnl`（ビュー）— 日次実現損益の集計
- 既存 DB への列追加は `db/migrations/0001_risk_columns.sql`（`make migrate`・冪等）。

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
| ライブの保護ストップ | ✅ エントリー約定に IBKR STP を付与（撤退で取消）。バックテストの ATR ストップに対応 |
| `fills` が発注ログだった | ✅ execDetails で **実約定**（約定価格・数量・execId）を冪等記録。realized_pnl は execId 基準で更新 |
| ライブ⇄バックテストのポジション遷移 | ✅ 実建玉から目標への差分注文（反転=クローズ+新規 / 目標0=クローズ / ストップ後再エントリー） |
| 自作戦略が既定でシグナル皆無 | ✅ 履歴取得を必要バー数から逆算（旧: 5秒足40本 < slow60+1 で恒常 None だった） |
| Webhook の本番堅牢化 | ✅ `main()` エントリ / XFF ホップ不足のフォールバック / IP 正規化（ポート・IPv4-mapped IPv6） |
| 自作戦略 `decide()` | ✅ ATR ストップ付き MA クロス + パラメータ自動再読込 |
| DB 接続プール | ✅ psycopg_pool へ移行 |
| レート制限の永続化 | ✅ Redis ZSET のスライディングウィンドウ（再起動で消えない） |

## 追加したミッションクリティカル要素
- プロ級リスクエンジン（`risk_engine.py`・純粋ロジック）: リスク基準サイジング（ストップ距離×
  口座リスク）/ 連敗スロットル / 日次・週次損失 / 同時保有数 / 通貨エクスポージャ /
  イベントブラックアウト。期待値・R 倍数のジャーナル（`journal.py`）と OOS 配備ゲート
  （`auto_optimize.should_deploy`）。設計と根拠は [RISK.md](./RISK.md)。
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
- **`stop_distance`**（任意）: 価格距離。入れると executor がエントリー約定に保護ストップ(STP)を
  付ける（`stop_price` と基準価格からの導出も可）。
- **`intent`**（任意）: 手仕舞いアラートは `"intent": "exit"` を入れる。executor が保護ストップを
  取り消してフラット化し、risk は入口ゲートを課さず素通しする（`close` / `flat` も同義）。
- **Content-Type の注意**: TradingView は本文を `text/plain` で送る。webhook は
  Content-Type に依存せず生ボディを JSON パースするので、そのままで受理できる
  （`application/json` を期待する実装だと 422 で全弾はじかれる—対策済み）。
- **送信元 IP**: TradingView 公式の 4 つ（`52.89.214.238 / 34.212.75.30 /
  54.218.53.128 / 52.32.178.7`）を `TV_ALLOWED_IPS` に設定。ngrok 経由では
  `X-Forwarded-For` の右端（信頼プロキシが付与した実クライアント）で照合するため、
  偽の XFF を足しても IP 検証は迂回されない（`TV_TRUSTED_PROXY_HOPS=1`）。XFF のホップ数が
  想定より少ない（＝信頼プロキシが本物を追記した形跡が無い）場合は XFF を信用せず TCP ピア IP に
  フォールバックする。IP はポート除去・IPv4-mapped IPv6 の平坦化を施してから照合する。
