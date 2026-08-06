# 反実仮想 Stop 方針の事前登録

**作成日:** 2026-08-07
**対象コホート:** `timeframe = 1h` かつ `direction = short` の canonical 建玉
**目的:** 「stopを広げれば `1h × short` の損失は減ったか」を、事後最適化を伴わずに測る

---

## ⚠️ 本文書の位置付け

`fx_intel/virtual_portfolio.py:3028` の `alternative_stop_target` は現在
`status = "unavailable"` であり、理由は:

> 事前登録された代替Stop/Target方針が無いため事後最適化を禁止

**本文書はその「事前登録」である。**

**この文書は replay を1件も実行する前に確定させ、コミットする。**
結果を見てから方針を書き換えることは、この文書の目的そのものを破壊する。
方針を変更する場合は、**新しい文書として別に事前登録し、両方の結果を報告する。**

---

## 1. 事前に固定する方針

### 1.1 検証する仮説

> `1h × short` の損失は、stop が近すぎることによる早期の被stopが原因である。
> stop を機械的に広げれば、被stop率が下がり期待値が改善する。

### 1.2 対立仮説（棄却されうる側）

> stop を広げても期待値は改善しない。被stop率は下がるが、
> 1件あたりの損失額が比例して増えるため、net-R の期待値は変わらないか悪化する。

**⚠️ `NEGATIVE_EDGE_ATTRIBUTION_20260807.md` §3 の実測では、USDJPY において
stop が広い群のほうが平均Rが低かった（<5p で +0.476、≥5p で −0.289）。
対立仮説は実際に支持されうる。**

### 1.3 代替 stop 方針（3案・固定）

各建玉について、**entry と target は一切変更しない**。stop 距離のみを倍率で変更する。

| 方針ID | 定義 |
|---|---|
| `stop_x1_0` | 実際の stop（対照群。再現性の検証に使う） |
| `stop_x1_5` | stop 距離を **1.5倍** |
| `stop_x2_0` | stop 距離を **2.0倍** |

```
new_stop = entry_executable + sign * (|entry_executable - original_stop| * multiple)
  sign = +1 (short: stop は entry より上)
```

**倍率の根拠:** `NEGATIVE_EDGE_ATTRIBUTION_20260807.md` §3 の実測で、
被stop率は stop<5pips で 57.7%、5–10pips で 23.1% だった。
コホートの stop 距離中央値を1.5〜2.0倍すると概ねこの境界を跨ぐ。
**倍率はこの分布から先に決めており、結果を見て選んでいない。**

### 1.4 リスク額の扱い（重要）

**`planned_risk_jpy` を固定し、units を再計算する。**

```
new_units = planned_risk_jpy / (new_stop_distance * conversion_rate)
```

理由: stop を広げたまま units を据え置くと、1取引のリスク額が1.5〜2.0倍に増える。
それは「同じ戦略でstopだけ変えた」比較ではなく「リスクを増やした」比較になる。
R正規化の分母を固定するため、**リスク額固定・枚数可変**とする。

したがって net-R の分母は3方針で同一であり、直接比較できる。

### 1.5 決済判定

`fx_intel/canonical_label_path_verifier.py:272` `_independent_exit` と同一の
tick-walk 意味論を用いる:

1. 建玉時刻から horizon 終端まで、tick を時系列順に走査
2. short の executable は **ask**
3. `ask >= new_stop` なら `stop_loss`、`ask <= target` なら `target1`（先着優先）
4. どちらにも到達しなければ horizon 終端の tick で `time_exit`

**同一 tick 内で stop と target の両方を跨ぐ場合は `stop_loss` を採用する**
（不利側優先。`SYSTEM_OVERVIEW.md:78` の約定モデルと整合）。

### 1.6 コスト

実測コスト率を新しい枚数へ比例配分する。

```
new_cost = original_cost * (new_units / original_units)
```

spread / slippage / commission / financing / conversion のすべてに適用する。
**コストをゼロと仮定してはならない**（`CLAUDE.md` 非交渉ルール）。

### 1.7 対象母集団

- `timeframe = 1h` かつ `direction = short`
- **かつ `simulated_trade_sizing_assessments.canonical = 1`**

quarantine された建玉（+46.4R を含む）は**除外する**。
分母が壊れた建玉で stop 倍率を論じても意味がない。

### 1.8 tick データ要件

- 期間: 建玉時刻から horizon 終端まで
- ソース: `collect/log/quotes.jsonl` + `collect/index/quotes.sqlite3`
- 実測カバレッジ: 2026-07-28 〜 08-06（コホートは 07-30 〜 08-03 で**内包される**）
- **1件でも tick が欠落する建玉は `unavailable` とし、推定で埋めない**

---

## 2. 事前に固定する判定基準

### 2.1 主要評価指標

**canonical コホートの net-R 平均**（`stop_x1_0` 対比）

### 2.2 成功・失敗の定義

| 判定 | 条件 |
|---|---|
| **改善** | `stop_x1_5` または `stop_x2_0` の平均net-Rが `stop_x1_0` を上回り、**かつ** paired bootstrap 95%CI が0を含まない |
| **改善なし** | 平均は上回るが CI が0を含む |
| **悪化** | 平均が下回る |

**⚠️ n≈13〜18 と想定され、有意差の検出力は低い。**
「改善なし」が最も起こりやすい結果であり、それは失敗ではなく正しい測定結果である。

### 2.3 副次指標（事前登録）

- 被stop率の変化
- close_reason 分布の変化
- 1件あたり平均損失額の変化
- 対照群 `stop_x1_0` の再現性（実際の `close_reason` と一致する件数）

### 2.4 対照群による健全性チェック

**`stop_x1_0` が実際の決済結果を再現できない場合、replay 自体が信頼できない。**
一致率が低い場合は、他の方針の結果を**報告してはならない**。

理由として考えられるもの:
- tick 欠落
- available_time（PIT）境界の扱いの差
- horizon 終端の定義差

### 2.5 禁止事項

- 倍率を結果を見てから追加・変更すること
- コホートを結果を見てから絞ること（symbol別など）
- 「最良の倍率」を選んで報告すること
- この結果を根拠に本番の TP/SL 距離を変更すること
  （`FX_CODEX_TARGET_ARCHITECTURE_INTEGRATION_PLAN.md` §8 の変更禁止事項）

---

## 3. この測定が答えないこと

1. **因果関係** — 同一 tick 経路上の機械的比較であり、
   stop を変えれば市場が同じ経路を辿るという保証はない
2. **他コホートへの一般化** — `1h × short` 固有の結果である
3. **本番の妥当性** — n が小さく、昇格の根拠にはならない
4. **将来の期待値** — 過去の tick に対する事後計算である

`fx_intel/virtual_portfolio.py:2992` の `causal_claim: False` と同じ扱いとする。

---

## 4. 実行手順

1. **本文書をコミットする（replay 実行前）**
2. コホートを抽出（canonical のみ）
3. 3方針を tick 経路上で評価
4. 対照群の再現性を先に確認
5. 主要・副次指標を算出
6. 判定基準に従って結論を出す
7. **結果がどうであれ、方針を遡って変更しない**

**文書終端**
