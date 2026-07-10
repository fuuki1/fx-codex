# OPERATIONS RUNBOOK — 学習データ収集の常駐運用

学習データ収集(価格スナップショット・ブリーフィング判断・鮮度監視)を
launchd常駐サービスとして運用するための手順書。
2026-07-10のMac mini実機監査に基づく。実行環境の一次情報が常に優先。

## 0. サービス一覧

| Label | 周期 | 実体 | 役割 |
|---|---|---|---|
| `com.fx-codex.snapshot` | 5分毎(StartInterval 300) | `fx_tf_snapshot.py` | 時間足別採点用の価格系列供給。**止まると15m/1h採点が永久に不能** |
| `com.fx-codex.briefing` | 毎時:10(StartCalendarInterval) | `scripts/fx_briefing_once.sh`(融合+時間足別) | 判断生成・ジャーナル追記・学習更新・Discord通知 |
| `com.fx-codex.health` | 5分毎 | `tools/data_freshness_monitor.py` | データ鮮度監視。WARNING/CRITICAL/RECOVERYをDiscordへ |

全サービスは `tools/run_exclusive.py` の排他ロック(flock)経由で起動し、
**二重起動を構造的に防ぐ**(手動起動・cron・launchdのどの組合せでも同時実行は1つ)。

- LaunchAgent(gui/$UID)として動く。**Mac miniは自動ログイン運用が前提**
  (再起動→自動ログイン→エージェント自動起動)。
- ワンショット型のため「異常終了からの復旧」は次周期の再実行で担保される
  (常駐プロセスの再起動ループより単純で、部分実行の中間状態が残らない)。
- 秘密情報(Discord webhook)はplistに書かず、実行時に `.env` から読む。
  鮮度監視は `DISCORD_OPS_WEBHOOK_URL`(運用専用)があれば優先し、
  無ければ既存の `DISCORD_WEBHOOK_URL` を使う。

## 1. インストール / 確認 / 再起動 / 撤去

```bash
cd <fx-codexのルート>          # Mac mini: /Users/fuuki/srv/fx-codex
./scripts/install_launchd.sh --dry-run   # 生成されるplistの確認(変更なし)
./scripts/install_launchd.sh             # インストール+旧サービス置換
./scripts/status_fx_services.sh          # 状態・鮮度・ログを1画面で確認
./scripts/restart_fx_services.sh         # 全サービス再起動(kickstart -k)
./scripts/uninstall_launchd.sh           # rollback(データには触れない)
```

インストーラは旧 `com.fx-codex.briefing.hourly` を自動でbootout・退避し、
手動起動ループ(`fx_*_loop.sh`)の残存を検知して警告する(自動killはしない)。

## 2. 2026-07-10監査で発見した多重起動(移行時に必ず解消すること)

Mac mini (`trader-mini`) の実測。同一ジャーナルに対して**4系統のwriterが並走**していた:

1. 手動起動 `fx_briefing_loop.sh`+`fx_tf_snapshot_loop.sh` ×**3組**
   (日曜15時/木曜0時/金曜10:56開始。全てcwd=~/srv/fx-codex、ロック無し)
2. launchd `com.fx-codex.briefing.hourly`(毎時:10、per-timeframeのみ)
3. cron `*/5 * * * 1-5` が `~/trader/fx-codex` の**別チェックアウト**で
   `fx_briefing.py` を5分毎起動 → `params_gate` 欠落で**クラッシュループ中**
   (ModuleNotFoundError。~/trader/logs/fx_briefing.log 参照)
4. cron `5 * * * 1-5` の `tv_discord_notify.py`(これは正常・独立でOK)

結果: 融合ジャーナルに毎時2〜3回の重複判断(スナップショットログには同一秒の
3プロセス書込を確認)。**重複はlearning.pyのサンプル数を水増しし的中率推定を歪める。**

### 移行手順(Mac mini)

