# FX Codex 現状 → 目標アーキテクチャ差分

**作成日:** 2026-08-06
**目的:** 次に何を直すべきかを判断できる実行可能な差分文書
**方法:** 記憶・過去レポートを正としない。現在のコードと実機の実測を一次資料とする。

---

## 0. 本文書の観測基準

本文書は**2つの独立したツリー**を対象とする。両者はコア4モジュールすべてで内容が異なるため、
**1つの設計として統合しない。** 各主張がどちらの事実かを必ず明示する。

| 記号 | ツリー | パス | HEAD | branch | 状態 |
|---|---|---|---|---|---|
| **[DEV]** | 開発機 | `/Users/takahashifuuki/Desktop/fx-codex` | `c327552` | `feat/unified-decision-transaction-writer` | 追跡変更0件 / 未追跡258件 |
| **[PROD]** | Mac mini | `/Users/fuuki/srv/fx-codex` | `f473141` | `deploy/dashboard-wilson-20260729` | 追跡変更110件 / 未追跡110件 |

**実測したコア4モジュールの一致状況**（md5比較、2026-08-06）:

| ファイル | DEV vs PROD |
|---|---|
| `fx_briefing.py` | **DIFF** |
| `fx_intel/decision_log.py` | **DIFF** |
| `fx_intel/virtual_portfolio.py` | **DIFF** |
| `fx_intel/briefing.py` | **DIFF** |

→ **DEVのコードを読んでPRODの挙動を説明することはできない。** 本文書でコード行番号を引用する箇所は
特記なき限り**[DEV]** の実測値であり、PRODの同一関数が同一である保証はない。

**セッション中のHEAD変化（実測）:** セッション開始時スナップショットは `0aca644` かつ
`fx_intel/decision_log.py` がdirtyだったが、読み取り時点では `c327552`（`0aca644` の子）でクリーンだった。
別セッションがこのworktreeへコミットしている。**同一worktreeを複数セッションが変更し得る前提で読むこと。**

**未確認:** PROD の110件の追跡変更の内容は本作業では diff 取得していない（読み取り専用方針のため）。

---

## 1. 現状の実装済みアーキテクチャ

### 1.1 実行単位（[PROD] launchctl list 実測、2026-08-06）

| ジョブ | PID | 最終exit | 判定 |
|---|---|---|---|
| `com.fx-codex.snapshot` | 9688 | 0 | 稼働 |
| `com.fx-codex.horizon` | 9317 | 0 | 稼働 |
| `com.fx-codex.virtual-portfolio` | 9657 | 0 | 稼働 |
| `com.fx-codex.dashboard` | 85746 | 0 | 稼働 |
| `com.fx-codex.dashboard-state` | 9004 | 0 | 稼働 |
| `com.fx-codex.operational-read` | 90218 | **-15** | SIGTERM終了 |
| `com.fx-codex.virtual-portfolio-read` | 88611 | 0 | 稼働 |
| `com.fx-codex.briefing` | — | 0 | 稼働（間欠） |
| `com.fx-codex.health` | — | 0 | 稼働（間欠） |
| `com.fx-codex.quote-index` | — | 0 | 稼働（間欠） |
| `com.fx-codex.price-path-adapter` | — | 0 | 稼働（間欠） |
| `com.fx-codex.virtual-portfolio-close` | — | 0 | 稼働（間欠） |
| `com.fx-codex.virtual-portfolio-learning` | — | 0 | 週次（金18:00） |
| `com.fx-codex.monitors` | — | 0 | 稼働（間欠） |
| **`com.fx-codex.operational-sync`** | — | **2** | **失敗中** |
| **`com.fx-codex.datafeed-collector`** | — | **69** | **失敗中** |

計16ジョブ。SYSTEM_OVERVIEW.md が「正規設計」と記述する snapshot / briefing / health の**3本ではない**（→ §6.1）。

### 1.2 データストア（[PROD] 実測）

