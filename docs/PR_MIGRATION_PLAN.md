# Draft PR 移植計画（#2 / #3 / #4 / #5）

> **【2026-07-10 追記: この計画は失効（発注側スタック削除により無効）】**
> 本計画は `trader/`（webhook / risk / executor など発注スタック）の draft PR を
> `main` へ移植するためのものでした。その後、自動売買を取りやめて `trader/` を
> ディレクトリごと削除したため（→ [SYSTEM_OVERVIEW](../SYSTEM_OVERVIEW.md)）、
> 本計画の移植対象は現存しません。歴史的経緯の記録として残します。

作成: 2026-07-03 / 対象ブランチベース: `main` (`91d9648`)

## 背景

draft PR #2〜#5 はすべて **フラット再編前のコミット `0166d46` から分岐**しており、その後
`main` は 16 コミット先行（`trader/` サブツリー廃止 → フラット構成化 `2224f0a` を含む）。
このため全 PR が `CONFLICTING`。**マージ不可 → 現行 `main` への移植（再実装/cherry-pick）が唯一の道**。

全 PR は `trader/` スコープで、別途 push 済みの `fx_intel` 委員会作業（`feature/fx-intel-reliability`）とは領域が異なる。

| PR | 領域 | main 未取り込みの有用変更 | 方針 |
|---|---|---|---|
| #2 | webhook 堅牢化 | ✅ text/plain 取りこぼしバグ修正・freshness・publish 失敗解放 | 移植（close しない） |
| #3 | リスクエンジン | ✅ risk_engine/journal/DB マイグレーション | **#4 に内包 → close** |
| #4 | アドバイザリー分析＋ライブ整合 | ✅ 最大。#3 の上位互換 + robust backtest | 移植（#3 を吸収） |
| #5 | ATRストップ・祝日・自律最適化 | ⚠️ 一部。祝日は競合 | 有用部分のみ移植 → close |

---

## ① PR #2 — webhook（最優先・ライブバグ修正）

`main` の `trader/app/webhook.py` は `payload: dict = Body(...)` を使い **`application/json` のみ受理**。
TradingView は **`Content-Type: text/plain`** で送信するため FastAPI が **422 で全シグナルを破棄**する。
front は ngrok → uvicorn 直（Content-Type を書き換える proxy なし）なので**ライブ経路で実害**。

### 移植内容（main に統合）
- [ ] text/plain 生ボディを自前 `json.loads` でパース（**422 バグ修正**）
- [ ] `domain.py` に `signal_is_stale` を追加し、古い/未来 `{{timenow}}` シグナルを 409 拒否
- [ ] publish 失敗時に idem を解放して 503（dedup 永久ロスト防止）
- [ ] `MAX_BODY_BYTES`(64KB) DoS ガードは **main 側を残す**（PR#2 は落としているので統合）
- [ ] ブロッキング I/O を `run_in_threadpool` へ退避（PR#2 方式）

### 対象ファイル
- `trader/app/webhook.py`, `trader/app/domain.py`
- `trader/tests/test_webhook.py`, `trader/tests/test_domain.py`

---

## ② PR #4 — リスクエンジン（#3 を内包・最大）

#4 の `risk_engine.py` は #3 と **13 行差の上位互換** → **#3 は #4 経由で吸収し close**。

### 移植内容
- [ ] `trader/app/risk_engine.py`(432行) 新規
  - `RiskParams` / `RiskState` / `RiskDecision`
  - `position_size` / `evaluate`（中核）
  - 連敗スロットル: `loss_streak` / `streak_size_factor`
  - 相関エクスポージャ統制: `decompose_pair` / `net_currency_exposure` / `_exposure_breach`
  - イベント/薄商い統制: `in_blackout` / `in_thin_liquidity`
  - 報酬リスク比: `reward_risk_ratio`
- [ ] `trader/app/journal.py`（期待値ジャーナル）新規
- [ ] DB マイグレーション: `0001_risk_columns.sql` / `0002_daily_pnl_jst.sql` / `0003_fills_executions.sql`
- [ ] robust backtest（`robust.py`）を**現行フラット `fx_backtester/` へ配置し直し**（#4 では `trader/fx-codex/` 配下の再編前ツリー）
- [ ] `executor.py` / `strategy.py` / `webhook.py` の risk_engine 呼び出し配線

### テスト
- `test_risk_engine.py`(314行), `test_journal.py`, `test_services.py` 拡張

### 注意
- #4 の `trader/fx-codex/` は**再編前の重複ツリー**。現行フラット `fx_backtester/` へ読み替えて移植すること。

---

## ③ PR #5 — 有用部分のみ（祝日は破棄）

### 移植する
- [ ] ATR ストップ執行: `executor.py` に `compute_stop_price` / `stop_order_side` /
      `_place_protective_stop` / `_cancel_tracked_stop`（stop_distance 付きシグナルの実発注 + 失敗時 Discord 警告）
- [ ] `trader/app/export_history.py`（約定履歴エクスポート）新規
- [ ] 自律最適化: `deploy/optimize.sh` + `deploy/com.trader.optimize.plist`(launchd) +
      `auto_optimize.py` の `_export_history` / `_select_data_source`

### 破棄する（競合）
- ❌ `trader/app/holidays.py` + `trader/app/market_holidays.json`
  → 既存の `trader/app/market_calendar.py`（2024-2027 収録・12 関数、正規実装）と目的が衝突。

### 注意
- ATR ストップ移植時、現行 `strategy.py` が既に `stop_distance` を出力しているか確認し配線整合を取る。

---

## 実行順序

1. **PR #2 webhook**（ライブバグ修正・小）
2. **PR #4 risk_engine**（最大・#3 吸収）
3. **PR #5 有用部分**（ATRストップ・export・自律最適化）

各移植は現行 `main` から新ブランチを切り、`root job (pytest)` と `trader job (cd trader && pytest)`
を **別プロセスで**緑にしてから PR 化する（両 root を同時収集すると `auto_optimize` 名前衝突で
偽 failure が出るため — CI は 2 ジョブに分離済み）。
