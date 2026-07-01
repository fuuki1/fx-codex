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
| 自律最適化を手動実行 | `make optimize` |

## 3. 監視とアラート
- `monitor` が 60 秒ごとに webhook `/health` / DB / Redis / 各サービスのハートビート鮮度を確認。
- 異常は Discord へ通知（`common.notify` が同一内容を `NOTIFY_THROTTLE_SEC` 秒抑制してアラート嵐を防ぐ）。
- 毎朝 7 時（JST）に日次サマリ（約定件数 / 実現損益 / Kill switch / モード）。
- launchd `com.trader.supervisor`（120 秒ごと）が compose を up に保ち、unhealthy/exited を自動再起動。
- launchd `com.trader.optimize`（毎週日曜 05:00）が自律最適化を実行し、更新有無を Discord へ通知。

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
make up            # build 込みで再作成（restart: always なので順次入れ替え）
make ps
```
- `.env` を変えたら該当サービスを `docker compose up -d` で再作成。

## 7. go-live チェックリスト（paper → 本番）
**各段階で中止条件を決め、満たさなければ前段に戻る。**

1. **paper で安定運用**（IB Gateway 4002, `TRADING_MODE=paper`）
   - [ ] 監視・Discord 通知・日次サマリが届く
   - [ ] webhook→risk→executor→fills まで events に相関 ID（idem）で追える
   - [ ] Kill switch（手動・日次損失自動・連続エラー自動）が効く
   - [ ] 障害注入（redis/executor 停止→復旧、reconcile 差異検知、watchdog 再起動）を確認
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
| `MARKET_HOLIDAYS_FILE` | 休日カレンダー JSON（既定 `app/market_holidays.json` 同梱） |
| `STRATEGY_ENABLED` | 自作戦略のシグナル発行（既定 0=停止） |
| `TV_ALLOWED_IPS` / `WEBHOOK_SECRET` | webhook の IP / secret 検証 |

## 9. 既知の制約（拡張ポイント）
- セッション判定の祝日カレンダーは `app/market_holidays.json` に同梱のデフォルト（JP/US 2024-2027,
  国立天文台の暦要項ベース）。年をまたいで運用する場合や取引所側の変更があった場合は、
  `MARKET_HOLIDAYS_FILE` で指すファイルを更新するか上書きしてください（mtime を見てホットリロード）。
- 戦略のポジション管理は依然として固定数量（`STRATEGY_QTY`）ベース。反転(-1<->1)は 2 倍量の
  発注で正しく反対方向へ乗り換えるが、口座残高やボラティリティに応じた動的サイジングは未対応。
- ATR ストップは strategy 由来のシグナル（`stop_distance` 付き）にのみ実発注される。TradingView
  webhook 経由のシグナルにはストップを付けない（TradingView 側の戦略が自分でエグジットを送る想定）。
  複数ソースが同一シンボルを同時に取引する場合の整合は取らない。
- exactly-once は近似（claim 後クラッシュ時は重複回避を優先し、reconcile で人手確認）。
- バックテスタ `fx_backtester`（`optimize/auto_optimize.py` が依存）は本リポジトリに同梱
  （`fx-codex/`）。祝日カレンダーやボラ連動スリッページのシミュレーションは未対応
  （詳細は [fx-codex/README.md](./fx-codex/README.md) の既知の制約）。
