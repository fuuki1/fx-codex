# P0 / P1 実装記録

**作成日:** 2026-08-06
**対象ツリー:** `[DEV]` `/Users/takahashifuuki/Desktop/fx-codex` HEAD `c327552`
**前提文書:** `docs/audits/NET_R_OUTLIER_FORENSIC_20260806.md`

**⚠️ 本PRは [DEV] のみを変更する。実機 `[PROD]` への配備は行っていない。**
PROD の `fx_intel/virtual_portfolio.py` は [DEV] と md5 不一致であり、
そのまま適用できない（`FX_CODEX_CURRENT_TO_TARGET_ARCHITECTURE_GAP_2026-08-06.md` §0）。

---

## 結論サマリ

| 項目 | 結果 |
|---|---|
| P0-A 評価系凍結 | 実装済（**第2次で rolling KPI の漏れを修正**） |
| P0-B canonical/quarantine分離 | 実装済（**第2次で隔離を回帰テストで固定**） |
| P0-C サイジング不変条件 | 実装済（**第2次で閾値を実測に基づき是正**） |
| P0-D ラベル品質 | 実装済（**第2次で指標名と定義を是正**） |
| P0-E 1d建玉経路 | **調査完了・コード欠陥ではない**（下記） |
| P1 collector可視化 | 実装済 + **真因を特定**（**第2次で工程別追跡を追加**） |
| 既存レコードのバックフィル | 実装済（**追記2**。canonical 44件・期待値 −0.2489R） |
| テスト | **1,289 passed**（既存failure 1件はbaselineでも失敗） |
| ruff / black / mypy | 全て green |

> **⚠️ 本文書は3部構成である。** 以下「第1次」の記述のうち、
> P0-C の閾値（`notional/risk ≤ 2000`, `RR ≤ 20`）と P0-D の指標名（`mark_coverage`）は
> **後半の追記で是正済み**であり、現在のコードとは一致しない。
> また「未実施: 既存50件への遡及適用」は**追記2で完了**している。
> 最終的な仕様は第2次・追記2を正とする。

---

## P0-E: 1d建玉2件の真因（**修正不要**）

**結論: ポリシー適用漏れではない。ゲート導入前の遺物である。**

### 実測

allowlist の実装は**1箇所**に集約されている（分散していない）:

| ファイル:行 | 役割 |
|---|---|
| `fx_intel/virtual_portfolio.py:164` `classify_virtual_horizon()` | 唯一の分類器。fail-closed |
| `fx_intel/virtual_portfolio.py:2054` | 判断処理での適用 |
| `fx_intel/virtual_portfolio.py:3402` | 保留判断での適用 |
| `fx_intel/virtual_portfolio_replay.py:879` | replay での適用 |

3箇所の呼び出しはすべて**同一の分類器**を使っており、独立した allowlist の重複定義は存在しない。

### 決定的証拠

```sql
-- 対象2件は decision_horizon_assignments に行が存在しない
SELECT decision_id FROM decision_horizon_assignments
WHERE decision_id IN ('af6863b61e484d5c927b','ab5ce300548c43184062');
-- → 0 rows

-- 割当の分布は正しい
SELECT timeframe,allocation_class,capacity_eligible,COUNT(*)
FROM decision_horizon_assignments GROUP BY 1,2,3;
-- → 1d|observation_only|0|3134   ← 1dは3,134件すべて正しく観測専用
--   4h|observation_only|0|3130
```

**時刻の比較:**

| 事象 | UTC |
|---|---|
| 1d建玉 (USDJPY) | 2026-07-29T15:06:16.485Z |
| 1d建玉 (EURUSD) | 2026-07-29T15:06:16.587Z |
| **最初の horizon assignment** | **2026-07-30T08:00:46.288Z** |
| 差 | **16.91 時間** |

→ 2件は**ゲート実装より 16.9 時間前**に建玉されている。
`SELECT COUNT(*) ... WHERE h.decision_id IS NULL` → **2**（この2件のみ）。
以降に建玉された48件はすべて割当を持つ。

**既存コードは既にこれを除外している**（`virtual_portfolio.py:3655` 付近）:

```sql
LEFT JOIN decision_horizon_assignments h ON h.decision_id = o.decision_id
WHERE ... AND (h.allocation_class IS NULL OR h.allocation_class <> 'intraday_capacity')
```

