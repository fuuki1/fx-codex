# 判断セマンティクス V3 設計（7機能ギャップ）

**ステータス:** 設計のみ。**7機能すべて未実装**（コード0行）。本書は契約・スキーマ・
PIT境界・受け入れ基準・実装順を固定するためのものであり、実装済みを主張しない。
**一次情報はコードであり、本書とコードが食い違う場合はコードが正。**

**対象:** 外部ギャップ分析が指摘した7機能。本書執筆時点で、7つの行ラベル
（仮説オブジェクト／反応無効化／レジームの正式な状態モデル／因子別ポートフォリオ／
判断品質ラベル／プロセスマイニング／適応的な研究配分）は `docs/`・`README.md`・
`SYSTEM_OVERVIEW.md` のいずれにも存在しない。既存設計文書群
（[AI_LEARNING_V2_DESIGN](AI_LEARNING_V2_DESIGN.md)、
[B_LEARNING_AXES_AND_FEEDBACK_DESIGN](B_LEARNING_AXES_AND_FEEDBACK_DESIGN.md)、
[D_EVALUATION_LABEL_AND_TUNING_DESIGN](D_EVALUATION_LABEL_AND_TUNING_DESIGN.md)）
の下位に位置づける。

**安全境界:** 7機能はすべて研究・意思決定支援の範囲内である。いずれも broker 発注、
注文変更、ポジション操作を導入しない。本書に自動売買・live移行の段階は存在しない。
最終段階は shadow とオフラインシミュレーションである（[CLAUDE.md](../CLAUDE.md)）。

---

## 0. 現状の実測（設計の前提）

各行の「現状」は推測ではなく、本ブランチ `codex/timeframe-counterfactual-contract`
（HEAD `4a59ba5`）のコード実測に基づく。

| # | 機能 | 既存の土台（実測） | 欠けているもの |
|---|---|---|---|
| 1 | 仮説オブジェクト | `experiment_manifest.py:286` に `economic_hypothesis: str`（**自由文字列1本**）。`shadow_learning.py:463` の `shadow_hypothesis` は予測種別のラベルであり仮説ではない | 反証条件・期限・無効化トリガを持つ第一級オブジェクトと、その採点 |
| 2 | 反応無効化 | 材料側と価格側が**別経路**。`calendar.py:51` の `forecast`/`previous` は**str型**で `actual` フィールドが存在しない。`macro.py` に surprise 概念なし | 材料方向と価格反応の不一致を測る独立指標。**前提として actual/surprise の数値化が先** |
| 3 | レジーム状態モデル | `multi_axis_learning.py:288` `_market_regime_features` が regime を**one-hot特徴量**として消費するのみ。`fx_backtester/regime_mixture.py` はボラ分位でGBDT expertを分ける（状態遷移ではない） | 状態遷移確率と信頼度を独立に出力するモデル |
| 4 | 因子別ポートフォリオ | `virtual_portfolio.py` は建玉単位。`currency` 列は会計通貨 `JPY` 固定（`:293` の CHECK 制約）でエクスポージャー因子ではない | USD・金利・リスクオン・キャリーへの因子分解と重複集約 |
| 5 | 判断品質ラベル | `evaluation_labels.py` は損益恒等式中心（`realized_net_r`、`gross_realized_r`、`net_r_identity_mismatch`）。**プロセス品質の軸が無い** | 損益と分離した判断品質の評価軸（良い損失／悪い利益） |
| 6 | プロセスマイニング | `decision_log.py:1275,1369` が `gate_trace` を保存済み（**データ源は既にある**）。`policy_vetoed_by` も保存（`:798,993`） | `gate_trace` をイベントログとして再構成し、ボトルネックを自動抽出する層 |
| 7 | 適応的な研究配分 | `trial_ledger.py:108` `TrialLedger`（ハッシュ連鎖付き試行台帳）、`promotion.py:405` `update_stages` | 弱い戦略×レジーム×方向へ検証資源を配分する仕組み |

**重要:** #6 は既存 `gate_trace` の上に載るため最も安価。#2 は上流のデータ契約
（`actual` の数値化）が欠けているため最も高価であり、単独では着手できない。

---

## 1. 全機能に共通する非交渉制約

既存の非交渉ルール（[CLAUDE.md](../CLAUDE.md)、[AGENTS.md](../AGENTS.md)）を、
本設計の7機能へ具体化する。逸脱は実装不採用の理由になる。

### 1.1 PIT（point-in-time）契約

- すべての新オブジェクトは aware UTC を持ち、`availability` / `ingestion` /
  `revision` を分離する。判断時点で入手不能な情報を特徴量へ入れない。
