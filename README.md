# FX Codex

USD/JPY、EUR/USD、GBP/USD向けの中低頻度FXバックテスト基盤です。目的は勝率保証ではなく、検証可能性、取引コストの明示、リスク管理、運用停止条件の再現性を優先することです。

## 文書索引

| 領域 | 正本 |
|---|---|
| Target（到達目標・非目標） | [INSTITUTIONAL_FX_AI_TARGET](docs/INSTITUTIONAL_FX_AI_TARGET.md) |
| Architecture（境界・データフロー） | [INSTITUTIONAL_ARCHITECTURE](docs/INSTITUTIONAL_ARCHITECTURE.md)、[SYSTEM_OVERVIEW](SYSTEM_OVERVIEW.md) |
| Research protocol（検証・昇格手順） | [RESEARCH_PROTOCOL](docs/RESEARCH_PROTOCOL.md) |
| Model governance（所有権・承認・停止） | [MODEL_GOVERNANCE](docs/MODEL_GOVERNANCE.md) |
| Independent audit（設計と実装の差分） | [INSTITUTIONAL_READINESS_AUDIT](docs/audits/INSTITUTIONAL_READINESS_AUDIT.md) |
| Operations（Mac mini移行・監視・rollback） | [OPERATIONS_RUNBOOK](docs/OPERATIONS_RUNBOOK.md) |
| Benchmark（再現可能な比較結果） | [institutional_benchmark_20260711](reports/institutional_benchmark_20260711.md) |

## ディレクトリ構成

```text
fx_backtester/
  cli.py                    # CLI
  data.py                   # 価格CSV、経済指標CSVの読み込み
  engine.py                 # イベント型バックテストエンジン
  execution.py              # スプレッド、手数料、スリッページ
  indicators.py             # SMA、ATR、RSI
  metrics.py                # DD、期待値、tail risk、Sharpe/Sortino、コスト/回転率
  analysis.py               # OOS、月次、ペア別、DD期間、Monte Carlo、商用ゲート
  models.py                 # Instrument、Position、Trade
  risk.py                   # 1%リスク、1日2%損失停止、月次利益ターゲット
  tradingview.py            # TradingView Alert Webhook受信
  walk_forward.py           # ウォークフォワード検証
  strategies/
    ai_logistic.py           # ローリング学習するAI/ML方向予測戦略
    base.py
    moving_average_cross.py
    donchian_breakout.py
    rsi_mean_reversion.py
fx_intel/
  calendar.py               # 経済指標カレンダー取得、イベント警戒窓判定
  news.py                   # FXStreet/Google News RSSの収集と通貨タグ付け
  sentiment.py              # 語彙ベース+Claude API(任意)のセンチメント分析
  technicals.py             # TradingViewマルチタイムフレーム集約
  briefing.py               # 複合スコア→トレードプラン→Discord embed生成
  journal.py                # 判断ジャーナル記録と24時間後の的中率検証
  learning.py               # ジャーナル履歴から重み・確信度を自己学習
  market.py                 # FX週末クローズ判定・市場オープン時間換算
examples/
  generate_sample_data.py
tests/
  test_backtester.py
  test_fx_intel.py
```

## ニュース×経済指標×テクニカル統合ブリーフィング (fx_briefing.py)

機関投資家のモーニングブリーフィングを模したDiscord通知です。
テクニカルに加えて以下を統合する、正規のDiscord分析通知です。

- **経済指標カレンダー** (ForexFactory公開フィード): 今後48時間の重要イベント表示、
  イベント前後(前120分/後180分、research-maxプリセット準拠)の「様子見」判定
- **ニュースヘッドライン** (FXStreet / Google News RSS): 直近24時間、通貨タグ付け
- **センチメント分析**: 語彙ベース(常時動作) + Claude API(`.env` に
  `ANTHROPIC_API_KEY` があれば自動で有効化、失敗時は語彙ベースへフォールバック)
- **複合スコア**: テクニカル55% + ニュース45% → ペアごとに方向・確信度(0-100)・
  ATRベースのSL/TP(保守的な固定値 ATR×2.5 を使用)を提示
- **自己学習ループ**: 毎回の判断を `logs/briefing_journal.jsonl` に記録し、
  実行のたびに履歴全体を約24時間後(市場オープン時間換算)の値動きで相互採点。
  テクニカル/ニュースそれぞれの的中率から複合重みを再推定(シュリンク付き、
  テクニカル35〜70%にクランプ)し、確信度帯別の的中率キャリブレーションと
  的中率が低いペアの確信度減衰(×0.6〜1.0)を `logs/briefing_learning.json` に
  導出して、当日の分析に自動反映。結果は通知の「🧠 学習メモ」欄に表示されます。
  サンプル不足の間は既定値で安全に動作します(`--no-learning` で無効化)
