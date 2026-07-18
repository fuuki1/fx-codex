# D. 評価ラベルと調整対象の設計

## 1. 結論

D は次の境界で実装する。

1. 主評価ラベルを、約定可能な bid/ask と明示的なコスト来歴から作る
   `realized_net_r` に統一する。
2. 方向的中、固定ホライズン値幅、MFE/MAE、グロス R は診断値として残すが、
   収益モデルや売買閾値の昇格判定には使わない。
3. `DIRECTION_THRESHOLD=0.15` は直ちに自動変更しない。未使用期間で評価した
   challenger を shadow 登録し、人間が承認したものだけを通知判断へ適用する。
4. リスク量は恒久的に学習対象外とする。このシステムは分析・Discord 通知専用であり、
   `risk_pct` やポジション数量をラベル、特徴量、最適化目的へ入れない。

最初の実装単位は「純 R の会計契約とログ来歴」である。会計契約が固まる前に
PR #46 の回帰モデルや閾値最適化を取り込まない。

## 2. 用語と責務

| 値 | 意味 | 用途 |
|---|---|---|
| `direction_outcome` | 主ホライズン後に方向が合ったか | 説明、Brier、既存挙動の回帰監視 |
| `move_atr` | 固定ホライズンの方向付き値幅 ÷ ATR | 診断、レジーム比較 |
| `gross_realized_r` | コストを含まない反実仮想の R | コスト寄与の分解 |
| `execution_cost_r` | executable quote とコストモデルが生んだグロスとの差 | コスト監査 |
| `realized_net_r` | 約定可能価格で再現した paper 取引の純 R | 収益モデル、閾値候補、昇格判定の唯一の主ラベル |
| `broker_realized_net_r` | 将来、実注文と fill が存在する場合の口座実績 | paper ラベルとは別系列。現状は常に欠損 |

`realized_net_r` の「realized」は、実口座損益ではなく、判断後の市場経路で確定した
paper outcome を意味する。混同防止のため全レコードに
`label_provenance="paper_quote_model"` を保存する。

## 3. 純 R の会計契約

### 3.1 必須入力

判断時点で次を point-in-time に保存する。

- `entry_mid`, `entry_bid`, `entry_ask`, `quote_observed_at`
- `stop`, `target1`, `target2`, `planned_risk_distance`
- `spread_source`, `quote_source`, `source_record_id`
- スリッページと手数料の値、単位、モデル ID、モデル版
- `direction`, `symbol`, `timeframe`, `horizon_hours`

将来経路には、完了済みの bid/ask OHLC、`bar_start`、`bar_end`、取得元、品質フラグを使う。
判断前に開始した足、形成中足、方向観測専用行は TP/SL と純 R の経路に入れない。

### 3.2 算式

long の entry は ask、exit は bid、short の entry は bid、exit は ask とする。

```text
signed_price_pnl =
  long:  exit_bid - entry_ask
  short: entry_bid - exit_ask

quote_realized_r = signed_price_pnl / planned_risk_distance

realized_net_r = quote_realized_r
                 - slippage_r
                 - commission_r

execution_cost_r = gross_realized_r - realized_net_r
```

`gross_realized_r` は同じ entry/exit 時刻の mid 同士から計算する。TP 到達なら常に
`+1R` のような固定 payoff を使うと、bid が TP に達した時点の mid との差が失われ、
`execution_cost_r` と同じ取引を比較できなくなるためである。既存の固定 payoff は
`planned_payoff_r` という診断値に分離する。

bid/ask 自体が spread を含むため、`quote_realized_r` から round-trip spread をもう一度
引いてはならない。spread を別控除するのは、mid-only 経路を明示的に使う診断計算だけとする。

TP/SL の同一足先着が不明な場合は現行どおり SL 優先とし、ギャップ時は stop 値ではなく
最初に約定可能な不利な価格を使う。これにより `-1R` より悪い純 R も正しく表現できる。

### 3.3 欠損と品質

次のどれかに該当する行は `realized_net_r=None` とし、収益学習へ入れない。

- 判断時の executable bid/ask が無い
- `planned_risk_distance` が無い、非有限、または 0 以下
- 将来の executable bid/ask 経路が無い
- コストモデルの ID または単位が無い
- point-in-time 来歴、データ品質、ラベルスキーマが検証不能
- 合成データを実績として混入している

