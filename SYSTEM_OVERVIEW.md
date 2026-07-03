# FXシステム全体像

このドキュメントは「開発機（このMac）」「Mac mini（実運用サーバー）」を横断した、
FX分析・自動売買システム全体のロジックとデータフローをまとめたものです。
バックテストCLIの詳細なオプション・出力仕様は [README.md](README.md) を参照してください。
Mac miniの障害対応・復旧手順・既知の問題は memory の `project_system` に記録しています。

## 全体構成（3層）

```
┌─────────────────────────────┐     rsync      ┌──────────────────────────────────────┐
│ 開発機 (~/Desktop/fx-codex)   │ ───────────▶  │ Mac mini (192.168.11.15)               │
│  - バックテスト・戦略検証      │                │  ~/trader/fx-codex/ ... 分析コード同梱   │
│  - パラメータ最適化           │                │  ~/trader/        ... Docker実運用スタック│
│  - 分析→Discord通知(手動起動) │                │  ~/trader/tv-notify-venv (通知用venv)   │
└─────────────────────────────┘                └──────────────────────────────────────┘
```

- **開発機**: `fx_backtester/`（バックテストエンジン）と `fx_intel/`（分析・通知）を開発・検証する場所。
- **Mac mini**: 実際にDockerコンテナ8個が24時間稼働し、TradingViewのシグナルを受けて発注まで行う本番環境。`~/trader/fx-codex/` にfx-codexのサブセットをマウントし、戦略ロジックだけ共有している。
- 両者は独立したGitレイアウト（Mac miniは `trader/` サブツリー構成のまま稼働、開発機はフラット化済み）なので、コードの移動は **rsyncによる手動同期**。自動デプロイパイプラインは無い。

---

## 1. Mac mini 実運用パイプライン（Docker Compose、8コンテナ）

TradingViewのアラート受信から発注までを、Redis Streamsをバックボーンにした
パイプライン型アーキテクチャで実装（`trader/app/`）。

```
TradingView Alert
      │ HTTPS (ngrok)
      ▼
 ┌──────────┐   signals    ┌──────┐   orders   ┌───────────┐
 │ webhook  │ ──────────▶ │ risk │ ─────────▶ │ executor  │ ──▶ IBKR (ib-gateway)
 │(FastAPI) │  Redis      │      │  Redis     │(ib_async) │
 └──────────┘  Stream     └──────┘  Stream    └───────────┘
      ▲                                              │
      │ signals                                      │ commissionReport
 ┌──────────┐                                         ▼
 │ strategy │ (自作MAクロス、60秒ループ)      timescaledb (fills, processed_orders)
 └──────────┘
      │
 ┌──────────┐
 │ monitor  │ 死活監視・日次サマリ → Discord
 └──────────┘
```

### 各コンテナの役割

| コンテナ | 役割 | ソース |
|---|---|---|
| `ib-gateway` | IBKR自動ログイン（証券会社への実接続口） | `ghcr.io/gnzsnz/ib-gateway` |
| `redis` | シグナル/注文のStream、Kill switch、レート制限の永続化 | `redis:7-alpine` |
| `timescaledb` | 約定履歴・イベントログの時系列DB | `timescale/timescaledb` |
| `webhook` | TradingViewからのシグナル受信API | `trader/app/webhook.py` |
| `strategy` | 自作MAクロス戦略を60秒ごとに評価 | `trader/app/strategy.py` |
| `risk` | 発注前のリスクフィルタ（6段階チェック） | `trader/app/risk.py` |
| `executor` | IBKRへの実発注、ブラケット注文組み立て | `trader/app/executor.py` |
| `monitor` | 死活監視、Discordアラート、日次サマリ | `trader/app/monitor.py` |
| `ngrok` | webhookをHTTPS公開（TradingView Webhook受信用） | `ngrok/ngrok` |

### 1-1. シグナル受信 (`webhook.py`)