- **チャート状態×方向別の学習**: 判断時のチャート状態(RSI・MA乖離のATR換算・
  ボラティリティ・時間足一致度・関連ニュース量・ADX)を特徴量として
  ジャーナルに記録し、「売られすぎ圏」「全時間足一致」などの固定バケットを
  さらにロング/ショート別に分けて的中率を集計。同じ状態でも向きで成績は
  非対称になる(例: RSI買われすぎ圏はロングでは外しやすいがショートは当たる)
  ため、状態×方向のセル単位で学習します。過去に外しやすかったセル
  (方向別に12件以上かつ的中率45%未満)にいまの判断が該当するときは、
  確信度を×0.7〜1.0に減衰して理由を明示。当たりやすい/苦手な状態は
  方向付きで学習メモに一覧表示されます

### 運用モードと書込み責務（必読）

以下は**設計上の正規構成**です。Mac mini (`/Users/fuuki/srv/fx-codex`) では
launchd のワンショットジョブだけを常駐させます。

| ジョブ | 周期 | 正規の責務 |
|---|---:|---|
| `com.fx-codex.snapshot` | 5分 | `logs/briefing_tf_prices.jsonl` の唯一の定期writer |
| `com.fx-codex.briefing` | 5分境界 | 時間足別の判断ジャーナル・学習プロファイル・統合通知 |
| `com.fx-codex.health` | 5分 | 鮮度監視と運用通知（収集ジョブから独立） |

`--signal-board` と `fx_briefing_loop.sh` は**開発・一時確認専用**です。Mac miniの
正規サービス、cron、旧plistのいずれかが動いている間は起動せず、正規の
`logs/*.jsonl` へ同時に書き込ませてはいけません。表示確認は、ジャーナルを保存しない
`--dry-run` を優先してください。排他ロックは同じロック名を使う呼出し同士にしか効かず、
rawな手動コマンドとの共存を自動的に安全にはしません。

2026-07-10の実機監査で観測した状態は、この正規構成と一致していませんでした。旧サービス・
cron・過去の多重writer、価格系列の鮮度異常があり、リポジトリは観測時点で`origin/main`から
18コミット遅れていました。この数値はスナップショットであり、移行前に必ず再取得します。
証跡取得、安全な移行、通知経路、rollbackは
[運用Runbook](docs/OPERATIONS_RUNBOOK.md)を参照してください。

institutional governance方針が表現できる上限は **research → validated → shadow → paper** です。
ただしend-to-end承認サービスとbroker paper stackは現在存在せず、legacyマクロ/ML委員は
非影響のshadowへ固定されています。`--promote-live`とPython APIのpaper/live遷移は無効化されており、
実売買への昇格・発注はこのリポジトリの運用範囲外です。

```bash
.venv/bin/python fx_briefing.py --signal-board --dry-run --symbols GBPUSD EURUSD USDJPY
                                                   # 5分ボードを送信せず内容確認
.venv/bin/python fx_briefing.py --no-discord     # 送信せず判断ログ・学習ファイルだけ更新
.venv/bin/python fx_briefing.py                  # Discordへ送信
.venv/bin/python fx_briefing.py --no-llm         # Claude APIを使わない
python3 tools/learning_capture.py                # Discord送信なしで融合/時間足別/価格系列を1回収集
```

`--dry-run` は判断/価格ジャーナル、学習/model/promotion状態、Discord送信を更新しません。ただし
calendar/macro等のsource cacheは取得処理により更新され、イベントexport/archiveも明示的に
`--no-export-events --no-event-archive`を付けない限り更新され得ます。完全なzero-write確認は正規runtimeと
分離した作業copyで行います。学習を進めたいがDiscordへ送信したくない場合は`--no-discord`、または
`tools/learning_capture.py`を使いますが、どちらも状態を更新するためMac miniの正規サービス稼働中には
手動実行しません。

開発用の`fx_briefing_loop.sh`は5分境界（00/05/10…分）ごとに、**上位3候補・
エントリー適性・4層のデータ品質を1通へまとめたFXシグナルボード**を送ります。
発注経路は存在しません。障害通知はボードへ
集約せず、正規運用では独立した`com.fx-codex.health`から運用Webhookへ送ります。

### 時間足別モード (`--per-timeframe`)