推定スリッページは使用できるが、値を「実測」と偽らない。`cost_status` は
`quote_measured_modelled_execution`、`broker_measured`、`missing` のいずれかにする。
モデル ID が変わった場合は旧ラベルを黙って上書きせず、新しい `label_version` で再採点する。

### 3.4 一意な算出場所

純 R の算出責務は `fx_intel.trade_outcome` に一元化する。

- `learning.evaluate_history()` は方向的中と `move_atr` の診断だけを作る。
- `ml.py` は `move_atr - execution_cost_r` を再計算しない。
- ダッシュボード、promotion、閾値検証は保存済みの同じ `realized_net_r` を読む。
- 同じ `decision_id + label_version` に異なる値が出た場合は監査エラーにする。

推奨する保存スキーマは次のとおり。

```json
{
  "label_version": "net-r-v1",
  "label_provenance": "paper_quote_model",
  "decision_id": "...",
  "gross_realized_r": 0.82,
  "quote_realized_r": 0.79,
  "slippage_r": 0.02,
  "commission_r": 0.00,
  "execution_cost_r": 0.05,
  "realized_net_r": 0.77,
  "cost_status": "quote_measured_modelled_execution",
  "cost_model_id": "oanda-paper-v1",
  "path_quality": 0.96,
  "quality_flags": []
}
```

## 4. 売買閾値の学習

### 4.1 学習対象にするか

学習対象にする。ただしオンラインで連続更新するパラメータではなく、
`0.15` を champion とした承認制の abstention policy として扱う。

初期段階では候補は `0.15` 以上、すなわち判断を厳しくする方向だけを許可する。
閾値を下げる変更は判断数と潜在的なリスクを増やすため、別の明示承認とより長い
未使用期間を要求する。

### 4.2 評価単位

初期版は全ペア共通の対称閾値 1 個だけを評価する。

```text
composite >= +threshold  -> long candidate
composite <= -threshold  -> short candidate
otherwise                -> neutral
```

symbol、timeframe、long/short 別閾値はサンプルを細分化するので初期版では禁止する。
十分な OOS 実効サンプルが蓄積した後だけ、全体閾値へシュリンクする階層候補として追加する。

### 4.3 選択と検証

閾値候補の生成、選択、昇格判定を同じ期間で行わない。

1. walk-forward の train 区間で候補を生成する。
2. tune 区間で候補を 1 個に選ぶ。
3. embargo を挟んだ test 区間で champion `0.15` と比較する。
4. 最後の lockbox 区間は人間承認前の一度だけ開く。

重複する保有期間は独立サンプルとして数えず、既存の自己相関間引きを適用する。
主目的は test 区間の `realized_net_r` 平均ではなく、その片側信頼下限が 0 を超えること。
加えて、次をすべて満たす必要がある。

- label coverage と実効サンプル数が事前設定値以上
- champion より純 R、累積純 R、最大 DD のいずれも許容範囲内
- 候補探索数を考慮した DSR が事前設定値以上
- コスト 0.5x / 1.0x / 1.5x / 2.0x の stress で符号が不安定にならない
- 単一ペア、単一レジーム、低品質経路だけに利益が集中していない

候補数、閾値範囲、必要サンプル、DSR、DD の値はコードへ散在させず設定ファイルへ置き、
変更自体を新しい実験として記録する。

### 4.4 状態機械

```text
candidate -> shadow -> ready_for_review -> approved -> active
                  \-> rejected
active -> auto_paused -> approved（再承認時のみ）
```

- `shadow`: 記録だけ。通知判断を変えない。
- `ready_for_review`: OOS ゲート合格。自動適用しない。
- `approved`: 人間が対象、期間、dataset hash、差分を確認済み。
- `active`: `briefing.build_trade_plan()` へ読み取り専用で注入する。
- `auto_paused`: 純 R の信頼下限が非正、品質不足、ドリフト、スキーマ不一致で即時停止。

読み込み失敗、期限切れ、未承認、対象外セルでは常に `0.15` へ戻る。承認済みポリシーが
安全ゲート、イベント見送り、品質見送りを上書きすることは禁止する。

保存するポリシーには最低限、次を含める。