`tests/test_virtual_portfolio.py::test_legacy_unassigned_close_is_excluded_from_intraday_learning_and_kpi`
がこの挙動を固定している。

**判定: コード修正は不要。** 現在の allowlist は正しく機能しており、
2件は遡及적に除外済み。**未確認:** これら2件が建玉された当時の経路の詳細は追跡していない。

---

## P0-C: サイジング不変条件

`fx_intel/virtual_portfolio.py` に追加:

- `VirtualSizingInvariantPolicy`（dataclass）
- `classify_sizing_invariants()`（純関数・DB非依存）

呼び出し: `_build_open_values()` 内、`planned_risk` 確定直後。

| 不変条件 | 閾値 | 違反コード |
|---|---|---|
| 正のrisk distance | `> 0` | `risk_distance_not_positive` |
| microstructure floor | `rd ≥ 2 × spread` | `microstructure_floor` |
| notional上限 | `notional/risk ≤ 2000` | `notional_to_risk_ratio` |
| planned RR異常 | `RR ≤ 20` | `planned_reward_to_risk` |
| planned_risk正値 | `> 0` | `planned_risk_not_positive` |

**k=2.0 の根拠（1サンプルではなく50件の分布から）:**

実測 rd/spread: min 0.73 / p10 1.23 / **p25 4.89** / **median 11.03** / max 1273.97。
< 2.0 の6件は本体から明確に分離しており、うち5件は負け（−0.445〜−1.346R）、
1件が +46.406R。**k=1.0 では3件しか捕捉できず +46.4R を通す。**

⚠️ **k=2.0 は50件中6件(12%)を quarantine に落とす。**

---

## P0-B: canonical / quarantine 分離

- rawデータは**削除も改変もしない**。
- 違反時は `allocation_stream = "quarantine_shadow"` を付与し、記録は継続。
- 判定結果は `simulated_trade_opens.components_json.sizing_invariants` に永続化
  （`record_sha256` に含まれる）。
- 違反は `warnings_json` にも人間可読で残す。

---

## P0-A: 評価系の凍結

`summarize_net_r_robust()` を追加。**生の平均を単独で返さない**:

| 返却キー | 意味 |
|---|---|
| `mean_unsafe` | 生平均。**この名前でしか取得できない** |
| `median` / `trimmed_mean_10pct` | ロバスト推定 |
| `winsorized_mean_5r` / `winsorized_mean_10r` | 裾を抑えた平均 |
| `max_abs_leave_one_out_delta` | LOO影響度 |
| `single_sample_dominated` | 単一サンプル支配の検出 |

**safety gate の変更**（`virtual_portfolio.py` `_safety_learning_gate_reason()`）:

```python
# 変更前: mean_net_r = sum(...) / len(rows)
# 変更後: summary = summarize_net_r_robust(...); robust = summary["winsorized_mean_5r"]
```

**理由:** 生平均を使うと、+46.4R 相当のサンプルが1件混じるだけで
平均が −0.357R → +0.579R へ跳ね、**安全ゲートが自動的に解除される**。
winsorized なら1件では動かない。

**未実装（要判断）:** ダッシュボード等の表示側で `mean_unsafe` を単独描画している箇所の洗い出しは
本PRのスコープ外。`GAP` §1.1 の通り実機ダッシュボードは git外実装であり、[DEV] からは変更できない。

---

## P0-D: ラベル品質（**設計を実測に基づき変更**）

### 当初案とその棄却

当初は「mark数 ≥ 2」を提案したが、**実測で棄却した**。

```sql
SELECT n_marks, COUNT(*) FROM (...) GROUP BY n_marks;
-- 1 mark:  37 trades
-- 2 marks: 11
-- 5 marks:  1
-- 10 marks: 1
```

**固定件数では50件中37件(74%)を除外する。** かつ mark は保有時間に比例していた:

| marks | 件数 | 平均保有 | 最短 |
|---:|---:|---:|---:|
| 1 | 37 | 497s | **3.3s** |
| 2 | 11 | 1,424s | 178s |
| 5 | 1 | 62,630s | — |

