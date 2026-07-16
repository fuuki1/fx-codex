# FXシステム全体像

このドキュメントはFX**分析→Discord通知**システムのロジックとデータフローをまとめたものです。
バックテストCLIの詳細なオプション・出力仕様は [README.md](README.md) を参照してください。

> **恒久方針（2026-07-10〜）**: このリポジトリはanalysis-onlyです。
> IBKRへの実発注を行う `trader/` Docker スタック、発注戦略の最適化・承認パイプライン
> （`auto_optimize.py` / `promote_params.py` / `params_gate.py`）、および `strategy_params.json`
> は削除済みです。現在のシステムは**分析に専念し、判断をDiscordへ通知する**ことだけを行います。
> ブローカー執行を扱う場合は、このリポジトリを変更せず、別リポジトリで独立した境界を設けます。

## この文書での「設計」と「観測」

- **正規設計**: Mac mini (`/Users/fuuki/srv/fx-codex`) では、launchdの
  `snapshot`（5分）、`briefing`（5分境界）、`health`（5分）だけを定期実行する。
- **実機観測**: 2026-07-10の読み取り専用監査では、旧launchd/cronの残存、過去の
  多重writer、価格系列の鮮度異常を確認した。リポジトリは観測時点で
  `origin/main`から18コミット遅れていた。この値は現在状態の保証ではなく、移行直前に
  `fetch`後のSHAと遅延数を再確認する。
- 正規設計を記述しているからといって、実機への導入完了を意味しない。移行判断は
  [運用Runbook](docs/OPERATIONS_RUNBOOK.md)の証跡と実機の一次情報に基づく。

## 全体構成

```
┌──────────────────────────────────────────────┐
│ 開発機 (~/Desktop/fx-codex)                     │
│  - バックテスト・分析ロジックの開発/検証            │
│  - --dry-run中心の手動確認                       │
└──────────────────────┬───────────────────────┘
                       │ レビュー済みSHAのみ
                       ▼
┌──────────────────────────────────────────────┐
│ Mac mini (/Users/fuuki/srv/fx-codex)           │
│  launchd snapshot 5分 ──▶ 価格系列（唯一のwriter） │
│  launchd briefing 5分境界 ─▶ 判断/学習/定期通知   │
│  launchd health   5分 ──▶ 鮮度監視/運用通知       │
└──────────────────────────────────────────────┘
```

- **バックテスト基盤** (`fx_backtester/`): イベント駆動型のバックテストエンジンと商用検証ダッシュボード。
- **分析・通知** (`fx_intel/` ＋ ルート直下スクリプト): ニュース×経済指標×テクニカル×マクロを統合し、
  複数AI委員会で複合スコアを作り、正規運用では**5分境界の統合ブリーフィング**をDiscordへ送る。
- 自動売買・発注は行わない。すべては**意思決定支援と記録**であり、実注文は出さない。

---

## 1. バックテスト基盤 (`fx_backtester/`)

イベント駆動型のバックテストエンジン。詳細なCLIオプションは [README.md](README.md) 参照。

```
CSV価格データ ─▶ data.py(読込・QA) ─▶ strategies/*.py(シグナル生成)
                                            │ target_position, stop_distance
                                            ▼
                                    engine.py(イベントループ)
                                            │
                          ┌─────────────────┼─────────────────┐
                          ▼                 ▼                 ▼
                    execution.py       risk.py           models.py
                  (次足始値約定,      (1%リスクキャップ,   (Instrument/
                   spread/slippage)   日次2%損失停止)      Position/Trade)
                          │
                          ▼
                    metrics.py (Sharpe/PF/DD/期待値)
                          │
                          ▼
                    artifacts.py / analysis.py (成果物・legacy分析ダッシュボード)
```

- **戦略は`target_position`（-1/0/1）と`stop_distance`だけを返す** シンプルなインターフェース（`strategies/base.py`）。
- 実装済み戦略:
  - `moving_average_cross.py` — fast/slow MA + ATRストップ
  - `donchian_breakout.py` — 高値・安値のブレイクアウト
  - `rsi_mean_reversion.py` — RSIの逆張り
  - `ai_logistic.py` — リターン/モメンタム/SMA乖離/ボラティリティ等を特徴量にしたロジスティック回帰。各時点で確定済みの過去データだけでローリング再学習（未来情報のリークを防ぐ設計）
  - `baselines.py` — buy&hold等、比較対象のベースライン