- `POST /webhook` でTradingViewのAlertを受信。
- **2重セキュリティ**: (1) 送信元IPを許可リストと照合（`X-Forwarded-For`はプロキシが追記した**末尾**だけを信頼。先頭はクライアントが偽装可能なため） (2) ペイロードの`secret`を`hmac.compare_digest`で定時間比較。
- DoS対策として`Content-Length`が64KBを超えるリクエストは413で拒否。
- **冪等性**: `idem`をRedisに`NX, EX=3600`で記録し、60分以内の重複シグナルは黙って破棄。
- 受理したシグナルは`domain.normalize_signal()`で内部正規形に変換し、`signals` Streamへpublish。

### 1-2. 自作戦略 (`strategy.py`)

- IBKRのhistorical barsを取得し、**MAクロス + ATRストップ**でシグナル生成（`ma_cross_signal()`）。
  - `fast_ma > slow_ma` → ロング目線、`fast_ma < slow_ma` → ショート目線。
  - ストップ距離 = ATR(14) × `atr_multiple`。
- `strategy_params.json`の**mtimeを監視**し、変更があればホットリロード（コンテナ再起動不要）。開発機の`auto_optimize.py`が書き出したパラメータがそのまま反映される仕組み。
- シグナルは**ポジション状態が変化した時だけ**発行（毎ループの連投防止、Redis上の`strategy:state`ハッシュで前回状態を保持）。
- ATRが計算できない（データ不足）場合はストップ無し発注を回避し、シグナル自体を出さない。
- 既定では`STRATEGY_ENABLED=0`で無効化されており、明示的に有効化しない限りシグナルを出さない。

### 1-3. リスクフィルタ (`risk.py`)

`signals` Streamを購読し、6段階のチェックを**上から順に**適用。1つでも引っかかれば却下（Redis Consumer Group `risk`）。

1. **Kill switch** — ONなら即却下
2. **数量上限** — `MAX_POSITION_QTY`超過を却下
3. **ストップロス必須** — `stop_price`/`stop_distance`が無いシグナルは却下（決済シグナルは免除）
4. **取引時間帯** — `ENFORCE_SESSION`時、`within_session()`外なら却下
5. **日次損失** — 当日実現損益が`MAX_DAILY_LOSS_JPY`を超過したら**自動でKill switchをON**にしてDiscord通知＋却下
6. **発注レート** — `MAX_ORDERS_PER_MIN`をRedis上のスライディングウィンドウで制限

通過したシグナルのみ`orders` Streamへpublish。

### 1-4. 発注実行 (`executor.py`)

`orders` Streamを購読し、IBKRへ実発注（Redis Consumer Group `exec`）。

- **二重発注防止**: `idem`から決定的な`client_order_id`（SHA1先頭16桁）を生成し、発注前にDBの`processed_orders`テーブルへINSERT。PK衝突（＝既に処理済み）ならスキップ。
- **Kill switch再確認**: riskで通過済みでも発注直前にもう一度チェック（TOCTOU対策）。
- **本番二重ガード**: `trading_mode=live`でも環境変数`ALLOW_LIVE=1`が無ければ実発注しない。
- **ストップロス必須のブラケット注文**: `stop_price`または`stop_distance`があるシグナルは、親注文を`transmit=False`にして子のストップ逆指値と一括送信（IBKRのブラケット規約）。親だけ約定してストップ無しの裸ポジションが生まれる隙を作らない。
- 通貨ペアの精度に応じてストップ価格を丸める（JPY建ては小数点3桁、それ以外は5桁）。
- `commissionReport`コールバックで約定後に`realized_pnl`を更新。
- 接続耐性: 起動時は指数バックオフでリトライ、アイドル毎に切断検知して自動再接続。

### 1-5. 死活監視 (`monitor.py`)

- 60秒ごとに (1) webhookの`/health` (2) DB (3) Redis (4) 各サービスのハートビート鮮度（180秒超で停止扱い＝プロセスは生きているがループが止まっている状態を検知）をチェック。
- 異常があればDiscordへ通知（`common.notify`のスロットルでアラート嵐を防止）。
- 毎朝7時(JST)に日次サマリ（約定件数・実現損益・Kill switch状態・trading_mode）をDiscordへ送信。送信済みフラグをRedisで1日1回に制御。

