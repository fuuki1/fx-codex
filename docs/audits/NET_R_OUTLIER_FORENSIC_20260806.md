# net-R 外れ値 `sim-718c586007c93eec140f9dc6` の実体調査

**作成日:** 2026-08-06
**対象:** `[PROD]` `/Users/fuuki/srv/fx-codex/logs/fx_virtual_portfolio.sqlite3`
**コード参照:** `[DEV]` HEAD `c327552` で全行番号を再検証済み
**⚠️ 注意:** コードは [DEV]、データは [PROD]。両ツリーは `virtual_portfolio.py` が md5 不一致
（`GAP_2026-08-06.md` §0）。本文書の行番号は [DEV] の値であり、PROD の同一関数が同一である保証はない（**未確認**）。

---

## 結論

**3仮説のうち、単独の「計算バグ」でも「データ異常」でもない。**

| 仮説 | 判定 |
|---|---|
| 1. データまたは計算の異常 | **否定。** 全構成要素から独立再計算し、丸め桁まで一致（§2） |
| 2. 正当に発生した大幅利益 | **数値としては正当。** ただし「正当なテール利益」と呼ぶのは誤り（§4） |
| 3. 数値は正しいが統計・学習・安全判定を過度に支配する高影響サンプル | **該当。ただしこれは結果であって原因ではない** |

**確定した結論:**

> **サイジング設計の欠陥（`risk_distance` に下限ガードが無い）による、
> R正規化の分母崩壊。net_r の値そのものは契約通り正しく計算されている。**

これは「計算バグ」ではない（式は正しい）。「データ異常」でもない（入力は実測値）。
**契約の欠落**であり、4分類でいえば **「正当なテール利益」ではなく、設計欠陥に起因する
測定単位の崩壊**である。分類を1つ選ぶなら **計算バグでもデータ異常でもなく「設計上の分母崩壊」**、
上記4択に強制的に当てはめるなら **「データ異常」に最も近い**（入力値ではなく導出値が壊れている）。

---

## 1. 対象レコードの全項目

### 1.1 同定と一意性

```sql
SELECT (SELECT COUNT(*) FROM simulated_trade_opens  WHERE trade_id='sim-718c586007c93eec140f9dc6'),
       (SELECT COUNT(*) FROM simulated_trade_closes WHERE trade_id='sim-718c586007c93eec140f9dc6'),
       (SELECT COUNT(*) FROM simulated_trade_close_observations WHERE trade_id='sim-718c586007c93eec140f9dc6');
-- → 1|1|1
```

**重複close・再送・二重計上なし。**

| 項目 | 値 |
|---|---|
| trade_id | `sim-718c586007c93eec140f9dc6` |
| decision_id | `134d44ba278d2f648d08` |
| symbol / side / timeframe | USDJPY / **short** / **1h** |
| writer_id | `virtual-portfolio-cli` |

**4h/1d 混入なし。** timeframe は `1h` で、建玉可能な時間足（`GAP` §2.3 の観測専用対象外）。

### 1.2 PIT・時刻整合

```sql
SELECT d.decision_time_ns, d.observed_at_ns, d.pit_eligible, d.disposition
FROM decision_records d JOIN simulated_trade_opens o ON o.decision_id=d.decision_id
WHERE o.trade_id='sim-718c586007c93eec140f9dc6';
-- → 1785486683521456128|1785486690441754112|1|opened
```

| 時刻 | UTC |
|---|---|
| decision_time | 2026-07-31T08:31:23.521456Z |
| open_time | 2026-07-31T08:31:25.106000Z |
| observed_at | 2026-07-31T08:31:30.441754Z |
| close_time | 2026-07-31T08:40:07.350000Z |

- 順序 `decision ≤ open ≤ close`: **成立**
- decision → open: 1.58 秒
- **保有時間: 522.24 秒 = 8.70 分**
- `pit_eligible = 1`

**PIT違反なし。** `observed_at` が `open_time` より 5.34 秒後だが、これは観測記録時刻であり
PIT境界違反ではない（open は decision の 1.58 秒後）。

### 1.3 価格・サイズ・コスト7分解