- **約定モデル**: シグナルは足確定後にのみ発生し、成行は次足始値へ設定済みspread/slippageを不利方向に加えた価格で約定する。これはbid/ask履歴のproxyであり、実quote replayではない。スプレッド・スリッページが0以下だとバックテスト自体を拒否する。
- **過学習対策**: walk-forwardのパラメータ組み合わせ数を`--max-params`（既定20）で制限、学習区間で選んだパラメータを隣接する未使用テスト区間にのみ適用、勝率単体ではなくSharpe/期待R/PF/DDを組み合わせたスコアで評価。さらに`trial_log.py`（探索全試行の記録）と`overfitting.py`（PBO/CSCVとDeflated Sharpe Ratioの統計検定、scipy非依存）を備え、`WalkForwardValidator`にも`trial_logger=`で試行記録を差し込める。
- `validation.py`/`qa.py`にデータ品質検証（OHLC整合性、時系列順、重複、spread/slippage設定の妥当性）があり、失敗時はバックテストを実行しない。これは入力QAであり、それだけで商用・昇格適格性を証明しない。

---

## 2. 分析・通知システム (`fx_intel/`, ルート直下スクリプト)

Mac miniの正規運用は`~/Desktop`ではなく`/Users/fuuki/srv/fx-codex`からlaunchdで実行する。
手動の`--signal-board`/`fx_briefing_loop.sh`は開発・一時確認用であり、正規サービス、cron、
旧plistと同時に動かさない。特に同一の判断ジャーナルへ複数経路から追記してはいけない。

### 2-1. `fx_briefing.py` + `fx_intel/` — 正規の統合通知

ニュース×経済指標×テクニカル×マクロを統合した、機関投資家のモーニングブリーフィングを模したDiscord通知。

```
                         テクニカル委員 ──┐
fx_intel/technicals.py     (TV 4時間足     │
  ＋MAクロス               ＋MA一致)        │
                         ニュース委員 ──────┤
fx_intel/analyst.py        (自前分析エンジン) │  委員会が重み付き平均で
  (Claude非依存の既定)                       ├─▶ 複合スコア ─▶ リスク
fx_intel/macro.py        マクロ委員 ─────────┤    (fx_intel/          オフィサー
  (任意PIT COT・DXY・VIX・金利) (非影響shadow固定)  │     committee.py)     (決定論ゲート)
                         ML委員 ────────────┘         │                 │
fx_intel/ml.py             (GBDT確率モデル)            │                 ▼
  (gbm.py＝依存ゼロGBDT)                                │         方向・確信度・SL/TP
      ▲                    fx_intel/promotion.py ──────┘
fx_intel/calendar.py       (legacy実績をshadow診断)
  (ForexFactory)
```

