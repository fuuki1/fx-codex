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
| 欠損遷移 | `final_action=="neutral"` なのに blocked ゲートが無い判断 |
| observed(shadow) | `observed_gate_counts` / `would_block_counts`(ブロックとは別軸) |
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

## 実機ログでの検証(2026-08-04)

`/Users/fuuki/srv/fx-codex/logs/briefing_decisions.jsonl`(1.86GB / 34,107行)に対して
実行した結果。**本番ツリーは一切変更していない**(読み取りのみ)。

```
判断行 n=32,875   passed=14,450   indeterminate=0
封筒除外: decision_batch 620 / decision_cross_log_commit 620

  market_closed               9,979   dominance=0.3035
  expectancy_guard            2,896   dominance=0.0881
  event_window                2,718   dominance=0.0827
  below_production_threshold  1,478   dominance=0.0450
  operational_data_stale      1,346   dominance=0.0409
  missing_technical               8   dominance=0.0002

observed: liquidity 24,541 / event_window_policy 10,945 / usd_factor_coherence 308
invalid_transitions=0   missing_transitions=6,211
処理: 14.2秒 / peak RSS 35MB
```

### 実機で判明した契約(設計と食い違う点)

1. **`blocked_by` は実機で全行が空**(32,867行中0行が非空)。ブロック根拠は
   `gate_trace` にのみ存在する。`blocked_by` だけを見る実装は全判断を
   「通過」と誤認する。本実装は `gate_trace` を優先する。

2. **`gate_trace` に `pass` 事象は存在しない**。実測の status は
   `blocked`(291)と `observed`(1,385)の2値のみ。
   設計文書 §2.6 の `outcome: "pass"|"veto"|"skip"` は実データに存在しない。

3. **observed が blocked を上回る**。observed を落とすと判断工程の大半が不可視になる。

4. **判断ログには判断以外の封筒レコードが混在する**
   (`decision_batch` / `decision_cross_log_commit` 各620件)。母数に入れると
   dominance が実態より小さく出るため除外し、件数のみ `skipped_event_types` に残す。

### 受け入れ基準の充足状況

- **既知事象の再現: 達成。** 「中立は閾値ではなく veto 起因」という既知の結論に
  本機能が独立に到達した(中立11,936件のうち gate 由来が明示されるもの5,725件、
  かつ timeframe 層別で 1h のみ `below_production_threshold` が支配的という
  セル固有の偏りを自動検出)。
- **全読込を要求しない: 達成。** 逐次走査に変更し、20,000行で
  **peak RSS 1,879MB → 30MB(62分の1)**、処理時間も 18.4秒 → 7.8秒。
  全34,107行でも 35MB 一定。

### 未解決の観測(コード修正はしていない)

**中立6,211件(全判断の18.9%)にブロック根拠が記録されていない。**
うち5,175件は `conviction: 0`。`below_production_threshold` は1,478件しか
発火しておらず、差分が説明されていない。`timeframe.py:429-432` の
fail-closed 経路(`operational_data_ok`/`market_open` 不成立時に
`analysis_direction` を空にする)など、`gate_reasons` に積まれない中立化経路が
存在する可能性がある。

これは**観測結果であって原因の断定ではない**。本MVPは読み取り専用であり、
判断側のコードは変更していない。原因調査と対処は別作業。

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
- **異常判定の閾値を持たない** — `dominance` を算出するだけで、
  「いくつ以上が異常か」の基準は運用側の判断に委ねる。上記の実機検証で
  既知事象の再現は確認したが、**閾値の妥当性は未評価**。
- **改善効果は未実証** — 本機能は観測を提供するだけで、判断品質や収益性を
  改善した証拠はない。可視化された6,211件の未説明中立も、原因調査は未実施。

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