- `multi_axis_learning.py:273` の `_reject_post_outcome_features` と同じ思想で、
  各新モジュールは**結果後情報の混入を能動的に拒否**する関数を持つ。
  「入れない」という規約ではなく、入ったら落ちるコードにする。
- 仮説（#1）の反証条件は**仮説作成時点で凍結**する。結果を見てから条件を書き換える
  経路を作らない。凍結は canonical JSON の SHA-256 で担保する
  （`experiment_manifest.py` と `virtual_portfolio` の policy_sha256 に既存の前例あり）。

### 1.2 veto を上書きしない

data-quality / risk veto を、品質ラベル（#5）や仮説の確信度（#1）や
レジーム確率（#3）で上書きしない。#5 が「良い損失」と評価しても、それは
veto を緩める根拠にならない。品質ラベルは**事後の学習信号**であり、
事前のゲートではない。

### 1.3 証拠水準の明示

7機能はいずれも「導入すれば改善する」ことを前提にしない。各機能は
`synthetic` / `research` / `shadow` のどの水準で検証されたかを記録し、
性能改善が観測されなければ「**改善なし**」、証拠不足なら
「**評価不能・昇格不能**」と記録する。`examples/sample_prices.csv` は合成データであり、
7機能いずれの有効性根拠にもできない。

### 1.4 shadow 固定

7機能はいずれも本番の方向・確信度・SL/TP を**変更しない**状態で導入する。
`AI_LEARNING_V2_DESIGN.md` §8 の中強気 shadow と同じく `activation mode:
counterfactual_only` から始める。本番判断への接続は、機能ごとに独立した
昇格判定を経た後にのみ検討する。

---

## 2. 機能別設計

### 2.1 仮説オブジェクト（#1）

**現状の問題:** スコアや方向は残るが、因果仮説と期限が弱い。
`economic_hypothesis` は自由文字列1本で、反証不能・採点不能である。

**導入後:** 「何が・なぜ・いつまでに・何で無効か」を固定する。

#### スキーマ（`fx_intel/hypothesis.py` 新規）

```
Hypothesis:
  hypothesis_id        : str            # canonical JSON の SHA-256
  created_at           : datetime       # aware UTC、凍結時刻
  symbol               : str
  timeframe            : str
  direction            : "long"|"short"|"neutral"
  claim_ja             : str            # 何が（人間可読）
  mechanism_ja         : str            # なぜ（因果経路）
  evidence_refs        : tuple[str,...] # 根拠の来歴（決定ID・系列ID）
  horizon_end          : datetime       # いつまでに（期限、必須）
  falsifiers           : tuple[Falsifier,...]  # 何で無効か（1件以上必須）
  frozen_sha256        : str
```

```
Falsifier:
  kind      : "price"|"macro"|"regime"|"time"
  field     : str                       # 監視対象
  operator  : "lt"|"lte"|"gt"|"gte"|"crosses"
  threshold : float
  rationale_ja : str
```

#### 不変条件（テストで担保する）

1. `falsifiers` が空の仮説は**構築時に拒否**する。反証不能な主張を保存しない。
2. `horizon_end > created_at` を強制する。期限なしを許さない。
3. 生成後の `claim_ja` / `mechanism_ja` / `falsifiers` / `horizon_end` の変更は
   `frozen_sha256` を壊す。改変された仮説は採点対象から除外する。
4. `falsifiers` の評価は `horizon_end` までの PIT 経路のみを見る。forming bar を含めない。

#### 採点（3値）

| 結果 | 条件 |
|---|---|
| `invalidated` | 期限内に falsifier のいずれかが発火 |
| `confirmed` | 期限到達時に falsifier 未発火かつ claim の方向が実現 |
| `expired_unresolved` | 期限到達時に falsifier 未発火だが方向も未実現 |

`expired_unresolved` を `confirmed` に丸めない。これを丸めると仮説の的中率が
構造的に水増しされる。

#### 受け入れ基準

- 仮説の採点は損益と独立に計算できる（#5 との分離を保つ）。
- 実データで `invalidated` / `confirmed` / `expired_unresolved` の3値すべてが
  観測されること。1値しか出ないなら falsifier 設計が壊れている。

---

### 2.2 反応無効化（#2）

**現状の問題:** 材料方向と価格反応の不一致を独立評価しにくい。

**導入後:** 良い材料で上がらない等を定量検出する。

#### ⚠ 前提となるブロッカー（最重要）

