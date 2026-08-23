# 設計P1: 履歴priorによるセル学習の分散上限化

実装ステータス: **Phase 1・2 実装済み（既定ON）／Phase 3（Tier B・実機パリティ）未着手**。本文書は
[AI学習の適合性レビュー](../research/LEARNING_FITNESS_REVIEW_20260823.md) の
提案P1を、実データ測定に基づいて実装可能な仕様まで落とす。
測定の一次証拠は
[reports/research/p1-historical-prior-feasibility-20260823](../../reports/research/p1-historical-prior-feasibility-20260823/README.md)。
本文書とコードが食い違う場合はコードが正。

安全境界: 本設計は研究・意思決定支援の範囲に閉じる。broker発注、live/paper昇格、
注文サイズ決定は恒久的に対象外であり、本設計にも含まれない。launchd / process /
runtime data の構成は変更しない。

## 0. 結論の先出し

測定の結果、P1は当初想定（「履歴で事前学習して優位を見つける」）から次へ再定義された。

> **現行コードは「セル間の真の分散は無限大」と暗黙に仮定している。
> 実データはその分散の上限が 0.5〜2.3 ポイントであることを示す。
> P1はこの上限を実測し、セル調整の可動域をその上限に縛る。**

期待される効果は「新しい優位の発見」ではなく、
**根拠のない確信度減衰（無情報入力でも新規判断の43〜72%に発生）の停止**である。
この効果は既存の実験ハーネスで事前に検証でき、受け入れゲートとして固定できる。

## 1. なぜ当初案のままでは失敗するか（測定で潰した5つの罠）

| # | 罠 | 測定値 | 設計への影響 |
|---|---|---|---|
| 1 | **トレンド焼き込み** — その年の相場方向がセルの優位に化ける | 2024年24hのUSDJPY: long 58.0% / short 42.0%（16.0ptの無条件差）。現行の減衰トリガー45%を単独で跨ぐ | (ペア×方向)のde-meaningを**必須契約**にする |
| 2 | **重複ラベル** — 1時間足バーに24hラベルを貼ると隣接標本が未来の23/24を共有 | 生nならHolm補正後も12/24セルが「有意」、実効nでは**0/24** | 実効n = 生n ÷ (ホライズン/バー間隔) を契約に埋め込む。バー整合ラベル（重複係数1）を優先する |
| 3 | **de-meaningを外したときの上限膨張** | tau上限が 0.0045→0.0110 (1h)、0.0015→0.0222 (4h)、0→0.0269 (24h)。**最大14倍** | de-meaningはtau推定の前処理として必須。省略すると可動域を5〜15倍過大に許可する |
| 4 | **推定量そのものの故障** | 初版の陽性対照が不合格（真のtau=0.03を注入しても推定0）。原因は注入側の希釈で、推定器をDerSimonian-Lairdへ、対照を単一特徴世界へ修正して合格 | 陽性対照を**CIの一部**にする。通らない限りtau=0は信用しない |
| 5 | **特徴量の再現不能性** | ライブ特徴量はTradingViewスキャナー由来。`rating_4h`/`rating_1d`/`tf_agreement` はTVレーティング、`news_count` は履歴に存在しない | 特徴量を再現可能性で階層化し、Tier Aだけで開始する。Tier Bはパリティゲート通過後 |

さらにリポジトリ内で**RSI/ATRの実装規約が3系統に分裂**していることを確認した。

- `fx_backtester/indicators.py`: Wilder平滑（`ewm(alpha=1/14, adjust=False)`）
- `fx_intel/historical_chart.py`: 単純移動平均（`rolling(14).mean()`）、RSIは0〜1スケール
- ライブ経路: TradingViewの計算値（RSIはWilder、0〜100スケール）

P1経路は**単一実装**を使い、他系統との差分をパリティゲートで測る。

## 2. 確定仕様

### 2.1 スコープ

やること:

