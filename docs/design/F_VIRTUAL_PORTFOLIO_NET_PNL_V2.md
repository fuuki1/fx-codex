# 設計F: 仮想ポートフォリオ・純損益・30日分布 V2

設計日: 2026-07-30
対象: `fx_intel/virtual_portfolio.py`、`fx_intel/virtual_portfolio_replay.py`、
`tools/virtual_portfolio*.py`、`tools/ai_learning_dashboard/`
状態: M0/M2 shadowとP2 hardeningをMac miniへ配備済み。V2 risk強制は未実施。

## 0. 実装進捗

2026-07-30にM0の加算型実装を追加した。

- `fx_intel/virtual_portfolio_v2.py`
  - previous hashを含む正準event chain
  - seal後にpostingを増やせないbalanced JPY journal
  - V1 cash ledgerの明示的な追記移行
  - position別base/low/high退出費用reserve
  - 同一cutoffのportfolio liquidation valuation
  - cash high-water markを維持したliquidation drawdown
  - chain、journal、legacy projection、valuation恒等式の再監査
- `fx_intel/virtual_portfolio.py`
  - 初期資本と終了損益を既存投影とV2へ同一transactionでdual-write
  - V2 accounting未移行なら新しいcash ledger writeをfail-closed
  - 同期清算評価のwriter APIとread-only snapshot
- `tests/test_virtual_portfolio_v2.py`
  - hash chain、balanced journal、費用帰属、同期評価、不完全評価、
    high-water mark、append-only、明示移行を検証
- `tools/virtual_portfolio_v2_migrate.py`
  - `status` / `audit`はread-only
  - `migrate`はexact confirmation必須で、V1行を変更せず不足eventだけ追記
- `fx_intel/virtual_portfolio_valuation.py`
  - persisted markだけから同一cutoffの清算評価を生成
  - 価格・換算source event、鮮度、position間skewをfail-closed検査
  - modeled exit reserveのbase/low/highとportfolio heatを記録
  - liquidation low、drawdown、期間lossを使うV2 gateをshadow-only評価
- `fx_intel/virtual_portfolio_replay.py`
  - 各replay cycleの末尾とidle cycleで同期清算評価を追記
- `reports/virtual_portfolio_v2_rehearsal_20260730.md`
  - V1原本を変更しないSQLite backup移行とcash valuationの証跡

Mac mini DBのM0移行は保全copyでのリハーサル後に加算型で実施した。V1の元writer観測時刻は
保存されていないため、移行時はquote ingestion timeを下限proxyとして記録し、当時の正確な
recorded timeを復元できたとは扱わない。M1の30自然cycle parity、risk gate V2の強制切替、
dashboard主表示切替、継続的な別権限checkpoint custody、30日分布は未完了である。

2026-07-30のP2 hardeningでは、期間baselineを境界鮮度・観測時刻・recorded_at・
source age・valuation model連続性でfail-closed選択する。portfolio inceptionを含む期間は
最初のmarkをbaselineにせず、初期資本と当該期間のliquidation-low HWMの大きい方を使う。
正の端数秒は全て切り上げる。
V2監査は各valuationが固定した`state_event_head_seq`までのjournalを再生してcashと
realized P&LをPIT再構築し、費用component、position reserve、mid/executable/liquidation
equity、HWM/drawdown、source age/skewを独立再計算する。仕訳はaccount一意かつ正確な順序・
行数で照合し、valuation detailとcanonical payloadを一致させ、各positionを元の
`simulated_position_marks`、quote/conversion evidence、mark hashへ結び付ける。後から到着した
過去effective-timeの決済を、それ以前のvaluationへ混入しない。

`virtual-portfolio-v2-checkpoint-v1`はtransactionally consistentなDB backupを監査し、
event head/countとsnapshot hashをcreate-onlyで固定する。検証は現在chainにそのprefixが
残っていること、checkpointの自己hash、別経路で渡したraw file SHA-256との一致を確認する。
別ホストcopyは`different_host_copy_observed`とだけ記録し、
`independent_custody_verified=false`を維持する。署名・trusted timestamp・組織的独立custodyを
備えたとは主張しない。

## 1. 結論

