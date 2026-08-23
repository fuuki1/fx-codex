# AI学習の適合性レビューと次世代学習方式の調査 — 2026-08-23

**対象:** `fx_intel/` の常設学習ループ（learning / tf_learning / tp_sl_learning /
decision_feedback / maximization / horizon_learning / ml）と、`fx_backtester/` の
研究パイプライン。
**目的:** 「いまのAI学習はFXに適しているか」を証拠付きで判定し、外部調査から
この基盤にとって実際に効きうる学習方式を優先度順に提示する。
**安全境界:** 本文書の提案はすべて研究・shadow・オフラインシミュレーションの範囲に閉じる。
broker発注、live/paper昇格、注文サイズ決定は恒久的に対象外であり、提案にも含めない。
本文書とコードが食い違う場合はコードが正。

## 0. 結論

判定は3層で分かれる。ひとつの答えにまとめると誤読になる。

| 層 | 判定 | 根拠 |
|---|---|---|
| 検証・ガバナンス基盤（`fx_backtester/` + PIT/embargo/lockbox/DSR/PBO/Holm） | **FXに適している。水準は高い** | 実データrunで「優位なし」を正しく検出し昇格を拒否した記録が残る |
| 常設の自己採点ループ（`fx_intel/learning.py` 系の状態×方向セル学習と重み再推定） | **不適合。学習ではなくノイズ由来の確信度抑制になっている** | 本レビューで実施したnull/power実験（[evidence](../../reports/research/learning-null-power-20260823/README.md)） |
| 最も設計の良い学習器（3ヘッドGBDT + DSR/PBO、`fx_intel/ml.py`） | **能力はあるが恒久的に判断へ不参加** | `committee.STAGE_ACTIVE = ()`、`promotion.STAGES = ("shadow",)` |

したがって「この基盤のAIがFXに向いていない」のではない。**向いている部分（反証装置）と、
向いていない部分（少標本counting）が同居しており、後者だけが実判断に接続されている**。
能力の上限を決めているのはモデルアーキテクチャではなく、次の4つである。

1. 標本量（自己生成ジャーナルだけで学習している）
2. 目的変数の選択（方向的中率はFXで最も学びにくい量）
3. 多重比較の未補正（オンライン経路にHolm/FDR/DSRが無い）
4. 調整の非対称性（減衰のみ。発見した優位を使う経路が融合側に無い）

Transformerやfoundation modelへの置換は、この4つのどれも解かない。

## 1. 維持すべき資産（FX研究基盤として適合している部分）

- **PIT契約とラベル分離** — `horizon-pit-v1` / `fusion-pit`、availability/ingestion/revisionの分離、
  aware UTC、未来as-of拒否。個人開発の研究基盤としては例外的に厳格。
- **時系列分割の作法** — `ml.py` の train/tune/calibration/test/lockbox 5分割＋各境界72時間embargo、
  Platt較正はcalibration区間のみ。`_temporal_partitions` で実装され、テストで担保されている。
- **コスト控除後の目的関数** — `trade_outcome` の canonical net R、`evaluation_labels` の
  label_version / cost_model_id 混在拒否。的中率ではなく期待Rを主指標に置く設計思想。
- **実効サンプル会計** — `effective_samples.py` が重複ホライズンを潰して実効数を数える。
- **見送りの一級市民化** — `direction_threshold.py` は閾値をより厳しい方向にしか動かせず、
  90日有効期限とガバナンス段階を持つ。`experiment_pipeline` は no-trade を正式候補として登録する。
- **反証の実績** — HistData USD/JPY 2024 1h の実データrunで、10候補中GBDTが選ばれた上で
  純期待値 −0.065R / DSR 0.167 / Holm補正後 p=1.0 / 昇格DENIED を記録している
  （[evidence](../../reports/evidence/histdata-usdjpy-real-2024-1h-20260713/README.md)）。
  **偽のalphaを製造しない**ことの直接証拠であり、この基盤の最大の資産。
- **分位点トラック** — 設計Aの9ホライズン × p10/p50/p90 × vol×session経験帯は、
  後述するとおり「FXで実際に学べる量」に最も近い場所に既に到達している。

## 2. 不適合の実態（証拠付き）

### 2.1 少標本counting＋多重比較未補正（実測）

