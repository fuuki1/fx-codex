# CLAUDE.md — fx-codex 開発ガイド

このファイルは Claude Code セッションの入口。詳細は各リンク先が一次情報。
**このファイルの記述とコードが食い違う場合はコードが正**（読んで検証してから作業する）。

## プロジェクト目的

USD/JPY・EUR/USD・GBP/USD 向けの中低頻度FX研究・分析・自動売買基盤。
最適化対象は「予測を多く出すこと」ではなく、**コスト控除後期待値・平均R・最大DD・
確率較正・見送りを含む意思決定品質・再現性・監査可能性**。

## 最上位原則（違反はP0）

1. **将来情報を混入させない** — 判断時点で取得可能だったデータのみ使用（point-in-time）
2. **Live取引を自動で有効化しない** — `ALLOW_LIVE=1` 明示 + paper/live二重ガード + 人間の承認が必須
3. **合成データの成績を実績として扱わない** — `params_gate` が来歴(provenance)無し・合成由来を拒否する
4. **インサンプル成績だけで昇格しない** — walk-forward / OOS / 統計的有意性 / shadow→paper→live の段階昇格
5. **不確実なら見送る** — 見送り(standby)は失敗ではなく意思決定クラス。データ品質不足時は強制見送り
6. **同一バー内のTP/SL先着は断定しない** — close-onlyパスは `ambiguous_sl_tp` → 保守的にSL扱い(-1R) + 品質フラグ

## アーキテクチャ（3層）

```
開発機 (~/Desktop/fx-codex, flat構成)          Mac mini 192.168.11.15 (~/trader/, Docker 8コンテナ)
  fx_backtester/  バックテストエンジン    rsync→   webhook→risk→executor→IBKR (Redis Streams)
  fx_intel/       分析・委員会・学習              strategy(MAクロス)/monitor/timescaledb/ngrok
  fx_briefing.py  Discordブリーフィング           ※IBKR口座未開設。executor系は稼働不能(実資金リスク無し)
```

全体像・データフロー・判断フローは **[SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md)** が一次ドキュメント。
Mac mini側スタックは [trader/ARCHITECTURE.md](trader/ARCHITECTURE.md) / [trader/RUNBOOK.md](trader/RUNBOOK.md)。
バックテストCLIの仕様は [README.md](README.md)。

## 主要ディレクトリ

| パス | 役割 |
|---|---|
| `fx_backtester/` | イベント駆動バックテスト（次足始値約定・spread/slippage必須・walk-forward・PBO/DSR過学習検定） |
| `fx_intel/` | 分析側の中核。委員会(`committee.py`)・リスクオフィサー(`briefing.build_trade_plan`)・採点(`journal.py`/`trade_outcome.py`)・学習(`learning.py`/`tf_learning.py`/`decision_feedback.py`)・昇格(`promotion.py`)・GBDT(`gbm.py`+`ml.py`) |
| `fx_briefing.py` | 統合エントリポイント（毎時実行想定、融合1判断 + `--per-timeframe` の2通） |
| `fx_tf_snapshot.py` | 5分ごとの価格スナップショット（短い足の採点に**必須**。止まると15m/1h採点が永久に不能） |
| `params_gate.py` | 戦略パラメータの安全ゲート（生成側+読み込み側）。`trader/app/params_gate.py` にミラー、`tests/test_params_gate_sync.py` が同期を担保 |
| `trader/` | Mac mini稼働スタック（独自ツールチェーン。ルートのblack/ruff/mypy対象外、CIの `trader-test` ジョブで検証） |
| `tools/` | 学習キャプチャ・期待値監視・読み取り専用ダッシュボード |
| `logs/` | ジャーナル・学習プロファイル・キャッシュ（git管理外、開発機とMac miniで別実体） |
| `docs/` | 既知課題 [docs/ISSUES.md](docs/ISSUES.md)・運用手順 [docs/FX_AI_OPERATIONS.md](docs/FX_AI_OPERATIONS.md) |

## 品質ゲート（変更時は全部通すこと = Definition of Done）