→ mark は約5分間隔。**3.3秒しか保有していない取引が1markなのは正常**であり、
「疎」ではない。固定件数は短時間取引を誤って除外する。

### 採用した設計: mark **coverage**

```
expected_marks = max(1, holding_seconds / 300)
coverage       = mark_count / expected_marks
学習適格        = coverage ≥ 0.5
```

これにより:
- 3.3秒保有・1mark → coverage 1.0 → **適格**
- 522秒(8.7分)保有・1mark（+46.4R の取引） → expected 1.74 → coverage 0.57

⚠️ **+46.4R の取引は coverage 単独では除外されない。**
この取引は別途 `denominator_unstable` で除外される（P0-C 経由）。

### 除外理由コード

| コード | 条件 |
|---|---|
| `sparse_path_marks` | coverage < 0.5 |
| `denominator_unstable` | サイジング不変条件で分母崩壊 |
| `sizing_quarantined` | その他のサイジング違反 |
| `sizing_invariants_unknown` | ゲート導入前の既存レコード |
| `ambiguous_close` | `close_reason == "session_end"` |

`learning_eligible` は上記が**すべて空のときのみ True**。
判定は `attribution_json.label_quality` に永続化（`record_sha256` に含まれる）。

**発見した既存バグ:** `learning_eligible` は INSERT で**リテラル `1` がハードコード**されており、
ハッシュ計算値と乖離し得る状態だった。実値を bind するよう修正した。

**未実施:** 既存50件への遡及適用（`sizing_invariants_unknown` 扱いになる）。

---

## P0-D 残: +46.4R の実tick再生

**未実施。** Dukascopy tick への遡及照合が必要で、本PRでは行っていない。
`NET_R_OUTLIER_FORENSIC_20260806.md` §9 の未確認事項として残る。

MAE到達価格 160.2890 は stop 160.2899 の **0.09 pips手前**であり、
1サンプルからの導出であることは実測済み。

---

## P1: collector 可視化 + **真因特定**

### 真因（実測で確定）

`com.fx-codex.datafeed-collector` exit 69 の正体:

```
lag=4 exit=69   ← 失敗
lag=3 exit=0
lag=2 exit=0
lag=1 exit=0
```

`scripts/datafeed_collect_once.sh`（**PROD で未追跡**）は4つの lag を順に実行し、
`overall=$?` で**最後の非ゼロを保持**する。一方 `tools/fx_datafeed_collector.py` は
毎回 `collect/state/datafeed-collector.json` を**上書き**するため、
最後に成功した lag=1 の結果だけが残る:

```json
{"exit_code": 0, "results":[{"status":"ingested"}]}   ← 失敗した lag=4 の記録が消える
```

**つまり launchd は exit 69 を報告し、state ファイルは exit 0 を報告する。**
stderr は空。これが「失敗理由がどこにも無い」状態の正体だった。

さらに個別再実行では EURUSD / GBPUSD / USDJPY いずれも lag=4 で成功したため、
**この失敗は transient な fetch 失敗**であり、スクリプト冒頭のコメントが説明する
「次回で回収する」設計どおりの挙動である。**バグではなく、可観測性の欠如が問題。**

**⚠️ 実測の副産物:** collector の1回の実行は **5分超**かかる。
launchd の `StartCalendarInterval` は毎時2分。実行時間と間隔の関係は**未検証**。

### 実装

`tools/job_run_record.py`（新規）: 任意のコマンドをラップし1行のJSON記録を永続化する。

| 保存項目 | 実装 |
|---|---|
| exit code | `exit_code` |
| 実行コマンド | `command`（argv配列） |
| stdout/stderr | `stdout_tail` / `stderr_tail`（各4,000字） |
| working directory | `working_directory` |
| 対象commit/hash | `code_provenance.head` / `.branch` / `.dirty_tracked_files` |
| 最終成功時刻 | state file の `last_success_at` |
| 失敗原因分類 | `failure_class`（sysexits + signal + timeout） |

- **終了コードを変えない**（観測専用。スケジューラの挙動に影響しない）
- `dirty_tracked_files` を持つ理由: PROD は110件の tracked 変更を抱えており、
  SHA単独では実行コードを同定できない
- state file に `consecutive_failures` を保持し「いつから壊れているか」を答えられる

### 配備（**未実施**）

