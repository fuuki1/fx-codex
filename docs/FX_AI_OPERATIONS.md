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
tail -n 20 logs/fx_integrated_briefing.log
tail -n 5 logs/briefing_tf_journal.jsonl
```

正規構成はlaunchdの `com.fx-codex.snapshot`（5分・唯一の価格writer）、`com.fx-codex.briefing`（5分境界・canonical時間足別判断/通知）、`com.fx-codex.virtual-portfolio`（3分・仮想口座受付）、`com.fx-codex.virtual-portfolio-postprocess`（5分・canonical損益export）、`com.fx-codex.virtual-portfolio-report`（5分・日次通知）、`com.fx-codex.virtual-portfolio-counterfactual`（5分・任意比較）、`com.fx-codex.health`（5分）です。受付・損益export・通知・反実仮想を別process/別lockにし、任意の後段停止で300秒の受付期限や将来のcanonical成果出力を失わないようにします。旧融合判断はmanual observation-onlyで定期実行せず、canonical仮想口座・鮮度SLO・方針更新に使いません。`fx_briefing_loop.sh` / `fx_tf_snapshot_loop.sh`、直接実行、cron writerが見つかった場合は競合です。自動killせず、プロセス一覧・cron・plist・ログを保存してから人間が停止対象を確認します。

定時時間足判断は旧fusion由来の承認済みTP/SL registryを適用せず、定時monitorもregistryを
更新しません。decision outcome/feedbackの正規writerは`com.fx-codex.monitor-diagnostics`だけです。
manual fusionは別latestへ観測記録するだけで、canonical outcome/feedback/policyを更新しません。
`com.fx-codex.monitors`は時間足別のdirection/TP・SL/maximization cacheだけを15分ごとに更新し、重いdiagnostic scanを同じ排他区間で実行しません。定時briefingは
全履歴を読み直さず、この3cacheの契約・30分鮮度・同一生成時刻とdecision feedbackの30分鮮度を
検証します。不一致時は再計算へfallbackせずneutralに停止するため、まず
`logs/timeframe_learning_monitor.json`と`logs/launchd/monitors.err.log`を確認してください。
3cacheとmanifestは同一bundle lock下で公開・読込し、`com.fx-codex.monitors`だけがcanonical cacheを書きます。
readerは5秒でlock待ちを打ち切り、cache利用不可としてその回の判断をneutralで記録します。
writerが複数fileのreplace途中で異常終了した場合も世代/hash検証でfail-closeし、次のmonitor成功まで
旧cacheへ自動復帰はしません。これは誤った世代を使わないためのavailability上の制約です。
`tools/maximization_monitor.py`は別名のdiagnostic artifactだけを出力します。
health monitorもmanifestのmtimeだけでなく、3cacheのsize/hash/contract/generated_atを照合し、
parse可能でも世代が混在したbundleをCRITICALとして通知します。
monitorは生成時刻より未来のdecision/price、hash不一致、時刻順序・provenance不足を学習から除外し、
入力digest・最大availability・3cache hashをmanifestへ残します。briefing側もmanifestと実ファイルの
size/hashを照合します。判断時の価格読込は末尾128MiB上限で、履歴成長に対して無界ではありません。

時間足別Discord通知とcanonical仮想口座候補はUSDJPY/EURUSDです。旧融合/GBDT artifactは
手動研究と監査用に保持しますが、定期更新・canonical入力・自動方針変更には使いません。

主要ジャーナル書込み失敗は終了コード4、Discord通知だけの失敗は終了コード5です。
通知失敗時も保存済み判断は保持しますが、定期wrapperは後続の融合処理を開始しません。

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

旧融合判断はmanual observation-onlyのためcanonical freshness reportの対象外です。
学習ダッシュボードの融合最終時刻と`fx_fusion_capture.log`はhistorical evidenceであり、
更新停止を障害または手動writer起動理由として扱いません。

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

## 6. 期待値ガードの根拠更新(シャドー反実仮想)

期待値ガードがblockした判断は `direction=neutral` で記録され採点対象から消えるため、
実績だけを根拠にするとガードのサンプルが増えず、blockが恒久化する(学習飢餓。
2026-07-17〜07-21の実機で根拠n=28のまま全新規判断が凍結した実例あり)。

このためガード根拠(`fx_briefing` の `guard_evidence_summary`)は
「実績 + expectancy_guard単独見送り行のシャドー計画(反実仮想)」で毎時更新する。

- 反実仮想は判断時に凍結記録された値のみから合成する(`journal.counterfactual_guard_entries`):
  ゲート前の `analysis_direction` / `analysis_conviction` と、`shadow_predictions` の
  `fusion_raw` に記録済みのSL/TP。事後の再計算はしない(PIT安全)。記録が欠けた行は除外(fail-closed)。
- `event_window` / `low_data_quality` 等が併発した行は含めない。ガードが無くても
  見送っていた行であり、根拠に混ぜると反実仮想が汚染されるため。
- 推奨(`direction`)はガード判定に従いneutralのまま。反実仮想がcanonical bid/ask
  scorerで採点され、現行label/cost契約と十分なeffective sampleを満たした場合だけ
  block解除の根拠にできる。legacy close/OHLC反実仮想は経路診断に限り、解除・boost・
  approvalへは使わない。
- 監視: 反実仮想の量は `quality.flags.expectancy_guard_counterfactual`、学習側は
  `briefing_learning.json` の `counterfactual_evaluated` に出る。改善候補レジストリと
  期待値レポートは従来どおり実績のみを使う。

## 7. USDファクター整合監査(観測専用)

同一実行で提示された判断群のUSD観の内部矛盾(例: USDJPY long=USD強 ∧
EURUSD/GBPUSD long=USD弱)を `fx_intel/usd_coherence.py` が監査する。
2026-07-16に3ペア同時longがUSD全面高で相関全敗した実測が動機。

- 各判断行の `gate_trace` に `gate: usd_factor_coherence / status: observed` が付く。
  recommended(ゲート後の推奨)とanalysis(ゲート前の分析)の2トラックで、
  スタンス(+1=USD強/-1=USD弱)・矛盾有無・確信度加重の少数派(`would_dampen`)を記録する。
- **観測専用**: 方向・確信度は変更しない(`applied: false`)。ガードで推奨が全て
  neutral化されている期間もanalysis側で観測が続く。
- 矛盾検出時はDiscord警告「🧭 USD観の内部矛盾を検出」が出る。
- 減衰(`DAMPEN_FACTOR_PROPOSAL`)の有効化は、蓄積した観測での期待値改善が
  独立レビューで確認されてからの別PR。レトロ検証では7/16(推奨9実行)と
  7/17・7/20(分析3+14実行、少数派=GBPUSD)を検出し、GBPUSDシャドー全敗5件は
  すべて検出ウィンドウ内だった。
