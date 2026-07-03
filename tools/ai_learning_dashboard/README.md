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

## 読み取るファイル

- `logs/briefing_journal.jsonl`
- `logs/briefing_learning.json`
- `logs/ml_model.json`
- `logs/promotion_state.json`

ファイルが無い場合も、未作成として表示します。