融合1判断の代わりに、**15m / 1h / 4h / 1d の各時間足を独立したアナリスト**として
分析し、時間足ごとに「ロング / ショート / 見送り / 様子見 / 休場」を判断します。
プロトレーダーや機関投資家が複数時間軸を別々に読み、各時間軸で別の結論を持つのと
同じ発想です。

- **時間足別の主ホライズンで自己採点**: 各時間足の判断を、その時間足に対応する
  未来の値動きで採点します(15m→15分後 / 1h→1時間後 / 4h→4時間後 / 1d→24時間後)。
  補助ホライズン(15m: 30分/1h、1h: 4h/8h 等)は Discord 表示・分析確認用の
  「観測」で、学習には主ホライズンのみを使います(多重検定の回避)。
- **将来価格の調達(5分スナップショット)**: TradingView スキャナーは現在値しか
  返さないため、記録から主ホライズン後の実勢価格を後続の終値から取ります。正規運用では
  **`com.fx-codex.snapshot`が`fx_tf_snapshot.py`を5分ごとに起動し、各時間足の現在終値だけを
  `logs/briefing_tf_prices.jsonl` へ記録**し、採点時にこの密な価格系列を判断
  ジャーナルと結合します。これで **15m / 1h / 4h / 1d のすべてが採点可能**に
  なります。外部の履歴OHLC(yfinance/OANDA 等)を差し込む注入口も用意しています
  (`fx_intel/price_history.py`)。
- **symbol×timeframe セル別の学習**: 融合モードと同じ学習コア(複合重み再推定・
  確信度キャリブレーション・ペア別減衰・状態×方向学習・反省レポート・Brier)を、
  `(通貨ペア × 時間足)` のセル単位で適用します。「この通貨のこの時間足でのこの状態の
  ロングは過去に外しやすい」といった粒度で確信度を自動調整します。結果は
  `logs/briefing_tf_learning.json` に保存されます。
- 融合1判断モードとはジャーナル・学習ファイルを分離しているため、両モードを
  混在させても互いに干渉しません。

```bash
.venv/bin/python fx_briefing.py --per-timeframe --dry-run   # 時間足別の内容確認
.venv/bin/python fx_briefing.py --per-timeframe             # Discordへ送信
```

Mac miniの正規運用では`com.fx-codex.snapshot`だけが価格専用系列を供給します。
`fx_briefing_loop.sh`と`fx_tf_snapshot_loop.sh`を同時に起動する旧方式、または
launchdとraw loopの併走は禁止です。開発機で一時的にどちらかを使う場合も、正規ログと
分離した作業ディレクトリで単独実行し、終了後にwriterが残っていないことを確認します。

### MFE/MAE/TP/SL期待値監視ランナー

`tools/trade_outcome_monitor.py` は cron/CI/dashboard 向けの運用コマンドです。判断
ジャーナルを MFE/MAE/TP/SL で採点し、期待値・サンプル数・経路品質をチェックしたうえで、
改善候補レジストリ、TP/SL候補paper再採点、承認済みTP/SLの自動停止、ダッシュボード用
監視JSONを1回で更新します。候補の`paper`/approval表記はoffline研究内の比較状態であり、
institutional model stageやbroker paper実行を意味せず、live反映も行いません。

```bash
python3 tools/trade_outcome_monitor.py
python3 tools/trade_outcome_monitor.py --journal logs/briefing_journal.jsonl --health-require-sample
python3 fx_briefing.py --approve-trade-candidate <candidate_id> --trade-approval-actor <name>
```

主な出力は `logs/trade_outcome_monitor.json`、`logs/trade_improvement_candidates.json`、
`logs/trade_outcome_report.json`、`logs/trade_variant_report.json` です。ヘルスチェックが
失敗した場合は終了コード1を返しますが、監視JSONは書き出すためダッシュボード側で状態を
確認できます。

副産物として `research_pack/upcoming_events.csv`(最新スナップショット、毎回上書き)と
`research_pack/event_history.csv`(追記アーカイブ)を書き出します。いずれも
`fx_backtester` の `--events` にそのまま渡せる形式で、分析時のイベント回避と
バックテストのイベント回避が同じデータを共有します。event_history.csv は実行のたびに
未観測のイベント・改定分だけを `recorded_at` 付きで蓄積する簡易 point-in-time 記録で、
運用を続けるほど過去期間のイベント回避を実カレンダーで再生できるようになります
(`--no-event-archive` で無効化)。

注意: これは意思決定支援であり、収益を保証する予測ではありません。イベント
回避・リスク管理・複数ソースの突き合わせという「プロセス」を自動化するものです。

## CSV形式

価格データは統合CSVまたは通貨ペア別CSVに対応します。必須列は以下です。