### 1-6. 自律最適化エンジン (Mac mini側、`trader/optimize/auto_optimize.py`)

> ⚠️ これは**Mac mini上のDocker実行版**で、開発機ルート直下の`auto_optimize.py`（後述2章）とは別ファイル。混同注意。

- Docker経由で`fx_backtester.cli optimize`をwalk-forward/OOS(Out-of-Sample)検証付きで実行し、`strategy_params.json`を書き換える。
- **合成データ検知による安全装置**: `OPTIMIZE_DATA`環境変数が未指定、または同梱の`examples/sample_prices.csv`（乱数生成の合成データ）を指している場合は**最適化自体を拒否**し、既存パラメータを維持する（`validate_data_path()`）。過剰最適化したパラメータが誤って本番配備される事故を防ぐガード（コミット`d597b76`で追加）。
- グリッド探索: `fast_window`, `slow_window`, `atr_window`, `atr_multiple`。
- OOS Sharpe・IS/OOS比・パラメータ安定性・取引数不足フラグ・過学習警告を記録し、配備前にログへ出力。

---

## 2. 開発機のバックテスト基盤 (`fx_backtester/`)

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
                    artifacts.py / analysis.py (成果物・商用検証ダッシュボード)
```

- **戦略は`target_position`（-1/0/1）と`stop_distance`だけを返す** シンプルなインターフェース（`strategies/base.py`）。これはMac mini実運用側の`strategy.py`の`ma_cross_signal()`と同一の設計思想で、バックテストとライブでロジックを共有しやすくしている。
- 実装済み戦略:
  - `moving_average_cross.py` — **実運用の主力戦略と同一ロジック**（fast/slow MA + ATRストップ）
  - `donchian_breakout.py` — 高値・安値のブレイクアウト
  - `rsi_mean_reversion.py` — RSIの逆張り
  - `ai_logistic.py` — リターン/モメンタム/SMA乖離/ボラティリティ等を特徴量にしたロジスティック回帰。各時点で確定済みの過去データだけでローリング再学習（未来情報のリークを防ぐ設計）
  - `baselines.py` — buy&hold等、比較対象のベースライン
- **約定モデル**: シグナルは足確定後にのみ発生、成行は次足始値で約定、買いはAsk/売りはBidという現実的な前提。スプレッド・スリッページが0以下だとバックテスト自体を拒否する。
- **過学習対策**: walk-forwardのパラメータ組み合わせ数を`--max-params`（既定20）で制限、学習区間で選んだパラメータを隣接する未使用テスト区間にのみ適用、勝率単体ではなくSharpe/期待R/PF/DDを組み合わせたスコアで評価。さらに`trial_log.py`（探索全試行の記録）と`overfitting.py`（PBO/CSCVとDeflated Sharpe Ratioの統計検定、scipy非依存）を備え、`WalkForwardValidator`にも`trial_logger=`で試行記録を差し込める。
- `validation.py`/`qa.py`にデータ品質検証（OHLC整合性、時系列順、重複、spread/slippage設定の妥当性）があり、失敗時はバックテスト自体を実行しない「商用運用ゲート」を構成している。

### 2-1. 開発機ルートの `auto_optimize.py`（安全ゲート付き）＋パラメータ承認フロー

`~/Desktop/fx-codex/auto_optimize.py`は`MovingAverageCross`専用のグリッドサーチスクリプト。**実データCSVの明示指定が必須**で、共有ゲートモジュール`params_gate.py`と対になった以下の安全設計を持つ:

- **データゲート** (`params_gate.validate_data_source`): `--data`（または環境変数`OPTIMIZE_DATA`）が未指定、同梱サンプル`examples/sample_prices.csv`（乱数生成の合成データ。**パスだけでなく内容ハッシュでも検知**するためコピーしても素通りしない）、行数1000未満、期間180日未満のいずれかに該当すると実行を拒否し、何も書き出さない。
- **IS/OOS分割**: 前半70%でパラメータを選択し、後半30%（アウトオブサンプル）で検証。OOSのSharpeが非正またはISの半分未満なら`overfit警告`、取引数が20未満なら`取引数不足`警告が付く。
- **試行ログ＋過剰最適化検定**: グリッドサーチの全試行（パラメータ・指標・リターン系列）を`runs/trial_logs/<run_id>/`（run.json / trials.jsonl / returns_matrix.csv）に記録し、そこから**PBO**（CSCV: ISで最良だった試行がOOSで中央値未満に沈む確率。0.5=探索順位に予測力ゼロ）と**DSR**（Deflated Sharpe Ratio: 探索回数ぶんのまぐれ期待値を控除した上でSharpeが本物である確率）を自動計算。結果は`provenance.overfitting`に埋め込まれ、**PBO≥0.5 / DSR<0.95は警告**として付く（警告付きcandidateは`promote_params.py`が既定で昇格拒否）。ログの場所と試行数は`provenance.trials`から辿れる。
- **来歴メタデータ**: 出力にデータのパス・sha256・行数・期間・取引数・OOS結果・警告一覧を`provenance`として埋め込む。来歴の無いパラメータは後段のゲートがすべて拒否する。
- **直接配備しない**: 出力は`strategy_params.candidate.json`まで。`strategy_params.json`への昇格は`promote_params.py`での明示的な承認が必要。

パラメータのライフサイクル:

```
python3 auto_optimize.py --data <実データ.csv>
    → strategy_params.candidate.json   (来歴+警告付き。ここでは配備されない)