| ファイル | サイズ | 最終更新 | 判定 |
|---|---|---|---|
| `logs/fx_virtual_portfolio.sqlite3` | 650MB | 08-06 13:56 | 生存 |
| `logs/briefing_decisions.jsonl` | **2,012MB** | 08-06 13:55 | 生存・肥大 |
| `logs/briefing_tf_journal.jsonl` | 278MB | 08-06 13:55 | 生存 |
| `logs/briefing_horizon_forecasts.jsonl` | 167MB | 08-06 13:44 | 生存 |
| `logs/briefing_tf_prices.jsonl` | 62MB | 08-06 13:51 | 生存 |
| `logs/briefing_journal.jsonl` | 13MB | 08-06 13:42 | 生存 |
| **`logs/canonical_outcomes.jsonl`** | 197KB / **48行** | **08-04 08:06** | **停止（2日間）** |

**[DEV] の `logs/` は最終更新 08-05、大半が 07-18 で停止しており、実運用データではない。**
DEVのログから運用状態を判断してはならない。

### 1.3 仮想ポートフォリオDBスキーマ（[PROD] 実測）

21テーブル。損益契約に必要な列は**すべて実装済み**（`simulated_trade_closes`、NOT NULL制約付き）:

```
gross_market_pnl_jpy / executable_pnl_jpy / spread_quote_cost_jpy /
slippage_cost_jpy / commission_jpy / financing_jpy / conversion_cost_jpy /
net_pnl_jpy / realized_net_r / maximum_favorable_excursion_r /
maximum_adverse_excursion_r / attribution_json / record_sha256 / writer_id
```

→ **net-Rの「スキーマが無い」わけではない。契約は完成している。不足しているのは行数である（→ §2.2）。**

---

## 2. 実際のデータフローと意思決定フロー

### 2.1 判断経路（[DEV] 実測、行番号は現ツリーで再検証済み）

```
scripts/fx_briefing_once.sh:25   --per-timeframe --symbols USDJPY EURUSD
        ↓
fx_briefing.py:876  _run_per_timeframe()
        ↓
fx_intel/briefing.py:482  build_trade_plan()   ← リスクオフィサー（決定論ゲート）
        ↓
fx_briefing.py:1081  journal.append_timeframe_plans()   → briefing_tf_journal.jsonl
fx_briefing.py:1102  decision_log.append_decision_events() → briefing_decisions.jsonl
        ↓
scripts/fx_briefing_once.sh:51   （融合）--symbols USDJPY EURUSD GBPUSD
        ↓
fx_briefing.py:1960  journal.append_plans()
fx_briefing.py:1980  decision_log.append_decision_events()
```

### 2.2 判断 → 学習の実測ファネル（[PROD]、`fx_virtual_portfolio.sqlite3`）

これが本文書の中心的な実測結果である。

```
decision_records          13,738
      ↓
decision_intakes          13,734   (−4)
      ↓
simulated_trade_opens         50   (−13,684 = 99.64%が棄却)
      ↓
simulated_trade_closes        50
      ↓
learning_eligible = 1         50
      ↓
learning_exports               0   ← 学習へ1件も到達していない
```

**disposition 実測:** `abstained` 13,688 / `opened` 50
**pit_eligible 実測:** 1 → 13,716 / 0 → 22

### 2.3 棄却理由の実測内訳（`rejection_reasons_json` 上位8件）

| 件数 | 理由 |
|---:|---|
| 4,196 | `4h/1dは観測専用で仮想ポジション枠を使用しない` |
| 2,052 | `市場休場中` + `4h/1dは観測専用…` |
| 1,392 | `学習安全ゲート:同一symbol/timeframe/directionで3連続純損失のため新規仮想建てを停止` |
| 1,018 | `市場休場中` + `方向判断がlong/shortではない` + `実行可能bid/askが無い` + `stop must be numeric` |
| 1,018 | 同上 + `quote-to-JPY conversion rate is missing` |
| 982 | `方向判断がlong/shortではない` + `実行可能bid/askが無い` + `stop must be numeric` |
| 918 | 同上 + `quote-to-JPY conversion rate is missing` |
| 654 | `同時保有上限2件に到達` |

**構造的読み取り:**

1. **timeframe別 decision_records 実測:** `15m` 3,319 / `1h` 3,314 / `4h` 3,320 / `1d` 3,320 / `fusion` 465。
   4h+1d = **6,640件（48.3%）が設計上ポジションを持てない**。つまり判断の約半分は最初から建玉対象外。
2. **`実行可能bid/askが無い` が3,936件に出現。** これは datafeed-collector（exit 69）の失敗と整合する（→ §7.2）。
3. **学習安全ゲートが1,392件を棄却。** 3連続純損失で停止する設計。50件しか建っていない母集団に対して
   このゲートが1,392回発火している = **エッジ測定に必要なサンプルをゲート自身が枯渇させている**（→ §8 P2）。