高度化の順序は次で固定する。

1. 現金、実現純損益、未実現損益、清算評価額を別の正準値にする。
2. 全コスト、通貨換算、丸め、データ品質を恒等式で照合する。
3. ポジション単位の非同期markではなく、同一cutoffのポートフォリオ評価を追記する。
4. 安全ゲートを実現損益だけでなく、保守的清算評価額と残存stopリスクへ接続する。
5. 追記専用イベントを連鎖hashし、訂正は反転・置換イベントとして残す。
6. 十分な成熟実績ができてから、30日残高を一点ではなく分布で予測する。

現在のDBは追記専用trigger、実行可能Bid/Ask、円換算、費用分解、cash ledger照合を
備えている。V2はこれを破棄せず、正準イベントと再構築可能なprojectionを追加する。
ブローカー、注文、取消、決済、口座リスク変更APIは追加しない。

## 2. 2026-07-30の実測

Mac mini正規read APIとSQLiteを読み取り専用で確認した。

| 項目 | 実測 |
|---|---:|
| cash | 1,000,000円 |
| executable equity | 996,290円 |
| 未実現損益 | -3,710円 |
| open / close | 2 / 0 |
| position marks | 7 |
| learning eligible | 0 / 150 |
| canonical reconciliation | 差額0円、pass |
| SQLite integrity | `ok` |
| schema | v3 |
| performance claim | `evaluation_unavailable` |

稼働writerはlaunchdのone-shot consumerで、loopback query-only readerは別プロセスである。
現時点で利益能力や30日分布を較正する終了取引はない。V2の予測欄は
`unavailable_insufficient_outcomes`を正とし、方針上限によるstress bandと統計的予測を
混同しない。

## 3. 現行設計の強み

- 初期資本と終了取引純損益を`ledger_events`で照合できる。
- longはask開始/bid終了、shortはbid開始/ask終了を使う。
- spreadをmid対比で一度だけ控除し、slippage、commission、financing、
  conversionを分けている。
- quote、model、data、policy、recordのhashとaware UTCを保存する。
- 判断を先に固定し、後から届いた遅延履歴tickで経路を再生する。
- UPDATE/DELETE trigger、single writer、SQLite query-only readerがある。
- データ不足、費用不足、PIT不成立を0円や成功にしない。
- 取引なし、反対方向、mid fill、1Rの反実仮想を因果主張なしで保存する。

この土台は維持する。

## 4. 設計ギャップ

### D0: 実績不足

終了取引0件では、期待値、分散、tail、費用誤差、30日予測のcoverageを推定できない。
現在の残高予想は方針stressであって、較正済み予測ではない。

### D1: equityが全退出費用込みではない

現行の未実現損益は実行可能Bid/Askまでは反映するが、将来のslippage、commission、
経過financing、conversion cost reserveを全て反映した清算価値ではない。
そのためclose時に未計上費用分の段差が発生し得る。

### D1: risk gateがcash中心

日次・週次・月次の損失判定は終了済み取引の実現損益を中心に計算する。
open loss、退出費用、gap stress、残存stop riskを同じ安全ゲートへ集約していない。

### D1: equity high-water markがない

現行equity drawdownはcash peakと現在equityの差である。open profitでequityがcash peakを
上回った後に下落しても、そのequity peakからのdrawdownを正確に測れない。

### D1: markの同時点性が弱い

各positionの最新markを個別に選ぶため、同じsnapshot内で価格時刻、利用可能時刻、
換算時刻がずれる可能性がある。ポートフォリオ合計は一つのvaluation cutoffで固定する。

### D1: 非JPY pairの換算寄与が粗い

最終円損益は終了時換算率で計算できるが、現在は価格損益、quote通貨のspread、
USDJPY等の換算変動、換算Bid/Askを十分に分離していない。mid換算と固定conversion costは、
実行方向付きの通貨変換とは別物である。

### D2: 追記専用は改竄耐性と同義ではない

SQLite triggerは通常動作の誤更新を防ぐが、同一OS権限の管理者によるDB置換まで証明しない。
全tableを横断する順序、previous hash、外部保全済みcheckpointがない。

### D2: 訂正契約がない