**この機能は単独では着手できない。** `calendar.py:51` の `EconomicEvent` は
`forecast: str = ""` / `previous: str = ""` を**文字列**で保持し、`actual`
フィールドを持たない。surprise（= actual − consensus）を数値で計算する経路が
存在しないため、「良い材料」を機械判定できない。

したがって #2 は2段階になる:

- **前提 P0:** `EconomicEvent` に `actual` を追加し、`forecast`/`previous`/`actual`
  を単位付き数値へ正規化する。改定（revision）を別レコードとして残し、
  判断時点で既知だった値のみを surprise 計算へ使う。文字列から数値への
  パースに失敗した値は**欠損として扱い、0 や「正常」に丸めない**。
- **本体 P1:** surprise 符号と価格反応符号の不一致を測る。

#### 指標（`fx_intel/reaction.py` 新規、P1）

```
ReactionScore:
  event_id            : str
  surprise_z          : float | None    # 正規化 surprise（None = 測定不能）
  expected_direction  : "up"|"down"|"none"
  realized_move_r     : float           # イベント窓の実現変動（ATR正規化）
  reaction_ratio      : float | None    # realized / expected_magnitude
  invalidation        : "none"|"muted"|"inverted"
  measurable          : bool
```

- `muted`: 符号は一致するが `reaction_ratio` が下限を大きく下回る（良い材料で上がらない）。
- `inverted`: 符号が逆（良い材料で下がる）。
- `measurable=False` の場合、`invalidation` を `none` にせず**除外**する。
  測定不能を「反応正常」と誤読させない。これは非交渉ルール
  「不明なコストや品質をゼロ・正常として扱わない」の適用である。

#### 受け入れ基準

- P0 なしに P1 を実装しない。文字列 forecast のまま「良い材料」を推定しない。
- surprise の分母（consensus の分散）が不足するイベント種別は `measurable=False`。

---

### 2.3 レジームの正式な状態モデル（#3）

**現状の問題:** 特徴量・カテゴリはあるが、証拠群別の状態遷移が弱い。
`_market_regime_features` は regime を one-hot に落とすだけで、状態間の遷移も
その確信度も表現しない。

**導入後:** レジーム確率と信頼度を独立出力する。

#### 設計（`fx_intel/regime_state.py` 新規）

離散状態 S の上の遷移モデルとして定義する。状態集合は既存の regime 語彙を
継承し、**新語彙を発明しない**（既存 `dimensions["regime"]` と互換を保つ）。

```
RegimeState:
  as_of            : datetime
  probabilities    : Mapping[str, float]   # 各状態の確率、合計1
  argmax_state     : str
  confidence       : float                 # 確率の集中度（エントロピー由来）
  transition_from  : str | None
  samples          : int                   # 推定に使った独立標本数
  usable           : bool                  # 標本不足なら False
```

#### 不変条件

1. 遷移確率は **train 区画のみ**で推定する。calibration / test / lockbox を使わない
   （`regime_mixture.py` の gate 推定と同じ規律）。
2. `confidence` は「確率が高い」ではなく「**標本が足りている**」を含む。
   `samples` が下限未満なら `usable=False` とし、確率を配布しない。
3. 状態確率を**方向シグナルへ直結しない**。#3 の出力は条件付けの軸であり、
   単独の売買根拠ではない。

#### 独立標本数の落とし穴（既知）

レジーム別に切ると各セルの標本は急速に痩せる。時間足別の重複窓を独立標本と
数えると証拠件数が水増しされる。`fx_intel/effective_samples.py` の実効標本
計算を必ず経由し、`span_h` / `horizon_h` の重複を考慮した件数で `usable` を
判定する。生の行数で判定しない。

---

### 2.4 因子別ポートフォリオ（#4）

**現状の問題:** 通貨エクスポージャーはあるが、マクロ因子重複が不十分。
`virtual_portfolio.py` の `currency` は会計通貨 `JPY` 固定（`:293` の CHECK 制約）
であり、リスク因子ではない。

**導入後:** USD、金利、リスクオン、キャリー等で集約する。

#### 設計（`fx_intel/factor_exposure.py` 新規）

建玉を通貨ペア単位ではなく因子単位へ射影する。

```
FactorExposure:
  as_of      : datetime
  factors    : Mapping[str, float]   # 因子名 -> 正味エクスポージャー（R単位）
  by_position: Mapping[str, Mapping[str, float]]
  concentration : Mapping[str, float]  # 因子ごとの集中度
  basis      : "static_map"|"estimated"
  usable     : bool
```

初期因子集合（最小・拡張可能）:

