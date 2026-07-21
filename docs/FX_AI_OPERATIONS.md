# FX分析AI 運用監視・復旧手順

これは分析・学習データ収集の監視手順です。実注文経路はありません。判断ログ、価格スナップショット、TP/SL/MFE/MAE採点、期待R監視が継続していても、それだけでモデル検証や昇格を意味しません。Mac miniの移行・復旧・rollbackは、証跡と単一writerを定めた[Operations runbook](OPERATIONS_RUNBOOK.md)を優先します。

## 1. 正規サービスの確認

```bash
cd /Users/fuuki/srv/fx-codex
./scripts/status_fx_services.sh
ps -axo pid=,command= | rg 'fx_briefing_loop|fx_tf_snapshot_loop|fx_briefing.py|fx_tf_snapshot.py'
tail -n 80 logs/launchd/snapshot.err.log
tail -n 80 logs/launchd/briefing.err.log
tail -n 80 logs/launchd/health.err.log
tail -n 5 logs/briefing_tf_prices.jsonl
tail -n 20 logs/fx_fusion_capture.log
tail -n 5 logs/briefing_journal.jsonl
```

正規構成はlaunchdの `com.fx-codex.snapshot`（5分・唯一の価格writer）、`com.fx-codex.briefing`（5分境界・時間足別統合通知、同じ排他ロック内で最大1時間ごとの融合判断）、`com.fx-codex.health`（5分）です。融合判断は`--no-discord --no-price-write`で動き、ログは`logs/fx_fusion_capture.log`に残ります。`fx_briefing_loop.sh` / `fx_tf_snapshot_loop.sh`、直接実行、cron writerが見つかった場合は競合です。自動killせず、プロセス一覧・cron・plist・ログを保存してから人間が停止対象を確認します。

時間足別Discord通知はUSDJPY/EURUSD、融合判断とGBDT候補収集はUSDJPY/EURUSD/GBPUSDです。
GBPUSDを含む3ペア×4時間足の価格完全性は`com.fx-codex.snapshot`とfreshness monitorが監視します。
GBDTは`source_cutoff`・`max_feature_available_time`・`prediction_time`の順序を検証できる
PIT適格な融合行だけを学習・昇格に使います。旧形式行は監査用に保持しますが学習件数には含めません。

時間足別処理の一般失敗では融合処理を開始しません。主要ジャーナル書込み失敗は終了コード4、
Discord通知だけの失敗は終了コード5です。通知失敗時はlaunchdへ非ゼロを返しつつ、保存済みの判断とは
独立した融合取得を継続します。

開発機 `/Users/takahashifuuki/Desktop/fx-codex` は検証用であり、Mac miniの収集責務を代替しません。

## 2. 監視コマンド

```bash
./scripts/status_fx_services.sh
.venv/bin/python tools/data_freshness_monitor.py --root "$PWD" --no-notify \
  --report /tmp/fx-codex-freshness-manual.json
.venv/bin/python tools/journal_gap_audit.py logs/briefing_tf_prices.jsonl \
  --expected-interval-hours 0.0833333333
```

`data_freshness_monitor --no-notify`は指定した一時reportだけを更新し、canonical notification state/reportを消費しません。`journal_gap_audit`は入力を変更しません。`decision_expectancy_monitor.py`、`learning_capture.py`とoutcome/feedback更新は書き込み処理なので、正規briefing稼働中の読み取り専用確認には使いません。

融合判断の鮮度は、自己依存するhard gateを避けるためcanonical freshness reportの対象外です。
学習ダッシュボードの融合最終時刻と`fx_fusion_capture.log`を併用して確認します。

鮮度監視を無効化した結果を運用判断に使ってはいけません。

## 3. 復旧手順

手動ループを復旧手段として起動しません。正規launchdサービスが全てロード済みで、loop/direct/cron/別checkoutのwriter候補がないことを確認した場合だけ再起動します。正規launchd子が実行中ならdirect writer候補として見えるため、親PID/cwdを確認して完了を待ちます。

```bash
cd /Users/fuuki/srv/fx-codex
./scripts/status_fx_services.sh
./scripts/restart_fx_services.sh
./scripts/status_fx_services.sh
```

`restart_fx_services.sh` は変更前に全labelを検証し、1サービスでも未ロード、またはwriter候補があれば何もkickstartせず非ゼロ終了します。未ロード、競合、stale、dirty/version driftがある場合は、その場で`install_launchd.sh`やraw loopを実行せず、Operations runbookのpre-state保存、paper-safe確認、commit SHA検証、dry-run、移行、post-check、rollback手順を実施します。

復旧後も欠損を現在値で補間せず、gap/duplicate/time reversalを別レポートに残します。

## 4. status の読み方

- `pass`: 対象チェックが通ったという意味だけで、モデル検証・収益性・昇格を保証しない。
- `warn`: サンプル不足、成熟セル不足、品質警告など。鮮度レポートの`warning`は正規briefingで新規判断をhard vetoする。
- `pending`: 24hなど主ホライズンがまだ未成熟。失敗ではなく、時間経過後に再採点。
- `fail`: 価格系列 stale、採点不能、期待Rが非正でブロックなど。復旧または品質改善が必要。

`scripts/status_fx_services.sh` は正常0、warning 1、critical/missing/競合2で終了します。終了コードを無視しないでください。

## 5. 重要な監視観点

- `tradable=0` は `summary.tradable_zero_reasons` の reason 別件数を見る。
- `pending_horizon_not_mature` は主ホライズン未成熟なので fail ではない。
- `close_only_path` は high/low が無い価格経路で、TP/SL先着判定の品質不足。
- `performance.net_R` と `performance.expected_R` で実現Rと期待Rを見る。
- `model_expectancy_delta` で `baseline_model` と `learning_model` の expected_R 差分を見る。
- 改善候補は研究評価に留める。前回値より良い、または警告がないだけでは昇格しない。
