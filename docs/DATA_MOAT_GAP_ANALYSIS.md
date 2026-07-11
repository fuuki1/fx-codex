# Data moat gap analysis

## Conclusion

The repository can improve the rigor of a public-data research process, but it currently has no defensible proprietary FX information moat. The largest blocker is not another technical indicator or model: it is point-in-time, licensed, execution-relevant market/macro/event history plus live-like paper fills and sufficient forward outcomes.

## Capability by data tier

| Capability | Free/public | Low-cost/pro retail | Institutional/contracted |
|---|---|---|---|
| Spot price | Aggregated/scanner snapshots or downloadable bars; venue/source ambiguity | Broker bid/ask bars/ticks, limited history | Multi-venue timestamped ticks/quotes, venue and liquidity metadata |
| Spread/execution | Current bid/ask may be missing; historical spread often modeled | Broker-specific historical/streaming quotes and paper fills | Venue/broker comparison, depth, reject/partial fill, detailed TCA and timestamps |
| Volume/order flow | Tick volume/candle proxies only | Futures volume/open interest; some retail positioning | Dealer/customer flow, ECN depth, prime-broker/venue flow, axes—subject to confidentiality |
| Rates/macro | FRED/central-bank releases, but vintages must be captured | Curated calendars/consensus histories | Licensed real-time releases, full consensus/revisions, OIS/curve histories and timestamps |
| Positioning | Weekly public CFTC futures positioning; contract-specific proxy, not symmetric spot-FX flow | More convenient normalized futures/retail positioning datasets | Faster/richer client/dealer positioning; still not the entire OTC market |
| Options | Little consistent FX vol-surface history | Vendor snapshots/risk reversals for selected pairs | Tradable vol surfaces, expiries, skew/term structure and flow context |
| News/events | RSS/headlines with uncertain first-seen/revision contracts | Licensed calendars/news with timestamps | Low-latency licensed feeds, corrections, entity/event IDs and source rights |
| Cross asset | Public daily/intraday series with mixed calendars | Normalized broker/vendor feeds | Synchronized low-latency cross-asset history and corporate/action metadata |
| Legal/operational | Public terms may prohibit redistribution or lack SLA | Retail terms and pacing limits | Contracted SLA, licensing, audit/support and disaster recovery |

## Current repository inventory and admissibility

| Source/path | Current use | Promotion-grade status | Gap |
|---|---|---|---|
| Community `tradingview_ta` scanner (`forex`, OANDA symbols) | Current/forming-interval technical and price snapshots | No | Unofficial scanner contract; no upstream source timestamp, immutable raw response or revision history. Local event/available/ingested fields do not prove when the source created or exposed the value; bid/ask may be absent |
| TradingView alert webhook transport | Incoming signal payloads | No market-data status | Vendor documentation covers HTTP alert delivery, not scanner OHLC/history. Delivery gaps, duplicates, latency and reconciliation still need forward evidence |
| FRED `fredgraph.csv` TTL cache (`VIXCLS`, `DGS10`, `DGS2`, `DTWEXBGS`) | Current US rates/VIX/broad-dollar regime inputs | No | Implemented route is current/latest history, not the vintage API. Cache refresh overwrites the prior body; no per-observation publication, ingestion, availability or revision history |
| CFTC Legacy Futures Only `6dca-aqww` | Legacy noncommercial weekly positioning proxy | No | Current code does not use TFF. It stores report date but not official release/first-ingestion, source row ID, holiday schedule snapshot, corrections or per-row raw hash |
| Stooq daily CSV constant/parsers | Candidate daily OHLC parser | No; not active ingestion | `fetch_macro_snapshot` does not call it. No accepted stable API, license, source timestamp, immutable capture or revision contract is recorded |
| FairEconomy weekly calendar | Event blackout/display and partial changed-row archive | No | Third-party schedule; actual/revised fields, authoritative publication time and per-field provenance are incomplete. The backtester discards `recorded_at`, so the archive is not currently replayed as-of |
| FXStreet and Google News search RSS | In-memory headline features and decision context | No | Publication time only; no GUID/first-seen/ingestion/update history, immutable raw feed hash, source-rights record or contracted SLA. Title-based deduplication can merge distinct items |
| `logs/*.jsonl` | Local decisions/prices | Research-only | Sparse history; price bid/ask absent; existing outcomes are mostly immature; remote journals show duplicates/time reversals |
| `examples/sample_prices.csv` | Deterministic functional tests | Synthetic only | Must never support promotion/performance claims |
| `runs/data/*.csv` | Historical local files | Inadmissible until provenanced | Source/license/acquisition/transformation hashes not attached |
| Historical/remote IBKR paper-stack copy | Prior paper execution architecture; local stack removed 2026-07-10 | Outside the current implementation | Observed Mac mini copies have version drift and no attested forward fill/reconciliation sample; they must not be treated as an active approved stack |

