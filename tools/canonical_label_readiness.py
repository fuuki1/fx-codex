#!/usr/bin/env python3
"""Audit Phase 2 canonical-label coverage from the verified outcome store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fx_intel.canonical_label_readiness import (  # noqa: E402
    assess_canonical_label_readiness,
)
from fx_intel.canonical_outcome_store import (  # noqa: E402
    CanonicalOutcomeStoreCorruption,
    CanonicalOutcomeStoreUnavailable,
    load_canonical_outcomes,
)
from fx_intel.canonical_label_evidence_store import (  # noqa: E402
    CanonicalLabelEvidenceStoreCorruption,
    CanonicalLabelEvidenceStoreUnavailable,
    load_canonical_label_evidence,
)

DEFAULT_STORE = REPO_ROOT / "logs" / "canonical_outcomes.jsonl"
DEFAULT_LABEL_EVIDENCE_STORE = REPO_ROOT / "logs" / "canonical_label_evidence.jsonl"
EXIT_INCOMPLETE = 3
EXIT_UNAVAILABLE = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only Phase 2 gate. Verifies the canonical outcome store and "
            "reports bid/ask, cost, target-field, and non-zero net-R coverage."
        )
    )
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument(
        "--label-evidence-store",
        type=Path,
        default=DEFAULT_LABEL_EVIDENCE_STORE,
    )
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.report is not None and _normalized_path(args.report) == _normalized_path(args.store):
        parser.error("--report must not resolve to the canonical --store path")
    if _normalized_path(args.label_evidence_store) == _normalized_path(args.store):
        parser.error("--label-evidence-store must differ from the canonical --store path")
    if args.report is not None and _normalized_path(args.report) == _normalized_path(
        args.label_evidence_store
    ):
        parser.error("--report must not resolve to the --label-evidence-store path")
    store_verification: dict[str, object]
    evidence_store_verification: dict[str, object]
    try:
        outcomes = load_canonical_outcomes(args.store)
        store_verification = {
            "verified": True,
            "path": str(args.store),
            "entry_count": len(outcomes),
            "canonical_outcomes_sha256": _canonical_outcomes_sha256(outcomes),
        }
    except (
        CanonicalOutcomeStoreUnavailable,
        CanonicalOutcomeStoreCorruption,
        OSError,
        UnicodeError,
    ) as error:
        store_verification = {
            "verified": False,
            "path": str(args.store),
            "entry_count": None,
            "canonical_outcomes_sha256": None,
        }
        readiness = assess_canonical_label_readiness(
            (), source_verified=False, source_error=str(error)
        )
        evidence_store_verification = {
            "verified": False,
            "path": str(args.label_evidence_store),
            "entry_count": None,
            "canonical_evidence_sha256": None,
        }
    else:
        try:
            evidence = load_canonical_label_evidence(args.label_evidence_store)
            evidence_store_verification = {
                "verified": True,
                "path": str(args.label_evidence_store),
                "entry_count": len(evidence),
                "canonical_evidence_sha256": _canonical_evidence_sha256(evidence),
            }
            readiness = assess_canonical_label_readiness(
                outcomes,
                source_verified=True,
                label_evidence=evidence,
                evidence_source_verified=True,
            )
        except (
            CanonicalLabelEvidenceStoreUnavailable,
            CanonicalLabelEvidenceStoreCorruption,
            OSError,
            UnicodeError,
        ) as error:
            evidence_store_verification = {
                "verified": False,
                "path": str(args.label_evidence_store),
                "entry_count": None,
                "canonical_evidence_sha256": None,
            }
            readiness = assess_canonical_label_readiness(
                outcomes,
                source_verified=True,
                label_evidence=(),
                evidence_source_verified=False,
                evidence_source_error=str(error),
            )

    payload = readiness.to_dict()
    payload["store_verification"] = store_verification
    payload["evidence_store_verification"] = evidence_store_verification
    if args.report is not None:
        _atomic_write_json(args.report, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
    if readiness.status == "evaluation_unavailable":
        return EXIT_UNAVAILABLE
    return 0 if readiness.phase2_complete else EXIT_INCOMPLETE


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _canonical_outcomes_sha256(outcomes: list[Any]) -> str:
    payload = [outcome.to_dict() for outcome in sorted(outcomes, key=lambda row: row.outcome_key)]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_evidence_sha256(evidence_rows: list[Any]) -> str:
    payload = [
        evidence.to_dict() for evidence in sorted(evidence_rows, key=lambda row: row.evidence_key)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


if __name__ == "__main__":
    raise SystemExit(main())