| 項目 | 値 |
|---|---|
| entry_bid / entry_ask | 160.285 / 160.289 |
| entry_mid / entry_executable | 160.287 / **160.285**（short→bid） |
| stop_price | **160.289908884407** |
| target_price | 160.058091115593 |
| units | **964,170** |
| planned_risk_jpy | **4,733** |
| exit_bid / exit_ask | 160.055 / 160.057 |
| exit_mid / exit_executable | 160.056 / **160.057**（short決済→ask） |
| close_reason | **target1** |
| quote_to_jpy_rate / source | 1.0 / `JPY quote currency` |

**コスト7分解（DB保存値）:**

| 項目 | JPY |
|---|---|
| gross_market_pnl | 222,723 |
| executable_pnl | 219,831 |
| spread_quote_cost | 2,892 |
| slippage_cost | 95 |
| commission | 48 |
| financing | 48 |
| conversion_cost | 0 |
| **net_pnl** | **219,640** |
| realized_net_r | **46.4060849355588** |
| MFE_r / MAE_r | 46.4463982236196 / **0.814849091646411** |

---

## 2. 独立再計算

保存済み `net_r` を検算するのではなく、**構成要素から式を再現**した。

### 2.1 正式なR定義（コード上のシンボル）

`fx_intel/virtual_portfolio.py:2556-2557`（HEAD `c327552` で検証）:

```python
planned_risk = int(opened["planned_risk_jpy"])
realized_net_r = net / planned_risk
```

`net` の定義は同ファイル `:2553`:

```python
net = executable - slippage - commission - financing - conversion_cost
```

恒等式チェックが `:2554` に存在（違反時 `InvalidVirtualRecord`）:

```python
if net != (gross - spread_quote_cost - slippage - commission - financing - conversion_cost):
    raise InvalidVirtualRecord("P&L attribution identity does not reconcile")
```

`planned_risk` の生成は `:4432`:

```python
risk_distance = abs(executable - stop)                                    # :4422
units_decimal = (Decimal(maximum_risk) / (risk_distance * conversion_rate)
                 ).quantize(Decimal("1"), rounding=ROUND_FLOOR)           # :4427
planned_risk = _round_jpy(units_decimal * risk_distance * conversion_rate) # :4432
```

→ **要求された式 `recomputed_net_pnl / open時点で固定されたrisk_amount` は、
このシステムでは `net / planned_risk_jpy` として実装されている。**
`planned_risk_jpy` は open 時に確定し `simulated_trade_opens` に保存されるため、
**close時点の値との混同は構造的に起こり得ない**（close側は `opened[...]` から読む、`:2556`）。

### 2.2 再計算結果

| 項目 | 独立再計算 | DB保存値 | 差 |
|---|---|---|---|
| risk_distance | 0.004908884407 JPY | (非保存・導出) | — |
| planned_risk | 4,732.999078697190 | **4,733** | 丸めのみ |
| gross | 222,723.270 | **222,723** | 丸めのみ |
| executable | 219,830.760 | **219,831** | 丸めのみ |
| spread_quote_cost | 2,892.510 | **2,892** | 丸めのみ |
| net | 219,639.760 | **219,640** | 丸めのみ |
| **net/planned_risk** | **46.40603422776** | **46.40608493556** | 5.07e-5 |

**単位整合:** USDJPY のため `quote_to_jpy_rate = 1.0`、pip = 0.01。
価格差 → JPY → R の変換に単位不整合なし。EURUSD 側も pip=0.0001 で別途検証済み（§5）。

→ **計算は正しい。丸め（`_round_jpy`）以外の乖離は存在しない。**

---

## 3. 分母崩壊の実体

再計算で判明した中心的事実:

```
risk_distance = |160.285 - 160.289908884407| = 0.004908884407 JPY = 0.491 pips
entry spread  = |160.289 - 160.285|          = 0.004        JPY = 0.400 pips

rd / spread = 1.227     ← ストップがスプレッドの 1.23 倍しか離れていない
```

**帰結:**

| 項目 | 値 | 評価 |
|---|---|---|
| units | 964,170 | 想定リスク¥4,733 に対し |
| notional | **¥154,541,988** | notional/risk = **32,652倍** |
| target距離 / risk距離 | **RR = 46.2** | 設計RRではなく分母が潰れた結果 |
| MAE | 0.815 R = **0.400 pips** | 到達価格 160.2890 |
| stop | 160.289908884407 | **差 0.0909 pips** |