```csv
timestamp,symbol,open,high,low,close
2024-01-01 00:00:00,EURUSD,1.1000,1.1010,1.0990,1.1005
```

経済指標CSVは任意です。`currency` または `symbol` が対象通貨ペアに一致する場合、指定分数の前後で新規エントリーを停止します。ストップや決済はリスク削減のため実行します。

```csv
timestamp,currency,symbol,impact,name
2024-01-05 22:30:00,USD,,high,Nonfarm payrolls
```

## 実行方法

サンプルデータを生成します。

```bash
python3 examples/generate_sample_data.py
```

移動平均クロスのバックテスト:

```bash
python3 -m fx_backtester.cli backtest \
  --data examples/sample_prices.csv \
  --events examples/sample_events.csv \
  --strategy ma_cross \
  --initial-cash 100000 \
  --risk-per-trade 0.01 \
  --max-open-positions 2 \
  --trading-start 07:00 \
  --trading-end 23:00 \
  --output-trade-log trade_log.csv \
  --output-equity equity.csv \
  --output-metrics metrics.json
```

Donchianブレイクアウト:

```bash
python3 -m fx_backtester.cli backtest \
  --data examples/sample_prices.csv \
  --strategy donchian \
  --param entry_window=40 \
  --param exit_window=20
```

RSI平均回帰:

```bash
python3 -m fx_backtester.cli backtest \
  --data examples/sample_prices.csv \
  --strategy rsi_mean_reversion \
  --param low_threshold=30 \
  --param high_threshold=70
```

AIロジスティック戦略:

```bash
python3 -m fx_backtester.cli backtest \
  --data examples/sample_prices.csv \
  --strategy ai_logistic \
  --param min_train_bars=300 \
  --param retrain_interval=24 \
  --param long_threshold=0.55 \
  --param short_threshold=0.45
```

`ai_logistic` は、リターン、モメンタム、SMA乖離、ボラティリティ、ATR、RSI、ローソク足位置、スプレッド比率から特徴量を作り、各時点で既に確定している過去データだけでロジスティックモデルを再学習します。シグナルは次バー方向の確率が `long_threshold` 以上ならロング、`short_threshold` 以下ならショート、それ以外はフラットです。外部AI APIやニュース感情分析はまだ使いません。

ウォークフォワード検証:

```bash
python3 -m fx_backtester.cli walk-forward \
  --data examples/sample_prices.csv \
  --events examples/sample_events.csv \
  --strategy ma_cross \
  --train-bars 500 \
  --test-bars 100 \
  --max-params 20
```

公開FX履歴データ向けの研究パックを生成:

```bash
python3 -m fx_backtester.cli research-pack --output-dir research_pack
```

ローカルのDeep Researchレポートを研究パックに取り込む場合:

```bash
python3 -m fx_backtester.cli research-pack \
  --output-dir runs/deep_research_max/research_pack \
  --source-report /Users/takahashifuuki/Downloads/deep-research-report.md
```

生成されるファイル:

- `public_fx_sources.csv`: 公開データ源、粒度、用途、注意点
- `major_fx_events.csv`: SNBショック、Brexit、COVID流動性ストレス、BOE介入、円介入などの高インパクトイベント
- `research_max_config.json`: 調査ベースの最大構成プリセット
- `deep_research_max_config.json`: Deep Researchレポート由来の最大構成プリセット
- `deep_research_decisions.csv`: レポートから抽出した市場・戦略・リスク・検証方針
- `research_notes.md`: 公開データを使う際の分析手順

データQA:

```bash
python3 -m fx_backtester.cli qa-data \
  --data examples/sample_prices.csv \
  --expected-frequency h \
  --output data_qa.csv
```

TradingView Alert Webhookを受信する場合:

```bash
export TRADINGVIEW_WEBHOOK_SECRET="change-me"

python3 -m fx_backtester.cli tradingview-webhook \
  --host 127.0.0.1 \
  --port 8080 \
  --secret-env TRADINGVIEW_WEBHOOK_SECRET \
  --output runs/tradingview_alerts.jsonl
```

TradingViewのWebhook URLには、公開HTTPS URLを指定します。ローカル検証ではngrokやCloudflare Tunnelなどで `http://127.0.0.1:8080/webhook/tradingview` を外部公開してください。TradingViewのAlertメッセージ例:

```json
{
  "secret": "change-me",
  "exchange": "{{exchange}}",
  "ticker": "{{ticker}}",
  "time": "{{time}}",
  "timeframe": "{{interval}}",
  "action": "{{strategy.order.action}}",
  "price": "{{strategy.order.price}}",
  "contracts": "{{strategy.order.contracts}}",
  "order_id": "{{strategy.order.id}}"
}
```