- **SL/TPの距離**は保守的な固定値（MA 20/100・ATR×2.5）で算出する。発注はしないため、あくまで判断の目安として提示する。
- **複数AI委員会** (`fx_intel/committee.py`): 役割の異なる4委員が意見を出し、シンセサイザーが重み付き平均で複合スコアを作り、**リスクオフィサー(`build_trade_plan`の決定論ゲート=休場・イベント窓・データ品質・確信度上限)が常に拒否権を持つ**。「アナリストの総意をリスク管理者が却下できる」機関投資家デスクの構造をコードで表現。追加委員が居なければ従来のtech55%/news45%合成と完全一致(後方互換)。
- **自前分析エンジン** (`fx_intel/analyst.py`): 「Claude級の分析AIを外部API非依存で」の要件に対する回答。汎用LLMは再現できないが、FXヘッドライン解釈という狭タスクに特化した決定論エンジンを実装。**否定の理解**(「rules out rate hike」は反転)・**ヘッジ割引**(「may/speculation」は×0.7)・**強調増幅**(「sharply/soars」は×1.3)・**鮮度減衰**(半減期12時間)・**ソース信頼度**・**テーマ抽出**(政策/インフレ/雇用/景気/地政学)・**合意度×物量の確信度**を備え、実効スコア=バイアス×確信度でClaude経路と同じ契約。同じ入力から必ず同じ判断=監査可能。
- **センチメント序列** (`fx_intel/sentiment.py`): Claude API(`ANTHROPIC_API_KEY`があれば上乗せ) → **自前分析エンジン(既定)**。旧来の単純語彙カウントは比較検証用に`score_headlines_lexicon`として残置。
- **マクロデータ層** (`fx_intel/macro.py` + `cot_pit.py`): FRED graph CSV（米10年・2年金利、VIX、広義ドル指数）はcurrent-onlyで、revision/first-ingestionを再生できない。legacy CFTC TTL parserも診断用に残るが、canonical briefingは`include_cot=False`で切断し、`--cot-pit-dataset`で明示された監査済みresearch artifactだけをoptional COT入力としてas-of読込する。COT artifactはconfigured-code raw pages、CFTC row ID、local observed revisions、local release sidecarを保持するが、外部認証・accepted licence・実corpus・配備はなく常にpromotion-ineligibleである。Stooqは定数/パーサ候補で現行snapshot取得には未接続。詳細は[Source ledger](docs/research/SOURCE_LEDGER.md)を参照する。
- **GBDT確率モデル** (`fx_intel/gbm.py` + `fx_intel/ml.py`): 依存ゼロの純Python実装。採点済みジャーナルをtrain/tune/calibration/test/未開封lockboxへ時系列分割し、境界前側へ72時間embargoを置く。Platt較正はcalibrationだけでfitし、testではcalibration基準率に対するBrier/logloss改善、AUC 0.55以上、非空特徴重要度を要求する。`usable=False`は委員会に参加しない。ただしlegacy outcome labelとPITデータのend-to-end接続は未完了で、institutional validationの証拠ではない。
- **legacy委員診断** (`fx_intel/promotion.py`): マクロ/ML委員の簡易サンプル数・的中率・ATR proxy期待値・二項片側p値を表示するが、24時間ラベル重複、PIT未証明journal、独立holdout/コスト欠如のため昇格根拠に使わない。このresearch buildでは委員を非影響の**shadow**へ固定し、保存済みpaper/live/未知状態もshadowへfail closedする。
  - institutionalな`research → validated → shadow → paper`の方針/証拠ゲートは`fx_backtester/governance.py`に別実装されているが、現在はend-to-end orchestrationへ未接続である。`--promote-live`は明示的に無効で、発注権限は存在しない。
- **経済指標カレンダー**: ForexFactory公開フィード(`nfs.faireconomy.media`)。429レート制限があるため`logs/calendar_cache.json`に**45分キャッシュ**。
- **イベント回避ロジック**: 高影響イベントの**前120分/後180分**は新規エントリーを強制「様子見」（`research-max`プリセットと同じ窓）。
- **legacy自己採点ループ** (`fx_intel/journal.py` + `fx_intel/learning.py`): 判断を`logs/briefing_journal.jsonl`へ記録し、約24時間後のterminal-price proxyで相互採点して、縮小付き重み、確信度帯、ペア/状態別の減衰候補を生成する。これは機構の説明であって改善効果の証拠ではない。現journalは疎・重複・非PITで、institutional label/validation pathへ未接続のため、昇格根拠に使わない。
- **チャート状態×方向別の学習** (同`learning.py`): 判断時に`briefing._extract_features`が特徴量（`rsi_1h`/`adx_1h`/`ma_gap_atr`=MA乖離のATR換算/`atr_pct`=ボラ/`tf_agreement`=時間足一致度/`news_count`）をTradePlanに記録→ジャーナルへ保存。学習側は「売られすぎ圏(35未満)」「全時間足一致」「高ボラ(0.25%以上)」など相場用語の固定バケットを、さらにロング/ショート別に分けたセル単位で的中率を集計する（同じ状態でも向きで成績が非対称になるため）。セルごとに12件以上かつ的中率45%未満なら減衰係数（×0.7〜1.0）を付与。新規判断時は方向確定後に`LearnedProfile.condition_adjustment(features, direction)`が現在の状態×方向と突き合わせ、苦手なセルに該当したら確信度を減衰して理由を注意点に明示する。
- 副産物として`research_pack/upcoming_events.csv`（最新スナップショット、毎回上書き）と`research_pack/event_history.csv`（追記アーカイブ）を出力する。CSV形式は`fx_backtester --events`と互換だが、backtesterは`recorded_at`をas-of cutoffとして解釈しない。したがってrevision-aware PIT replayではなく、過去評価へそのまま渡すと後知恵改訂を混ぜ得る。
- **ML学習・shadow診断**: `--train-ml`でジャーナルからGBDTを再学習でき、ブリーフィング実行時にlegacy参考指標を更新する。ただし委員はshadow固定で複合スコアへ入らない。`--no-macro`/`--no-ml`で個別の委員を無効化できる。追加ファイル: `logs/macro_cache.json`、`logs/ml_model.json`、`logs/promotion_state.json`。
- **時間足別モード** (`--per-timeframe`、`fx_intel/timeframe.py` + `price_history.py` + `tf_learning.py` + `tf_briefing.py`): 融合1判断の代わりに15m/1h/4h/1dを**独立したアナリスト**として判断し、**時間足別の主ホライズン**で自己採点する（15m→15分後 / 1h→1時間後 / 4h→4時間後 / 1d→24時間後。補助ホライズンは観測専用で学習には不使用）。学習は融合モードと同じコア（重み再推定・キャリブレーション・ペア別減衰・状態×方向学習・反省レポート・Brier）を`(通貨ペア × 時間足)`セル単位で適用し、`logs/briefing_tf_learning.json`へ保存。融合モードとはジャーナル・学習ファイルを分離しているので両モードは干渉しない。読み取り専用ダッシュボード(`tools/ai_learning_dashboard`)は両ジャーナルを各主ホライズンで採点し、時間足別の的中率も表示する。
  - **将来価格の調達（5分系列）**: TradingViewスキャナーは現在値しか返さないため、主ホライズン後の実勢価格は後続の終値から取る。正規運用では`com.fx-codex.snapshot`だけが`fx_tf_snapshot.py`を5分ごとに起動し、`logs/briefing_tf_prices.jsonl`へ追記する。手動シグナルボードやraw loopをこの価格writerと併走させない。