- 履歴バーから、`learning.FEATURE_SPECS` と同一規約のTier A特徴量を計算する。
- 両方向にラベルを貼り、(ペア×方向)でde-meanし、実効nでセル間分散 tau を推定する。
- tau の**片側95%上限**を版付きartifactへ固定する。
- `learning.derive_profile` のセル率推定を、この上限で収縮した推定へ置き換える。

やらないこと:

- 履歴サンプルを運用ジャーナル（`logs/briefing_journal.jsonl` 等）へ混ぜない。
- 履歴由来のセル偏差を**事前平均としては使わない**（後述2.4）。
- 重み再推定・ペア別減衰・decision_feedback・maximization は本PRの対象外
  （同じ病理を持つが、別契約として順次適用する）。
- 昇格判定・発注・通知内容の構造変更は行わない。

### 2.2 中核となる一文

現行の減衰係数は次の推定量を使う。

```
rate_c = hits_c / n_c            (n_c >= 12 のとき)
factor = clip(rate_c / 0.5, [0.7, 1.0])   (rate_c < 0.45 のとき発火)
```

これは「セル c の真の的中率は、他のセルの情報から一切推測できない」＝
**セル間分散 tau = ∞** を仮定した推定量である。P1はこの仮定を実測値へ置き換える。

```
lambda_c = tau^2 / (tau^2 + sigma_c^2),  sigma_c^2 = b(1-b) / n_c
rate_c'  = b + lambda_c * (rate_c - b)          # James-Stein 収縮
factor   = clip(rate_c' / 0.5, [0.7, 1.0])      # 発火条件も rate_c' で判定
```

`b` は減衰係数の分母と同じ 0.5（中立）。`tau -> ∞` で `lambda -> 1` となり現行と一致する。
つまり**現行挙動は本設計の特殊ケース**であり、後方互換は数学的に保証される。

> 【実装時の修正】初版はスライス全体の的中率を寄せ先にしていたが、実装中の
> テストで欠陥が出た。全体が不調な期間（全体的中率 < 0.45）には、収縮によって
> **全セルが基準割れして「どのセルも苦手」になる**。スライス全体の不調は
> `symbol_factors` の責務であり、状態別セルは「中立と区別できるほど悪いか」だけを
> 判定すべきなので、寄せ先を 0.5 固定にした
> （`tests/test_learning_prior.py::test_prior_removes_false_decay_under_a_coin_flip_null`）。

### 2.3 tau の推定（DerSimonian-Laird）

各特徴量 f について、その6セル（3バケット×2方向）を1組として推定する。

```
y_i = p_i - b_i                  # (ペア×方向) de-mean 済みのセル偏差
v_i = b_i (1 - b_i) / n_eff_i    # 実効nに基づく標本分散
w_i = 1 / v_i
Q   = sum w_i (y_i - ybar_w)^2
tau2_hat   = max(0, (Q - (k-1)) / (sum w_i - sum w_i^2 / sum w_i))
tau2_upper = max(0, (Q - chi2_{0.05,k-1}) / (同上))
```

**運用に使うのは `tau_upper95`**（点推定ではなく上限）。点推定 `tau_hat = 0` を
そのまま使うと可動域が恒久的にゼロへ固定され、運用母集団が履歴と異なっていても
永久に検出できなくなる。上限を使えば「統計的に否定できない最大の分散」まで
動くことを許し、かつそれ以上は許さない。

### 2.4 履歴は「位置」ではなく「分散」だけを与える

履歴由来のセル偏差を事前平均として注入**しない**。理由は3つ。

1. 測定の結果、履歴のセル偏差は全ホライズンで標本誤差と区別できない（Qが自由度以下）。
   位置として注入すればノイズをそのまま2026年の判断へ持ち込む。
2. 履歴は**無条件母集団**（全バー両方向）、運用は**判断条件付き母集団**である。
   位置は転移しないが、分散の上限は「市場側の構造的上限」としてより頑健に転移する。
3. レジーム変化（2020-2025 → 2026）に対して、位置は脆く、上限は保守的に安全側。

