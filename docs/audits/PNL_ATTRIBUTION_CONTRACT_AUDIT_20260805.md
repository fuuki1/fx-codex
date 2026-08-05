# 損益帰属分解のための正準損益契約 監査レポート

- 監査日: 2026-08-05
- 親commit: `949eb096c4db6f6b70c34cd3f3feeb8673bf983d`
- 親PR: #89
- 種別: **監査のみ**(帰属ロジックの実装は含まない)
- source data: **一切変更していない**(読み取り専用ツールのみ使用)

## 0. 監査に使った実データ

| 項目 | 値 |
|---|---|
| ファイル | `/Users/fuuki/srv/fx-codex/logs/canonical_outcomes.jsonl`(実機) |
| sha256 | `abe42dc6f6177c7fce9fb60079a6b09c0e2a2d83c3abceb4f85e592b17111a1c` |
| サイズ | 197,914 bytes(2026-08-04 08:06 時点) |
| 取得時刻(UTC) | 2026-08-05T14:28:30Z |
| レコード数 | 48(`outcome` 配下に入れ子) |
| 読み取り専用性 | `tools/pnl_contract_audit.py` は書き込み経路を持たない。`--output` は既存ファイルがあれば拒否する |

⚠️ **母集団が48件しかない。** 本レポートの数値はすべてこの制約下の観測であり、
統計的に有意な原因断定には使えない。以下、件数を必ず併記する。

## 1. 恒等式の実測検証(Phase 1)

### 1.1 コード上の正

数式の唯一の正は `fx_intel/evaluation_labels.py:188 canonical_net_label_contract_flags()`。
監査ツールは数式を再実装せず、この契約関数が何をflagしたかを数えるだけにした
(再実装すると契約が二重定義になるため)。

指示書の想定式は**すべてコードと一致**した。

| 恒等式 | コード上の位置 | 一致 |
|---|---|---|
| `realized_net_r = quote_realized_r - additional_cost_r` | L443-447 | ✅ |
| `execution_cost_r = gross_realized_r - realized_net_r` | L448-452 | ✅ |
| `additional_cost_r = slippage_r + commission_r + financing_r + conversion_r` | L430-441 | ✅ |
| `quote_realized_r = gross_realized_r - realized_spread_cost_r` (full-cost) | L566-571 | ✅ |
| `execution_cost_r = realized_spread_cost_r + additional_cost_r` (full-cost) | L572-577 | ✅ |
| `executable_pnl_jpy = gross_market_pnl_jpy - spread_quote_cost_jpy` | L484-485 | ✅ |
| `net_pnl_jpy = executable_pnl_jpy - (slippage+commission+financing+conversion)` | L486-487 | ✅ |
| R↔JPY: 各R = 対応JPY / `planned_risk_jpy` (abs_tol=1e-12) | L488-501 | ✅ |

`entry_spread_r` は `(entry_ask - entry_bid) / planned_risk_distance` として
**独立に検算されるだけ**で、損益からの控除には使われない(L424-429)。
→ **`entry_spread_r` は診断値であり、再控除対象ではない**ことをコードで確認。

### 1.2 実データでの結果

```
records_with_label_version : 48
contract_clean             : 48
contract_flagged           :  0
flag_counts                : {}
```

**48件すべてが契約に適合し、恒等式違反は0件。**
R空間・JPY空間・R↔JPY対応のいずれも不一致なし。

## 2. フィールド監査表(Phase 2)

`observation_type` は監査ツールが以下の規則で分離する(ゼロ埋めしない)。

| observation_type | 条件 | 意味 |
|---|---|---|
| `unavailable` | `cost_model_id` が空 or `"missing"` | 費用不明。**ゼロで埋めてはいけない** |
| `measured_not_executable` | `dukascopy-historical-measured-spread-v1` | 実測spreadだが**約定可能性は主張しない** |
| `simulated` | `label_provenance == virtual_portfolio_simulated_fill` | オフラインシミュレーション |
| `modelled_executable_quote` | `KNOWN_EXECUTABLE_COST_MODEL_IDS` | 執行可能気配モデル |

