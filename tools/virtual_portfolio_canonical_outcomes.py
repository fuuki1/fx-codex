#!/usr/bin/env python3
"""Connect every mature virtual trade to the append-only canonical outcome store."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.canonical_outcome_store import (  # noqa: E402
    append_canonical_outcomes,
    load_canonical_outcomes,
    verify_canonical_outcome_store,
)
from fx_intel.canonical_label_evidence import (  # noqa: E402
    CanonicalLabelEvidence,
    HASH_BOUND_LABEL_QUALITY,
    VERIFIED_LABEL_QUALITIES,
    canonical_label_evidence_from_virtual_row,
    validate_evidence_binding,
)
from fx_intel.canonical_label_path_verifier import (  # noqa: E402
    verify_virtual_label_path,
)
from fx_intel.canonical_label_evidence_store import (  # noqa: E402
    CanonicalLabelEvidenceConflict,
    append_canonical_label_evidence,
    load_canonical_label_evidence,
    verify_canonical_label_evidence_store,
)
from fx_intel.evaluation_labels import canonical_net_label_contract_flags  # noqa: E402
from fx_intel.trade_outcome import CanonicalTradeOutcome  # noqa: E402
from fx_intel.trade_outcome import assert_canonical_outcome_idempotent  # noqa: E402
from fx_intel.virtual_portfolio import open_virtual_portfolio_reader  # noqa: E402

DEFAULT_DB = REPO_ROOT / "logs" / "fx_virtual_portfolio.sqlite3"
DEFAULT_STORE = REPO_ROOT / "logs" / "canonical_outcomes.jsonl"
DEFAULT_LABEL_EVIDENCE_STORE = REPO_ROOT / "logs" / "canonical_label_evidence.jsonl"


def _aware(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--as-of must be timezone-aware")
    return parsed.astimezone(UTC)


def _outcome(payload: object) -> CanonicalTradeOutcome:
    if not isinstance(payload, dict):
        raise ValueError("canonical_outcome must be an object")
    prepared = dict(payload)
    for name in ("quality_flags", "cost_quality_flags"):
        value = prepared.get(name)
        if isinstance(value, list):
            prepared[name] = tuple(str(flag) for flag in value)
    outcome = CanonicalTradeOutcome(**prepared)
    flags = canonical_net_label_contract_flags(outcome.to_dict())
    if flags:
        raise ValueError(f"canonical outcome contract violation: {','.join(flags)}")
    return outcome


def connect(
    *,
    database: Path,
    store_path: Path,
    as_of: datetime,
    label_evidence_store_path: Path | None = None,
    quote_log_path: Path | None = None,
    raw_store_root: Path | None = None,
) -> dict[str, object]:
    """Append eligible v3 outcomes and optional versioned label evidence."""

    database_resolved = database.expanduser().resolve(strict=False)
    store_resolved = store_path.expanduser().resolve(strict=False)
    if database_resolved == store_resolved:
        raise ValueError("virtual portfolio database and canonical outcome store must differ")
    if label_evidence_store_path is not None:
        evidence_resolved = label_evidence_store_path.expanduser().resolve(strict=False)
        if evidence_resolved in {database_resolved, store_resolved}:
            raise ValueError(
                "virtual database, canonical outcome store, and label evidence store "
                "must be distinct"
            )
    if (quote_log_path is None) != (raw_store_root is None):
        raise ValueError("quote log and immutable raw store must be supplied together")

    with open_virtual_portfolio_reader(database) as store:
        learning = store.export_learning_rows(as_of=as_of)
    rows = learning.get("rows")
    if not isinstance(rows, list):
        raise ValueError("virtual learning export rows are unavailable")
    eligible = learning.get("eligible_rows")
    if isinstance(eligible, bool) or not isinstance(eligible, int) or eligible < 0:
        raise ValueError("virtual learning eligible denominator is unavailable")
    outcomes = [_outcome(row.get("canonical_outcome")) for row in rows if isinstance(row, dict)]
    if len(outcomes) != len(rows):
        raise ValueError("not every eligible virtual row has a canonical outcome")
    if len(outcomes) != eligible:
        raise ValueError(
            "canonical label export count does not match the eligible trade denominator"
        )
    keys = [outcome.outcome_key for outcome in outcomes]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate canonical virtual outcome key")
    evidence_rows: list[CanonicalLabelEvidence] = []
    raw_replay_attempted = 0
    legacy_prefix_rows = 0
    if label_evidence_store_path is not None:
        for row, outcome in zip(rows, outcomes, strict=True):
            verification = None
            attribution = row.get("attribution") if isinstance(row, dict) else None
            replay = attribution.get("replay_evidence") if isinstance(attribution, dict) else None
            has_pinned_prefix = isinstance(replay, dict) and bool(
                replay.get("source_prefix_sha256")
            )
            if quote_log_path is not None and raw_store_root is not None and has_pinned_prefix:
                raw_replay_attempted += 1
                verification = verify_virtual_label_path(
                    row,
                    quote_log=quote_log_path,
                    raw_store_root=raw_store_root,
                )
            elif quote_log_path is not None and raw_store_root is not None:
                legacy_prefix_rows += 1
            evidence_rows.append(
                canonical_label_evidence_from_virtual_row(
                    row,
                    outcome,
                    path_verification=verification,
                )
            )
    _preflight_existing_stores(
        store_path=store_path,
        outcomes=outcomes,
        label_evidence_store_path=label_evidence_store_path,
        evidence_rows=evidence_rows,
    )
    appended = append_canonical_outcomes(store_path, outcomes)
    persisted = {outcome.outcome_key: outcome for outcome in load_canonical_outcomes(store_path)}
    expected = {outcome.outcome_key: outcome for outcome in outcomes}
    missing = [key for key, outcome in expected.items() if persisted.get(key) != outcome]
    if missing:
        raise ValueError(f"canonical store coverage mismatch for {missing[0][0]}")
    audit = verify_canonical_outcome_store(store_path)
    connected = eligible - len(missing)
    coverage = connected / eligible if eligible else None
    if eligible and coverage != 1.0:
        raise ValueError("canonical virtual outcome coverage is below 100%")
    result: dict[str, object] = {
        "ok": True,
        "contract": "virtual-portfolio-canonical-connection-v1",
        "as_of": as_of.isoformat(),
        "learning_contract": learning.get("contract"),
        "learning_dataset_sha256": learning.get("dataset_sha256"),
        "eligible_rows": eligible,
        "connected_rows": connected,
        "coverage": coverage,
        "appended_rows": appended,
        "store": audit.to_dict(),
        "research_only": True,
        "promotion_eligible": False,
    }
    if label_evidence_store_path is not None:
        evidence_appended = append_canonical_label_evidence(
            label_evidence_store_path,
            evidence_rows,
        )
        persisted_evidence = {
            row.evidence_key: row
            for row in load_canonical_label_evidence(label_evidence_store_path)
        }
        evidence_missing = []
        for evidence, outcome in zip(evidence_rows, outcomes, strict=True):
            stored = persisted_evidence.get(evidence.evidence_key)
            if stored != evidence:
                evidence_missing.append(evidence.evidence_key)
                continue
            validate_evidence_binding(stored, outcome)
        if evidence_missing:
            raise ValueError(
                f"canonical label evidence coverage mismatch for {evidence_missing[0][0]}"
            )
        evidence_audit = verify_canonical_label_evidence_store(label_evidence_store_path)
        complete_rows = sum(row.label_quality in VERIFIED_LABEL_QUALITIES for row in evidence_rows)
        hash_bound_rows = sum(
            row.label_quality == HASH_BOUND_LABEL_QUALITY for row in evidence_rows
        )
        result["label_evidence"] = {
            "contract": "virtual-portfolio-canonical-label-evidence-connection-v1",
            "connected_rows": eligible - len(evidence_missing),
            "coverage": (1.0 if eligible else None),
            "appended_rows": evidence_appended,
            "hash_bound_unrecomputed_path_rows": hash_bound_rows,
            "hash_bound_unrecomputed_path_coverage": (
                hash_bound_rows / eligible if eligible else None
            ),
            "phase2_verified_path_rows": complete_rows,
            "phase2_verified_path_coverage": (complete_rows / eligible if eligible else None),
            "raw_replay_attempted_rows": raw_replay_attempted,
            "legacy_unpinned_prefix_rows": legacy_prefix_rows,
            "store": evidence_audit.to_dict(),
        }
    return result


def _preflight_existing_stores(
    *,
    store_path: Path,
    outcomes: list[CanonicalTradeOutcome],
    label_evidence_store_path: Path | None,
    evidence_rows: list[CanonicalLabelEvidence],
) -> None:
    """Reject existing corruption/conflicts before mutating either JSONL store."""

    if store_path.exists():
        existing_outcomes = {
            outcome.outcome_key: outcome for outcome in load_canonical_outcomes(store_path)
        }
        for outcome in outcomes:
            prior_outcome = existing_outcomes.get(outcome.outcome_key)
            if prior_outcome is not None:
                assert_canonical_outcome_idempotent(prior_outcome, outcome)
    if label_evidence_store_path is None or not label_evidence_store_path.exists():
        return
    existing_evidence = {
        evidence.evidence_key: evidence
        for evidence in load_canonical_label_evidence(label_evidence_store_path)
    }
    for evidence in evidence_rows:
        key = evidence.evidence_key
        prior_evidence = existing_evidence.get(key)
        if prior_evidence is not None and prior_evidence != evidence:
            raise CanonicalLabelEvidenceConflict(
                "conflicting canonical label evidence for " f"{key[0]} + {key[1]} + {key[2]}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Connect mature virtual fills to canonical full-cost net-R labels"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument(
        "--label-evidence-store",
        type=Path,
        default=DEFAULT_LABEL_EVIDENCE_STORE,
    )
    parser.add_argument("--as-of")
    parser.add_argument(
        "--quote-log",
        type=Path,
        help="pinned append-only Dukascopy quote JSONL used for independent path replay",
    )
    parser.add_argument(
        "--raw-store",
        type=Path,
        help="immutable content-addressed raw blob root used for independent path replay",
    )
    args = parser.parse_args()
    try:
        result = connect(
            database=args.db.expanduser().resolve(),
            store_path=args.store.expanduser().resolve(),
            as_of=_aware(args.as_of),
            label_evidence_store_path=args.label_evidence_store.expanduser().resolve(),
            quote_log_path=(args.quote_log.expanduser().resolve() if args.quote_log else None),
            raw_store_root=(args.raw_store.expanduser().resolve() if args.raw_store else None),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
