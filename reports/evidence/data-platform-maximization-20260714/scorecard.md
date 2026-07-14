# Data platform scorecard v1.1.0

**status: capped — score 72.67 / 100** (raw 72.67, hard cap 80)

| section | points | max |
|---|---:|---:|
| trading_market_data | 22.0 | 30.0 |
| dual_source_verification | 15.0 | 15.0 |
| macro_event_pit | 15.0 | 15.0 |
| continuous_operation | 0.67 | 20.0 |
| reproducibility_audit | 10.0 | 10.0 |
| fault_tolerance_secrets | 10.0 | 10.0 |

## Hard caps

- **≤80**: live market data from non-broker aggregator only (no broker live stream)
- **≤85**: fewer than 30 trading days (1)

## Unmet conditions

- [trading_market_data] no live broker bid/ask stream (aggregator only)

## Award basis

- [trading_market_data] +7/15: live NON-BROKER aggregator stream: 655 quotes from ['truefx'] (collection_summary.json)
- [trading_market_data] +7/7: real bid/ask quotes ingested: 2624357 (modes: ['historical_download', 'live_stream']) (collection_summary.json)
- [trading_market_data] +4/4: pair coverage: ['EURUSD', 'GBPUSD', 'USDJPY'] (collection_summary.json)
- [trading_market_data] +2/2: bid/ask sizes present or honestly flagged provider_does_not_supply_* (collection_summary.json)
- [trading_market_data] +2/2: raw-first storage verified (raw sha256 recorded before normalization) (collection_summary.json)
- [dual_source_verification] +8/8: independent providers compared: ['dukascopy', 'fxcm', 'histdata'] (divergence_report.json)
- [dual_source_verification] +3/3: compared pairs: ['EURUSD', 'GBPUSD', 'USDJPY'] (divergence_report.json)
- [dual_source_verification] +2/2: required divergence metrics measured (divergence_report.json)
- [dual_source_verification] +2/2: breach policy exercised (no averaging; degraded/quarantined transition observed) (divergence_report.json)
- [macro_event_pit] +8/8: real macro records: 14 from alfred (vintage_correct=True) (macro_pit_report.json)
- [macro_event_pit] +4/4: as-of query verified on real data (pre-availability blocked) (macro_pit_report.json)
- [macro_event_pit] +3/3: initial vs revised values stored separately (vintage evidence) (macro_pit_report.json)
- [continuous_operation] +0.67/20: only 1/30 qualifying trading days (daily_report_*.json)
- [reproducibility_audit] +5/5: deterministic replay on real data (hash 4ebadf625129…) (replay_report.json)
- [reproducibility_audit] +5/5: independent-environment reproduction matched on real data (independent_reproduction.json)
- [fault_tolerance_secrets] +6.0/6: fault injection: 54/54 scenarios fail-closed (fault_injection_report.json)
- [fault_tolerance_secrets] +2/2: secrets scan clean (0 leaks) (secrets_scan.json)
- [fault_tolerance_secrets] +2/2: collector verified to import no order/executor path (secrets_scan.json)