受信したAlertはJSONLで保存します。これは実注文ではなく、paper/forward証跡を作るための連携です。

研究パックを使った最大構成のバックテスト:

```bash
python3 -m fx_backtester.cli backtest \
  --data examples/sample_prices.csv \
  --events research_pack/major_fx_events.csv \
  --strategy ma_cross \
  --preset deep-research-max \
  --output-trades trades.csv \
  --output-equity equity.csv \
  --output-metrics metrics.json
```

商用運用向けに監査可能な成果物ディレクトリを作る場合:

```bash
python3 -m fx_backtester.cli backtest \
  --data examples/sample_prices.csv \
  --events research_pack/major_fx_events.csv \
  --strategy ma_cross \
  --preset deep-research-max \
  --expected-frequency h \
  --output-dir runs/production_candidate

python3 -m fx_backtester.cli audit-run --run-dir runs/production_candidate
```

商用判断に必要な分析パックを追加する場合:

```bash
python3 -m fx_backtester.cli walk-forward \
  --data examples/sample_prices.csv \
  --events research_pack/major_fx_events.csv \
  --strategy ma_cross \
  --preset deep-research-max \
  --train-bars 500 \
  --test-bars 100 \
  --max-params 20 \
  --output-summary runs/production_candidate/walk_forward_summary.csv

python3 -m fx_backtester.cli analyze-run \
  --run-dir runs/production_candidate \
  --monte-carlo-paths 2000 \
  --ruin-threshold-pct 30
```

2025年から2026年6月29日までの期間で月利8%を目標に検証する場合:

```bash
python3 -m fx_backtester.cli backtest \
  --data path/to/prices_2025_2026.csv \
  --events research_pack/major_fx_events.csv \
  --strategy ma_cross \
  --preset deep-research-max \
  --start-date 2025-01-01 \
  --end-date 2026-06-29 \
  --monthly-profit-target 8 \
  --expected-frequency h \
  --output-dir runs/monthly_8_target

python3 -m fx_backtester.cli walk-forward \
  --data path/to/prices_2025_2026.csv \
  --events research_pack/major_fx_events.csv \
  --strategy ma_cross \
  --preset deep-research-max \
  --start-date 2025-01-01 \
  --end-date 2026-06-29 \
  --output-summary runs/monthly_8_target/walk_forward_summary.csv

python3 -m fx_backtester.cli analyze-run \
  --run-dir runs/monthly_8_target \
  --monthly-target-return 8 \
  --walk-forward-summary runs/monthly_8_target/walk_forward_summary.csv \
  --monte-carlo-paths 2000 \
  --ruin-threshold-pct 30
```

`--monthly-profit-target 8` は月初資産比で+8%に到達した後、その月の新規リスクをロックします。`close_positions_on_portfolio_stop` が有効な通常設定では、保有ポジションも次の約定可能バーで `monthly_profit_target` 理由として決済します。これは利益を保証するものではなく、到達後に利益を守るための運用停止条件です。

`--output-dir` は以下をまとめて出力します。

- `manifest.json`: 入力ファイルhash、実行環境、戦略、設定、品質ゲート
- `config.json`: 実行時設定
- `data_qa.csv`: データ品質検査
- `trade_log.csv`: 約定検証用ログ
- `equity_curve.csv`: 資産曲線
- `metrics.json`: 指標

`audit-run` は成果物欠損、QA失敗、trade log必須列欠損、spread/slippage不正、metricsとtrade logの不整合を検出し、失敗時は終了コード1を返します。

`analyze-run` は既存のrunディレクトリに以下を追加します。

- `index.html`: 商用検証ダッシュボード
- `commercial_readiness.json`: 販売・運用前の必須ゲート
- `pair_performance.csv`: ペア別成績
- `monthly_pnl.csv`: 月次損益
- `monthly_target.csv`: 月利ターゲットの達成/未達、不足率、余剰率
- `drawdown_periods.csv`: DD期間と回復日数
- `period_performance.csv`: 年、四半期、月ごとの成績
- `oos_summary.json`: インサンプル / アウトオブサンプル分割
- `cost_sensitivity.csv`: スプレッド・スリッページ感度
- `pnl_breakdown.csv`: 総損益、コスト控除前損益、スプレッド損、手数料、スリッページ、スワップ
- `pnl_by_side.csv`: ロング / ショート別損益
- `pnl_by_hour.csv`: 時間帯別損益
- `pnl_by_pair.csv`: 通貨ペア別損益
- `pnl_by_strategy.csv`: 戦略別損益
- `pnl_breakdown_summary.json`: 損益分解のJSONサマリー
- `strategy_diagnosis.json`: マイナス原因、OOS不足、ペア偏り、時間帯偏り、baseline劣後などの自動分類
- `usable_segments.csv`: ペア、時間帯、long/short、戦略別に残す候補とブロック候補
- `baseline_comparison.csv`: 現戦略、flat、buy-and-hold、random baselineとの比較
- `paper_backtest_diff.json`: forward/paper trade logがある場合のバックテストとの差分
- `lot_control_summary.json`: ロット制御の確認
- `monte_carlo_summary.json` / `monte_carlo_quantiles.csv`: 破産確率と分布
- `forward_test_summary.json`: フォワードテスト確認

