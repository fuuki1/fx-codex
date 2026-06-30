# RISK — プロ級リスクエンジン

> 本書は教育・研究目的の設計文書であり、特定の売買推奨や利益保証ではない。

添付リサーチ（FXトッププロの思考法 / その批判的再評価）の結論は一貫している:
**長期的に生き残るプロの強みは「予測力」ではなく「リスク管理・資金管理・撤退の速さ」**である。
本システムの `risk_engine.py` は、その中でも個人が再現可能で効果の大きい原則を、
機械可読なルールとして実装する。判断は純粋関数（`app/risk_engine.py`）に集約し、Redis/DB/
ブローカー無しで単体テストできる。I/O（残高・損益・建玉・カレンダー・通知・Kill switch）は
`risk.py` が担う。

## 設計思想（なぜこの形か）
- **当てるゲームではなく、壊れないゲーム**: 「間違っても小さく、稀に来る歪み/トレンドを取り切る」。
  だからサイズ・相関・撤退を主役にし、相場観は入力の一つに留める。
- **サイズは確信ではなくリスクで決まる**（Kovner「ポジションサイズはストップで決まる」）。
- **期待値 > 勝率**（Lipschutz「50%超の勝率に依存してはいけない」）。だから勝率ではなく
  期待値・R 倍数・損益比を記録・監視する（`journal.py`）。
- **入らない自由**（Druckenmiller「危険なところではやらない」）。ブラックアウト・セッション・
  連敗停止は「やらない」を自動化したもの。
- **フェイルセーフ**: 判断材料を読めないとき（DB 不通など）は承認しない。Redis 不通時は
  Kill switch を ON 扱い。

## 実装している統制と根拠

| 統制 | 既定 | 根拠（リサーチ対応） | 実装 |
|---|---|---|---|
| リスク基準サイジング | OFF（要有効化） | サイズはストップ距離×口座リスクで決める（Kovner / 個人モデル「許容損失÷ストップ幅」） | `position_size()` |
| 連敗スロットル | 3 連敗で半減 / 5 連敗で停止 | 連敗時はサイズを落とす→停止（Lipschutz / 個人モデル） | `loss_streak()`, `streak_size_factor()` |
| 日次損失上限 | 50,000 | ドローダウン中に退場しない | `evaluate()` → 自動 Kill switch |
| 週次損失上限 | 0=無効 | 1 週間の損失上限で翌週まで新規停止（個人モデル） | `evaluate()` → 自動 Kill switch |
| 同時保有数の上限 | 3 | 「最大 3 つ」。4 つ目は独立理由が要る（個人モデル） | `evaluate()`（新規銘柄のみ計数） |
| 通貨エクスポージャ上限 | 0=無効 | 高相関ポジションを 1 つの巨大ポジションとして扱う（Lipschutz） | 通貨レッグ分解 + `_exposure_breach()` |
| イベント・ブラックアウト | カレンダー次第 | CPI・NFP・FOMC 前後は新規制限（Marcus / 個人モデル） | `BlackoutCalendar` + `in_blackout()` |
| 取引セッション | ON | 時間外は新規しない | `within_session()` |
| 発注レート上限 | 10/分 | 暴走・連投の抑止 | `rate_limit_allow()`（Redis 永続） |
| 期待値/R 倍数ジャーナル | 常時 | 勝率ではなく期待値で検証（Lipschutz / プロセス検証） | `journal.py`, `fills.realized_r` |

## サイジングの計算式
```
許容損失額 = 口座残高(account_equity) × risk_per_trade_pct% × 連敗縮小係数(factor)
発注数量   = floor( 許容損失額 ÷ (ストップ距離 × value_per_point), lot_step )
想定リスク = 発注数量 × ストップ距離 × value_per_point   # fills.intended_risk（R 倍数の分母）
```
- **ストップ距離**は価格単位。シグナルの `stop_distance`、または `stop_price` と基準価格から導出
  （`normalize_signal`）。自作戦略 `strategy.py` は ATR×倍率を `stop_distance` として送る。