この選択により、罠#1（トレンド焼き込み）と罠#6（レジーム変化）は
**設計上到達不能**になる。履歴が判断へ注入できるのはスカラー1個（可動域）だけである。

### 2.5 特徴量の階層

| Tier | 特徴量 | 履歴再現 | P1での扱い |
|---|---|---|---|
| A | `rsi_1h` / `adx_1h` / `ma_gap_atr` / `atr_pct` | OHLCから計算可能 | **Phase 1で対象**。ただしパリティゲート通過が条件 |
| B | `rating_4h` / `rating_1d` / `tf_agreement` | TradingViewレーティング。指標値は再計算できるが、FXのVWMA等は出来高依存で完全一致しない | Phase 3。パリティゲート（バケット一致率0.8以上）を通ったものだけ |
| C | `news_count` | 履歴ニュースコーパスが存在しない | 対象外。tau未測定として可動域は現行のまま残す（= 明示的に「未測定」と表示する） |

Tier Cを黙って0扱いにしないことが重要である。**未測定と測定済み0を区別する。**

## 3. データ契約

### 3.1 コーパス

| 用途 | パス | 内容 | 重複係数 |
|---|---|---|---|
| 本番（Mac mini） | `data/historical_training/histdata/bars_m5` | 2020-2025 M5 bid/ask。既存の `fx_intel/historical_chart.py` が使用中 | バー整合ラベルなら1 |
| CI・任意clone | `data/real/histdata/*_2024_1h.csv` | 2024年 1時間足 close-only、コミット済み | 24hラベルで24 |

本番コーパスは `historical_chart.load_pair_bars` / `resample_bid_ask` を再利用する。
**新しいローダを作らない。** ラベルは各時間足の主ホライズン＝1バー先に揃え、
重複係数1で実効n=生nとなるようにする。

コーパスはPITとして自明に安全である（履歴バーのみ、特徴量は t 以前のバーだけを参照、
ラベルは t より後のバーのみ）。それでも生成器は次をfail-closedで検証する。

1. 特徴量の計算に使ったバーの最終時刻 <= 判断時刻
2. ラベルに使ったバーの開始時刻 > 判断時刻
3. 全時刻がaware UTC
4. `spread > 0`（bid/askコーパスのみ）、`label_gap <= 上限`

### 3.2 prior artifact

配置は **`config/learning_prior_v1.json`**（runtime dataではなく版管理下）。
理由: 本番判断の挙動を変える入力であり、変更が必ずPRレビューを通るべきだから。
`logs/` へは書かない（writer ownershipの問題を発生させない）。

```jsonc
{
  "schema": 1,
  "contract": "learning-prior-v1",
  "generated_at": "2026-..-..T..:..:..+00:00",
  "generator": "tools/build_learning_prior.py",
  "git_commit": "....",
  "corpus": {
    "corpus_id": "histdata-m5-2020-2025",
    "datasets": [{"path": "...", "sha256": "...", "rows": 0, "license_note": "..."}],
    "pairs": ["USDJPY", "EURUSD", "GBPUSD"],
    "window": {"start": "...", "end": "..."},
    "bar_interval_hours": 0.0833
  },
  "method": {
    "label": "direction_hit_at_primary_horizon",
    "flat_band": "atr * 0.1",
    "demean": "pair_direction",
    "effective_n": "raw / (horizon_hours / bar_interval_hours)",
    "tau_estimator": "dersimonian_laird",
    "tau_used": "upper95"
  },
  "horizons": {
    "24h": {
      "features": {
        "rsi_1h": {
          "tau_upper95": 0.0143, "tau_hat": 0.0, "cells": 6, "Q": 2.0,
          "n_eff_median": 260.7, "parity": "verified",
          "cells_detail": {"oversold|long": {"deviation": -0.010, "n_eff": 120.0}},
          "advisory_only": ["cells_detail"]
        }
      }
    }
  },
  "promotion_admissible": false
}
```

