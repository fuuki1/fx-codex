# PR #71 Mac mini isolated functional test

Test date: 2026-07-26 (Asia/Tokyo)

Candidate: `122b04b62e8f3cf50c23a861107a85cefa3cdc47`

Host: `trader-mini`, macOS 26.5.2, Apple silicon (`arm64`)

Decision: **ISOLATED FUNCTIONAL TEST PASSED; CANONICAL DEPLOYMENT PREFLIGHT
CORRECTLY REFUSED LEGACY EXECUTION CHECKOUTS; NOT DEPLOYED**

## Scope and invariants

The test used a new create-only detached checkout at
`/Users/fuuki/fx-codex-isolated/pr71-122b04b`. Evidence and all generated test
outputs were written outside that checkout under
`/Users/fuuki/fx-codex-isolated/evidence/pr71-122b04b-20260726T052219Z`.

The active runtime `/Users/fuuki/srv/fx-codex` was read only. Before and after
the test it remained on `bee7427ec0272fbb2fce85345a22e39e8ceb9cf7`,
branch `deploy/main-20260723`, with three pre-existing dirty entries.

No real OANDA or Discord credential was loaded. Collector dry-run used a
dedicated dummy environment file with mode `0600`; it produced no quote output.
No service was installed, restarted, enabled or unloaded. Before and after the
test:

- canonical launchd labels: 0;
- canonical launchd plist files: 0;
- canonical collector/materializer/freshness processes: 0;
- prohibited execution processes: 0.

## Environment construction

The macOS system Python 3.9.6 and its pip 21.2.4 could not resolve the locked
`hatchling==1.31.0`. The test did not relax or edit the lock. It used the
already-installed `/opt/homebrew/bin/python3.12` (Python 3.12.13), created a
fresh venv, then completed:

```text
pip install --require-hashes -r requirements.lock
pip install --no-deps --no-build-isolation .
pip check
```

The resulting candidate checkout was still clean and at the exact approved
SHA. The runtime/test lock intentionally does not install the development-only
ruff, black and mypy tools; those checks were completed on the same SHA in the
development validation recorded by
`PR71_ADVERSARIAL_REMEDIATION_20260726.md`.

## Results

| Check | Result |
|---|---|
| Hash-pinned clean environment and package build | passed |
| Full pytest on Mac mini | 1,100 passed, 1 skipped in 73.90 seconds |
| `compileall` | passed |
| Synthetic sample backtest | passed as a functional check only; inadmissible for performance claims |
| Collector launchd dry-run with dummy credentials | exit 0 |
| Canonical topology dry-run | exit 78, expected fail-closed refusal |
| 10,000-message synthetic journal soak | passed; 10,000/10,000 replayed; 385.29 msg/s; 24.081 times the documented comparison rate |
| Process crash probe | passed at all seven boundaries |
| Archive/restore probe | passed; two archives verified and restored as a matching prefix |
| Candidate worktree after tests | exact SHA; clean |

The soak used representative generated OANDA-shaped PRICE payloads and the
production parser. It is not provider-captured evidence. Its RSS scope is the
probe process, not the production collector daemon.

## Deployment preflight refusal

The collector-only dry-run passed. The enclosing canonical topology dry-run
refused installation before any launchd write:

```text
拒否: legacy execution checkoutを先に隔離してください: /Users/fuuki/Desktop/fx-codex
```

Both `/Users/fuuki/Desktop/fx-codex` and `/Users/fuuki/fx-codex` remain dirty
legacy checkouts and each tracks 61 prohibited execution paths. This is the
expected safety-gate result. It prevents Ready/merge/deployment treatment until
those checkouts are separately quarantined through an approved, reversible
migration.

## Evidence

| Artifact | SHA-256 |
|---|---|
| `pytest.log` (retained on isolated host) | `a7ebe96c8cb00c4f17e9259f6831830ea93516a0738cfbf9b6aa0aa325f23ec9` |
| `collector-dry-run.log` (retained on isolated host) | `bdef9cdc5f07a5e84262b1783469dcbf22ee0171ea4d3e07ac355fcf7ba4eb77` |
| `canonical-dry-run.log` (retained on isolated host) | `8b2f5a26c0558c597693e4bca4035b7397bf0984477717db75eb371df46a0f03` |
| `MAC_MINI_PR71_ISOLATED_SOAK_20260726_10000.json` | `9f69bbff1d3fb20d7529dd411aecd6e7e8ddd010577dd60ea5730cf96b34d1ee` |
| `MAC_MINI_PR71_ISOLATED_CRASH_PROBE_20260726.json` | `4477fee2d669520121396d610b179771d4726638ca2fffd5571530e57ebfc6e4` |
| `MAC_MINI_PR71_ISOLATED_ARCHIVE_PROBE_20260726.json` | `5ea27516984e7a988ee50560a1b2ce5a916e7fd776c9e40b7e8191b0d7c1494f` |

## Remaining acceptance blockers

- legacy execution checkouts have not been quarantined;
- no provider-captured sizing or production-daemon RSS soak has passed;
- no target-filesystem `ENOSPC` drill, full-size off-host archive or independent
  restore has passed;
- no independent secondary price source is connected;
- the qualifying 30-trading-day prospective window remains at zero;
- no independent formal approval has been recorded.

The test is evidence for checklist item 4 only. It does not authorize Ready
conversion, merge or deployment.