plist の `ProgramArguments` を以下のように包む想定。**本PRでは変更していない。**

```
.venv/bin/python tools/job_run_record.py \
  --name datafeed-collector \
  --record logs/job_runs.jsonl \
  --state logs/job_state/datafeed-collector.json \
  -- /bin/zsh scripts/datafeed_collect_once.sh
```

---

## 変更ファイル

| ファイル | 変更 |
|---|---|
| `fx_intel/virtual_portfolio.py` | P0-A/B/C/D。新規 public: `classify_sizing_invariants` / `summarize_net_r_robust` / `VirtualSizingInvariantPolicy` |
| `tests/test_virtual_portfolio.py` | 新規6件。既存3件に mark 追加（P0-D の契約変更に伴う） |
| `tools/job_run_record.py` | 新規（P1） |
| `tests/test_job_run_record.py` | 新規6件 |

## テスト結果

```
tests/test_virtual_portfolio.py     39 passed
tests/test_job_run_record.py         6 passed
全tracked tests                   1,274 passed, 1 failed
```

**唯一の failure** `test_learning.py::test_plan_embed_marks_moderate_aggressive_shadow_as_comparison_only`
は **baseline（変更をstashした状態）でも同様に失敗**することを確認済み。本変更とは無関係。

`tests/test_evaluation_gates.py` と `tests/test_lockbox.py` は
`ModuleNotFoundError: test_experiment_pipeline` で collection error（既知・HEADのcommit本文が言及）。

### 変異テストによる検証

`microstructure_floor_multiple` を 2 → 0 に改変すると
`test_sizing_invariants_quarantine_the_production_denominator_collapse` が
`assert False is True` で失敗することを確認。**ガードを外すと落ちる**ことを実証済み。

## 品質ゲート

```
ruff  : All checks passed
black : 4 files unchanged
mypy  : Success (fx_intel/virtual_portfolio.py, tools/job_run_record.py)
```

---

## 未実施・要判断

1. **既存50件の再分類（遡及適用）** — P0-A の「既存50件を再分類」は未実施。
   新レコードのみ判定される。遡及には別途バックフィルが必要。
2. **PROD への配備** — [DEV] と md5 不一致のため、そのまま適用できない。
3. **plist の書き換え** — P1 wrapper の実配線。人間の明示承認が必要（CLAUDE.md）。
4. **ダッシュボード表示側の `mean_unsafe` 単独描画の洗い出し** — 実機は git外実装。
5. **+46.4R の実tick再生** — 未実施。
6. **collector の実行時間(5分超) vs launchd間隔(毎時2分)** — 未検証。
7. **k=2.0 の運用判断** — 現状は建玉時に quarantine する実装。
   母集団が50件しかない状況で12%減らすことの是非は判断が必要。
   「まずフラグのみ、1週間実測後に拒否へ昇格」を推奨（`GAP` §8）。

**文書終端**

---

# 追記: コミット前の完了作業（2026-08-06 第2次）

## 1. P0-C 全条件の dry-run 実測（読み取り専用）

実機50件へ現在の分類器を適用した。**DBは一切更新していない。**
再実装ではなく `classify_sizing_invariants` を import して適用。

### ⚠️ 第1次実装の閾値は誤っていた（実測で発覚・修正済み）

初回 dry-run の結果:

| | 件数 |
|---|---:|
| canonical | 15 (30%) |
| **quarantine** | **35 (70%)** |

理由別: `notional_to_risk_ratio` **32件** / `microstructure_floor` 6 / `planned_reward_to_risk` 2。

**原因:** `notional/planned_risk` は代数的に `price / risk_distance` へ簡約される。

```
units    = maximum_risk / risk_distance   (virtual_portfolio.py:4427)
notional = units × price
⇒ notional/planned_risk = price / risk_distance
```

つまり microstructure floor と**冗長**であり、独立した情報を持たない。
EURUSD の**通常の5pips stop** ですら `1.15/0.0005 = 2300` となり、閾値2000を超えて誤検知する。

### 修正: notional/risk → gross leverage

実測した実レバレッジ（equity 1,000,000 JPY 基準）:

| 統計 | 値 |
|---|---:|
| min | 0.4x |
| p25 | 8.6x |
| median | **15.1x** |
| p75 | 22.6x |
| max | **199.1x** |

