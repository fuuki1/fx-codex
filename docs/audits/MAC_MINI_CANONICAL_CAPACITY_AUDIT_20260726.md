# Mac mini canonical bid/ask capacity and integrity audit

Audit date: 2026-07-26 (Asia/Tokyo)

Candidate base: `bee7427ec0272fbb2fce85345a22e39e8ceb9cf7` (`origin/main`)

Decision: **LOCAL SYNTHETIC CAPACITY/RECOVERY EVIDENCE PASSED; ABSTAIN FROM
DEPLOYMENT pending target-host retention/restore evidence and independent
adversarial review**

## Remediation result

The rejected per-message raw/segment topology has been replaced in the local
candidate with an insert-only SQLite transaction journal:

- one UTC-day database shard, plus bounded SQLite WAL/SHM companions;
- exact provider bytes committed as `raw_durable` before parsing;
- one atomic terminal transaction for normalized rows and disposition;
- replay exposes only `committed`; incomplete raw remains invisible;
- explicit restart recovery appends `unavailable` without editing raw;
- SHA-256 event continuity crosses daily shard boundaries;
- evidence tables reject update/delete and readers revalidate raw, row,
  terminal binding, counts and chain hashes;
- the production OANDA path disables legacy JSONL and per-message raw mirrors;
- an 8 GiB active-shard ceiling rejects the next raw append before uncontrolled
  daily growth;
- historical, non-active shards can be checkpointed, fully verified, sealed as
  create-only deterministic gzip plus a canonical hash manifest, verified, and
  restored only as a contiguous genesis-anchored prefix;
- archive and restore operations preflight disk working space, reject
  symlinks/collisions/mode drift, never overwrite a different target, and never
  remove the hot source;
- process crash probes cover raw commit, terminal rollback/commit, checkpoint
  and UTC rotation, synthetic capacity rejection, and materializer output fsync
  before checkpoint replacement;
- daily reporting streams journal history and uses a bounded deterministic
  freshness sample; an invalid canonical journal never falls back to legacy
  JSONL;
- future file timestamps, source timestamps and report quote timestamps fail
  closed beyond the explicit five-second skew tolerance.

A local full-logical-day synthetic probe on 2026-07-26 produced:

| Measure | Observed |
|---|---:|
| logical messages / window | 1,382,400 / 2026-01-05 00:00:00–23:59:59.937500 UTC |
| raw profile / parser | representative OANDA-like PRICE JSON (581–589 bytes) / production `parse_price_line` with replay provenance |
| ingest wall / CPU time | 3,928.268961 / 3,393.323483 seconds |
| ingest throughput | 351.911 messages/second |
| documented four-pair comparison rate | 16 messages/second |
| throughput multiple | 21.994× |
| two-transaction latency mean / sampled p50 / p95 / p99 / max | 2.824 / 2.692 / 3.154 / 4.750 / 1,273.209 ms |
| journal files for one day shard | 3 |
| journal bytes / bytes per message | 4,955,021,312 / 3,584.361 |
| observed filesystem consumption | 5,020,069,888 bytes |
| free space before / after | 20,121,878,528 / 15,101,808,640 bytes |
| maximum RSS | 35,602,432 bytes |
| verified replay of 1,382,400 accepted rows | 331.475042 seconds |
| raw-only rows exposed before recovery | 0 |
| append-only unavailable recovery | passed |

The 1,273.209 ms maximum is a real observed local outlier, not removed as
warm-up. Mean and sampled p99 stayed below 5 ms, but target-host canary
monitoring must retain tail latency and writer-stall alerts rather than relying
on aggregate throughput alone.

The observed synthetic journal is 4,955,021,312 bytes/day. A 30-day hot window
at that size is 148,650,639,360 bytes (about 138.4 GiB) before filesystem,
backup and safety headroom. The test volume had only 20,121,878,528 bytes free
at start, so this host cannot support that illustrative hot window as-is.

This is not a production measurement. The payload is representative and parsed
through the production parser, but it was generated locally rather than
captured from OANDA. A small three-day archive/restore probe passed, but a
full-size off-host archive, retention prune, disaster restore and target-host
`ENOSPC` drill remain unproven. The deployment abstention remains in force.

## Local evidence artifacts

| Artifact | SHA-256 | Result |
|---|---|---|
| `CAPTURE_JOURNAL_FULL_DAY_SOAK_20260726.json` | `4d7af0731d061ba65d9132a3ea29bfb49e2d00591df236aea0c25c1cea2f901f` | 1,382,400/1,382,400 replayed; passed |
| `CAPTURE_JOURNAL_SOAK_20260726_10000.json` | `c628a816452ecaafdc52d2b02118da5c2d481f20ef9f7198856c28c8bc1bfe0f` | production parser preflight; passed |
| `CAPTURE_JOURNAL_CRASH_PROBE_20260726.json` | `6237cff1b794cb1cd4bea805bd0a8cec8fa773706cc547a51b96a453882752af` | seven transaction/process boundaries; passed |
| `CAPTURE_JOURNAL_ARCHIVE_RESTORE_PROBE_20260726.json` | `f3f0ecc141eeeccdbeba6863a55dec7b48767cd76e13ccf28aba4ff63f54956c` | two-shard prefix restore; passed |

All four artifacts identify the synthetic evidence class. The capacity
artifacts also record the dirty-worktree state and base revision; they cannot
substitute for an approved clean commit or independent review.

## Continuation audit

