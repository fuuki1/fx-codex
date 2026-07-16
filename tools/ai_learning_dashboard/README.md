# AI Learning Dashboard

`fx_intel` の学習状態をブラウザで見るための読み取り専用UIです。
既存の取引・分析システムコードは変更せず、`logs/` 配下のファイルだけを読みます。

## 起動

```bash
cd /Users/fuuki/srv/fx-codex
.venv/bin/python tools/ai_learning_dashboard/server.py --host 127.0.0.1 --port 8765
```

別のログディレクトリを見る場合:

```bash
.venv/bin/python tools/ai_learning_dashboard/server.py --log-dir /path/to/fx-codex/logs
```

ブラウザで開く:

```text
http://127.0.0.1:8765/
```

期待値・改善候補監視パネルを更新するには、別ターミナルや cron で監視ランナーを実行します。
ヘルスチェック失敗時は終了コード1になりますが、JSONは書き出されるためUIで状態を確認できます。

```bash
python3 tools/trade_outcome_monitor.py
python3 tools/decision_expectancy_monitor.py
```

正規launchdが動いていない隔離済み開発環境で、学習ログをDiscord送信なしで作り始める場合:

```bash
.venv/bin/python tools/learning_capture.py
```

Mac miniの正規運用中は上記を手動実行しません。`com.fx-codex.briefing`とwriter競合するため、
復旧は`docs/FX_AI_OPERATIONS.md`とOperations runbookに従います。

`fx_briefing.py --dry-run` は表示確認用で、判断ログ・学習ファイルを保存しません。
保存だけ行いDiscordに送らない場合は `fx_briefing.py --no-discord` を使います。

上部の運用状態パネルは、正規launchd 3サービス（snapshot / briefing / health）の登録状態と
前回終了コード、判断ログ、時間足別価格スナップショット、学習プロファイル、各journalの更新時刻を
読み取り専用で確認します。ワンショットサービスは周期の間に子プロセスがいなくても正常です。

サービス確認と復旧手順は `docs/FX_AI_OPERATIONS.md` にまとめています。

## 読み取るファイル

- `logs/briefing_journal.jsonl`
- `logs/briefing_learning.json`
- `logs/ml_model.json`
- `logs/promotion_state.json`
- `logs/trade_outcome_monitor.json`
- `logs/trade_improvement_candidates.json`
- `logs/briefing_decisions.jsonl`
- `logs/briefing_decision_outcomes.json`
- `logs/briefing_decision_feedback.json`
- `logs/decision_expectancy_monitor.json`
- `logs/briefing_tf_journal.jsonl`
- `logs/briefing_tf_learning.json`
- `logs/briefing_tf_prices.jsonl`

ファイルが無い場合も、未作成として表示します。