`learning.derive_profile` は `8特徴量 × 3バケット × 2方向 = 48セル` を同時に検定し、
`MIN_CONDITION_SAMPLES = 12` かつ的中率 `< 0.45` のセルに減衰係数を付ける。
補正は無い。`fx_intel/` 配下に Holm / Bonferroni / FDR の実装は存在せず、
Holm・PBO・DSR は `fx_backtester/` と `ml.py` の収益ヘッドにしか無い。

本番コードそのままにコイン投げ（真の優位ゼロ）を食わせた結果:

| 実効n | 市場週 | 偽陽性セル(平均/48) | 新規判断が減衰される割合 | tech重みSD |
|---:|---:|---:|---:|---:|
| 500 | 4.2 | 9.75 | **58.9%** | 0.133 |
| 1000 | 8.3 | 6.08 | **43.3%** | 0.151 |
| 2000 | 16.7 | 3.02 | 22.9% | 0.143 |

学習すべき情報がゼロの入力でも、運用規模では新規判断の4〜7割が「過去に外しやすかった状態」を
理由に減衰される。複合重み `tech_weight` は既定0.55に対しSD 0.13〜0.15で振れ、
全水準でクランプ両端（0.35 / 0.70）到達シードが出た。**クランプだけが暴れを止めている。**

真に劣位なセル（的中率0.40）を1つ埋め込んだ検出力試験:

| 実効n | 検出率 | 同時に出る偽陽性 | 精度 |
|---:|---:|---:|---:|
| 500 | 70.0% | 11.65 | **5.7%** |
| 1000 | 80.0% | 6.83 | **10.5%** |
| 4000 | 92.5% | 1.38 | 40.2% |

10ポイントもの大きな劣位を見つけたとしても、運用規模では**本物1件あたり偽物7〜12件**。
トリガー境界の0.45に至っては n=4000（約33市場週）でも検出率52.5%。
二項検定で的中率0.55を検出力80%で検出するには約783件、0.52なら約4,903件を要する。
`MIN_CONDITION_SAMPLES = 12` は必要数より1〜2桁小さい。

再現手順と全結果: [reports/research/learning-null-power-20260823/](../../reports/research/learning-null-power-20260823/README.md)

### 2.2 標本飢餓 — 自己生成ジャーナルだけで学習している

`learning.evaluate_history` / `tf_learning` / `horizon_learning` の教師データは、
すべて**このシステムが稼働してから自分で書いたジャーナル**である。履歴相場からは学習しない。

- 融合判断: 約1時間周期 × 3ペア。1時間間引き後で1スライスあたり週120件が上限。
- 時間足別: 間引き後 (symbol, timeframe) セルあたり週120件、8スライスへ分散。
- 状態×方向セルへ分配すると1セルあたり週2〜3件。§2.1の必要数783件には**5〜6年**かかる。

一方、同じ特徴量・同じラベル契約は履歴価格に対しても計算できる。リポジトリ内の実データは
`data/real/histdata/` の3ペア × 2024年 × 1h（各6,265バー）に留まるが、この経路を通せば
1セルあたりの標本は3桁増える。**現在の学習が遅いのはアルゴリズムのせいではなく、
学習データを稼働後の自己記録に限定しているため。**

### 2.3 目的変数のミスマッチ

主目的が「固定ホライズン後に方向が合っていたか」である限り、FXでは学習信号がほぼ無い。
`docs/AI_LEARNING_V2_DESIGN.md` は既にこれを補助へ降格し Triple Barrier / net R を主に据えているが、
**常設ループで実判断を動かしているのは依然として方向的中率ベースの係数**である
（`learning.py` の `condition_factors` / `symbol_factors`、`tf_learning` の profile_lookup）。

対照的に、ボラティリティ・値幅・到達時間は予測可能性が確立した量であり、
設計Aの p10/p50/p90 と経験帯はそこに当たっている。**資産は既にあるのに、
実判断を動かす経路は学びにくい量の側に繋がっている。**

### 2.4 調整の非対称性