重要: `forward_test_summary.json` はフォワードまたはペーパートレードのログがない限り未完了になります。バックテストだけで `commercial_ready=true` にはしません。

損益の名称は以下で統一します。

- `gross_profit`: 勝ちトレードの利益合計。定義上、通常はマイナスになりません。
- `gross_pnl`: 約定価格ベースの損益。スプレッドとスリッページは反映済み、手数料は控除前です。負けていればマイナスになります。
- `pre_cost_pnl`: スプレッド、スリッページ、手数料、スワップを戻した推定コスト控除前損益です。
- `total_net_pnl`: すべてのモデル化済みコスト控除後の最終損益です。
- `swap`: 現時点では `trade_log.csv` に `swap_usd` または `swap` がある場合だけ反映します。ない場合は0として明示します。

## 商用検証の追加制御

CLIで実行する戦略は、標準で生の `ma_cross` / `donchian` / `rsi_mean_reversion` をそのまま使わず、共通フィルタを重ねます。

- Regime filter: 長期SMA、SMA傾き、ATRパーセンタイルで不利な相場を除外します。
- AI strategy: `ai_logistic` は過去バーだけでローリング学習します。モデル自体も過剰最適化しやすいため、Walk-forward、OOS、Monte Carlo、forward/paper差分を必須の判断材料にします。
- Entry-only no-trade filter: 指定時間帯と異常スプレッド時の新規エントリーだけを止めます。既存ポジションの決済は妨げません。
- Currency exposure: `--max-currency-exposure 2.0` のように通貨別USD換算エクスポージャー上限を設定できます。
- Time-varying cost: `--spread-time-multiplier 21=2.0`、`--slippage-time-multiplier 21=2.0` のように時間帯でコストを変動させます。価格CSVに `spread_pips` または `spread_price` がある場合、CSVの値を優先します。
- Walk-forward purge/embargo: `--purge-bars` と `--embargo-bars` で学習区間とテスト区間の間に空白を置きます。
- Date range: `--start-date 2025-01-01 --end-date 2026-06-29` で検証期間を限定できます。日付だけの終了指定は、その日の終わりまで含めます。
- Monthly profit target: `--monthly-profit-target 8` または `--monthly-profit-target 0.08` で月初比+8%到達後の新規リスクを停止します。
- Monthly target gate: `analyze-run --monthly-target-return 8` は全評価月が月利8%以上かを `monthly_target.csv` と `commercial_readiness.json` に出力します。
- Paper差分: `analyze-run --forward-trades forward_trade_log.csv` を渡すと、paper/forwardとbacktestの損益、期待値、勝率、平均spread/slippage差分を出します。

フィルタを外して純粋な戦略ロジックだけを検証したい場合:

```bash
python3 -m fx_backtester.cli backtest \
  --data examples/sample_prices.csv \
  --strategy ma_cross \
  --disable-regime-filter \
  --disable-signal-no-trade-filter
```

`--preset research-max` は、公開されている価格履歴が実約定履歴ではない前提で、リスク0.5%、最大日次損失1.5%、最大レバレッジ5倍、同時保有2件、イベント前後の長めの新規停止、週末新規停止、保守的なスプレッド/スリッページを適用します。個別オプションを指定した場合は、その指定がプリセットより優先されます。

`--preset deep-research-max` は、Deep Researchレポートの推奨に合わせて、単一トレード0.5%、日次損失1.5%、週次損失3%、月次DD6%、ハードDD10%、最大レバレッジ5倍、G10コアペア、イベント前後の新規停止、週末停止、Walk-Forward検証を前提にします。

## 出力指標

CLIはJSONで以下を出力します。