- **依存の据え置き**: 分析モジュール(analyst/macro/gbm/ml/committee/promotion/timeframe/price_history/tf_learning/tf_briefing)は追加のサードパーティ依存を一切増やさない。macro.pyの取得はrequestsのみ、GBDT・学習・shadow診断・分析エンジン・時間足別レイヤは標準ライブラリだけで動く。
- テスト(`tests/test_fx_intel.py`ほか`test_analyst`/`test_macro`/`test_gbm`/`test_ml`/`test_committee`/`test_promotion`、時間足別は`test_timeframe`/`test_price_history`/`test_tf_learning`/`test_tf_snapshot`/`test_tf_briefing`/`test_fx_briefing_per_timeframe`/`test_dashboard_timeframe`)はネットワーク不要で完結する設計。

### FXシグナルボード（開発・一時確認専用）

`fx_briefing_loop.sh`は5分境界（00/05/10…分）ごとに時間足別分析を行い、**上位3候補・エントリー適性・
データ品質をまとめた「FXシグナルボード」1通**を送信できる。ただし開発・一時確認専用であり、
Mac miniの正規運用には組み込まない。loopは`--no-price-write`を使うが判断ジャーナルは更新するため、
launchd briefingとの併走も禁止する。`--dry-run`はauthoritative journal/model状態を更新しないが、
source cacheやevent exportは更新し得るため、完全なzero-write確認は正規runtimeと分離したcopyで行う。

- ボードのヘッダーは「データ品質（テクニカル/ニュース/経済指標/マクロ）」を表示する。
  発注経路は存在しないため、システム状態（executor死活監視など）は表示しない。
- `fx_tf_snapshot_loop.sh`は開発機の一時検証用に限る。Mac miniではlaunchd snapshotの代替・併走に使わない。

---

## 3. 運用メモ

- **writer所有権**: 価格系列は`com.fx-codex.snapshot`、判断ジャーナル/プロファイルは
  `com.fx-codex.briefing`が正規writer。healthはそれらを監視するだけで書き込まない。
- **残存リスク**: `fx_intel/journal.py`と`fx_intel/decision_log.py`の追記APIには、全呼出し元を
  横断する単一writer保証がない。launchd上の所有権と`run_exclusive.py`は運用上の防壁だが、
  rawな別名コマンドや別チェックアウトからの直接実行は回避できない。トランザクションDBまたは
  共通ファイルロックへ移行するまで、これは未解決のリスクとして扱う。
- 分析→Discord通知はネットワーク（TradingView/ニュースRSS/FRED/CFTC/ForexFactory）に依存する。
  取得失敗は「データ品質」欄に注意として表示され、分析自体は保守的な既定値で継続する。
- `.env` の `DISCORD_WEBHOOK_URL` に通知先WebhookのURLを設定する。
- 自動売買・発注機能は本システムには含めない。ブローカー執行は別リポジトリの責務とする。
