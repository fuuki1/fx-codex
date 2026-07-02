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
- **過学習対策**: walk-forwardのパラメータ組み合わせ数を`--max-params`（既定20）で制限、学習区間で選んだパラメータを隣接する未使用テスト区間にのみ適用、勝率単体ではなくSharpe/期待R/PF/DDを組み合わせたスコアで評価。
- `validation.py`/`qa.py`にデータ品質検証（OHLC整合性、時系列順、重複、spread/slippage設定の妥当性）があり、失敗時はバックテスト自体を実行しない「商用運用ゲート」を構成している。

### 2-1. 開発機ルートの `auto_optimize.py`（安全ゲート付き）＋パラメータ承認フロー

`~/Desktop/fx-codex/auto_optimize.py`は`MovingAverageCross`専用のグリッドサーチスクリプト。**実データCSVの明示指定が必須**で、共有ゲートモジュール`params_gate.py`と対になった以下の安全設計を持つ:

- **データゲート** (`params_gate.validate_data_source`): `--data`（または環境変数`OPTIMIZE_DATA`）が未指定、同梱サンプル`examples/sample_prices.csv`（乱数生成の合成データ。**パスだけでなく内容ハッシュでも検知**するためコピーしても素通りしない）、行数1000未満、期間180日未満のいずれかに該当すると実行を拒否し、何も書き出さない。
- **IS/OOS分割**: 前半70%でパラメータを選択し、後半30%（アウトオブサンプル）で検証。OOSのSharpeが非正またはISの半分未満なら`overfit警告`、取引数が20未満なら`取引数不足`警告が付く。
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

- `params_gate.py`は**標準ライブラリのみに依存**する共有モジュールで、rsyncでMac miniへそのまま同期できる。生成側（データ検証）と読み込み側（スキーマ・境界値・来歴検証）の両方のゲートを提供する。
- **読み込み側の統合は未実施**: Mac miniの`strategy.py`はホットリロード時に`params_gate.load_validated_params()`を通し、不合格なら現行パラメータを維持してDiscordへ通知するよう改修が必要（strategyコンテナはコード焼き込みのためイメージ再ビルドも必要）。テストは`tests/test_params_gate.py`。
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
fx_intel/technicals.py ──┐
  (TradingView 4時間足     │  55%
   +MAクロス一致ボーナス)   ├──▶ 複合スコア ──▶ 方向・確信度(0-100)・
fx_intel/news.py ─────────┤  45%              ATRベースSL/TP
  (FXStreet/Google News)   │
fx_intel/sentiment.py ────┘
  (語彙ベース or Claude API)
      ▲
fx_intel/calendar.py (ForexFactory、イベント警戒窓判定)
```

- **複合スコア**: テクニカル55%（TradingView 4時間足重み付き＋MAクロス一致ボーナス）＋ニュース45%（ベース通貨とクオート通貨のセンチメント差）。
- **センチメント分析**: 語彙ベース（ポジティブ/ネガティブ単語カウント）が常時動作する既定経路。`.env`に`ANTHROPIC_API_KEY`があれば自動でClaude API分析（claude-sonnet-5）に昇格し、失敗時は語彙ベースへフォールバック。現状キー未設定のため語彙ベース運用。
- **経済指標カレンダー**: ForexFactory公開フィード(`nfs.faireconomy.media`)。429レート制限があるため`logs/calendar_cache.json`に**45分キャッシュ**。
- **イベント回避ロジック**: 高影響イベントの**前120分/後180分**は新規エントリーを強制「様子見」（`research-max`プリセットと同じ窓）。
- 副産物として`research_pack/upcoming_events.csv`を出力。これは`fx_backtester`の`--events`にそのまま渡せる形式で、**ライブのイベント回避とバックテストのイベント回避が同じデータソースを共有**する設計。
- 定期実行: `fx_briefing_loop.sh`（毎時10分。`tv_notify_loop.sh`の毎時5分と5分ずらして負荷分散）。
- テスト(`tests/test_fx_intel.py`)はネットワーク不要で完結する設計。

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
