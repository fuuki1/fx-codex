# AI Learning Dashboard

`fx_intel` の学習状態をブラウザで見るための読み取り専用UIです。
既存の取引・分析システムコードは変更せず、`logs/` 配下のファイルだけを読みます。

## 起動

```bash
cd ~/trader/fx-codex
python3 tools/ai_learning_dashboard/server.py --host 127.0.0.1 --port 8765
```

別のログディレクトリを見る場合:

```bash
python3 tools/ai_learning_dashboard/server.py --log-dir /path/to/fx-codex/logs
```

ブラウザで開く:

```text
http://127.0.0.1:8765/
```

期待値・改善候補監視パネルを更新するには、別ターミナルや cron で監視ランナーを実行します。
ヘルスチェック失敗時は終了コード1になりますが、JSONは書き出されるためUIで状態を確認できます。

```bash
python3 tools/trade_outcome_monitor.py
```

学習ログをDiscord送信なしで作り始める場合:

```bash
python3 tools/learning_capture.py
```

`fx_briefing.py --dry-run` は表示確認用で、判断ログ・学習ファイルを保存しません。
保存だけ行いDiscordに送らない場合は `fx_briefing.py --no-discord` を使います。

上部の運用状態パネルは、`fx_briefing_loop.sh` / `fx_tf_snapshot_loop.sh` の稼働有無、
判断ログ、時間足別価格スナップショット、学習プロファイル、各実行ログの更新時刻を
読み取り専用で確認します。Discord送信ループは自動起動しません。

## 読み取るファイル

- `logs/briefing_journal.jsonl`
- `logs/briefing_learning.json`
- `logs/ml_model.json`
- `logs/promotion_state.json`
- `logs/trade_outcome_monitor.json`
- `logs/trade_improvement_candidates.json`
- `logs/briefing_tf_journal.jsonl`
- `logs/briefing_tf_learning.json`
- `logs/briefing_tf_prices.jsonl`

ファイルが無い場合も、未作成として表示します。