```json
{
  "schema": 1,
  "policy_id": "...",
  "stage": "shadow",
  "threshold": 0.2,
  "fallback_threshold": 0.15,
  "scope": "overall",
  "label_version": "net-r-v1",
  "cost_model_id": "oanda-paper-v1",
  "dataset_hash": "...",
  "train_end": "...",
  "test_end": "...",
  "expires_at": "...",
  "effective_samples": 0,
  "oos_mean_net_r": null,
  "oos_net_r_lcb": null,
  "dsr": null,
  "max_drawdown_r": null,
  "approved_by": null,
  "approved_at": null
}
```

## 5. リスク量を対象外にする境界

`realized_net_r` は 1 トレードの R 倍率であり、口座残高や数量から独立させる。
次の経路を作らない。

- 純 R や確信度から `risk_pct` を学習する
- 閾値候補が position units を変更する
- promotion が口座残高、レバレッジ、日次損失枠を更新する
- Discord 分析結果から broker/executor へ注文を送る

既存の `risk_pct` は説明用の固定値として残せるが、D の候補レジストリには含めない。
将来発注を再導入する場合は、D とは別のリスク設計・paper 運用・人間承認を必須とする。

## 6. 実装境界

### Phase D1: ラベル契約

- `TradePlan` と判断ログへ entry bid/ask、quote 時刻、コストモデル来歴を追加
- `TradeOutcome` へ `gross_realized_r`、`quote_realized_r`、`realized_net_r`、内訳を追加
- `trade_outcome.py` に一意な算出関数を実装
- 既存ログは backfill せず `realized_net_r=None` とする

### Phase D2: 読み取り系

- 期待値集計とダッシュボードを純 R 対応
- label coverage、欠損理由、cost model、label version を同時表示
- 方向的中率と純 R を別系列で表示し、どちらか一方で成功を主張しない

### Phase D3: 収益モデル

- ML の収益 dataset は canonical `TradeOutcome` から構築
- 期待純 R 回帰と分位点は shadow のみ
- 分類モデルの `usable` と収益モデルの `return_usable` を分離
- 収益ヘッドは D4 の閾値ポリシーに直接自動接続しない

### Phase D4: 閾値 challenger

- 閾値評価器、実験 manifest、候補レジストリを追加
- `build_trade_plan(direction_threshold=...)` として依存注入
- 承認済み active 候補だけを `fx_briefing.py` がロード
- 劣化時の auto-pause と `0.15` fallback を運用監視へ追加

## 7. 必須テスト

1. long は ask entry / bid exit、short は bid entry / ask exit を使う。
2. bid/ask 使用時に spread を二重控除しない。
3. TP、SL、terminal、gap、同一足 ambiguous の各純 R が会計恒等式を満たす。
4. コストまたは来歴欠損時は `realized_net_r=None` になり、学習件数へ入らない。
5. `learning.py`、ML、dashboard、promotion が同じ decision ID の同じ純 R を読む。
6. label version または cost model が混在する dataset を拒否する。
7. threshold の train/tune/test/lockbox が時系列順かつ embargo 付きである。
8. shadow 候補は方向を変えず、未承認・破損・期限切れ時は `0.15` へ戻る。
9. active 候補でも休場、イベント、品質、期待値ガードの拒否権を上書きしない。
10. D の全経路から position size、broker 注文、live 有効化へ到達できない。

## 8. PR #46 の扱い

PR #46 は設計材料として利用するが、そのまま main へマージしない。

- base の PR #26 は main へ未マージのまま closed であり、#46 はその上の stacked PR。
- main との差分には D 以外の大規模な追加・削除が含まれる。
- `trade_outcome.py` は TP/SL の `realized_r - execution_cost_r`、
  `learning.py` は固定ホライズンの `move_atr - execution_cost_r` を
  それぞれ `realized_net_r` としており、主ラベルが一意でない。
- PR 作成後の現行 worktree では bid/ask executable path の統合作業が進んでいるため、
  単純なコスト再控除は spread の二重計上を起こしうる。

したがって、D1 を現行 main 上で先に実装し、#46 からは回帰器、分位点、可視化の
独立した部分だけを小さい PR に移植する。マージ順は D1 -> D2 -> D3 -> D4 とする。

## 9. 完了条件

D 完了は「モデルが学習できる」ではなく、次を満たした状態とする。

- 純 R の算式、来歴、欠損理由が decision 単位で監査できる
- すべての収益系 consumer が同じ canonical label を使う
- threshold challenger が OOS、DSR、stress、承認、rollback を通る
- `0.15` fallback と全安全ゲートの拒否権がテストで固定される
- リスク量と発注が学習・昇格経路から構造的に切り離されている
