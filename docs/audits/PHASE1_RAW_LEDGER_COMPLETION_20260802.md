# Phase 1 Raw Ledger completion — 2026-08-02

Phase 1 implementation: **COMPLETE**. Production activation: **NOT AUTHORIZED**. The Raw Ledger
remains behind the default-off `FX_RAW_LEDGER_ENABLED` flag. No Mac mini service, environment
file, launchd definition, writer, database, journal, or feature flag was changed during this
completion run.

Superseding post-review evidence ID: `20260802T041211Z-phase1-post-review` (UTC). The mode-`0700`
audit root is:

- `/Users/takahashifuuki/fx-codex-audit/20260802T041211Z-phase1-post-review`

The evidence contains a code inventory, validation summary, synthetic functional trial, exact
payload blobs, append-only metadata database, create-only backup, and verified restore. The trial
is explicitly synthetic and the backup is on the same device; neither is activation evidence.

## Completion decision

| Phase 1 condition | Result | Evidence |
|---|---:|---|
| Existing path is unaffected | PASS | Flag defaults false; disabled-hook comparison preserves existing quote JSONL byte-for-byte and creates no ledger path |
| Every target symbol can be recorded | PASS | One integration test and the sealed synthetic trial record `USDJPY`, `EURUSD`, and `GBPUSD`; required-symbol health is `ok` |
| Redelivery is idempotent | PASS | Exact delivery replay keeps one event and increments `raw_idempotent`; reconnect delivery is separately retained and classified as a duplicate |
| Receive loss is measurable | PASS | Live metrics expose raw attempted/stored/idempotent/failed/overflow counters and raw failure/overflow ratios separately from annotations |
| Append-only raw ledger | PASS | Content-addressed exact bytes, crash-atomic SQLite appends, update/delete/replace-denying triggers, record hashes, and integrity checks |
| Provenance and payload hash | PASS | Every RawEvent requires non-empty provenance and an exact lower-case SHA-256 matching the payload bytes |
| Timestamp semantics | PASS | Event, publication, receipt, ingestion, validity, ledger-recorded, annotation, and conservative availability clocks are distinct and aware UTC |
| Duplicate/quarantine annotation | PASS | Scoped source identity detects duplicates/revisions; post-parse quarantine is a separate append-only annotation |
| Retention and backup | PASS (implementation) | Create-only online SQLite snapshot, referenced blobs, manifest/completion ordering, directory fsync, and full read-only restore verification |
| Production activation | BLOCKED | Requires a clean Mac mini release plus prospective capacity, independent retention, freshness, alert, clock, writer, and signed attestation evidence |

“Every target symbol” above is a functional acceptance claim, not a statement that production
shadow collection is active. The integration plan explicitly requires production activation to
be a separate PR and explicit approval. Phase 0 also found the Mac mini primary checkout unsuitable
as a release candidate, so this run did not deploy or enable it.

## Implemented boundary

The read-only OANDA pricing collector preserves the source bytes through the existing raw-first
store before invoking the optional adapter. With the flag enabled in a separately approved
environment, `OandaRawShadowHooks` queues RawEvent metadata to a bounded daemon worker. Lock or
disk failures do not propagate into the accepted/quarantine quote path; loss is visible in
counters and a hashed diagnostic path. The adapter has no decision, label, notification, account
risk, position, or broker-order authority.

The ledger stores exact payload bytes by SHA-256 before metadata. A repeated `event_id` with the
same evidence is idempotent and conflicting evidence fails closed. Source duplicates and revisions
use `source + source_event_id_scope + source_event_id`; OANDA account identity is hashed before it
enters the scope. Quarantine annotations never rewrite the receipt row.

PIT availability is at least `max(ingest_time, ledger_recorded_at)`. Source event/publication time
never backdates local availability. Unknown source clocks remain null. The local clock evidence is
research/shadow evidence only, not externally attested promotion-grade availability.

## Post-review corrections

The initial acceptance run found four failures caused by APFS/File Provider changing ctime when a
dataless virtual-environment file was first materialized. The trusted-file reader discards one
unstable observation, reopens the same no-follow path, and requires the second read to be stable
across device, inode, size, mtime, and ctime. Continuous change remains a hard failure, unsafe
owner/mode/path state remains rejected, and stable bytes must still match the reviewed dependency
lock. Regression tests cover both the one-time materialization and continuous-change cases.

