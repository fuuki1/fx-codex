# Canonical Price Frame — Data Truth Plane 再設計

**作成日:** 2026-08-07
**コード:** `[DEV]` HEAD `57dcd45`
**位置付け:** **設計文書。実装・実機変更は含まない。**
**前提:** `docs/audits/PRICE_SOURCE_DIVERGENCE_20260807.md`

---

## 0. 要件と、実測により判明した制約

**要件:** Dukascopy を canonical price truth とし、
decision → stop/target → scoring → replay → learning を同一価格座標系へ統一する。

**⚠️ 実測により、この要件は文字通りには実現できないことが判明した。**

```
Dukascopy の取込ラグ（2026-08-07 06:12Z 実測）
  最新 tick: 2026-08-07T04:59:58Z
  現在時刻 : 2026-08-07T06:12:52Z
  ラグ     : 約 73 分
```

これは障害ではなく**設計**である。Dukascopy は1時間単位のアーカイブを公開し、
`scripts/datafeed_collect_once.sh:32` は `LAGS=(4 3 2 1)` で
1〜4時間前を意図的に収集している。

**判断の生成間隔は5分**である。したがって:

> **Dukascopy を「判断時点の価格ソース」にすることはできない。**
> 10:00 の判断は自身の tick を約 11:02 まで待つことになる。

よって「同一座標系へ統一」は、**「全段で Dukascopy を引く」ことではあり得ない。**
以下、実現可能な3案を提示し、推奨を示す。

---

## 1. 現状の実測（何が既にあり、何が無いか）

### 1.1 価格ソースの現況

| 段階 | 現在のソース | 実測根拠 |
|---|---|---|
| **decision の close/stop/target** | **TradingView scanner (OANDA)** | `fx_intel/technicals.py:1`、`briefing.py:724` `close = tech.close()`、`briefing.py:731` `quote_source = "tradingview_oanda_scanner"` |
| **decision の entry_bid/ask** | **全 5,330 件 null** | 実測（`PRICE_SOURCE_DIVERGENCE` §1） |
| **建玉 quote** | **Dukascopy** | 実機 `simulated_trade_opens.quote_conversion_source` が `dukascopy:USDJPY:...` |
| **scoring / replay** | **Dukascopy** | `fx_intel/price_path_adapter.py:59` `ADAPTER_SOURCE = "dukascopy_datafeed"` |
| **learning** | 上記 scoring の出力 | — |

→ **座標系の分裂は「decision だけが TradingView、それ以外は Dukascopy」という形で既に存在する。**
統一すべき境界は1箇所に特定できる。

### 1.2 既に存在するが未配線の資産

| 資産 | 状態 |
|---|---|
| `data_platform/collect/divergence.py` `compare_sources()` | **実装済み・呼び出し元0件**。二重ソースの乖離を測り `degraded`/`quarantined` へ落とす。**合成consensus価格を作らない**と明記 |
| `fx_intel/dukascopy_quotes.py` | **実装済み・未追跡・未配線**。`tradeable=False` を明示し、executable cost model から解決されないよう設計済み |
| `fx_briefing.py:295` `FX_DECISION_QUOTE_PROVIDER` | **切替スイッチが既に存在**（`scanner` / `oanda` / `ibkr`） |
| `fx_intel/oanda_prices.py` `fetch_decision_quotes()` | 実装済み・`briefing.py:298` から呼ばれる経路あり |

### 1.3 実機の設定

| 項目 | 実測 |
|---|---|
| `FX_DECISION_QUOTE_PROVIDER` | **未設定 → 既定 `scanner`** |
| OANDA 認証情報 | **`.env` に0件** |

→ **OANDA リアルタイム経路は「コードはあるが認証が無く動かせない」状態。**

---

## 2. 実現可能な3案

### 案A: Dukascopy を「ラベル座標系」の canonical とし、decision は再アンカーする

decision は従来どおり scanner の参照価格で**方向と ATR 倍率**だけを決め、
**絶対価格の stop/target を確定させない**。
scoring 時に canonical (Dukascopy) 上で再アンカーしてから初めて価格が定まる。