誤quote、遅延訂正、誤った費用仮定が判明したとき、既存行を変更せずにどの値を正とするかを
示す反転・置換イベントが必要である。

### D2: 収益率と寄与率が不足

円損益は確認できるが、同一cutoffのperiod return、capital-weighted contribution、
cost drag、exposure-adjusted result、benchmarkとの差が正準化されていない。

### D2: 30日予測の正規契約がない

既存の単純trade bootstrapは独立同分布を暗黙に置く。仮想口座では、相関した同時保有、
no-trade日、日週月stop、risk sizing、費用、regime、gapを状態付きで再生する必要がある。

## 5. V2の状態モデル

画面とAPIは次の値を明確に分ける。

| 値 | 定義 | 主用途 |
|---|---|---|
| `cash_jpy` | 初期資本 + 終了済み純損益 | 台帳照合 |
| `realized_net_pnl_jpy` | 終了済み取引の全費用後損益 | 実現実績 |
| `mid_equity_jpy` | cash + 全openのmid評価損益 | 市場方向寄与 |
| `executable_equity_jpy` | cash + direction別Bid/Ask評価損益 | 現在の実行可能価格proxy |
| `liquidation_equity_base_jpy` | executable equity - base exit cost reserve | 主表示 |
| `liquidation_equity_low_jpy` | 保守的費用・gap仮定の清算下限 | risk gate |
| `liquidation_equity_high_jpy` | 低費用仮定の参考値 | 不確実性表示 |
| `realized_drawdown_jpy` | cash high-water markからの低下 | 実現DD |
| `liquidation_drawdown_jpy` | liquidation high-water markからの低下 | 主risk DD |

`equity_jpy`という曖昧な単独名はV2 APIでは使わない。互換APIでは
`equity_jpy=liquidation_equity_base_jpy`へ移行するまでversionと意味を返す。

## 6. 純損益の二層恒等式

### 6.1 quote通貨の経済事実

```text
gross_market_pnl_quote
= direction_sign × units × (exit_mid - entry_mid)

executable_pnl_quote
= direction_sign × units × (exit_executable - entry_executable)

spread_quote_cost_quote
= gross_market_pnl_quote - executable_pnl_quote
```

価格と数量はbinary floatだけを正としない。sourceのdecimal文字列、scale、
canonical decimal、表示用floatを分ける。

### 6.2 JPY換算と全費用

quote通貨損益が正ならquote通貨を売るBid、負なら不足quote通貨を買うAskを使う。
各換算quoteはevent/available/ingested time、source、revision、raw hashを持つ。

```text
gross_market_pnl_jpy_at_entry_fx
= gross_market_pnl_quote × entry_conversion_mid

fx_translation_pnl_jpy
= gross_market_pnl_quote × (exit_conversion_mid - entry_conversion_mid)

spread_quote_cost_jpy
= spread_quote_cost_quote × exit_conversion_mid

conversion_spread_cost_jpy
= executable_pnl_quote × exit_conversion_mid
 - convert(executable_pnl_quote, direction_aware_exit_bid_ask)

net_pnl_jpy
= gross_market_pnl_jpy_at_entry_fx
 + fx_translation_pnl_jpy
 - spread_quote_cost_jpy
 - conversion_spread_cost_jpy
 - slippage_cost_jpy
 - commission_jpy
 - financing_jpy
 - other_pre_registered_cost_jpy
 - rounding_residual_jpy
```

各費用は次を必須にする。

- `evidence_kind`: `observed` / `contracted` / `modeled` / `unavailable`
- `source_currency`と元額
- JPY換算quote IDと換算side
- model ID、version、policy hash
- base/low/high見積り
- `recognized_at`と対象期間

`unavailable`を0円として合計しない。主表示の清算評価額は必要費用が欠けたら
`valuation_unavailable`とする。

## 7. 全退出費用込みposition valuation

各open positionについて同じcutoffで次を追記する。

