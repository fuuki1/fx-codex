# `planned_risk_distance` 契約 監査レポート

- 監査日: 2026-08-06
- 親commit: `f499598`(PR #90)/ 親PR: #90 → #89
- 種別: **監査のみ**(実装・ガード追加は含まない)
- source data: **一切変更していない**

## 0. 監査対象

| 項目 | 値 |
|---|---|
| ファイル | `/Users/fuuki/srv/fx-codex/logs/canonical_outcomes.jsonl`(実機) |
| sha256 | `abe42dc6f6177c7fce9fb60079a6b09c0e2a2d83c3abceb4f85e592b17111a1c` |
| レコード数 | 48 |

⚠️ **n=48。以下の数値はすべてこの制約下の観測**であり、統計的有意性の主張ではない。

## 1. 契約の実体(コード追跡)

```python
# fx_intel/briefing.py:295 freeze_target_plan()
risk_distance = atr * atr_multiple
stop     = close - sign * risk_distance
target1  = close + sign * risk_distance * target1_r
target2  = close + sign * risk_distance * target2_r
```

| 項目 | 値 | 場所 |
|---|---|---|
| `DEFAULT_ATR_MULTIPLE` | **2.5**(定数) | `briefing.py:90` |
| 時間足別倍率 | **shadow専用**。本番判断に接続しない | `timeframe.py:153` `COUNTERFACTUAL_TIMEFRAME_ATR_MULTIPLE`(⚠️**未追跡の作業ツリーにのみ存在**。commit されていないため clean clone には無い) |
| 生成前提 | `atr > 0` かつ `atr_multiple > 0` かつ有限 | `briefing.py:286-293` |

→ **本番の `planned_risk_distance` は `ATR × 2.5` のみ**。倍率は通貨・時間足によらず一定。
変動の源は **ATR そのもの**である。

## 2. ⭐ 検証されていない前提:下限ガードが存在しない

コード全体を検索した結果、**stop 幅の下限を課すガードは1つも存在しない**。

| 検査 | 実装 | 場所 |
|---|---|---|
| `planned_risk > 0` | ✅ある | `evaluation_labels.py:422` / `trade_outcome.py:536` |
| `planned_risk >= 下限` | ❌**無い** | — |
| `entry_spread_r` による建玉拒否 | ❌**無い** | `entry_spread_r` は**算術一致の検算のみ**(`evaluation_labels.py:424-429`) |

`entry_spread_r`(= spread / stop幅)は計算・記録されているが、
**どこからも拒否判断に使われていない**。値が 1.0 を超えても契約は通る。

## 3. 実測:stop 幅の分布

### 3.1 生値は通貨スケールに支配される(前回の6,661倍は誤読)

| symbol | tf | n | prd_min | prd_med | prd_max | max/min |
|---|---|---|---|---|---|---|
| EURUSD | 15m | 15 | 0.000036 | 0.000303 | 0.000872 | 24x |
| EURUSD | 1h | 16 | 0.000140 | 0.000340 | 0.000700 | 5x |
| USDJPY | 15m | 10 | 0.003810 | 0.091905 | 0.164828 | 43x |
| USDJPY | 1h | 7 | 0.004909 | 0.157391 | 0.242647 | 49x |

⚠️ **前回レポートの「6,661倍レンジ」は訂正する。** あれは EURUSD(約0.0003)と
USDJPY(約0.15)を混ぜた**通貨スケール差**であり、契約の異常ではない。
**同一 symbol/timeframe 内では 5〜49倍**。

### 3.2 価格比(bps)で正規化すると比較可能になる

| symbol | tf | n | bps_min | bps_med | bps_max |
|---|---|---|---|---|---|
| EURUSD | 15m | 15 | 0.32 | 2.63 | 7.57 |
| EURUSD | 1h | 16 | 1.22 | 2.96 | 6.07 |
| USDJPY | 15m | 10 | 0.24 | 5.85 | 10.34 |
| USDJPY | 1h | 7 | 0.31 | 9.84 | 15.21 |

## 4. ⭐⭐ 中核の発見:spread が stop 幅を上回る取引が存在する

`entry_spread_r` = (entry_ask − entry_bid) / planned_risk_distance の分布:

```
min=0.005  p10=0.027  median=0.102  p90=0.815  max=1.373
```

| 条件 | 件数 | 意味 |
|---|---|---|
| `spread >= stop幅` (ratio ≥ 1.0) | **3/48 (6.2%)** | **建てた瞬間に stop を越えている** |
| `spread >= stop幅の50%` (ratio ≥ 0.5) | **6/48 (12.5%)** | 期待値が構造的に成立しない |

### sub-1bps の3件(詳細)

| symbol/tf/dir | stop幅 | spread | **spread/stop** | net_r | first_touch |
|---|---|---|---|---|---|
| EURUSD 15m short | 0.0000364 | 0.000050 | **1.37** | −1.143 | stop_loss |
| USDJPY 15m short | 0.0038104 | 0.004000 | **1.05** | −1.090 | stop_loss |
| USDJPY 1h short | 0.0049089 | 0.004000 | 0.81 | **+46.406** | target1 |

上2件は spread が stop より広く、**stop_loss で終わるのが必然**。
3件目は 46.4R の外れ値で、逆算 ATR = 0.00196(約0.2pip)という異常な低ボラ値。

## 5. ⭐ stop 幅が R の桁を支配している

| 群 | n | max\|net_r\| | mean net_r |
|---|---|---|---|
| stop < 1bps | **3** | **46.41** | +14.724 |
| stop ≥ 1bps | 45 | 2.40 | −0.321 |

**1bps に明確な断層がある。** 狭い stop は R の分母を潰し、値を1桁以上増幅する。

### `entry_spread_r` 帯別の成績(単調劣化)

| entry_spread_r | n | mean R | median R | 勝率 |
|---|---|---|---|---|
| <0.1 | 23 | −0.153 | −0.207 | **48%** |
| 0.1–0.3 | 15 | −0.337 | −1.064 | **33%** |
| 0.3–0.5 | 4 | −0.757 | −0.663 | **0%** |
| ≥0.5 | 6 | +6.887 | −1.074 | **17%** |

⚠️ `≥0.5` の平均が正なのは **46.4R の1件のみ**による。中央値は −1.074、勝率17%。
**平均で判断してはいけない典型例**。

## 6. ⭐⭐ 母集団への影響:符号が反転する

```
全48件            : mean = +0.6192  median = -0.6632
spread比<0.5 の42件: mean = -0.2762  median = -0.5762
```

**病的 stop の6件を除くと、母集団平均は +0.62R から −0.28R へ反転する。**

→ 前回の帰属監査で「mean +0.62R」と観測した正の期待値は、
**stop 幅契約の欠陥が生んだ人工物**であり、実力を表していない。

## 7. 監査の結論

### 確定した事実

1. `planned_risk_distance = ATR × 2.5` で、**倍率は本番では定数**
2. **stop 幅の下限ガードは存在しない**(`> 0` のみ)
3. `entry_spread_r` は記録されるが**拒否に使われていない**
4. 実機48件中 **3件(6.2%)で spread ≥ stop 幅**
5. stop < 1bps の3件が \|R\| を最大46倍まで増幅
6. 病的6件を除くと**母集団平均の符号が反転する**(+0.62R → −0.28R)

### 前回レポートの訂正

「`planned_risk_distance` が6,661倍レンジ」は**通貨スケールの混同**だった。
正しくは**同一 symbol/timeframe 内で 5〜49倍**であり、真の問題はレンジ幅ではなく
**spread に対して stop が狭すぎる取引が存在すること**である。

### まだ答えられないこと

| 問い | 状態 |
|---|---|
| ATR が異常低値になった原因 | canonical outcome に `atr` が**無い**。judgment 側の journal を突合しないと不明 |
| 病的 stop の発生頻度は増えているか | n=48・期間が短く**判定不能** |
| 1bps という閾値が普遍的か | **n=3 の観測にすぎない**。閾値として固定するのは早い |
| 他 symbol/timeframe でも起きるか | GBPUSD の canonical outcome が**0件**で検証不能 |

## 8. 次の最小実装案(証拠に基づく)

### 推奨: `entry_spread_r` による **記録のみの fail-closed フラグ**を先に入れる

理由:
- `entry_spread_r` は**既に計算・記録されている**。新規観測が不要
- `spread >= stop幅` は**算術的に成立しない取引**であり、閾値の恣意性が無い
  (「1bps」のような経験的閾値と違い、ratio ≥ 1.0 は定義上おかしい)
- まず**観測して頻度を測る**段階に留め、建玉拒否は頻度が分かってから判断する

### 実装しないと判断したもの

| 案 | 判断 | 理由 |
|---|---|---|
| stop 幅の絶対下限(例 1bps) | ❌ | **n=3 の観測で閾値を固定するのは早い**。通貨・時間足・ボラ環境で妥当値が変わる |
| ATR 倍率の時間足別化 | ❌ | 既に shadow 側に存在し、本番接続は EV 負で見送り済み。今回の問題とは別 |
| 病的レコードの遡及除外 | ❌ | source data を変更しない原則に反する。集計側でフィルタすべき |

### 実装より先に埋めるべき観測

1. **canonical outcome に `atr` を含める** — ATR 異常の原因究明に必須
2. **`entry_spread_r` を集計軸に追加** — 現在ダッシュボード・学習側で使われていない
3. GBPUSD の canonical outcome が 0件 — 3通貨の比較ができない

## 9. 読み取り専用性

本監査は `tools/pnl_contract_audit.py` と ssh 経由の読み取り専用クエリのみ。
source data の sha256 は監査前後で不変(`abe42dc6...`)。