### 2.4 net-R の実測分布（[PROD]、50件全数）

| 統計 | 値 |
|---|---|
| 平均 | **+0.5786 R** |
| 最小 | −1.3456 R |
| 最大 | **+46.4061 R** |
| 勝ち | 17 / 50 |
| 決済理由 | `stop_loss` 23 / `target1` 14 / `time_exit` 11 / `session_end` 2 |

**`|net_r| ≤ 5` で除外した場合: 49件、平均 −0.3566 R**

→ **単一サンプル（+46.4R）がエッジの符号を反転させている。**
50件の母集団に対し1件の病的サンプルが平均を +0.579R に押し上げているが、
それを除くと **−0.357R** で負である。現状のnet-R平均は統計的に意味を持たない。

**推論（実測ではない）:** +46.4R は planned_risk_distance が異常に小さい（spread ≧ stop）ケースで
R正規化の分母が潰れた結果と考えられる。これは PR#91 の監査所見と整合するが、
本作業では当該trade_idの `attribution_json` を開いて確認していないため**未確認**。

---

## 3. 目標の9-planeアーキテクチャ

`FX_CODEX_TARGET_ARCHITECTURE_INTEGRATION_PLAN.md`（Downloads配下、リポジトリ未収録）が定義する到達点。

| # | Plane | 責務 |
|---|---|---|
| 1 | Data Truth | raw ledger → consensus → canonical PIT → feature store |
| 2 | Research & Digital Twin | exact replay / fill simulator / walk-forward / registry |
| 3 | Alpha Factory | 承認済みartifactによるオンライン推論のみ |
| 4 | Uncertainty & Arbitration | 分布・較正・OOD・不一致・棄権ゲート |
| 5 | Strategy & Capital Allocation | net edge → budget → MPC → trade intent |
| 6 | Independent Safety | 独立した BLOCK / CANCEL_ALL / REDUCE / FLATTEN |
| 7 | Execution & State | OMS / broker adapter / reconciliation |
| 8 | Attribution & Learning Feedback | TCA / 帰属分解 / 反実仮想 / 正準ラベル |
| 9 | Governance・Resilience・Security | registry / shadow→canary→promotion / 観測性 |

**重要な前提の訂正:** 本リポジトリは CLAUDE.md により**恒久的に research/shadow 専用**であり、
broker発注経路を持たない。したがって **Plane 6 の緊急執行経路（REDUCE/FLATTEN/CANCEL_ALL）と
Plane 7 の OMS/broker/reconciliation は、本システムでは実装対象が存在しない。**
統合計画のこの部分は broker接続を前提にした記述であり、そのまま適用してはならない（→ §10）。

---

## 4. 現状 → 目標 対応表

凡例: ✅実装済 / 🟨部分実装 / 🔌配線切れ / ❌未実装 / 🚫対象外

| Plane | 状態 | 実装の所在（[DEV]） | 実測根拠 |
|---|---|---|---|
| 1 Data Truth | 🟨 | `data_platform/raw/event_ledger.py`（未追跡）、`docs/audits/PHASE1_RAW_LEDGER_COMPLETION_20260802.md` | Phase0/1完了文書あり。ただし実装ファイル群は**未追跡** |
| 2 Research/Replay | 🟨 | `fx_backtester/walk_forward.py`, `overfitting.py`（PBO/DSR） | backtester側は実装済。実quote replayは未接続 |
| 3 Alpha Factory | ✅ | `fx_intel/committee.py`, `ml.py`, `gbm.py` | 稼働中。ただしML/macro委員は shadow 固定 |
| 4 Uncertainty | 🟨 | `fx_intel/briefing.py:482` `build_trade_plan()` | 棄権ゲートは稼働（13,688件 abstained）。分布・OOD・不一致は未接続 |
| 5 Capital Allocation | 🟨 | `fx_intel/virtual_portfolio.py`（5,220行） | 同時保有上限2件のみ。budget/MPC/相関制約なし |
| 6 Independent Safety | 🚫 | — | broker発注なし。対象外 |
| 7 Execution & State | 🚫 | — | broker発注なし。対象外 |
| 8 Attribution/Labels | 🔌 | `fx_intel/canonical_outcome_pipeline.py`（508行）, `virtual_portfolio.py:2897` `export_learning_rows()` | **スキーマ・関数は完成、出力0件** |
| 9 Governance | 🟨 | `fx_backtester/governance.py`, `fx_intel/promotion.py:264` `evaluate_shadow_member()` | 実装あり。end-to-end未接続（SYSTEM_OVERVIEW.md:118 が明記） |