```bash
python3 -m pytest -q                          # 全テスト（ネットワーク不要で完結する設計）
python3 -m ruff check .
python3 -m black --check .                    # line-length 100
python3 -m mypy fx_backtester fx_intel *.py   # tools/ は対象外（既知ギャップ）
# trader/ 配下を触った場合（CIの trader-test 相当）:
cd trader && ruff check app tests && mypy app && pytest
```

- CIはツール版数固定: `ruff==0.15.20 black==26.5.1 mypy==2.1.0`（未固定だと無関係PRが赤くなる）
- 依存追加は原則禁止（`fx_intel` は標準ライブラリ+requestsのみ。Mac mini軽量venvへrsyncで移設できることが要件）
- 新しい閾値・重みはハードコードせずモジュール冒頭の定数へ。根拠（なぜその値か）をコメントかdocstringに残す
- 環境依存で実行できない検証は「未実行」と明示する（成功したことにしない）

## 判断フロー（要点）

```
technicals + news + macro + ml → 委員会 deliberate() → 複合スコア
  → リスクオフィサー build_trade_plan()（休場・イベント窓・データ品質・確信度上限の決定論ゲート、常に拒否権）
  → 学習補正（learning.py の重み再推定・確信度較正・セル別減衰、decision_feedback.py の失敗理由フィードバック）
  → TradePlan（方向・確信度・SL/TP・根拠）→ Discord通知 + ジャーナル追記
採点: journal.py（方向、24h±2h 市場オープン時間換算、ATR10%未満は判定除外）
      trade_outcome.py（MFE/MAE/TP/SL先着・実現R・経路品質。close-onlyは品質キャップ0.70）
昇格: promotion.py（shadow→paper: サンプル数+的中率+ATR正規化期待値+二項検定。→live は人間の明示承認のみ）
```

- 固定重み tech55%/news45% は**事前分布**であり、`learning.py` が実績で再推定する（シュリンク n/(n+40)、35〜70%クランプ、20件未満は既定のまま）
- ML委員は検証Brierが基準率予測を2%以上改善しないと `usable=False`（判断に不参加）。学習は時系列split+72hエンバーゴ+自己相関間引き

## 運用・デプロイの約束事

- **Mac miniへのデプロイはファイル単位rsync禁止**。依存漏れで本番が落ちた事故歴あり。
  `git checkout origin/main -- trader/` で全体を揃えてから `docker compose build`
- `trader/app/market_calendar.py` の休場カレンダーは2024-2027収録。**毎年更新必須**
- ブリーフィング運用は2本立て: 判断=毎時（`fx_briefing_loop.sh`）、価格系列=5分ごと（`fx_tf_snapshot_loop.sh`）。
  **snapshotループが止まると学習層全体がデータ飢餓になる**（昇格ゲートが0件で足止め→委員が永久にshadow）
- launchd/cronはTCC制限で`~/Desktop`を読めないため、ループはターミナルから手動起動

## 開発ワークフロー

- ブランチ→PR→CI緑→マージ。mainへ直接pushしない
- 並行Claudeセッションが同時に走ることがある。**着手前に `gh pr list --state open` で重複を確認**（同じ修正のPRが既に開いていることがある）
- コミットメッセージは日本語のconventional commits（`feat(fx_intel): ...` / `fix(ci): ...`）
- 実験・モデル変更は「仮説→変更→評価指標→ベースライン比較→採用/却下理由」を記録する（「良さそうだから採用」禁止）

## 既知の課題・制約

[docs/ISSUES.md](docs/ISSUES.md) が登記簿。2026-07-10時点の要点:

- IBKR口座未開設 → 実発注経路は全て未検証。「TradingView分析→Discord通知」が現在の実運用範囲
- 現行 `strategy_params.json` は合成データ由来の過学習パラメータ（読み込みゲートが拒否する。実データで再最適化が必要）
- 学習ジャーナルのサンプルが極小（収集ループの常駐化が最大のボトルネック）
- 価格経路がclose-only（TradingViewスキャナー現在値のみ）。OHLC履歴ソースの注入口は `price_history.py` にあり