```json
{
  "contract": "virtual-position-valuation-v2",
  "trade_id": "sim-...",
  "valuation_cutoff": "aware UTC",
  "observed_at": "aware UTC",
  "mid_unrealized_pnl_jpy": 0,
  "executable_unrealized_pnl_jpy": 0,
  "accrued_financing_jpy": 0,
  "exit_slippage_reserve_jpy": 0,
  "exit_commission_reserve_jpy": 0,
  "conversion_reserve_jpy": 0,
  "liquidation_net_pnl_base_jpy": 0,
  "liquidation_net_pnl_low_jpy": 0,
  "liquidation_net_pnl_high_jpy": 0,
  "quote_ids": [],
  "valuation_model_sha256": "...",
  "quality": {
    "complete": true,
    "max_source_age_seconds": 0,
    "cross_position_skew_seconds": 0,
    "warnings": []
  }
}
```

close時は直前の清算評価から実現cashへのbridgeを照合する。

```text
close_bridge
= realized_net_pnl
 - latest_liquidation_net_pnl
```

bridgeは最終price move、quote skew、費用見積り誤差、丸めに分解する。理由不明の段差は
reconciliation failureである。

## 8. 同一cutoffのportfolio valuation

5分ごとのconsumerは、position marksとは別に一つの
`virtual-portfolio-valuation-v2`を追記する。

- `valuation_cutoff`: 使用する市場event timeの上限
- `as_of`: システムが評価を生成できた時刻
- `event_head_seq/hash`: 評価対象の正準event head
- cash、realized PnL、3種equity、cost reserve
- cash/equity high-water marksとdrawdown
- pair notional、gross leverage、currency exposure vector
- stop risk、gap stress、portfolio heat
- mark coverage、最大鮮度、position間skew
- policy、valuation model、source setのhash

全positionを同じcutoffで評価できない場合、直前値を無警告で混ぜない。
`incomplete_synchronized_valuation`として主equityをnullにする。

## 9. 通貨factor exposure

pair notionalの単純合計だけでなく、基軸・決済通貨ごとのsigned exposureを作る。

```text
USDJPY long  = +USD / -JPY
EURUSD short = -EUR / +USD
```

V2は少なくとも次を返す。

- currency別gross/net exposure JPY換算
- USD共通factor集中度
- pair別planned stop loss
- 同時stop stress
- 1.5倍/2倍/3倍spread・slippage stress
- conversion quote欠損時のunpriced exposure

相殺は完全なhedgeとみなさない。満期、数量、source、conversion、timeframeが異なるため、
grossとnetを併記する。

## 10. 安全ゲート V2

新規のオフライン仮想建て可否は、次の全てが利用可能かつ上限内の場合だけtrueにする。

1. canonical event chainとdouble-entry照合
2. synchronized liquidation valuation
3. current liquidation drawdown
4. 日次・週次・月次のliquidation return
5. 全openのstop risk + gap/cost buffer
6. currency concentrationとgross leverage
7. quote freshness、spread anomaly、conversion coverage
8. 現行の同時保有、連敗、安全学習gate

```text
risk_equity_jpy = liquidation_equity_low_jpy

portfolio_heat_jpy
= Σ(planned_stop_loss_jpy + gap_buffer_jpy + exit_cost_reserve_jpy)

daily_loss_jpy
= start_of_risk_day_liquidation_equity_jpy - risk_equity_jpy
```

V2初版は既存方針との互換性のため`risk_day_timezone=Asia/Tokyo`、
`risk_day_cutoff=00:00`、週初め月曜、月初1日をpolicyへ明記する。市場session、
reporting day、risk dayを同じ「日」という名前で暗黙に混ぜない。境界を変える場合は
新policy versionとして将来時点から適用し、過去損益を再区分しない。

cashが無傷でもopen lossで上限へ到達した場合は見送る。confidence、committee、
日次KPIはこのvetoを上書きできない。

## 11. 正準event logとdouble-entry

### 11.1 `portfolio_event_log`

```text
event_seq INTEGER PRIMARY KEY
event_id TEXT UNIQUE
event_type TEXT
effective_time_ns INTEGER
observed_at_ns INTEGER
recorded_at_ns INTEGER
correlation_id TEXT
causation_id TEXT
schema_version INTEGER
payload_json TEXT
payload_sha256 TEXT
prev_event_sha256 TEXT
event_sha256 TEXT
policy_sha256 TEXT
writer_id TEXT
```

