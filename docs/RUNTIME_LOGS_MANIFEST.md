# Runtime logs — 学習・監視の実行時ログ置き場

`logs/` の実データは **Git管理しない**(`.gitignore` の `logs/*`)。
リリース先で `logs/` 全体を正規runtimeディレクトリへのsymlinkにできるよう、
このマニフェスト自体は `docs/` で追跡する。

すべて判断ログ・採点・監視のための **paper相当のローカル記録** であり、
発注系(`trader/`)はこのディレクトリを読まない。

## 判断・学習

| ファイル | 内容 | 書き手 |
| --- | --- | --- |
| `briefing_journal.jsonl` | 融合1判断モードの判断ジャーナル(方向・確信度・SL/TP・特徴量) | `fx_briefing.py` |
| `briefing_learning.json` | 融合モードの学習プロファイル(重み・確信度較正・状態別成績) | `fx_briefing.py` |
| `briefing_tf_journal.jsonl` | 時間足別モード(`--per-timeframe`)の判断ジャーナル | `fx_briefing.py --per-timeframe` |
| `briefing_tf_learning.json` | 時間足別の方向学習cache（世代整合bundleの一員） | `tools/timeframe_learning_monitor.py`のみ |
| `briefing_tp_sl_learning.json` | 時間足別のTP/SL学習cache（同bundle） | `tools/timeframe_learning_monitor.py`のみ |
| `briefing_maximization.json` | 時間足別の期待値最大化cache（同bundle） | `tools/timeframe_learning_monitor.py`のみ |
| `timeframe_learning_monitor.json` | 3cacheの世代・入力watermark・hash manifest（同bundle） | `tools/timeframe_learning_monitor.py`のみ |
| `decision_pipeline_stage_timings.jsonl` | briefing/cache生成/仮想口座のUTC開始終了・monotonic工程時間 | 各正規scheduled wrapper |
| `briefing_tf_prices.jsonl` | OANDA完了済みM5 bid/ask OHLC(短い足の採点用・判断なし) | `fx_tf_snapshot.py` |
| `briefing_horizon_forecasts.jsonl` | `horizon-pit-v1`の9ホライズン予測(本番候補8本+5m恒久shadow) | `fx_briefing.py --horizon-only` |
| `briefing_horizon_learning.json` | symbol×horizonの採点・較正・経験帯・昇格ゲート | `fx_briefing.py --horizon-only` |
| `macro_cache.json` | マクロデータのTTLキャッシュ | `fx_intel/macro.py` |
| `ml_model.json` | ML確率モデル(GBDT)の保存物 | `fx_briefing.py` |
| `promotion_state.json` | 委員のshadow/paper/live昇格状態 | `fx_intel/promotion.py` |

## トレード結果採点・期待値監視(tools/trade_outcome_monitor.py が書く)

| ファイル | 内容 | 書き手 |
| --- | --- | --- |
| `trade_improvement_candidates.json` | 改善候補レジストリ(paper_ready/approved/auto_paused と監査イベント) | 人手review CLIのみ（`trade_outcome_monitor.py --update-registry` または明示approval操作）。定時monitor/fusionはread-only |
| `trade_outcome_monitor.json` | ダッシュボード用の期待値監視スナップショット(ヘルス・候補件数) | `tools/trade_outcome_monitor.py` |
| `trade_outcome_report.json` | MFE/MAE/TP/SL採点の詳細監査レポート | `tools/trade_outcome_monitor.py` |
| `trade_variant_report.json` | TP1/TP2のR倍率候補のpaper再採点レポート | `tools/trade_outcome_monitor.py` |

## 実行ログ(ループスクリプトが書く)

| ファイル | 内容 | 書き手 |
| --- | --- | --- |
| `fx_briefing.log` | 毎時の融合ブリーフィング実行ログ | `fx_briefing_loop.sh` |
| `fx_briefing_tf.log` | 毎時の時間足別ブリーフィング実行ログ | `fx_briefing_loop.sh` |
| `fx_tf_snapshot.log` | 5分ごとの価格スナップショット実行ログ | `fx_tf_snapshot_loop.sh` |
| `fx_horizon.log` | 5分ごとのマルチホライズンshadow実行ログ | `scripts/fx_horizon_once.sh` |

canonical仮想口座受付は`scripts/virtual_portfolio_once.sh`だけが担当する。反実仮想、
canonical outcome exportは`scripts/virtual_portfolio_postprocess_once.sh`、日次Discordは
`scripts/virtual_portfolio_report_once.sh`、反実仮想は
`scripts/virtual_portfolio_counterfactual_once.sh`へ分離し、4者はlockを共有しない。すべてoffline
simulationでありbroker接続はない。

launchd templateは`EnvironmentVariables.PYTHONPATH=__FX_ROOT__`を持ち、manual wrapperも実行時の
repository rootを先頭へ追加する。共有venvのeditable install先はruntime codeの正本にしない。

## 読み手

- `tools/ai_learning_dashboard/server.py` — 全ファイルを読み取り専用で可視化
- `fx_briefing.py` — ジャーナル・学習・レジストリを次回判断への入力に使う
- `tools/learning_capture.py` — Discord送信なしで上記一式を1回分収集するランナー