python3 promote_params.py [--check|--force]
    → strategy_params.json             (現行を strategy_params.prev.json へ自動退避。
                                        警告付きcandidateは--force無しでは昇格拒否)
python3 promote_params.py --rollback
    → 直前のパラメータへ1コマンド復旧
```

- `params_gate.py`は**標準ライブラリのみに依存**する共有モジュールで、rsyncでMac miniへそのまま同期できる。生成側（データ検証）と読み込み側（スキーマ・境界値・来歴検証）の両方のゲートを提供する。コンテナ焼き込み用に`trader/app/params_gate.py`へミラーを置き、`tests/test_params_gate_sync.py`が両者のロジック一致（docstring以外）を検証する。
- **読み込み側の統合は完了**: 全読み込み経路が`params_gate.load_validated_params()`を通す。
  - Mac miniの`trader/app/strategy.py`（`ParamStore`）はホットリロード時にゲートへ通し、不合格なら直近の合格値（初回は`DEFAULT_PARAMS`）を維持し、`params_rejected`イベントとログを（同一mtimeにつき1回）残す。テストは`trader/tests/test_strategy_params.py`。
  - 開発機の`fx_briefing.py`／`tv_discord_notify.py`も同ゲートを通し、不合格時は保守的な既定値（MA 20/100）で継続し、ブリーフィング本文と標準エラーに警告を明示する。
  - ※`strategy`コンテナはコード焼き込みのため、更新反映にはイメージ再ビルドが必要。
- **歴史的経緯**: 旧版はデータが合成サンプルに無条件ハードコードされており、現行の`strategy_params.json`（Sharpe 3.96 / 最大DD 0.0109%という非現実的な数値）は**合成データ由来の過学習パラメータ**。実データ入手後に再最適化→承認で置き換えること（provenance無しの現行ファイルは読み込み側ゲート導入後は自動的に拒否される）。

---

## 3. 分析・通知システム (`fx_intel/`, ルート直下スクリプト)

Mac miniで**手動起動**するとDiscordに分析結果を通知する2つの独立したツール。
（`launchd`/`cron`はmacOSのTCC制限で`~/Desktop`配下を読めないため自動起動不可。ターミナルから直接起動する運用。）

### 3-1. `tv_discord_notify.py` — シンプル版

- TradingViewスキャナー(`tradingview-ta`)から複数時間足の総合レーティングとRSI/MACD/SMAを取得。
- 自作MAクロス戦略の目線と突き合わせて一致/不一致を判定。
- Discord Webhookへ送信（`fx_backtester`非依存、単体実行可能）。
- Mac miniでは `~/trader/tv-notify-venv`（`fx-codex/`の**一つ上の階層**）から実行:
  ```bash
  cd ~/trader/fx-codex && ../tv-notify-venv/bin/python tv_discord_notify.py
  ```
- 定期実行: `tv_notify_loop.sh`（毎時5分）

### 3-2. `fx_briefing.py` + `fx_intel/` — 統合版（上位互換）

ニュース×経済指標×テクニカルを統合した、機関投資家のモーニングブリーフィングを模したDiscord通知。

```
                         テクニカル委員 ──┐