### 主要フィールド(実測48件)

| field | unit | obs_type | non_null | zero | mean | median | 備考 |
|---|---|---|---|---|---|---|---|
| `gross_realized_r` | R | simulated | 48 | 0 | +0.877 | **−0.137** | ⚠️alphaと呼んではいけない |
| `realized_spread_cost_r` | R | simulated | 48 | 0 | 0.214 | 0.106 | bid/ask由来。**既にquote_realized_rで控除済** |
| `quote_realized_r` | R | simulated | 48 | 0 | +0.663 | −0.623 | = gross − spread |
| `slippage_r` | R | **modelled** | 48 | 0 | 0.0201 | 0.0201 | ⚠️実測ではない |
| `commission_r` | R | **modelled** | 48 | 0 | 0.0101 | 0.0101 | ⚠️実測ではない |
| `financing_r` | R | **modelled** | 48 | 0 | 0.0101 | 0.0101 | ⚠️実測ではない |
| `conversion_r` | R | modelled | 48 | **17** | 0.0033 | 0.0050 | JPY建てはゼロ |
| `additional_cost_r` | R | modelled | 48 | 0 | 0.0436 | 0.0452 | 上4つの合計 |
| `execution_cost_r` | R | mixed | 48 | 0 | 0.258 | 0.152 | ⚠️**spread+追加費用を既に合算済** |
| `realized_net_r` | R | simulated | 48 | 0 | +0.619 | **−0.663** | 正準純損益 |
| `entry_spread_r` | R | diagnostic | 48 | 0 | 0.226 | 0.102 | ⚠️**再控除禁止** |
| `planned_payoff_r` | R | planned | 48 | 0 | 4.227 | 0.934 | 計画値 |

### 二重控除リスク(明示)

1. **`execution_cost_r` から spread を再控除してはいけない** — full-cost では
   `execution_cost_r = realized_spread_cost_r + additional_cost_r` で既に合算済み。
2. **`entry_spread_r` を損益から控除してはいけない** — 診断値。実際の spread 控除は
   `realized_spread_cost_r` 側で `quote_realized_r` に反映済み。
3. **spread は bid/ask 価格に埋め込み済み** — `entry_executable` / `exit_executable` が
   long なら ask/bid、short なら bid/ask で正準化されている。mid からの再控除は二重。

### label_version の扱い

```
PROMOTION_NET_LABEL_VERSIONS = {"net-r-v2"}   # net-r-v3 は含まれない
```

- `net-r-v2` (`NET_LABEL_VERSION`): promotion 対象
- `net-r-v3` (`FULL_COST_NET_LABEL_VERSION`): **research-only 強制**。
  契約関数が `research_only is not True` / `promotion_eligible is not False` を
  flag する(L380-382)。

→ **`net-r-v2` と `net-r-v3` は無条件に混ぜてはいけない。** 母集団を分けること。

## 3. coverage 監査(Phase 3)

```
label_version    : {"net-r-v3": 48}      # 全件が full-cost = research only
observation_type : {"simulated": 48}     # 全件がシミュレーション
```

⚠️ **promotion 対象の `net-r-v2` レコードは canonical store に 0 件。**
現時点で昇格判断に使える正準 net-R 実績は存在しない。

## 4. EURUSD 負け越しの診断(Phase 5)

### 4.1 段階別分解(EURUSD n=31)

```
gross_realized_r          -0.1321   <- 費用control前から既に負
- realized_spread_cost_r   0.1876
= quote_realized_r        -0.3197
- additional_cost_r        0.0454
= realized_net_r          -0.3651
```