---

## 5. 未実装・部分実装・配線切れ・重複実装

### 5.1 配線切れ（コードは存在するが呼ばれていない）— 最重要

**(a) `append_decision_transaction` の本番呼び出しが0件**

実測（[DEV]、`git grep`）:

```
fx_intel/decision_log.py:396   def append_decision_transaction(...)   ← 定義
tests/test_decision_transaction_writer.py                             ← 唯一の呼び出し元
```

本番経路（`fx_briefing.py`）は依然として旧APIを呼ぶ:

```
fx_briefing.py:1081  journal.append_timeframe_plans()
fx_briefing.py:1102  decision_log.append_decision_events()
fx_briefing.py:1960  journal.append_plans()
fx_briefing.py:1980  decision_log.append_decision_events()
```

→ HEAD `c327552` は「本番書き込みを append_decision_transaction へ統合」と題しているが、
**実測では本番の呼び出し元は移行されていない。** commit本文も「呼び出し側の移行は別PRで行う」と
明記しており、コミット件名と実際のスコープが一致していない（→ §6.2）。

**(b) `learning_exports` が0件**

`export_learning_rows()` は `fx_intel/virtual_portfolio.py:2897` に実装され、
`com.fx-codex.virtual-portfolio-learning` が週次で実行している。
実測出力（[PROD] `logs/launchd/virtual-portfolio-learning.out.log`、**最終 08-01 18:00**）:

```json
{"ok": true, "status": "insufficient_data",
 "eligible_for_candidate_training": false, "promotion_eligible": false}
```

→ **これは故障ではない。ゲートが正しく `insufficient_data` を返している。**
50件（うち病的1件）では学習を開始できないという正しい判定。
**修正すべきは学習側ではなく、上流のサンプル供給（→ §8 P1/P2）。**

**(c) `canonical_outcomes.jsonl` が48行で停止**

最終更新 08-04 08:06。`fx_intel/canonical_outcome_pipeline.py` の唯一の呼び出し元は
`tools/canonical_outcome_pipeline.py:18` で、**launchd テンプレートに登録が無い**
（`grep -l canonical_outcome ops/launchd/*.tmpl` → NONE）。
→ 手動実行専用。定期実行されていないため、48行は監査時の一回限りの出力と推定される（**推論**）。

### 5.2 重複実装

| 重複 | 実測 |
|---|---|
| 判断writer 3系統 | `journal.append_plans` (journal.py:273) / `append_timeframe_plans` (journal.py:367) / `decision_log.append_decision_events` (decision_log.py:476) / `append_decision_transaction` (decision_log.py:396) の**4API並存**。HEADのcommitは「後方互換のため残す」と明記 |
| 仮想ポートフォリオ | `virtual_portfolio.py`(5,220行) と `virtual_portfolio_v2.py`(2,109行) が並存 |
| 未追跡ファイルの " 2.py" 複製 | `fx_intel/` 配下に `operational_read_api 2.py` `3.py` `4.py` など**同一名の番号付き複製**が多数（Finder複製の痕跡） |
| launchd tmpl 複製 | `ops/launchd/` に `... .plist 2.tmpl` `3.tmpl` `4.tmpl` が多数 |

### 5.3 未追跡だが本番稼働している実装

**`scripts/datafeed_collect_once.sh`（[PROD]）**

```
git ls-files --error-unmatch → error: pathspec did not match any file(s) known to git
ls -la → -rwxr-xr-x 1,944 bytes  Aug 6 08:53
```

→ **launchd が実行しているスクリプトが git に存在しない。** 消失した場合、gitから復元できない。
かつこのジョブは exit 69 で失敗し続けている。

---

## 6. 設計書と実コードの不一致

### 6.1 SYSTEM_OVERVIEW.md