`cells_detail` は監査・将来利用のためだけに残し、**P1のコードは読まない**
（`advisory_only` に列挙して契約として明示する）。

## 4. 実装計画

### Phase 1 — 測定と生成（judgement path 不変）

新規 `fx_intel/learning_prior.py`（標準ライブラリのみ、純ロジック）:

```python
@dataclass(frozen=True)
class FeaturePrior:
    tau: float            # 運用に使う上限
    tau_hat: float
    cells: int
    q_statistic: float
    n_eff_median: float
    parity: str           # "verified" | "unverified" | "excluded"

def dersimonian_laird(deviations: Sequence[float],
                      variances: Sequence[float]) -> tuple[float, float]: ...
def shrink_rate(rate: float, n_eff: float, baseline: float, tau: float) -> float: ...
def load_prior(path: str | Path) -> LearningPrior | None: ...
```

新規 `tools/build_learning_prior.py`: コーパス → artifact。決定論的。
`--corpus {m5,csv1h}`、`--dry-run` で既存artifactとの差分だけ表示。

この段階では `learning.py` を変更せず、artifactの生成と検査だけを行う。

### Phase 2 — 学習ループへの接続（既定OFF）

`fx_intel/learning.py`:

```python
def derive_profile(
    calls, now=None, thin_gap_hours=DERIVE_THIN_GAP_HOURS,
    horizon_label="約24時間後",
    prior: LearningPrior | None = None,     # 追加。None なら現行と完全一致
    horizon_key: str = "24h",               # 追加
) -> LearnedProfile: ...
```

変更は状態×方向セルのループ1か所のみ。

```python
rate = cell["hits"] / cell["evaluated"]
if key in prior_features:  # parity が excluded の特徴量は入らない
    rate = learning_prior.shrink_rate(
        rate, cell["evaluated"], SYMBOL_HIT_RATE_BASELINE, prior_features[key]
    )
if rate >= CONDITION_HIT_RATE_TRIGGER:
    continue
```

`LearnedProfile` に `prior_taus` / `prior_corpus_id` / `prior_slice` を保存し、
学習メモへ「どの上限で収縮したか」と「どの特徴量が未測定のまま無補正か」を残す。
**tauが0でも黙らない**し、未測定を測定済み0と混同させない。

`fx_briefing.py` は artifact を読み、`learning.derive_profile` と
`tf_learning.derive_timeframe_learning` へ渡す。CLIは `--no-learning-prior` を追加。

> 【運用判断】初版は「Phase 2 は既定OFF、shadow比較を経てON」としていたが、
> 利用者の明示的な導入要求により**既定ONで導入した**。根拠は3つ。
> (1) 本変更は既存挙動を*狭める*方向にしか働かない（lambda <= 1）。
> (2) `tau -> ∞` で現行と厳密に一致することがテストで保証されている（G2）。
> (3) `--no-learning-prior` と artifact 削除の二重のロールバック経路がある。
> ただし Phase 3 の実機パリティ計測（§4.1）と shadow比較（G8）は未実施のままであり、
> **未実施であることを承知の上での導入**である。

### Phase 3 — Tier B とロールアウト

1. Mac mini でパリティ計測（4.1）。合格したTier B特徴量をartifactへ追加。
2. shadow比較: 同一ジャーナルに対し prior 有無の両プロファイルを算出し、
   確信度分布・発火セル数・的中率の差を30市場日ぶん記録する。
3. 独立レビュー後に既定ON。

## 5. 失敗モードと検知