`learning.py` の学習は減衰のみ（`SYMBOL_FACTOR_MIN = 0.6`、`CONDITION_FACTOR_MIN = 0.7`、
的中率が良くても係数は1.0で頭打ち）。増幅経路は `maximization.py` の
`BOOST_FACTOR_MAX = 1.10`（30件から、boostは100件から）だけ。
§2.1の偽陽性率と組み合わせると、ループの実効的な働きは
**ノイズ由来の下方バイアスを判断へ注入すること**になる。安全側ではあるが学習ではない。

### 2.5 未使用のedge source（既に手元にあるのに使っていない情報）

| 情報 | 現状 | 損失 |
|---|---|---|
| 経済指標の actual | `calendar.py` は forecast / previous のみ parse。actual を取得していない | **サプライズ（actual − forecast）はFXで最も再現性のある短期シグナルのひとつ**。現状カレンダーは「回避窓」としてしか使われていない |
| 金利差 / carry | FREDから米10年・2年・VIX・ドル指数は取るが、通貨ペアの金利差を作っていない | carry はFXの代表的リスクプレミアム。日次でも効く軸を捨てている |
| cross-sectional USD factor | `usd_coherence.py` が3ペアからUSDスタンスの矛盾を監査するが**観測専用**、かつ学習特徴量ではない | 3ペアが共有するUSD成分を分離すれば、実質的な独立標本数と信号対雑音比が上がる |
| COT positioning | `cot_pit.py` に監査付きPIT実装があるが canonical briefing は `include_cot=False` で切断 | 収集済み情報が学習へ届いていない |
| spread / liquidity | `ml.direction_features` には入っているが、そのMLは shadow 固定 | 実判断へ届かない |

### 2.6 非定常性・較正の扱い

- レジームは `learning_dimensions` に記録され `_derive_dimension_stats` で集計されるが、
  設計B のとおり**観測専用**。ドリフト検出（変化点・ADWIN相当）も常設経路に無い。
- 較正はBrierの観測と、horizon側の疑似観測3件シュリンクによる経験較正まで。
  **被覆保証（この帯に入る確率が名目どおりか）を持つ手法は入っていない。**

## 3. 外部調査 — 2026年時点で「効く/効かない」と報告されている方向

### 3.1 時系列foundation modelは「リターン」には効かない、「ボラティリティ」には有望

Chronos-2 / TimesFM / Moirai-2 / Lag-Llama がゼロショット予測の主役になっているが、
金融特化の評価では**日次超過リターンのゼロショット予測でCatBoost/LightGBMに劣後**し、
Chronos(large)でOOS R² −1.37% / 方向的中率わずか51%超、TimesFM(500M)でR² −2.80% / 方向的中率50%未満と報告されている。
一方、実現ボラティリティ予測ではHAR系ベンチマークと比較可能な土俵に乗る研究が続いている。
**「モデルを大きくすればFXの方向が当たる」という筋は、外部証拠が否定している。**

### 3.2 conformal prediction（適合予測）— 分布仮定なしの被覆保証

Adaptive Conformal Inference (ACI) は誤被覆率を逐次更新して名目水準へ収束させる。
2025〜2026年には変化点対応、非交換可能ストリーム対応（drift-aware）、
多段ホライズン向けの自己相関補正（AcMCP）まで揃った。
**設計Aの帯（p10/p50/p90）とカバレッジゲート（70〜90%）に直接嵌まる。**

### 3.3 TabPFN系（prior-data fitted network）— 少標本表形式の基盤モデル

Hollmann et al. (2025, Nature) の TabPFN は合成データで事前学習した transformer で、
**1万件以下の表形式データでチューニング済みアンサンブルを短時間で上回る**。
後継は10万行〜100万行規模まで拡張されている。
このシステムのセル標本規模（数百〜数千）は、まさにこの手法が最も強い領域。

### 3.4 meta-labeling — 「方向」と「張るか否か」を分離する

López de Prado の枠組み。一次モデルが方向、二次モデルが「そのシグナルに乗るべきか」を学ぶ。
`maximization.py` / `decision_feedback.py` は**countingで作った劣化版meta-labeling**であり、
同じ役割をGBDT + net Rラベルで正しく実装する余地がある。

### 3.5 階層ベイズの部分プーリング

セル別に独立集計して閾値で叩く方式の正しい代替。全体平均へ縮約された事後分布は、
標本が少ないセルほど強く全体へ寄り、多いセルほど自分の値に近づく。
§2.1の偽陽性は、統計的には**この推定量を使えば自動的に消える種類の誤り**である。