**ストップに 0.09 pips まで接近して、触れずに生還している。**
`|executable - stop|` は `abs()` で計算されるため（`:4422`）、
ストップがスプレッド内に置かれても検出されない。

**さらに:** `simulated_position_marks` の観測は **1件のみ**。
8.70分の保有に対しイントラトレードのサンプルが1点しかなく、
**MFE/MAE は1サンプルから導出されている**。ストップに 0.09 pips まで寄った経路を
1点サンプルで「触れなかった」と判定している。

→ **この取引は「+46.4R勝ち」と「−1R負け」の境界を 0.09 pips で分けており、
かつその判定根拠が1サンプルである。** 再現性のある優位性の証拠として使えない。

**未確認:** 実tickでストップが触れたか否かは、Dukascopy tick への遡及照合が必要。
本作業では未実施。

---

## 4. なぜ「正当なテール利益」ではないか

数値は正しい。8.7分で22.7pipsのUSDJPY変動は実在し得る。しかし:

1. **R の意味が壊れている。** R は「1単位のリスクを取って何単位得たか」。
   ここでは分母が spread の 1.23 倍しかなく、`R` が測っているのは
   優位性ではなく **「ストップをどれだけスプレッドに近づけたか」**。
2. **同じ設計が負け側も歪めている。** rd/spread < 2.0 の6件のうち、
   +46.4R の1件を除く5件は **全て負け**（−0.445 〜 −1.346R）。
   スプレッド近傍にストップを置くと、多くは即座に狩られ、稀に巨大Rが出る。
   これは優位性ではなく**分母のくじ引き**。
3. **再現条件が「ストップに0.09pips触れない」。** 制御不能。

→ したがって仮説2は「数値として正当」だが、**「正当なテール利益」として
学習・昇格の根拠に使ってはならない**。

---

## 5. 50件全体の統計

### 5.1 位置・散布

| 統計量 | 値 |
|---|---|
| mean | **+0.5786** |
| median | **−0.5978** |
| 10%トリム平均 (n=40) | **−0.4509** |
| winsorized @5R | **−0.2495** |
| winsorized @10R | **−0.1495** |
| stdev | 6.6728 |
| MAD | 0.5008 |
| **stdev/MAD** | **13.3**（>>1 = 極端な裾） |

| 分位点 | 値 |
|---|---|
| min | −1.3456 |
| p10 | −1.1232 |
| p25 | −1.0706 |
| p50 | −0.5978 |
| p75 | +0.2085 |
| p90 | +1.0532 |
| p95 | +1.4936 |
| **max** | **+46.4061** |

**平均だけが正で、中央値・トリム平均・winsorized平均はすべて負。**

### 5.2 leave-one-out 影響度

| 統計 | 値 |
|---|---|
| LOO平均 最小 | **−0.3566**（+46.4Rを除外時） |
| LOO平均 最大 | +0.6179 |
| LOO レンジ | 0.9745 R |

**上位5件の影響度:**

| trade_id | net_r | 除外時平均 | Δ |
|---|---:|---:|---:|
| `sim-718c586007c93eec140f9dc6` | +46.406 | **−0.3566** | **−0.9353** |
| `sim-2c0a5c81ca989fe60f9f06d2` | −1.346 | +0.6179 | +0.0393 |
| `sim-84a08aad47489ed18c1a5ed9` | +2.401 | +0.5414 | −0.0372 |
| `sim-cd3e15af1c7ac42b1f7a3ecd` | −1.168 | +0.6143 | +0.0357 |
| `sim-75cce78c0897d081774fa153` | −1.161 | +0.6141 | +0.0355 |

**1件の影響度が2位の 23.8倍。**

### 5.3 bootstrap 信頼区間（20,000 resamples, seed=42）

| 母集団 | 95% CI | 0 を含む |
|---|---|---|
| 全50件 | **[−0.5349, +2.5758]** | **含む**（符号判定不能） |
| \|R\|≤5 の49件 | **[−0.5957, −0.0960]** | **含まない（有意に負）** |

→ **外れ値を含めると符号が判定できず、除くと有意に負。**

### 5.4 層別

**symbol別:**

| | n | mean | median |
|---|---:|---:|---:|
| EURUSD | 32 | −0.3724 | −1.0526 |
| USDJPY | 18 | **+2.2693** | −0.5002 |

