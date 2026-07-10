# FX分析AI 運用監視・復旧手順

学習型AIの運用では、判断ログ、価格スナップショット、TP/SL/MFE/MAE採点、期待R監視が継続して回っている必要があります。ここでは停止確認と復旧だけを扱います。

## 1. ループ稼働状況の確認

```bash
cd /Users/takahashifuuki/Desktop/fx-codex

ps -axo pid=,command= | rg 'fx_briefing_loop|fx_tf_snapshot_loop|ai_learning_dashboard'
tail -n 80 logs/fx_signal_board.log
tail -n 80 logs/fx_tf_snapshot.log
tail -n 5 logs/briefing_tf_prices.jsonl
```

価格スナップショットの最終更新時刻は、以下で確認します。

```bash
python3 - <<'PY'
from datetime import UTC, datetime
from pathlib import Path
path = Path("logs/briefing_tf_prices.jsonl")
if not path.exists():
    print("missing: logs/briefing_tf_prices.jsonl")
else:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    age = (datetime.now(UTC) - mtime).total_seconds() / 60
    print(f"mtime={mtime.isoformat()} age_minutes={age:.1f}")
PY
```

## 2. 監視コマンド

```bash
python3 tools/decision_expectancy_monitor.py
python3 tools/trade_outcome_monitor.py
python3 tools/maximization_monitor.py
python3 tools/learning_capture.py --keep-going
```

`decision_expectancy_monitor` は `logs/briefing_tf_prices.jsonl` の鮮度も確認します。既定では最終価格行が15分を超えて古い場合に `fail` です。

```bash
python3 tools/decision_expectancy_monitor.py --price-stale-minutes 15
python3 tools/decision_expectancy_monitor.py --price-stale-minutes none  # stale監視だけ無効
```

## 3. 復旧手順

シグナルボードを止めたまま価格スナップショットだけ継続する場合:

```bash
cd /Users/takahashifuuki/Desktop/fx-codex
mkdir -p logs
./fx_tf_snapshot_loop.sh >> logs/fx_tf_snapshot_supervisor.log 2>&1 &
```

Discord配信込みのブリーフィングループが必要な場合:

```bash
./fx_briefing_loop.sh >> logs/fx_briefing_supervisor.log 2>&1 &
```

このループは5分ごとにFXシグナルボードを1通だけ送ります。旧ループが残っている環境では
`tv_notify_loop.sh` と旧版 `fx_briefing_loop.sh` のプロセスを停止してから、新版を1つだけ
起動してください。取引スタックの `.env` は
`DISCORD_NOTIFICATION_MODE=signal_board`（未指定時も既定値は同じ）にします。
シグナルボード自身が `logs/briefing_tf_prices.jsonl` も更新するため、通常は
`fx_tf_snapshot_loop.sh` を同時起動する必要はありません。

復旧後に学習・監視ファイルを更新します。

```bash
python3 tools/learning_capture.py --keep-going
python3 tools/decision_expectancy_monitor.py
```

## 4. status の読み方

- `pass`: 価格系列、採点、期待R監視が運用可能。
- `warn`: サンプル不足、成熟セル不足、品質警告など。運用は継続できるが監視対象。
- `pending`: 24hなど主ホライズンがまだ未成熟。失敗ではなく、時間経過後に再採点。
- `fail`: 価格系列 stale、採点不能、期待Rが非正でブロックなど。復旧または品質改善が必要。

## 5. 重要な監視観点

- `tradable=0` は `summary.tradable_zero_reasons` の reason 別件数を見る。
- `pending_horizon_not_mature` は主ホライズン未成熟なので fail ではない。
- `close_only_path` は high/low が無い価格経路で、TP/SL先着判定の品質不足。
- `performance.net_R` と `performance.expected_R` で実現Rと期待Rを見る。
- `model_expectancy_delta` で `baseline_model` と `learning_model` の expected_R 差分を見る。
- 改善昇格は、既存の昇格ゲートが前回 performance より改善している場合だけ進める。