| 問い | 答え | 根拠 |
|---|---|---|
| 1. `gross_realized_r` 自体が負か | **はい** | mean −0.132 / median −0.149 |
| 2. spread控除で負へ転落したか | **いいえ(主因ではない)** | gross<0 が19/31、net<0 が20/31。**費用起因の転落は1件のみ** |
| 3. slippage等が原因か | **いいえ** | additional_cost は 0.045R で、負け幅 0.365R の12% |
| 4. 1h short と 1h long のどちらが主因か | **1h long が最悪** | 1h long n=4 net −1.138 勝率**0%** / 1h short n=12 net −0.326 勝率41.7% |

**結論: EURUSD の負けは費用ではなく、方向判断そのもの(gross段階で既に負)。**

### 4.2 セル別(n併記・すべて小標本)

| cell | n | gross | quote | net | 勝率 |
|---|---|---|---|---|---|
| EURUSD/15m/long | 10 | +0.033 | −0.137 | −0.182 | 40.0% |
| EURUSD/15m/short | 5 | +0.173 | −0.161 | −0.206 | 40.0% |
| EURUSD/1h/long | **4** | −0.756 | −1.092 | **−1.138** | **0.0%** |
| EURUSD/1h/short | 12 | −0.189 | −0.281 | −0.326 | 41.7% |
| USDJPY/15m/long | 7 | +0.539 | +0.351 | +0.311 | 57.1% |
| USDJPY/15m/short | 3 | −0.226 | −0.891 | −0.931 | 0.0% |
| USDJPY/1h/short | 7 | +6.157 | +5.992 | +5.951 | 28.6% |

⚠️ **15m は gross が正なのに net が負**(long +0.033→−0.182、short +0.173→−0.206)。
ここだけは費用が符号を反転させている。ただし n=10 / n=5 で**断定不能**。

### 4.3 出口別(EURUSD)

| first_touch | n | mean net | median net |
|---|---|---|---|
| stop_loss | 17 | −1.102 | −1.071 |
| target1 | 8 | +0.835 | +0.764 |
| time_exit | 6 | +0.122 | −0.076 |

stop_loss が **31件中17件(55%)** を占め、平均 −1.10R。

### 4.4 ⚠️ 外れ値1件が全体像を歪めている

USDJPY 1h short の net_r 分布:
```
[+46.406, +0.212, −0.596, −1.053, −1.057, −1.085, −1.168]
```

**+46.4R の1件を除くと残り6件はすべて負。** この1件が全体平均を +0.62R
(中央値は −0.66R)に押し上げている。

外れ値の内訳:
```
planned_risk_jpy      4,733   (中央値5,700と同水準 = 分母は正常)
planned_risk_distance 0.00491 (約0.5pip = 極端に近いstop)
価格移動              0.22800 (22.8pip = stop距離の46.4倍)
first_touch           target1
```

⚠️ 当初「分母が小さいから」と考えたが**誤り**。`planned_risk_jpy` は
4,707〜5,847 の範囲で安定している。真因は **`planned_risk_distance` が
0.000036〜0.242647 と 6,661倍のレンジで散らばっている**こと。
R=46 は計算上正しいが、0.5pip の stop は現実の約定を表さない疑いが強い
(既知の「SL短縮の副作用:判断closeと実tickが10-14pips乖離」と整合)。

### 4.5 まだ答えられない問い

母集団48件では以下が**識別不能**。データが足りない。

| 問い | 状態 |
|---|---|
| 5. TP/SLでMFEを取り逃しているか | `mfe_r`/`mae_r` が canonical store に無く**判定不能** |
| 6. MAEが大きく入口方向が誤りか | 同上 |
| 7. session別 | `session` フィールド **absent(48/48)** |
| 8. volatility bucket別 | フィールド**存在しない** |
| 9. ニュース・イベント窓別 | canonical store に**無い** |
| 10. expectancy guard の効果 | 反実仮想レコードが canonical store に**無い** |
| 11. 損失集中率 | ✅測定済(上記4.4) |
| 12. 従属サンプルを独立扱いしていないか | ⚠️**要注意**。同一 symbol/tf/direction が5分間隔で並ぶ構造。`thin_calls` 相当の間引きが canonical store 側に**無い** |

