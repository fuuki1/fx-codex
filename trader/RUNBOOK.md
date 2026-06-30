# RUNBOOK — trader 運用手順書

実弾を扱うシステムなので、迷ったら **まず Kill switch を ON**（`make kill-on`）。
安全を確保してから原因調査する。

## 1. 緊急時：全発注を止める
```bash
cd trader
make kill-on          # Redis kill_switch=1。risk と executor の二段で発注を遮断
make kill-status
```
- 再開は安全確認後に `make kill-off`。
- Redis 自体が不通のときは `kill_switch` を読めないため **executor は自動的に発注停止**（fail-safe）。

## 2. 日常運用
| やること | コマンド |
|---|---|
| 起動 | `make up` |
| 状態確認 | `make ps` |
| ログ追従 | `make logs` |
| 再起動 | `make restart` |
| 停止（データ保持） | `make down` |
| DB バックアップ | `make backup` |
| リコンサイル手動実行 | `make reconcile` |

## 3. 監視とアラート
- `monitor` が 60 秒ごとに webhook `/health` / DB / Redis / 各サービスのハートビート鮮度を確認。
- 異常は Discord へ通知（`common.notify` が同一内容を `NOTIFY_THROTTLE_SEC` 秒抑制してアラート嵐を防ぐ）。
- 毎朝 7 時（JST）に日次サマリ（約定件数 / 実現損益 / Kill switch / モード / 期待値・R 倍数・連敗）。
- launchd `com.trader.supervisor`（120 秒ごと）が compose を up に保ち、unhealthy/exited を自動再起動。

## 4. 障害対応
### サービスが unhealthy / 落ちた
1. `make ps` で対象を特定、`docker compose logs <svc> --tail=200` を確認。
2. 一過性なら `docker compose restart <svc>`（watchdog も自動再起動する）。
3. 繰り返すなら Kill switch ON にして根本原因を調査。

### IB Gateway 切断
- executor はアイドル毎に切断検知 → 指数バックオフで自動再接続（通知あり）。
- 2FA タイムアウト等で再ログインが必要なら VNC（`127.0.0.1:5900`）で確認。

### メッセージが詰まった / dead-letter
- N 回処理に失敗したメッセージは `signals:dead` / `orders:dead` に退避され ACK される（通知あり）。
- 中身を確認: `docker compose exec redis redis-cli XRANGE orders:dead - +`
- 原因修正後、必要なら手動で再投入（内容を確認のうえ `XADD orders ...`）。

### 取りこぼし疑い / 建玉のズレ
- `make reconcile`（または executor 起動時に自動実行）でブローカーと DB を突合。
- `processed_orders.status='submitting'` が残る = 発注確保後に完了記録が無い（クラッシュ疑い）。
  → IB 側の実際の注文/建玉を VNC・TWS で確認し、必要なら手動是正。**自動是正はしない**（誤是正回避）。

## 5. データ
- DB バックアップ: `make backup`（`backups/` に gzip、既定 14 世代保持）。launchd `com.trader.backup` が毎日 06:00 実行。
- リストア: `gunzip -c backups/trader-YYYYMMDD-HHMMSS.sql.gz | docker compose exec -T timescaledb psql -U $POSTGRES_USER -d $POSTGRES_DB`
- Redis は AOF 永続（`redis-data` ボリューム）。

## 6. デプロイ / 更新
```bash
git pull
make migrate       # DB スキーマ変更がある更新では先に適用（冪等。無ければ no-op）
make up            # build 込みで再作成（restart: always なので順次入れ替え）
make ps
```
- `.env` を変えたら該当サービスを `docker compose up -d` で再作成。
- リスクエンジンの調整（残高更新・指標カレンダー更新）は `risk` を再作成、または
  `risk_calendar.json` の編集（mtime 監視でホットリロード）で反映される。

## 7. go-live チェックリスト（paper → 本番）
**各段階で中止条件を決め、満たさなければ前段に戻る。**

0. **リスクエンジンの準備**（サイジングを使う場合・→ [RISK.md](./RISK.md)）
   - [ ] `make migrate`（既存 DB に `fills.intended_risk/stop_distance/realized_r` を追加）
   - [ ] `ACCOUNT_EQUITY` を実残高に、`RISK_PER_TRADE_PCT` を 0.25–0.5% に設定
   - [ ] `RISK_VALUE_PER_POINT` を取引ペアに合わせる（JPY 建て×JPY 口座以外）
   - [ ] `cp app/risk_calendar.example.json app/risk_calendar.json` し、当面の CPI/NFP/FOMC を記入
   - [ ] `MAX_WEEKLY_LOSS_JPY` / `MAX_CONCURRENT_POSITIONS` / `MAX_CURRENCY_EXPOSURE` を設定
   - [ ] `RISK_SIZING_ENABLED=1`（有効化）。paper で `events.kind='risk_decision'` のサイズ・想定リスクを確認