前提: `~/srv/fx-codex` は2026-07-10時点でmainから10PR遅れ(HEAD=025db10)+
未コミットの手パッチ約800行を持つ。**手パッチを失わないため必ずrescueから始める。**

```bash
cd ~/srv/fx-codex

# 0) 未コミットの手パッチをrescueブランチへ保全(何も失わない)
git checkout -b rescue/srv-local-changes-$(date +%Y%m%d)
git add -A && git commit -m "rescue: srv実体の手パッチ保全(運用移行前)"
git push -u origin rescue/srv-local-changes-$(date +%Y%m%d)

# 1) mainへ更新(PR#28マージ後に実行。ops/scripts/toolsの新ファイルが入る)
git checkout main && git pull origin main

# 2) 現状の重複・欠損を監査証跡として保存(読み取り専用)
mkdir -p logs/audit
.venv/bin/python tools/journal_gap_audit.py logs/briefing_journal.jsonl \
  --output logs/audit/journal_gap_audit_fusion_$(date +%Y%m%d).json
.venv/bin/python tools/journal_gap_audit.py logs/briefing_tf_journal.jsonl \
  --output logs/audit/journal_gap_audit_tf_$(date +%Y%m%d).json

# 3) 手動ループを全停止(3組=6プロセスが対象。tail -fの監視端末は無関係)
pkill -f 'fx_briefing_loop.sh|fx_tf_snapshot_loop.sh'

# 4) 壊れた5分毎cronを削除(params_gate欠落でクラッシュループ中。tv_notifyの行は残す)
crontab -l | grep -v 'fx_briefing.py' | crontab -

# 5) 新サービスをインストール(旧briefing.hourlyは自動bootout+plist退避)
./scripts/install_launchd.sh
./scripts/status_fx_services.sh   # 3サービスLOADED+鮮度okを確認

# 6) 5〜10分後に再確認: snapshotが5分毎に追記され、healthがレポートを生成していること
./scripts/status_fx_services.sh
```

## 3. 鮮度監視の閾値(ops/freshness_targets.json)

閾値はコードでなく設定ファイルで管理する。既定値の根拠:

| 対象 | 期待周期 | WARNING | CRITICAL | 根拠 |
|---|---|---|---|---|
| `briefing_tf_prices.jsonl` | 5分 | 15分(3周期) | 45分(9周期) | 15m採点窓(9〜21分)を守るには45分停止が実害ライン |
| `briefing_tf_journal.jsonl` | 1時間 | 2時間 | 6時間 | 1周期スキップは許容(API一時失敗)。6時間=営業日の1/4欠損 |
| `briefing_journal.jsonl` | 1時間 | 2時間 | 6時間 | 同上 |
| `briefing_tf_learning.json` | 1時間 | 3時間 | なし(warnのみ) | ブリーフィング成功の副産物。停止検知はジャーナル側が担う |
| `promotion_state.json` | 1時間 | 3時間 | なし(warnのみ) | 同上 |

週末クローズ中もwriter自体は動き続ける設計(判断はstandbyでも書込みは継続)のため、
休場例外は設けていない。週末に誤検知が出る場合はこの前提が壊れた証拠なので、
閾値を緩める前にwriterの挙動を確認すること。

## 4. Discord通知仕様

- **WARNING**(黄): 更新遅延(warn閾値超過)。同一状態の再通知はcooldown(既定6時間)後のみ
- **CRITICAL**(赤): ファイル欠落 / critical閾値超過 / JSONL末尾破損。悪化遷移は即通知
- **RECOVERY**(緑): 通知済みの異常が正常へ戻ったとき1回だけ。停止時間を含む
- 全通知に: ホスト名 / 対象 / 発生時刻 / 最終更新 / 経過 / 最終正常 / 理由 / 連続検知回数 / 手動対応
- 重複抑止: 状態遷移時のみ送信 + cooldown + `logs/freshness_state.json` に永続化
  (監視プロセスが再起動しても再送しない)
- **Discord送信失敗は監視を止めない**(失敗はレポートに `sent: false` で記録)