| # | 失敗モード | 検知 | 対策 |
|---|---|---|---|
| 1 | トレンド焼き込み | 生成器がde-mean前後のtauを両方記録し、比が2倍を超えたら警告 | de-mean必須。位置は注入しない（2.4） |
| 2 | 重複ラベルで過小分散 | artifactに `bar_interval_hours` と重複係数を記録。1でない場合は明示 | 実効n契約。バー整合ラベルを優先 |
| 3 | 特徴量パリティ欠如 | パリティゲート（4.1）。不合格の特徴量は `parity: "excluded"` で収縮対象外 | Tier制。除外された特徴量は現行挙動のまま（悪化しない） |
| 4 | 推定器のバグ | 陽性対照をテスト化。tau=0.03注入で回収できなければ失敗 | CI必須項目 |
| 5 | 選択バイアス（無条件 vs 判断条件付き） | 運用ジャーナル側でも同じ推定器でtauを測り、artifact値と比較して記録 | 位置を注入しないため影響は可動域のみ。運用tauが上回れば artifact を更新 |
| 6 | レジーム変化 | artifactに `window` を記録し、生成から18か月で `stale` 警告 | 上限のみの注入。定期再生成 |
| 7 | 履歴行の運用ジャーナル混入 | 生成器は `logs/` を開かない（テストで禁止を検証）。artifactは `config/` | producer/contractを分離。PITの `pit_eligible` を立てない |
| 8 | priorが隠れた権威になる | `gate_trace` と学習メモに tau と収縮量を必ず記録 | 可動域は [0.7, 1.0] のまま。priorは範囲を**狭める**方向にしか働かない |
| 9 | 規約分裂（RSI/ATRの3実装） | パリティ計測で数値差を記録 | P1経路は単一実装。差分はartifactへ残す |
| 10 | ライセンス逸脱 | artifactに `license_note` と `promotion_admissible: false` | 昇格証拠には使わない。conviction減衰の可動域制限にのみ使う |
| 11 | 未測定と測定済み0の混同 | Tier Cは `parity: "unverified"`、収縮対象外 | 表示で区別。黙って0にしない |

## 6. 受け入れゲート（すべて事前に固定、falsifiable）

| # | ゲート | 基準 | ハーネス |
|---|---|---|---|
| G1 | 後方互換 | `prior=None` で既存テスト全件が無変更で緑 | 既存 `tests/` |
| G2 | 数学的等価 | `tau = 1e9` で現行 `derive_profile` と同一の `condition_factors` | 新規テスト |
| G3 | 偽陽性除去 | コイン投げ入力での偽減衰セルが**全nで0.1未満**（現行 0.88〜12.0） | [null実験](../../reports/research/learning-null-power-20260823/README.md) を回帰テスト化 |
| G4 | 推定器の陽性対照 | 注入 tau=0.03 を回収（推定 > 0.01） | 新規テスト |
| G5 | 真の劣位の検出保持 | tau=0.05 の世界では検出率が現行と同等以上 | 合成世界テスト |
| G6 | 決定論 | 同一コーパスで artifact の sha256 が一致 | 生成器テスト |
| G7 | 特徴量パリティ | Tier A各特徴量で Spearman >= 0.9 かつバケット一致率 >= 0.8 | 4.1（Mac mini） |
| G8 | shadow比較 | 30市場日で確信度分布の変化と発火セル数を記録。**改善が出なければ「改善なし」と記録して既定ONにしない** | 実機 |

G3とG5は同時に満たす必要がある。片方だけなら「常に減衰しない」か
「常に減衰する」で通ってしまう。

### 4.1 パリティ計測（G7の手順）

Mac mini上で、同一 `(symbol, timestamp)` について
運用ジャーナルに記録済みの `features` と、履歴コーパスから再計算した値を突き合わせる。

- Spearman順位相関 >= 0.9
- `FEATURE_SPECS` のバケット一致率 >= 0.8
- 系統的バイアス（中央値差 / IQR）を記録

不合格の特徴量は `parity: "excluded"` として収縮対象から外す。
**除外しても現行より悪化しない**（現行挙動が維持されるだけ）ことが、この設計の
フェイルセーフ性である。

## 7. 運用

- launchd / plist / 収集プロセスは**変更しない**。
- artifactは版管理下（`config/learning_prior_v1.json`）。実機は `git pull` で受け取る。
- 生成は人間が明示的に実行する（`tools/build_learning_prior.py`）。定期実行しない。
- ロールバック: `--no-learning-prior`、または artifact を削除。
  どちらも現行挙動へ**厳密に**戻る（G2で保証）。