`event_sha256`はcanonical header、payload hash、previous hashを含める。
table別の既存行はprojectionとして残し、V2の正本はevent順序から再構築できるようにする。

### 11.2 double-entry journal

`journal_transactions`と`journal_postings`を追加し、一つのtransactionを同じSQLite
transaction内で追記する。JPY postingのsigned sumは必ず0円にする。

初期account例:

- `asset.cash_jpy`
- `equity.initial_capital`
- `pnl.market_move`
- `pnl.fx_translation`
- `expense.spread`
- `expense.conversion_spread`
- `expense.slippage`
- `expense.commission`
- `expense.financing`
- `expense.rounding`

これは会計基準準拠の主張ではなく、研究台帳の機械的照合契約である。

### 11.3 訂正

UPDATE/DELETEは引き続き禁止する。訂正は次の順で行う。

1. `record_disputed`
2. 元transactionを打ち消す`journal_reversal`
3. 正しい根拠を持つ`replacement_record`
4. `supersedes_event_id`とreason/evidence hash

as-observed viewとlatest-corrected research viewを別にする。予測・shadow評価の正本は
当時利用可能だったas-observed viewであり、後日訂正値へ黙って置換しない。

### 11.4 checkpoint

日次でevent tree root、event count、DB SHA-256、schema、last event hashをcreate-only成果物に
保存する。RFC 9162のMerkle consistency proofは設計参照として使えるが、SQLiteを
Certificate Transparency logと呼ばない。信頼できる別権限へcheckpointを保全して初めて、
DB置換に対するtamper evidenceが強くなる。ローカルだけなら限界を明記する。

## 12. return contract

固定100万円で外部入出金を許可しない現行方針では、期間収益率は次でよい。

```text
period_return
= ending_liquidation_equity / beginning_liquidation_equity - 1
```

日次returnは同じrisk-day cutoffの同期済み清算評価だけを使い、幾何連鎖する。
将来、資本を変更したい場合はdeposit/withdrawal機能を足さず、新しいportfolio IDと
初期化eventを作る。このリポジトリで口座資本を外部から変更する機能は作らない。

表示する収益指標:

- since-inception / day / week / month net return
- realized-only returnとliquidation return
- net expectancy R + block-bootstrap CI
- cost drag in JPY、R、notional bps
- max drawdown、drawdown duration、recovery
- expected shortfall、loss streak、tail contribution
- exposure、turnover、abstention coverage
- pair/timeframe/direction/regime/session contribution

win rateとdaily KPIは二次診断に置く。

## 13. 30日残高分布

### 13.1 出力

```json
{
  "contract": "virtual-portfolio-forecast-v1",
  "as_of": "aware UTC",
  "horizon": "30d",
  "status": "unavailable_insufficient_outcomes",
  "state_sha256": "...",
  "model_sha256": "...",
  "policy_sha256": "...",
  "sample": {
    "eligible_trades": 0,
    "market_days": 0,
    "regimes": 0
  },
  "distribution": {
    "p05_liquidation_equity_jpy": null,
    "p50_liquidation_equity_jpy": null,
    "p95_liquidation_equity_jpy": null,
    "probability_of_loss": null,
    "probability_monthly_stop": null,
    "probability_hard_drawdown": null,
    "expected_shortfall_jpy": null
  },
  "stress_scenarios": [],
  "calibration": {
    "rolling_origin_windows": 0,
    "interval_coverage": null,
    "crps": null,
    "baseline_improvement": null
  },
  "limitations": []
}
```

### 13.2 状態付きsimulation

tradeを独立にshuffleしない。市場日単位または重複しないposition cluster単位をblock化し、
次をevent-drivenで再生する。

- 同時保有と通貨相関
- no-trade / abstain日
- risk sizingの複利変化
- 日次・週次・月次stop
- hard drawdown
- observed/model cost分布
- gap-through stop
- quote欠損とevaluation unavailable
- regime/pair/session構成

候補はcircular/stationary block bootstrapと、事前登録したregime mixtureである。
block長、regime定義、trial、seedを固定し、都合のよい30日だけ選ばない。

### 13.3 段階