fx_intel/technicals.py     (TV 4時間足     │
  ＋MAクロス               ＋MA一致)        │
                         ニュース委員 ──────┤
fx_intel/analyst.py        (自前分析エンジン) │  委員会が重み付き平均で
  (Claude非依存の既定)                       ├─▶ 複合スコア ─▶ リスク
fx_intel/macro.py        マクロ委員 ─────────┤    (fx_intel/          オフィサー
  (COT・DXY・VIX・金利)     (shadow/paper/live)│     committee.py)     (決定論ゲート)
                         ML委員 ────────────┘         │                 │
fx_intel/ml.py             (GBDT確率モデル)            │                 ▼
  (gbm.py＝依存ゼロGBDT)                                │         方向・確信度・SL/TP
      ▲                    fx_intel/promotion.py ──────┘
fx_intel/calendar.py       (実績で委員を段階昇格)
  (ForexFactory)
```

- **複数AI委員会** (`fx_intel/committee.py`): 役割の異なる4委員が意見を出し、シンセサイザーが重み付き平均で複合スコアを作り、**リスクオフィサー(`build_trade_plan`の決定論ゲート=休場・イベント窓・データ品質・確信度上限)が常に拒否権を持つ**。「アナリストの総意をリスク管理者が却下できる」機関投資家デスクの構造をコードで表現。追加委員が居なければ従来のtech55%/news45%合成と完全一致(後方互換)。
- **自前分析エンジン** (`fx_intel/analyst.py`): 「Claude級の分析AIを外部API非依存で」の要件に対する回答。汎用LLMは再現できないが、FXヘッドライン解釈という狭タスクに特化した決定論エンジンを実装。**否定の理解**(「rules out rate hike」は反転)・**ヘッジ割引**(「may/speculation」は×0.7)・**強調増幅**(「sharply/soars」は×1.3)・**鮮度減衰**(半減期12時間)・**ソース信頼度**・**テーマ抽出**(政策/インフレ/雇用/景気/地政学)・**合意度×物量の確信度**を備え、実効スコア=バイアス×確信度でClaude経路と同じ契約。同じ入力から必ず同じ判断=監査可能。
- **センチメント序列** (`fx_intel/sentiment.py`): Claude API(`ANTHROPIC_API_KEY`があれば上乗せ) → **自前分析エンジン(既定)**。旧来の単純語彙カウントは比較検証用に`score_headlines_lexicon`として残置。
- **マクロデータ層** (`fx_intel/macro.py`): APIキー不要の公開ソース(Stooq日次OHLC / FRED CSVの米10年・2年金利・VIX・広義ドル指数 / CFTC COT投機筋ポジション)を**TTLキャッシュ+staleness品質ゲート**付きで取得。パースは純粋関数でフィクスチャテスト可能。**リスクレジーム判定を語彙の雰囲気から実データの固定規則**(VIX水準・急騰、金利急低下、ドル指数急騰の多数決、判定理由を必ず文字列で返す)へ格上げ。マクロ委員はCOTポジ差(重み0.6)+レジーム整合(0.4)でペア方向スコアを出す。
- **GBDT確率モデル** (`fx_intel/gbm.py` + `fx_intel/ml.py`): LightGBM/XGBoost系のアルゴリズム(Newtonブースティング+ヒストグラム分割)を**依存ゼロの純Python**で実装(Mac mini軽量venvにネイティブ拡張を持ち込まない方針。`overfitting.py`がscipy非依存なのと同じ判断)。ジャーナルから「P(hit | 状態, 方向)」を学習し、ロング/ショートの的中確率差をML委員の意見にする。**自己相関の間引き**(毎時×24hホライズンは評価窓が23h重複するため同一ペア4時間ゲート)・**時系列split+72hエンバーゴ**(学習サンプルの評価窓が検証期間に食い込むリーク防止)・**Plattスケーリング較正**・**スキルゲート**(検証Brierが基準率予測を2%以上改善しないと`usable=False`で判断に不参加)を備える。
- **shadow/paper/live 昇格ゲート** (`fx_intel/promotion.py`): 新任委員(macro/ml)は必ず**shadow**(意見を記録・表示するが複合スコアに不参加)から始まり、ジャーナル採点で**サンプル数・的中率・ATR正規化期待値・二項片側検定の有意性**を全て満たすと**paper**(複合スコア参加)へ自動昇格。劣化時は即座に自動降格(ヒステリシス付き)。**live**(実売買接続)への昇格だけは数字が揃っても自動化せず、`--promote-live`の人間の明示承認を要求(`promote_params.py`と同じ「来歴+明示承認」思想)。状態は`logs/promotion_state.json`に永続化。
- **経済指標カレンダー**: ForexFactory公開フィード(`nfs.faireconomy.media`)。429レート制限があるため`logs/calendar_cache.json`に**45分キャッシュ**。
- **イベント回避ロジック**: 高影響イベントの**前120分/後180分**は新規エントリーを強制「様子見」（`research-max`プリセットと同じ窓）。
- **自己学習ループ** (`fx_intel/journal.py` + `fx_intel/learning.py`): 毎回の判断を`logs/briefing_journal.jsonl`に記録し、実行のたびに履歴全体を相互採点（各判断を約24時間後・市場オープン時間換算の後続エントリの終値と突き合わせ。ATRの10%未満の小動きは判定除外）。そこから①テクニカル/ニュース単独の的中率で複合重みを再推定（シュリンク`n/(n+40)`、テクニカル35〜70%クランプ、20件未満は既定のまま）、②確信度帯別の的中率キャリブレーション、③的中率45%未満のペアの確信度減衰（×0.6〜1.0、8件以上で発動、減衰のみ）を導き、`logs/briefing_learning.json`へ保存して当日の分析に自動反映する。分析を重ねるほど自分の当たり外れから調整が効く設計（`--no-learning`で無効化、プロファイル破損時は既定値で継続）。
- **チャート状態×方向別の学習** (同`learning.py`): 判断時に`briefing._extract_features`が特徴量（`rsi_1h`/`adx_1h`/`ma_gap_atr`=MA乖離のATR換算/`atr_pct`=ボラ/`tf_agreement`=時間足一致度/`news_count`）をTradePlanに記録→ジャーナルへ保存。学習側は「売られすぎ圏(35未満)」「全時間足一致」「高ボラ(0.25%以上)」など相場用語の固定バケットを、さらにロング/ショート別に分けたセル単位で的中率を集計する（同じ状態でも向きで成績が非対称になるため。例: RSI買われすぎ圏はロングでは外しやすいがショートは当たる）。セルごとに12件以上かつ的中率45%未満なら減衰係数（×0.7〜1.0）を付与。新規判断時は方向確定後に`LearnedProfile.condition_adjustment(features, direction)`が現在の状態×方向と突き合わせ、苦手なセルに該当したら確信度を減衰して理由を注意点に明示する（複数該当時は最悪の1条件のみ適用し過剰減衰を防ぐ。neutral/standby/closedでは照合しない）。学習メモには「👍 当たりやすい/⚠️ 苦手なチャート状態」として全体的中率±10pt以上のセルを「バケット×方向」表記で表示。方向で分けるぶんセルあたりのサンプルは半分になるため発動は遅くなるが、向きの非対称性を混ぜた誤った減衰を防ぐことを優先する。
- 副産物として`research_pack/upcoming_events.csv`（最新スナップショット、毎回上書き）と`research_pack/event_history.csv`（**追記アーカイブ**）を出力。いずれも`fx_backtester`の`--events`にそのまま渡せる形式で、**ライブのイベント回避とバックテストのイベント回避が同じデータソースを共有**する設計。
- `event_history.csv`は実行のたびに未観測のイベント・改定分（時刻変更やforecast確定）だけを`recorded_at`付きで追記する簡易point-in-time記録。運用を続けるほど過去期間の実カレンダーが蓄積され、バックテストでのイベント回避再生に使えるようになる（`--no-event-archive`で無効化）。
- 定期実行: `fx_briefing_loop.sh`（毎時10分。`tv_notify_loop.sh`の毎時5分と5分ずらして負荷分散）。
- **ML学習・昇格の運用**: `--train-ml`でジャーナルからGBDTを再学習(週次程度で十分)。委員の昇格はブリーフィング実行のたびに自動採点され、`--promote-live macro ml`を付けたときだけ条件を満たす委員をliveへ昇格承認する。`--no-macro`/`--no-ml`で個別の委員を無効化できる。追加ファイル: `logs/macro_cache.json`(マクロTTLキャッシュ)、`logs/ml_model.json`(学習済みモデル+来歴)、`logs/promotion_state.json`(委員の段階)。
- **依存の据え置き**: 新モジュール(analyst/macro/gbm/ml/committee/promotion)は追加のサードパーティ依存を一切増やさない。macro.pyの取得はrequestsのみ、GBDT・学習・昇格・分析エンジンは標準ライブラリだけで動く。Mac miniの軽量venvへrsyncするだけで移設できる。
- テスト(`tests/test_fx_intel.py`ほか`test_analyst`/`test_macro`/`test_gbm`/`test_ml`/`test_committee`/`test_promotion`)はネットワーク不要で完結する設計（マクロ取得はキャッシュ事前投入でオフライン検証）。

### 使い分け

| | `tv_discord_notify.py` | `fx_briefing.py` |
|---|---|---|
| 依存 | 単体（fx_backtester非依存） | `fx_intel/`一式 |
| 情報源 | テクニカルのみ | テクニカル＋ニュース＋経済指標 |
| Mac miniでの状態 | 設置・動作確認済み | **未転送**（2026-07-02時点、rsyncが必要） |

---

## 4. 開発機↔Mac mini 協調開発サイクル

```
1. 開発機: python3 auto_optimize.py --data <実データ.csv> → strategy_params.candidate.json
2. 開発機: python3 promote_params.py で検証+承認 → strategy_params.json（旧値は.prevへ退避）
3. rsync: ~/Desktop/fx-codex/ → Mac mini ~/trader/fx-codex/
4. Mac mini: strategy.py が strategy_params.json のmtime変更を検知 → 自動反映（再起動不要）
   ※読み込み側ゲート（params_gate.load_validated_params）の統合は未実施、2-1章参照