- 再生成の頻度: コーパス更新時、または18か月経過時。

## 8. 期待される効果と、期待してはいけないこと

### 実装後の実測（無情報入力・30シード・コミット済みartifact）

| 実効n | 減衰セル 前→後 | 新規判断が減衰される割合 前→後 |
|---:|---|---|
| 200 | 11.2 → 6.6 | 71.9% → 59.6% |
| 500 | 9.5 → 4.8 | 61.0% → 48.1% |
| 1000 | 5.2 → 2.2 | 35.9% → 21.8% |
| 2000 | 2.2 → 0.8 | 16.1% → 8.4% |

**偽減衰はおよそ半減にとどまる。** 測定できた特徴量（Tier A 4件）のセルでは
ほぼゼロになる（`test_prior_removes_false_decay_under_a_coin_flip_null` で
平均0.0を要求）が、残る4件（`rating_4h` / `rating_1d` / `tf_agreement` /
`news_count`）は履歴再現できず未測定のため、従来どおり無補正で動く。
**残り半分を消すにはPhase 3（Tier Bのパリティ計測）が必要**であり、
本実装だけで「偽減衰が消えた」とは言えない。学習メモには
「未測定のまま無補正: ...」として該当特徴量を毎回明示する。

期待される（測定で裏付けあり）:

- Tier A 特徴量のセルでは無情報入力での偽減衰が実質ゼロになる（G3）。
  全体では上表のとおり約半減。
- 学習メモから根拠のない「苦手パターン」の断定が消え、
  「セル間の差は検出できない（上限 x.x%）」という**正しい不確実性の報告**に変わる。
- 「状態別の調整は最大どれだけ正当化できるか」という数値が、以後の全提案の基準になる。

期待してはいけない:

- 的中率の改善。履歴はTier A特徴量に**検出可能な条件付き優位が無い**と言っている。
- 収益性の改善。本設計は収益に関する主張を一切含まない。
- Tier B（上位足整合）についての結論。未測定であり、Phase 3の主題である。

最も重要な効果は、**システムが偽の相場知識を人間に提示するのを止めること**である。
現行は標本誤差から「RSI売られすぎ圏のロングは苦手」といった断定を生成し、
人間がそれを学習してしまう。これは確信度の数値誤差より深刻な害である。

## 9. 未解決事項

- Tier Bのパリティは未測定。TradingViewのFXレーティングは出来高依存指標を含み、
  完全一致は不可能。どこまでの一致で「使える」とするかはPhase 3で決める。
- 運用母集団のtau（選択バイアス）は実機ジャーナルでしか測れない。
- 同じ病理を持つ `symbol_factors` / `decision_feedback` / `maximization` /
  `_estimate_weights` への適用は本設計の対象外。契約を分けて順次行う。
- 分解能の下限は現在 0.01〜0.02。2020-2025 M5コーパスを使えば
  24hホライズンの実効nが約6倍になり、分解能は約0.008まで改善する見込み。
- `tf:15m` スライスは1時間足コーパスから作れないため未生成で、15m判断のセル学習は
  従来どおり無補正のまま。M5コーパス（`data/historical_training/histdata/bars_m5`）を
  読む生成経路は未実装。
- 実機での `--no-learning-prior` 有無のA/B比較（G8）は未実施。

## 10. 非主張

- Phase 1・2 は実装済みだが、**実機での効果は測定されていない**。上表は合成入力での
  実測であり、運用ジャーナルの実分布では偽減衰率もその削減幅も変わりうる。
- 測定はTier A 4特徴量・1年・3ペア・1時間足に基づく（本番コーパスでは拡張予定）。
- HistDataはclose-only（1hコーパス）で、昇格証拠には使えない。
- 発注、live/paper昇格、自動売買に関する提案は一切含まない。恒久的に対象外。