The independent review also closed flag-off transport/clock/wrapper drift, private-env symlink and
TOCTOU races, trusted-parent enforcement, exact loaded-env digest binding, duplicate argparse path
options, and misleading loaded-code claims. Code evidence is now explicitly current on-disk
evidence; production still requires a sealed release. Health and stats use one WAL-aware SQLite
read transaction, validate both stored and input digests, and are documented as logically
read-only because SQLite may manage WAL/SHM sidecars.

## Validation

| Check | Result |
|---|---:|
| Post-review Raw Ledger/collector/backup/runtime/preflight slice | 113 passed |
| Post-review Raw Ledger subrun | 33 passed |
| PIT/price-history/labeling data-integrity slice | 51 passed before post-review; current rerun blocked during dependency import |
| Ruff | PASS |
| Black | PASS — 350 Python files observed |
| Mypy | PASS — canonical repository targets |
| Monolithic pytest attempt | RESOURCE EXHAUSTED — 1,633 passed, 46 child-process SIGKILL failures, 1 skipped |
| Isolated replay of every failed node | PASS |

The post-review PIT rerun remained inside pandas/C-extension materialization for more than nine
minutes and was terminated before collection; no test item or assertion ran. Its last 51-test pass
is retained as prior composed evidence, and the post-review changes do not implement PIT,
price-history, or labeling behavior.

The monolithic run's 46 failures shared child-process exit `-9` under local resource/File Provider
pressure. They were not assertion failures in the Phase 1 logic. After the monolithic process
ended, every failed node passed under lower pressure: the full Fusion/operations group passed
45 tests, the Phase 0/technical-cache/virtual-portfolio residual group passed 10 tests, and the
preflight/runtime failures are included in the superseding 113-pass Phase 1 slice. No failing node
remained.
This is a composed validation result, not a claim that one monolithic invocation exited zero.

The superseding synthetic functional trial contains four stored receipts: one for each required pair plus one
malformed payload, one idempotent replay, and one quarantine annotation. Health was `ok`, raw
failure and overflow ratios were `0.0`, and backup restore health was `ok`. Its report SHA-256 is
`bd3ac7ff51278a2801845d09a98b7e249f38421b2f166fc982c78f4b5c3a930d`.

The code inventory SHA-256 is
`b16662451eb3867c02c5bde5efe53ead404bcb3ff7d1a4b97b2eacf9d6e5ccfa`, and the validation summary
SHA-256 is `070775e08f337b929b466f34ec1db2e1fd4e870fdb45b043071712c0a9552526`.

## Independent review

The post-review tree and superseding evidence received independent **APPROVE**. The reviewer
recomputed the inventory and all 28 registered file hashes/sizes, verified the report and
validation hashes, checked backup manifest/completion/database consistency, ran SQLite
`quick_check`, and verified all four content-addressed blobs. No unresolved P0–P3 Phase 1 code
finding remains. This approval is for implementation completion only; production activation
remains `NOT_AUTHORIZED`.

## Acceptance criteria mapping

1. Feature flag default false: PASS.
2. Disabled output byte-level/semantic invariance: PASS.
3. Raw payload preserved unchanged: PASS.
4. Hash and provenance present: PASS.
5. Timestamp semantics documented: PASS.
6. Same-event replay idempotent: PASS.
7. Quarantine evidence retained: PASS.
8. Shadow failure cannot break existing processing: PASS.
9. Failure metrics and hashed error log exist: PASS.
10. Unit, integration, and restart tests pass: PASS.
11. Ruff, Black, Mypy, and pytest coverage pass by the composed result above: PASS with the
    documented monolithic resource limitation.
12. Rollback is documented: PASS.
13. Existing dirty changes were preserved and not normalized: PASS.
14. Before/after data-flow diagrams are present in `RAW_LEDGER_PHASE_0_1.md`: PASS.
15. Production enablement requires a separate PR and explicit approval: PASS.

## Rollback and residual gates

Rollback is to keep or restore `FX_RAW_LEDGER_ENABLED=false`, or remove the explicit adapter from
the read-only collector in a reviewed code change. Existing raw-ledger bytes are retained for
audit; they are never deleted or rewritten during rollback. Existing accepted/quarantine quote
outputs remain authoritative until a later architecture phase explicitly changes that contract.

Before any production shadow activation, all Phase 0 residual gates remain mandatory: clean
release deployment, unique writer proof, owner-only storage, prospective three-symbol coverage,
reviewed loss thresholds, capacity and growth evidence, independently retained backup, restore,
freshness alert delivery, clock evidence, clean deployed SHA, and independent signed attestation.
The preflight is read-only and cannot change the feature flag or restart a service.