### 3.6 非定常環境のオンライン学習（regret保証）

ADWIN等の明示的ドリフト検出と、変動予算 V_T に対する O(√T + V_T) の動的regret保証。
指数重み付け（Hedge）系の集約は「事後的な最良エキスパートに対して大きく負けない」ことを保証する。
現在の手作りシュリンク（`n/(n+40)`）＋クランプは、この保証をどこにも持たない近似。

### 3.7 FX固有の予測可能性

- **order flow**: 同時点のリターンとは強く相関するが、**将来リターンへの予測力は弱い**
  （日次〜週次では相対的に強い）。個人が取得できない板情報に幻想を持つ必要はない。
- **carry / momentum / value / basis-momentum**: 200年超の長期検証を含め通貨横断で反復報告。
  2025年の研究では、横断的特性からのML予測でファクター・タイミングを行い、
  ベンチマークを上回るシャープを報告する例が出ている。
- **options / risk reversal / 歪度**: 四半期末のリスクリバーサル予測など、条件付きの予測力の報告あり。
- **CFTC positioning**: 1986年以降の系列でポジションと通貨リターンの関係が研究されている。

### 3.8 LLMニュース系は「期待は高いが検証が甘い」

ChatGPT/RoBERTa等で為替テキストを処理して予測力を報告する研究、
シャープ3.05や5.0といった数値を出す研究が並ぶ一方、
**LLMの学習データに評価期間が含まれるlook-ahead biasを扱った研究が同時に存在する**。
本リポジトリの評価規律（PIT、Holm、DSR）を当てれば、これらの多くは通らない。
LLM系は**shadow producer限定**で、既存の `shadow_learning` 契約に載せて採点するのが正しい扱い。

## 4. 提案 — この基盤にとって「画期的」になる7手（優先度順）

各提案は「shadow producer として並走 → canonical net R / 3クラスBrier で採点 →
既存の昇格ゲート（Brier ≤ climatology、帯カバレッジ70〜90%、平均純R>0、
DSR/PBO/Holm、直近7日のPIT違反0）を通ってから初めて既定経路へ」の順で扱う。
どの提案も発注・live/paper昇格とは無関係である。

### P1. 学習の「履歴事前学習」— 標本を10²から10⁵へ

**内容:** `learning.evaluate_history` と同じ特徴量・ラベル契約を、稼働後ジャーナルではなく
履歴価格のリプレイに対して計算し、セル別事前分布（および重み初期値）を作る。
運用ジャーナルはその事前分布を更新する追加証拠として扱う。

**なぜ効くか:** §2.2の飢餓を直接解く唯一の手。他の全提案の効果もこれに乗る。
GBDTを賢くするより桁が大きい。

**実装の勘所:** `fx_backtester/pit_dataset.py` + `labeling.py` で特徴量とラベルを生成し、
`learning.EvaluatedCall` 互換の系列へ落として `derive_profile` へ渡す。
リプレイは train/tune/test 区画とembargoを既存 `_temporal_partitions` の作法で切る。
運用ジャーナル由来と履歴由来のサンプルは `provenance` で必ず分離し、混ぜた集計を残さない。

**リスク:** 履歴の特徴量とライブの特徴量の分布差（TradingView値と履歴計算値の乖離）。
まず「同一期間で両者を計算して差分を測る」PIT対称性チェックが先。

### P2. counting撤廃 → 階層ベイズ部分プーリング + FDRゲート

**内容:** セルごとの `hits/evaluated` と閾値判定をやめ、Beta-Binomialの階層モデルで
各セルの的中率を全体平均へ縮約推定する。減衰係数は事後平均から連続的に作る。
さらに「調整を発火してよいセル」を Benjamini-Hochberg で選抜する。

**なぜ効くか:** §2.1の偽陽性はこの推定量で構造的に消える。
n=12のセルは事後がほぼ全体平均に張り付き、係数は自動的に1.0近傍になる。
閾値・クランプという手作りのガードが不要になる。

