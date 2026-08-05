# Canonical Bid/Ask and Net-R Phase 2 gate

## Scope

This gate is read-only and analysis-only. It verifies the hash-checked canonical
outcome store and the separately versioned canonical label evidence store; it
does not connect to a broker, rescore a decision, mutate an outcome, or replace
a missing cost with zero.

Run it from the repository root:

```bash
.venv/bin/python tools/canonical_label_readiness.py \
  --store logs/canonical_outcomes.jsonl \
  --label-evidence-store logs/canonical_label_evidence.jsonl \
  --report reports/canonical_label_readiness.json
```

Exit status is `0` only when Phase 2 is complete, `3` when both stores are
verified but target evidence is incomplete, and `4` when either required store
is unavailable, corrupt, or cannot be bound to the other.

## Migration-safe evidence contract

`net-r-v2` and `net-r-v3` retain their existing definitions, natural keys, and
hashes. Aggregate-only path evidence remains under
`canonical-label-evidence-v1`. Only a successfully raw-replayed upgrade is
stored under `canonical-label-evidence-v2`; both are keyed by
`decision_id + label_version + evidence_version`. Every row contains the
canonical outcome SHA-256, the complete canonical close payload, and its
immutable virtual close SHA-256. Load-time validation recomputes the close hash
and binds MAE/MFE, exit, net-R, and path hashes back to that payload. The evidence
store uses `flock`, `fsync`, an envelope hash, and duplicate-key rejection;
identical replay is a no-op and conflicting replay is a hard error.
The gate selects the explicitly supported v2 evidence by the full three-part
key. Existing v1 rows remain loadable as immutable legacy evidence, so the
append-only side-by-side migration does not create a false duplicate. Evidence
for a decision absent from the outcome store remains an orphan and fails closed.

The connector preflights both existing stores for corruption and natural-key
conflicts before its first append. The two JSONL files are independently
durable, not one cross-file transaction: a process crash after the first fsync
can leave outcome-only coverage. The gate then fails closed, and an identical
connector retry appends the missing sidecar idempotently. Operators must not
delete or rewrite either file to repair an irreconcilable conflict.

The virtual-portfolio connector writes both stores in analysis-only mode:

```bash
.venv/bin/python tools/virtual_portfolio_canonical_outcomes.py \
  --db logs/fx_virtual_portfolio.sqlite3 \
  --store logs/canonical_outcomes.jsonl \
  --label-evidence-store logs/canonical_label_evidence.jsonl \
  --quote-log collect/log/quotes.jsonl \
  --raw-store collect/raw
```

The connector first reproduces the canonical outcome from the immutable
learning row. It then persists entry/exit bid and ask, every R-cost component,
MAE/MFE, touch times, holding seconds, and path provenance in the sidecar.

## Target projection

The gate projects existing canonical accounting into the architecture names:

| Target | Canonical evidence |
|---|---|
| `gross_r` | stored `gross_realized_r` |
| `spread_cost_r` | stored `realized_spread_cost_r`, otherwise exact `gross_realized_r - quote_realized_r` |
| `slippage_cost_r` | stored `slippage_r` |
| `commission_cost_r` | stored `commission_r` |
| `financing_cost_r` | stored `financing_r` |
| `execution_cost_r` | stored `execution_cost_r` |
| `net_r` | stored `realized_net_r` |
| `mae_r`, `mfe_r` | stored versioned evidence; missing remains unavailable |
| `time_to_tp`, `time_to_sl` | stored evidence derived from prediction and first-touch times when applicable |
| `holding_seconds` | stored evidence from prediction to holding end |
| `label_quality` | `raw_replayed_research_path` only after immutable raw replay; `hash_bound_unrecomputed_path` for aggregate-only legacy paths; otherwise `incomplete` |
| `label_version` | stored `label_version` |

Every field reports its provenance as `stored`, `derived:*`, `not_applicable`,
or `unavailable`. `not_applicable` is valid for TP time on a non-TP outcome and
for SL time on a non-SL outcome. Missing MAE/MFE is not treated as zero.

## Completion contract

`phase2_complete=true` requires all of the following over every accepted
canonical row:

1. At least one finite canonical net-R label exists. Exactly-zero outcomes count
   as labels; their count is reported separately from non-zero outcomes.
2. Long uses ask entry and bid exit; short uses bid entry and ask exit, with
   both exit sides preserved so the claim can be recomputed.
3. Spread, slippage, commission, financing, total execution cost, gross R, and
   net R satisfy the accounting identities.
4. The old gross-label difference is exactly explained by execution cost.
5. Every target label field is available or explicitly not applicable.
6. Every accepted row has independently replayed path evidence; aggregate hashes
   without their verified preimages are insufficient.

Rows failing the canonical label contract are excluded from the numerator and
reported by reason. Readiness also requires explicitly verified outcome and
evidence sources. The CLI supplies this only after both append-only stores pass
schema, natural-key, record-hash, and accounting checks and every evidence hash
binds to the corresponding outcome. An unverified mapping, empty outcome store,
unreadable store, corrupt store, orphan evidence, or binding mismatch produces
`evaluation_unavailable`, never readiness.

The report binds the evaluated in-memory snapshot with
`store_verification.canonical_outcomes_sha256`. It also rejects crossed exit
books, an executable side that is disconnected from stored quote-R/gross-R, and
a first-touch timestamp outside the prediction-to-holding interval.

Touch vocabulary is normalized by label contract: `tp1/tp2` and
`target1/target2` are TP, `sl/sl_gap/stop_loss/stop_gap` are SL, and
`terminal/time_exit/session_end` are terminal exits. A touch at prediction time
has an elapsed time of zero. `net-r-v3` side checks rely on the already verified
JPY accounting and entry/exit conversion rates; raw price-distance R
recalculation is used only for `net-r-v2`.

## Raw-first verification boundary

Each new delayed-replay close pins the exact quote-log prefix with byte length
and SHA-256. The independent verifier reads only that prefix, checks the ordered
source-ID and raw-payload hash aggregates, loads every referenced content-
addressed `.bi5` blob, verifies its address, reparses bid/ask ticks, and
recomputes first trigger, exit quote, MAE, and MFE. Only an exact match emits
the creation-time attestation `raw_replayed_research_path`. The attestation also
stores the ordered distinct-blob aggregate.
The data remains explicitly historical, research-only, and non-tradable.

No quality is currently accepted as Phase 2 verified. A persisted attestation
is not a substitute for freshly reopening the pinned quote prefix and raw
preimages at readiness time, and the content-addressed blob store does not yet
carry an immutable `(provider, instrument, hour_start, available_time, raw_sha)`
manifest that can prove an entire hour blob was not omitted. Until both fresh
replay and a complete-hour manifest exist, the gate intentionally returns
`incomplete` even for `raw_replayed_research_path` rows.

Legacy paths without a pinned quote-log prefix remain
`hash_bound_unrecomputed_path` and do not pass the gate. Contemporaneous close
rows that only preserve entry and exit quotes cannot prove
the intervening excursion path. They are persisted as `incomplete`, even when
caller-supplied MAE/MFE values exist. Older `net-r-v2` rows also remain
incomplete when exit sides or path evidence are absent. This is expected
fail-closed behavior and does not invalidate historical gross diagnostics.

Code readiness is not operational evidence. Phase 2 remains incomplete until
readiness itself freshly replays every v2 row against the pinned quote prefix,
immutable raw preimages, and complete-hour manifest. A missing local store, a
legacy unpinned row, an unavailable/corrupt blob, a changed quote prefix, or any
recomputation mismatch remains `evaluation_unavailable` or `incomplete`; none
is inferred as success.
