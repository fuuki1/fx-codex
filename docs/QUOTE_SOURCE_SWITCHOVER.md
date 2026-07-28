# bid/ask 供給源の切り替え手順

## なぜ 2 系統あるのか

コスト控除後の実現R(`realized_net_r`)を算出するには **実 bid/ask** が要る。
既定の価格収集(`fx_tf_snapshot.py` / TradingView scanner)は **mid 値のみ**で
spread を持たないため、これだけでは採点が原理的にできない。

本命は OANDA の read-only pricing stream(`tools/fx_quote_collector.py`)だが、
資格情報が用意できるまでの間、**資格情報不要**の公開 datafeed から
実 bid/ask を取る経路(`tools/fx_datafeed_collector.py`)を用意している。

## 下流は完全に共通

```
dukascopy.parse_hour_payload ─┐
                               ├─→ CollectedQuote ─→ ingest_payload ─→ raw store + QuoteLog
oanda.parse_price_line       ─┘
```

両者とも `data_platform.collect.raw_first.ingest_payload` を通り、
同じ `CollectedQuote` 契約・同じ出力先(`collect/`)を使う。
**切り替えで変わるのは「どの launchd service を動かすか」だけ**で、
下流のスキーマ・検証・保存経路は一切変わらない。

区別は `CollectedQuote.provider`(`"dukascopy"` / `"oanda"`)で付く。

## 現在: Dukascopy(資格情報なし)

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fx-codex.datafeed-collector.plist
```

動作確認:

```bash
.venv/bin/python tools/fx_datafeed_collector.py \
  --output-root ~/srv/fx-codex/collect --dry-run
```

### 限界(誇張しないこと)

| 項目 | 値 | 意味 |
|---|---|---|
| `collection_mode` | `historical_download` | live stream ではない |
| `tradable` | **`False`** | その瞬間に約定できた気配ではない |
| 遅延 | 既定 2 時間(`--lag-hours`) | provider の確定を待つ |
| `account_environment` | `datafeed` | broker 口座由来ではない |

**spread の実測値としては使えるが、live quote の代替ではない。**
この区別は `CollectedQuote` に記録されるので、下流で混同されることはない。

## OANDA が使えるようになったら

1. read-only **pricing scope** のトークンと account ID を用意する
2. `.env`(または `collector.env`)へ 3 つを設定する

```
FX_OANDA_API_TOKEN=...
FX_OANDA_ACCOUNT_ID=...
FX_OANDA_ENV=practice
```

3. 設定を検証する(トークンの有効性ではなく存在の確認)

```bash
.venv/bin/python tools/fx_quote_collector.py \
  --output-root ~/srv/fx-codex/collect --dry-run
```

未設定なら **exit 78 (EX_CONFIG)** で、quote log に一切触れずに落ちる。

4. service を入れ替える

```bash
# OANDA を有効化
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fx-codex.quote-collector.plist

# datafeed 側を止める(任意 — 併用も可)
launchctl bootout gui/$(id -u)/com.fx-codex.datafeed-collector
```

### 併用してもよい

両方動かした場合、`provider` フィールドで区別されたまま両系統が蓄積される。
後から「どちらを正準とするか」を選べるので、**移行期は併用が安全**。
OANDA の live stream が安定していることを確認してから datafeed を止めればよい。

## 切り替え後に確認すること

```bash
# provider ごとの件数
grep -o '"provider": "[a-z]*"' ~/srv/fx-codex/collect/log/quotes.jsonl | sort | uniq -c

# tradable な live quote が入り始めたか(OANDA なら true が出る)
grep -c '"tradable": true' ~/srv/fx-codex/collect/log/quotes.jsonl
```

## 未接続の後続作業

bid/ask が入っても、それだけでは `outcomes` は埋まらない。残りは 2 段:

1. `execution_cost_r` を判断行へ載せる配線(spread → コスト換算)
2. `record_outcome()` を呼ぶ scorer の実装

`fx_intel/operational_store.py` の `record_outcome()` は
`status="scored"` に `realized_net_r` を必須とする fail-closed 実装で、
**コスト根拠が無いまま行数だけ増やすことはできない**(意図的な設計)。