**実装の勘所:** 共役なので追加依存ゼロ、`learning.py` 内で完結する。
`condition_factors` の契約（`(features, direction) -> (factor, reason)`）は維持できるので、
`timeframe.py` / `briefing.py` 側は無改修。
移行時は旧counting版と新版を並走させ、同一ジャーナルでの係数分布を比較する。

**検証ゲート:** 本レビューの null 実験を回帰テスト化し、
**コイン投げ入力での偽陽性セル数が全nで1未満**になることを要求する。

### P3. 実判断を動かす量を「分布・到達・純R」へ移す

**内容:** 常設ループが判断へ注入する係数の主軸を、方向的中率ベース（`learning.py`）から
(a) 分位点予測とその被覆、(b) TP/SL先着確率、(c) コスト控除後期待R へ移す。
方向は「分布から導かれる派生量」に格下げする。

**なぜ効くか:** §2.3と§3.1。学べる量に学習資源を集中させる。
設計A（horizon）と `tp_sl_learning` / `trade_outcome` に実装済み資産がある。

**実装の勘所:** horizon側の `promotion.integration_eligible` が付いたセルから順に、
時間足別判断の確信度・見送りへ接続する。方向的中率の学習は観測・反省レポートへ降格。

### P4. オンライン conformal による被覆保証つき見送り

**内容:** horizon帯に Adaptive Conformal Inference を被せ、
誤被覆率を逐次更新して名目カバレッジへ収束させる。
帯が広がりすぎる（＝不確実性が高い）局面を定量的な見送り条件にする。

**なぜ効くか:** 現在の帯は経験分位点で、被覆は事後観測しかできない。
ACIは非定常下でもカバレッジを目標へ引き戻す。既存ゲート「帯カバレッジ70〜90%」に
**保証付きで到達する手段**を与える。追加依存ゼロで実装できる。

**実装の勘所:** `horizon_learning._band` の後段に、セル別の誤被覆推定と
分位点オフセットの逐次更新を足す。`shadow_only` セルで先に検証する。

### P5. 既に持っている未使用のedge source 3つを接続

1. **経済指標サプライズ** — `calendar.py` の parse に `actual` を追加し、
   公表時刻・観測時刻付きで保存。`(actual − forecast)` を指標ごとに標準化して特徴量化。
   カレンダーを「回避窓」だけでなく**予測入力**として使う。
   PIT上、actualは公表後にしか存在しないことを契約で保証する（`source_contracts.py`）。
2. **金利差 / carry** — FREDで既に取得している金利系列にペア通貨側を足し、
   金利差と其の変化を特徴量化。
3. **cross-sectional USD factor** — `usd_coherence.py` のスタンス計算を発展させ、
   3ペアから共通USD成分と各ペア固有成分を分離して両方を特徴量にする。
   三角整合（USDJPY・EURUSD・GBPUSD から導かれるクロス）を制約として使えば、
   ラベルノイズの一部を相殺できる。

**なぜ効くか:** 新しいデータ購入も新依存も不要で、§3.7の外部証拠が支持する軸に届く。
現在の特徴量はRSI/ADX/MA/ATR/ニュース件数が中心で、FXのリスクプレミアム構造を全く見ていない。

### P6. ホライズン間の予測整合（temporal hierarchy reconciliation）

**内容:** 同一ペアの 15m/30m/1h/3h/6h/12h/24h/3d 予測を独立に出さず、
時間階層として整合させる（短期の和が長期と矛盾しない射影）。

**なぜ効くか:** 9本のホライズンを毎5分出しているシステムは珍しく、
**この構造自体が未活用の統計的資産**。整合射影は分散低減として働き、
標本不足のホライズンが標本の多いホライズンから強度を借りられる。
既存の `PRIOR_WEIGHTS`（ホライズン別の入力重み）とも自然に接続する。

### P7. regret保証つきオンライン集約 + ドリフト検出

**内容:** 「経験較正」「GBDT」「climatology」「no-trade」を候補エキスパートとして、
指数重み付け（Hedge）で逐次集約する。重みの更新則は手作りシュリンクではなく損失に基づく。
併せてADWIN相当の変化点検出を入れ、検出時にウィンドウをリセットする。

**なぜ効くか:** §3.6。`_learn_weights` の `n/(n+40)` は理論保証の無い近似で、
§2.1のとおりノイズで暴れる。Hedgeなら「事後的に最良のエキスパートに対する後悔」が
有界であることを、標本数の仮定なしに言える。