| 因子 | 意味 |
|---|---|
| `usd` | USD ロング／ショートの正味 |
| `rates` | 金利差方向の正味 |
| `risk_on` | リスク選好方向の正味 |
| `carry` | キャリー方向の正味 |

#### 段階（重要）

- **第1段階 `basis="static_map"`:** ペア→因子の負荷を**固定マップ**で与える。
  USD/JPY・EUR/USD・GBP/USD の3ペアでは、共通因子は主に USD である。
  推定を挟まないため検証可能で、誤りが説明可能。
- **第2段階 `basis="estimated"`:** 収益率からの推定負荷。**PITローリング窓で
  train 区画のみ**から推定する。全期間 fit を禁止する。

第2段階を第1段階より先に実装しない。推定負荷は標本を要求し、現在の標本量では
`usable=False` になる可能性が高い。

#### 受け入れ基準

- 3ペアすべてが USD を含むため、単純な「3ペア＝分散」は誤りであることを
  因子集約が示せること。これが本機能の最小の有用性証明である。
- 逆符号の共通通貨（例: EUR/USD ロングと USD/JPY ロング）が正しく相殺方向へ
  集計されること。逆符号を独立扱いすると証拠件数と分散度が水増しされる。

---

### 2.5 判断品質ラベル（#5）

**現状の問題:** 損益・的中中心になりやすい。`evaluation_labels.py` は
`realized_net_r` を軸とした損益恒等式の検証器であり、プロセス品質の軸を持たない。

**導入後:** 良い損失／悪い利益を区別する。

#### 2軸の分離（本機能の核）

```
                 結果 good        結果 bad
プロセス good   正当な利益      良い損失（process_sound_loss）
プロセス bad    悪い利益        正当な損失
```

`ProcessQuality` は**結果を入力に取らない**。ここが本機能の成否を決める。
結果を見てプロセス品質を決めると、損益ラベルの言い換えになり価値が消える。

```
DecisionQualityLabel:
  decision_id     : str
  process_score   : float          # 判断時点情報のみから計算
  process_flags   : tuple[str,...] # 具体的な欠陥（例: 証拠不足、期待値薄）
  outcome_net_r   : float | None   # 結果（別軸、参考）
  quadrant        : "sound_win"|"sound_loss"|"lucky_win"|"deserved_loss"
  computable      : bool
```

#### 不変条件

1. `process_score` の計算関数は、結果由来の値を**引数に取れない型**にする。
   規約ではなくシグネチャで担保する。
2. `quadrant` の決定は `process_score`（事前）と `outcome_net_r`（事後）の
   合成であり、`process_score` 自体は結果非依存であること。
3. `computable=False`（判断時点情報が不足）を `process_score=0` に丸めない。

#### 用途

「悪い利益」（`lucky_win`）を成功として学習させないこと、および
「良い損失」（`sound_loss`）を過剰に罰しないことが目的。既存の学習が
`realized_net_r` のみを見ている限り、この2つは区別できない。

---

### 2.6 プロセスマイニング（#6）

**現状の問題:** 遅延や veto 連鎖を個別調査している。

**導入後:** 判断工程全体からボトルネックを自動発見する。

#### 実装が最も安価な理由

データ源が**既にある**。`decision_log.py:1275,1369` が `gate_trace` を、
`:798,993` が `policy_vetoed_by` を保存済みである。新規収集は不要で、
既存ログをイベントログとして再構成する読み取り層だけを作る。

#### 設計（`fx_intel/process_mining.py` 新規、読み取り専用）

```
ProcessEvent:
  decision_id : str
  stage       : str          # gate_trace 由来の工程名
  entered_at  : datetime
  outcome     : "pass"|"veto"|"skip"
  reason      : str | None

ProcessSummary:
  stage_counts    : Mapping[str, int]
  veto_attribution: Mapping[str, int]   # 工程別 veto 件数
  first_blocker   : Mapping[str, int]   # 「最初に止めた工程」の分布
  chains          : Mapping[tuple[str,...], int]  # veto 連鎖パターン
```

#### 不変条件

1. **読み取り専用**。判断ログを書き換えない。既存 writer 契約に触れない。
2. `first_blocker` を主指標にする。1判断が複数 veto に該当する場合、
   全 veto を等価に数えると下流工程が過大評価される。「最初に止めた工程」が
   実際のボトルネックである。
3. 中立が続く場合の最短経路として `gate_trace` を使う既存の運用知見と整合させる。

#### 受け入れ基準