```
decision  : direction + atr_multiple + reference_price(source-tagged)
   ↓ (canonical tick が到着してから)
anchoring : entry = canonical tick executable
            stop  = entry ∓ atr * multiple
            target= entry ± atr * multiple * rr
   ↓
scoring / replay / learning : すべて canonical 座標
```

| 長所 | 短所 |
|---|---|
| **判断と検証が同一座標になる**（乖離が構造的に消える） | 判断時に stop/target を提示できない（Discord 通知の内容が変わる） |
| 追加の外部依存が無い | ラグ73分の間、判断は「未確定」状態になる |
| 5.7%の「乖離が stop を超える」が**発生源で消える** | 既存の decision 契約の変更が大きい |

### 案B: OANDA リアルタイムを canonical にし、Dukascopy を独立監査に回す

`FX_DECISION_QUOTE_PROVIDER=oanda` を有効化し、判断も scoring も OANDA で行う。
Dukascopy は `compare_sources()` による**独立した第2ソース**として乖離監視に使う。

| 長所 | 短所 |
|---|---|
| ラグが無く、判断時に executable bid/ask が得られる | **OANDA 認証情報が実機に無い**（実測0件） |
| `divergence.py` の設計意図（独立2ソース）に合致 | scoring 側を Dukascopy から OANDA へ移す作業が必要 |
| スイッチが既に実装済み | 外部API依存とレート制限が増える |

**⚠️ 認証情報の取得は人間の作業であり、本設計では前提にできない。**

### 案C: decision が相対オフセットのみを出す

decision は `stop_atr_multiple` / `target_rr` を出し、**絶対価格を一切持たない**。
価格解決は canonical 上で一度だけ行う。

案A の一般化であり、案A が「参照価格を記録するが拘束力を持たせない」のに対し、
案C は「参照価格を記録しない」。

| 長所 | 短所 |
|---|---|
| 座標系の分裂が定義上あり得なくなる | 判断ログから当時の価格水準が失われ、事後検証が弱くなる |
| 契約が最も単純 | ATR も scanner 由来なので、ATR の座標系問題が残る |

---

## 3. 推奨: 案A（段階導入）

### 理由

1. **案B は実機に認証が無く、着手できない**（実測）
2. **案C は来歴を捨てる。** decision 時点の観測価格は監査資産であり、
   `CLAUDE.md` の「監査可能性」に反する
3. **案A は既存資産で組める。** `divergence.py` と `dukascopy_quotes.py` は
   まさにこの用途で書かれており、配線されていないだけである

### ⚠️ 案A が解決しないこと

- **方向判断の情報量は改善しない。** `PATH_DEPENDENT_SIGNAL_AUDIT_20260807.md` は
  eff n=549 で方向に情報が無いことを示した。座標系を直しても方向は良くならない
- **ATR は依然として scanner 由来。** stop 幅の基準そのものは
  TradingView の ATR に依存し続ける（§5.3 の未解決事項）
- **ラグ73分は消えない。** 判断の即時性と canonical 性はトレードオフのまま

---

## 4. 案A の設計

### 4.1 契約の変更

decision に**座標系タグ**を必須化する。

```python
# 追加フィールド（decision_json）
"price_frame": {
    "reference_source": "tradingview_oanda_scanner",
    "reference_close": 1.15382,
    "reference_observed_at": "2026-08-03T01:18:12Z",
    "canonical_source": "dukascopy",
    "anchor_status": "pending",        # pending / anchored / unavailable
    "stop_atr_multiple": 0.25,
    "target_rr": 1.0,
}
```

- `stop` / `target1` は **`anchor_status == "anchored"` になるまで null**
- 既存の `stop`/`target1` を残す場合は `reference_stop`/`reference_target` へ改名し、
  **拘束力を持たないことを名前で示す**

### 4.2 アンカリング

canonical tick が到着した後に一度だけ実行し、**以後不変**とする。