### 補: 採らない/限定する選択肢

- **TabPFN / TSFM** — §3.3は魅力的だが、`fx_intel` の「追加依存ゼロ」方針と衝突する。
  `fx_backtester` 側のオフライン研究で**ベースライン比較の1候補**として評価し、
  常設経路には入れない。TSFMのゼロショットをリターン予測に使うのは§3.1により非推奨。
- **LLMニュース解析** — shadow producer 限定。`shadow_learning.prediction_draft` に載せ、
  canonical net R で採点する。look-ahead biasの検査（学習データ期間の申告、
  同一ニュースの過去日での再現テスト）を通るまで昇格候補にしない。

## 5. やっても効かないこと（明示的な非推奨）

| 案 | なぜ効かないか |
|---|---|
| GBDTをTransformer/LLMへ置換 | §2の4つのボトルネックをどれも解かない。§3.1の外部証拠も否定的 |
| 通貨ペア・時間足・ホライズンを増やす | セル数が増え、§2.1の偽陽性が線形に増える。標本は増えない |
| 方向的中率の改善を目標に据える | FXで最も学びにくい量。§2.3 |
| 5分周期をさらに短くしてサンプルを稼ぐ | 間引き前の重複が増えるだけで実効標本は増えない（`thin_calls` の設計どおり） |
| 減衰トリガーを緩める / 閾値を下げる | 偽陽性が増える。問題は閾値の位置ではなく推定量の選択（P2） |

## 6. 検証設計 — この提案自体をどう反証するか

1. **事前登録**: 各提案を trial ledger に候補として登録し、seed・窓・データhashを記録する。
2. **shadow並走**: 既存判断は変更せず、新方式を shadow producer として同時記録する。
3. **同一ラベルで採点**: canonical net R（`label_version` / `cost_model_id` 固定）と
   3クラスBrier / pinball / 帯カバレッジ。
4. **既存ゲート適用**: Brier ≤ climatology、カバレッジ70〜90%、平均純R > 0、
   DSR/PBO/Holm、直近7日のPIT・鮮度違反0。
5. **回帰テスト化**: 本レビューのnull実験を `tests/` へ移し、
   「コイン投げ入力で偽陽性セル1未満」を恒久的な不変条件にする。
6. **負の結果の記録**: 改善が出なければ「改善なし」、証拠不足なら「評価不能・昇格不能」と記録する。
   lockboxは触らない。

## 7. この文書が主張しないこと

- 収益性、優位の存在、institutional-grade readiness を一切主張しない。
- 提案の効果はいずれも未検証の仮説であり、実装前後の比較証拠は存在しない。
- null/power実験は合成特徴量分布に基づく。実運用分布での偽陽性率は
  バケット占有率に依存して上下する。読むべきは桁と傾向。
- 外部調査は二次情報（検索結果の要約）を含み、原論文の完全な追試はしていない。
  特に§3.8のシャープ比の報告値は、本リポジトリの評価規律では検証されていない。
- 発注、live/paper昇格、自動売買に関する提案は含まない。本リポジトリでは恒久的に対象外。

## 8. 参考文献・出典