USDJPY の平均が正なのは +46.4R が USDJPY だから。**中央値は負。**

**side別:**

| | n | mean | median |
|---|---:|---:|---:|
| short | 28 | +1.1901 | −1.0618 |
| long | 22 | −0.1997 | −0.2209 |

**timeframe別:**

| | n | mean | median |
|---|---:|---:|---:|
| 15m | 25 | −0.1388 | −0.2286 |
| 1h | 23 | +1.4432 | −1.0659 |
| 1d | 2 | −0.3962 | −0.3962 |

**⚠️ `1d` が2件建玉されている。** `GAP` §2.3 では「4h/1dは観測専用」で4,196件棄却されているが、
2件は建玉されている。**要調査**（本作業では原因未特定=**未確認**）。

**close_reason別:**

| | n | mean | median |
|---|---:|---:|---:|
| stop_loss | 23 | −1.1013 | −1.0790 |
| target1 | 14 | **+4.0647** | +0.6367 |
| time_exit | 11 | −0.1685 | −0.2286 |
| session_end | 2 | −0.3962 | −0.3962 |

**stop_loss が 23/50 = 46%。** 中央値 −1.079R は「1R負け」として正常。

---

## 6. risk_distance の実測分布（50件全数）

これが **k値を決めるための一次データ**である。

| 統計 | rd/spread 比 |
|---|---|
| min | **0.73** |
| p10 | 1.23 |
| p25 | 4.89 |
| median | **11.03** |
| p75 | 22.29 |
| max | 1273.97 |

| 統計 | rd (pips) |
|---|---|
| min | 0.364 |
| p10 | 1.703 |
| median | 4.815 |
| p90 | 16.483 |
| max | 158.609 |

### rd/spread < 2.0 の全6件

| trade_id | sym | tf | rd(pips) | spread(pips) | 比 | net_r | units |
|---|---|---|---:|---:|---:|---:|---:|
| `sim-5ac6a341add4217390cf4667` | EURUSD | 15m | 0.364 | 0.500 | **0.73** | −1.143 | 817,659 |
| `sim-c4b2abe20569ef345606276b` | USDJPY | 15m | 0.381 | 0.400 | **0.95** | −1.090 | 1,242,134 |
| `sim-494a056d9b738b08308ae901` | EURUSD | 15m | 6.222 | 6.300 | **0.99** | −1.058 | 49,615 |
| `sim-2c0a5c81ca989fe60f9f06d2` | EURUSD | 1h | 6.999 | 6.300 | 1.11 | −1.346 | 44,107 |
| `sim-718c586007c93eec140f9dc6` | USDJPY | 1h | 0.491 | 0.400 | 1.23 | **+46.406** | 964,170 |
| `sim-071de1a9ed7ccf92334e4aac` | USDJPY | 15m | 9.394 | 6.000 | 1.57 | −0.445 | 60,485 |

**重要:** 3件は **rd < spread（比 < 1.0）**。ストップがスプレッドより内側にあり、
**約定した瞬間に理論上ストップ圏内**。うち5件は負け、1件だけが +46.4R。

**中央値が 11.03 に対し、これら6件は 0.73〜1.57。** 分布として明確に分離している。

---

## 7. P0 の再設計（A / B 分離）

**原則: raw realized net-R を改変しない。元帳には生値を残す。**

### 併存フィールド設計

| フィールド | 定義 | 用途 |
|---|---|---|
| `raw_net_r` | 現行 `realized_net_r` の生値。**不変** | 元帳・監査 |
| `validated_net_r` | Aを通過したもののみ。不通過は `NULL` | 分析の既定 |
| `model_target_net_r` | Bの変換後（winsorize等） | 学習ターゲット |
| `robust_summary_net_r` | Bの変換後 | 平均・昇格判定 |
| `exclusion_reason` | A/Bの適用理由コード | 監査 |

### A. データ完全性ガード（拒否）

計算不能・分母崩壊・単位不整合・重複・PIT違反を**建玉時に拒否**する。
50件の実測分布（§6）に基づく:

