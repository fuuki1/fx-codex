# Real bid/ask data-platform evidence — 2026-07-14

**Machine-judged score: 61 / 100（hard cap 75）** — [scorecard.json](scorecard.json) / [scorecard.md](scorecard.md)。
コード量・テスト数は不加点、**このバンドル内の証拠ファイルのみ**から `tools/data_platform_scorecard.py` が算出。

## 実証できたこと（全て実データ・再現可能）

| 項目 | 実測値 |
|---|---|
| **実bid/ask quote（サイズ付き）** | **52,732** — Dukascopy銀行datafeed、USDJPY/EURUSD/GBPUSD × 2024-01-10 12–15h UTC、9 raw blob、検疫0 |
| raw-first保存 | 生LZMA bytes を content-addressed 保存→hash再検証→正規化（全quoteが raw sha256 を引用） |
| **deterministic replay（実データ）** | 9 raw blob再parse → 52,732行のdata-hash **完全一致** (`cdca393e…`) |
| **独立環境再現** | 別git worktree（同一commit）から同一hash **一致** |
| **独立2ソース照合（実データ）** | Dukascopy vs HistData（独立provider）、3pair×9bar、**max 2.80 pips → usable**（平均化なし） |
| **macro PIT（実vintage）** | ALFRED keyless、16観測値。**GDPC1 2023Q4: vintage 2024-02-01=22,672.859 ≠ 2024-04-05=22,679.255**（改定を別recordで分離）。as-of が capture前=0行 を実証 |
| 障害注入 | **23/23シナリオ合格**（切断/timeout/token失効/malformed/重複/順序/未来時刻/crossed book/乖離/heartbeat/多重起動/再起動復旧/disk full/raw改竄/replay不一致=fatal/token非ログ） |
| secrets | 追跡ファイルにcredential形状なし、.env非追跡、collector は order経路を import しない（全てテスト実行で検証） |

## 未達（正直な明示）— hard cap の根拠

| 未達 | cap | 理由 |
|---|---|---|
| **live市場データ 0件** | **≤75** | OANDA adapter は実装済（fail-closed・replayテスト済）だが credentials 未保有。歴史ダウンロードは live ではない |
| **30取引日連続稼働 0日** | ≤85 | 収集開始前。コード完成では埋まらない（物理時間が必要） |
| divergence metrics 一部測定不能 | 減点 | HistData は close-only（spread比較不能）、両者historical（receive skew無意味）→ null のまま、捏造しない |
| breach policy 実データ未発火 | 減点 | 実データが閾値内で頑健だった（テストでは発火を検証） |

## 既知の注意点

- `out_of_order_quote` 16,273件は**補完フェッチの取り込み順序**（USDJPY 14hを先に取り込み、後から12/13hをbackfill）を正直に記録したもの。各raw blob内のevent timeは単調で、replayは完全一致 → データ破損ではない（[incident_report.json](incident_report.json) INC-20260714-2）
- Dukascopy quote は `tradable=false` 固定（歴史データは執行可能な板ではない）
- raw本体（LZMA 9 blob）は未コミット。hash・サイズ・再取得手順は [source_manifest.json](source_manifest.json)

## 再現

```bash
reports/evidence/data-platform-real-bidask-20260714/reproduce.sh
```
（Dukascopy/ALFREDへの公開アクセスが必要。同一 pair×hour × vintage を再取得→raw-first→replay→scorecard 再計算）

## ファイル

`scorecard.{json,md}` `collection_summary.json` `quality_report.json` `divergence_report.json`
`macro_pit_report.json` `replay_report.json` `independent_reproduction.json`
`fault_injection_report.json` `secrets_scan.json` `incident_report.json`
`source_manifest.json` `provider_contract.json` `config_hashes.json`
`command_transcript.txt` `reproduce.sh`
