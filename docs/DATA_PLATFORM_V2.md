# データ基盤 V2 設計

**ステータス:** 契約・raw store・quality・bar materializer は実装済み（テスト緑）。**COT PIT は実CFTCで実証済み。** broker bid/ask は未接続、30取引日連続運用は未達（判定2）。
**一次コード:** `data_platform/`（contracts / raw / quality / materialize / adapters / lineage）＋ `fx_intel/cot_pit.py`。**コードが正。**

## 1. レイヤ構成

```
raw source → immutable raw store → PIT contract → quality state → bid/ask bar materialize → feature as-of
```

## 2. broker quote adapter（`data_platform/adapters/broker.py`）

`QuoteSource` Protocol（source_id / writer_id / instrument / quotes()）。実装:
- `ReplayQuoteSource` — fixture 決定論replay（研究replay・テスト用）
- `UnimplementedQuoteSource` — **fail-closed**。「空ストリーム＝データ無し」と誤認されないよう、未実装ソースは例外を投げる

**実broker adapter は未実装**（credentials 無し、`a live connection is unvalidated` と明記）。実装しても**実注文は行わない**。

### `MarketQuote` 契約フィールド（`contracts/market_quote.py`）
```
source_id / instrument / bid / ask / (mid, spread は導出) / bid_size / ask_size
source_timestamp / received_timestamp / available_at / sequence_id / writer_id
revision_id / tradable
```
不明値は捏造しない（`| None`）。single-writer 規律: 各 quote に `writer_id` をスタンプ。

## 3. immutable raw store（`data_platform/raw/`）

- content-addressed（SHA-256）/ append-only / source・ingestion metadata / schema version / writer ID / replay可能 / duplicate detection / quarantine
- **実データで実証**: COT capture が各ページを `body_sha256` で content-address 保存、82MB を改竄検出可能に保持（[evidence](../reports/evidence/cot-cftc-real-pit-20260713/README.md)）。

## 4. bid/ask bar materialization（`data_platform/materialize/bid_ask_bars.py`）

quote から生成: bid/ask/mid OHLC、spread open/mean/median/p95/max、quote_count、stale_seconds、source_coverage、gap flags、out_of_order count、duplicate count。対応足: 5s/1m/5m/15m/1h/4h/1d。

- **quote が無い区間に架空バーを作らない。** バー時刻は close 時刻基準。バー完成前の値を正式特徴量に使わない。
- 再生成 determinism 実証（`test_data_platform_bars.py`）。
- **実quote 未接続のため、実データでの quote→bar は未実証。**

## 5. 品質 SLO（`data_platform/quality/state.py`）

4状態 `QualityState`（StrEnum）:

| 状態 | 意味 | 正式研究での可否 |
|---|---|---|
| `usable` | 全hard invariant成立＋freshness/completeness が SLO 内 | ✅ 原則これのみ |
| `degraded` | soft SLO miss（例: 軽微なstale ≤300s）だが hard違反なし | ⚠️ 明示waiver＋品質上限＋昇格不可条件付きでのみ |
| `quarantined` | hard invariant違反（dup key / future ts / bid>ask 等） | ❌ |
| `unavailable` | 取得・測定不能 | ❌ |

測定項目（タスク§6）: freshness / completeness / duplicate rate / late arrival / out-of-order / clock skew / schema violation / source divergence / revision latency / gap rate / writer count / hash mismatch。

## 6. cross-source照合

primary（取引broker）と secondary（独立ソース）の `primary_mid / secondary_mid / divergence_pips / divergence_threshold / divergence_breach` を保存。乖離時は**平均化せず** `usable→degraded/quarantined/unavailable` へ品質を落とす（§2-3、PR #35）。**2系統の実データ未接続のため実証は1ソースのみ。**

## 7. PIT外部データ

| ソース | 契約 | 実装状態 |
|---|---|---|
| **COT（CFTC）** | `fx_intel/cot_pit.py` | ✅ **実データ実証**（実fetch＋PIT＋revision＋as-of） |
| 経済指標（macro release） | `contracts/macro_release.py` | ⚠️ 契約のみ（`scheduled_time≠公開時刻`、revision別保存、fetch失敗を0/前回値に置換しない設計） |
| 経済カレンダー | `contracts/economic_event.py` | ⚠️ 契約のみ |
| ニュース | `contracts/news_event.py` | ⚠️ 契約のみ（バックフィル記事を過去既知扱いしない設計） |

### COT PIT の実証内容（[evidence bundle](../reports/evidence/cot-cftc-real-pit-20260713/README.md)）
- 実CFTC 13,727行（8通貨・1986-2026）/ SHA256照合 / count整合 / deterministic replay
- **PITゲート**: 取得前=unavailable、取得後=usable（将来情報非混入を実データで実証）
- availability を「実際に保持した時刻」へ正規化 / `research_only` / `promotion_eligible:false`
- 自己申告する限界: local custody / 先物ポジション代理（スポット注文フローではない）/ revision検出は stable row id 依存

## 8. アラート（重複抑制）

複数writer検出 / snapshot停止 / quote鮮度超過 / gap急増 / duplicate急増 / clock skew / source divergence / schema drift / raw hash不一致 / bar再生成hash不一致 / macro・calendar・news停止。Discord は重複抑制（`tools/data_freshness_monitor.py` + 通知層）。**本番配備は未達（P0-3）。**

## 9. 完了条件に対する現状（タスク§12）

| 条件 | 状態 |
|---|---|
| PIT / immutable raw / revision / single-writer / quote→bar再生成 / 品質SLO | ✅ 構造 |
| macro/calendar/news の PIT 運用 | ⚠️ 契約のみ（実接続なし） |
| **実broker bid/ask** | ❌ **未接続** |
| **30取引日以上の連続稼働証拠** | ❌ **未**（単発snapshot） |
| 重大な stale/duplicate/gap 汚染なし | ❌ 未実証（連続運用が無い） |
| cross-source 照合 | ⚠️ 1ソースのみ実証 |
| COT PIT 実運用 | ✅ **実データ実証** |

→ **構造 60以上 / 実運用 40未満**。broker bid/ask 接続＋常駐配備＋30取引日連続が主要ボトルネック。