| 閾値 | 超過件数 |
|---|---:|
| >25x | 10/50 |
| >50x | 3/50 |
| **>100x** | **3/50** |
| >200x | 0/50 |

100x 超の3件は、いずれも stop がスプレッド内にある取引と**完全に一致**した。

planned RR の実測: median **0.98** / p90 4.02 / max 74.33。>10 は 4件。

**採用した最終閾値:**

| 条件 | 閾値 | 根拠 |
|---|---|---|
| `microstructure_floor_multiple` | **2.0**（維持） | rd/spread median 11.03, p25 4.89 |
| `maximum_gross_leverage` | **100x** | median 15.1x の6.6倍。通常運用を大きく上回る位置 |
| `maximum_planned_reward_to_risk` | **10**（20→10） | median 0.98, p90 4.02 |

### 最終 dry-run 結果

| | 件数 |
|---|---:|
| **canonical** | **43 (86%)** |
| **quarantine** | **7 (14%)** |

**理由別（重複あり）:**

| 理由 | 件数 |
|---|---:|
| `microstructure_floor` | 6 |
| `planned_reward_to_risk` | 4 |
| `gross_leverage` | 3 |

**重複理由の組み合わせ:**

| 件数 | 組み合わせ |
|---:|---|
| 3 | `microstructure_floor` + `gross_leverage` + `planned_reward_to_risk` |
| 3 | `microstructure_floor` 単独 |
| 1 | `planned_reward_to_risk` 単独 |

**quarantine 7件の全内訳:**

| trade_id | sym | tf | rd/spr | lev | RR | net_r |
|---|---|---|---:|---:|---:|---:|
| `sim-5ac6a341add4217390cf4667` | EURUSD | 15m | 0.73 | 151.2 | 13.17 | −1.143 |
| `sim-c4b2abe20569ef345606276b` | USDJPY | 15m | 0.95 | 199.1 | 74.33 | −1.090 |
| `sim-494a056d9b738b08308ae901` | EURUSD | 15m | 0.99 | 9.1 | 0.25 | −1.058 |
| `sim-2c0a5c81ca989fe60f9f06d2` | EURUSD | 1h | 1.11 | 8.1 | 0.11 | −1.346 |
| `sim-718c586007c93eec140f9dc6` | USDJPY | 1h | 1.23 | 154.5 | 46.22 | **+46.406** |
| `sim-071de1a9ed7ccf92334e4aac` | USDJPY | 15m | 1.57 | 9.5 | 0.75 | −0.445 |
| `sim-bcc011af28e8810e4e310057` | USDJPY | 15m | 2.84 | 20.0 | 15.08 | −1.146 |

**symbol別:** EURUSD 3/32 / USDJPY 4/18
**timeframe別:** 15m 5/25 / 1h 2/23 / 1d 0/2

**境界付近（k=2.0 近傍）:**

| trade_id | rd/spr | 判定 | net_r |
|---|---:|---|---:|
| `sim-071de1a9ed7ccf92334e4aac` | 1.566 | quarantine | −0.445 |
| `sim-f719c99af96a3a030dd5d4ae` | **2.289** | **canonical** | −0.556 |
| `sim-2c980cebcaa9df3417b2aed6` | 2.554 | canonical | −0.596 |
| `sim-bcc011af28e8810e4e310057` | 2.841 | quarantine（RR違反） | −1.146 |

**net-R への影響:**

| 母集団 | n | mean | median |
|---|---:|---:|---:|
| ALL | 50 | **+0.5786** | −0.5978 |
| **canonical** | **43** | **−0.2616** | −0.5559 |
| quarantine | 7 | +5.7397 | −1.0902 |

→ canonical 母集団の平均 **−0.2616R** は単一サンプルに支配されない安定した推定値。

**`risk_distance` / `planned_risk` 条件:** 50件中で違反 **0件**（いずれも正値）。
実装はしているが、この母集団では発火しない。

---

## 2. quarantine の評価系からの完全隔離

### コード

`learning_eligible = 1` によるフィルタが以下5箇所に存在し、
P0-D により quarantine 取引は `learning_eligible = 0` になる:

