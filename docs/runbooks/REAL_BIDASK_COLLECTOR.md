# Runbook: read-only bid/ask collector（Primary/Secondary・macro PIT）

**対象:** `data_platform/collect/` の運用。市場データの**read-only収集専用** — 発注経路とはプロセス・設定・認証情報を完全分離（`tests/test_collect_no_order_path.py` が担保）。
**Mac mini への適用は人間の明示承認後のみ**（本runbookは手順書であり実行承認ではない）。

## 1. ソース構成

| 役割 | provider | 認証 | 状態 |
|---|---|---|---|
| Primary(live, broker) | OANDA v3 pricing stream | `FX_OANDA_API_TOKEN` / `FX_OANDA_ACCOUNT_ID` / `FX_OANDA_ENV` | **実装済・未接続**（credentials未保有。無しでは fail-closed EX_CONFIG=78） |
| Live(aggregator) | TrueFX webrates (無認証) | **不要** | **実証済**（`--source truefx`。indicative・常に`tradable=false`・scorecardはaggregator枠=部分加点+cap80） |
| 実bid/ask tick(historical) | Dukascopy datafeed ticks (.bi5) | 不要 | 実証済（52,732 quote / 3pair、503リトライ実装） |
| 実bid/ask candle(historical) | Dukascopy datafeed m1/h1 candles (.bi5) | 不要 | **実証済**（2019-2026 h1 + 2024通年m1 ×3pair → 5m/1h バー実体化） |
| 独立第2 bid/ask(historical) | FXCM candledata (H1 週次csv.gz) | 不要 | **実証済**（2023-2025のみ採用。2021-22はcrossed-book 2.3-6.3%を計測し除外） |
| Secondary照合 | HistData 1h bars | 不要 | 実証済（bar-mid照合。**既存コミットCSVは+1hラベルずれを計測** → incident参照、研究はDukascopyバーを使用） |
| macro PIT | ALFRED alfredgraph.csv | 不要 | **実証済**（vintage刻印列を必須化。素のfredgraph.csvはvintage無視のため使用禁止） |

practice/demo は接続技術の実証にのみ使い、本番実証に数えない（scorecard cap 90）。

## 2. credentials（値を聞かない・書かない・ログしない）

```bash
mkdir -p ~/.config/fx-codex
cat > ~/.config/fx-codex/collector.env <<'EOF'
FX_OANDA_API_TOKEN=<ここに読み取り専用トークン>
FX_OANDA_ACCOUNT_ID=<アカウントID>
FX_OANDA_ENV=live        # または practice（本番実証には数えない）
EOF
chmod 600 ~/.config/fx-codex/collector.env
```
- `.env` は **絶対にcommitしない**（`test_env_files_are_not_tracked` が監視）
- plistに秘密情報は入らない。token は repr/ログで `***masked***`
- token失効時: collector は EX_NOPERM=77 で停止（KeepAlive は再起動しない設定）→ 人間がtokenを更新して `install` し直す

## 3. launchd 運用（Mac mini、承認後）

```bash
scripts/quote_collector_launchd.sh dry-run    # plist lint + 設定検証（変更なし）
scripts/quote_collector_launchd.sh install    # 常駐化（collector.env 600 必須、無ければ拒否）
scripts/quote_collector_launchd.sh status     # launchctl状態 + last_run.json
scripts/quote_collector_launchd.sh uninstall  # 停止+plist撤去（raw/logは保持）
scripts/quote_collector_launchd.sh rollback   # uninstallと同義
```
- single-writer: daemon が `ExclusiveLock`（flock）を取得。二重起動は EX_TEMPFAIL=75 で即退出
- graceful shutdown: SIGTERM/SIGINT 捕捉、`state/last_run.json` に接続状態・gap・reconnect回数を保存
- 再起動復旧: QuoteLog は既存JSONLから重複watermarkを再構築（テスト済）

## 4. データ経路（raw-first、順序固定）

```
provider raw bytes → ImmutableRawStore(content-addressed) → hash再検証
  → schema validation → CollectedQuote正規化 → 品質判定(dup/ooo/stale)
  → quotes.jsonl(append-only+fsync) / quarantine.jsonl
```
- 不正データは**修復せず検疫**: bid≥ask・NaN・naive/未来時刻は契約拒否、stale は検疫、欠損フィールドは `provider_does_not_supply_*` フラグ（0埋め禁止）
- 接続断は gap として明示記録。**前値穴埋め禁止**

## 5. 日次運用（30取引日カウントの前提）

カウント開始条件（全て満たした日から）: collector/schema/SLO version固定・Primary+Secondary稼働・raw hash検証成功・daily report生成・critical incident 0。過去データで水増ししない。

daily report（`daily_report_YYYY-MM-DD.json`）: uptime/availability/quote count(pair別)/freshness p50-p99/missing・duplicate・out-of-order率/reconnect回数/最長outage/divergence分布/quarantine数/raw hash/replay hash/disk・clock skew/incidents/MTTR/scorecard/commit・config・lock hash。

SLO は [config/data_platform_slo_v1.json](../../config/data_platform_slo_v1.json)（変更はversion up+理由記録、過去スコアは書き換えない）。

## 6. 監視・アラート

- 鮮度: 既存 `tools/data_freshness_monitor.py` 系のDiscord通知に統合（重複抑制）
- quarantine急増・divergence breach・token失効・lock競合・disk逼迫を通知対象へ
- divergence breach 時は**平均化せず** degraded/quarantined へ落とし、下流研究から除外

## 7. 障害対応早見表

| 事象 | collector挙動 | 対応 |
|---|---|---|
| ネットワーク断 | backoff+jitter再接続、gap記録、tradable=false | 放置可（gap は証跡） |
| token失効 | **停止**(77)、再起動しない | token更新→install |
| 二重起動 | 後発が75で退出 | 先発を確認 |
| disk full | raw書込失敗→ingest中断（データ受理なし） | 容量確保→再起動（重複は自動検疫） |
| raw改竄 | store.get がhash不一致で例外 | 調査。scorecard は fatal=0点 |
| provider片系停止 | divergence=unavailable | 復旧待ち。研究入力から除外 |

## 8. 再取得・再現

- Dukascopy: pair×hour単位で公開再取得可（`reproduce.sh`）
- deterministic replay: raw blob→re-parse→data-hash一致（本セッションで実データ52,732行一致を実証、独立worktreeでも一致）

## 9. 研究側への接続（bid/askバー → authoritative pipeline、2026-07-15）

- dataset: `data/real/dukascopy/*.csv.gz`（正準CandleBar CSV。lineageは同ディレクトリの
  `dataset_registry.jsonl`）。読み込みは `fx_backtester.data.load_bidask_bars_csv` —
  **価格基準はbid側OHLC + `spread_price`=バー始端の実測スプレッド**（mid高値/安値は捏造しない）
- manifest: `data.sources[].kind = "bidask_bars_csv"`、
  `costs.cost_model_version = "measured_bar_spread_v1"`（entry_lag=1に合わせ
  **エントリーバー始端の実測スプレッド**をトレード毎コストに使用。計測欠落はfail-closed、
  close-onlyソースとの併用はmanifest段階で拒否）
- 実証: `reports/evidence/dukascopy-bidask-usdjpy-2024-1h-20260715/`
  （USD/JPY 2024・916トレード・net −0.0796R・昇格DENIED・決定論2回一致）
- HistData CSVは補助照合用（v2でラベル修正済み・bid基準宣言、
  `data/real/histdata/README.md`）。研究入力はDukascopyバーを第一選択とする