- `max_drawdown_pct`: 最大ドローダウン
- `max_drawdown_usd`: 最大ドローダウン金額
- `win_rate`: 勝率
- `expectancy_usd`: 1トレードあたり期待値
- `expectancy_r`: 1トレードあたりR倍数期待値
- `gross_profit` / `gross_loss`: 総利益、総損失
- `profit_factor`: Profit Factor
- `average_win` / `average_loss`: 平均利益、平均損失
- `sharpe_ratio`: Equity CurveリターンのSharpe Ratio
- `sortino_ratio`: downside deviationだけを分母に使うSortino Ratio
- `downside_deviation`: Equity Curveリターンの下方偏差
- `calmar_ratio`: 年率リターン ÷ 最大DD
- `recovery_factor`: 純損益 ÷ 最大DD金額
- `median_trade_usd` / `median_r`: 1トレード損益の中央値（USD / R倍数）
- `expected_shortfall_r_05`: R倍数損益の下位5% Expected Shortfall（5% ES R）
- `longest_loss_streak`: 最長連敗数
- `average_holding_hours` / `median_holding_hours`: 平均/中央値の保有時間
- `total_fees_usd`: 手数料合計（USD）
- `round_trip_turnover_units`: entryとexitを含むround-trip取引数量
- `exposure_pct`: ポジション保有バー比率

## 約定モデル

バックテストは現実的な足確定後シグナルを前提にしています。

- シグナルはローソク足の確定後にだけ発生します。
- シグナル発生足の終値では約定しません。
- 成行注文は次足の始値を期待価格として約定します。
- 買いはAsk、売りはBidで約定します。
- `spread_pips` 列が価格CSVにある場合は、その値をpipsとして必ず使います。
- `spread_price` 列が価格CSVにある場合は、価格差を対象ペアのpip sizeで割ってpipsに変換します。
- `spread` 列は後方互換のためpipsとして扱います。新しいCSVでは `spread_pips` または `spread_price` を使ってください。
- 価格CSVにspread列がない場合も、`ExecutionConfig.spread_pips` またはCLIの `--spread-pips` による正のスプレッドを使います。
- `slippage_pips` は必須で、常に不利方向に加算します。CLIでは `--slippage-pips EURUSD=0.2` のように指定します。
- スプレッドまたはスリッページが0以下の場合はバックテストを拒否します。
- EUR/JPYなどUSDを含まないペアは、USD/JPYなどの換算レート系列を同時に渡すか、`--conversion-rate USDJPY=150.0` のように静的換算レートを指定します。
- 利確指値は高値・安値の単純なタッチだけでは約定扱いにしません。
- 同一足で利確と損切りの両方に到達した場合は、保守的に損切りを優先します。
- 現時点のエンジンはswap/rollover、週明けギャップ、祝日やロールオーバー時間帯の流動性低下を自動では加算しません。スイング以上の検証では外部コスト列または分析側の調整が必要です。

`trade_log.csv` には以下の約定検証用カラムを出力します。
取引が0件でもヘッダー付きCSVとして出力されます。

- `signal_time`
- `order_time`
- `fill_time`
- `side`
- `expected_price`
- `fill_price`
- `spread_pips`
- `slippage_pips`
- `order_type`
- `exit_reason`

## 商用運用ゲート

通常の `backtest` と `walk-forward` は実行前に以下を検証します。

- OHLC列、時系列順、重複、OHLC整合性
- 2本以上の足があり、次足始値約定が可能であること
- 通貨ペアごとの正のspread設定、`spread_pips` 列、または `spread_price` 列
- 通貨ペアごとの正のslippage設定
- 初期資金、リスク率、レバレッジ、停止条件、同時保有数などの設定範囲

この検証に失敗した場合、バックテストは実行しません。

## 主要CLIオプション

リスク・約定・取引時間の主要な設定はCLIから上書きできます。

```bash
--risk-per-trade 0.01
--risk-cap 0.01
--max-daily-loss 0.02
--max-weekly-loss 0.03
--max-monthly-drawdown 0.06
--hard-drawdown 0.10
--min-stop-pips 5
--max-leverage 10
--max-position-units 100000
--allow-fractional-units
--max-open-positions 2
--cooldown-bars-after-stop 3
--commission-per-million 30
--fixed-fee 0
--minimum-fee 0
--spread-pips EURUSD=0.6
--slippage-pips EURUSD=0.1
--conversion-rate USDJPY=150.0
--trading-start 07:00
--trading-end 23:00
--blocked-weekday sat,sun
--no-close-on-daily-stop
--no-close-on-portfolio-stop
--keep-open-on-end
--preset research-max
--preset deep-research-max
```

## リスク管理