- **value_per_point**: 価格が 1.0 動いたときの「1 単位あたり損益（口座通貨）」。
  **JPY 建てペア × JPY 口座は 1.0**（USDJPY が 1 円動く = 1 通貨あたり 1 円）。他は
  `RISK_VALUE_PER_POINT="EURUSD=150.0,..."` で銘柄ごとに設定（未指定は 1.0）。
- ストップが広すぎてリスク予算では `min_lot` に満たない発注は **却下**（`stop_too_wide_for_risk`）。
  これも「割に合わないトレードをしない」統制。
- `RISK_SIZING_ENABLED=0`（既定）ではシグナルの `qty` をそのまま使う（後方互換）。ただし
  **連敗縮小だけは効く**。有効化前に `ACCOUNT_EQUITY` と `RISK_VALUE_PER_POINT` を必ず実値に。

## 判断順（1 つでも引っかかれば却下）
`risk.py`: **Kill switch（Redis, fail-safe）** → 状態収集 → `risk_engine.evaluate`:
1. イベント・ブラックアウト → 2. 取引セッション → 3. 日次損失 → 4. 週次損失 →
5. 連敗停止 → 6. リスク基準サイジング → 7. 数量上限 → 8. 同時保有数 → 9. 通貨エクスポージャ
→（承認後）`risk.py`: **発注レート**。

日次/週次/連敗停止に当たると `trip_kill_switch=True` を返し、`risk.py` が Kill switch を
自動 ON にして通知する（手動 `make kill-off` で再開）。

## 同時保有・相関の扱い
- 現在ポジションは `fills` の符号付き合計（BUY=+ / SELL=−）で近似する。フラット⇄ロング/
  ショートに反転する戦略では正確。厳密な突合は `reconcile`（ブローカー実ポジション）に委ねる。
- **通貨レッグ分解**: `USDJPY` を +1000 持つ = `USD +1000 / JPY −1000`。複数ペアの
  「同方向 USD ロング」を合算して上限 `MAX_CURRENCY_EXPOSURE` で抑える。**エクスポージャを
  減らす方向（手仕舞い）の発注は、上限超過中でも許す**。

## トレード・ジャーナル（期待値で検証する）
`fills` に `intended_risk` / `stop_distance` / `realized_r` を記録し、`journal.py` が
**勝率に依存しない**指標を出す: 期待値・期待値R・PF・損益比・最大/現在連敗・合計損益。
```bash
make journal                 # 直近 30 日の成績
python journal.py --days 90 --json
```
`monitor` の毎朝 7 時（JST）サマリにも要約が載る。

## 運用の勘所
1. `ACCOUNT_EQUITY` を実残高に合わせる（サイジングの基準）。定期的に更新する。
2. `RISK_PER_TRADE_PCT` は初心者 0.25–0.5%、慣れても 0.75% を上限目安（個人モデル）。
3. `RISK_VALUE_PER_POINT` を取引ペアに合わせる（JPY 建て×JPY 口座以外）。
4. `MAX_WEEKLY_LOSS_JPY` / `MAX_CURRENCY_EXPOSURE` は既定 0（無効）。有効化を推奨。
5. `risk_calendar.json` を経済カレンダーから生成して更新（CPI/NFP/FOMC/ECB/BOJ/BOE 前後）。
   `app/risk_calendar.example.json` を雛形にする。ファイルは mtime 監視でホットリロード。
6. 連敗停止で Kill switch が ON になったら、**サイズを落として原因をレビューしてから** `make kill-off`。

## 限界（正直な明示）
- ポジション近似は発注ログ由来（部分約定・手動取引があると近似）。厳密性は `reconcile` で補う。
- `value_per_point` は静的設定（クロス円以外は要設定）。動的な為替換算は未対応（拡張点）。
- ブラックアウトはファイル運用（外部カレンダー API 連携は未実装）。
- 相関は「通貨レッグの同方向」を相関の代理にしている（統計的相関の推定は未実装）。
- これらは「個人が再現できる範囲で、退場率を下げる」ことを優先した設計判断である。