The candidate was rechecked against the fetched remote on 2026-07-26:
`HEAD` and `origin/main` both resolved to
`bee7427ec0272fbb2fce85345a22e39e8ceb9cf7` before the candidate changes.
The original developer checkout remained dirty and was not modified.

The first continuation diff audit found that the draft runtime ignore rule
`collect/` also matched the source directory `data_platform/collect/`. That
would have omitted the new journal and archive modules from a normal commit
while leaving local tests able to import them. The rule was corrected to the
repository-root-only `/collect/`, and an ignored-source scan was added to the
pre-commit evidence. Any approval must include
`data_platform/collect/capture_journal.py` and
`data_platform/collect/journal_archive.py`.

A new read-only target-host audit attempt to the configured SSH alias
`trader-mini` failed at authentication (`Permission denied`) before any remote
command ran. Consequently, the prior Mac mini observations have not been
refreshed and current host state remains unknown. No deploy, service change,
credential read, or remote filesystem change was attempted.

## Scope and evidence

This is a code-path and vendor-contract audit. No OANDA credential was present
on the Mac mini, so there is no prospective canonical dataset or dataset hash
to assess. The existing legacy journals are not admissible substitutes for this
decision.

The OANDA v20 pricing documentation states that the account pricing stream can
send at most four prices per second **for each requested instrument**, can omit
intermediate prices, and emits heartbeats every five seconds:

- <https://developer.oanda.com/rest-live-v20/pricing-ep/>
- <https://developer.oanda.com/rest-live-v20/authentication/>

The personal access token is account API access across sub-accounts, not a
documented read-only scope. The candidate therefore treats the token as
potentially trading-capable and relies on a pricing-only `GET` allowlist and
order-path absence tests.

## Rejected candidate record contract

| Layer | Natural key / order | Time fields | Durability and visibility |
|---|---|---|---|
| raw provider payload | SHA-256 content address | local receipt time in ingest ledger | separate immutable file, file and directory `fsync` |
| ingest ledger | global sequence and capture ID | aware UTC `occurred_at` | five append-only hash-chained state rows for a successful capture |
| capture segment | capture ID | aware UTC capture time | separate canonical file; visible only after a hash-bound `COMMITTED` ledger row |
| accepted operational log | provider, instrument, event/receipt time, provider sequence | provider event and local receipt time | append-only compatibility evidence; not the materializer source |
| canonical one-minute row | event time, symbol, decision timeframe | open, event, available and ingested times | content-hashed JSONL; only completed one-minute bars |
| bridge checkpoint | commit sequence and row index | aware UTC update time | atomically replaced; bound to ledger identity/genesis/commit hash and output path |

The integrity tests cover hash-chain tampering, invalid state transitions,
uncommitted/orphan segments, segment substitution, checkpoint rotation,
configuration changes, duplicate replay, aware UTC, completed-bar scope,
crossed quotes, content hashes and natural-key conflicts.

## Rejected topology capacity failure

The draft inherited a per-provider-message storage shape:

- one content-addressed raw file;
- five ingest-ledger rows for the successful state path;
- one capture-segment file;
- one accepted or quarantine JSONL row.

For four instruments, the documented upper bound is:

```text
4 prices/second/instrument × 4 instruments = 16 price messages/second
16 × 86,400 = 1,382,400 price messages/day
1,382,400 × 5 = 6,912,000 ingest-ledger rows/day
1,382,400 raw files + 1,382,400 segment files = 2,764,800 files/day
```

Heartbeats and incident/state files add further records. These are documented
upper-bound calculations, not an assertion that every trading day reaches the
maximum. Even materially lower realized rates would produce excessive inode
growth, directory traversal, full-ledger replay cost and backup/restore burden
over a 30-day evidence window. That rejected design had no proved capacity
envelope, rotation/compaction protocol, retention proof or restore benchmark.

## Other deployment blockers

1. `~/.config/fx-codex/collector.env` is absent on the Mac mini.
2. Two dirty legacy checkouts still track prohibited execution files. They are
   not running, but must be isolated as whole recoverable checkouts before
   canonical activation.
3. Docker is unavailable on the host command path, so container state could not
   be positively verified.
4. The active runtime is dirty. A separate clean `origin/main` candidate passed
   dependency, lint, format, type and full-test checks, but `origin/main` does
   not contain the canonical materializer.
5. The host has no approved off-host archive target or retention budget, and
   the local free-space observation cannot support a 30-day hot window.
6. Material data-path changes still require independent adversarial review.

## Remaining remediation before deployment

Before another deployment attempt:

1. measure provider-captured OANDA message sizes and realized rate on the
   target host without treating the synthetic full-day result as production
   evidence;
2. approve a measured hot-retention budget and off-host/WORM-capable backup,
   then perform a full-size archive verification and genesis-prefix disaster
   restore; source pruning remains prohibited until that evidence exists;
3. inject target-host filesystem `ENOSPC` and process termination around WAL
   commit/checkpoint/UTC rotation while confirming alert persistence and
   deterministic materializer restart;
4. prove prospective no-future timestamps, unique natural keys, aware UTC
   ordering, completed-bar scope and deterministic replay over the operating
   window;
5. obtain an independent material-data review and merge to a clean approved
   SHA;
6. only then isolate legacy checkouts, install the private pricing credential
   file and perform an analysis-only shadow Mac mini canary.

Until these pass, canonical collection and any downstream performance claim are
evaluation-unavailable.