5. 必要なら: ssh fuuki@192.168.11.15 "cd ~/trader && docker compose restart strategy"
```

- `strategy`コンテナはコードをイメージに焼き込み済み（マウント無し）なので、**戦略ロジック自体の変更**はイメージ再ビルドが必要。`strategy_params.json`のような**パラメータ変更**のみファイル同期で反映される。
- 同期は`~/Desktop/sync-fx-codex.sh`（fswatch常駐）や`~/Desktop/sync-and-deploy.sh`（最適化→rsync→再起動を一括）を使うが、`launchd`からは起動できずターミナルからの手動起動が必要。

---

## 5. 現状の運用制約（2026-07-02時点）

- **IBKR口座が未開設**のため、`ib-gateway`/`executor`は資格情報が無く動作不能。当面は「TradingView分析→Discord通知」のみが実運用機能。
- `executor`/`ngrok`/`ib-gateway`は接続エラーで再起動ループに入ったまま放置（`ALLOW_LIVE=0`のpaperモードなので実資金リスクは無い）。
- `monitor`コンテナは「ハートビート停止」アラートの誤検知を避けるため停止中。ただし`restart: always`のため、Dockerデーモン再起動やMac mini再起動で自動的に復活する点に注意。

詳細な障害履歴・復旧手順はセッションメモリの`project_system`を参照。
