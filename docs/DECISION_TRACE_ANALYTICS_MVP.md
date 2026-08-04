# Decision Trace Analytics MVP

判断イベント(decision event)の `gate_trace` / `blocked_by` を**読み取り専用**で集計し、
どのゲートで判断が止まっているかを層別(pair × timeframe × regime)で可視化する最小実装。

**コードとこの文書が食い違う場合はコードが正。**

- 実装: `fx_intel/decision_trace.py`(純関数・書き込みAPIなし)
- CLI: `tools/decision_trace_report.py`(stdout出力のみ)
- テスト: `tests/test_decision_trace.py`

## 保証範囲(このMVPが行うこと)

| 指標 | 内容 |
|---|---|
| 判断ファネル | `total_decisions` / `blocked_at[gate]`(first blockerのみ計上)/ `passed_all_gates` / `indeterminate` |
| veto支配率 | `dominance[g] = blocked_at[g] / (total - indeterminate)` |
| 不正な状態遷移 | 相互排他ゲートの同時出現 |
| 欠損遷移 | `final_action=="neutral"` かつ `blocked_by==[]` |
| 層別 | pair / timeframe / regime / セル(pair×timeframe×regime)ごとに全指標 |
| 遅延 | 判断単位の分布のみ。**gate単位は測定不能** |

### 件数恒等式(fail-closed)

    passed_all_gates + Σ blocked_at + indeterminate == total_decisions

不一致の場合は `FunnelReconciliationError` を送出して**FAILする**。黙って続行しない。
分類不能な行(未知の `parse_status`)を "通過" に落とすと恒等式が**偶然成立して誤集計を隠す**ため、
これも同じく送出する。

### 壊れた入力の扱い

壊れた行・欠損フィールド・非aware時刻を**黙って除外しない**。
`parse_status="indeterminate"` として件数と理由(`parse_reason_counts`)を必ず残す。

- naive時刻を勝手にUTCとみなさない(PIT整合の保護)。理由 `naive_ts` を残す。
- `regime` 欠損は `"unknown"`。値を捏造しない。

## 依拠する構造的事実

`fx_intel/timeframe.py` の実コードを読んで確認した事実に依拠している。

1. **`gate_trace` は `status="blocked"` のみ記録される**(timeframe.py:625-638)。
   pass事象は存在しないため、「通過したゲート」を推論で**捏造しない**。

2. **liquidity のトレースだけ形が違う**(input_context.py:480-498)。
   `status="observed"` / `would_block` / `applied: False` を持つ観測専用行であり、
   ブロックではない。indeterminate でもなく `"observed"` として別分類する。

3. **`blocked_by` は固定評価順で append される**ので index 0 が真の first blocker:

       operational_data_stale → market_closed → event_window
       → missing_technical / low_data_quality → below_production_threshold
       → expectancy_guard → missing_atr

4. **相互排他**(timeframe.py:435-462 の if/elif 分岐):
   `operational_data_stale` / `market_closed` / `event_window` /
   `missing_technical` / `low_data_quality` / `below_production_threshold`。
   同時出現は不正遷移。

5. **独立**(elif連鎖の外の単独 if):
   `expectancy_guard`(timeframe.py:487)と `missing_atr`(timeframe.py:569)。
   他ゲートとの共起は**合法**。ここを不正扱いすると偽陽性を量産するため、
   回帰防止テストで固定している。

## 未実装事項(このMVPが行わないこと)

意図的に範囲外とした項目。**「プロセスマイニング導入済み」とは言えない。**

- **pass事象の記録なし** — `gate_trace` は blocked のみ。各ゲートの通過率は算出できない。
  現状の指標は「どこで止まったか」であって「各ゲートの通過確率」ではない。
- **gate単位の遅延は測定不能** — ゲートごとのtimestampが存在しない。
  `per_gate.measurable=false` / `reason="gate_level_timestamps_absent"` を返し、
  推定値を**捏造しない**。判断単位の分布のみ算出する。
- **`policy_vetoed_by` 未対応** — このフィールドは main に存在せず、未コミットの作業ツリーに
  のみ存在する。入力として使用せず、他ブランチから復元もしない。
- **真のプロセス発見は未実装** — アルファアルゴリズム等によるプロセスモデル導出、
  適合性検査(conformance checking)、変種分析(variant analysis)はいずれも行わない。
  本実装は固定されたゲート順序を前提とした集計にとどまる。
- **実データでの有効性は未実証** — 合成fixtureでの動作確認のみ。
  実機ログに対する有効性・閾値の妥当性は評価していない。
- **異常判定の閾値を持たない** — `dominance` を算出するだけで、
  「いくつ以上が異常か」の基準は運用側の判断に委ねる。

## 使い方

```bash
python tools/decision_trace_report.py --input logs/decisions.jsonl
python tools/decision_trace_report.py --input logs/decisions.jsonl --format json
cat logs/decisions.jsonl | python tools/decision_trace_report.py --input -
```

終了コード: `0`=正常 / `2`=入力が見つからない / `3`=ファネル恒等式の破れ。

## 読み取り専用であることの担保

- モジュールは書き込みAPI(`open` / `write` / `dump` / `execute` / `commit` 等)を含まない。
  `tests/test_decision_trace.py` が**AST検査**で固定している。
- `sqlite3` / `shutil` を import しない(同テストで固定)。
- 全公開関数の実行後に入力ファイルの内容と mtime が不変であることをテストで検証している。
- CLI は stdout にのみ出力し、書き込みモードの `open` を持たない。
- **既存の判断結果を一切変更しない。** 解析は事後の読み取りであり、
  判断・採点・学習の経路には影響しない。