- 既知の事象を再現できること。過去に手作業で特定したボトルネック（veto 支配による
  中立多発）を、本機能が自動で同じ結論に到達すること。**再現しないなら実装が誤り**。
- 大きな判断ログに対して全読込を要求しないこと（既存の肥大化の経緯を踏まえ、
  逐次走査で成立させる）。

---

### 2.7 適応的な研究配分（#7）

**現状の問題:** モデル全体を一律に改善しがち。

**導入後:** 弱い戦略×レジーム×方向へ検証資源を集中する。

#### 設計（`fx_backtester/research_allocation.py` 新規）

`(strategy, regime, direction)` セルごとに「情報利得の期待値」を推定し、
次に検証すべきセルを提案する。

```
AllocationCell:
  strategy   : str
  regime     : str
  direction  : "long"|"short"
  n_effective: int              # 実効標本数（effective_samples 経由）
  uncertainty: float            # 現在の推定の不確実性
  priority   : float            # 配分優先度
  status     : "starved"|"adequate"|"saturated"
```

#### 不変条件（本機能は最も危険）

1. **配分は選択ではない。** 検証資源をどこへ向けるかを決めるだけであり、
   昇格判定を緩めない。優先度の高いセルにも同じ baseline 優越基準を課す。
2. **多重検定の帳簿を必ず更新する。** 探索を集中させると試行回数が増え、
   偶然の有意が出やすくなる。すべての試行を `TrialLedger`
   （`trial_ledger.py:108`、ハッシュ連鎖付き）へ記録し、探索回数を
   昇格判定へ持ち込む。台帳を経由しない探索を許さない。
3. `status="starved"`（標本不足）のセルの優先度が高いのは、**そこが有望だから
   ではなく未知だから**である。「弱い＝有望」と読み替えない。
4. lockbox / test を配分判断の入力に使わない。使えば探索が test を汚染する。

#### 受け入れ基準

- 配分を回した後、`TrialLedger` の試行数が正しく増加し、昇格閾値が
  それに応じて厳しくなること。これが担保できないなら本機能は
  **過学習の加速装置**であり、導入してはならない。

---

## 3. 依存関係と実装順

```
#6 プロセスマイニング   ← 既存 gate_trace のみに依存。独立。
#1 仮説オブジェクト     ← 独立。#5 と #7 の前提。
#5 判断品質ラベル       ← #1 の process 情報を利用可能（必須ではない）
#3 レジーム状態モデル   ← effective_samples に依存
#4 因子別ポートフォリオ ← 第1段階は独立。第2段階は標本依存
#7 適応的研究配分       ← #3（レジーム軸）と TrialLedger に依存
#2 反応無効化           ← P0（calendar actual 数値化）に依存。最も高価
```

推奨実装順と根拠:

| 順 | 機能 | 根拠 |
|---|---|---|
| 1 | #6 プロセスマイニング | データ源が既存。読み取り専用で回帰リスク最小。既知事象で正しさを検証できる |
| 2 | #1 仮説オブジェクト | 新規追加のみで既存経路に触れない。#5/#7 の土台 |
| 3 | #5 判断品質ラベル | #1 の上に載る。学習信号としての価値が大きい |
| 4 | #4 因子別ポートフォリオ 第1段階 | 固定マップのみ。3ペアの USD 重複という具体的な既知問題に効く |
| 5 | #3 レジーム状態モデル | 標本制約が厳しい。`usable=False` が続く可能性を許容して進める |
| 6 | #7 適応的研究配分 | #3 と台帳が揃ってから。順序を早めると多重検定が制御不能になる |
| 7 | #2 反応無効化 | P0 のデータ契約変更が先。単独着手不可 |

**#7 を #3 と TrialLedger 連携より先に実装してはならない。** 配分だけ先に入れると
探索回数が増え、多重検定の補正が効かないまま偽の有意が量産される。

---

## 4. 完了条件

| 条件 | 状態 |
|---|---|
| 7機能の契約・スキーマ・PIT境界の固定 | ✅ 本書 |
| 実装順と依存関係の固定 | ✅ 本書 |
| #2 のブロッカー（`calendar.actual` 欠如）の特定 | ✅ 本書 |
| 各機能の実装 | ❌ **未着手（コード0行）** |
| 各機能のテスト | ❌ 未 |
| 実データでの有効性実証 | ❌ 未 |
| 本番判断への接続 | ❌ **未**（かつ機能ごとの独立昇格判定を経ない限り行わない） |

→ **設計のみ。7機能いずれも「実装済み」と記述してはならない。**
本書の存在は実装の証拠にならない。実装状況の一次情報はコードである。