| 行 | 関数 | 用途 |
|---|---|---|
| 3213 / 3252 / 3379 | `export_learning_rows` | learning export・promotion |
| 3727 | `snapshot` | mature_learning_rows |
| 4912 | `_safety_learning_gate_reason` | safety gate |

### ⚠️ 回帰テストが実際の漏れを検出した

`research_kpis.rolling_20d` の集計クエリは `allocation_class` のみで絞っており、
**`learning_eligible` を見ていなかった**。テスト実行で発覚:

```
E  assert 8.2692 == 0.5384
```

quarantine 取引1件で expectancy が **0.5384 → 8.2692** へ変化していた。

**修正:** 集計側に `AND c.learning_eligible = 1` を追加し、
除外側 (`excluded_from_primary`) に `OR c.learning_eligible <> 1` を追加して
**両者が厳密に補集合**になるようにした（取引が両方から消えないことを保証）。
`reason` も `legacy_unassigned_or_non_intraday_or_label_ineligible` へ更新。

### テスト

`test_quarantined_trade_is_excluded_from_every_evaluation_surface`:
実機と同じ幾何（stop がスプレッド内）の取引を canonical 母集団へ追加し、以下が
**完全に不変**であることを固定:

- learning export の行数と net-R 値
- `rolling_20d.cost_adjusted_expectancy_net_r` / `.net_r` / `.trade_count`
- `mature_learning_rows`
- `summarize_net_r_robust(...)` の全出力

`test_quarantined_losses_do_not_arm_the_safety_gate`:
quarantine された3連続損失が safety gate を発火させないことを固定。

→ **winsorize による減衰ではなく、母集団からの除外**として実装されている。

---

## 3. label quality の定義修正

### 「coverage」は誤称だったため改名

`mark_coverage` → **`path_sampling_adequacy`**、
`MINIMUM_PATH_MARK_COVERAGE` → `MINIMUM_PATH_SAMPLING_ADEQUACY`。
contract は `virtual-trade-label-quality-v1` → **`v2`**。

理由: この値は「経路を観測できた割合」ではなく、
**期待される約5分間隔に対する観測数の比**にすぎない。

### コードに明記した内容（docstring）

| 項目 | 内容 |
|---|---|
| **何を測るか** | 期待cadence(300秒)に対する観測マーク数の比 |
| **何を保証しないか** | 価格経路を観測したこと、MFE/MAEが真の極値であること、stopに触れなかったこと。**マーク間の変動は比率が高くても不可視** |
| **sparse** | `path_sampling_adequacy < 0.5` |
| **ambiguous** | `close_reason == "session_end"`（自身のstop/target/time規則ではなくセッション処理による決済） |
| **学習適格性への影響** | 除外理由が1つでもあれば `learning_eligible = False` → learning export / safety gate / promotion / canonical KPI / robust集計から除外。**行の削除・改変はしない** |

出力にも `measures` と `does_not_guarantee` を機械可読なフィールドとして埋め込んだ。

### 追加した品質指標

`largest_observation_gap_seconds`: open→初回mark、mark間、最終mark→close を含む
**最大未観測区間**。比率が高くても長い空白があれば検出できる。

実機 +46.4R 取引は `path_sampling_adequacy = 0.57`(適格相当)でありながら
stop を 0.09 pips で回避しており、**この指標単独では検出できない**ことを
docstring とテストに明記した（除外は P0-C の `denominator_unstable` 経由）。

---

## 4. collector の工程別追跡

### 実測: launchd 起動間隔と多重起動

| 項目 | 実測値 |
|---|---|
| launchd schedule | `StartCalendarInterval { Minute = 2 }` = **毎時1回**（5分毎ではない） |
| 実行時間 | **300秒超** |
| 間隔 | 3,600秒 |
| 余裕 | 約3,270秒 |

**多重起動対策は既に存在する:** `tools/fx_datafeed_collector.py:140` が
`ExclusiveLock("fx-datafeed-collector")` を取得し、二重起動時は exit 75 (EX_TEMPFAIL) で終了する。

**残存ギャップ（記録のみ・今回未対応）:** ラッパーは4つの collector プロセスを
逐次起動するため、**ロックはジョブ全体をカバーしない**。ただし実行時間3,270秒の余裕があり、
現状の設定では重複しない。