## 5. 欠損期間の扱い(捏造禁止)

- 停止期間のデータを現在値から補間・捏造しない
- `tools/journal_gap_audit.py` で欠損期間(開始・終了の絶対時刻)・重複行数・
  時刻逆転を監査レポートとして残す(読み取り専用)
- 既知の欠損(2026-07-10時点):
  - 開発機 `~/Desktop/fx-codex/logs/`: 2026-07-08T15:21Z以降停止(開発機は収集責務なし。
    本番データはMac mini `~/srv/fx-codex/logs/` が正)
  - Mac mini: ジャーナルは2026-07-04頃から継続。ただし多重起動期間(上記)の
    重複汚染があるため、learning評価時は監査レポートを参照
- **バックフィル方針**: 外部OHLC(§6)から価格経路の後埋めは可能だが、行に
  `source` / `source_timestamp` / `ingested_at` / `is_backfill: true` / `backfill_run_id` /
  quality flagsを必須とし、リアルタイム収集行と区別する。
  当時のニュース・スプレッド・取得条件は再現できないため、バックフィル期間を
  **完全なpoint-in-time判断データとして扱わない**(価格採点の補助のみ)

## 6. OHLCデータソース設計(close-only経路の改善、次段階)

現状: TradingViewスキャナーの現在値のみ=close-only経路。TP/SL先着判定の
経路品質上限0.70。改善は `fx_intel/price_history.py` の注入口に
プロバイダを差し込む形で行う(分析ロジック変更なし)。

| 候補 | bid/ask | OHLC | 粒度 | 履歴 | レート制限 | PIT性 | 評価 |
|---|---|---|---|---|---|---|---|
| Dukascopy | ✅(tick) | ✅ | tick〜 | 数年 | 緩い(無償) | 高(確定足) | **本命**。fx_backtester系のdukascopy_cftc_modelで実績あり |
| OANDA v20 | ✅ | ✅ | 5s〜 | 数年 | 有(無償枠) | 高 | 口座必要。live/historical整合が良い |
| IBKR | ✅ | ✅ | 1s〜 | 制限有 | pacing厳しい | 高 | 口座未開設のため現状不可 |
| TradingView(現行) | ❌ | ❌(現在値のみ) | - | なし | 非公式 | 低 | 継続はlive現在値の補助のみ |
| yfinance | ❌ | ✅(日足中心) | 1m(7日制限) | 長期は日足 | 非公式・不安定 | 低 | **主要ソース不採用**。開発時の補助のみ |

プロバイダ抽象(実装時のインターフェース):
`fetch_latest(symbols)` / `fetch_range(symbol, start, end, granularity)` /
`fetch_ohlc(...)` / `fetch_bid_ask(...)` / `health_check()` /
`provider_metadata()`(名称・粒度・遅延・ライセンス) / 各行にquality flags。
全行に `source_timestamp` と `ingested_at` を保持し、point-in-time監査を可能にする。

## 7. rollback

```bash
./scripts/uninstall_launchd.sh   # launchdサービス除去(データ・ログは無傷)
# 旧方式に戻す場合(非推奨・暫定):
nohup ./fx_tf_snapshot_loop.sh </dev/null > logs/fx_tf_snapshot_loop.out 2> logs/fx_tf_snapshot_loop.err &
nohup ./fx_briefing_loop.sh   </dev/null > logs/fx_briefing_loop.out   2> logs/fx_briefing_loop.err &
```

旧plist(`com.fx-codex.briefing.hourly.plist.disabled-<日付>`)は
`~/Library/LaunchAgents/` に退避されているため、名前を戻して
`launchctl bootstrap gui/$(id -u) <plist>` すれば旧構成へ完全復帰できる。

## 8. Live安全設定

この運用変更は分析・収集経路のみに触れる。Live系(`ALLOW_LIVE` /
`STRATEGY_ENABLED` / kill switch)には**一切変更を加えない**。
移行後も `trader/` スタックの設定は従前のまま(paper mode / live disabled)。