## 5. 帰属設計案(Phase 4)

### A. 会計的帰属(加法的・実装可能)

現行フィールドで **そのまま成立する**。追加観測は不要。

```
gross_realized_r
− realized_spread_cost_r      → quote_spread_effect
− slippage_r                  → modelled_slippage_effect
− commission_r                → modelled_commission_effect
− financing_r                 → financing_effect
− conversion_r                → conversion_effect
= realized_net_r
```

受け入れ条件 `abs(attributed_total_r − realized_net_r) < 1e-8` は、
実測48件で恒等式違反0件のため**達成可能**。

⚠️ 命名は指示どおり broker fill を主張しない語にする。
`execution_alpha` / `execution_quality` は使わない。

不一致は押し込まず `unexplained_r` / `unexplained_jpy` /
`attribution_status` / `attribution_reason_codes` を必ず持たせる。

### B. 意思決定帰属(反実仮想)

**現時点では実装しない。** 理由を軸ごとに明示する。

| 軸 | 実装可否 | 理由 |
|---|---|---|
| `exit_policy_effect` | ⚠️条件付き | `first_touch` はあるが `mfe_r`/`mae_r` が canonical store に無く、「取り逃し」を測れない |
| `direction_selection_effect` | ❌ | 同一時刻の反対方向の反実仮想が無い |
| `entry_timing_effect` | ❌ | 同一判断の別時刻エントリーが無い |
| `horizon_selection_effect` | ❌ | 同一判断を別ホライズンで採点した対がcanonical storeに無い |
| `risk_gate_effect` | ❌ | guard反実仮想が canonical store に無い(journal側にはある) |
| `size_effect_jpy` | ⚠️保留 | `planned_risk_jpy` が4,707〜5,847で**ほぼ一定**。現データではサイズは変動していないので効果を識別できない |
| `regime_selection_effect` | ❌ | session/volatility bucket が存在しない |
| `data_quality_intervention_effect` | ❌ | 介入前後の対が無い |

**`gross_realized_r` を alpha と呼ばない。** これは方向・エントリー時刻・保有時間・
TP/SL・timeout・exit価格が混在した値であり、いずれも固定していない。

## 6. 次の最小実装PR案(証拠に基づく1軸)

### 選定: **会計的帰属のみ(A層)を1軸だけ実装する**

根拠:
- 実測48件で恒等式違反0件 → 加法分解が**確実に閉じる**
- 追加の観測・データ収集が**不要**
- B層はすべて必要データが欠けており、今実装しても識別不能

### 実装しないと判断した軸と理由
上表Bの通り。特に `size_effect_jpy` は、**サイズがほぼ一定の現データでは
効果がゼロと区別できない**ため、サイズが実際に変動するまで保留する。

### 先に埋めるべき欠落(実装より優先)

1. ⭐ **`planned_risk_distance` の 6,661倍レンジ** — 0.5pip stop が R を46倍に
   増幅する。帰属分解より先に、この stop 距離契約の妥当性を検証すべき。
2. **`mfe_r` / `mae_r` を canonical store へ** — 出口効果の測定に必須。
3. **`session` / volatility bucket** — レジーム別診断に必須。現在 absent。
4. **従属サンプルの間引き契約** — canonical store 側に `thin_calls` 相当が無い。
5. **`net-r-v2` レコードが0件** — 昇格判断の根拠が現状存在しない。

## 7. 監査ツールの読み取り専用性

`tools/pnl_contract_audit.py`:
- 書き込みは `--output` で明示された**新規ファイルのみ**。既存があれば拒否して終了
- source data は `open("rb")` の読み取りのみ
- cursor・キャッシュ・append-only evidence を**一切触らない**
- 入力ファイルの sha256 とバイト数を記録
- 実行コードの `code_hash` / `config_hash` を記録
- corrupt行・非有限値・欠損経路は**ゼロ埋めせず件数として残す**
- 同じ入力から同じ結果(決定的)