## Current COT proxy contract

The implemented feature is **CFTC Legacy Futures Only**, not Traders in Financial Futures. It computes Legacy noncommercial net position divided by each contract’s own open interest and then compares the two currency legs.

That comparison is not economically symmetric. The configured USD leg is ICE U.S. Dollar Index futures, while the other legs are individual currency futures. Their baskets, quote conventions, contract populations and open-interest denominators differ. Dividing by open interest makes each series scale-free inside its own contract, but it does not turn the values into directly comparable spot-FX customer positioning. The score may be tested only as a named **cross-contract public positioning proxy**. It cannot be described as dealer flow, USD flow, consolidated COT pair exposure or an information moat.

TFF dealer, asset-manager and leveraged-money categories are a candidate alternative feature family. Switching to them would change the hypothesis and require an explicit report-type filter, versioned transformation and new leakage-resistant OOS validation; it is not a correction that can be silently substituted for the current Legacy feature.

## Unresolved PIT, revision and ingestion limits

- The repository has a common PIT record/as-of utility, but the source loaders in the inventory are not yet routed through one immutable, bitemporal store. The utility therefore does not prove system-wide PIT compliance.
- The target join cutoff is `available_time = max(authoritative publication/revision time, successful ingestion time, validation completion time)`. The current source paths do not consistently record those inputs per observation.
- FRED graph CSV and CFTC TTL caches overwrite the preceding response. They can support a current snapshot, not reconstruction of what a historical decision knew.
- COT report date is an as-of Tuesday, not its public availability time. The official 3:30 p.m. Eastern release and holiday-delayed schedule must be captured immutably per report; it cannot be replaced by a fixed three-day lag.
- Scanner price rows use local capture metadata without an upstream source timestamp. Current forming-bar OHLC is not a post-prediction path, and a local timestamp does not establish bar-close or executable-quote semantics.
- RSS publication time is not first-seen time. Corrections, delayed aggregation and future-dated feed errors cannot be replayed from the current in-memory representation.
- The FairEconomy changed-row archive is useful partial evidence, but lacks actual/revised release fields and its `recorded_at` is currently ignored by the backtester.
- Decisions may contain selected values or headlines, but they do not consistently reference immutable raw-object hashes and source-version records for macro, COT, calendar, news and scanner inputs.

## What public data can honestly support

- Exploratory research hypotheses about carry/rates, medium-horizon momentum/reversion, public positioning proxies, event risk and cross-asset regimes, with the current sources explicitly excluded from promotion evidence where historical availability is unproven.
- A rigorous PIT/revision capture pipeline if this repository begins archiving official releases and broker quotes now.
- Conservative execution models calibrated to the chosen broker’s paper/real fills.
- An institutional-grade **process** after sufficient forward evidence and operational stability.

It cannot support statements that the system sees bank flow, consolidated OTC liquidity, dealer inventory, true stop books, or option/customer flow. “Order imbalance,” “liquidity sweep,” and “stop cascade” may be names for explicitly defined proxies only.

## Highest-value acquisition sequence

1. Contract or capture a reliable broker bid/ask M1/tick history with source timestamps, timezone/calendar rules and paper fill linkage for the three target pairs.
2. Route every source through an immutable raw-object store and a common bitemporal envelope with source record ID, publication/revision, first ingestion, validation completion, availability, schema/transform version and raw payload hash.
3. Replace current-only macro history with FRED/ALFRED-capable vintages and archive CFTC release schedules/rows; capture calendar/news revisions and first-seen evidence. Evaluate Legacy and TFF as separate hypotheses.
4. Accumulate at least one frozen shadow/paper cycle with order/fill/reject/latency/reconciliation/TCA events.
5. Add synchronized futures volume/open interest and rates/OIS histories; validate incremental OOS value against simple baselines.
6. Consider institutional options/flow data only after the process demonstrates it can avoid leakage and exploit cheaper data robustly.

Vendor selection must evaluate license, timestamps, revisions, coverage, outages, SLA, API limits, redistribution, cost and exit/portability—not marketing claims.