1. **paper で安定運用**（IB Gateway 4002, `TRADING_MODE=paper`）
   - [ ] 監視・Discord 通知・日次サマリ（期待値・R 倍数・連敗を含む）が届く
   - [ ] webhook→risk→executor→fills まで events に相関 ID（idem）で追える
   - [ ] Kill switch（手動・日次/週次損失自動・連敗自動・連続エラー自動）が効く
   - [ ] 連敗時にサイズが縮小→停止すること、ブラックアウト窓で新規が止まることを確認
   - [ ] 障害注入（redis/executor 停止→復旧、reconcile 差異検知、watchdog 再起動）を確認
   - [ ] `make journal` で期待値・R 倍数が記録されている
   - [ ] N 日（推奨 5 営業日以上）無人で安定
2. **本番・最小ロット**（`TRADING_MODE=live` かつ `ALLOW_LIVE=1`、`MAX_POSITION_QTY` を最小に）
   - [ ] 1 件の往復約定で realized_pnl が `fills` に反映される
   - [ ] 日次損失上限・レート制限が実弾でも機能
   - 中止条件例: 想定外の約定 / 二重発注 / 監視欠落が 1 度でも起きたら即 Kill switch
3. **段階増量**: 数量・対象銘柄・戦略を少しずつ拡大。各拡大の前に上記を再確認。

## 8. 設定の勘所（.env）
| キー | 意味 |
|---|---|
| `TRADING_MODE` / `ALLOW_LIVE` | paper/live と本番二重ガード |
| `MAX_POSITION_QTY` | 1 発注あたり数量上限 |
| `MAX_DAILY_LOSS_JPY` | 日次実現損失の上限（超過で自動 Kill switch） |
| `MAX_ORDERS_PER_MIN` | 1 分あたり発注数の上限 |
| `MAX_CONSECUTIVE_ERRORS` | 連続発注エラーでの自動 Kill switch 閾値 |
| `ENFORCE_SESSION` | 取引時間帯チェックの有効化 |
| `RISK_SIZING_ENABLED` | リスク基準サイジングの有効化（既定 0=qty はシグナルのまま） |
| `ACCOUNT_EQUITY` / `RISK_PER_TRADE_PCT` | サイジング基準（残高 / 1 取引リスク%） |
| `RISK_VALUE_PER_POINT` | 銘柄ごとの単価（JPY建て×JPY口座=1.0。例 `EURUSD=150.0`） |
| `MAX_WEEKLY_LOSS_JPY` | 週次実現損失の上限（0=無効、超過で自動 Kill switch） |
| `LOSS_STREAK_REDUCE_AT` / `_HALT_AT` | 連敗でサイズ半減 / 新規停止する閾値 |
| `MAX_CONCURRENT_POSITIONS` | 同時に持てる別銘柄数（0=無効） |
| `MAX_CURRENCY_EXPOSURE` | 1 通貨あたり純エクスポージャ上限（0=無効） |
| `RISK_BLACKOUT_FILE` | 重要指標ブラックアウト窓ファイル（→ `risk_calendar.json`） |
| `MIN_REWARD_RISK` / `REQUIRE_TARGET_FOR_RR` | 非対称性(R:R)の下限 / 利確目標の必須化（0=無効） |
| `REQUIRE_REASON` | 根拠(reason)の無いシグナルを却下 |
| `MAX_DRAWDOWN_PCT` / `DRAWDOWN_LOOKBACK_DAYS` | 実現DDキル（高値からの% / 集計期間。0=無効） |
| `THIN_LIQUIDITY_WINDOWS` | 薄商い時間帯（UTC `HH:MM-HH:MM` 区切り）で新規抑止 |

> リスクエンジンの詳細・計算式・根拠は [RISK.md](./RISK.md)。
| `STRATEGY_ENABLED` | 自作戦略のシグナル発行（既定 0=停止） |
| `TV_ALLOWED_IPS` / `WEBHOOK_SECRET` | webhook の IP / secret 検証 |
| `TV_TRUSTED_PROXY_HOPS` | 信頼プロキシ段数（ngrok=1, 直接公開=0）。IP 照合に使う XFF の位置 |
| `MAX_WEBHOOK_BODY_BYTES` | 受信ボディ上限（超過は 413） |
| `MAX_SIGNAL_AGE_SEC` | シグナル鮮度上限（0=無効。`time` 付き受信が古い/未来すぎると 409） |