### 実装

`tools/job_run_record.py` に `--step` / `--run-id` を追加（contract `v2`）。

| 要求項目 | 実装 |
|---|---|
| どのlagが失敗したか | `step`（例 `lag=4`）+ state の `steps` キー |
| 開始・終了時刻 | `started_at` / `finished_at` |
| 実行時間 | `duration_seconds` |
| exit code | `exit_code` |
| 失敗分類 | `failure_class` |
| 他のlagが成功したか | `steps` に各stepの `last_success_at` / `last_failure_at` を個別保持 |
| ジョブ全体の結果 | `run_id` で1回の起動をグループ化 |

**上書き防止:** 記録は append-only JSONL。state は step をキーとする辞書で、
**後続の成功が先行の失敗記録を消さない**。
これは実機の欠陥（lag=1 の成功が lag=4 の失敗を上書き）への直接の対策。

`test_per_step_failures_survive_later_successful_steps` が
lag=4 失敗 → lag=3/2/1 成功の順で実行し、lag=4 の失敗が残ることを固定。

---

## 5. 検証結果

| 項目 | 結果 |
|---|---|
| 全tracked test | **1,279 passed, 1 failed** |
| baseline由来の切り分け | 唯一の failure `test_learning.py::test_plan_embed_marks_moderate_aggressive_shadow_as_comparison_only` は**変更をstashしたbaselineでも同一に失敗**（確認済） |
| collection error | `test_evaluation_gates.py` / `test_lockbox.py` は `ModuleNotFoundError: test_experiment_pipeline`（既知・HEADのcommit本文が言及） |
| ruff | All checks passed |
| black | 4 files（1 reformatted） |
| mypy | Success（2 source files） |
| **変異テスト1** | `microstructure_floor_multiple` 2→0 で **2件失敗** |
| **変異テスト2** | rolling KPI の `AND c.learning_eligible = 1` 削除で **1件失敗** |
| DEV のみ | `git status` で PROD 未変更。実機への配備・plist変更は**未実施** |

### 敵対的レビューで確認した項目

| 観点 | 結果 |
|---|---|
| ゲート導入前の既存レコード | `sizing_invariants_unknown` で fail-closed（`learning_eligible=False`） |
| `components_json` の破損・空・非dict | すべて `None` を返し fail-closed |
| close の冪等性 | 既存4テスト（retry/conflict/immutable）が pass |
| 集計と除外の補集合性 | 両クエリを同時に修正し、取引が両方から消えないことを保証 |

---

## 6. 次工程（今回のコミットに**含めない**）

1. **既存50件のDBバックフィル** — 新規レコードのみ判定。遡及適用は別作業
2. **PROD 配備** — `virtual_portfolio.py` は md5 不一致のためそのまま適用不可
3. **plist 変更** — P1 wrapper の実配線（人間の明示承認が必要）
4. **+46.4R の実tick再生** — Dukascopy tick への遡及照合
5. **ダッシュボード表示側の `mean_unsafe` 単独描画の洗い出し** — 実機は git外実装
6. **ラッパー全体をカバーするロック** — 現状は余裕3,270秒で重複しないため優先度低

**追記終端**

---

# 追記2: 既存レコードのバックフィル（2026-08-07）

## 背景

P0-C の判定は**新規建玉にしか付かない**ため、既存レコードはすべて
`sizing_invariants_unknown` となり `learning_eligible = False` へ落ちる。
結果として **canonical 母集団が実質空**になり、エッジの符号を測れない状態が続いていた。

## 設計: サイドカー表（元帳を書き換えない）

`simulated_trade_opens` は `record_sha256` を持つ append-only 表であり、
**追記専用トリガで UPDATE / DELETE が禁止**されている。遡及付与のために
元行を書き換えることは、監査が依拠する来歴そのものを壊す。

したがって既存の `simulated_trade_open_observations` /
`simulated_trade_close_observations` と**同じ設計パターン**を採用した。

新表 `simulated_trade_sizing_assessments`:

| 列 | 内容 |
|---|---|
| `trade_id` | PK, FK → `simulated_trade_opens` |
| `canonical` / `denominator_unstable` | 0/1 |
| `allocation_stream` | `canonical` または `quarantine_shadow` |
| `assessment_basis` | `legacy_open_record_backfill` |
| `policy_sha256` | 判定に使ったポリシーの同定 |
| `assessment_json` | 判定の全内容 |
| `record_sha256` / `writer_id` | 既存表と同じ来歴契約 |

`_APPEND_ONLY_TABLES` へ登録し、追記専用トリガの対象とした。
`SCHEMA_VERSION` を **7 → 8** へ更新。

判定は建玉時に保存済みの値（bid/ask/executable/stop/target/units/planned_risk）
から再計算する。equity は建玉時点以前の直近 `portfolio_marks.cash_balance_jpy`
を用いる（実測: 建玉53件すべてに先行 mark が存在）。

## ⚠️ 実測で発見した追加の欠陥

サイドカーを入れただけでは**不十分だった**。

`simulated_trade_closes.learning_eligible` は append-only 列であり、
P0-C 導入前に書かれた行は**古い `1` を保持し続ける**。実測:

```sql
SELECT s.allocation_stream, c.learning_eligible, COUNT(*) ...
-- canonical        |1|46
-- quarantine_shadow|1|7   ← +46.4R を含む7件が依然として eligible
```

つまり quarantine 判定が出ていても、評価クエリには到達してしまう。

**対策:** 全6箇所の適格性フィルタを共有述語 `_LEARNING_ELIGIBLE_SQL` へ統一し、
サイドカーに `canonical = 0` の行があれば除外する `NOT EXISTS` を追加した。
サイドカー行が無い取引は従来どおり保存列にフォールバックする。

## 実測結果（実機DBのコピーに対して実行。PRODは未変更）

実機 `logs/fx_virtual_portfolio.sqlite3`（687MB）を `.backup` で整合スナップショット
取得し、ローカルコピーへ適用した。

| 項目 | 結果 |
|---|---|
| 移行時間 | **2.9秒**（687MB, 53建玉） |
| サイドカー行数 | 53 / 53（欠落なし） |
| canonical | **46** |
| quarantine | **7**（うち `denominator_unstable` 6） |
| +46.4R の判定 | `canonical=0`, `denominator_unstable=1` |

**元行の不変性（md5で照合）:**

```
vp_prod opens:  066c0b298e21e77ef47a7fb5edb0d61c
vp_test opens:  066c0b298e21e77ef47a7fb5edb0d61c   ← 一致
vp_prod closes: 524ebed4286a1b40cadf11f99933bcca
vp_test closes: 524ebed4286a1b40cadf11f99933bcca   ← 一致
```

**冪等性:** 移行を追加で3回実行してもサイドカーは53行のまま。

### バックフィル後の canonical 母集団

| 指標 | バックフィル前(全53件) | **バックフィル後(canonical)** |
|---|---:|---:|
| n | 53 | **44** |
| mean | +0.5786 相当 | **−0.2489** |
| median | −0.5978 | −0.5762 |
| winsorized@5R | −0.2495 | −0.2489 |
| **LOO最大影響度** | **0.9353** | **0.0616** |
| **単一サンプル支配** | **True** | **False** |

`rolling_20d` KPI も `trade_count=44` / `expectancy=−0.2489` を返し、
除外2件は legacy 1d（horizon 未割当）である。

→ **単一サンプル支配が解消され、母集団としてエッジの符号を議論できる状態になった。**
現時点の canonical 期待値は **−0.2489R**（負）。

## 検証

| 項目 | 結果 |
|---|---|
| 全tracked test | **1,289 passed**, 1 failed（baseline由来・確認済） |
| ruff / black / mypy | 全て green |
| 変異テスト | サイドカー override を削除すると
`test_backfilled_verdict_excludes_legacy_trades_from_evaluation` が失敗 |
| 元行の改変 | **なし**（md5一致で実証） |
| PROD | **未変更**（コピーに対してのみ実行） |

## 未実施（次工程）

1. **PROD への適用** — 移行はDBを開いた時点で自動実行される。
   実機適用は人間の承認が必要。実測2.9秒だがバックアップ取得を推奨
2. **`learning_exports` の生成** — 母集団は整ったが、学習ジョブは週次（金18:00）
3. **plist 変更 / +46.4R の実tick再生** — 従来どおり未着手

**追記2終端**