| 記述 | 実測 | 判定 |
|---|---|---|
| `SYSTEM_OVERVIEW.md:14-16`「launchdの snapshot / briefing / health **だけ**を定期実行する」 | [PROD] に**16ジョブ**がロード済 | **不一致** |
| `SYSTEM_OVERVIEW.md:36` 図（3ジョブ構成） | 同上 | **不一致** |
| `SYSTEM_OVERVIEW.md:148-151`「単一writer保証がない…未解決のリスク」 | 4writer API並存を実測で確認 | **一致**（正直な記述） |
| `SYSTEM_OVERVIEW.md:118` governance「end-to-end orchestrationへ未接続」 | `learning_exports`=0 で整合 | **一致** |
| `SYSTEM_OVERVIEW.md:6-10` analysis-only / 発注なし | broker経路の実装を発見せず | **一致** |

→ SYSTEM_OVERVIEW.md は**設計方針の記述としては正確だが、稼働ジョブ数において現実と乖離している。**
本文書 §1.1 が実測の正である。

### 6.2 コミットメッセージと実スコープ

HEAD `c327552` の件名「本番書き込みを append_decision_transaction へ統合」に対し、
実測では本番呼び出し元は未移行（§5.1a）。本文には正しく「呼び出し側の移行は別PRで行う」とある。
→ **件名だけでスコープを判断すると誤る**（CLAUDE.md「コミット件名だけでscopeを推測せず」と整合）。

### 6.3 PHASE_EXECUTION_STATE.md

`docs/implementation/PHASE_EXECUTION_STATE.md:4-11` は Verdict **FAIL**、
Mac mini HEAD `f473141` / `deploy/dashboard-wilson-20260729` と記録。
→ **本作業の実測（`f473141` / 同branch）と完全一致。** この文書は現在も有効。
記載の「110 tracked modifications」も実測110件と一致。

---

## 7. 運用上の単一障害点と観測不能箇所

### 7.1 単一障害点

| # | SPOF | 実測根拠 | 影響 |
|---|---|---|---|
| S1 | `scripts/datafeed_collect_once.sh` が**git外** | §5.3 | 消失時に復元不能。かつ現在 exit 69 で失敗中 |
| S2 | `logs/briefing_decisions.jsonl` **2,012MB 単一ファイル** | §1.2 | 破損時に全判断履歴を喪失。全読込コストも過大 |
| S3 | `fx_virtual_portfolio.sqlite3` **650MB 単一ファイル** | §1.2 | 建玉・損益・帰属の唯一の正 |
| S4 | 4つの判断writer APIに横断的単一writer保証なし | §5.2, `SYSTEM_OVERVIEW.md:148` | 別チェックアウトからの直接実行を防げない |
| S5 | PROD が110件のdirty変更を保持 | §0 | 実機コードがgitのどのSHAとも一致しない |

### 7.2 観測不能箇所（silent failure）

**最重要: 失敗しているジョブのstderrが空**

```
[PROD] logs/launchd/datafeed-collector.err.log   0 bytes,  Jul 28 13:07 以降更新なし
       （launchctl の最終exitは 69）
[PROD] logs/launchd/operational-sync.err.log     ファイル自体が存在しない
       （launchctl の最終exitは 2）
```

→ **2つのジョブが失敗し続けているのに、失敗の理由がどこにも記録されていない。**
`datafeed-collector` の失敗は §2.3 の `実行可能bid/askが無い`（3,936件）と時系列的に整合する（**推論**）。

| # | 観測不能箇所 | 実測 |
|---|---|---|
| O1 | datafeed-collector の失敗理由 | err.log が 0 bytes（Jul 28以降） |
| O2 | operational-sync の失敗理由 | err.log が存在しない |
| O3 | `canonical_outcomes.jsonl` の停止 | 08-04で停止しているがアラートなし |
| O4 | learning 週次結果 | 08-01が最終。`insufficient_data` が5週間続いても通知経路なし |
| O5 | PROD の110件dirty差分 | 内容未取得（**未確認**） |
| O6 | net-R の病的サンプル | +46.4R が平均を反転させているが検出ゲートなし |

---

## 8. 優先順位付き移行計画

**前提:** 目標は9-planeの形式的完成ではなく、**エッジの符号を測定可能にすること**。
現状 net-R は50件・うち1件が符号を支配しており、**測定不能**である。

---

### P0 — net-R の病的サンプルを検出・除外可能にする

**問題:** 平均 +0.579R / 病的1件除外で −0.357R。単一サンプルが符号を決めている（§2.4）。