| 段階 | 最低証拠 | 表示 |
|---|---|---|
| F0 | 終了取引なし | policy stressのみ |
| F1 diagnostic | PIT適格取引と複数market day | 分布を研究画面だけに表示 |
| F2 calibrated | 事前登録minimum、rolling-origin、coverage、baseline合格 | p05/p50/p95表示 |
| F3 governed | drift/cost/incident gate、独立review | 定例報告へ掲載 |

開始時のgovernance候補は150取引かつ60 market days以上とするが、これは性能を保証する
統計定数ではない。30日tailには不足し得るため、minimum track recordとcoverage結果で
より多くを要求する。pairまたはregime一つへの過度な集中も不合格にする。

### 13.4 検証

rolling-originで過去の各as-ofから30日分布を凍結し、後の実現清算equityと比較する。

- CRPSまたは事前登録したproper score
- p05–p95、p10–p90の実測coverage
- calibration slope/intercept
- probability of lossのBrier/log loss
- no-trade、zero-drift、単純historical block baseline
- 1.5倍/2倍/3倍cost、gap、source outage stress
- PIT、purge、embargo、test、one-time lockbox
- 全trial PBO/DSR/PSR、多重性、block uncertainty

point estimateだけ当たってもinterval coverageが崩れたら不合格にする。
F2前は確率を表示せず、scenarioに「統計的確率ではない」と付ける。

forecastは当面read-onlyの説明出力であり、position size、risk limit、判断thresholdを
自動変更しない。

## 14. dashboard

### 14.1 先頭カード

1. 全退出費用込み清算評価額
2. cash
3. 本日total net PnL（実現 + 未実現清算）
4. 累積実現純損益
5. open risk / portfolio heat
6. 30日分布status

各値に`observed`、`modeled`、`unavailable` badgeとvaluation cutoffを表示する。

### 14.2 chart

- cash
- mid equity
- executable equity
- liquidation base/low/high band
- cash drawdownとliquidation drawdownのunderwater chart
- risk stop発動時刻
- quote stale/incident区間

0件時に平坦な線を「安定実績」と誤認させず、`終了実績なし`を中央に表示する。

### 14.3 waterfall

```text
市場mid損益
+ FX換算変動
- quote spread
- conversion spread
- slippage
- commission
- financing
- rounding
= 純損益
```

期間、pair、timeframe、direction、regime、sessionで同じ恒等式を集約し、合計差額0円を
表示する。

### 14.4 30日欄

F0/F1では「予測」ではなく「方針stress」と表示する。F2以降だけp05/p50/p95、
loss probability、monthly stop probability、calibration結果を出す。
日次7万円を30倍した線は表示しない。

## 15. read API

新規候補:

```text
GET /v2/virtual-portfolio/snapshot
GET /v2/virtual-portfolio/equity-series?range=24h|7d|30d
GET /v2/virtual-portfolio/pnl-attribution?period=day|week|month|inception
GET /v2/virtual-portfolio/exposure
GET /v2/virtual-portfolio/forecast?horizon=30d
GET /v2/virtual-portfolio/integrity
```

- 任意path、SQL、自由な長期rangeを受け付けない。
- 一つのread transactionで`event_head_seq/hash`を固定する。
- 全応答に`snapshot_token`、`as_of`、`valuation_cutoff`、qualityを返す。
- ETagをevent head + query contract hashから作る。
- stale時は直前200を無警告で返さず、値をnullまたは明示的degradedにする。

## 16. 移行

既存v3 DBを直接書き換えない。

### M0: schemaとreplay

- V2 event/journal/valuation schemaを新規DBまたは新規table群に追加
- v3 DBのSHA-256を固定
- v3全行を順序付きV2 eventへreplay
- cash、close、cost、trade、decisionの件数とhashを照合

### M1: shadow dual-write

- 現行writerを正本のまま維持
- V2へ同じ入力をshadow write
- 30 natural cycles以上、event/cash/PnL parity
- V2不合格は現行処理を壊さず記録

### M2: synchronized valuation

- 5分cutoff valuationを有効化
- close bridge、cross-position skew、cost reserveを監視
- open→closeの連続性を自然終了で実証

### M3: read切替