## 9. TradingView Webhook（受信経路）の運用
ミッションクリティカルでは「アラートが確実に届き、確実に 1 回だけ発注される」ことが要。

### アラート設定（TradingView 側）
- **URL**: `https://<NGROK_DOMAIN>/webhook`（POST）。
- **メッセージ（JSON）**: 必ず `secret` と、冪等用の一意な `id`、鮮度用の `time` を入れる。
  ```json
  {"secret":"<WEBHOOK_SECRET>","symbol":"USDJPY","asset":"fx",
   "side":"{{strategy.order.action}}","qty":1000,"type":"market",
   "time":"{{timenow}}","id":"{{timenow}}-{{ticker}}"}
  ```
  - `time` は発火時刻 `{{timenow}}`。bar 時刻 `{{time}}` は上位足で古くなり 409 誤拒否の元なので不可。
- **Content-Type**: TradingView は `text/plain` で送る。webhook は CT 非依存で受理する（対策済み）。

### 受信できているかの確認（スモークテスト）
```bash
# ローカル（127.0.0.1:8000）へ直接。secret を正しく入れて 200/accepted を確認
curl -s -XPOST localhost:8000/webhook -H 'Content-Type: text/plain' \
  -d '{"secret":"<WEBHOOK_SECRET>","symbol":"USDJPY","side":"buy","qty":1000,"type":"market","time":"'"$(date -u +%FT%TZ)"'","id":"smoke-'"$(date +%s)"'"}'
# events と stream に乗ったか
docker compose exec redis redis-cli XLEN signals
docker compose exec -T timescaledb psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select ts,kind from events order by ts desc limit 5;"
```
公開 URL の疎通は `curl -s https://<NGROK_DOMAIN>/health`（200/`{"status":"ok"}`）。

### アラートが届かない / 弾かれるとき
| 症状 | 原因と対処 |
|---|---|
| 422 | 旧実装の Content-Type 問題（対策済み）。古いイメージなら `make up` で再ビルド |
| 403 forbidden | 送信元 IP 不一致。`TV_ALLOWED_IPS` に公式 4 IP、`TV_TRUSTED_PROXY_HOPS=1` を確認 |
| 401 unauthorized | `secret` 不一致。アラート本文と `.env` の `WEBHOOK_SECRET` を一致させる |
| 409 stale signal | `time` が古い/未来。`{{timenow}}` を使う。Mac の NTP 同期を確認 |
| 400 | JSON 不正 or 必須欠落（symbol/side/qty）。アラート本文を見直す |
| 503 | Redis 不通 or publish 失敗。idem は解放済みなので復旧後に再送可。`make ps` で redis 確認 |
| 200 だが発注されない | risk で却下。理由は `events.kind='risk_decision'` の `reason` を確認（`kill_switch_on`/`event_blackout`/`thin_liquidity_window`/`out_of_session`/`max_drawdown_exceeded`/`daily_loss_exceeded`/`weekly_loss_exceeded`/`loss_streak_halt`/`missing_trade_reason`/`reward_risk_too_low`/`missing_target_for_rr`/`stop_too_wide_for_risk`/`qty_over_limit`/`max_concurrent_positions`/`currency_exposure`/`rate_limited`）。詳細は [RISK.md](./RISK.md) |

### 公開トンネルの信頼性（ngrok）
- ngrok は単一障害点。**無料枠は使わない**——予約ドメイン（`NGROK_DOMAIN`）+ 有料枠で固定 URL に。
- より堅牢にするなら **Cloudflare Tunnel（cloudflared）** を推奨：固定ホスト名・冗長経路・無料。
  TradingView の URL を独自ドメインにできるので URL ローテーション不要。`ngrok` サービスを
  `cloudflared` コンテナに差し替え、`webhook:8000` を published hostname に向ける。
- どちらでもトンネル断は webhook 自体の停止ではない。アラート取りこぼし＝発注機会損失なので、
  TradingView アラートは「重要シグナルは複数回（別 `id`）」か、戦略側 `strategy.py` を冗長化する。

## 10. 既知の制約（拡張ポイント）
- セッション判定は祝日未考慮（市場休日カレンダーの注入が望ましい）。
- 戦略のポジション管理は単純化（状態変化時に固定数量を発注）。実運用ロジックは要拡張。
- exactly-once は近似（claim 後クラッシュ時は重複回避を優先し、reconcile で人手確認）。
- リプレイ防止は `MAX_SIGNAL_AGE_SEC`（要 `time` 付き）+ idem の二段。`time` 無しは受信時刻扱い。
- バックテスタ `fx_backtester`（`optimize/auto_optimize.py` が依存）は本リポジトリ範囲外。