- **変更:** `planned_risk_distance` に下限ガード（`stop_distance ≧ k × spread`、初期 k=2）を入れ、
  違反サンプルに `label_quality` フラグを立てる。既存50件は遡及フラグのみ（データ削除しない）。
- **依存:** なし。単独で実施可能。
- **期待効果:** net-R平均が単一サンプルに支配されなくなる。以降のサンプルで病的ケースが混入しない。
- **失敗リスク:** k が大きすぎると建玉がさらに減る（現状50件から悪化）。**低めのkから開始し、
  除外件数を実測してから上げる。**
- **検証方法:** ①`ABS(realized_net_r)>5` の件数が0であること ②フラグ付与前後で
  `realized_net_r` の平均・中央値を両方記録 ③既存50件に対する遡及適用で除外が1件（+46.4R）であることを確認。

---

### P1 — datafeed-collector の失敗を可視化する

**問題:** exit 69 で失敗し続けているが stderr が0バイト。`実行可能bid/askが無い` 3,936件と整合（推論）。

- **変更:** ①`scripts/datafeed_collect_once.sh` を **git管理下に置く**（S1解消）
  ②stderrが実際に書かれるようリダイレクトを修正 ③exit非0時にhealth経路へ通知。
- **依存:** なし。
- **期待効果:** 建玉を阻害している最大要因の一つが特定可能になる。
- **失敗リスク:** 低。観測を足すだけで挙動を変えない。
- **検証方法:** 次回実行後 `err.log` のサイズが0でないこと。exit codeの意味を特定できること。

---

### P2 — 学習安全ゲートの発火条件を再設計する

**問題:** 「3連続純損失で停止」が **1,392件を棄却**。母集団50件に対し過剰。

- **変更:** ゲートを**無効化しない**。発火閾値を「3連続」から
  「3連続 **かつ** 当該セルのサンプル数 ≧ N」へ変更（統計的裏付けのない停止を防ぐ）。
- **依存:** **P0の完了後に実施。** P0前は net-R の符号自体が信頼できないため、
  「純損失」の判定基盤が無い。
- **期待効果:** サンプル枯渇の緩和。エッジ測定に必要な件数が蓄積し始める。
- **失敗リスク:** **中。** ゲートを緩めると、本当に負のセルで損失を積む。
  これは仮想口座なので資本毀損は無いが、学習データが負のサンプルで汚染される。
  → **N の初期値を保守的に置き、棄却件数の変化を1週間実測してから次の判断をする。**
- **検証方法:** 変更前後で①`rejection_reasons_json` の当該理由の件数 ②`simulated_trade_opens` の増加数
  ③増加分の net-R 平均（P0のフラグ適用後）を比較。

---

### P3 — 4h/1d を観測専用から外すかを決める

**問題:** 4h+1d で **6,640件（48.3%）** が構造的に建玉対象外（§2.3）。

- **変更:** 本PRでは**変更しない。決定のための計測のみ**行う。
  4h/1dの判断に対し「もし建てていたら」の反実仮想net-Rを shadow で算出する。
- **依存:** P0（net-R契約）完了後。
- **期待効果:** 判断の半分を捨てている設計が正しいかを、意見でなく数値で判定できる。
- **失敗リスク:** 低（shadowのみ）。ただし §10 の通り、**建玉解禁の判断は本計画に含めない。**
- **検証方法:** 4h/1d の反実仮想 net-R が15m/1hと比較可能な形で出力されること。

---

### P4 — 判断writerを1本化する

**問題:** 4つのwriter APIが並存し、`append_decision_transaction` は本番未接続（§5.1a, §5.2）。

- **変更:** `fx_briefing.py:1081/1102`（per_timeframe）と `1960/1980`（fusion）を
  `append_decision_transaction` へ移行。旧APIは残置。
- **依存:** **P0〜P2より後。** これは正しさの改善であってエッジ測定を前進させない。
  かつ HEADのcommit本文が警告する通り、**再送がfail-closedになる**運用契約変更を伴う。
- **期待効果:** full/compact の原子性が保証され、cross-log整合が取れる。
- **失敗リスク:** **高。** commit本文が明記する通り、トランザクション再実行時に
  同一transaction_idで内容の異なるcommitが2つ並ぶと `load_commits` が**両方破棄**する。
  本番の再送・クラッシュ復旧手順を先に確定しないと、判断が消える。