- loopback V2 reader
- dashboardをdual-readし値を比較
- server-authoritative snapshot tokenを確認
- V1 fallbackを無警告で行わない

### M4: forecast research

- minimum evidenceまでF0
- rolling-origin artifactをcreate-only生成
- F1/F2 gateを独立review
- shadow表示だけを許可

各段階にrollbackを用意する。rollbackはV2 reader/writerを外すだけで、v3 DB、
V2 events、cycle artifactsを削除しない。

## 17. acceptance

### 会計

- 全journal transactionでposting sumが0円
- cash = initial capital + Σ realized net PnL
- realized net PnL = 全component恒等式
- liquidation equity = cash + Σ liquidation net unrealized
- close bridge差額を全componentへ説明可能
- day/week/month/inception集約差額0円

### PIT・valuation

- `event_time <= available_time <= observed_at <= snapshot_as_of`
- future quote、naive time、bid>ask、stale conversionを拒否
- 全positionが一つのcutoffと許容skew内
- forming-bar high/lowをfirst-touchに使わない
- correction前後のas-observed viewを再現可能

### risk

- open lossだけで日/週/月stopが発動するfixture
- equity HWMからのdrawdownを検出
- currency concentration、gap、2x/3x cost stress
- missing cost/quoteで`can_open=false`

### integrity

- event順序、previous hash、payload hashを全件検証
- projectionを空DBへdeterministic replay
- reversal/replacement後も元recordを保持
- checkpoint不一致をfail closed

### forecast

- 0件をflat profitable forecastにしない
- no-trade日と同時position clusterを保持
- seed/hash/window/trialを記録
- rolling-origin以外を正式coverageに使わない
- baseline未改善、coverage不良、cost stress不良でF2拒否

### UI/API

- cash、realized、executable、liquidationを混同しない
- 値ごとにcutoffとqualityを表示
- snapshot内のevent headが一致
- query allowlist、bounded rows、query-only接続
- broker/order/account mutation語彙・機能なし

## 18. 調査根拠と適用限界

- [GIPS Standards Handbook for Firms](https://www.gipsstandards.org/standards/gips-standards-for-firms/gips-standards-handbook-for-firms/)
  は、整合したvaluation period、外部cash flow時の評価、幾何連鎖、transaction cost控除を
  支持する。V2はGIPS準拠を主張せず、性能表示を誤解させないための計算規律だけを採用する。
- [GFXC TCA Data Template](https://www.globalfxc.org/the-importance-of-disclosures-and-transparency-in-the-fx-market/algo-tca-templates/)
  は、UTC時刻、通貨pair、direction、amount、arrival mid、fee込み/除外rate、
  reference Bid/Offerなどの標準化を支持する。遅延Dukascopy replayは実際のalgo execution
  ではない。
- [Basel market risk framework](https://www.bis.org/bcbs/publ/d457.htm)はExpected Shortfallを
  tail risk指標として採用し、P&L attribution/backtestingではstatic risk-theoretical P&Lと
  actual/hypothetical P&Lを区別する。V2は銀行資本規制を適用せず、tailとPnL viewを
  混ぜない設計原則だけを使う。
- [Federal Reserve SR 26-2](https://www.federalreserve.gov/frrs/guidance/supervisory-guidance-on-model-risk-management.htm)
  は、目的、概念妥当性、outcome analysis、ongoing monitoring、effective challenge、
  model inventoryを支持する。これは本リポジトリへの法的要件ではない。
- [Gneiting and Raftery (2007)](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf)
  は確率分布予測をproper scoring ruleで比較する根拠である。金融時系列の非定常性や
  小標本を解決するものではない。
- [RFC 9162](https://www.rfc-editor.org/rfc/rfc9162.html)はMerkle inclusion/consistency
  proofでappend-only性を検証する構造を定義する。V2のローカル台帳はCT logではなく、
  外部checkpoint custodyなしでは同等の保証を持たない。

## 19. 完了定義

この文書だけではV2は完成していない。完成には実装、migration replay、自然なopen→close、
十分なPIT適格outcome、forecast rolling-origin検証、cost stress、独立した敵対的review、
全check再実行が必要である。