| # | ガード | 提案閾値 | 根拠 |
|---|---|---|---|
| A1 | `risk_distance ≧ k × spread` | **k = 2.0**（初期） | 実測 median 11.03、p25 4.89。k=2.0 は6件(12%)を拒否。**k=1.0 では3件しか拒否できず、+46.4R を通してしまう** |
| A2 | `risk_distance > 0` | 必須 | `:4422` に現在チェック無し |
| A3 | units の notional 上限 | 要検討 | 実測 notional/risk = 32,652倍 |
| A4 | mark観測数 ≧ N | **要検討** | 本件は1サンプル。MFE/MAE の信頼性に直結 |
| A5 | 重複close検出 | 実装済 | 本件は 1|1|1 で正常 |
| A6 | timeframe が建玉可能 | **要調査** | `1d` が2件建玉されている（§5.4） |

**k の根拠:** 本件1サンプルからではなく、50件の rd/spread 分布から決めた。
median 11.03 に対し < 2.0 の6件は明確に分離しており、その6件のうち5件が負け・1件が+46.4R。
**ただし k=2.0 は建玉を12%減らす。** 現在50件しかない母集団でさらに減ることを
許容するかは判断が必要（→ §8）。

### B. 統計的影響ガード（変換・削除しない）

| # | ガード | 提案 |
|---|---|---|
| B1 | 学習ターゲットの winsorize | ±5R（実測 p95=+1.49、5R は十分外側） |
| B2 | 要約統計に median / trimmed / winsorized を**併記必須** | 平均単独表示を禁止 |
| B3 | 昇格判定に LOO 影響度チェック | 単一サンプルの Δ が閾値超なら昇格保留 |
| B4 | bootstrap CI が 0 を跨ぐ場合は「符号判定不能」と記録 | 現状がまさにこれ |

**B は元帳を変えない。評価時変換のみ。**

---

## 8. 判断が必要な点

1. **k=2.0 は建玉を 50→44 件に減らす。** 母集団がすでに枯渇している（`GAP` §2.2）状況で
   さらに減らすことの是非。→ **推奨: A1 を「拒否」ではなく初期は「フラグのみ」で導入し、
   1週間の発生率を実測してから拒否へ昇格する。**
2. **A4（mark観測数の下限）は未設計。** 本件は1サンプルで MFE/MAE を出しており、
   ストップ到達判定の信頼性が低い。何点必要かは実測が要る。
3. **`1d` 2件の建玉経路**が未特定（§5.4）。

---

## 9. 未確認事項

1. **実tickでのストップ到達可否** — Dukascopy tick への遡及照合は未実施
2. **PROD の `virtual_portfolio.py` が [DEV] と同一か** — md5不一致を確認済み。本文書の行番号は [DEV]
3. **`1d` 2件が建玉された理由**
4. **+46.4R 以外の rd/spread<2.0 の5件が全て負けである統計的有意性** — n=5 では検定不能
5. **`simulated_position_marks` が1件しかない理由** — マーク間隔の設計を未確認

---

## 付録: 再現コマンド

```bash
# 対象レコード
ssh fuuki@trader-mini "sqlite3 -line /Users/fuuki/srv/fx-codex/logs/fx_virtual_portfolio.sqlite3 \
  \"SELECT * FROM simulated_trade_opens WHERE trade_id='sim-718c586007c93eec140f9dc6';\""

# 50件エクスポート
ssh fuuki@trader-mini "sqlite3 -csv .../fx_virtual_portfolio.sqlite3 \
  \"SELECT c.trade_id,o.symbol,o.direction,o.timeframe,c.close_reason,c.realized_net_r, \
    o.planned_risk_jpy,o.units,o.entry_bid,o.entry_ask,o.entry_executable,o.stop_price, \
    c.net_pnl_jpy,c.maximum_favorable_excursion_r,c.maximum_adverse_excursion_r \
   FROM simulated_trade_closes c JOIN simulated_trade_opens o ON o.trade_id=c.trade_id \
   ORDER BY c.realized_net_r DESC;\""

# コード参照（HEAD c327552 で検証済み）
sed -n '4422p;4427p;4432p' fx_intel/virtual_portfolio.py   # サイジング
sed -n '2553,2557p' fx_intel/virtual_portfolio.py          # net-R 定義
sed -n '409p'  fx_intel/virtual_portfolio.py               # planned_risk_jpy INTEGER
```

**文書終端**
