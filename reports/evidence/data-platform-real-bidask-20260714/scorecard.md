# Data platform scorecard v1.0.0

**status: capped — score 61.0 / 100** (raw 61.0, hard cap 75)

| section | points | max |
|---|---:|---:|
| trading_market_data | 15.0 | 30.0 |
| dual_source_verification | 11.0 | 15.0 |
| macro_event_pit | 15.0 | 15.0 |
| continuous_operation | 0.0 | 20.0 |
| reproducibility_audit | 10.0 | 10.0 |
| fault_tolerance_secrets | 10.0 | 10.0 |

## Hard caps

- **≤75**: no live market data (historical download and/or demo only)
- **≤85**: fewer than 30 trading days (0)

## Unmet conditions

- [trading_market_data] no live non-demo broker bid/ask stream collected
- [dual_source_verification] divergence metrics not all measured (need ['mid_diff_pips', 'receive_time_skew_ms', 'spread_diff_pips'])
- [dual_source_verification] divergence breach policy never exercised
- [continuous_operation] no qualifying trading days of continuous operation

## Award basis

- [trading_market_data] +7/7: real bid/ask quotes ingested: 52732 (modes: ['historical_download']) (collection_summary.json)
- [trading_market_data] +4/4: pair coverage: ['EURUSD', 'GBPUSD', 'USDJPY'] (collection_summary.json)
- [trading_market_data] +2/2: bid/ask sizes present or honestly flagged provider_does_not_supply_* (collection_summary.json)
- [trading_market_data] +2/2: raw-first storage verified (raw sha256 recorded before normalization) (collection_summary.json)
- [dual_source_verification] +8/8: independent providers compared: ['dukascopy', 'histdata'] (divergence_report.json)
- [dual_source_verification] +3/3: compared pairs: ['EURUSD', 'GBPUSD', 'USDJPY'] (divergence_report.json)
- [macro_event_pit] +8/8: real macro records: 16 from alfred (vintage_correct=True) (macro_pit_report.json)
- [macro_event_pit] +4/4: as-of query verified on real data (pre-availability blocked) (macro_pit_report.json)
- [macro_event_pit] +3/3: initial vs revised values stored separately (vintage evidence) (macro_pit_report.json)
- [reproducibility_audit] +5/5: deterministic replay on real data (hash cdca393e2e7a…) (replay_report.json)
- [reproducibility_audit] +5/5: independent-environment reproduction matched on real data (independent_reproduction.json)
- [fault_tolerance_secrets] +6.0/6: fault injection: 23/23 scenarios fail-closed (fault_injection_report.json)
- [fault_tolerance_secrets] +2/2: secrets scan clean (0 leaks) (secrets_scan.json)
- [fault_tolerance_secrets] +2/2: collector verified to import no order/executor path (secrets_scan.json)
