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
| Positioning | Weekly CFTC COT futures positioning | More convenient normalized datasets | Faster/richer client/dealer positioning; still not the entire OTC market |
| Options | Little consistent FX vol-surface history | Vendor snapshots/risk reversals for selected pairs | Tradable vol surfaces, expiries, skew/term structure and flow context |
| News/events | RSS/headlines with uncertain first-seen/revision contracts | Licensed calendars/news with timestamps | Low-latency licensed feeds, corrections, entity/event IDs and source rights |
| Cross asset | Public daily/intraday series with mixed calendars | Normalized broker/vendor feeds | Synchronized low-latency cross-asset history and corporate/action metadata |
| Legal/operational | Public terms may prohibit redistribution or lack SLA | Retail terms and pacing limits | Contracted SLA, licensing, audit/support and disaster recovery |

## Current repository inventory and admissibility

| Source/path | Current use | Promotion-grade status | Gap |
|---|---|---|---|
| TradingView scanner/alerts | Current technical snapshots and webhook signals | No | Not a proven immutable historical bid/ask/availability feed; delivery can fail; forming bar ranges cannot label future paths |
| FRED CSV/API cache | US rates/VIX/dollar inputs | No | Current cache overwrites latest state; no complete vintage/revision/availability store yet |
| CFTC public TFF/COT | Weekly positioning | No | Report date exists, but actual release/first-ingestion and holiday schedule are not persisted per record |
| ForexFactory-style calendar | Event blackout/display | No | Third-party availability/revision contract; actual/revised fields and first-seen history incomplete |
| News RSS | Headline features | No | Source rights, first-seen/correction history and full immutable article lineage incomplete |
| `logs/*.jsonl` | Local decisions/prices | Research-only | Sparse history; price bid/ask absent; existing outcomes are mostly immature; remote journals show duplicates/time reversals |
| `examples/sample_prices.csv` | Deterministic functional tests | Synthetic only | Must never support promotion/performance claims |
| `runs/data/*.csv` | Historical local files | Inadmissible until provenanced | Source/license/acquisition/transformation hashes not attached |
| IBKR paper stack | Paper execution architecture | Not currently evidence-ready | Mac mini version drift, restart loops, reconciliation/health gaps and no forward fill sample |

## What public data can honestly support

- Research hypotheses about carry/rates, medium-horizon momentum/reversion, public positioning, event risk and cross-asset regimes.
- A rigorous PIT/revision capture pipeline if this repository begins archiving official releases and broker quotes now.
- Conservative execution models calibrated to the chosen broker’s paper/real fills.
- An institutional-grade **process** after sufficient forward evidence and operational stability.

It cannot support statements that the system sees bank flow, consolidated OTC liquidity, dealer inventory, true stop books, or option/customer flow. “Order imbalance,” “liquidity sweep,” and “stop cascade” may be names for explicitly defined proxies only.

## Highest-value acquisition sequence

1. Contract or capture a reliable broker bid/ask M1/tick history with source timestamps, timezone/calendar rules and paper fill linkage for the three target pairs.
2. Build immutable FRED/central-bank/COT/calendar/news vintage storage with official release/first-seen times and source payload hashes.
3. Accumulate at least one frozen shadow/paper cycle with order/fill/reject/latency/reconciliation/TCA events.
4. Add synchronized futures volume/open interest and rates/OIS histories; validate incremental OOS value against simple baselines.
5. Consider institutional options/flow data only after the process demonstrates it can avoid leakage and exploit cheaper data robustly.

Vendor selection must evaluate license, timestamps, revisions, coverage, outages, SLA, API limits, redistribution, cost and exit/portability—not marketing claims.