- **検証方法:** ①clean cloneでのpytest全通過 ②本番投入前にshadowで1週間の並行書き込み
  ③`load_commits` の衝突破棄が0件であること。

---

### 実施順序と依存

```
P0 (net-R病的サンプル) ──┬─→ P2 (安全ゲート再設計)
                         └─→ P3 (4h/1d 反実仮想計測)
P1 (collector可視化) ────── 独立・即時実施可

P4 (writer 1本化) ← P0/P1/P2 の後
```

**P0とP1は依存が無く、同時に着手できる。P1は観測追加のみでリスクが最も低い。**

---

## 9. 今は導入しない方がよい要素

| 要素 | 理由（実測に基づく） |
|---|---|
| **Plane 6 / Plane 7（OMS・broker・reconciliation・FLATTEN）** | CLAUDE.md により恒久的にbroker発注なし。**実装対象が存在しない。** 統合計画のこの部分はbroker前提の記述であり適用しない |
| **守り80:攻め20 / 攻勢重み ρ の動的配分** | 前提は「防御モードで期待値が正」。実測は病的1件除外で **−0.357R**。負のエッジに配分を増やす設計になる |
| **Transformer / Distributional RL / CPO などのchallengerモデル** | 統合計画 Priority 7 自身が「canonical net-R coverageが十分」を条件としている。現状 **50件**。条件未達 |
| **MPC / Capital Allocator** | 同時保有上限2件・建玉50件の規模で最適化する対象が無い |
| **確信度較正の継続利用** | 過去実測で Brier 0.318 > base 0.247。**本作業では未再測定（未確認）**だが、再測定するまで新規に依存を増やさない |
| **9-planeへの大規模ディレクトリ移行** | 統合計画 §6 自身が「既存モジュールを即時移動しない」と指示。現在257件の未追跡ファイルがあり、移動は追跡不能な差分を増やす |
| **GBPUSD の建玉解禁** | `scripts/fx_briefing_once.sh:25` の symbols 修正は1行だが、**P0完了前に母集団を増やすと病的サンプルが混入する。** P0後に実施 |

---

## 10. 本文書の限界（未確認事項）

以下は**確認していない**。推測で補completeしていない。

1. **[PROD] の110件の追跡変更の内容**（読み取り専用方針のため diff 未取得）
2. **+46.4R サンプルの `attribution_json` 実体** — 病的である「推論」の直接確認は未実施
3. **[PROD] のコードが [DEV] のどの行に対応するか** — コア4モジュールすべてが md5 不一致。
   §2.1 の行番号は [DEV] の値であり、PROD の同一関数が同一である保証はない
4. **datafeed-collector の exit 69 の意味** — stderr が空のため原因未特定
5. **operational-sync の exit 2 の意味** — err.log 不在のため原因未特定
6. **`canonical_outcomes.jsonl` が08-04で停止した理由** — launchd未登録は確認したが、
   48行がいつ・何によって書かれたかは未確認
7. **確信度較正の現在値** — 本作業で再測定していない
8. **他worktree（26個）の内容** — `git worktree list` で存在は確認したが内容は未調査

---

## 付録: 本文書の実測コマンド

再検証のため、主要な数値の取得元を記す。

```bash
# ツリー状態
git rev-parse HEAD && git status --porcelain | awk '{print $1}' | sort | uniq -c

# DEV/PROD 差分
md5 -q fx_briefing.py
ssh fuuki@trader-mini "md5 -q /Users/fuuki/srv/fx-codex/fx_briefing.py"

# launchd
ssh fuuki@trader-mini "launchctl list | grep fx-codex"

# ファネル
ssh fuuki@trader-mini "sqlite3 /Users/fuuki/srv/fx-codex/logs/fx_virtual_portfolio.sqlite3 \
  'SELECT COUNT(*) FROM decision_records;'"

# net-R
ssh fuuki@trader-mini "sqlite3 ... 'SELECT AVG(realized_net_r) FROM simulated_trade_closes;'"
ssh fuuki@trader-mini "sqlite3 ... 'SELECT COUNT(*), AVG(realized_net_r) \
  FROM simulated_trade_closes WHERE ABS(realized_net_r)<=5;'"

# 配線切れ
git grep -n "append_decision_transaction" -- '*.py'
```

**文書終端**