```
入力: decision(direction, atr, multiple, rr, decision_time)
      canonical tick (decision_time 以降で最初の tick)
出力: entry / stop / target / anchor_provenance
```

- **PIT 保証:** アンカーに使う tick は `event_time >= decision_time` の**最初の1本**。
  未来の tick を選ばない
- **既存の追記専用契約に従う。** decision 行は書き換えず、
  `decision_price_anchors` サイドカー表へ追記する
  （`simulated_trade_sizing_assessments` と同じパターン）

### 4.3 分母ガード（decision 段階）

P0-C は**建玉時**にしか効いていない。アンカリング時に同じ floor を課す。

```
risk_distance = |entry - stop|
reject if risk_distance < 2 * spread_at_anchor
```

実測では 15m/1h の 10.9% / 10.6% がここで落ちる見込みである
（`PRICE_SOURCE_DIVERGENCE` §4）。

### 4.4 乖離の監視（`divergence.py` の配線）

`compare_sources()` を scanner と Dukascopy に対して定期実行し、
`degraded` / `quarantined` を記録する。

**⚠️ 同モジュールの設計方針どおり、2ソースを平均した合成価格は作らない。**

### 4.5 段階

| 段階 | 内容 | 完了条件 |
|---|---|---|
| **S0** | `price_frame` を **記録のみ** で追加（既存挙動を変えない） | 既存出力が不変。flag 既定 off |
| **S1** | `decision_price_anchors` サイドカーへアンカーを追記（shadow） | アンカー率と reject 率を実測 |
| **S2** | `divergence.py` を配線し乖離を継続記録 | degraded/quarantined の発生率を実測 |
| **S3** | scoring/learning がアンカー済み価格のみを読む | 旧経路との差分を説明可能 |
| **S4** | decision の `stop`/`target1` を `reference_*` へ改名 | 通知文面の変更を人間が承認 |

**S0〜S2 は既存挙動を変えない。S3 以降は独立した設計承認が必要。**

---

## 5. 未解決事項（設計前に測るべきこと）

### 5.1 ラグ73分下でのラベル鮮度

アンカリングは canonical tick を待つため、
**ラベル確定が最大73分（＋horizon）遅れる。**
15m ホライズンの判断は、ラベルが出る頃には4〜5世代先の判断が走っている。

→ 学習ループの実効サイクルタイムへの影響は**未測定**。

### 5.2 ラベル生成率とのトレードオフ

§4.3 の分母ガードは 15m/1h の約10%を落とす。
一方で 0.25 ATR 化は**ラベル生成率 0%→66.9% を目的とした変更**だった
（memory `atr_multiple_per_timeframe_implemented_20260730`）。

→ 「R が意味を持つこと」と「ラベルが出ること」のトレードオフは**未測定**。

### 5.3 ATR の座標系

stop 幅は `atr * multiple` で決まるが、**ATR は scanner 由来**である。
canonical 上で ATR を再計算すべきかは未検討。

### 5.4 GBPUSD

`scripts/fx_briefing_once.sh:25` は判断経路で GBPUSD を出さない
（`GAP` §2.3）。canonical 化とは独立の問題だが、
座標系を直しても GBPUSD の母集団は増えない。

---

## 6. 本設計が変更しないもの

`FX_CODEX_TARGET_ARCHITECTURE_INTEGRATION_PLAN.md` §8 に従い、
以下は**独立した設計承認なしに変更しない**:

- TP / SL 距離、ATR 倍率
- holding horizon
- risk per trade、leverage 上限
- promotion threshold
- label 定義、net-R 定義

本文書は**座標系の統一**を扱い、**パラメータの最適化は扱わない。**

---

## 7. 次の作業（推奨順）

1. **§5.1 と §5.2 の測定** — 設計の前提が成立するかを先に確かめる
2. **S0 の実装**（`price_frame` の記録のみ、既定 off）
3. **S1 の shadow 実行**でアンカー率・reject 率を実測

**⚠️ S3 以降（scoring の切替）は、1 と 2 の結果を見てから判断する。**

**文書終端**