**時系列foundation model**
- [The 2026 Time Series Toolkit: 5 Foundation Models for Autonomous Forecasting](https://machinelearningmastery.com/the-2026-time-series-toolkit-5-foundation-models-for-autonomous-forecasting/)
- [Time-Series Foundation Models in Finance: Pretraining Corpora, Architectures, Financial Benchmarks, and Risk-Aware Evaluation (ACM, 2025)](https://dl.acm.org/doi/full/10.1145/3785706.3785728)
- [Re(Visiting) Time Series Foundation Models in Finance — Rahimikia (FoFI 2026)](http://wp.lancs.ac.uk/fofi2026/files/2026/03/FoFI-2026-020-Eghbal-Rahimikia.pdf)
- [Forecasting Realized Volatility with Time Series Foundation Models: A Comparison with Econometric Benchmarks](https://arxiv.org/pdf/2607.05291)

**conformal prediction**
- [Adaptive Conformal Predictions for Time Series — Zaffran et al. (PMLR)](https://proceedings.mlr.press/v162/zaffran22a/zaffran22a.pdf)
- [Conformal Prediction Algorithms for Time Series Forecasting: Methods and Benchmarking (2026)](https://arxiv.org/html/2601.18509v2)
- [Bias-Corrected Adaptive Conformal Inference for Multi-Horizon Time Series Forecasting](https://arxiv.org/pdf/2604.13253)
- [Drift-Aware Spectral Conformal Prediction for Non-Exchangeable Streaming Data](https://arxiv.org/pdf/2606.15953)
- [Conformal Prediction for Time-series Forecasting with Change Points (OpenReview)](https://openreview.net/forum?id=HgLaVgCpCl)

**少標本・表形式の基盤モデル**
- [Accurate predictions on small data with a tabular foundation model — Hollmann et al., Nature (2025)](https://www.nature.com/articles/s41586-024-08328-6)
- [TabPFN (Wikipedia、後継版の規模)](https://en.wikipedia.org/wiki/TabPFN)

**meta-labeling / 過学習検定**
- [Meta-Labeling (Wikipedia)](https://en.wikipedia.org/wiki/Meta-Labeling)
- [Does Meta Labeling Add to Signal Efficacy? — Hudson & Thames](https://hudsonthames.org/does-meta-labeling-add-to-signal-efficacy-triple-barrier-method/)
- [Why Meta-Labeling Is Not a Silver Bullet — QuantConnect](https://www.quantconnect.com/forum/discussion/14706/why-meta-labeling-is-not-a-silver-bullet/)
- [The Deflated Sharpe Ratio — Bailey & López de Prado](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
- [Deflated Sharpe ratio / False Strategy Theorem (Wikipedia)](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio)

**階層ベイズ・部分プーリング**
- [Shrinkage and Hierarchical Models — Bayesian Notes](https://jrnold.github.io/bayesian_notes/shrinkage-and-hierarchical-models.html)
- [Hierarchical Linear Models: Notes on Shrinkage and Partial Pooling](https://vasishth.github.io/IntroBayesSMLP2021/slides/04HLMAdditionalNotesShrinkage.pdf)

**非定常オンライン学習**
- [Retrieval-Augmented Online Learning for Concept Drift Adaptation](https://arxiv.org/html/2512.02333)
- [Handling Concept Drift in Global Time Series Forecasting](https://www.researchgate.net/publication/374078576_Handling_Concept_Drift_in_Global_Time_Series_Forecasting)

**FXの予測可能性**
- [An investigation of customer order flow in the foreign exchange market](https://www.sciencedirect.com/science/article/abs/pii/S0378426610004504)
- [Exchange rate forecasting, order flow and macroeconomic information](https://www.sciencedirect.com/science/article/abs/pii/S0022199609000464)
- [Assessing cross-currency predictability in forex markets: Insights from limit order book data (2025)](https://www.sciencedirect.com/science/article/pii/S0169207025001281)
- [Conditional currency momentum portfolios (2025)](https://www.sciencedirect.com/science/article/abs/pii/S1057521925000511)
- [Understanding the Performance of Currency Basis-Momentum (2025)](https://onlinelibrary.wiley.com/doi/full/10.1111/eufm.12555)
- [FX Carry + Value + Momentum Strategies over Their 200+ Year History — QuantPedia](https://quantpedia.com/fx-carry-value-momentum-strategies-over-their-200-year-history/)
- [Forecasting the realized variance in the presence of intraday periodicity](https://www.sciencedirect.com/science/article/pii/S0378426624002565)
- [Intraday FX Volatility-Curve Forecasting with Functional GARCH Approaches](https://arxiv.org/html/2311.18477v3)

**LLM / テキスト系（検証上の注意付き）**
- [The power of ChatGPT in processing text: Evidence from the exchange rate markets](https://link.springer.com/article/10.1186/s40854-025-00789-6)
- [EUR/USD Exchange Rate Forecasting incorporating Text Mining](https://arxiv.org/html/2411.07560v2)
- [Sentiment trading with large language models](https://arxiv.org/pdf/2412.19245)
- [Assessing Look-Ahead Bias in Stock Return Predictions Generated By GPT Sentiment Analysis](https://arxiv.org/html/2309.17322)