- 1回のリスクは資金の1%以内に強制キャップします。`--risk-per-trade` に1%超を渡しても、サイズ計算では1%に丸めます。
- キャップ値は `--risk-cap` で変更できます。初期値は1%です。
- サイズ計算ではストップ幅に加え、推定往復スプレッド、スリッページ、手数料をリスクに含めます。
- `--min-stop-pips`、`--max-leverage`、`--max-position-units`、`--max-open-positions` でサイズと同時保有を制限できます。
- `--trading-start` / `--trading-end` と `--blocked-weekday` で新規エントリーの時間帯を制限できます。保有中のストップや決済は実行します。
- ストップ後の即時再エントリーを避けたい場合は `--cooldown-bars-after-stop` を指定します。
- 1日の損失が日初資金の2%に到達したら、その日は新規エントリーを停止します。
- `close_positions_on_daily_stop=True` のため、日次停止に到達した時点で保有ポジションも決済します。
- `--max-weekly-loss`、`--max-monthly-drawdown`、`--hard-drawdown` で、週次・月次・全期間ピーク比の停止条件も設定できます。

## 過学習を避ける設計

- パラメータ探索は小さなグリッドのみを想定しています。
- ウォークフォワードの組み合わせ数は `max_parameter_combinations` で制限し、CLIでは `--max-params` の既定値を20にしています。
- 学習区間で選んだパラメータを、隣接する未使用のテスト区間だけに適用します。
- 勝率だけを目的関数にせず、Sharpe、期待R、Profit Factor、DDを組み合わせた簡易スコアを使います。
- 試行ログ (`fx_backtester/trial_log.py`): `WalkForwardValidator` に `trial_logger=` を
  渡すと、探索の全試行(パラメータ・指標・リターン系列)を `runs/trial_logs/<run_id>/` に
  記録します(trials.jsonl / returns_matrix.csv / run.json)。探索履歴の監査証跡であり、
  下記検定の入力です。
- 過剰最適化の統計検定 (`fx_backtester/overfitting.py`、scipy非依存):
  - PBO (CSCV; Bailey et al. 2015) — 時系列をブロック分割した全組み合わせで
    「ISで最良の試行がOOSで中央値未満に沈む確率」を測ります。0.5でIS順位に予測力ゼロ。
  - Deflated Sharpe Ratio (Bailey & López de Prado 2014) — 探索回数Nのまぐれで
    達成しうる期待最大Sharpeを控除した上で、観測Sharpeが本物である確率を
    歪度・尖度込みで出します。
  - PBO >= 0.5 / DSR < 0.95 は過剰最適化の警告として扱います。

  ※ 発注戦略パラメータを自動最適化・承認していた `auto_optimize.py` /
  `promote_params.py` は自動売買の取りやめに伴い削除済みです（→ [SYSTEM_OVERVIEW](SYSTEM_OVERVIEW.md)）。
  上記の試行ログと過剰最適化検定は `fx_backtester` の walk-forward 検証内で引き続き利用できます。

## ネット上のFX履歴データを使う際の前提

- 公開されているFX履歴の多くは約定履歴ではなく、ブローカーや公的機関の価格・レート履歴です。
- OTC市場には株式のような単一の統合テープがないため、データ源、タイムゾーン、Bid/Ask、欠損、夏時間を必ず確認します。
- Federal Reserve H.10のような公的日次データは長期検証と基準値確認、DukascopyやHistDataのような公開Intradayデータは戦略検証に向いています。
- SNBショック、Brexit、COVID流動性ストレス、英国ギルト市場混乱、円介入のようなイベントは、通常期間とは分けてストレス検証します。

## テスト方法

```bash
python3 -m pytest
```

テストではCSV読み込み、経済指標マスク、日次損失停止、ウォークフォワードの探索上限を確認しています。

## 永続的なanalysis-only境界

本リポジトリは**分析・履歴研究・offline simulation・shadow判断・Discord通知専用**です。
ブローカー注文、ポジション変更、口座リスク変更、paper/live broker executionは恒久的に
対象外であり、旧`trader/`、executor、order client、自動パラメータ→注文配線を復元しません。
ブローカー執行を研究する場合は、別リポジトリで独立した権限・レビュー・運用境界を設けます。

## 改善すべき点

- USD以外の口座通貨、クロス円以外の換算レート対応
- スワップポイント、休日、流動性低下時間帯のコストモデル
- 約定拒否、部分約定、API遅延、再接続のシミュレーション
- TickまたはBid/Askデータによるより厳密な検証
- 経済指標カレンダーの自動取得と改定値管理
- 複数戦略・複数通貨ペアの相関リスク制御
